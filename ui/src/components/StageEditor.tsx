import {useEffect,useRef,useState} from 'react';
import {Alert,Button,Card,Col,Input,Row,Select,Space,Table,Tabs,Tag,message,Drawer} from 'antd';
import ReactMarkdown from 'react-markdown'; import remarkGfm from 'remark-gfm';
import {runApi} from '../api'; import type {Case,Requirement,Stage} from '../types';

export default function StageEditor({runId,stage,refreshToken,onDirtyChange}:{runId:string;stage:Stage;refreshToken?:string;onDirtyChange?:(dirty:boolean)=>void}){
  const [markdown,setMarkdown]=useState(''); const [rows,setRows]=useState<any[]>([]);
  const [files,setFiles]=useState<any[]>([]); const [trace,setTrace]=useState<any>(); const [loading,setLoading]=useState(false); const loadedRef=useRef(false);
  // 保存前的原始快照：用于判断「是否有未保存修改」并驱动父级的提示。
  // 这里刻意不做自动保存 —— 用户可能只是想看看，而非改动。
  const [dirty,setDirty]=useState(false);
  useEffect(()=>{onDirtyChange?.(dirty)},[dirty,onDirtyChange]);
  const [selectedCase,setSelectedCase]=useState<number>();
  const [selectedRequirement,setSelectedRequirement]=useState<number>();
  /** 加载产物。
   *
   *  `keepEdits=true`（轮询路径）时**不覆盖**用户正在编辑的 markdown/rows ——
   *  原先每 5 秒无条件重载，会把用户刚打的字静默冲掉，
   *  再点「确认」时保存的是服务端旧值而非他的修改（实测的真实数据丢失路径）。
   *  产物清单与追踪矩阵仍照常刷新，因此外部变化依然可见。 */
  const load=async({keepEdits=false}:{keepEdits?:boolean}={})=>{
    if(!loadedRef.current)setLoading(true);
    try{
      const list=await runApi.artifacts(runId); setFiles(list);
      if(!keepEdits){
        if(stage==='analysis'&&list.some((f:any)=>f.path==='analysis/requirements.json')){const [req,md]=await Promise.all([runApi.artifact(runId,'analysis/requirements.json'),runApi.artifact(runId,'analysis/需求分析报告.md')]);setRows(req.requirements||[]);setMarkdown(md)}
        if(stage==='plan'&&list.some((f:any)=>f.path==='plan/测试方案.md')){setMarkdown(await runApi.artifact(runId,'plan/测试方案.md'));setRows([])}
        if(stage==='cases'){const caseFiles=list.filter((f:any)=>/^cases\/cases\/.*\.json$/.test(f.path));const docs=await Promise.all(caseFiles.map((f:any)=>runApi.artifact(runId,f.path)));setRows(docs.flatMap((d:any)=>d.cases||d))}
      }
      if(stage==='cases'&&list.some((f:any)=>f.path==='cases/traceability.json')){try{setTrace(await runApi.artifact(runId,'cases/traceability.json'))}catch{setTrace(undefined)}}
    }finally{setLoading(false);loadedRef.current=true}
  };
  useEffect(()=>{
    loadedRef.current=false;setRows([]);setMarkdown('');setTrace(undefined);setDirty(false);
    load();
    // 轮询只为感知外部变化；带 keepEdits 以免冲掉用户正在输入的内容
    const timer=window.setInterval(()=>load({keepEdits:true}),5000);
    return()=>window.clearInterval(timer);
  },[runId,stage]);
  const change=(index:number,key:string,value:any)=>{setRows(current=>current.map((row,i)=>i===index?{...row,[key]:value}:row));setDirty(true)};
  const save=async()=>{const body=stage==='analysis'?{markdown,requirements:rows}:stage==='plan'?{markdown}:{cases:rows};await runApi.saveDraft(runId,stage,body);setDirty(false);message.success('修改已保存')};
  const reqColumns=[{title:'REQ ID',dataIndex:'id',width:155},{title:'模块',dataIndex:'module',width:100},{title:'标题',dataIndex:'title',render:(v:string,_:any,i:number)=><Input value={v} onChange={e=>change(i,'title',e.target.value)}/>},{title:'优先级',dataIndex:'priority',width:100,render:(v:string,_:any,i:number)=><Select value={v} onChange={x=>change(i,'priority',x)} options={['P0','P1','P2','P3'].map(x=>({value:x,label:x}))}/>},{title:'来源',dataIndex:'sourceRefs',width:100,render:(v:any[])=><Tag title={v?.map(x=>`${x.chunkId} [${x.start}-${x.end}]`).join('\n')}>{v?.length||0} 段</Tag>},{title:'编辑',render:(_:any,row:any,i:number)=><Button size="small" onClick={()=>setSelectedRequirement(i)}>详情</Button>}];
  const caseColumns=[{title:'用例 ID',dataIndex:'id',width:180},{title:'模块',dataIndex:'module',width:100},{title:'标题',dataIndex:'title',render:(v:string,_:any,i:number)=><Input value={v} onChange={e=>change(i,'title',e.target.value)}/>},{title:'优先级',dataIndex:'priority',width:90},{title:'状态',dataIndex:'status',width:140,render:(v:string,_:any,i:number)=><Select value={v||'active'} onChange={x=>change(i,'status',x)} options={['active','needs_review','deprecated','superseded'].map(x=>({value:x,label:x}))}/>},{title:'REQ',dataIndex:'requirement',render:(v:string[])=>v?.join(', ')},{title:'编辑',render:(_:any,row:any,i:number)=><Button size="small" onClick={()=>setSelectedCase(i)}>详情</Button>}];
  const currentCase=selectedCase===undefined?undefined:rows[selectedCase];
  const updateCase=(key:string,value:any)=>{if(selectedCase!==undefined)change(selectedCase,key,value)};
  const currentRequirement=selectedRequirement===undefined?undefined:rows[selectedRequirement];
  const updateRequirement=(key:string,value:any)=>{if(selectedRequirement!==undefined)change(selectedRequirement,key,value)};
  const markdownView=<Row gutter={16}><Col span={12}><Input.TextArea value={markdown} onChange={e=>{setMarkdown(e.target.value);setDirty(true)}} autoSize={{minRows:22}}/></Col><Col span={12}><div className="markdown compact"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>{markdown}</ReactMarkdown></div></Col></Row>;
  const traceView=trace?<Space direction="vertical" style={{width:'100%'}}><Space><Tag color="green">覆盖 {trace.summary?.covered||0}</Tag><Tag color="orange">不足 {trace.summary?.insufficient||0}</Tag><Tag color="red">未覆盖 {trace.summary?.uncovered||0}</Tag><Tag>覆盖率 {trace.summary?.coverageRate||0}%</Tag></Space><Table size="small" rowKey="id" pagination={false} dataSource={trace.requirements||[]} columns={[{title:'REQ',dataIndex:'id'},{title:'模块',dataIndex:'module'},{title:'优先级',dataIndex:'priority'},{title:'覆盖状态',dataIndex:'status',render:(v:string)=><Tag color={v==='covered'?'green':v==='uncovered'?'red':'orange'}>{v}</Tag>},{title:'用例',dataIndex:'caseIds',render:(v:string[])=>v?.join(', ')||'-'}]}/><Table size="small" rowKey="caseId" pagination={false} dataSource={trace.caseToRequirements||[]} columns={[{title:'TC',dataIndex:'caseId'},{title:'关联 REQ',dataIndex:'requirementIds',render:(v:string[])=>v?.join(', ')||'-'}]}/>{(trace.unknownReferences?.length||trace.duplicateMappings?.length)&&<Alert type="error" message={`未知引用 ${trace.unknownReferences?.length||0} · 重复映射 ${trace.duplicateMappings?.length||0}`}/>}</Space>:<Alert type="info" message="尚无追踪矩阵"/>;
  const tabs=stage==='analysis'?[{key:'structured',label:'结构化需求',children:<Table rowKey="id" loading={loading} dataSource={rows as Requirement[]} columns={reqColumns}/>},{key:'markdown',label:'Markdown 源码与预览',children:markdownView},{key:'json',label:'JSON',children:<pre className="json-view">{JSON.stringify(rows,null,2)}</pre>}]:stage==='plan'?[{key:'markdown',label:'方案源码与预览',children:markdownView}]:[{key:'cases',label:'结构化用例',children:<Table rowKey="id" loading={loading} dataSource={rows as Case[]} columns={caseColumns} scroll={{x:1000}}/>},{key:'json',label:'JSON',children:<pre className="json-view">{JSON.stringify(rows,null,2)}</pre>},{key:'trace',label:'追踪矩阵',children:traceView}];
  return <Card title={`${stage} 阶段产物`} extra={<Space>{dirty&&<Tag color="gold">未保存</Tag>}<Button onClick={()=>load()}>刷新</Button><Button type="primary" onClick={save} disabled={!dirty}>保存修改</Button></Space>}><Tabs items={tabs}/><Drawer title={currentRequirement?`编辑 ${currentRequirement.id}`:'编辑需求'} open={currentRequirement!==undefined} onClose={()=>setSelectedRequirement(undefined)} width={560}>{currentRequirement&&<Space direction="vertical" style={{width:'100%'}}><Input addonBefore="REQ ID" value={currentRequirement.id} onChange={e=>updateRequirement('id',e.target.value)}/><Input addonBefore="模块" value={currentRequirement.module} onChange={e=>updateRequirement('module',e.target.value)}/><Input addonBefore="标题" value={currentRequirement.title} onChange={e=>updateRequirement('title',e.target.value)}/><Input.TextArea rows={4} value={currentRequirement.description} onChange={e=>updateRequirement('description',e.target.value)} placeholder="描述"/><Input.TextArea rows={5} value={(currentRequirement.acceptanceCriteria||[]).join('\n')} onChange={e=>updateRequirement('acceptanceCriteria',e.target.value.split('\n').filter(Boolean))} placeholder="验收标准，每行一项"/><Input.TextArea rows={5} value={(currentRequirement.exceptionFlows||[]).join('\n')} onChange={e=>updateRequirement('exceptionFlows',e.target.value.split('\n').filter(Boolean))} placeholder="异常分支，每行一项"/><Alert type="info" message={`ID 来源：${currentRequirement.idSource||'explicit'} · fingerprint：${currentRequirement.fingerprint||'-'}`} description={(currentRequirement.sourceRefs||[]).map((ref:any)=><div key={ref.chunkId}>{ref.chunkId} · 字符 {ref.start}-{ref.end}</div>)}/></Space>}</Drawer><Drawer title={currentCase?`编辑 ${currentCase.id}`:'编辑用例'} open={currentCase!==undefined} onClose={()=>setSelectedCase(undefined)} width={520}>{currentCase&&<Space direction="vertical" style={{width:'100%'}}><Input addonBefore="标题" value={currentCase.title} onChange={e=>updateCase('title',e.target.value)}/><Select style={{width:'100%'}} value={currentCase.priority} onChange={v=>updateCase('priority',v)} options={['P0','P1','P2','P3'].map(v=>({value:v,label:v}))}/><Select mode="tags" style={{width:'100%'}} value={currentCase.tags||[]} onChange={v=>updateCase('tags',v)} placeholder="标签"/><Input.TextArea rows={5} value={(currentCase.requirement||[]).join(', ')} onChange={e=>updateCase('requirement',e.target.value.split(',').map(v=>v.trim()).filter(Boolean))} placeholder="REQ-...，多个用逗号分隔"/><Input.TextArea rows={8} value={JSON.stringify(currentCase.steps||[],null,2)} onChange={e=>{try{updateCase('steps',JSON.parse(e.target.value))}catch{/* wait until valid JSON */}}} placeholder="步骤 JSON"/><Input.TextArea rows={5} value={(currentCase.expected||[]).join('\n')} onChange={e=>updateCase('expected',e.target.value.split('\n').filter(Boolean))}/></Space>}</Drawer><Card size="small" title="全部产物" style={{marginTop:16}}>{files.map(f=><a key={f.path} className="artifact-link" href={`/api/v1/runs/${runId}/artifacts/${encodeURIComponent(f.path).replace(/%2F/g,'/')}`} download={f.path.split('/').pop()} target="_blank" rel="noreferrer">{f.path}</a>)}</Card></Card>
}
