import {useSyncExternalStore} from 'react'
type Track={title?:string;artist?:string;album?:string}
type Notice={message:string;job_id?:string;request_id?:string}
const notices=new Map<string,Notice>(),listeners=new Set<()=>void>()
const key=(t:Track)=>JSON.stringify([t.artist||'',t.album||t.title||''].map(v=>v.trim().toLowerCase()))
export function recordAction(track:Track,notice:Notice){notices.set(key(track),notice);listeners.forEach(f=>f())}
export default function ActionFeedback({track}:{track:Track}){const notice=useSyncExternalStore(f=>{listeners.add(f);return()=>{listeners.delete(f)}},()=>notices.get(key(track)));return notice?<small className="inline-job-feedback" role="status">{notice.message} {notice.request_id?<a href={`#requests/${encodeURIComponent(notice.request_id)}`}>View request →</a>:notice.job_id&&<a href={`#activity/${notice.job_id}`}>View job →</a>}</small>:null}
