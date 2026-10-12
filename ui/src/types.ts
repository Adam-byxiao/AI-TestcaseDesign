export type Stage = 'analysis'|'plan'|'cases';
/** 摄取摘要（服务端回显）：正文来自哪个文件、识别出多少资产、有哪些告警。 */
export interface UploadBundle {
  primary:string; textChars:number; sourceCount:number;
  assetCount:number; imageCount:number; kinds:string[]; warnings:number;
}
export interface PrdFile { name:string; size:number; relpath?:string; sha256:string; }
export interface Prd { prdId:string; filename:string; path?:string; version:string; sha256:string; chars:number; content?:string;
  files?:PrdFile[]; primary?:string; bundle?:UploadBundle; warnings?:string[]; }
export interface Run { taskId:string; runId?:string; prdId?:string; status:string; mode?:'real'|'stub'; auto?:boolean; currentStage?:string; percent?:number; output?:string; error?:string; errors?:string[]; stageStates?:Record<string,string>; modules?:Record<string,string>; review?:Record<string,any>; events?:any[]; llm?:{provider:string;model:string;apiBase:string}; qualityGate?:Record<string,any>; tokens?:{total:number; stages:Record<string,{tokens:number; moduleTokens?:Record<string,number>}>}; metrics?:RunMetrics; }
export type ModuleState = 'pending'|'running'|'done'|'failed'|'skipped';
/** `estimated:true` marks character-based token estimates; `silentMs` is ms since the last streamed delta. */
export interface ModuleMetrics { state:ModuleState; requirements?:number; batch?:string; batchIndex?:number; batchTotal?:number; promptTokens?:number; completionTokens?:number; estimated?:boolean; chars?:number; tokensPerSec?:number; firstTokenMs?:number|null; silentMs?:number; deltas?:number; requests?:number; preview?:string; error?:string; reasoningChars?:number; reasoningTokens?:number; contentTokens?:number; reasoningOnly?:boolean; reasoningPreview?:string; }
export interface RunMetrics { type?:string; stage?:string; elapsedMs?:number; activeRequests?:number; silentMs?:number; cancelled?:boolean; finished?:boolean; totals?:{promptTokens?:number;completionTokens?:number;totalTokens?:number;cacheReadTokens?:number;cacheCreationTokens?:number;estimated?:boolean}; modules?:Record<string,ModuleMetrics>; }
export interface RunEvent { eventId:string; runId:string; stage?:string; module?:string|null; type:string; status?:string; percent?:number; message:string; createdAt:string; }
export interface Requirement { id:string; module:string; title:string; description:string; acceptanceCriteria:string[]; exceptionFlows:string[]; priority:string; sourceRefs?:any[]; fingerprint?:string; idSource?:string; }
export interface Case { id:string; module:string; title:string; priority:string; status?:string; tags:string[]; requirement:string[]; steps:any[]; expected:string[]; lastExecution?:any; }
