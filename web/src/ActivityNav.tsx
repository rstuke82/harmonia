import {currentJobLanes,activityBadgeCount} from './jobActivity'
import {useEffect,useState} from 'react'
import {Job,activeJob} from './api'
import {Icon} from './Controls'
import {useLidarrRequests} from './LidarrRequests'
import {actionName,jobNeedsAttention,newestActive,showActivity} from './Activity'
import {reviewedJobs,jobReviewedEvent,isCurrentReview} from './jobReview'

export default function ActivityNav({jobs,page,mobile=false}:{jobs:Job[];page:string;mobile?:boolean}){
 const [reviewed,setReviewed]=useState(reviewedJobs)
 useEffect(()=>{
  const review=(event:Event)=>{
   const update=event as CustomEvent
   if(isCurrentReview(update)&&typeof update.detail.id==='string'&&typeof update.detail.finished_at==='string'){
    setReviewed(previous=>({...previous,[update.detail.id]:update.detail.finished_at}))
   }
  }
  window.addEventListener(jobReviewedEvent,review)
  return()=>window.removeEventListener(jobReviewedEvent,review)
 },[])
 const active=newestActive(jobs)
 const queuedCount=activityBadgeCount(jobs)
 const previousFailure=jobs.filter(j=>!['prepare_views','request_status'].includes(j.action)&&!activeJob(j)&&jobNeedsAttention(j)&&j.finished_at&&Number.isFinite(Date.parse(j.finished_at))&&reviewed[j.id]!==j.finished_at).sort((a,b)=>Date.parse(b.finished_at!)-Date.parse(a.finished_at!))[0]
 const lane=currentJobLanes(active).find(l=>l.playlist_index)
 const downloadCount=useLidarrRequests().filter(r=>r.downloads?.length&&r.download_status!=='Imported into Lidarr').length
 const previousLabel=previousFailure?.status==='failed'?'Previous job failed':previousFailure?.status==='interrupted'?'Previous job interrupted':'Previous job partially completed'
 const jobLabel=active?(active.status==='queued'?'Job queued':lane?.mode==='waiting'?`Waiting for ${lane.service}`:lane?.playlist_index?`${active.action==='sync'?'Syncing':actionName(active.action)} ${lane.playlist_index} of ${lane.playlist_total}`:actionName(active.action)):previousFailure?previousLabel:''
 const label=[jobLabel,downloadCount?`${downloadCount} download${downloadCount===1?'':'s'}`:''].filter(Boolean).join(' · ')||'No activity'
 const historical=!!previousFailure&&!active
 const target=active||previousFailure
 const title=historical?`${label} · ${actionName(previousFailure.action)} · ${previousFailure.finished_at}`:label
 return <button className={`activity-nav ${page==='activity'?'active':''} ${historical?'has-history-warning':''}`} aria-current={page==='activity'?'page':undefined} onClick={()=>{if(target)showActivity(target.id);else location.hash='activity'}} title={title} aria-label={`Jobs: ${label}`}><span className="activity-nav-icon"><Icon name="activity"/>{(active||previousFailure||downloadCount>0)&&<i className={active||downloadCount>0?'activity-dot':'attention-dot'}/>}</span><span>{mobile?'Activity':'Jobs'}{!mobile&&<small>{label}</small>}</span>{queuedCount>0&&<b className="nav-count queued-count" aria-label={`${queuedCount} queued job${queuedCount===1?'':'s'}`}>{queuedCount}</b>}</button>
}
