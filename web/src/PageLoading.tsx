import {useEffect,useState,useSyncExternalStore} from 'react'
import {dismissReadFailure,failRead,pageReads,readEpoch,subscribeReads} from './readActivity'
import {request} from './api'

export default function PageLoading(){
 const state=useSyncExternalStore(subscribeReads,pageReads),[now,setNow]=useState(Date.now())
 useEffect(()=>{
  if(!state.reads.length)return
  setNow(Date.now());const timer=setInterval(()=>setNow(Date.now()),250)
  return()=>clearInterval(timer)
 },[state.reads.length>0])
 const route=location.hash||'#discover'
 const reads=state.reads.filter(row=>row.route===route&&now-row.started>=500)
 const row=reads[0],failure=state.failure?.route===route?state.failure:null
 if(row){const elapsed=Math.max(0,Math.floor((now-row.started)/1000));return <div className="page-read-status" role="status" aria-live="polite"><span className="page-read-spinner" aria-hidden="true"/><div><strong>{row.label}</strong><span className="muted">{elapsed>=3?`${elapsed}s elapsed · `:''}You can keep browsing.</span></div></div>}
 if(failure)return <div className="page-read-status page-read-error" role="alert"><div><strong>Background refresh could not finish</strong><span>{failure.message}</span></div><button onClick={()=>{dismissReadFailure();const epoch=readEpoch(),url=new URL(failure.path,location.origin);url.searchParams.set('refresh','true');void request(url.pathname+url.search).then(()=>window.dispatchEvent(new CustomEvent('saved-view-updated',{detail:{path:failure.path}}))).catch(()=>{if(readEpoch()===epoch)failRead(failure.path,failure.route,'The refresh could not finish. Check the connection and retry.')})}}>Retry</button><button aria-label="Dismiss refresh message" onClick={dismissReadFailure}>×</button></div>
 return null
}
