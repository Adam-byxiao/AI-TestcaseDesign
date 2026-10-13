import {cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import {MemoryRouter} from 'react-router-dom';
import StageSteps from '../components/StageSteps';
import {runApi} from '../api';
import type {Run} from '../types';

vi.mock('../api',async()=>{
  const actual=await vi.importActual<any>('../api');
  return {...actual,runApi:{...actual.runApi,approve:vi.fn(),regenerateStage:vi.fn(),artifacts:vi.fn(),artifact:vi.fn(),saveDraft:vi.fn()}};
});

/** 基准运行：阶段一待确认（最常见的中间态）。 */
const baseRun=(over:Partial<Run>={}):Run=>({
  taskId:'run-1',runId:'r1',status:'awaiting_review',currentStage:'analysis',
  stageStates:{analysis:'awaiting_review',plan:'pending',cases:'pending'},
  stageMeta:{analysis:{model:'m',tokens:1234,durationMs:2000,status:'succeeded'}},
  modules:{},errors:[],
  ...over,
});

function renderSteps(run:Run,stage:'analysis'|'plan'|'cases'='analysis'){
  const onStageChange=vi.fn(); const onRefresh=vi.fn();
  render(<MemoryRouter><StageSteps run={run} stage={stage} onStageChange={onStageChange} onRefresh={onRefresh}/></MemoryRouter>);
  return {onStageChange,onRefresh};
}

describe('StageSteps 三步式阶段',()=>{
  afterEach(cleanup);
  beforeEach(()=>{
    vi.clearAllMocks();
    vi.mocked(runApi.artifacts).mockResolvedValue([]);
    vi.mocked(runApi.approve).mockResolvedValue({} as any);
    vi.mocked(runApi.regenerateStage).mockResolvedValue({discarded:[]} as any);
  });

  it('把三个阶段分别呈现，而不是一个全局进度条',async()=>{
    renderSteps(baseRun());
    expect(screen.getByText('① 需求分析')).toBeInTheDocument();
    expect(screen.getByText('② 测试方案')).toBeInTheDocument();
    expect(screen.getByText('③ 用例设计')).toBeInTheDocument();
  });

  it('每个阶段显示各自的相位标签',async()=>{
    renderSteps(baseRun());
    // 阶段一待确认；阶段二/三未开始
    expect(screen.getByText('待确认')).toBeInTheDocument();
    expect(screen.getAllByText('待开始').length).toBe(2);
  });

  it('运行中的阶段显示为「运行中」而非「待开始」',async()=>{
    // stageStates 里没有记录「正在跑」，只有 currentStage 有 —— 两者都要看
    renderSteps(baseRun({status:'running',currentStage:'plan',
      stageStates:{analysis:'approved',plan:'',cases:''}}));
    const planCard=screen.getByText('② 测试方案').closest('.ant-card') as HTMLElement;
    expect(within(planCard).getByText('运行中')).toBeInTheDocument();
  });

  it('阶段三运行中显示真实的模块进度（不编造平滑百分比）',async()=>{
    renderSteps(baseRun({status:'running',currentStage:'cases',
      stageStates:{analysis:'approved',plan:'approved',cases:'running'},
      modules:{A:'succeeded',B:'running',C:'failed',D:'skipped'}}),'cases');
    // 4 个模块中 2 个已落定（succeeded + skipped）
    expect(screen.getByText('2/4 模块')).toBeInTheDocument();
  });

  it('只在待确认且是当前阶段时提供「确认并进入下一步」',async()=>{
    renderSteps(baseRun());
    expect(screen.getByRole('button',{name:/确认并进入下一步/})).toBeInTheDocument();
    cleanup();
    // 阶段二未开始 → 不应出现确认按钮
    renderSteps(baseRun({stageStates:{analysis:'approved',plan:'pending',cases:'pending'}}));
    expect(screen.queryByRole('button',{name:/确认并进入下一步/})).not.toBeInTheDocument();
  });

  it('阶段三的确认按钮文案是「完成并结束」',async()=>{
    renderSteps(baseRun({status:'awaiting_review',currentStage:'cases',
      stageStates:{analysis:'approved',plan:'approved',cases:'awaiting_review'}}),'cases');
    expect(screen.getByRole('button',{name:/完成并结束/})).toBeInTheDocument();
  });

  it('确认动作调用 approve（不再要求先手动保存草稿）',async()=>{
    const {onRefresh}=renderSteps(baseRun());
    fireEvent.click(screen.getByRole('button',{name:/确认并进入下一步/}));
    await waitFor(()=>expect(runApi.approve).toHaveBeenCalledWith('run-1','analysis'));
    await waitFor(()=>expect(onRefresh).toHaveBeenCalled());
  });

  it('每个有产物的阶段都可以单独重跑',async()=>{
    renderSteps(baseRun({stageStates:{analysis:'approved',plan:'awaiting_review',cases:'pending'}}));
    // 阶段一已确认、阶段二待确认 → 两者都应可重跑
    expect(screen.getAllByRole('button',{name:/重跑/}).length).toBeGreaterThanOrEqual(2);
  });

  it('运行进行中不提供重跑（避免与正在跑的阶段冲突）',async()=>{
    renderSteps(baseRun({status:'running',currentStage:'plan',
      stageStates:{analysis:'approved',plan:'running',cases:'pending'}}));
    expect(screen.queryByRole('button',{name:/重跑/})).not.toBeInTheDocument();
  });

  it('重跑前有二次确认，且说明会影响下游',async()=>{
    renderSteps(baseRun({stageStates:{analysis:'approved',plan:'awaiting_review',cases:'pending'}}));
    fireEvent.click(screen.getAllByRole('button',{name:/重跑/})[0]);
    expect(await screen.findByText(/重跑「① 需求分析」？/)).toBeInTheDocument();
    expect(screen.getByText(/及其下游产物会被丢弃/)).toBeInTheDocument();
  });

  it('失败阶段给出错误详情与「重跑」出口',async()=>{
    renderSteps(baseRun({status:'failed',currentStage:'plan',errors:['门禁未通过：覆盖不足'],
      stageStates:{analysis:'approved',plan:'failed',cases:'pending'},
      stageMeta:{analysis:{status:'succeeded'},plan:{status:'failed',gate:{errors:['a','b']}}}}));
    expect(screen.getByText(/该阶段失败（门禁 2 项）/)).toBeInTheDocument();
    expect(screen.getByText(/门禁未通过：覆盖不足/)).toBeInTheDocument();
    // 有产物/失败阶段都应能重跑
    expect(screen.getAllByRole('button',{name:/重跑/}).length).toBeGreaterThanOrEqual(1);
  });

  it('展示每阶段的耗时/模型/token（拆分的意义所在）',async()=>{
    renderSteps(baseRun());
    expect(screen.getByText('2s')).toBeInTheDocument();
    expect(screen.getByText('m')).toBeInTheDocument();
    expect(screen.getByText('1,234 tok')).toBeInTheDocument();
  });

  it('未开始的阶段不显示「查看产物」',async()=>{
    renderSteps(baseRun({stageStates:{analysis:'approved',plan:'pending',cases:'pending'}}));
    const planCard=screen.getByText('② 测试方案').closest('.ant-card') as HTMLElement;
    const casesCard=screen.getByText('③ 用例设计').closest('.ant-card') as HTMLElement;
    expect(within(planCard).queryByRole('button',{name:'查看产物'})).not.toBeInTheDocument();
    expect(within(casesCard).queryByRole('button',{name:'查看产物'})).not.toBeInTheDocument();
    // 已确认的阶段一则可以查看产物（这是正确行为，不是遗漏）
    const analysisCard=screen.getByText('① 需求分析').closest('.ant-card') as HTMLElement;
    expect(within(analysisCard).getByRole('button',{name:'查看产物'})).toBeInTheDocument();
  });
});
