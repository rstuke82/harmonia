import ErrorNotice from './ErrorNotice'
import {useEffect,useState,useSyncExternalStore} from 'react'
import {api,Job} from './api'
import {showActivity} from './Activity'

let queueData:any={rows:[],playlist_count:0},queueLoad:Promise<any>|null=null
const listeners=new Set<()=>void>()
const notify=()=>listeners.forEach(listener=>listener())
export function refreshMatchQueue(){if(queueLoad)return queueLoad;queueLoad=api.matchQueue().then(data=>{queueData=data;notify();return data}).finally(()=>{queueLoad=null});return queueLoad}
export function useMatchQueue(){return useSyncExternalStore(listener=>{listeners.add(listener);return()=>listeners.delete(listener)},()=>queueData)}
export function queuedMatchFor(data:any,track:any,playlistKey?:string){const normalize=(value:any)=>String(value||'').replace(/[^a-z0-9]+/gi,' ').trim().toLowerCase();return data.rows.find((row:any)=>(!playlistKey||row.playlist_key===playlistKey)&&normalize(row.source?.title)===normalize(track.title)&&normalize(row.source?.artist)===normalize(track.artist))}

export default function MatchQueue({jobs}:{jobs:Job[]}){
 const data=useMatchQueue(),[error,setError]=useState(''),[busy,setBusy]=useState(false)
 const revision=jobs.filter(j=>j.action==='match_batch').map(j=>`${j.id}:${j.status}`).join('|')
 useEffect(()=>{let active=true;const load=()=>refreshMatchQueue().catch(e=>{if(active)setError(e.message)});void load();window.addEventListener('jobs-refresh',load);return()=>{active=false;window.removeEventListener('jobs-refresh',load)}},[revision])
 if(!data.rows.length&&!error)return null
 return <section className="panel match-queue"><div className="panel-head"><div><h2>Match queue · {data.rows.length} edits</h2><p className="muted">Pending reviewed edits. Sync Now applies edits for its selected playlists, or apply together to sync each of {data.playlist_count} affected playlists once.</p></div><button className="primary" disabled={busy||!data.rows.length} onClick={async()=>{setBusy(true);setError('');try{const job=await api.applyMatchQueue();await refreshMatchQueue();showActivity(job.id)}catch(e:any){setError(e.message)}finally{setBusy(false)}}}>Apply Matches & Sync</button></div><details><summary>Review queued edits</summary>{data.rows.map((r:any)=><div className="queued-match" key={r.id}><span><strong>{r.source.title}</strong> — {r.source.artist}<small>{r.playlist_name} · {r.provenance==='automatic'?'Suggested automatically':'Selected by you'} → {r.candidate?.title||`Plex track ${r.plex_id}`}</small></span><button disabled={busy} onClick={async()=>{setBusy(true);try{await api.discardMatch(r.id);await refreshMatchQueue()}catch(e:any){setError(e.message)}finally{setBusy(false)}}}>Discard</button></div>)}</details>{error&&<ErrorNotice error={error}/>}</section>
}
