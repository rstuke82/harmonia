import {useEffect,useState,useSyncExternalStore} from 'react'
import {dismissReadFailure,failRead,pageReads,readEpoch,subscribeReads} from './readActivity'
import {api,Job,request} from './api'
import {usePolling} from './usePolling'

// Mounted only on Activity. Background work never inserts a banner into pages.
export default function BackgroundActivity(){
 const state=useSyncExternalStore(subscribeReads,pageReads),[now,setNow]=useState(Date.now())
 const [jobs,setJobs]=useState<Job[]>([]),[error,setError]=useState('')
 usePolling(async()=>{try{setJobs(await api.backgroundJobs());setError('')}catch{setError('Background activity is temporarily unavailable. Retrying automatically.')}},()=>5000,'page-activity-refresh')
 const running=jobs.filter(job=>['queued','running','cancelling'].includes(job.status))
 useEffect(()=>{
  if(!state.reads.length&&!running.length)return
  const timer=setInterval(()=>setNow(Date.now()),1000)
  return()=>clearInterval(timer)
 },[state.reads.length>0||running.length>0])
 const failure=state.failure
 const retry=()=>{
  if(!failure)return
  dismissReadFailure();const epoch=readEpoch(),url=new URL(failure.path,location.origin)
  url.searchParams.set('refresh','true')
  void request(url.pathname+url.search).then(()=>window.dispatchEvent(new CustomEvent('saved-view-updated',{detail:{path:failure.path}}))).catch(()=>{
   if(readEpoch()===epoch)failRead(failure.path,failure.route,'The refresh could not finish. Check the connection and retry.')
  })
 }
 return <details className="panel background-updates"><summary><strong>Background updates</strong><span className="muted">{failure?'An update needs attention':running.length+state.reads.length?`${running.length+state.reads.length} in progress`:'No updates in progress'}</span></summary><p className="muted">Page reads and saved-page preparation appear here. Your existing page content stays available.</p>{error&&<p role="status">{error}</p>}{state.reads.map(row=><div className="background-update-row" role="status" key={row.id}><strong>{row.label}</strong><span className="muted">{Math.max(0,Math.floor((now-row.started)/1000))}s elapsed</span></div>)}{failure&&<div className="background-update-row" role="alert"><strong>Page update could not finish</strong><span>{failure.message}</span><div className="actions"><button onClick={retry}>Retry</button><button aria-label="Dismiss refresh message" onClick={dismissReadFailure}>Dismiss</button></div></div>}{jobs.slice(0,20).map(job=><div className="background-update-row" key={job.id}><strong>{job.status==='queued'?'Waiting to prepare page':job.status==='running'?'Preparing page data':`Page preparation ${job.status}`}</strong><span>{job.progress}</span>{job.owner_name&&<small className="muted">{job.owner_name}</small>}<a href={`#activity/${encodeURIComponent(job.id)}`}>View details</a></div>)}{!jobs.length&&!state.reads.length&&!failure&&<p className="muted">No recent background page updates.</p>}</details>
}
