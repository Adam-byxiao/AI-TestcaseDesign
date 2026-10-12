import {useEffect,useState} from 'react';
import {Card,List,Button,Tag,Space,Modal,Select,InputNumber,Empty,message,Switch,Form,Tooltip} from 'antd';
import {ThunderboltOutlined,PlayCircleOutlined} from '@ant-design/icons';
import {Link} from 'react-router-dom';
import {prdApi,runApi} from '../api';
import type {Prd,Run} from '../types';

export default function Dashboard(){
  const [runs,setRuns]=useState<Run[]>([]); const [prds,setPrds]=useState<Prd[]>([]);
  const [prdId,setPrdId]=useState<string>(); const [open,setOpen]=useState(false);
  const [busy,setBusy]=useState(false);
  // 正式工作流默认使用真实模型。离线 Stub 只能由用户在创建运行时显式开启，
  // 避免开发期的固定样例悄悄成为生产产物。
  const [jobs,setJobs]=useState(4); const [stub,setStub]=useState(false); const [profile,setProfile]=useState('generic');
  const load=async()=>{try{setRuns(await runApi.list());setPrds(await prdApi.list())}catch{message.error('无法加载工作区状态')}};
  useEffect(()=>{load()},[]);
  const sessionId=()=>sessionStorage.getItem('prd2tcSessionId')||undefined;
  const create=async()=>{if(!prdId){message.warning('请选择 PRD');return} const sid=sessionId();
    if(!stub&&!sid){message.error('请先在模型配置中保存当前 LLM 会话');return}
    try{const r=await runApi.create({prdId,stub,jobs,profile,sessionId:sid});setOpen(false);message.success('任务已创建');load();window.location.href=`/runs/${r.taskId}`}catch(error:any){message.error(error?.response?.data?.detail||'创建运行失败')}};
  /** 一键运行：用最新上传的 PRD + 当前 LLM 会话，以 auto 模式一次跑完 分析→方案→用例（跳过逐阶段人工审阅）。 */
  const oneClick=async()=>{
    if(!prds.length){message.warning('请先在「PRD 编辑」上传一份 PRD');return}
    const sid=sessionId();
    if(!sid){message.error('请先在模型配置中保存当前 LLM 会话');return}
    const target=prds[0]; setBusy(true);
    try{const r=await runApi.create({prdId:target.prdId,auto:true,stub:false,jobs:4,profile:'generic',sessionId:sid});
      message.success(`已启动：${target.filename} → 自动跑完全流程`); window.location.href=`/runs/${r.taskId}`}
    catch(error:any){message.error(error?.response?.data?.detail||'一键运行失败')}
    finally{setBusy(false)}
  };
  return <div className="page"><Space direction="vertical" size="large" style={{width:'100%'}}>
    <Card title="运行工作台"
      extra={<Space>
        <Link to="/prd"><Button>管理 PRD</Button></Link>
        <Tooltip title="使用最新 PRD 与当前 LLM 会话一次跑完分析→方案→用例（并发 4）；自动模式会跳过逐阶段人工审阅，审阅记录标记为 one-click"><Button type="primary" icon={<ThunderboltOutlined/>} loading={busy} onClick={oneClick} disabled={!prds.length||!sessionId()}>一键运行</Button></Tooltip>
        <Button icon={<PlayCircleOutlined/>} onClick={()=>setOpen(true)}>新建运行（逐阶段审阅）</Button>
      </Space>}>
      <List dataSource={runs} locale={{emptyText:<Empty description="暂无运行"/>}}
        renderItem={r=><List.Item actions={[<Link key="open" to={`/runs/${r.taskId}`}>打开</Link>]}>
          <List.Item.Meta title={r.runId||r.taskId}
            description={<Space size={6} wrap>
              <span>{r.currentStage||'-'} · {r.percent||0}%</span>
              {r.auto&&<Tag color="cyan">一键</Tag>}
              {r.mode&&<Tag color={r.mode==='stub'?'gold':'blue'}>模式：{r.mode==='stub'?'Stub 离线验收':'真实模型'}</Tag>}
              {r.llm&&<span>{r.llm.provider}/{r.llm.model}</span>}
            </Space>} />
          <Tag color={r.status==='succeeded'?'green':r.status==='failed'?'red':r.status==='awaiting_review'?'orange':'blue'}>{r.status}</Tag>
        </List.Item>}/>
    </Card>
  </Space>
  <Modal title="新建运行（逐阶段审阅）" open={open} onCancel={()=>setOpen(false)} onOk={create}>
    <Form layout="vertical">
      <Form.Item label="PRD"><Select style={{width:'100%'}} placeholder="选择 PRD" onChange={setPrdId} options={prds.map(p=>({value:p.prdId,label:`${p.filename} (${p.version})`}))}/></Form.Item>
      <Form.Item label="运行模式"><Space><Switch checked={stub} onChange={setStub} checkedChildren="Stub" unCheckedChildren="真实模型"/><span>{stub?'离线 Stub 模式：仅用于验证流程，不代表真实生成质量':'正式运行需要先在模型配置中保存当前 LLM 会话；如仅验证流程，请显式开启 Stub 模式。'}</span></Space></Form.Item>
      <Form.Item label="Profile"><Select value={profile} onChange={setProfile} options={[{value:'generic',label:'通用（动态模块）'},{value:'ecommerce',label:'电商'}]}/></Form.Item>
      <Form.Item label="模块并发数" extra="模块/分段并行执行；实测串行 5 模块约 1013s，并发 5 约 332s。过高可能触发 Provider 限流。"><InputNumber min={1} max={8} value={jobs} onChange={v=>setJobs(v||1)} /></Form.Item>
    </Form>
  </Modal></div>
}
