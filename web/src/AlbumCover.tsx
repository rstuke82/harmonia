import {useEffect,useState,ReactNode} from 'react'
import {downloadGroup} from './requestPresentation'
export function coverStatus(row:any){
 const raw=row.download_status||''
 const state=`${row.status||''} ${raw}`.toLowerCase(),group=downloadGroup(row)
 if(group==='Needs Action')return {label:'Needs attention',icon:'!',tone:'error'}
 if(group==='Cancelled / Failed')return {label:'Cancelled',icon:'!',tone:'error'}
 if(group==='Partially Available')return {label:'Partially available',icon:'◐',tone:'partial'}
 if(row.plex_fully_available||row.plex_available||/^(Available|In Library|In Plex|Completed)$/.test(row.availability||''))return {label:'Available',icon:'✓',tone:'available'}
 if(row.status==='approval')return {label:'Awaiting approval',icon:'◷',tone:'pending'}
 if(/imported|awaiting plex|waiting for plex/.test(state))return {label:'Waiting for Plex',icon:'◷',tone:'imported'}
 if(/importing|waiting for import/.test(state))return {label:'Importing',icon:'◷',tone:'progress'}
 if(/waiting for download/.test(state))return {label:'Waiting for download',icon:'◷',tone:'pending'}
 if(/downloading|download grabbed|in progress/.test(state))return {label:'Downloading',icon:'↓',tone:'progress'}
 if(row.status==='search_pending'||/searching/.test(state))return {label:'Searching',icon:'↓',tone:'progress'}
 if(['search_completed','added'].includes(row.status))return {label:'Waiting for download',icon:'◷',tone:'pending'}
 if(['queued','adding'].includes(row.status))return {label:'Queued',icon:'◷',tone:'pending'}
 const label=row.availability||raw||'Queued'
 if(/not available|missing/i.test(label))return {label,icon:'○',tone:'missing'}
 return {label,icon:'◷',tone:'pending'}
}
export function CoverStatus({row}:{row:any}){const status=coverStatus(row);return <span className={'cover-status '+status.tone} role="img" aria-label={status.label} title={status.label}>{status.icon}</span>}
export default function AlbumCover({src,children}:{src?:string;children?:ReactNode}){
 const [failed,setFailed]=useState(false)
 useEffect(()=>setFailed(false),[src])
 return <div className="album-cover">{src&&!failed?<img src={src} alt="" loading="lazy" onError={()=>setFailed(true)}/>:<div className="album-art-placeholder" aria-hidden="true">♫</div>}{children}</div>
}
