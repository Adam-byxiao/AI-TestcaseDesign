import {cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import {MemoryRouter} from 'react-router-dom';
import StageSteps from '../components/StageSteps';
import {api,runApi} from '../api';

vi.mock('../api',async()=>{
  const actual=await vi.importActual<any>('../api');
  return {...actual,runApi:{...actual.runApi,approve:vi.fn(),regenerateStage:vi.fn(),
    artifacts:vi.fn(),artifact:vi.fn(),saveDraft:vi.fn()}};
});

const baseRun=(over:any={})=>({
  taskId:'run-1',runId:'r1',status:'awaiting_review',currentStage:'analysis',
  stageStates:{analysis:'awaiting_review',plan:'pending',cases:'pending'},
  stageMeta:{},modules:{},errors:[],...over});

function renderSteps(run:any){
  return render(<MemoryRouter><StageSteps run={run} stage="analysis"
    onStageChange={vi.fn()} onRefresh={vi.fn()}/></MemoryRouter>);
}

/** 某阶段卡片的 Collapse 面板是否已展开。 */
function panelActive(cardTitle:string):boolean{
  const card=screen.getByText(cardTitle).closest('.ant-card') as HTMLElement;
  const item=card.querySelector('.ant-collapse-item');
  return Boolean(item?.className.includes('ant-collapse-item-active'));
}

describe('「查看产物」按钮',()=>{
  afterEach(cleanup);
  beforeEach(()=>{
    vi.clearAllMocks();
    vi.mocked(runApi.artifacts).mockResolvedValue([
      {path:'analysis/requirements.json'},{path:'analysis/需求分析报告.md'},
      {path:'plan/测试方案.md'},{path:'cases/cases/A.json'}]);
    vi.mocked(runApi.artifact).mockImplementation(async(_id:string,path:string)=>{
      if(path.endsWith('requirements.json'))return {requirements:[
        {id:'REQ-A-001',module:'A',title:'登录',description:'',acceptanceCriteria:[],
         exceptionFlows:[],priority:'P0',sourceRefs:[]}]};
      if(path.endsWith('.json'))return {cases:[{id:'TC-A-001',module:'A',title:'x',
        priority:'P1',tags:[],requirement:['REQ-A-001'],steps:[],expected:[]}]};
      return '# 文档内容';
    });
    vi.mocked(runApi.saveDraft).mockResolvedValue({});
  });

  it('**点击后展开该阶段的面板**（此前只改了一个无人消费的状态）',async()=>{
    // **核心回归**：`onStageChange` 只是设置 RunPage 的一个状态，
    // 而没有任何渲染消费它；Collapse 又是非受控的 —— 于是点击毫无反应。
    renderSteps(baseRun());
    const button=await screen.findByRole('button',{name:'查看产物'});
    expect(panelActive('① 需求分析')).toBe(false);       // 初始折叠
    fireEvent.click(button);
    await waitFor(()=>expect(panelActive('① 需求分析')).toBe(true));
  });

  it('展开后加载并显示产物内容',async()=>{
    renderSteps(baseRun());
    fireEvent.click(await screen.findByRole('button',{name:'查看产物'}));
    // StageEditor 的 tab 标签出现即证明内容已挂载
    expect(await screen.findByText('结构化需求')).toBeInTheDocument();
  });

  it('再点面板头可折叠回去（受控状态仍允许用户手动收起）',async()=>{
    renderSteps(baseRun());
    fireEvent.click(await screen.findByRole('button',{name:'查看产物'}));
    await waitFor(()=>expect(panelActive('① 需求分析')).toBe(true));
    const card=screen.getByText('① 需求分析').closest('.ant-card') as HTMLElement;
    fireEvent.click(within(card).getByText('查看 / 编辑产物'));
    await waitFor(()=>expect(panelActive('① 需求分析')).toBe(false));
  });

  it('多个阶段可以各自展开（互不影响）',async()=>{
    renderSteps(baseRun({stageStates:{analysis:'approved',plan:'awaiting_review',cases:'pending'}}));
    const buttons=await screen.findAllByRole('button',{name:'查看产物'});
    expect(buttons.length).toBe(2);
    fireEvent.click(buttons[0]);
    await waitFor(()=>expect(panelActive('① 需求分析')).toBe(true));
    fireEvent.click(buttons[1]);
    await waitFor(()=>expect(panelActive('② 测试方案')).toBe(true));
    // 前一个仍保持展开 —— 用户可能想看两处对比
    expect(panelActive('① 需求分析')).toBe(true);
  });
});

describe('查询参数序列化（数组必须是重复键）',()=>{
  it('**数组序列化为重复键**，而不是 axios 默认的 `key[]=`',()=>{
    // **核心回归**：axios 默认把数组序列化成 `modules[]=KB`（带方括号），
    // 而 FastAPI 的 `List[str] = Query(None)` 只认 `modules=KB&modules=AGENT`。
    // 结果是后端**完全收不到** modules → 视为无筛选 → 用户选了条件却仍然全量导出。
    const url=api.getUri({url:'/exports/run-1/excel',
      params:{modules:['KB','AGENT'],priorities:['P0'],tags:['@smoke']}});
    expect(url).not.toContain('%5B%5D');           // 不得出现 []
    expect(url).not.toContain('[]');
    expect(url).toContain('modules=KB');
    expect(url).toContain('modules=AGENT');
    expect(url).toContain('priorities=P0');
    expect(url).toContain('tags=%40smoke');
  });

  it('空数组不发参数（空集合 ≠ 该维度不限）',()=>{
    const url=api.getUri({url:'/exports/run-1/excel',
      params:{modules:[],priorities:['P0'],tags:[]}});
    expect(url).not.toContain('modules');
    expect(url).not.toContain('tags');
    expect(url).toContain('priorities=P0');
  });

  it('标量参数照常传递',()=>{
    const url=api.getUri({url:'/exports/run-1/excel',
      params:{filename:'我的用例.xlsx',xmind_mode:'full',ignore:'*'}});
    expect(url).toContain('xmind_mode=full');
    expect(url).toContain('ignore=*');
  });

  it('undefined/null 参数被跳过',()=>{
    const url=api.getUri({url:'/exports/run-1/excel',
      params:{filename:undefined,xmind_mode:null,sheet_name:'用例明细'}});
    expect(url).not.toContain('filename');
    expect(url).not.toContain('xmind_mode');
    expect(url).toContain('sheet_name');
  });
});
