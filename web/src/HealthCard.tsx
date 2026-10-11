import { Playlist } from './api'
import { useEffect, useState } from 'react'
export const stamp=(value?:string|null)=>value?new Date(value).toLocaleString():'Never'
export function RelativeTime({value,prefix=''}:{value?:string|null;prefix?:string}){
 const [now,setNow]=useState(Date.now())
 useEffect(()=>{const timer=setInterval(()=>setNow(Date.now()),60000);return()=>clearInterval(timer)},[])
 if(!value||!Number.isFinite(Date.parse(value)))return <span>Never synced</span>
 const minutes=Math.max(0,Math.floor((now-Date.parse(value))/60000))
 const text=minutes<1?'just now':minutes<60?`${minutes} minutes ago`:minutes<1440?`${Math.floor(minutes/60)} hours ago`:`${Math.floor(minutes/1440)} days ago`
 return <time dateTime={value} title={stamp(value)}>{prefix} {text}</time>
}
export function HealthIndicator({playlist:p}:{playlist:Playlist}){
 const error=p.health_attempt?.error
 const warning=!!p.health&&!p.health.healthy||p.unresolved>0||p.lost>0
 const kind=error?'error':warning?'warning':p.health?'healthy':'unknown'
 const detail=error?`Health check failed: ${error}`:warning?`Needs attention: ${p.unresolved} missing, ${p.lost} LOST${p.health?.missing_from_plex_playlist?`, ${p.health.missing_from_plex_playlist} missing from Plex`:''}${p.health?.extra_in_plex_playlist?`, ${p.health.extra_in_plex_playlist} extra in Plex`:''}${p.health?.source_added_since_last_sync||p.health?.source_removed_since_last_sync?', source playlist changed':''}`:p.health?'Healthy':'Health not checked yet'
 return <span className={`health-indicator ${kind}`} role="img" aria-label={detail} title={detail} tabIndex={0}>{error?'⊗':warning?'⚠':p.health?'✓':'○'}<span className="health-tooltip">{detail}</span></span>
}
export default function HealthCard({playlist}:{playlist:Playlist}){
  const h=playlist.health
  const reasons=h?[
    [h.unresolved,'missing source tracks'],[h.missing_from_plex_playlist,'tracks missing from Plex playlist'],
    [h.extra_in_plex_playlist,'extra tracks in Plex playlist'],[h.source_added_since_last_sync,'source additions since sync'],
    [h.source_removed_since_last_sync,'source removals since sync']
  ].filter(([count])=>Number(count)>0):[]
  return <div className="health-card">
    <p className="timestamps"><RelativeTime value={playlist.last_synced} prefix="Synced"/><br/>Health last updated: {stamp(h?.checked_at)}</p>
    {playlist.health_attempt?.error&&<p role="alert" className="health-error">{playlist.health_attempt.error}<br/>Last attempt: {stamp(playlist.health_attempt.attempted_at)}. Previous successful results are retained. <a href="#settings">Open Settings</a></p>}
    {h&&!h.healthy&&<p className="drift-summary"><strong>Needs Attention</strong></p>}
    <details><summary>Health details · {h?(h.healthy?'Healthy':'Needs Attention'):'Not checked'}</summary><p className="muted">Stored mappings are saved choices. These counts describe the last completed health/sync check, not pending match edits.</p>
      <div className="health-grid">{[['Source',h?.source_tracks],['Plex playlist',h?.plex_playlist_tracks],['Validated matches at last check',h?.matched_in_library],['Missing',h?.unresolved],['Ignored',h?.ignored],['Missing from Plex',h?.missing_from_plex_playlist],['Extra in Plex',h?.extra_in_plex_playlist]].map(([label,value])=><div className="health-metric" key={label}><span>{label}</span><strong>{value??'--'}</strong></div>)}</div>
      {h?.drift_details?Object.entries(h.drift_details).filter(([,rows])=>rows.length).map(([kind,rows])=><details className="drift-list" key={kind}><summary>{{missing_from_plex:'Missing from Plex playlist',extra_in_plex:'Extra in Plex playlist',unresolved:'Missing source tracks',source_added:'Added to source',source_removed:'Removed from source'}[kind]||kind} ({rows.reduce((n,t)=>n+(t.count||1),0)})</summary><ul>{rows.map((t,i)=><li key={i}>{t.title||t.plex_id} {t.artist&&`— ${t.artist}`} {t.count>1&&`× ${t.count}`}</li>)}</ul></details>):h&&!h.healthy&&<p>Run Health Check again to populate track-level differences in this view.</p>}
    </details>
  </div>
}
