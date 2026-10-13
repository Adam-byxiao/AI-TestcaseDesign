import {useEffect,useMemo,useState} from 'react';
import {Alert,Button,Form,Input,Modal,Radio,Space,Typography,message} from 'antd';
import {DownloadOutlined,SaveOutlined} from '@ant-design/icons';
import {canPickSaveLocation,defaultExportName,fetchExport,saveBlob,selectionLabel} from '../api';
import type {ExportOptions} from '../api';
import type {CaseSelection} from '../types';
import {selectionActive} from '../types';

/** 从错误响应的 Blob 里取出后端 detail。
 *
 *  `responseType:'blob'` 会把**错误响应**也包成 Blob，因此必须读回文本。
 *  `Blob.text()` 在旧浏览器与部分测试环境（jsdom）不可用，
 *  故回退到 FileReader —— 否则用户只会看到笼统的「导出失败」。 */
async function detailFromBlob(blob:Blob):Promise<string|undefined>{
  let text='';
  try{
    if(typeof (blob as any).text==='function')text=await blob.text();
    else text=await new Promise<string>((resolve,reject)=>{
      const reader=new FileReader();
      reader.onload=()=>resolve(String(reader.result||''));
      reader.onerror=()=>reject(reader.error);
      reader.readAsText(blob);
    });
  }catch{ return undefined; }
  try{ return JSON.parse(text)?.detail; }catch{ return undefined; }
}

/** 导出对话框：让用户决定**格式、文件名、保存位置**。
 *
 *  此前的实现是 `window.open(url)` —— 文件名固定、位置只能是浏览器默认下载目录，
 *  且**无法感知失败**（后端报错时浏览器会把错误页当成文件存下来）。
 *  现在改为「拉取 Blob → 另存为」，并在不支持该 API 的浏览器上回退为
 *  指定文件名的普通下载（仍比原先多出「可改名」这一项）。 */
export default function ExportDialog({open,runId,product,version,onClose,selection}:{
  open:boolean; runId:string; product:string; version:string;
  selection?:CaseSelection; onClose:()=>void;
}){
  const [fmt,setFmt]=useState<'excel'|'xmind'>('excel');
  const [filename,setFilename]=useState('');
  const [xmindMode,setXmindMode]=useState<'case'|'full'|'module'>('case');
  const [sheetName,setSheetName]=useState('用例明细');
  const [busy,setBusy]=useState(false);

  // 每次打开都按当前格式与筛选重置默认名，避免上次的输入串到这次
  useEffect(()=>{ if(open) setFilename(defaultExportName(product,version,fmt,new Date(),selection)); },[open,fmt,product,version,selection]);

  const canPick=useMemo(()=>canPickSaveLocation(),[]);
  const extension=fmt==='excel'?'.xlsx':'.xmind';
  const filtered=selectionActive(selection);
  // 用户可能只写主名；提交前补齐扩展名，避免存出无扩展名的文件
  const resolved=useMemo(()=>{
    const name=filename.trim()||defaultExportName(product,version,fmt,new Date(),selection);
    return name.toLowerCase().endsWith(extension)?name:`${name}${extension}`;
  },[filename,extension,fmt,product,version,selection]);

  const doExport=async()=>{
    setBusy(true);
    try{
      const options:ExportOptions={filename:resolved,selection,
        xmindMode:fmt==='xmind'?xmindMode:undefined,
        sheetName:fmt==='excel'?sheetName:undefined};
      const blob=await fetchExport(runId,fmt,options);
      const outcome=await saveBlob(blob,resolved,fmt);
      if(outcome==='cancelled'){ message.info('已取消保存'); return; }
      message.success(outcome==='saved'?`已保存 ${resolved}`:`已下载 ${resolved}`);
      onClose();
    }catch(error:any){
      // 后端把校验失败写成 JSON/文本响应体，而 responseType=blob 会把它包成 Blob，
      // 因此需要先读回文本才能给出可读的错误（否则只会看到「导出失败」）。
      // 读取本身也可能失败（响应不是 Blob、或流已被消费），故整体包在 try 里，
      // 失败就退回通用文案 —— 绝不能因为「解析错误信息失败」而吞掉整个错误处理。
      let detail=error?.response?.data?.detail;
      if(!detail&&error?.response?.data instanceof Blob){
        detail=await detailFromBlob(error.response.data);
      }
      message.error(detail||error?.message||'导出失败');
    }finally{ setBusy(false); }
  };

  return <Modal open={open} onCancel={busy?undefined:onClose} title="导出用例集"
    okText={canPick?'选择位置并保存':'下载'}
    cancelText="取消" confirmLoading={busy} onOk={doExport}
    okButtonProps={{icon:canPick?<SaveOutlined/>:<DownloadOutlined/>}} width={520}>
    <Form layout="vertical" size="small">
      <Form.Item label="导出范围" style={{marginBottom:12}}>
        <Alert type={filtered?'info':'success'} showIcon
          message={selectionLabel(selection)}
          description={filtered
            ? '仅导出命中的用例；统计与需求覆盖会按该子集重算。'
            : '导出全部用例。可在运行页的「用例批量选择」中按模块/优先级/标签缩小范围。'}/>
      </Form.Item>
      <Form.Item label="格式">
        <Radio.Group value={fmt} onChange={e=>setFmt(e.target.value)} optionType="button" buttonStyle="solid"
          options={[{value:'excel',label:'Excel 工作簿'},{value:'xmind',label:'XMind 思维导图'}]}/>
      </Form.Item>
      <Form.Item label="文件名" help={<span>将保存为 <Typography.Text code>{resolved}</Typography.Text></span>}>
        <Input value={filename} onChange={e=>setFilename(e.target.value)}
          placeholder={defaultExportName(product,version,fmt,new Date(),selection)} allowClear/>
      </Form.Item>
      {fmt==='xmind'&&<Form.Item label="导图粒度" help="节点过多时系统会自动降级为「仅模块层」">
        <Radio.Group value={xmindMode} onChange={e=>setXmindMode(e.target.value)}
          options={[{value:'case',label:'到用例'},{value:'full',label:'到步骤'},{value:'module',label:'仅模块'}]}/>
      </Form.Item>}
      {fmt==='excel'&&<Form.Item label="Sheet 名">
        <Input value={sheetName} onChange={e=>setSheetName(e.target.value)} maxLength={31}/>
      </Form.Item>}
      {!canPick&&<Alert type="info" showIcon
        message="当前浏览器不支持「另存为」对话框"
        description={<Space direction="vertical" size={2}>
          <span>文件会以你指定的名字下载到浏览器默认下载目录。</span>
          <span>如需自选保存位置，请用 Chrome / Edge 打开本页面。</span>
        </Space>}/>}
    </Form>
  </Modal>;
}
