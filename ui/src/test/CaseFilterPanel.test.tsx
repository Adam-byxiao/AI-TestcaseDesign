import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import CaseFilterPanel from '../components/CaseFilterPanel';
import {fetchFacets,selectionLabel,selectionSuffix} from '../api';
import {EMPTY_SELECTION} from '../types';
import type {CaseSelection} from '../types';

vi.mock('../api',async()=>{
  const actual=await vi.importActual<any>('../api');
  return {...actual,fetchFacets:vi.fn()};
});

const FACETS={total:5,matched:5,
  modules:[{value:'KB',count:3},{value:'AGENT',count:2}],
  priorities:[{value:'P0',count:2},{value:'P1',count:3}],
  tags:[{value:'@regression',count:5},{value:'@smoke',count:2}]};

function renderPanel(value:CaseSelection=EMPTY_SELECTION,onChange=vi.fn()){
  render(<CaseFilterPanel runId="run-1" value={value} onChange={onChange}/>);
  return onChange;
}

describe('CaseFilterPanel 批量选择',()=>{
  afterEach(cleanup);
  beforeEach(()=>{
    vi.clearAllMocks();
    vi.mocked(fetchFacets).mockResolvedValue(FACETS as any);
  });

  it('渲染三个维度及其数量',async()=>{
    renderPanel();
    expect(await screen.findByText('KB')).toBeInTheDocument();
    expect(screen.getByText('AGENT')).toBeInTheDocument();
    expect(screen.getByText('P0')).toBeInTheDocument();
    expect(screen.getByText('@smoke')).toBeInTheDocument();
  });

  it('用 ignore=* 一次请求取全部维度（避免多次响应互相覆盖）',async()=>{
    // **核心回归**：若逐维度请求再合并，三个响应会互相覆盖，
    // 计数会随机取决于哪次最后到达。
    renderPanel();
    await waitFor(()=>expect(fetchFacets).toHaveBeenCalled());
    expect(fetchFacets).toHaveBeenCalledWith('run-1',expect.anything(),'*');
    expect(vi.mocked(fetchFacets).mock.calls.length).toBe(1);
  });

  it('点击维度值把选择条件交给父级（维度内多选累加）',async()=>{
    const onChange=renderPanel();
    fireEvent.click(await screen.findByText('KB'));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({modules:['KB']}));
  });

  it('再次点击同一值取消选择',async()=>{
    const onChange=renderPanel({...EMPTY_SELECTION,modules:['KB']});
    fireEvent.click(await screen.findByText('KB'));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({modules:[]}));
  });

  it('跨维度选择互不覆盖（复合条件的构造）',async()=>{
    const onChange=renderPanel({...EMPTY_SELECTION,modules:['KB']});
    fireEvent.click(await screen.findByText('@regression'));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({
      modules:['KB'],tags:['@regression']}));
  });

  it('显示命中数量与排除数量',async()=>{
    vi.mocked(fetchFacets).mockResolvedValue({...FACETS,matched:3} as any);
    // 必须有**生效的选择**才会显示「已选 N/M」；否则显示的是总条数
    renderPanel({...EMPTY_SELECTION,modules:['KB']});
    expect(await screen.findByText(/已选 3\/5 条/)).toBeInTheDocument();
    expect(screen.getByText(/排除 2/)).toBeInTheDocument();
  });

  it('无筛选时提示总条数',async()=>{
    renderPanel();
    expect(await screen.findByText(/共 5 条用例/)).toBeInTheDocument();
  });

  it('有筛选时给出语义说明（维度内并集、跨维度交集）',async()=>{
    renderPanel({...EMPTY_SELECTION,modules:['KB'],tags:['@regression']});
    expect(await screen.findByText(/模块=KB；标签=@regression/)).toBeInTheDocument();
    expect(screen.getByText(/维度内取并集、跨维度取交集/)).toBeInTheDocument();
  });

  it('清空按钮重置全部条件（含关键词）',async()=>{
    const onChange=renderPanel({modules:['KB'],priorities:['P0'],tags:['@smoke'],keyword:'x'});
    fireEvent.click(screen.getByRole('button',{name:'清空全部条件'}));
    expect(onChange).toHaveBeenCalledWith({modules:[],priorities:[],tags:[],keyword:''});
  });

  it('请求失败时给出提示而不崩',async()=>{
    vi.mocked(fetchFacets).mockRejectedValue({response:{data:{detail:'该运行尚无用例产物'}}});
    renderPanel();
    expect(await screen.findByText('该运行尚无用例产物')).toBeInTheDocument();
  });

  it('禁用时仍展示可选项，但不响应点击',async()=>{
    // **核心行为**：禁用只阻止交互，不应隐藏信息 ——
    // 否则用户看不出是「没得选」还是「被禁用了」。
    const onChange=vi.fn();
    render(<CaseFilterPanel runId="run-1" value={EMPTY_SELECTION} onChange={onChange} disabled/>);
    await screen.findByText('KB');
    fireEvent.click(screen.getByText('KB'));
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe('筛选描述与文件名片段',()=>{
  it('selectionLabel 描述各维度',()=>{
    expect(selectionLabel(undefined)).toBe('全部用例');
    expect(selectionLabel(EMPTY_SELECTION)).toBe('全部用例');
    expect(selectionLabel({...EMPTY_SELECTION,modules:['KB','AGENT']})).toBe('模块=KB,AGENT');
    expect(selectionLabel({...EMPTY_SELECTION,modules:['KB'],priorities:['P0']}))
      .toBe('模块=KB；优先级=P0');
    expect(selectionLabel({...EMPTY_SELECTION,tags:['@smoke']})).toBe('标签=@smoke');
    expect(selectionLabel({...EMPTY_SELECTION,keyword:'REQ-KB'})).toBe('关键词=REQ-KB');
  });

  it('selectionSuffix 生成文件安全片段（与服务端同构）',()=>{
    expect(selectionSuffix(undefined)).toBe('');
    expect(selectionSuffix(EMPTY_SELECTION)).toBe('');
    expect(selectionSuffix({...EMPTY_SELECTION,modules:['KB','AGENT']})).toBe('KB-AGENT');
    expect(selectionSuffix({...EMPTY_SELECTION,modules:['KB'],priorities:['P0']})).toBe('KB-P0');
    // 标签去掉 @（文件名里更干净），危险字符替换
    expect(selectionSuffix({...EMPTY_SELECTION,tags:['@smoke']})).toBe('smoke');
    expect(selectionSuffix({...EMPTY_SELECTION,modules:['a/b']})).not.toContain('/');
  });
});
