import {useState} from 'react';
import {Alert,Button,Card,Collapse,Descriptions,Progress,Space,Tag,Tooltip,Typography,Popconfirm,message} from 'antd';
import {CheckCircleOutlined,CloseCircleOutlined,ClockCircleOutlined,LoadingOutlined,ReloadOutlined,PlayCircleOutlined} from '@ant-design/icons';
import type {Run,Stage,StagePhase} from '../types';
import {runApi} from '../api';
import StageEditor from './StageEditor';

/** 三个阶段的展示名与说明。
 *
 *  为什么用中文名而不是 analysis/plan/cases：这三个词是**内部阶段码**，
 *  对使用者没有意义；「需求分析 / 测试方案 / 用例设计」才是他要理解的三步。 */
const STAGE_INFO: {key:Stage;title:string;desc:string;artifact:string}[]=[
  {key:'analysis',title:'① 需求分析',desc:'从 PRD 抽取结构化需求，逐条对应原文位置',artifact:'analysis/requirements.json'},
  {key:'plan',title:'② 测试方案',desc:'确定范围、策略、优先级口径与用例量目标',artifact:'plan/测试方案.md'},
  {key:'cases',title:'③ 用例设计',desc:'按模块生成可执行用例，含步骤与预期',artifact:'cases/cases/*.json'},
];

const PHASE_LABEL:Record<StagePhase,{text:string;color:string}>={
  pending:{text:'待开始',color:'default'},
  running:{text:'运行中',color:'processing'},
  awaiting_review:{text:'待确认',color:'warning'},
  approved:{text:'已确认',color:'success'},
  succeeded:{text:'已完成',color:'success'},
  failed:{text:'失败',color:'error'},
  cancelled:{text:'已取消',color:'default'},
  rejected:{text:'已驳回',color:'error'},
};

/** 判定某阶段此刻的相位。
 *
 *  优先级：显式状态 > 运行中（当前阶段）> 未开始。
 *  `stageStates` 是后端的权威记录，但「正在跑」这件事只在 `currentStage` 上，
 *  因此两者都要看，否则运行中的阶段会显示成「待开始」。 */
function phaseOf(run:Run,stage:Stage):StagePhase{
  const state=(run.stageStates?.[stage]||'') as StagePhase;
  if(state)return state;
  if(run.currentStage===stage&&['pending','running'].includes(run.status))return 'running';
  return 'pending';
}

function iconFor(phase:StagePhase){
  if(phase==='running'||phase==='awaiting_review')return <LoadingOutlined/>;
  if(phase==='failed'||phase==='rejected')return <CloseCircleOutlined style={{color:'#ff4d4f'}}/>;
  if(phase==='approved'||phase==='succeeded')return <CheckCircleOutlined style={{color:'#52c41a'}}/>;
  return <ClockCircleOutlined style={{color:'#bfbfbf'}}/>;
}

const fmtMs=(ms?:number)=>{
  const v=Math.max(0,Math.round(ms||0));
  if(!v)return '-';
  if(v<1000)return `${v}ms`;
  const s=Math.round(v/1000);
  return s<60?`${s}s`:`${Math.floor(s/60)}m ${s%60}s`;
};
const fmtTokens=(n?:number)=>n?n.toLocaleString():'-';

/** 阶段三的**真实**进度：已完成模块 / 总模块。
 *
 *  只对阶段三可行 —— 阶段一/二没有可用的细分刻度，
 *  与其编造一个平滑百分比，不如显示**不确定态**进度条 + 已用时间。 */
function casesProgress(run:Run):{percent:number;done:number;total:number}|undefined{
  const modules=Object.entries(run.modules||{});
  if(!modules.length)return undefined;
  const done=modules.filter(([,s])=>['succeeded','skipped'].includes(String(s))).length;
  return {percent:Math.round(done*100/modules.length),done,total:modules.length};
}

export default function StageSteps({run,stage,onStageChange,onRefresh}:{
  run:Run; stage:Stage; onStageChange:(stage:Stage)=>void; onRefresh:()=>Promise<void>|void;
}){
  const [busy,setBusy]=useState<string>('');
  const [dirty,setDirty]=useState<Record<string,boolean>>({});
  /** 已展开产物面板的阶段。
   *
   *  **必须是受控状态**：Collapse 若不受控，「查看产物」按钮就无法从外部展开它 ——
   *  按钮只能去改一个无人消费的状态，点击后界面毫无反应（实测的真实缺陷）。
   *  用数组而非单值：允许多个阶段同时展开，便于对比（如分析 vs 方案）。 */
  const [expanded,setExpanded]=useState<Stage[]>([]);
  const isCurrent=(s:Stage)=>run.currentStage===s;

  /** 展开某阶段的产物面板，并把视图滚动到该卡片。 */
  const openArtifact=(s:Stage)=>{
    setExpanded(current=>current.includes(s)?current:[...current,s]);
    onStageChange(s);
    // jsdom 没有 scrollIntoView（用可选调用兜住），真实浏览器里才滚动
    window.requestAnimationFrame(()=>{
      document.getElementById(`stage-card-${s}`)?.scrollIntoView?.({block:'nearest'});
    });
  };

  /** 确认并进入下一步。
   *
   *  这道操作**取代了原先「先保存草稿、再批准」的两步心智模型** ——
   *  用户只需表达「这一步的结果我认可」，是否改过内容由系统自行处理：
   *  `approve` 接口本就支持带 body（等价于先存草稿再批准）。 */
  const confirm=(s:Stage)=>{
    setBusy(s);onStageChange(s);
    runApi.approve(run.taskId,s)
      .then(()=>{message.success(s==='cases'?'已确认，运行完成':'已确认，正在进入下一步');return onRefresh()})
      .catch((error:any)=>message.error(error?.response?.data?.detail||'确认失败'))
      .finally(()=>setBusy(''));
  };
  const regenerate=(s:Stage)=>{
    setBusy(s);
    runApi.regenerateStage(run.taskId,s)
      .then((result:any)=>{
        const discarded=(result?.discarded||[]).length;
        message.success(`已开始重跑${discarded?`（丢弃 ${discarded} 项旧产物）`:''}`);
        return onRefresh();
      })
      .catch((error:any)=>message.error(error?.response?.data?.detail||'重跑失败'))
      .finally(()=>setBusy(''));
  };

  const runningSomewhere=['pending','running'].includes(run.status);

  return <Space direction="vertical" size="middle" style={{width:'100%'}}>
    {STAGE_INFO.map((info,index)=>{
      const phase=phaseOf(run,info.key);
      const label=PHASE_LABEL[phase];
      const meta=run.stageMeta?.[info.key];
      const progress=info.key==='cases'&&phase==='running'?casesProgress(run):undefined;
      const hasOutput=['awaiting_review','approved','succeeded'].includes(phase);
      const canConfirm=phase==='awaiting_review'&&isCurrent(info.key);
      const canRegenerate=!runningSomewhere&&['approved','succeeded','failed','awaiting_review','rejected'].includes(phase);
      const isOpen=expanded.includes(info.key);
      return <Card key={info.key} id={`stage-card-${info.key}`} size="small"
        style={stage===info.key?{borderColor:'#1677ff'}:undefined}
        title={<Space>{iconFor(phase)}<Typography.Text strong>{info.title}</Typography.Text>
          <Tag color={label.color}>{label.text}</Tag>
          {dirty[info.key]&&<Tag color="gold">有未保存修改</Tag>}</Space>}
        extra={<Space size={4}>
          {meta?.durationMs?<Tooltip title="该阶段耗时"><span className="metrics-muted">{fmtMs(meta.durationMs)}</span></Tooltip>:null}
          {meta?.model?<Tooltip title="该阶段使用的模型"><span className="metrics-muted">{meta.model}</span></Tooltip>:null}
          {meta?.tokens?<Tooltip title="该阶段消耗 token"><span className="metrics-muted">{fmtTokens(meta.tokens)} tok</span></Tooltip>:null}
          {hasOutput&&<Button size="small" onClick={()=>openArtifact(info.key)}>{isOpen?'收起产物':'查看产物'}</Button>}
          {canRegenerate&&<Popconfirm title={`重跑「${info.title}」？`}
            description={index<2?'该阶段及其下游产物会被丢弃并重新生成；上游不受影响。':'用例产物会被丢弃并重新生成；上游分析与方案不受影响。'}
            okText="重跑" cancelText="取消" onConfirm={()=>regenerate(info.key)}>
            <Button size="small" icon={<ReloadOutlined/>} loading={busy===info.key}>重跑</Button></Popconfirm>}
          {canConfirm&&<Button size="small" type="primary" loading={busy===info.key}
            icon={info.key==='cases'?<CheckCircleOutlined/>:<PlayCircleOutlined/>}
            onClick={()=>confirm(info.key)}>
            {info.key==='cases'?'完成并结束':'确认并进入下一步'}</Button>}
        </Space>}>
        <Typography.Paragraph type="secondary" style={{marginBottom:8}}>{info.desc}</Typography.Paragraph>
        {phase==='running'&&(progress
          ?<Progress percent={progress.percent} size="small" status="active"
             format={()=>`${progress.done}/${progress.total} 模块`}/>
          :<Progress percent={100} size="small" status="active" showInfo={false}
             strokeColor="#1677ff" trailColor="#f0f0f0"/>)}
        {phase==='failed'&&<Alert type="error" showIcon
          message={`该阶段失败${meta?.gate?.errors?.length?`（门禁 ${meta.gate.errors.length} 项）`:''}`}
          description={<Space direction="vertical" size={2}>
            <span>{run.errors?.join('；')||run.error||'未提供详细错误'}</span>
            <span className="metrics-muted">可点「重跑」重试本阶段；上游产物不受影响。</span>
          </Space>}/>}
        {phase==='pending'&&runningSomewhere&&isCurrent(info.key)&&
          <Typography.Text type="secondary">正在准备…</Typography.Text>}
        {(hasOutput||phase==='failed')&&<Collapse ghost size="small" style={{marginTop:4}}
          activeKey={isOpen?['artifact']:[]}
          onChange={(keys)=>{
            const open=Array.isArray(keys)?keys.includes('artifact'):keys==='artifact';
            setExpanded(current=>open
              ?(current.includes(info.key)?current:[...current,info.key])
              :current.filter(x=>x!==info.key));
          }}
          items={[{
            key:'artifact',
            label:phase==='failed'?'查看失败输出':'查看 / 编辑产物',
            children:<StageEditor runId={run.taskId} stage={info.key}
              onDirtyChange={(value)=>setDirty(current=>({...current,[info.key]:value}))}/>,
          }]}/>}
      </Card>;
    })}
    <Descriptions size="small" column={1} style={{marginTop:-8}}>
      <Descriptions.Item label="流程说明">
        <Typography.Text type="secondary">
          三个步骤各自独立：每步产出后由你确认再进入下一步；任何一步都可以单独重跑，且不会影响上游。
          编辑产物是可选的 —— 「确认并进入下一步」会自动保存你的修改。
        </Typography.Text>
      </Descriptions.Item>
    </Descriptions>
  </Space>;
}
