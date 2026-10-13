import {useCallback,useEffect,useMemo,useRef,useState} from 'react';
import {Alert,Button,Card,Empty,Input,Space,Spin,Tag,Tooltip,Typography} from 'antd';
import {ClearOutlined,FilterOutlined,ThunderboltOutlined} from '@ant-design/icons';
import {fetchFacets} from '../api';
import type {CaseFacets,CaseSelection} from '../types';
import {EMPTY_SELECTION,selectionActive} from '../types';

/** 用例批量选择器：按**模块 / 优先级 / 标签**三维度筛选，支持复合条件。
 *
 *  语义（与服务端 `casefilter` 严格一致）：**维度内取并集、跨维度取交集**。
 *    只选 KB,AGENT               → KB 或 AGENT
 *    KB + P0                     → KB 且 P0
 *    KB + @regression            → KB 且含 @regression（「单模块中的回归用例」）
 *
 *  为什么每个维度都显示数量：数量来自服务端的**分面统计**，且统计该维度时
 *  会忽略它自身的条件（`ignore`），因此用户能在选择过程中比较
 *  「换成 P1 会剩多少条」，而不是盲选 —— 这是复合筛选可用性的关键。 */

const DIMENSIONS:{key:'modules'|'priorities'|'tags';label:string;hint:string;color:string}[]=[
  {key:'modules',label:'模块',hint:'可多选：同维度取并集',color:'blue'},
  {key:'priorities',label:'优先级',hint:'可多选：P0 为业务关键项',color:'purple'},
  {key:'tags',label:'标签',hint:'@smoke 为冒烟集，@regression 为全量回归',color:'green'},
];

export default function CaseFilterPanel({runId,value,onChange,disabled}:{
  runId:string; value:CaseSelection; onChange:(next:CaseSelection)=>void; disabled?:boolean;
}){
  const [facets,setFacets]=useState<CaseFacets>();
  const [loading,setLoading]=useState(false);
  const [error,setError]=useState('');
  const [keyword,setKeyword]=useState(value.keyword);
  // 请求序号：并发/竞态下只接受最后一次响应，避免旧结果覆盖新结果
  const seqRef=useRef(0);

  const load=useCallback(async(selection:CaseSelection)=>{
    const seq=++seqRef.current;
    setLoading(true);
    try{
      // `ignore="*"`：让服务端**每个维度各自忽略自身约束**再做计数。
      // 这样一次请求就够 —— 若逐维度请求再合并，三个响应会互相覆盖
      // （实测那样写会让计数随机取决于哪次响应最后到达）。
      const next=await fetchFacets(runId,selection,'*');
      if(seq===seqRef.current){setFacets(next);setError('')}
    }catch(err:any){
      if(seq===seqRef.current)setError(err?.response?.data?.detail||'无法获取可选项');
    }finally{
      if(seq===seqRef.current)setLoading(false);
    }
  },[runId]);

  useEffect(()=>{
    // **即使 disabled 也要加载**：否则面板会空成一片，用户看不出
    // 「是没得选」还是「被禁用了」。禁用只应阻止**交互**，不应隐藏信息。
    void load(value);
  },[load,value]);

  const toggle=(dim:'modules'|'priorities'|'tags',item:string)=>{
    const current=value[dim];
    const next=current.includes(item)?current.filter(x=>x!==item):[...current,item];
    onChange({...value,[dim]:next});
  };

  const summary=useMemo(()=>{
    const matched=facets?.matched??0;
    const total=facets?.total??0;
    return {matched,total,excluded:Math.max(0,total-matched)};
  },[facets]);

  const active=selectionActive(value);

  return <Card size="small" title={<Space><FilterOutlined/>用例批量选择</Space>}
    extra={<Space size={4}>
      <Typography.Text type="secondary">
        {active?`已选 ${summary.matched}/${summary.total} 条（排除 ${summary.excluded}）`:`共 ${summary.total} 条用例`}
      </Typography.Text>
      <Tooltip title="清空全部条件">
        <Button size="small" icon={<ClearOutlined/>} disabled={!active}
          aria-label="清空全部条件"
          onClick={()=>{setKeyword('');onChange({...EMPTY_SELECTION})}}/>
      </Tooltip>
    </Space>}
    style={{marginBottom:8}}>
    {error&&<Alert type="warning" showIcon message={error} style={{marginBottom:8}}/>}
    <Spin spinning={loading} size="small">
      {DIMENSIONS.map(dim=>{
        const items=(facets?.[dim.key]||[]);
        return <div key={dim.key} style={{marginBottom:10}}>
          <Space size={6} wrap>
            <Tooltip title={dim.hint}>
              <Typography.Text strong style={{width:52,display:'inline-block'}}>{dim.label}</Typography.Text>
            </Tooltip>
            {items.length===0&&<Typography.Text type="secondary">（当前条件下无可用值）</Typography.Text>}
            {items.map(item=>{
              const on=value[dim.key].includes(item.value);
              return <Tag.CheckableTag key={item.value} checked={on}
                onChange={()=>!disabled&&toggle(dim.key,item.value)}>
                <span style={disabled?{opacity:.5}:undefined}>
                  {item.value} <Typography.Text type="secondary" style={{fontSize:11}}>{item.count}</Typography.Text>
                </span>
              </Tag.CheckableTag>;
            })}
          </Space>
        </div>;
      })}
      <Space size={6} wrap>
        <Typography.Text strong style={{width:52,display:'inline-block'}}>关键词</Typography.Text>
        <Input size="small" style={{width:260}} allowClear value={keyword}
          disabled={disabled} placeholder="匹配用例 ID / 标题 / 需求编号，如 REQ-KB-001"
          onChange={e=>setKeyword(e.target.value)}
          onBlur={()=>onChange({...value,keyword})}
          onPressEnter={()=>onChange({...value,keyword})}/>
      </Space>
      {active&&<div style={{marginTop:10}}>
        <Alert type="info" showIcon icon={<ThunderboltOutlined/>}
          message={`当前筛选：${describe(value)}`}
          description={<Typography.Text type="secondary">
            维度内取并集、跨维度取交集。例如「模块=KB + 标签=@regression」表示「KB 模块中的回归用例」。
            导出时只包含命中的 {summary.matched} 条。
          </Typography.Text>}/>
      </div>}
      {!active&&itemsEmpty(facets)&&<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚无可筛选的用例"/>}
    </Spin>
  </Card>;
}

function itemsEmpty(facets?:CaseFacets){
  return !facets||(facets.modules.length===0&&facets.priorities.length===0&&facets.tags.length===0);
}

/** 人类可读描述（与 `api.selectionLabel` 同构，内联以免形成额外依赖）。 */
export function describe(selection:CaseSelection):string{
  const parts:string[]=[];
  if(selection.modules.length)parts.push(`模块=${selection.modules.join(',')}`);
  if(selection.priorities.length)parts.push(`优先级=${selection.priorities.join(',')}`);
  if(selection.tags.length)parts.push(`标签=${selection.tags.join(',')}`);
  const keyword=selection.keyword.trim();
  if(keyword)parts.push(`关键词=${keyword}`);
  return parts.length?parts.join('；'):'全部用例';
}
