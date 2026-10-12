import {useCallback,useEffect,useState} from 'react';
import {runApi} from './api';
import type {Run,RunEvent,RunMetrics} from './types';

/** Load a recoverable HTTP snapshot (which seeds metrics), then receive incremental events and metrics over WebSocket. */
export function useRun(id?:string){
  const [run,setRun]=useState<Run>();
  const [events,setEvents]=useState<RunEvent[]>([]);
  const [metrics,setMetrics]=useState<RunMetrics>();
  const refresh=useCallback(async()=>{
    if(!id)return;
    const [nextRun,nextEvents]=await Promise.all([runApi.get(id),runApi.events(id)]);
    setRun(nextRun); setEvents(nextEvents); if(nextRun.metrics)setMetrics(nextRun.metrics);
  },[id]);
  useEffect(()=>{
    if(!id)return;
    let live=true; let socket:WebSocket|undefined; let reconnect:number|undefined;
    let polling:number|undefined; let attempts=0; let terminal=false;
    const load=async()=>{try{const snapshot=await runApi.get(id); if(['succeeded','failed','cancelled'].includes(snapshot.status)) terminal=true; setRun(snapshot); if(snapshot.metrics)setMetrics(snapshot.metrics); const current=await runApi.events(id); setEvents(current)}catch{/* transient outage; retry below */}};
    const connect=()=>{
      if(!live||terminal)return;
      const protocol=window.location.protocol==='https:'?'wss:':'ws:';
      // `/events/ws` avoids the GET `/events` route shadowing WebSocket upgrades in Starlette. `metrics=1` opts into run_metrics pushes; an older backend simply never sends them.
      socket=new WebSocket(`${protocol}//${window.location.host}/api/v1/runs/${encodeURIComponent(id)}/events/ws?metrics=1`);
      socket.onopen=()=>{attempts=0};
      socket.onmessage=(message)=>{try{
        const item=JSON.parse(message.data);
        // `run_metrics` is a metrics snapshot, not a run event: it must never enter `events` and carries no eventId.
        if(item?.type==='run_metrics'){setMetrics(item as RunMetrics);return}
        setEvents(previous=>previous.some(event=>event.eventId===item.eventId)?previous:[...previous,item]);
        setRun(previous=>{const next=previous?({...previous,lastEventId:item.eventId,status:item.status||previous.status,currentStage:item.stage||previous.currentStage,percent:item.percent??previous.percent}):previous; if(next&&['succeeded','failed','cancelled'].includes(next.status)){terminal=true; if(reconnect)window.clearTimeout(reconnect)} return next});
      }catch{/* ignore malformed events */}};
      socket.onclose=()=>{if(live&&!terminal){const delay=Math.min(1000*2**attempts,10000);attempts++;reconnect=window.setTimeout(connect,delay)}};
      socket.onerror=()=>socket?.close();
    };
    load(); connect(); polling=window.setInterval(load,10000);
    return()=>{live=false;socket?.close();if(reconnect)window.clearTimeout(reconnect);if(polling)window.clearInterval(polling)};
  },[id,refresh]);
  return {run,events,metrics,refresh};
}
