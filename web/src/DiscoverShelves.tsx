import AlbumCover,{CoverStatus,coverStatus} from './AlbumCover'
import {useEffect,useState} from 'react'
import {request,savedPath} from './api'
import {refreshLidarrRequests,useLidarrRequests,useRequestLoadState} from './LidarrRequests'
import {useAccount} from './Auth'
import ErrorNotice from './ErrorNotice'
import FittedTitle from './FittedTitle'
export function RecentRequestCard({row}:{row:any}){
 const status=coverStatus(row),people=row.requesters||[]
 return <a className="recent-request-card" href={'#requests/'+encodeURIComponent(row.key)}>
  {row.artwork&&<span className="recent-request-backdrop" aria-hidden="true" style={{backgroundImage:`url(${row.artwork})`}}/>}
  <span className="recent-request-copy"><FittedTitle title={row.title}/><span className="recent-request-artist">{row.artist||'Unknown artist'}</span>
   <span className="recent-request-users">{people.length?people.map((person:any,index:number)=><span className="recent-request-user" key={person.user_id||index}>{person.avatar?<img src={person.avatar} alt="" loading="lazy" referrerPolicy="no-referrer" onError={e=>{e.currentTarget.style.display='none'}}/>:<span className="recent-request-avatar" aria-hidden="true">{(person.name||'?').slice(0,1).toUpperCase()}</span>}<span>{person.name||'Requester unavailable'}</span></span>):<span>Requester unavailable</span>}</span>
   <span className="recent-request-state"><span>Status</span><span className={'recent-request-status '+status.tone}>{status.label}</span></span>
  </span><span className="recent-request-art"><AlbumCover src={row.artwork}/></span>
 </a>
}
export default function DiscoverShelves(){
 const [data,setData]=useState<any>(null),[error,setError]=useState(''),[addedCount,setAddedCount]=useState(6),[requestCount,setRequestCount]=useState(6)
 const requests=useLidarrRequests(),requestState=useRequestLoadState(),account=useAccount()
 useEffect(()=>{if(account?.lidarr_enabled!==false)void refreshLidarrRequests()},[account?.id,account?.lidarr_enabled])
 useEffect(()=>{let alive=true,inFlight=false;async function load(){if(inFlight)return;inFlight=true;try{const d=await request('/api/library/recently-added');if(alive){setData(d);setError('')}}catch(e:any){if(alive)setError(e.message)}finally{inFlight=false}}void load();const update=(e:Event)=>{if(savedPath((e as CustomEvent).detail.path)==='/api/library/recently-added')void load()};const refresh=()=>{if(!document.hidden)void load()};const timer=setInterval(refresh,15000);window.addEventListener('jobs-refresh',refresh);window.addEventListener('focus',refresh);document.addEventListener('visibilitychange',refresh);window.addEventListener('saved-view-updated',update);return()=>{alive=false;clearInterval(timer);window.removeEventListener('jobs-refresh',refresh);window.removeEventListener('focus',refresh);document.removeEventListener('visibilitychange',refresh);window.removeEventListener('saved-view-updated',update)}},[])
 const recent=[...requests].sort((a,b)=>(b.created_at||0)-(a.created_at||0))
 return <><section className="discover-shelf"><div className="panel-head"><div><h2>Recently added</h2><p className="muted">The newest albums in Plex.</p></div><a href="#library">View library</a></div>{error&&<ErrorNotice error={error}/>}<div className="discover-grid">{data?.rows.slice(0,addedCount).map((a:any)=><a className="panel album-card shelf-card artwork-only" aria-label={`${a.title} · ${a.artist}`} title={`${a.title} · ${a.artist}`} key={a.album_id} href={'#library/album/'+encodeURIComponent(a.album_id)}><AlbumCover src={a.artwork}><CoverStatus row={{availability:"In Plex",plex_available:true}}/></AlbumCover></a>)}</div>{!data&&!error&&<p role="status">Loading saved Plex albums…</p>}{data&&!data.rows.length&&<p className="muted">{data.dates_available?'No recently added albums.':'Run a Plex library scan to save album-added dates.'}</p>}{data?.rows.length>addedCount&&<button onClick={()=>setAddedCount(n=>n+6)}>Show more recently added</button>}</section>{account?.lidarr_enabled!==false&&<section className="discover-shelf"><div className="panel-head"><div><h2>Recent requests</h2><p className="muted">{account?.admin?'Latest requests from all users.':'Your latest album requests.'}</p></div><a href="#requests">View requests</a></div><div className="recent-request-list">{recent.slice(0,requestCount).map(a=><RecentRequestCard row={a} key={a.key}/>)}</div>{requestState.error&&<ErrorNotice error={requestState.error}/>} {!requestState.loaded&&!requestState.error&&<p role="status">Loading requests…</p>}{requestState.loaded&&!recent.length&&!requestState.error&&<p className="muted">No requests yet.</p>}{recent.length>requestCount&&<button onClick={()=>setRequestCount(n=>n+6)}>Show more requests</button>}</section>}</>
}
