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

  it('loads structured requirements, edits them and saves a draft',async()=>{
    render(<StageEditor runId="run-1" stage="analysis"/>);
    expect(await screen.findByDisplayValue('登录')).toBeInTheDocument();
    fireEvent.change(screen.getByDisplayValue('登录'),{target:{value:'账号登录'}});
    fireEvent.click(screen.getByRole('button',{name:'保存草稿'}));
    await waitFor(()=>expect(runApi.saveDraft).toHaveBeenCalledWith('run-1','analysis',expect.objectContaining({requirements:[expect.objectContaining({title:'账号登录'})]})));
  });

  it('opens the complete requirement editor',async()=>{
    render(<StageEditor runId="run-1" stage="analysis"/>);
    await screen.findByDisplayValue('登录');
    fireEvent.click(screen.getByRole('button',{name:/详\s*情/}));
    expect(await screen.findByDisplayValue('用户登录')).toBeInTheDocument();
    expect(screen.getByText(/fingerprint/)).toBeInTheDocument();
  });
});
