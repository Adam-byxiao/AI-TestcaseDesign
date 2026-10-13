import axios from 'axios'; import type {CaseFacets,CaseSelection,Prd,Run,Stage,UploadBundle} from './types';
import {selectionActive} from './types';

/** 查询参数序列化：数组用**重复键**，不用 axios 默认的 `key[]=`。
 *
 *  为什么必须自定义：axios 默认把数组序列化成 `modules[]=KB&modules[]=AGENT`，
 *  而后端（FastAPI `List[str] = Query(None)`）只认 `modules=KB&modules=AGENT`。
 *  实测后果：后端**完全收不到** modules/priorities/tags → 视为无筛选
 *  → 用户明明选了条件，导出却仍是全量。
 *
 *  顺带统一两条约定：
 *    - 空数组**不发参数**（空集合的语义是「什么都不要」，而我们要的是「该维度不限」）；
 *    - `undefined`/`null`/空串同样不发（避免后端把 `""` 当成有效取值。 */
const serializeParams=(params:Record<string,any>):string=>{
  const search=new URLSearchParams();
  for(const [key,value] of Object.entries(params||{})){
    if(value===undefined||value===null||value==='')continue;
    if(Array.isArray(value)){
      for(const item of value){
        if(item===undefined||item===null||item==='')continue;
        search.append(key,String(item));
      }
    }else{
      search.append(key,String(value));
    }
  }
  return search.toString();
};

export const api=axios.create({baseURL:'/api/v1',timeout:120000,
  paramsSerializer:{serialize:serializeParams}});
/** 当前浏览器里保存的 LLM 会话（由「模型配置」写入）。
 *
 *  为什么每个变更操作都要带上它：会话是**服务端内存态**、随重启失效，
 *  而任务里记录的 sessionId 是创建/上次运行时的旧值。用户重新配置后，
 *  旧 ID 已死 → 服务端取不到会话 → 报「未配置 LLM 凭证」（其实真因是会话过期）。
 *  带上当前会话可让重新配置立即生效。 */
const sid=()=>sessionStorage.getItem('prd2tcSessionId')||undefined;
/** 上传（单文件或多文件/文件夹）。
 *  文件夹上传时浏览器给出 `webkitRelativePath`；把它作为 multipart 的 **filename**
 *  提交（而非另开一个字段再按索引配对 —— 那样在重排/并发下极易错位）。
 *  服务端据此还原层级，正确判定「哪个是 PRD 正文」。 */
const uploadPrd=(files:File[])=>{
  const f=new FormData();
  for(const file of files){
    const rel=((file as any).webkitRelativePath as string|undefined)||file.name;
    f.append('files',file,rel);
  }
  return api.post<Prd>('/prds',f).then(r=>r.data);
};
export const prdApi={list:()=>api.get<Prd[]>('/prds').then(r=>r.data),get:(id:string)=>api.get<Prd&{content:string}>(`/prds/${id}`).then(r=>r.data),upload:(file:File)=>uploadPrd([file]),uploadMany:uploadPrd,update:(id:string,content:string)=>api.put<Prd>(`/prds/${id}`,{content}).then(r=>r.data),reanalyze:(id:string,body:any={})=>api.post(`/prds/${id}/reanalyze`,body).then(r=>r.data)};
export const runApi={list:()=>api.get<Run[]>('/runs').then(r=>r.data),get:(id:string)=>api.get<Run>(`/runs/${id}`).then(r=>r.data),create:(body:any)=>api.post<Run>('/runs',body).then(r=>r.data),events:(id:string)=>api.get<any[]>(`/runs/${id}/events`).then(r=>r.data),saveDraft:(id:string,stage:Stage,body:any)=>api.put(`/runs/${id}/stages/${stage}`,body).then(r=>r.data),
  /** 批准并进入下一步。`draft` 非空时**随批准一起提交**（服务端 approve 支持带 body：先存草稿再应用），
   *  这样用户无需先点「保存修改」——只传 `{sessionId}` 会让未保存的编辑被静默丢弃。 */
  approve:(id:string,stage:Stage,draft?:any)=>api.post(`/runs/${id}/stages/${stage}/approve`,
    draft?{...draft,sessionId:sid()}:{sessionId:sid()}).then(r=>r.data),
  reject:(id:string,stage:Stage,reason:string)=>api.post(`/runs/${id}/stages/${stage}/reject`,{reason}).then(r=>r.data),resume:(id:string)=>api.post(`/runs/${id}/resume`,{sessionId:sid()}).then(r=>r.data),cancel:(id:string)=>api.post(`/runs/${id}/cancel`).then(r=>r.data),retryModule:(id:string,module:string)=>api.post(`/runs/${id}/modules/${module}/retry`,{sessionId:sid()}).then(r=>r.data),regenerateStage:(id:string,stage:Stage)=>api.post(`/runs/${id}/stages/${stage}/regenerate`,{sessionId:sid()}).then(r=>r.data),artifacts:(id:string)=>api.get(`/runs/${id}/artifacts`).then(r=>r.data),artifact:(id:string,path:string)=>api.get(`/runs/${id}/artifacts/${path}`).then(r=>r.data)};
/** 导出选项：文件名与 XMind 粒度由用户决定（此前是固定文件名 + 强制直接下载）。 */
export interface ExportOptions {
  filename?:string; xmindMode?:'case'|'full'|'module'; sheetName?:string;
  selection?:CaseSelection;
}
/** 把选择条件转成查询参数（数组用重复键，与服务端 `List[str]` 对齐）。
 *  空维度**不发送** —— 发送空数组会被解析成「空集合」，
 *  语义上等于「什么都不要」，而不是「该维度不限」。 */
function selectionParams(selection?:CaseSelection):Record<string,any>{
  const out:Record<string,any>={};
  if(!selection)return out;
  if(selection.modules?.length)out.modules=selection.modules;
  if(selection.priorities?.length)out.priorities=selection.priorities;
  if(selection.tags?.length)out.tags=selection.tags;
  const keyword=selection.keyword?.trim();
  if(keyword)out.keyword=keyword;
  return out;
}

/** 用例分面统计（工作台批量选择用）。
 *  `ignore` 指定统计哪个维度时忽略它自身的条件 —— 否则选定某维度后，
 *  该维度的候选会塌缩成已选项，用户无法比较「换个条件会剩多少条」。 */
export async function fetchFacets(id:string,selection:CaseSelection,ignore?:string):Promise<CaseFacets>{
  const response=await api.get(`/runs/${encodeURIComponent(id)}/cases/facets`,{
    params:{...selectionParams(selection),ignore:ignore||undefined},
  });
  return response.data as CaseFacets;
}
/** 选择条件 → 文件名片段（与服务端 `CaseFilter.name_suffix` 同构）。 */
export function selectionSuffix(selection?:CaseSelection):string{
  if(!selection||!selectionActive(selection))return '';
  const parts:string[]=[];
  if(selection.modules.length)parts.push(selection.modules.join('-'));
  if(selection.priorities.length)parts.push(selection.priorities.join('-'));
  if(selection.tags.length)parts.push(selection.tags.map(t=>t.replace(/^@/,'')).join('-'));
  const keyword=selection.keyword.trim();
  if(keyword)parts.push(keyword);
  return parts.filter(Boolean).join('-').replace(/[<>:"/\\|?*]/g,'_').slice(0,60);
}

/** 选择条件 → 人类可读描述。 */
export function selectionLabel(selection?:CaseSelection):string{
  if(!selection||!selectionActive(selection))return '全部用例';
  const parts:string[]=[];
  if(selection.modules.length)parts.push(`模块=${selection.modules.join(',')}`);
  if(selection.priorities.length)parts.push(`优先级=${selection.priorities.join(',')}`);
  if(selection.tags.length)parts.push(`标签=${selection.tags.join(',')}`);
  const keyword=selection.keyword.trim();
  if(keyword)parts.push(`关键词=${keyword}`);
  return parts.join('；');
}

/** 导出文件的默认名（含日期与筛选描述，便于同一天多次导出时不互相覆盖）。 */
export function defaultExportName(product:string,version:string,fmt:'excel'|'xmind',
                                  when=new Date(),selection?:CaseSelection):string{
  const stamp=`${when.getFullYear()}${String(when.getMonth()+1).padStart(2,'0')}${String(when.getDate()).padStart(2,'0')}`;
  const scope=selectionSuffix(selection);
  const base=[product,version,'用例集',scope].filter(Boolean).join('-')||'用例集';
  return `${base}-${stamp}.${fmt==='excel'?'xlsx':'xmind'}`;
}

/** 拉取导出文件为 Blob。
 *  不走 `window.open`：那样无法自定义文件名、无法选择保存位置，
 *  也无法感知成功/失败（浏览器会静默把错误页存成文件）。 */
export async function fetchExport(id:string,fmt:'excel'|'xmind',options:ExportOptions={}):Promise<Blob>{
  const response=await api.get(`/exports/${encodeURIComponent(id)}/${fmt}`,{
    responseType:'blob',
    params:{filename:options.filename||undefined,xmind_mode:options.xmindMode||undefined,
            sheet_name:options.sheetName||undefined,...selectionParams(options.selection)},
  });
  return response.data as Blob;
}

/** 浏览器是否支持「另存为」对话框（可选择位置与文件名）。Chrome/Edge 支持，Firefox/Safari 不支持。 */
export const canPickSaveLocation=()=>typeof (window as any).showSaveFilePicker==='function';

export type SaveOutcome='saved'|'downloaded'|'cancelled';

/** 保存 Blob：优先弹出「另存为」（可选位置与名字），否则回退为普通下载。
 *
 *  回退路径仍比原先好：用 `<a download>` 明确指定文件名，而不是依赖
 *  `Content-Disposition` 且无法改名。 */
export async function saveBlob(blob:Blob,filename:string,fmt:'excel'|'xmind'):Promise<SaveOutcome>{
  const picker=(window as any).showSaveFilePicker;
  if(typeof picker==='function'){
    try{
      const handle=await picker({
        suggestedName:filename,
        types:[fmt==='excel'
          ?{description:'Excel 工作簿',accept:{'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':['.xlsx']}}
          :{description:'XMind 思维导图',accept:{'application/vnd.xmind.workbook':['.xmind']}}],
      });
      const writable=await handle.createWritable();
      await writable.write(blob); await writable.close();
      return 'saved';
    }catch(error:any){
      // 用户在系统对话框里点了取消 —— 不是错误，不应弹错误提示
      if(error?.name==='AbortError')return 'cancelled';
      // 其它异常（权限/磁盘）回退到下载，保证用户仍能拿到文件
    }
  }
  const url=URL.createObjectURL(blob);
  const anchor=document.createElement('a');
  anchor.href=url; anchor.download=filename; document.body.appendChild(anchor); anchor.click();
  anchor.remove(); window.setTimeout(()=>URL.revokeObjectURL(url),1000);
  return 'downloaded';
}
export const llmApi={session:(body:any)=>api.post('/sessions/llm-config',body).then(r=>r.data),verify:(body:any)=>api.post('/sessions/llm-config/verify',body).then(r=>r.data)};
