import {markJobReviewed} from './jobReview'
import {currentJobLanes} from './jobActivity'
import ErrorNotice from './ErrorNotice'
import {LidarrRequestStatus} from './LidarrRequests'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api, Job, activeJob } from './api'
import { Icon } from './Controls'
import { displayText } from './labels'
import { stamp } from './HealthCard'

export {actionName} from './jobLabels'
import {actionName} from './jobLabels'
export function showActivity(id:string){location.hash=`activity/${encodeURIComponent(id)}`}
export function newestActive(jobs:Job[]){
 const priority=(job:Job)=>job.status==='running'||job.status==='cancelling'?2:job.status==='queued'?1:0
 return jobs.filter(j=>!['prepare_views','request_status'].includes(j.action)&&activeJob(j)).sort((a,b)=>priority(b)-priority(a)||(b.started_at||b.created_at||'').localeCompare(a.started_at||a.created_at||''))[0]
}
export function awaitingPlex(job:Job){
 const local=job.result?.local||{}
 const localIssue=Number(local.missing||0)>0||Number(local.unmapped||0)>0
 const legacyPending=job.result?.partial_success&&/Plex is refreshing/i.test(String(job.result?.summary||''))
 return job.action==='album_finalize'&&!job.error&&!localIssue&&!!(job.result?.pending||legacyPending)
}
export function jobNeedsAttention(job:Job){return ['failed','interrupted'].includes(job.status)||!!(job.result?.partial_success&&!awaitingPlex(job))}
export function jobResultLabel(job:Job){return awaitingPlex(job)?'Waiting for Plex':job.result?.partial_success?'Partially completed':job.status}
const duration=(seconds:number)=>seconds<60?`${seconds}s`:`${Math.floor(seconds/60)}m ${seconds%60}s`
const elapsed=(value?:string,end=Date.now())=>value?duration(Math.max(0,Math.floor((end-Date.parse(value))/1000))):'0s'
type EventRow={id?:number;created_at:string;stage?:string;message:string}

export function Terminal({events,copy,download,empty='Waiting for output…'}:{events:EventRow[];copy?:()=>Promise<string>;download?:string;empty?:string}){
 const viewport=useRef<HTMLDivElement>(null)
 const [follow,setFollow]=useState(true),[copied,setCopied]=useState(''),[raw,setRaw]=useState(false),[rawText,setRawText]=useState(''),[loading,setLoading]=useState(false),[blocked,setBlocked]=useState(false)
 const copyTimer=useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
 const available=!!navigator.clipboard&&window.isSecureContext&&!blocked
 useEffect(()=>()=>clearTimeout(copyTimer.current),[])
 // Follow is an explicit choice. DOM updates and browser scroll anchoring
 // must never silently turn it off as the log grows.
 useLayoutEffect(()=>{
  const el=viewport.current
  if(!follow||raw||!el)return
  const bottom=()=>{el.scrollTop=el.scrollHeight}
  bottom()
  const resize=new ResizeObserver(bottom)
  resize.observe(el)
  if(el.firstElementChild)resize.observe(el.firstElementChild)
  return()=>resize.disconnect()
 },[events,follow,raw])
 const text=()=>copy?copy():Promise.resolve(events.map(e=>`${stamp(e.created_at)}  ${e.message}`).join('\n'))
 async function copyLog(){try{await navigator.clipboard.writeText(await text());setCopied('Copied');clearTimeout(copyTimer.current);copyTimer.current=setTimeout(()=>setCopied(''),2000)}catch{setBlocked(true);setCopied('Clipboard unavailable. Use Raw to select and copy.')}}
 async function toggleRaw(){if(raw){setRaw(false);return}setRaw(true);setLoading(true);try{setRawText(await text())}catch(e:any){setRawText(e.message)}finally{setLoading(false)}}
 return <div className="terminal-shell"><div className="terminal-tools"><span>{raw?'Raw log snapshot':'Live output'}</span><div className="actions">{!raw&&<button className="small-button" aria-pressed={follow} title={follow?'Pause automatic scrolling':'Follow the latest output'} onClick={()=>setFollow(v=>!v)}>{follow?'Following':'Follow ↓'}</button>}<button className="small-button" title={available?'Copy full log':'Clipboard unavailable — use Raw'} aria-label="Copy full log" disabled={!available} onClick={copyLog}><Icon name="copy"/>{copied==='Copied'?'Copied ✓':'Copy'}</button><button className="small-button" aria-pressed={raw} onClick={toggleRaw}><Icon name="raw"/>Raw</button>{download&&<a className="small-button button-link" href={download} download><Icon name="download"/>Download</a>}</div></div>
 {!available&&<p className="copy-status">Clipboard unavailable. Use Raw, select the text, and press ⌘C or Ctrl+C.</p>}
 {raw?<pre className="terminal-output raw-output" tabIndex={0} aria-label="Selectable raw log">{loading?'Loading full log…':rawText}</pre>:<div className="terminal-output" ref={viewport} tabIndex={0} aria-label="Operation output"><div className="terminal-lines">{events.length?events.slice(-1000).map((e,i)=><div className={`terminal-line ${/failed|error/i.test(e.message)?'line-error':/LOST|unresolved|unmatched|⚠|✗/i.test(e.message)?'line-warning':/✓|complete/i.test(e.message)?'line-success':''}`} key={e.id||i}><time title={stamp(e.created_at)}>{new Date(e.created_at).toLocaleTimeString()}</time><span>{displayText(e.message)}</span></div>):<p className="muted">{empty}</p>}</div></div>}</div>
}

export function OperationOutput({stage,loading,error}:{stage:any;loading:boolean;error:string}){
 const [time,setTime]=useState(Date.now())
 useEffect(()=>{if(!loading)return;setTime(Date.now());const timer=setInterval(()=>setTime(Date.now()),1000);return()=>clearInterval(timer)},[loading])
 // Loading a page is transient status, not a second job terminal.
 // Errors and retry controls remain on the parent detail page.
 if(!loading||error)return null
 const last=stage.events.at(-1)
 return <p className="muted" role="status"><span className="spinner"/>{displayText(last?.message)||'Loading playlist…'}{last&&` · ${elapsed(last.created_at,time)} in this stage`}</p>
}

function ResultSummary({job}:{job:Job}){
 const payload=job.payload||{}
 const results=job.result?.playlists
 const batch=Array.isArray(results)
 const total=job.result?.total??job.activity?.playlist_total
 const successful=batch?results.filter((r:any)=>r.ok===true||(r.ok==null&&r.summary&&!r.summary.errors)).length:0
 const failed=batch?results.filter((r:any)=>r.ok===false).length:0
 const running=activeJob(job)
 const lanes=Object.values(job.activity?.lanes||{}) as any[]
 const started=lanes.filter(l=>l.key!=='job'&&l.playlist_index).length
 const notStarted=total!=null?Math.max(0,total-Math.max(started,results?.length||0)):null
 const records=batch?results.filter((r:any)=>r.ok!==false).map((r:any)=>r.result?.health||r.result?.summary||r.result||r.summary):[job.result?.health||job.result]
 const sum=(keys:string[])=>{let known=false;const n=records.reduce((n:number,r:any)=>{const key=keys.find(k=>typeof r?.[k]==='number');if(!key)return n;known=true;return n+r[key]},0);return known?n:null}
 const counts=[['Matched',sum(['matched_in_library','matched'])],['Missing',sum(['unresolved'])],['LOST',sum(['lost','newly_lost'])]]
 return <div className="result-summary">{(job.result?.partial_success||job.result?.pending||job.action==='ignore_batch')&&job.result?.summary&&<p className={job.result.partial_success&&!awaitingPlex(job)?'warning-notice':job.result?.pending||awaitingPlex(job)?'muted':'success-notice'} role="status">{job.result.summary}</p>}{['lidarr_add','lidarr_search'].includes(job.action)&&<LidarrRequestStatus albumKey={payload.album_id}/>}{batch&&<p>{successful} playlists completed · {failed} failed{notStarted!==null?` · ${notStarted} not started`:''}{!running&&results.length<Number(total)&&started>results.length?' · remaining started work was stopped':''}</p>}<div className="result-counts">{counts.filter(([,v])=>v!==null).map(([label,value])=><span key={label}>{value} <small>{label}</small></span>)}</div>{job.error&&<ErrorNotice error={displayText(job.error)}/>}{!running&&job.status==='cancelled'&&<p>Stopped at a safe checkpoint. Completed changes were kept; review the output for any unfinished playlist.</p>}{job.result?.key&&<a href={`#playlist/${encodeURIComponent(job.result.key)}`}>Open {job.result.name||'playlist'}</a>}</div>
}

export default function Activity({jobs,jobId=''}:{jobs:Job[];jobId?:string}){
 const [id,setId]=useState(jobId||newestActive(jobs)?.id||'')
 const [open,setOpen]=useState(true),[detail,setDetail]=useState<Job|null>(null),[events,setEvents]=useState<EventRow[]>([]),[error,setError]=useState(''),[clock,setClock]=useState(Date.now())
 const cursor=useRef(0),panel=useRef<HTMLElement>(null)
 const listed=jobs.find(j=>j.id===id)
 // Expanded state comes from the same response as its event stream.
 const job=detail?.id===id?detail:listed
 useEffect(()=>{setId(jobId||newestActive(jobs)?.id||'');setOpen(true)},[jobId])
 const latestActive=newestActive(jobs)
 useEffect(()=>{if(!jobId&&latestActive){setId(latestActive.id);setOpen(true)}},[jobId,latestActive?.id])
 useEffect(()=>{cursor.current=0;setEvents([]);setDetail(null);setError('')},[id])
 useEffect(()=>{
   if(!id||!open)return
   let alive=true,timer:ReturnType<typeof setTimeout>|undefined,terminalReads=0
   async function poll(){
     try{
       const response=await api.live(id,cursor.current)
       if(!alive)return
       setDetail(response.job);setError('')
       if(response.events.length){cursor.current=response.events.at(-1).id;setEvents(old=>[...old,...response.events].slice(-1000))}
       terminalReads=activeJob(response.job)?0:terminalReads+1
       if(activeJob(response.job)||response.events.length===1000||terminalReads<2)timer=setTimeout(poll,response.events.length===1000?50:1000)
       if(!activeJob(response.job))window.dispatchEvent(new Event('jobs-refresh'))
     }catch(e:any){if(alive){if(!/timed out/i.test(e.message))setError(e.message);timer=setTimeout(poll,10000)}}
   }
   void poll()
   return()=>{alive=false;if(timer)clearTimeout(timer)}
 },[id,open])
 useEffect(()=>{if(!open&&listed)setDetail(listed)},[listed,open])
 useEffect(()=>{if(!job||!activeJob(job))return;const timer=setInterval(()=>setClock(Date.now()),1000);return()=>clearInterval(timer)},[job?.id,job?.status])
 useEffect(()=>{if(open&&!error&&detail?.id===id&&!activeJob(detail)&&jobNeedsAttention(detail))markJobReviewed(detail)},[id,open,error,detail?.id,detail?.status,detail?.finished_at,detail?.result?.partial_success])
 if(!id)return <section className="panel idle-activity"><Icon name="activity"/><h2>No active jobs</h2><p className="muted">Recent jobs and their saved output are below.</p></section>
 if(!job)return <section className="panel"><p>{error||'Opening activity…'}</p><button onClick={()=>{location.hash='activity';setId('')}}>Dismiss</button></section>
 const active=activeJob(job)
 const payload=job.payload||{}
 const current=currentJobLanes(job)
 const target=current[0]?.name||payload.url||payload.title||({all:'All playlists',favorites:'Favorites',automatic:'Auto Sync playlists'}[payload.scope as string])||payload.playlist_keys?.join(', ')||'Playlist operation'
 const stage=active&&current[0]?.mode==='waiting'?`Waiting for ${current[0].service}`:displayText(job.progress)
 async function cancel(){try{await api.cancel(id);window.dispatchEvent(new Event('jobs-refresh'))}catch(e:any){setError(e.message)}}
 return <section className={`activity-panel ${open?'is-open':'is-collapsed'}`} ref={panel} aria-label="Current activity"><div className="activity-bar"><button className="activity-toggle" aria-expanded={open} onClick={()=>setOpen(v=>!v)}><span className={`activity-light ${job.status}`} aria-hidden="true"/><span><strong>{actionName(job.action)} · {active?target:jobResultLabel(job)}</strong><small>{stage} · {elapsed(job.started_at,active?clock:Date.parse(job.finished_at||job.started_at||''))}</small></span><span aria-hidden="true">{open?'⌃':'⌄'}</span></button>{!active&&<button className="small-button" onClick={()=>{location.hash='activity';setId('');setDetail(null)}}>Dismiss</button>}</div>
 {open&&<div className="activity-body"><div className="panel-head"><h2>{(job as any).owner_name&&`${(job as any).owner_name} · `}{actionName(job.action)} <small className="muted">{jobResultLabel(job)}</small></h2>{active&&<button disabled={job.status==='cancelling'} onClick={cancel}>{job.status==='cancelling'?'Stopping…':'Stop job'}</button>}</div>
<small className="muted">{job.worker&&`${job.worker} worker · `}{payload.trigger_label&&`${payload.trigger_label} · `}Started {stamp(job.started_at)}{job.finished_at&&` · Finished ${stamp(job.finished_at)}`}</small>
 {active&&current.map(l=><div className="activity-stage" key={l.key}><strong>{l.name}{l.playlist_index&&l.playlist_total?` · playlist ${l.playlist_index} of ${l.playlist_total}`:''}</strong><p role="status">{l.mode==='waiting'?`Waiting for ${l.service} · ${elapsed(l.since,clock)}`:displayText(l.stage)}</p>{l.mode==='waiting'&&<small className="muted">{displayText(l.stage)} · request pending</small>}{l.total>0&&l.completed!=null&&<progress max={l.total} value={l.completed}/>}</div>)}
 {job.status==='queued'&&<p>{job.progress}</p>}{job.status==='cancelling'&&<p>Stopping at the next safe checkpoint. If a Plex update has begun, that playlist update finishes first. Completed changes are kept.</p>}
 <ResultSummary job={job}/>{error&&<ErrorNotice error={error}/>}
 <Terminal events={events} download={`/api/jobs/${id}/log`} copy={()=>api.jobLog(id)} empty={active?'Waiting for the worker’s first output…':'No detailed events in this older job. Saved results are available below.'}/>

 {Array.isArray(job.result?.tracks)&&<details><summary>Track results</summary>{job.result.tracks.map((r:any,i:number)=><div key={i}><p>{r.title} — {r.artist} · {r.ok?'Ignored':'Failed'}</p>{r.error&&<ErrorNotice error={r.error}/>}</div>)}</details>}{job.result?.backup&&<p>Backup saved: <a href={`/api/backups/${encodeURIComponent(job.result.backup)}/download`}>Download backup</a></p>}{job.result?.restored&&<p>Restored {job.result.restored}. Safety backup: {job.result.safety_backup}. Plex was left untouched.</p>}{Array.isArray(job.result?.playlists)&&<details><summary>Playlist results</summary>{job.result.playlists.map((r:any)=><p key={r.key}><a href={`#playlist/${encodeURIComponent(r.key)}`}>{r.name||r.key}</a> · {r.ok===false?displayText(r.error):'Completed'}</p>)}</details>}
 </div>}</section>
}
