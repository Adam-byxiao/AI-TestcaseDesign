import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import StageEditor from '../components/StageEditor';
import {runApi} from '../api';

vi.mock('../api',()=>({runApi:{artifacts:vi.fn(),artifact:vi.fn(),saveDraft:vi.fn()}}));

describe('StageEditor',()=>{
  afterEach(cleanup);
  beforeEach(()=>{
    vi.mocked(runApi.artifacts).mockResolvedValue([
      {path:'analysis/requirements.json'}, {path:'analysis/需求分析报告.md'}
    ]);
    vi.mocked(runApi.artifact).mockImplementation(async(_id:string,path:string)=>path.endsWith('.json')?{
      requirements:[{id:'REQ-AUTH-001',module:'AUTH',title:'登录',description:'用户登录',acceptanceCriteria:['成功'],exceptionFlows:['密码错误'],priority:'P0',sourceRefs:[{chunkId:'chunk-1',start:0,end:20}],fingerprint:'sha256:x',idSource:'explicit'}]
    }:'# 分析报告');
    vi.mocked(runApi.saveDraft).mockResolvedValue({});
  });

  it('loads structured requirements, edits them and saves the changes',async()=>{
    render(<StageEditor runId="run-1" stage="analysis"/>);
    expect(await screen.findByDisplayValue('登录')).toBeInTheDocument();
    // 未改动时保存按钮应禁用 —— 「保存修改」只在确有修改时才有意义
    expect(screen.getByRole('button',{name:'保存修改'})).toBeDisabled();
    fireEvent.change(screen.getByDisplayValue('登录'),{target:{value:'账号登录'}});
    fireEvent.click(screen.getByRole('button',{name:'保存修改'}));
    await waitFor(()=>expect(runApi.saveDraft).toHaveBeenCalledWith('run-1','analysis',expect.objectContaining({requirements:[expect.objectContaining({title:'账号登录'})]})));
  });

  it('reports unsaved changes to the parent so the confirm step can warn',async()=>{
    // 编辑产物是**可选**的：父级（三步卡片）需要知道是否有未保存修改，
    // 才能在「确认并进入下一步」时给出提示，而不是要求用户先手动保存草稿。
    const onDirtyChange=vi.fn();
    render(<StageEditor runId="run-1" stage="analysis" onDirtyChange={onDirtyChange}/>);
    await screen.findByDisplayValue('登录');
    await waitFor(()=>expect(onDirtyChange).toHaveBeenCalledWith(false));
    fireEvent.change(screen.getByDisplayValue('登录'),{target:{value:'账号登录'}});
    await waitFor(()=>expect(onDirtyChange).toHaveBeenCalledWith(true));
  });

  it('does not clobber in-progress edits when the background poll fires',async()=>{
    // **核心回归**：原先每 5 秒无条件重载，会把用户刚打的字静默冲掉，
    // 再点「确认」时保存的是服务端旧值而非他的修改。
    vi.useFakeTimers();
    try{
      render(<StageEditor runId="run-1" stage="analysis"/>);
      await vi.waitFor(()=>expect(screen.getByDisplayValue('登录')).toBeInTheDocument());
      fireEvent.change(screen.getByDisplayValue('登录'),{target:{value:'我的改动'}});
      // 触发两轮轮询
      await vi.advanceTimersByTimeAsync(5000);
      await vi.advanceTimersByTimeAsync(5000);
      expect(screen.getByDisplayValue('我的改动')).toBeInTheDocument();
    }finally{
      vi.useRealTimers();
    }
  });

  it('opens the complete requirement editor',async()=>{
    render(<StageEditor runId="run-1" stage="analysis"/>);
    await screen.findByDisplayValue('登录');
    fireEvent.click(screen.getByRole('button',{name:/详\s*情/}));
    expect(await screen.findByDisplayValue('用户登录')).toBeInTheDocument();
    expect(screen.getByText(/fingerprint/)).toBeInTheDocument();
  });
});
