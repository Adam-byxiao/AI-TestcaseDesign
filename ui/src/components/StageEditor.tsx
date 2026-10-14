import {useEffect,useRef,useState} from 'react';
import {Alert,Button,Card,Col,Input,Row,Select,Space,Table,Tabs,Tag,message,Drawer} from 'antd';
import ReactMarkdown from 'react-markdown'; import remarkGfm from 'remark-gfm';
import {runApi} from '../api'; import type {Case,Requirement,Stage} from '../types';

export default function StageEditor({runId,stage,refreshToken,onDirtyChange,onDraftChange}:{runId:string;stage:Stage;refreshToken?:string;onDirtyChange?:(dirty:boolean)=>void;onDraftChange?:(payload:any)=>void}){
  const [markdown,setMarkdown]=useState(''); const [rows,setRows]=useState<any[]>([]);
  const [files,setFiles]=useState<any[]>([]); const [trace,setTrace]=useState<any>(); const [loading,setLoading]=useState(false); const loadedRef=useRef(false);
  // 这里刻意不做自动保存 —— 用户可能只是想看看，而非改动。
  const [dirty,setDirty]=useState(false);
  /** 向上通报未保存状态**与当前草稿内容**。
   *
   *  为什么要连内容一起上报：用户明确要求**不要用「先手动保存草稿」来分离流程** ——
   *  「确认并进入下一步」应当直接带上当前编辑内容。若只上报 `dirty` 标志，
   *  父级在确认时无从取得内容，只能依赖「用户此前已点过保存」，
   *  于是未保存的编辑会被静默丢弃（这正是原先的缺陷）。
   *
   *  ⚠ 必须把回调放进 ref 并**只依赖值**：父级通常传内联箭头函数，
   *  每次渲染都是新引用。若直接写进依赖数组，就会
   *  「effect 触发 → setState → 重渲染 → 新回调 → effect 再触发」**无限循环**
   *  （实测：面板一展开页面即卡死。此前面板无法展开，所以这个缺陷被隐藏了很久）。 */
  const callbacksRef=useRef({onDirtyChange,onDraftChange});
  callbacksRef.current={onDirtyChange,onDraftChange};
  const [selectedCase,setSelectedCase]=useState<string>();
  const [selectedRequirement,setSelectedRequirement]=useState<string>();
  /** 行的**稳定标识**。
   *
   *  ⚠ 绝不能用 antd Table 的 `render(value, record, index)` 里的 `index` ——
   *  那是**当前页内**的序号，不是全局下标。实测后果：
   *    - 在第 2 页点「详情」，抽屉里打开的是第 1 页同位置的用例；
   *    - 在第 2 页改标题/状态，会**静默改到第 1 页的那一条**（数据损坏，
   *      比显示错误严重得多，且保存后才会发现）。
   *  因此一律用 `id` 定位（`rowKey` 已是 id，可用且唯一），
   *  仅当 id 缺失（契约上不该发生）才退化到全局下标。 */
  const keyOf=(row:any,index:number)=>String(row?.id ?? row?.caseId ?? `#${index}`);
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
  },[runId,stage,refreshToken]);
  /** 按**稳定标识**改字段（不是页内序号，见 keyOf 的说明）。 */
  const change=(key:string,field:string,value:any)=>{setRows(current=>current.map((row,i)=>keyOf(row,i)===key?{...row,[field]:value}:row));setDirty(true)};
  /** 草稿体：`approve` 可直接把它作为请求体提交（服务端 approve 会先存草稿再应用）。 */
  const draftBody=()=>stage==='analysis'?{markdown,requirements:rows}:stage==='plan'?{markdown}:{cases:rows};
  // 同时上报「是否有未保存修改」与「当前内容」——父级确认时据此提交，无需用户先手动保存。
  useEffect(()=>{
    callbacksRef.current.onDirtyChange?.(dirty);
    if(dirty)callbacksRef.current.onDraftChange?.(draftBody());
  },[dirty,markdown,rows,stage]);
  const save=async()=>{await runApi.saveDraft(runId,stage,draftBody());setDirty(false);message.success('修改已保存')};
  const reqColumns=[{title:'REQ ID',dataIndex:'id',width:155},{title:'模块',dataIndex:'module',width:100},{title:'标题',dataIndex:'title',render:(v:string,row:any,i:number)=><Input value={v} onChange={e=>change(keyOf(row,i),'title',e.target.value)}/>},{title:'优先级',dataIndex:'priority',width:100,render:(v:string,row:any,i:number)=><Select value={v} onChange={x=>change(keyOf(row,i),'priority',x)} options={['P0','P1','P2','P3'].map(x=>({value:x,label:x}))}/>},{title:'来源',dataIndex:'sourceRefs',width:100,render:(v:any[])=><Tag title={v?.map(x=>`${x.chunkId} [${x.start}-${x.end}]`).join('\n')}>{v?.length||0} 段</Tag>},{title:'编辑',render:(_:any,row:any,i:number)=><Button size="small" onClick={()=>setSelectedRequirement(keyOf(row,i))}>详情</Button>}];
  const caseColumns=[{title:'用例 ID',dataIndex:'id',width:180},{title:'模块',dataIndex:'module',width:100},{title:'标题',dataIndex:'title',render:(v:string,row:any,i:number)=><Input value={v} onChange={e=>change(keyOf(row,i),'title',e.target.value)}/>},{title:'优先级',dataIndex:'priority',width:90},{title:'状态',dataIndex:'status',width:140,render:(v:string,row:any,i:number)=><Select value={v||'active'} onChange={x=>change(keyOf(row,i),'status',x)} options={['active','needs_review','deprecated','superseded'].map(x=>({value:x,label:x}))}/>},{title:'REQ',dataIndex:'requirement',render:(v:string[])=>v?.join(', ')},{title:'编辑',render:(_:any,row:any,i:number)=><Button size="small" onClick={()=>setSelectedCase(keyOf(row,i))}>详情</Button>}];
  const findRow=(key?:string)=>key===undefined?undefined:rows.find((row,i)=>keyOf(row,i)===key);
  const currentCase=findRow(selectedCase);
  const updateCase=(field:string,value:any)=>{if(selectedCase!==undefined)change(selectedCase,field,value)};
  const currentRequirement=findRow(selectedRequirement);
  const updateRequirement=(field:string,value:any)=>{if(selectedRequirement!==undefined)change(selectedRequirement,field,value)};
  const markdownView=<Row gutter={16}><Col span={12}><Input.TextArea value={markdown} onChange={e=>{setMarkdown(e.target.value);setDirty(true)}} autoSize={{minRows:22}}/></Col><Col span={12}><div className="markdown compact"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>{markdown}</ReactMarkdown></div></Col></Row>;
  const traceView=trace?<Space direction="vertical" style={{width:'100%'}}><Space><Tag color="green">覆盖 {trace.summary?.covered||0}</Tag><Tag color="orange">不足 {trace.summary?.insufficient||0}</Tag><Tag color="red">未覆盖 {trace.summary?.uncovered||0}</Tag><Tag>覆盖率 {trace.summary?.coverageRate||0}%</Tag></Space><Table size="small" rowKey="id" pagination={false} dataSource={trace.requirements||[]} columns={[{title:'REQ',dataIndex:'id'},{title:'模块',dataIndex:'module'},{title:'优先级',dataIndex:'priority'},{title:'覆盖状态',dataIndex:'status',render:(v:string)=><Tag color={v==='covered'?'green':v==='uncovered'?'red':'orange'}>{v}</Tag>},{title:'用例',dataIndex:'caseIds',render:(v:string[])=>v?.join(', ')||'-'}]}/><Table size="small" rowKey="caseId" pagination={false} dataSource={trace.caseToRequirements||[]} columns={[{title:'TC',dataIndex:'caseId'},{title:'关联 REQ',dataIndex:'requirementIds',render:(v:string[])=>v?.join(', ')||'-'}]}/>{(trace.unknownReferences?.length||trace.duplicateMappings?.length)&&<Alert type="error" message={`未知引用 ${trace.unknownReferences?.length||0} · 重复映射 ${trace.duplicateMappings?.length||0}`}/>}</Space>:<Alert type="info" message="尚无追踪矩阵"/>;
  const tabs=stage==='analysis'?[{key:'structured',label:'结构化需求',children:<Table rowKey="id" loading={loading} dataSource={rows as Requirement[]} columns={reqColumns}/>},{key:'markdown',label:'Markdown 源码与预览',children:markdownView},{key:'json',label:'JSON',children:<pre className="json-view">{JSON.stringify(rows,null,2)}</pre>}]:stage==='plan'?[{key:'markdown',label:'方案源码与预览',children:markdownView}]:[{key:'cases',label:'结构化用例',children:<Table rowKey="id" loading={loading} dataSource={rows as Case[]} columns={caseColumns} scroll={{x:1000}}/>},{key:'json',label:'JSON',children:<pre className="json-view">{JSON.stringify(rows,null,2)}</pre>},{key:'trace',label:'追踪矩阵',children:traceView}];
  return <Card title={`${stage} 阶段产物`} extra={<Space>{dirty&&<Tag color="gold">未保存</Tag>}<Button onClick={()=>load()}>刷新</Button><Button type="primary" onClick={save} disabled={!dirty}>保存修改</Button></Space>}><Tabs items={tabs}/><Drawer title={currentRequirement?`编辑 ${currentRequirement.id}`:'编辑需求'} open={currentRequirement!==undefined} onClose={()=>setSelectedRequirement(undefined)} width={560}>{currentRequirement&&<Space direction="vertical" style={{width:'100%'}}><Input addonBefore="REQ ID" value={currentRequirement.id} onChange={e=>updateRequirement('id',e.target.value)}/><Input addonBefore="模块" value={currentRequirement.module} onChange={e=>updateRequirement('module',e.target.value)}/><Input addonBefore="标题" value={currentRequirement.title} onChange={e=>updateRequirement('title',e.target.value)}/><Input.TextArea rows={4} value={currentRequirement.description} onChange={e=>updateRequirement('description',e.target.value)} placeholder="描述"/><Input.TextArea rows={5} value={(currentRequirement.acceptanceCriteria||[]).join('\n')} onChange={e=>updateRequirement('acceptanceCriteria',e.target.value.split('\n').filter(Boolean))} placeholder="验收标准，每行一项"/><Input.TextArea rows={5} value={(currentRequirement.exceptionFlows||[]).join('\n')} onChange={e=>updateRequirement('exceptionFlows',e.target.value.split('\n').filter(Boolean))} placeholder="异常分支，每行一项"/><Alert type="info" message={`ID 来源：${currentRequirement.idSource||'explicit'} · fingerprint：${currentRequirement.fingerprint||'-'}`} description={(currentRequirement.sourceRefs||[]).map((ref:any)=><div key={ref.chunkId}>{ref.chunkId} · 字符 {ref.start}-{ref.end}</div>)}/></Space>}</Drawer><Drawer title={currentCase?`编辑 ${currentCase.id}`:'编辑用例'} open={currentCase!==undefined} onClose={()=>setSelectedCase(undefined)} width={520}>{currentCase&&<Space direction="vertical" style={{width:'100%'}}><Input addonBefore="标题" value={currentCase.title} onChange={e=>updateCase('title',e.target.value)}/><Select style={{width:'100%'}} value={currentCase.priority} onChange={v=>updateCase('priority',v)} options={['P0','P1','P2','P3'].map(v=>({value:v,label:v}))}/><Select mode="tags" style={{width:'100%'}} value={currentCase.tags||[]} onChange={v=>updateCase('tags',v)} placeholder="标签"/><Input.TextArea rows={5} value={(currentCase.requirement||[]).join(', ')} onChange={e=>updateCase('requirement',e.target.value.split(',').map(v=>v.trim()).filter(Boolean))} placeholder="REQ-...，多个用逗号分隔"/><Input.TextArea rows={8} value={JSON.stringify(currentCase.steps||[],null,2)} onChange={e=>{try{updateCase('steps',JSON.parse(e.target.value))}catch{/* wait until valid JSON */}}} placeholder="步骤 JSON"/><Input.TextArea rows={5} value={(currentCase.expected||[]).join('\n')} onChange={e=>updateCase('expected',e.target.value.split('\n').filter(Boolean))}/></Space>}</Drawer><Card size="small" title="全部产物" style={{marginTop:16}}>{files.map(f=><a key={f.path} className="artifact-link" href={`/api/v1/runs/${runId}/artifacts/${encodeURIComponent(f.path).replace(/%2F/g,'/')}`} download={f.path.split('/').pop()} target="_blank" rel="noreferrer">{f.path}</a>)}</Card></Card>
}
