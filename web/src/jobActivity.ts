import type {Job} from './api'

// Completed result rows also retire lanes written by older workers.
export function currentJobLanes(job?:Job){
 const finished=new Set((job?.result?.playlists||[]).map((r:any)=>r.key))
 const lanes=(Object.values(job?.activity?.lanes||{}) as any[]).filter(l=>!['completed','failed'].includes(l.mode)&&!finished.has(l.key))
 let playlists=lanes.filter(l=>l.key!=='job')
 const sequential=['sync','match_batch','remove'].includes(job?.action||'')
 if(sequential){
  const completedIndex=Math.max(0,...(Object.values(job?.activity?.lanes||{}) as any[]).filter(l=>finished.has(l.key)||['completed','failed'].includes(l.mode)).map(l=>l.playlist_index||0))
  playlists=playlists.filter(l=>(l.playlist_index||0)>completedIndex)
 }
 // Health checks intentionally run several playlists at once. Sync operations
 // are sequential, so the furthest started playlist is the current one.
 if(playlists.length&&sequential)return [playlists.reduce((a,b)=>(b.playlist_index||0)>(a.playlist_index||0)?b:a)]
 return playlists.length?playlists:lanes.filter(l=>l.key==='job')
}

export function activityBadgeCount(jobs:Job[]){
 const running=jobs.filter(j=>!['prepare_views','request_status'].includes(j.action)&&['running','cancelling'].includes(j.status))
 return running.length>1||running.some(j=>['sync','match_batch'].includes(j.action))?running.length:0
}
