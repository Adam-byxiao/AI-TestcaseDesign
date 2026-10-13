import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import ExportDialog from '../components/ExportDialog';
import {defaultExportName,fetchExport,saveBlob} from '../api';

vi.mock('../api',async()=>{
  const actual=await vi.importActual<any>('../api');
  // 只替换网络/对话框相关函数；`defaultExportName` 保留真实实现（纯函数，需要真实行为）
  return {...actual,fetchExport:vi.fn(),saveBlob:vi.fn(),canPickSaveLocation:vi.fn(()=>true)};
});

/** 真实（未被 mock 的）`saveBlob`：它要操作 `showSaveFilePicker`/DOM，必须测真实实现。 */
async function realSaveBlob(){
  const actual=await vi.importActual<any>('../api');
  return actual.saveBlob as typeof saveBlob;
}

describe('导出流程',()=>{
  afterEach(()=>{cleanup();vi.unstubAllGlobals()});
  beforeEach(()=>{
    vi.clearAllMocks();
    vi.mocked(fetchExport).mockResolvedValue(new Blob(['x']));
    vi.mocked(saveBlob).mockResolvedValue('saved');
  });

  it('默认文件名含 product/version/日期与正确扩展名',()=>{
    const name=defaultExportName('PRD','v0.1','excel',new Date('2026-10-13T00:00:00'));
    expect(name).toBe('PRD-v0.1-用例集-20261013.xlsx');
    expect(defaultExportName('','','xmind',new Date('2026-10-13T00:00:00')))
      .toBe('用例集-20261013.xmind');
  });

  it('对话框可改文件名，并据此命名下载',async()=>{
    render(<ExportDialog open runId="run-1" product="PRD" version="v0.1" onClose={()=>{}}/>);
    const input=screen.getByPlaceholderText(/用例集/) as HTMLInputElement;
    fireEvent.change(input,{target:{value:'我的验收用例'}});
    expect(screen.getByText('我的验收用例.xlsx')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button',{name:/选择位置并保存/}));
    await waitFor(()=>expect(fetchExport).toHaveBeenCalledWith('run-1','excel',
      expect.objectContaining({filename:'我的验收用例.xlsx'})));
    await waitFor(()=>expect(saveBlob).toHaveBeenCalledWith(expect.any(Blob),'我的验收用例.xlsx','excel'));
  });

  it('用户只写主名时自动补扩展名',async()=>{
    render(<ExportDialog open runId="run-1" product="P" version="v1" onClose={()=>{}}/>);
    const input=screen.getByPlaceholderText(/用例集/);
    fireEvent.change(input,{target:{value:'report'}});
    expect(screen.getByText('report.xlsx')).toBeInTheDocument();
  });

  it('切换到 XMind 时扩展名与专属选项同步变化',async()=>{
    render(<ExportDialog open runId="run-1" product="P" version="v1" onClose={()=>{}}/>);
    fireEvent.click(screen.getByText('XMind 思维导图'));
    await waitFor(()=>expect(screen.getByText('导图粒度')).toBeInTheDocument());
    expect(screen.queryByText('Sheet 名')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('到步骤'));
    fireEvent.click(screen.getByRole('button',{name:/选择位置并保存/}));
    await waitFor(()=>expect(fetchExport).toHaveBeenCalledWith('run-1','xmind',
      expect.objectContaining({xmindMode:'full'})));
  });

  it('Excel 可自定义 sheet 名',async()=>{
    render(<ExportDialog open runId="run-1" product="P" version="v1" onClose={()=>{}}/>);
    const sheet=screen.getByDisplayValue('用例明细');
    fireEvent.change(sheet,{target:{value:'我的表'}});
    fireEvent.click(screen.getByRole('button',{name:/选择位置并保存/}));
    await waitFor(()=>expect(fetchExport).toHaveBeenCalledWith('run-1','excel',
      expect.objectContaining({sheetName:'我的表'})));
  });

  it('保存成功后关闭对话框',async()=>{
    const onClose=vi.fn();
    render(<ExportDialog open runId="run-1" product="P" version="v1" onClose={onClose}/>);
    fireEvent.click(screen.getByRole('button',{name:/选择位置并保存/}));
    await waitFor(()=>expect(onClose).toHaveBeenCalled());
  });

  it('用户在系统对话框取消时不报错、不关闭',async()=>{
    vi.mocked(saveBlob).mockResolvedValue('cancelled');
    const onClose=vi.fn();
    render(<ExportDialog open runId="run-1" product="P" version="v1" onClose={onClose}/>);
    fireEvent.click(screen.getByRole('button',{name:/选择位置并保存/}));
    await waitFor(()=>expect(saveBlob).toHaveBeenCalled());
    expect(onClose).not.toHaveBeenCalled();
  });

  it('后端校验失败时从 Blob 里解析出可读错误（不是笼统的「导出失败」）',async() => {
    // responseType=blob 会把错误响应也包成 Blob，必须读回文本才能拿到 detail；
    // 只断言「弹出了错误」没有意义 —— 关键是错误文案来自后端 detail。
    const errorBlob=new Blob([JSON.stringify({detail:'该运行尚无用例产物'})],{type:'application/json'});
    vi.mocked(fetchExport).mockRejectedValue({response:{data:errorBlob}});
    const onClose=vi.fn();
    render(<ExportDialog open runId="run-1" product="P" version="v1" onClose={onClose}/>);
    fireEvent.click(screen.getByRole('button',{name:/选择位置并保存/}));
    await waitFor(()=>expect(document.body.textContent).toContain('该运行尚无用例产物'));
    // 失败不应关闭对话框，用户可以改设置重试
    expect(onClose).not.toHaveBeenCalled();
  });

  it('浏览器不支持另存为时改用下载并给出提示',async()=>{
    const api=await import('../api');
    vi.mocked(api.canPickSaveLocation).mockReturnValue(false);
    render(<ExportDialog open runId="run-1" product="P" version="v1" onClose={()=>{}}/>);
    expect(screen.getByText(/不支持「另存为」对话框/)).toBeInTheDocument();
    expect(screen.getByRole('button',{name:/下载/})).toBeInTheDocument();
  });
});

describe('fetchExport（真实实现）',()=>{
  afterEach(()=>vi.unstubAllGlobals());

  it('以 blob 拉取，并把文件名与格式选项作为查询参数传给后端',async()=>{
    // **核心回归**：文件名必须真的到达后端 —— 否则「可改文件名」只是 UI 假象
    // （浏览器只会用 URL 的 Content-Disposition 名，用户的输入被忽略）。
    const actual=await vi.importActual<any>('../api');
    const get=vi.fn().mockResolvedValue({data:new Blob(['x'])});
    const spy=vi.spyOn(actual.api,'get').mockImplementation(get);
    try{
      await actual.fetchExport('run-1','xmind',{filename:'我的导图.xmind',xmindMode:'full',sheetName:'表'});
      expect(get).toHaveBeenCalledWith('/exports/run-1/xmind',expect.objectContaining({
        responseType:'blob',
        params:expect.objectContaining({filename:'我的导图.xmind',xmind_mode:'full'}),
      }));
    }finally{ spy.mockRestore(); }
  });

  it('未提供选项时不传空参数（保持后端默认行为）',async()=>{
    const actual=await vi.importActual<any>('../api');
    const get=vi.fn().mockResolvedValue({data:new Blob(['x'])});
    const spy=vi.spyOn(actual.api,'get').mockImplementation(get);
    try{
      await actual.fetchExport('run-1','excel');
      const params=get.mock.calls[0][1].params;
      expect(params.filename).toBeUndefined();
      expect(params.xmind_mode).toBeUndefined();
    }finally{ spy.mockRestore(); }
  });
});

describe('saveBlob（真实实现）',()=>{
  afterEach(()=>vi.unstubAllGlobals());

  it('支持另存为时写入用户选择的位置',async()=>{
    const write=vi.fn(); const close=vi.fn();
    const picker=vi.fn().mockResolvedValue({createWritable:async()=>({write,close})});
    vi.stubGlobal('showSaveFilePicker',picker);
    const outcome=await (await realSaveBlob())(new Blob(['x']),'a.xlsx','excel');
    expect(outcome).toBe('saved');
    expect(write).toHaveBeenCalled();
    expect(close).toHaveBeenCalled();
  });

  it('用户取消（AbortError）视为取消而不是失败',async()=>{
    const abort=Object.assign(new Error('aborted'),{name:'AbortError'});
    vi.stubGlobal('showSaveFilePicker',vi.fn().mockRejectedValue(abort));
    expect(await (await realSaveBlob())(new Blob(['x']),'a.xlsx','excel')).toBe('cancelled');
  });

  it('不支持另存为时回退为指定文件名的下载',async()=>{
    vi.stubGlobal('showSaveFilePicker',undefined);
    const click=vi.fn();
    // jsdom 的 `createElement` 返回真实元素，直接拦截 click 即可 ——
    // 若把 createElement 整个替换成普通对象，appendChild 会因不是 Node 而失败。
    const created=vi.spyOn(document,'createElement').mockImplementation(((tag:string)=>{
      const el=document.implementation.createHTMLDocument('').createElement(tag);
      if(tag==='a'){ (el as any).click=click; }
      return el;
    }) as any);
    const outcome=await (await realSaveBlob())(new Blob(['x']),'报告.xlsx','excel');
    expect(outcome).toBe('downloaded');
    expect(click).toHaveBeenCalled();
    created.mockRestore();
  });

  it('另存为抛非取消异常时回退为下载（保证用户仍能拿到文件）',async()=>{
    vi.stubGlobal('showSaveFilePicker',vi.fn().mockRejectedValue(new Error('disk full')));
    const click=vi.fn();
    const created=vi.spyOn(document,'createElement').mockImplementation(((tag:string)=>{
      const el=document.implementation.createHTMLDocument('').createElement(tag);
      if(tag==='a'){ (el as any).click=click; }
      return el;
    }) as any);
    expect(await (await realSaveBlob())(new Blob(['x']),'a.xlsx','excel')).toBe('downloaded');
    expect(click).toHaveBeenCalled();
    created.mockRestore();
  });
});
