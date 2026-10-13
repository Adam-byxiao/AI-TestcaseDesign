import {cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import StageEditor from '../components/StageEditor';
import {runApi} from '../api';

vi.mock('../api',()=>({runApi:{artifacts:vi.fn(),artifact:vi.fn(),saveDraft:vi.fn()}}));

/** 15 条用例 → antd Table 默认每页 10 条，因此必然分两页。 */
const CASES=Array.from({length:15},(_,i)=>({
  id:`TC-A-${String(i+1).padStart(3,'0')}`,module:'A',subModule:'S',
  title:`场景 ${i+1}`,design_method:'场景法',priority:'P1',
  tags:['@regression'],requirement:[`REQ-A-${String(i+1).padStart(3,'0')}`],
  preconditions:['已登录'],testData:[],
  steps:[{action:'goto',target:'首页',data:''}],
  expected:[`场景 ${i+1} 的预期结果`],
}));

/** 取某条用例所在的 `<tr>`。 */
function rowOf(caseId:string):HTMLElement{
  return screen.getByText(caseId).closest('tr') as HTMLElement;
}

/** 切到第 N 页。
 *  用 `.ant-pagination-item` 精确定位 —— `getByTitle` 会同时命中表格内的元素。 */
async function gotoPage(page:number){
  const items=Array.from(document.querySelectorAll('.ant-pagination-item'));
  const target=items.find(el=>el.getAttribute('title')===String(page));
  if(!target)throw new Error(`找不到分页项 ${page}`);
  fireEvent.click(target.querySelector('a')||target);
  await waitFor(()=>{
    const active=document.querySelector('.ant-pagination-item-active');
    expect(active?.getAttribute('title')).toBe(String(page));
  });
}

/** 「详情」按钮的可访问名是「详 情」（antd 在 CJK 之间插了空格）。 */
const DETAIL=/详\s*情/;

describe('StageEditor 分页下的行定位',()=>{
  afterEach(cleanup);
  beforeEach(()=>{
    vi.clearAllMocks();
    vi.mocked(runApi.artifacts).mockResolvedValue([{path:'cases/cases/A.json'}]);
    vi.mocked(runApi.artifact).mockResolvedValue({cases:CASES});
    vi.mocked(runApi.saveDraft).mockResolvedValue({});
  });

  it('第 2 页点「详情」必须打开该行的用例，而不是第 1 页同位置的用例',async()=>{
    // **核心回归**：antd Table 的 `render(value, record, index)` 中
    // `index` 是**当前页内**序号，不是全局下标。原先用它做选择，
    // 导致第 2 页第 1 行（TC-A-011）打开的是 rows[0]（TC-A-001）。
    render(<StageEditor runId="run-1" stage="cases"/>);
    await screen.findByText('TC-A-001');
    await gotoPage(2);

    expect(screen.getByText('TC-A-011')).toBeInTheDocument();
    fireEvent.click(within(rowOf('TC-A-011')).getByRole('button',{name:DETAIL}));

    await waitFor(()=>expect(screen.getByText('编辑 TC-A-011')).toBeInTheDocument());
    expect(screen.queryByText('编辑 TC-A-001')).not.toBeInTheDocument();
  });

  it('第 2 页点第 3 行的「详情」同样定位正确',async()=>{
    render(<StageEditor runId="run-1" stage="cases"/>);
    await screen.findByText('TC-A-001');
    await gotoPage(2);
    fireEvent.click(within(rowOf('TC-A-013')).getByRole('button',{name:DETAIL}));
    await waitFor(()=>expect(screen.getByText('编辑 TC-A-013')).toBeInTheDocument());
  });

  it('第 2 页修改标题不得改到第 1 页的同位置用例（数据损坏）',async()=>{
    // **比显示错误更严重**：原先 `change(i,...)` 也用了页内序号，
    // 于是「改第 2 页第 1 行的标题」会**静默改到第 1 页第 1 行**。
    render(<StageEditor runId="run-1" stage="cases"/>);
    await screen.findByText('TC-A-001');
    await gotoPage(2);

    const input=within(rowOf('TC-A-011')).getByDisplayValue('场景 11');
    fireEvent.change(input,{target:{value:'场景 11 已改'}});
    fireEvent.click(screen.getByRole('button',{name:/保存修改/}));

    await waitFor(()=>expect(runApi.saveDraft).toHaveBeenCalled());
    const saved=(vi.mocked(runApi.saveDraft).mock.calls[0] as any)[2].cases as any[];
    expect(saved[10].title).toBe('场景 11 已改');   // 第 11 条 = 第 2 页第 1 行
    expect(saved[0].title).toBe('场景 1');          // 第 1 条必须**未被动过**
  });

  it('第 2 页切换状态不得改到第 1 页的同位置用例',async()=>{
    render(<StageEditor runId="run-1" stage="cases"/>);
    await screen.findByText('TC-A-001');
    await gotoPage(2);
    const row=rowOf('TC-A-012');
    fireEvent.mouseDown(within(row).getByRole('combobox'));
    fireEvent.click(await screen.findByTitle('needs_review'));
    fireEvent.click(screen.getByRole('button',{name:/保存修改/}));
    await waitFor(()=>expect(runApi.saveDraft).toHaveBeenCalled());
    const saved=(vi.mocked(runApi.saveDraft).mock.calls[0] as any)[2].cases as any[];
    expect(saved[11].status).toBe('needs_review');
    // fixture 本来没有 status 字段 → 未被改动的行应保持 undefined（而不是被"顺手"初始化）
    expect(saved[1].status).toBeUndefined();
  });

  it('在抽屉里改标题也只影响被打开的那一条',async()=>{
    render(<StageEditor runId="run-1" stage="cases"/>);
    await screen.findByText('TC-A-001');
    await gotoPage(2);
    fireEvent.click(within(rowOf('TC-A-014')).getByRole('button',{name:DETAIL}));
    await screen.findByText('编辑 TC-A-014');

    const drawer=document.querySelector('.ant-drawer-content') as HTMLElement;
    fireEvent.change(within(drawer).getByDisplayValue('场景 14'),{target:{value:'抽屉改名'}});
    fireEvent.click(screen.getByRole('button',{name:/保存修改/}));
    await waitFor(()=>expect(runApi.saveDraft).toHaveBeenCalled());
    const saved=(vi.mocked(runApi.saveDraft).mock.calls[0] as any)[2].cases as any[];
    expect(saved[13].title).toBe('抽屉改名');
    expect(saved[3].title).toBe('场景 4');
  });

  it('第 1 页仍然正常（回归保护）',async()=>{
    render(<StageEditor runId="run-1" stage="cases"/>);
    await screen.findByText('TC-A-001');
    fireEvent.click(within(rowOf('TC-A-002')).getByRole('button',{name:DETAIL}));
    await waitFor(()=>expect(screen.getByText('编辑 TC-A-002')).toBeInTheDocument());
  });
});
