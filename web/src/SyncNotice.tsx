import {useEffect,useState} from 'react'
import {createPortal} from 'react-dom'
type Notice={job_id:string;count:number|null;status:string;serial:number}
export default function SyncNotice(){
 const [notice,setNotice]=useState<Notice|null>(null),[paused,setPaused]=useState(false)
 useEffect(()=>{const show=(event:Event)=>{const value=(event as CustomEvent).detail;if(!value?.job_id)return;setNotice({...value,serial:Date.now()});setPaused(false)};window.addEventListener('bridge-sync-started',show);return()=>window.removeEventListener('bridge-sync-started',show)},[])
 useEffect(()=>{if(!notice||paused)return;const timer=setTimeout(()=>setNotice(null),10000);return()=>clearTimeout(timer)},[notice,paused])
 if(!notice)return null
 const title=notice.status==='running'?'Playlist sync started':'Playlist sync queued'
 return createPortal(<div role="region" className="sync-notice" aria-label="Sync confirmation" onMouseEnter={()=>setPaused(true)} onMouseLeave={()=>setPaused(false)} onFocus={()=>setPaused(true)} onBlur={e=>{if(!e.currentTarget.contains(e.relatedTarget))setPaused(false)}}><span className="sync-notice-check" aria-hidden="true">✓</span><div><p role="status"><strong>{title}</strong>{notice.count!=null&&<small>{notice.count} playlist{notice.count===1?'':'s'} · You can keep browsing.</small>}</p><a href={`#activity/${encodeURIComponent(notice.job_id)}`} onClick={()=>setNotice(null)}>View job →</a></div><button type="button" className="icon-button" aria-label="Dismiss sync confirmation" onClick={()=>setNotice(null)}>×</button></div>,document.body)
}
