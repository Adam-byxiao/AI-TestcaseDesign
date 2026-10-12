import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import {MemoryRouter} from 'react-router-dom';
import PrdPage from '../pages/PrdPage';
import {prdApi} from '../api';

vi.mock('../api',()=>({prdApi:{list:vi.fn(),get:vi.fn(),upload:vi.fn(),uploadMany:vi.fn(),update:vi.fn(),reanalyze:vi.fn()}}));

const BUNDLE={primary:'PRD.md',textChars:120,sourceCount:3,assetCount:1,imageCount:1,
  kinds:['markdown','image'],warnings:0};
const PRD={prdId:'prd-1',filename:'PRD.md',path:'prds/prd-1/我的PRD/PRD.md',dir:'prds/prd-1',
  version:'v0.1',sha256:'abc',chars:120,primary:'PRD.md',bundle:BUNDLE,warnings:[],
  files:[{name:'PRD.md',size:120,relpath:'我的PRD/PRD.md',sha256:'a'},
         {name:'x.png',size:51,relpath:'我的PRD/图片和附件/x.png',sha256:'b'}],
  createdAt:'2026-10-12T10:09:21+00:00'};

/** 构造 File；jsdom 不提供 webkitRelativePath，需手工挂上。 */
function makeFile(name:string,rel?:string):File{
  const f=new File(['# x'],name,{type:'text/markdown'});
  if(rel) Object.defineProperty(f,'webkitRelativePath',{value:rel});
  return f;
}

/** 渲染并等待挂载完成。
 *
 *  URL 带 `?id=prd-1`，页面才会自动选中该 PRD 并渲染「摄取结果」面板 ——
 *  页面本身不自动选中（`choose` 仅由 `?id=` 或用户点选触发），
 *  早期测试误以为会自动选中，导致一直等不到面板。 */
function renderPage(prdId:string|null='prd-1'){
  const entry=prdId?`/?id=${prdId}`:'/';
  return render(<MemoryRouter initialEntries={[entry]}><PrdPage/></MemoryRouter>);
}

async function ready(){
  // 等两个 file input 出现（普通多选 / 文件夹）。
  // 不用 `findByRole('button',{name:'上传文件'})`：按钮内的图标带
  // `aria-label="file-text"`，会参与可访问名计算，导致按名匹配失败。
  await waitFor(()=>expect(document.querySelectorAll('input[type="file"]')).toHaveLength(2));
}

/** 取两个 file input：普通多选 / 文件夹。 */
function fileInputs(){
  const inputs=document.querySelectorAll('input[type="file"]');
  return {plain:inputs[0] as HTMLInputElement,directory:inputs[1] as HTMLInputElement};
}

describe('PrdPage 上传交互',()=>{
  afterEach(cleanup);
  beforeEach(()=>{
    vi.clearAllMocks();
    vi.mocked(prdApi.list).mockResolvedValue([PRD]);
    vi.mocked(prdApi.get).mockResolvedValue({...PRD,content:'# PRD'});
    vi.mocked(prdApi.uploadMany).mockResolvedValue({...PRD});
  });

  it('多文件选择只发起一次上传请求',async()=>{
    /** **核心回归**：antd 对多文件/文件夹会逐文件调用回调
     *  （`customRequest({file})` 每文件一次、`beforeUpload(file,fileList)` 亦然）。
     *  原实现直接在回调里上传 → 选 N 个文件发 N 次请求 →
     *  PRD 列表堆了 8 条同名记录（真实事故）。
     *  此处一次选出 12 个文件，断言**只发一次**且携带全部 12 个。 */
    renderPage();
    await ready();

    const files=Array.from({length:12},(_,i)=>makeFile(`f${i}.md`));
    fireEvent.change(fileInputs().plain,{target:{files}});

    await waitFor(()=>expect(prdApi.uploadMany).toHaveBeenCalledTimes(1));
    expect(vi.mocked(prdApi.uploadMany).mock.calls[0][0] as File[]).toHaveLength(12);
  });

  it('文件夹选择只发起一次上传请求，且保留相对路径',async()=>{
    renderPage();
    await ready();

    const files=[
      makeFile('PRD.md','我的PRD/PRD.md'),
      ...Array.from({length:11},(_,i)=>makeFile(`p${i}.png`,`我的PRD/图片和附件/p${i}.png`)),
    ];
    fireEvent.change(fileInputs().directory,{target:{files}});

    await waitFor(()=>expect(prdApi.uploadMany).toHaveBeenCalledTimes(1));
    const sent=vi.mocked(prdApi.uploadMany).mock.calls[0][0] as File[];
    expect(sent).toHaveLength(12);
    expect((sent[0] as any).webkitRelativePath).toBe('我的PRD/PRD.md');
  });

  it('对同一批文件重复触发 change 不会按文件数放大请求',async()=>{
    /** 防御 antd 在不同版本/浏览器下分批策略变化：请求数不得随文件数增长。 */
    renderPage();
    await ready();

    const files=[makeFile('a.md'),makeFile('b.md')];
    const {plain}=fileInputs();
    fireEvent.change(plain,{target:{files}});
    fireEvent.change(plain,{target:{files}});

    await waitFor(()=>expect(prdApi.uploadMany).toHaveBeenCalled());
    const calls=vi.mocked(prdApi.uploadMany).mock.calls;
    expect(calls.length).toBeLessThanOrEqual(2);          // 两次 change = 至多两批
    for(const [batch] of calls) expect((batch as File[]).length).toBeLessThanOrEqual(4);
  });

  it('单文件上传也走同一入口',async()=>{
    renderPage();
    await ready();
    fireEvent.change(fileInputs().plain,{target:{files:[makeFile('only.md')]}});
    await waitFor(()=>expect(prdApi.uploadMany).toHaveBeenCalledTimes(1));
    expect(vi.mocked(prdApi.uploadMany).mock.calls[0][0] as File[]).toHaveLength(1);
  });

  it('展示摄取结果：正文来源、格式标签、图片数与文件清单',async()=>{
    renderPage();
    expect(await screen.findByText('摄取结果')).toBeInTheDocument();
    expect(screen.getByText('Markdown')).toBeInTheDocument();
    expect(screen.getByText('图片')).toBeInTheDocument();
    expect(screen.getByText('1 张图片')).toBeInTheDocument();
    expect(screen.getByText('我的PRD/图片和附件/x.png')).toBeInTheDocument();
    expect(screen.getByText('正文')).toBeInTheDocument();     // 主文件角色标签
  });

  it('解析告警可见（上传后不致静默）',async()=>{
    const warn='图片 1 跳过：远程图片默认不下载（用 --fetch-images 显式开启）';
    vi.mocked(prdApi.list).mockResolvedValue([{...PRD,warnings:[warn]}]);
    vi.mocked(prdApi.get).mockResolvedValue({...PRD,content:'# PRD',warnings:[warn]});
    renderPage();
    expect(await screen.findByText(warn)).toBeInTheDocument();
  });

  it('无 PRD 时给出上传指引',async()=>{
    vi.mocked(prdApi.list).mockResolvedValue([]);
    renderPage(null);
    await ready();
    expect(screen.getByText(/支持上传单个文件、多个文件，或整个文件夹/)).toBeInTheDocument();
    expect(screen.getByText(/Word\(\.docx\)/)).toBeInTheDocument();
  });

  it('上传成功后自动选中新 PRD 并刷新列表',async()=>{
    renderPage(null);
    await ready();
    fireEvent.change(fileInputs().plain,{target:{files:[makeFile('new.md')]}});
    await waitFor(()=>expect(prdApi.uploadMany).toHaveBeenCalledTimes(1));
    await waitFor(()=>expect(prdApi.get).toHaveBeenCalledWith('prd-1'));
  });
});
