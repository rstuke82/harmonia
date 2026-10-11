import {useEffect,useState} from 'react'
import {createPortal} from 'react-dom'
type Notice={job_id?:string;count?:number|null;status?:string;message?:string;serial:number}
export default function SyncNotice(){
 const [notice,setNotice]=useState<Notice|null>(null),[paused,setPaused]=useState(false)
 useEffect(()=>{const show=(event:Event)=>{const detail=(event as CustomEvent).detail,value=typeof detail==='string'?{message:detail}:detail;if(!value?.job_id&&!value?.message)return;setNotice({...value,serial:Date.now()});setPaused(false)};window.addEventListener('bridge-sync-started',show);window.addEventListener('bridge-notice',show);return()=>{window.removeEventListener('bridge-sync-started',show);window.removeEventListener('bridge-notice',show)}},[])
 useEffect(()=>{if(!notice||paused)return;const timer=setTimeout(()=>setNotice(null),10000);return()=>clearTimeout(timer)},[notice,paused])
 if(!notice)return null
 const title=notice.message||(notice.status==='running'?'Playlist sync started':'Playlist sync queued')
 return createPortal(<div role="region" className="sync-notice" aria-label="Action confirmation" onMouseEnter={()=>setPaused(true)} onMouseLeave={()=>setPaused(false)} onFocus={()=>setPaused(true)} onBlur={e=>{if(!e.currentTarget.contains(e.relatedTarget))setPaused(false)}}><span className="sync-notice-check" aria-hidden="true">✓</span><div><p role="status"><strong>{title}</strong>{notice.count!=null&&<small>{notice.count} playlist{notice.count===1?'':'s'} · You can keep browsing.</small>}</p><a href={notice.job_id?`#activity/${encodeURIComponent(notice.job_id)}`:'#activity'} onClick={()=>setNotice(null)}>{notice.job_id?'View job →':'View Activity →'}</a></div><button type="button" className="icon-button" aria-label="Dismiss confirmation" onClick={()=>setNotice(null)}>×</button></div>,document.body)
}
