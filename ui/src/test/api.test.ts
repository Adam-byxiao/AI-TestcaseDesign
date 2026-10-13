import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import {api,runApi} from '../api';

/** 直接验证 `api.ts` 内部的**真实实现**（不被 mock）。
 *
 *  为什么必须单独测：上层组件测试普遍把 `runApi.*` 整个 mock 掉，
 *  于是 `api.ts` 里真正的请求构造逻辑**从未被执行** ——
 *  变异测试证实了这一点：把 `approve` 里的 draft 合并逻辑删掉，
 *  全部前端测试依然通过（因为没人调用真实实现）。
 *  这里通过替换 axios 的 adapter 来截获最终请求配置，从而覆盖真实路径。
 *
 *  注意：axios 会在进入 adapter 前把 body 序列化成 JSON **字符串**，
 *  因此下面用 `body()` 统一解析回对象再断言（不解析会得到「undefined」这类误导性失败）。 */
interface Captured {method?:string;url?:string;data?:any;params?:any}
function captureRequests(){
  const seen:Captured[]=[];
  api.defaults.adapter=async(config:any)=>{
    seen.push({method:config.method,url:config.url,data:config.data,params:config.params});
    return {data:{ok:true},status:200,statusText:'OK',headers:{},config} as any;
  };
  return seen;
}

/** 把 adapter 收到的 body 解析为对象（兼容已被序列化成字符串的情况）。 */
function body(req:Captured):any{
  return typeof req.data==='string'?JSON.parse(req.data):req.data;
}

describe('api.ts 请求构造（真实实现）',()=>{
  let seen:ReturnType<typeof captureRequests>;
  beforeEach(()=>{
    vi.restoreAllMocks();
    seen=captureRequests();
    sessionStorage.setItem('prd2tcSessionId','sess-123');
  });
  afterEach(()=>{sessionStorage.clear()});

  it('approve 带草稿时，草稿内容与 sessionId **同时**进入请求体',async()=>{
    // **核心回归**：`draft` 必须被真正合并进 body。
    // 若只传 `{sessionId}`，用户未点「保存修改」的编辑就会被静默丢弃。
    const draft={markdown:'# 改过的报告',requirements:[{id:'REQ-A-001',title:'x'}]};
    await runApi.approve('run-1','analysis',draft);
    expect(seen).toHaveLength(1);
    expect(seen[0].method).toBe('post');
    expect(seen[0].url).toBe('/runs/run-1/stages/analysis/approve');
    const sent=body(seen[0]);
    expect(sent.markdown).toBe('# 改过的报告');
    expect(sent.requirements).toHaveLength(1);
    expect(sent.sessionId).toBe('sess-123');       // 两者都必须存在
  });

  it('approve 不带草稿时只发 sessionId（保持向后兼容）',async()=>{
    await runApi.approve('run-1','plan');
    expect(body(seen[0])).toEqual({sessionId:'sess-123'});
  });

  it('用例阶段的草稿以 cases 字段提交',async()=>{
    await runApi.approve('run-1','cases',{cases:[{id:'TC-A-001'}]});
    const sent=body(seen[0]);
    expect(sent.cases).toHaveLength(1);
    expect(sent.sessionId).toBe('sess-123');
  });

  it('未配置会话时 approve 仍可发出（sessionId 为 undefined）',async()=>{
    sessionStorage.clear();
    await runApi.approve('run-1','analysis',{markdown:'m'});
    const sent=body(seen[0]);
    expect(sent.markdown).toBe('m');
    expect(sent.sessionId).toBeUndefined();
  });

  it('saveDraft 把内容原样 PUT 到对应阶段',async()=>{
    await runApi.saveDraft('run-1','cases',{cases:[]});
    expect(seen[0].method).toBe('put');
    expect(seen[0].url).toBe('/runs/run-1/stages/cases');
    expect(body(seen[0])).toEqual({cases:[]});
  });

  it('其余变更端点同样带上 sessionId',async()=>{
    await runApi.regenerateStage('run-1','cases');
    expect(body(seen[0]).sessionId).toBe('sess-123');
    expect(seen[0].url).toBe('/runs/run-1/stages/cases/regenerate');
    seen.length=0;
    await runApi.retryModule('run-1','KB');
    expect(body(seen[0]).sessionId).toBe('sess-123');
    expect(seen[0].url).toBe('/runs/run-1/modules/KB/retry');
    seen.length=0;
    await runApi.resume('run-1');
    expect(body(seen[0]).sessionId).toBe('sess-123');
  });
});
