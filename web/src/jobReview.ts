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
export function markJobReviewed(job:Job){
  if(!job.finished_at||!Number.isFinite(Date.parse(job.finished_at)))return
  const previous=reviewedJobs()
  if(previous[job.id]===job.finished_at)return
  const key=reviewKey()
  const reviews=Object.fromEntries(Object.entries({...previous,[job.id]:job.finished_at}).sort((a,b)=>Date.parse(b[1])-Date.parse(a[1])).slice(0,200))
  try{localStorage.setItem(key,JSON.stringify(reviews))}catch{}
  window.dispatchEvent(new CustomEvent(jobReviewedEvent,{detail:{id:job.id,finished_at:job.finished_at,key}}))
}
export function isCurrentReview(event:CustomEvent){return event.detail?.key===reviewKey()}
