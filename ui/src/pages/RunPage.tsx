import {useEffect,useState} from 'react'; import {useParams} from 'react-router-dom'; import {Card,Timeline,Button,Space,Alert,Tag,message,Table,Typography} from 'antd'; import {runApi} from '../api'; import {useRun} from '../hooks'; import type {CaseSelection,Stage} from '../types'; import {EMPTY_SELECTION,selectionActive} from '../types'; import StageSteps from '../components/StageSteps'; import RunProgress from '../components/RunProgress'; import ExportDialog from '../components/ExportDialog'; import CaseFilterPanel from '../components/CaseFilterPanel';
const stages:Stage[]=['analysis','plan','cases'];
export default function RunPage(){
  const {id}=useParams();
  const {run,events,metrics,refresh}=useRun(id);
  const [stage,setStage]=useState<Stage>('analysis');
  const [exportOpen,setExportOpen]=useState(false);
  const [selection,setSelection]=useState<CaseSelection>({...EMPTY_SELECTION});
  useEffect(()=>{if(run?.currentStage&&stages.includes(run.currentStage as Stage))setStage(run.currentStage as Stage)},[run]);
  if(!run)return <Card loading/>;

  const reject=async()=>{const reason=window.prompt('驳回原因');if(reason){try{await runApi.reject(run.taskId,stage,reason);refresh()}catch(error:any){message.error(error?.response?.data?.detail||'驳回失败')}}};
  const retry=async(module:string)=>{try{await runApi.retryModule(run.taskId,module);message.success(`已重试 ${module}`);refresh()}catch(error:any){message.error(error?.response?.data?.detail||'模块重试失败')}};
  // 导出：走后端 /api/v1/exports/{id}/{fmt}。文件名、保存位置与**用例筛选**
  // 都在对话框/选择器里决定（原先是 window.open 直接下载，一律全量且不可改）。
  const canExport=Boolean(run.modules&&Object.keys(run.modules).length>0);
  const product=run.product||''; const version=run.version||'';
  const modules=Object.entries(run.modules||{}).map(([module,status])=>({module,status}));
  const errors=run.errors||[];
  const isRunning=['pending','running'].includes(run.status);
  const hasCases=canExport||Object.values(run.modules||{}).some(s=>['succeeded','skipped'].includes(String(s)));

  return <div className="page"><Space direction="vertical" size="large" style={{width:'100%'}}>
    <Card title={`运行 ${run.runId||run.taskId}`} extra={<Space wrap>
        {run.mode&&<Tag color={run.mode==='stub'?'gold':'blue'}>模式：{run.mode==='stub'?'Stub 离线验收':'真实模型'}</Tag>}
        {run.llm&&<Tag>{run.llm.provider} / {run.llm.model}</Tag>}
        <Tag color={run.status==='succeeded'?'green':run.status==='failed'?'red':isRunning?'processing':'default'}>整体：{run.status}</Tag>
        <Button type="primary" onClick={()=>setExportOpen(true)} disabled={!canExport}
          title={canExport?'选择要导出的用例范围、格式与保存位置':'尚无用例产物'}>
          导出{selectionActive(selection)?'（已筛选）':''}…
        </Button>
        <Button danger onClick={()=>runApi.cancel(run.taskId).then(refresh)} disabled={['succeeded','cancelled'].includes(run.status)}>取消</Button>
      </Space>}>
      {run.status==='failed'&&<Alert type="error" showIcon style={{marginBottom:12}} message="质量门禁或运行失败"
        description={<Space direction="vertical" size={2}>
          <div>{errors.join('；')||run.error||'未提供详细错误'}</div>
          <div className="metrics-muted">可在下方对应阶段点「重跑」单独重试；已完成的其它阶段不受影响，无需整轮重来。</div>
        </Space>}/>}
      <StageSteps run={run} stage={stage} onStageChange={setStage} onRefresh={refresh}/>
    </Card>

    {/* 批量选择放在导出之前：先决定「要哪些」，再决定「怎么导出」。 */}
    {hasCases&&<CaseFilterPanel runId={run.taskId} value={selection} onChange={setSelection}/>}

    <RunProgress metrics={metrics}/>

    {modules.length>0&&<Card title="模块状态" size="small" extra={<Typography.Text type="secondary">用例设计阶段的逐模块产出；失败模块可单独重试</Typography.Text>}>
      <Table size="small" pagination={false} rowKey="module" dataSource={modules} columns={[
        {title:'模块',dataIndex:'module'},
        {title:'状态',dataIndex:'status',render:(status:string,row:any)=><Space>
          <Tag color={status==='succeeded'?'green':status==='failed'?'red':status==='skipped'?'default':'blue'}>{status}</Tag>
          {status==='failed'&&<Button size="small" onClick={()=>retry(row.module)}>重试</Button>}</Space>}]}/>
    </Card>}

    <Card title="质量门禁" size="small"><pre className="json-view">{JSON.stringify(run.qualityGate||{},null,2)}</pre></Card>
    {run.tokens&&<Card title="Token 消耗" size="small"><pre className="json-view">{JSON.stringify(run.tokens,null,2)}</pre></Card>}

    <Card title="实时事件" size="small">
      <Timeline items={events.slice().reverse().slice(0,60).map(e=>({children:<><Tag>{e.stage}</Tag>{e.message}<small> {e.createdAt}</small></>}))}/>
    </Card>
  </Space>
  <ExportDialog open={exportOpen} runId={run.taskId} product={product} version={version}
    selection={selection} onClose={()=>setExportOpen(false)}/>
  </div>;
}
