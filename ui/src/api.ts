import axios from 'axios'; import type {Prd,Run,Stage,UploadBundle} from './types';
export const api=axios.create({baseURL:'/api/v1',timeout:120000});
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
export const runApi={list:()=>api.get<Run[]>('/runs').then(r=>r.data),get:(id:string)=>api.get<Run>(`/runs/${id}`).then(r=>r.data),create:(body:any)=>api.post<Run>('/runs',body).then(r=>r.data),events:(id:string)=>api.get<any[]>(`/runs/${id}/events`).then(r=>r.data),saveDraft:(id:string,stage:Stage,body:any)=>api.put(`/runs/${id}/stages/${stage}`,body).then(r=>r.data),approve:(id:string,stage:Stage)=>api.post(`/runs/${id}/stages/${stage}/approve`,{sessionId:sid()}).then(r=>r.data),reject:(id:string,stage:Stage,reason:string)=>api.post(`/runs/${id}/stages/${stage}/reject`,{reason}).then(r=>r.data),resume:(id:string)=>api.post(`/runs/${id}/resume`,{sessionId:sid()}).then(r=>r.data),cancel:(id:string)=>api.post(`/runs/${id}/cancel`).then(r=>r.data),retryModule:(id:string,module:string)=>api.post(`/runs/${id}/modules/${module}/retry`,{sessionId:sid()}).then(r=>r.data),artifacts:(id:string)=>api.get(`/runs/${id}/artifacts`).then(r=>r.data),artifact:(id:string,path:string)=>api.get(`/runs/${id}/artifacts/${path}`).then(r=>r.data)};
export const llmApi={session:(body:any)=>api.post('/sessions/llm-config',body).then(r=>r.data),verify:(body:any)=>api.post('/sessions/llm-config/verify',body).then(r=>r.data)};
