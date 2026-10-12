import {useEffect,useState} from 'react';
import {Card,Select,Upload,Button,Input,Row,Col,Typography,message,Space,Alert,Tag,Table,Tooltip} from 'antd';
import {InboxOutlined,FolderOpenOutlined,FileTextOutlined,PictureOutlined,WarningOutlined} from '@ant-design/icons';
import {useSearchParams,useNavigate} from 'react-router-dom';
import ReactMarkdown from 'react-markdown'; import remarkGfm from 'remark-gfm';
import {prdApi} from '../api'; import type {Prd} from '../types';

const KIND_CN:Record<string,string>={markdown:'Markdown',text:'文本',html:'HTML',docx:'Word',pdf:'PDF',zip:'压缩包',image:'图片',other:'其它'};

export default function PrdPage(){
  const [items,setItems]=useState<Prd[]>([]);
  const [current,setCurrent]=useState<Prd&{content:string;versions?:any[]}>();
  const [text,setText]=useState('');
  const [lastRun,setLastRun]=useState<string>();
  const [uploading,setUploading]=useState(false);
  const [params]=useSearchParams(); const navigate=useNavigate();

  const load=async()=>{try{const values=await prdApi.list();setItems(values);const requested=params.get('id');if(requested)choose(requested)}catch{message.error('无法加载 PRD 列表')}};
  const choose=async(id:string)=>{try{const p=await prdApi.get(id);setCurrent(p);setText(p.content||'')}catch{message.error('无法加载 PRD')}};
  useEffect(()=>{load()},[]);

  const save=async()=>{if(current){try{const p=await prdApi.update(current.prdId,text);setCurrent({...current,...p,content:text});await load();message.success(`已保存 ${p.version} 新 PRD 版本`)}catch{message.error('保存 PRD 失败')}}};

  const reanalyze=async()=>{if(!current)return;const sessionId=sessionStorage.getItem('prd2tcSessionId')||undefined;
    // 重新分析属于正式语义生成流程，不允许在没有模型会话时静默降级为 Stub。
    if(!sessionId){message.error('请先到“模型配置”保存当前 LLM 会话；如仅验证流程，请从运行工作台显式开启 Stub 模式');return}
    try{const task=await prdApi.reanalyze(current.prdId,{stub:false,sessionId});setLastRun(task.taskId);message.success('已使用当前模型创建需求分析任务')}catch(error:any){message.error(error?.response?.data?.detail||'创建分析任务失败')}};

  /** 统一上传入口：单文件、多文件、整个文件夹都走这里。
   *  文件夹由 <Upload directory> 提供 webkitRelativePath，服务端据此还原层级。 */
  const doUpload=async(files:File[])=>{
    if(!files.length)return;
    setUploading(true);
    try{
      const p=await prdApi.uploadMany(files);
      await load(); await choose(p.prdId);
      const b=p.bundle;
      message.success(b?`上传成功：正文来自 ${p.primary}，识别 ${b.imageCount} 张图片`:'上传成功');
      if(b?.warnings) message.warning(`有 ${b.warnings} 条解析告警，请查看下方提示`);
    }catch(error:any){
      message.error(error?.response?.data?.detail||'上传失败');
    }finally{ setUploading(false); }
  };
  const pickSingle=async({file}:{file:any})=>doUpload([file]);

  const b=current?.bundle;
  const files=current?.files||[];
  return <div className="page">
    <Card title="PRD 原文与实时预览" extra={<Space wrap>
      <Select placeholder="选择版本" style={{width:240}} value={current?.prdId} onChange={choose}
        options={items.map(p=>({value:p.prdId,label:`${p.filename} · ${p.version}`}))}/>
      <Upload accept=".md,.markdown,.mdown,.txt,.docx,.pdf,.zip,.png,.jpg,.jpeg,.gif,.webp,.bmp"
        multiple showUploadList={false} customRequest={pickSingle} disabled={uploading}>
        <Button icon={<FileTextOutlined/>} loading={uploading}>上传文件</Button>
      </Upload>
      <Upload directory showUploadList={false} disabled={uploading}
        customRequest={pickSingle} beforeUpload={(f:any,list:any)=>{doUpload(list as File[]);return false;}}>
        <Tooltip title="选择整个文件夹（如 PRD + 图片和附件），服务端按相对路径还原层级并自动判定正文">
          <Button icon={<FolderOpenOutlined/>} loading={uploading}>上传文件夹</Button>
        </Tooltip>
      </Upload>
      <Button onClick={reanalyze} disabled={!current}>重新分析</Button>
      <Button type="primary" onClick={save} disabled={!current}>保存新版本</Button>
    </Space>}>

    {lastRun&&<Alert type="success" message="需求分析任务已创建" action={<Button size="small" onClick={()=>navigate(`/runs/${lastRun}`)}>打开运行</Button>} style={{marginBottom:12}}/>}

    {b&&<Card size="small" type="inner" title="摄取结果" style={{marginBottom:12}} extra={
      <Space size={4} wrap>
        {(b.kinds||[]).map(k=><Tag key={k}>{KIND_CN[k]||k}</Tag>)}
        {b.imageCount>0&&<Tag icon={<PictureOutlined/>} color="blue">{b.imageCount} 张图片</Tag>}
      </Space>}>
      <Space direction="vertical" size={4} style={{width:'100%'}}>
        <Typography.Text>
          正文来自 <Typography.Text strong>{b.primary}</Typography.Text>
          （{b.textChars} 字符）· 共 {b.sourceCount} 个来源 · 资产 {b.assetCount} 项
        </Typography.Text>
        {files.length>0&&<Table size="small" pagination={files.length>8?{pageSize:8}:false}
          rowKey="name" dataSource={files}
          columns={[
            {title:'文件',dataIndex:'relpath',render:(v:string,n:any)=>v||n.name,
             ellipsis:true},
            {title:'大小',dataIndex:'size',width:100,
             render:(v:number)=>`${(v/1024).toFixed(1)} KB`},
            {title:'角色',width:100,render:(_:any,n:any)=>
              n.name===b.primary?<Tag color="green">正文</Tag>:<Tag>附件/资产</Tag>},
          ]}/>
        }
        {(current?.warnings||[]).map((w,i)=>
          <Alert key={i} type="warning" showIcon icon={<WarningOutlined/>} message={w}/>)}
      </Space>
    </Card>}

    {!b&&files.length===0&&<Alert type="info" showIcon icon={<InboxOutlined/>} style={{marginBottom:12}}
      message="支持上传单个文件、多个文件，或整个文件夹"
      description="可上传 Markdown / Word(.docx) / PDF / 压缩包(.zip) / 图片；文件夹上传时会自动还原层级、解析其中每个文件，并从子目录里收集配图与附件。"/>}

    {current&&<><Typography.Text type="secondary">{current.filename} · {current.version} · SHA-256 {current.sha256} · {current.chars} 字符 · 原文版本与结构化需求分离</Typography.Text>
      <Row gutter={16} style={{marginTop:16}}>
        <Col span={12}><Input.TextArea value={text} onChange={e=>setText(e.target.value)} autoSize={{minRows:28}}/></Col>
        <Col span={12}><div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>{text}</ReactMarkdown></div></Col>
      </Row></>}
    {!current&&files.length===0&&<Typography.Text type="secondary">尚未选择 PRD，请上传或从上方下拉框选择。</Typography.Text>}
  </Card></div>;
}
