import {useState} from 'react';
import {Card,Form,Select,Input,InputNumber,Button,Alert,Space,Typography,message} from 'antd';
import {llmApi} from '../api';

type Probe = {ok:boolean;kind:string;message:string;hint?:string;endpoint?:string;
  http_status?:number|null;model?:string;suggested_base?:string;latency_ms?:number};

const KIND_LABEL:Record<string,string>={
  ok:'连接成功',config:'配置不完整',auth:'鉴权失败',endpoint:'接口地址不正确',
  model:'模型不可用',rate_limit:'触发限流',server:'服务端错误',network:'网络不可达',malformed:'探测失败'};

function ProbeAlert({probe}:{probe:Probe}){
  const type=probe.ok?'success':'error';
  return <Alert style={{marginTop:16}} type={type} showIcon
    message={`${KIND_LABEL[probe.kind]||probe.kind}：${probe.message}`}
    description={<Space direction="vertical" size={4} style={{width:'100%'}}>
      {probe.hint&&<Typography.Text type="secondary">{probe.hint}</Typography.Text>}
      {probe.endpoint&&<Typography.Text code copyable>{probe.endpoint}</Typography.Text>}
      {probe.suggested_base&&<Typography.Text>建议 API Base：<Typography.Text code copyable>{probe.suggested_base}</Typography.Text></Typography.Text>}
      <Typography.Text type="secondary">
        {probe.http_status?`HTTP ${probe.http_status} · `:''}{probe.latency_ms!=null?`${probe.latency_ms}ms`:''}
      </Typography.Text>
    </Space>}/>;
}

export default function Settings(){
  const [provider,setProvider]=useState('openai');
  const [probe,setProbe]=useState<Probe|undefined>();
  const [saved,setSaved]=useState(false);
  const [testing,setTesting]=useState(false);
  const [form]=Form.useForm();

  const payload=(values:any)=>({...values,provider,model:values.model||undefined,apiBase:values.apiBase||undefined});
  const explain=(error:any,fallback:string)=>{
    const status=error?.response?.status; const detail=error?.response?.data?.detail;
    // 旧版后端没有 verify 路由，也不做保存前探测；提示重启而不是显示裸的 "Not Found"
    if(status===404&&(detail==='Not Found'||detail==null))
      return '后端未提供该检测接口：服务可能仍是旧版本，请重启后端（npm start 或 uvicorn）后重试';
    if(!error?.response) return '无法连接后端服务，请确认后端已启动（默认 127.0.0.1:8000）';
    if(detail&&typeof detail==='object'){setProbe(detail as Probe);return detail.notice||detail.message||fallback}
    return typeof detail==='string'?detail:fallback;
  };

  const submit=async(values:any)=>{
    setSaved(false);
    try{
      const result=await llmApi.session(payload(values));
      sessionStorage.setItem('prd2tcSessionId',result.sessionId);
      sessionStorage.setItem('prd2tcProvider',JSON.stringify({provider:result.provider,model:result.model,apiBase:result.apiBase}));
      setSaved(true);
      if(!result.probe){
        // 旧版后端不做保存前探测：绝不能谎报「检测通过」
        setProbe({ok:false,kind:'malformed',message:'服务端未执行配置检测',
          hint:'后端可能是旧版本（未做保存前探测），本次配置未经验证；请重启后端后重新保存'});
        message.warning('已保存，但后端未执行检测，配置未经验证');
        return;
      }
      setProbe(result.probe);
      message.success('检测通过，会话配置已保存，Key 不会写入磁盘');
    }catch(error:any){setProbe(undefined);message.error(explain(error,'保存配置失败'))}
  };

  const test=async()=>{
    setTesting(true);setSaved(false);
    try{
      const values=await form.validateFields();
      const result=await llmApi.verify(payload(values));
      setProbe(result);
      message[result.ok?'success':'error'](result.ok?'检测通过':(KIND_LABEL[result.kind]||'检测未通过'));
    }catch(error:any){
      if(error?.errorFields)return;
      setProbe(undefined);message.error(explain(error,'检测失败'));
    }finally{setTesting(false)}
  };

  return <div className="page"><Card title="LLM Provider 会话配置">
    <Alert type="info" showIcon style={{marginBottom:16}}
      message="保存前会真实探测一次端点"
      description="检测不通过将拒绝保存，避免错误配置到运行阶段才以无信息量的形式暴露。Key 仅存于服务端内存。"/>
    <Form form={form} layout="vertical" onFinish={submit} initialValues={{model:'',temperature:.4,timeout:120,apiBase:''}}>
      <Form.Item label="Provider"><Select value={provider} onChange={v=>{setProvider(v);setProbe(undefined);setSaved(false)}} options={['openai','anthropic','ollama'].map(v=>({value:v,label:v}))}/></Form.Item>
      <Form.Item label="模型" name="model"><Input placeholder="留空使用 Provider 默认模型"/></Form.Item>
      <Form.Item label="API Base" name="apiBase" extra="OpenAI 兼容端点通常需要以 /v1 结尾，例如 https://api.minimax.chat/v1">
        <Input placeholder="留空使用 Provider 默认地址"/></Form.Item>
      <Form.Item label="API Key（仅本次会话）" name="apiKey"><Input.Password autoComplete="off" disabled={provider==='ollama'} placeholder={provider==='ollama'?'Ollama 无需 Key':'不会回显或持久化'}/></Form.Item>
      <Form.Item label="温度" name="temperature"><InputNumber min={0} max={2} step={.1}/></Form.Item>
      <Form.Item label="超时（秒）" name="timeout"><InputNumber min={10} max={1800}/></Form.Item>
      <Space>
        <Button type="primary" htmlType="submit">检测并保存会话配置</Button>
        <Button onClick={test} loading={testing}>仅检测连接</Button>
      </Space>
    </Form>
    {probe&&<ProbeAlert probe={probe}/>}
    {saved&&<Alert style={{marginTop:12}} type="success" showIcon message="已保存到服务端内存，会话过期或服务重启后失效"/>}
  </Card></div>;
}
