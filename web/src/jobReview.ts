import type {Job} from './api'

export const jobReviewedEvent = 'harmonia-job-reviewed'
function reviewKey(){
  try{return `harmonia-job-reviews:${localStorage.getItem('bridge-account-id')||'session'}`}catch{return 'harmonia-job-reviews:session'}
}
export function reviewedJobs():Record<string,string>{
  try{
    const value=JSON.parse(localStorage.getItem(reviewKey())||'{}')
    if(!value||typeof value!=='object'||Array.isArray(value))return {}
    return Object.fromEntries(Object.entries(value).filter(([,stamp])=>typeof stamp==='string'&&Number.isFinite(Date.parse(stamp)))) as Record<string,string>
  }catch{return {}}
}
export function markJobsReviewed(jobs:Job[]){
  const valid=jobs.filter(job=>!['queued','running','cancelling'].includes(job.status)&&job.finished_at&&Number.isFinite(Date.parse(job.finished_at)))
  if(!valid.length)return
  const previous=reviewedJobs(),updates=Object.fromEntries(valid.map(job=>[job.id,job.finished_at!]))
  const key=reviewKey()
  const retained=Object.fromEntries(Object.entries(previous).sort((a,b)=>Date.parse(b[1])-Date.parse(a[1])).slice(0,2000))
  const reviews={...retained,...updates}
  try{localStorage.setItem(key,JSON.stringify(reviews))}catch{}
  const last=valid[valid.length-1]
  window.dispatchEvent(new CustomEvent(jobReviewedEvent,{detail:{id:last.id,finished_at:last.finished_at,key,reviews:updates}}))
}
export function markJobReviewed(job:Job){
  if(reviewedJobs()[job.id]===job.finished_at)return
  markJobsReviewed([job])
}
export function isCurrentReview(event:CustomEvent){return event.detail?.key===reviewKey()}
