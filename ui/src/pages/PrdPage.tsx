import {useEffect,useState} from 'react';
import {Card,Select,Upload,Button,Input,Row,Col,Typography,message,Space,Alert} from 'antd';
import {useSearchParams,useNavigate} from 'react-router-dom';
import ReactMarkdown from 'react-markdown'; import remarkGfm from 'remark-gfm';
import {prdApi} from '../api'; import type {Prd} from '../types';

export default function PrdPage(){
  const [items,setItems]=useState<Prd[]>([]); const [current,setCurrent]=useState<Prd&{content:string;versions?:any[]}>(); const [text,setText]=useState(''); const [lastRun,setLastRun]=useState<string>();
  const [params]=useSearchParams(); const navigate=useNavigate();
  const load=async()=>{try{const values=await prdApi.list();setItems(values);const requested=params.get('id');if(requested)choose(requested)}catch{message.error('无法加载 PRD 列表')}};
  const choose=async(id:string)=>{try{const p=await prdApi.get(id);setCurrent(p);setText(p.content)}catch{message.error('无法加载 PRD')}};
  useEffect(()=>{load()},[]);
  const save=async()=>{if(current){try{const p=await prdApi.update(current.prdId,text);setCurrent({...current,...p,content:text});await load();message.success(`已保存 ${p.version} 新 PRD 版本`)}catch{message.error('保存 PRD 失败')}}};
  const reanalyze=async()=>{if(!current)return;const sessionId=sessionStorage.getItem('prd2tcSessionId')||undefined;
    // 重新分析属于正式语义生成流程，不允许在没有模型会话时静默降级为 Stub。
    if(!sessionId){message.error('请先到“模型配置”保存当前 LLM 会话；如仅验证流程，请从运行工作台显式开启 Stub 模式');return}
    try{const task=await prdApi.reanalyze(current.prdId,{stub:false,sessionId});setLastRun(task.taskId);message.success('已使用当前模型创建需求分析任务')}catch(error:any){message.error(error?.response?.data?.detail||'创建分析任务失败')}};
  const upload=async({file}:{file:any})=>{try{const p=await prdApi.upload(file);await load();choose(p.prdId);message.success('上传成功')}catch(error:any){message.error(error?.response?.data?.detail||'上传失败')}};
  return <div className="page"><Card title="PRD 原文与实时预览" extra={<Space><Select placeholder="选择版本" style={{width:240}} value={current?.prdId} onChange={choose} options={items.map(p=>({value:p.prdId,label:`${p.filename} · ${p.version}`}))}/><Upload accept=".md,.markdown,.txt" maxCount={1} showUploadList={false} customRequest={upload}><Button>上传 PRD</Button></Upload><Button onClick={reanalyze} disabled={!current}>重新分析</Button><Button type="primary" onClick={save} disabled={!current}>保存新版本</Button></Space>}>
    {lastRun&&<Alert type="success" message="需求分析任务已创建" action={<Button size="small" onClick={()=>navigate(`/runs/${lastRun}`)}>打开运行</Button>} style={{marginBottom:12}}/>}
    {current&&<><Typography.Text type="secondary">{current.filename} · {current.version} · SHA-256 {current.sha256} · {current.chars} 字符 · 原文版本与结构化需求分离</Typography.Text><Row gutter={16} style={{marginTop:16}}><Col span={12}><Input.TextArea value={text} onChange={e=>setText(e.target.value)} autoSize={{minRows:28}} /></Col><Col span={12}><div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>{text}</ReactMarkdown></div></Col></Row></>}
  </Card></div>
}
