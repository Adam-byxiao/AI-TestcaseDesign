import {useState} from 'react';
import {Card,Form,Select,Input,InputNumber,Button,Alert,message} from 'antd';
import {llmApi} from '../api';

export default function Settings(){
  const [provider,setProvider]=useState('openai'); const [saved,setSaved]=useState(false);
  const submit=async(values:any)=>{try{const result=await llmApi.session({...values,provider}); sessionStorage.setItem('prd2tcSessionId',result.sessionId); sessionStorage.setItem('prd2tcProvider',JSON.stringify({provider:result.provider,model:result.model,apiBase:result.apiBase})); setSaved(true); message.success('会话配置已保存，Key 不会写入磁盘')}catch(error:any){message.error(error?.response?.data?.detail||'保存配置失败')}};
  return <div className="page"><Card title="LLM Provider 会话配置"><Form layout="vertical" onFinish={submit} initialValues={{model:'',temperature:.4,timeout:120,apiBase:''}}><Form.Item label="Provider"><Select value={provider} onChange={setProvider} options={['openai','anthropic','ollama'].map(v=>({value:v,label:v}))}/></Form.Item><Form.Item label="模型" name="model"><Input placeholder="留空使用 Provider 默认模型"/></Form.Item><Form.Item label="API Base" name="apiBase"><Input placeholder="留空使用 Provider 默认地址"/></Form.Item><Form.Item label="API Key（仅本次会话）" name="apiKey"><Input.Password autoComplete="off" disabled={provider==='ollama'} placeholder={provider==='ollama'?'Ollama 无需 Key':'不会回显或持久化'}/></Form.Item><Form.Item label="温度" name="temperature"><InputNumber min={0} max={2} step={.1}/></Form.Item><Form.Item label="超时（秒）" name="timeout"><InputNumber min={10} max={1800}/></Form.Item><Button type="primary" htmlType="submit">保存会话配置</Button>{saved&&<Alert style={{marginTop:16}} type="success" message="已保存到服务端内存，会话过期或服务重启后失效"/>}</Form></Card></div>
}
