import {currentJobLanes,activityBadgeCount} from './jobActivity'
import { useEffect, useState } from 'react'
import { Job, activeJob } from './api'
import { Icon } from './Controls'
import {useLidarrRequests} from './LidarrRequests'
import { actionName,jobNeedsAttention,newestActive } from './Activity'
export default function ActivityNav({jobs,page,mobile=false}:{jobs:Job[];page:string;mobile?:boolean}){
 const [seen,setSeen]=useState(()=>{try{return localStorage.getItem('bridge-activity-reviewed')||''}catch{return ''}})
 const active=newestActive(jobs)
 const queuedCount=activityBadgeCount(jobs)
 const latestFailure=jobs.filter(j=>jobNeedsAttention(j)&&j.finished_at).sort((a,b)=>(b.finished_at||'').localeCompare(a.finished_at||''))[0]
 const attention=!!latestFailure&&(latestFailure.finished_at||'')>seen
 useEffect(()=>{if(page==='activity'&&latestFailure?.finished_at){setSeen(latestFailure.finished_at);try{localStorage.setItem('bridge-activity-reviewed',latestFailure.finished_at)}catch{}}},[page,latestFailure?.finished_at])
 const lane=currentJobLanes(active).find(l=>l.playlist_index)
 const downloadCount=useLidarrRequests().filter(r=>r.downloads?.length&&r.download_status!=='Imported into Lidarr').length
 const jobLabel=active?(active.status==='queued'?'Job queued':lane?.mode==='waiting'?`Waiting for ${lane.service}`:lane?.playlist_index?`${active.action==='sync'?'Syncing':actionName(active.action)} ${lane.playlist_index} of ${lane.playlist_total}`:actionName(active.action)):attention?'Needs attention':'No active jobs'
 const label=[active||attention?jobLabel:'',downloadCount?`${downloadCount} download${downloadCount===1?'':'s'}`:''].filter(Boolean).join(' · ')||'No activity'
 return <button className={`activity-nav ${page==='activity'?'active':''} ${attention&&!active?'needs-attention':''}`} aria-current={page==='activity'?'page':undefined} onClick={()=>{location.hash='activity'}} title={label} aria-label={`Jobs: ${label}`}><span className="activity-nav-icon"><Icon name="activity"/>{(active||attention||downloadCount>0)&&<i className={active||downloadCount>0?'activity-dot':'attention-dot'}/>}</span><span>{mobile?'Activity':'Jobs'}{!mobile&&<small>{label}</small>}</span>{queuedCount>0&&<b className="nav-count queued-count" aria-label={`${queuedCount} queued job${queuedCount===1?'':'s'}`}>{queuedCount}</b>}</button>
}
