import PageLoading from './PageLoading'
import SyncNotice from './SyncNotice'
import LibraryInitialization from './LibraryInitialization'
import {DesktopNavigation,MobileNavigation} from './AppNavigation'
import {FileToolsSettings} from './Library'
import SourceHistory from './SourceHistory'
import {DiscoverPreferences,BlockedArtists} from './DiscoverPreferences'
import {RequestAlbumLabel,CanRequest,CanPlaylists} from './Auth'
import {PendingRequests} from './Discover'
import TextPlaylist from './TextPlaylist'
import PlaylistEditor from './PlaylistEditor'
import SharedPlaylists from './SharedPlaylists'
import ManualMatches from './ManualMatches'
import {useAccount,AccountButton,AccountSettings} from './Auth'
import Discover,{Users,LastfmSettings} from './Discover'
import Library from './Library'
import LibraryIssues from './LibraryIssues'
import BulkIgnore from './BulkIgnore'
import ErrorNotice from './ErrorNotice'
import IgnoreDialog from './IgnoreDialog'
import {installNavigation,usePageState,trackOrigin} from './Navigation'
import Brand from './Brand'
import {LidarrRequestStatus, refreshLidarrRequests, LidarrMonitor, LidarrDownloads} from './LidarrRequests'
import PlaylistSchedule from './PlaylistSchedule'
import RenamePlaylist from './RenamePlaylist'
import AvailabilitySettings from './Availability'
import MusicBrainzSettings from './MusicBrainz'
import MetadataSources from './MetadataSources'
import FanartSettings from './FanartSettings'
import { useCallback, useEffect, useRef, useState } from 'react'
import { request, api, Job, activeJob, activityJob, Playlist, MissingTrack } from './api'
import General from './General'
import MatchQueue,{useMatchQueue,queuedMatchFor,refreshMatchQueue} from './MatchQueue'
import ConsoleSettings from './ConsoleSettings'
import LidarrSettings, { AddLidarrAlbum } from './Lidarr'
import Modal from './Modal'
import QuickActions from './QuickActions'
import { playlistPredicates, openPlaylistView } from './playlistFilters'
import { matchLabel, displayText } from './labels'
import Appearance from './Appearance'
import ActivityNav from './ActivityNav'
import { About, Backups, DatabaseMaintenance, Matching, Ignored } from './SettingsTools'
import { usePolling } from './usePolling'
import MatchPicker from './MatchPicker'
import HealthCard, { stamp, RelativeTime } from './HealthCard'
import JobsPanel, { JobRow, JobHistory, actionName } from './JobsPanel'

import Activity, { OperationOutput, showActivity } from './Activity'
import PlaylistsView from './PlaylistsView'
import SearchPage from './SearchPage'

import { filterMatches, Icon, FilterMenu, FilterChips, SortMenu, trackLink } from './Controls'
import TrackDetails from './TrackDetails'
import { ThemeListener, applyTheme } from './Appearance'
applyTheme()

type Enqueue=(action:string,payload?:any)=>Promise<Job>
const currentRoute=()=>{const raw=location.hash.slice(1);try{const key=decodeURIComponent(raw);if(key.startsWith('spotify:')||key.startsWith('apple:')||key.startsWith('applemusic:'))return `playlist/${encodeURIComponent(key)}`}catch{}return raw.startsWith('dashboard')?(raw.endsWith('/add')?'playlists/add':'discover'):(raw||'discover')}
const link=(key:string)=>`#playlist/${encodeURIComponent(key)}`
function ordered<T>(rows:T[],value:(r:T)=>any,direction:string){return [...rows].sort((a,b)=>{const x=value(a),y=value(b);if(x===null||x===undefined||x==='')return y===null||y===undefined||y===''?0:1;if(y===null||y===undefined||y==='')return -1;const n=typeof x==='number'?x-y:String(x).localeCompare(String(y),undefined,{numeric:true,sensitivity:'base'});return direction==='asc'?n:-n})}
function Direction({value,onChange}:{value:string;onChange:(s:string)=>void}){return <select aria-label="Sort direction" value={value} onChange={e=>onChange(e.target.value)}><option value="asc">Ascending</option><option value="desc">Descending</option></select>}

export default function App(){
  const account=useAccount()
  const admin=!!account?.admin
  const [route,setRoute]=useState(currentRoute)
  const [headerQuery,setHeaderQuery]=useState('')
  useEffect(()=>installNavigation(currentRoute,()=>setRoute(currentRoute())),[])
  const page=route.startsWith('playlist/')?'playlists':route.startsWith('track/')?(trackOrigin()?.route.startsWith('playlist')?'playlists':trackOrigin()?.route.split('/')[0]||'search'):route.split('/')[0]
  let detail='';try{if(route.startsWith('playlist/'))detail=decodeURIComponent(route.slice(9))}catch{}
  let track:any=null;try{if(route.startsWith('track/')){const value=JSON.parse(decodeURIComponent(route.slice(6)));if(typeof value.title==='string'&&typeof value.artist==='string')track=value}}catch{}
  const [playlists,setPlaylists]=useState<Playlist[]>([]),[missing,setMissing]=useState<MissingTrack[]>([]),[jobs,setJobs]=useState<Job[]>([]),[health,setHealth]=useState<any>({})
  const [loaded,setLoaded]=useState({playlists:false,missing:false})
  const [connection,setConnection]=useState({jobs:false,health:false})
  const [message,setMessageValue]=useState(''),[messageKind,setMessageKind]=useState<'success'|'error'>('success')
  const setMessage=(value:string,kind:'success'|'error'='success')=>{setMessageValue(value);setMessageKind(kind)}
  const [revision,setRevision]=useState(0)
  const jobsRef=useRef<Job[]>([])
  const initializedJobs=useRef(false)
  const refreshPage=useCallback(async()=>{
    const reads:Promise<void>[]=[]
    if(page==='dashboard'||page==='playlists') reads.push(api.playlists().then(rows=>{setPlaylists(rows);setLoaded(v=>({...v,playlists:true}))}))
    if(page==='dashboard'||(page==='missing'&&!admin)) reads.push(api.missing().then(rows=>{setMissing(rows);setLoaded(v=>({...v,missing:true}))}))
    await Promise.all(reads)
  },[page])
  const readJobs=useCallback(async()=>{
    const rows=(await api.jobs()).filter(activityJob)
    const previous=new Map(jobsRef.current.map(j=>[j.id,j]))
    if(initializedJobs.current && rows.some(j=>!activeJob(j) && (!previous.has(j.id)||activeJob(previous.get(j.id)!)))) setRevision(v=>v+1)
    initializedJobs.current=true;jobsRef.current=rows;setJobs(rows)
  },[])
  usePolling(readJobs,()=>jobsRef.current.some(activeJob)?1000:45000,'jobs-refresh',jobs=>setConnection(v=>({...v,jobs})))
  usePolling(async()=>setHealth(await api.health()),()=>45000,'health-refresh',health=>setConnection(v=>({...v,health})))
  useEffect(()=>{
    let stopped=false,timer:ReturnType<typeof setTimeout>|undefined
    refreshPage().catch(e=>{
      const hasData=page==='playlists'?loaded.playlists:page==='missing'?loaded.missing:false
      if(hasData&&/timed out|Failed to fetch|NetworkError|Load failed/i.test(e.message))timer=setTimeout(()=>{if(!stopped)void refreshPage().catch(()=>{})},10000)
      else setMessage(e.message,'error')
    })
    return()=>{stopped=true;clearTimeout(timer)}
  },[refreshPage,route])
  useEffect(()=>{if(revision){if(page==='missing'||page==='dashboard')void api.missing().then(setMissing).catch(()=>{});if(page==='playlists'||page==='dashboard')void api.playlists().then(setPlaylists).catch(()=>{})}},[revision,page])
  useEffect(()=>{
    const wake=()=>{if(!document.hidden)void refreshPage().catch(()=>{})}
    window.addEventListener('focus',wake);document.addEventListener('visibilitychange',wake)
    return()=>{window.removeEventListener('focus',wake);document.removeEventListener('visibilitychange',wake)}
  },[refreshPage])
  const refresh=useCallback(async()=>{
    await Promise.all([refreshPage(),readJobs(),api.health().then(setHealth)])
  },[refreshPage,readJobs])
  const enqueue:Enqueue=async(action,payload={})=>{try{setMessage('');const job=await api.enqueue(action,payload);await readJobs().catch(()=>{});return job}catch(e:any){setMessage(e.message,'error');throw e}}
  useEffect(()=>{const changed=(event:Event)=>{const path=(event as CustomEvent).detail.path;if(path.startsWith('/api/missing')&&((page==='missing'&&!admin)||page==='dashboard'))void api.missing().then(setMissing).catch(()=>{});if(path==='/api/playlists'&&(page==='playlists'||page==='dashboard'))void api.playlists().then(setPlaylists).catch(()=>{})};window.addEventListener('saved-view-updated',changed);return()=>window.removeEventListener('saved-view-updated',changed)},[page])
  useEffect(()=>{if(route==='playlists/add')requestAnimationFrame(()=>{const form=document.getElementById('add-playlist');form?.scrollIntoView({behavior:'auto'});form?.querySelector<HTMLInputElement>('input')?.focus({preventScroll:true})})},[route])
  useEffect(()=>{const notice=()=>{void readJobs().catch(()=>{})};const changed=()=>{void readJobs().catch(()=>{});void refreshLidarrRequests()};window.addEventListener('bridge-notice',notice);window.addEventListener('lidarr-requested',changed);return()=>{window.removeEventListener('bridge-notice',notice);window.removeEventListener('lidarr-requested',changed)}},[readJobs])
  const lidarrRevision=jobs.filter(j=>j.action==='lidarr_add'||j.action==='lidarr_search').map(j=>`${j.id}:${j.status}:${j.progress}`).join('|')
  useEffect(()=>{if((page==='missing'||page==='requests'||page==='activity'||detail||track))void refreshLidarrRequests()},[page,detail,route,lidarrRevision])
  const active=jobs.filter(activeJob)
  const playlistCard=(label:string,filter?:string)=> <Stat key={label} label={label} value={filter?playlists.filter(playlistPredicates[filter]).length:playlists.length} onClick={()=>openPlaylistView(filter?[filter]:[])}/>
  return <div className="shell"><SyncNotice/><ThemeListener/><LidarrMonitor enabled={account?.lidarr_enabled!==false&&(page==='missing'||page==='requests'||page==='activity'||!!detail||!!track)}/><DesktopNavigation jobs={jobs} page={page} admin={admin} health={health}/>
  <main><header><a className="mobile-brand" href="#discover" aria-label="Harmonia home"><Brand wordmark/></a><form className="header-search" onSubmit={e=>{e.preventDefault();location.hash="search/"+encodeURIComponent(headerQuery)}}><input type="search" aria-label="Search Harmonia" placeholder="Search artists, albums, or songs…" value={headerQuery} onChange={e=>setHeaderQuery(e.target.value)}/></form><AccountButton/>{!['library','issues','requests','users','search'].includes(page)&&<div className="page-heading"><h1>{track?'Track details':detail?'Playlist details':page==='activity'?'Jobs':route==='account/appearance'?'Appearance':page[0]?.toUpperCase()+page.slice(1)}</h1></div>}</header>
    <PageLoading/>
    <LibraryInitialization/>
    {['missing','playlists','search'].includes(page)&&<CanPlaylists><MatchQueue jobs={jobs}/></CanPlaylists>}
    {(connection.jobs||connection.health)&&<p role="status" className="muted">Connection interrupted. Showing the last known status and retrying automatically; running jobs continue on the server.</p>}
    {message&&<div className={`notice ${messageKind==='success'?'success-notice':''}`}>{messageKind==='error'?<ErrorNotice error={message}/>:<p role="status">{message}</p>}<button onClick={()=>setMessage('')} aria-label="Dismiss message">×</button></div>}
    {page==='discover'&&<Discover key={route} initialQuery={decodeURIComponent(route.slice(9))}/>}{page==='users'&&admin&&<Users/>}{page==='issues'&&(account?.admin||account?.can_library_issues?<LibraryIssues/>:<section className="panel"><h1>Issues</h1><p>Access is disabled for your account. Contact an administrator.</p></section>)}{page==='library'&&<Library initialAlbumId={route.startsWith('library/album/')?decodeURIComponent(route.slice(14)):''} initialArtistId={route.startsWith('library/artist/')?decodeURIComponent(route.slice(15)):''}/>}{page==='blocklist'&&<><BlockedArtists/><Ignored/></>}{page==='account'&&(route==='account/appearance'?<Appearance/>:route==='account/matches'?<ManualMatches/>:<><AccountSettings/><Appearance/><DiscoverPreferences/></>)}
    {page==='activity'&&<><Activity jobs={jobs} jobId={route.split('/')[1]||''}/><JobHistory jobs={jobs} refresh={refresh}/></>}
    {page==='requests'&&account?.lidarr_enabled===false&&<section className="panel"><h1>Requests unavailable</h1><p>Lidarr is disabled. You can still browse and sync your Plex library.</p></section>}{page==='requests'&&account?.lidarr_enabled!==false&&admin&&<PendingRequests/>}{page==='requests'&&account?.lidarr_enabled!==false&&!track&&<LidarrDownloads historyView albumKey={decodeURIComponent(route.split('/')[1]||'')}/>}
    {page==='search'&&!track&&<SearchPage initialQuery={route.startsWith('search/')?decodeURIComponent(route.slice(7)):undefined}/>}{track&&<TrackDetails key={JSON.stringify(track)} track={track} jobs={jobs} enqueue={enqueue}/>}
    {page==='dashboard'&&!track&&<><section className="cards">{playlistCard('Playlists')}{playlistCard('Ready to Sync','Ready to Sync')}<Stat label="Missing Tracks" value={missing.length} onClick={()=>{location.hash='missing'}}/>{playlistCard('Needs Attention','Needs Attention')}<Stat label="Active Jobs" value={active.length} onClick={()=>{location.hash='activity'}}/>{playlistCard('Never Synced','Never Synced')}</section>{!health.plex_configured&&<p className="health-error">Plex is not configured. <a href="#settings/plex">Open Plex settings</a></p>}<QuickActions enqueue={enqueue}/><CanPlaylists><AddPlaylist jobs={jobs} enqueue={enqueue} playlists={playlists}/></CanPlaylists><section className="panel"><h2>Recent activity</h2>{jobs.slice(0,4).map(j=><JobRow key={j.id} job={j} refresh={refresh}/>)}{!jobs.length&&<p>No jobs yet.</p>}</section></>}

    {page==='playlists'&&!detail&&!track&&<CanPlaylists><AddPlaylist jobs={jobs} enqueue={enqueue} playlists={playlists}/></CanPlaylists>}
    {page==='playlists'&&!detail&&!track&&<SharedPlaylists/>}
    {page==='playlists'&&!detail&&!track&&<PlaylistsView key={route} initialView={route.startsWith('playlists/view/')?route.slice(15):undefined} loaded={loaded.playlists} playlists={playlists} enqueue={enqueue} refresh={refresh}/>}
    {detail&&<Detail key={detail} playlistKey={detail} latest={playlists.find(p=>p.key===detail)} enqueue={enqueue} refresh={refresh} jobs={jobs}/>}
    {page==='missing'&&!track&&<Missing loaded={loaded.missing} tracks={missing} enqueue={enqueue} refresh={refresh}/>}
    {page==='settings'&&!admin&&<><nav className="settings-subnavigation" aria-label="Settings sections"><a href="#settings">Preferences</a></nav>{route.split('/')[1]==='shared'?<p>Shared playlist sources are managed by an administrator. <a href="#playlists">Browse shared playlists</a>.</p>:<><Appearance/><DiscoverPreferences/></>}</>}{page==='settings'&&admin&&<Settings tab={route.split('/')[1]||''} jobs={jobs} refresh={refresh} setMessage={setMessage}/>}
  </main><MobileNavigation jobs={jobs} page={page} admin={admin} health={health}/></div>

}
function Stat({label,value,onClick}:{label:string;value:number;onClick:()=>void}){return <button className="stat stat-link" onClick={onClick} aria-label={`Open ${label}: ${value}`}><span>{label}</span><strong>{value}</strong></button>}
function AddPlaylist({jobs,enqueue,playlists}:{jobs:Job[];enqueue:Enqueue;playlists:Playlist[]}){
 const account=useAccount()
 const [url,setUrl]=useState(''),[name,setName]=useState(''),[mode,setMode]=useState('inherit'),[id,setId]=useState(''),[error,setError]=useState(''),[pending,setPending]=useState(false)
 const job=jobs.find(j=>j.id===id),busy=pending
 async function add(){setPending(true);setError('');try{const j=await enqueue('add',{url:url.trim(),name:name.trim(),sync_mode:mode});setId(j.id);setUrl('');setName('')}catch(e:any){setError(e.message)}finally{setPending(false)}}
 return <section className="panel" id="add-playlist"><div className="panel-head"><h2>Add Playlist</h2><TextPlaylist/></div><p className="muted">Paste a public Spotify or Apple Music playlist. Harmonia will match its tracks and create the Plex copy.</p><form onSubmit={e=>{e.preventDefault();void add()}}><div className="add"><input required aria-label="New playlist URL" placeholder="Spotify or Apple Music playlist URL" value={url} onChange={e=>{setUrl(e.target.value);setError('');setId('')}} disabled={busy}/><button className="primary" disabled={busy||!url.trim()}>{busy?'Adding…':'Add Playlist'}</button></div><div className="toolbar"><label>Plex playlist name (optional)<input value={name} maxLength={200} placeholder="Use source playlist name" onChange={e=>setName(e.target.value)} disabled={busy}/></label>{account?.admin&&<label>Sync mode<select value={mode} onChange={e=>setMode(e.target.value)} disabled={busy}><option value="inherit">Server schedule</option><option value="disabled">Manual only</option></select></label>}</div>{name.trim()&&playlists.some(p=>p.name.toLowerCase()===name.trim().toLowerCase())&&<p role="status">A playlist already uses this name. This will create a separate playlist; choose a distinct name to tell them apart.</p>}<p className="muted">Names can be shared, but playlists stay separate. Use a custom name to distinguish owners. {account?.admin?'Set a custom weekly schedule in Playlist Details after adding.':'Sync scheduling is controlled by your administrator.'}</p></form>{error&&<ErrorNotice error={error}/>}{job&&<div role="status"><p>{displayText(job.progress)}</p>{job.error&&<ErrorNotice error={displayText(job.error)}/>}{job.status==='completed'&&job.result?.key&&<p>Playlist added: <a href={link(job.result.key)}>{job.result.name}</a></p>}<button onClick={()=>showActivity(job.id)}>View Activity</button></div>}</section>
}
function Missing({tracks,loaded,enqueue,refresh}:{tracks:MissingTrack[];loaded:boolean;enqueue:Enqueue;refresh:()=>Promise<void>}){
 const matchQueue=useMatchQueue()
 const account=useAccount(),[userFilter,setUserFilter]=useState('all'),[userTracks,setUserTracks]=useState<MissingTrack[]>([]),[adminLoaded,setAdminLoaded]=useState(false),[missingUsers,setMissingUsers]=useState<any[]>([])
 const loadUsers=useCallback(async()=>{const rows=await request<MissingTrack[]>('/api/admin/missing');setUserTracks(rows);setAdminLoaded(true)},[])
 useEffect(()=>{if(!account?.admin)return;void request<any[]>('/api/users').then(users=>setMissingUsers(users.filter(u=>!u.disabled&&!u.pending_login))).catch(e=>setError(e.message));void loadUsers().catch(e=>setError(e.message));const changed=()=>{void loadUsers().catch(e=>setError(e.message))};const updated=(e:Event)=>{if((e as CustomEvent).detail.path==='/api/admin/missing')changed()};const timer=setInterval(()=>{if(!document.hidden)changed()},15000);window.addEventListener('jobs-refresh',changed);window.addEventListener('saved-view-updated',updated);return()=>{clearInterval(timer);window.removeEventListener('jobs-refresh',changed);window.removeEventListener('saved-view-updated',updated)}},[account?.admin,loadUsers])
 if(account?.admin){tracks=userTracks;loaded=adminLoaded;refresh=loadUsers}
 const [prefs,setPrefs]=useState<{query:string;sort:string;filters:string[]}>(()=>{try{const p=JSON.parse(localStorage.getItem('bridge-missing-view')||'{}');return {query:typeof p.query==='string'?p.query:'',sort:typeof p.sort==='string'?p.sort:'occurrence_count:desc',filters:Array.isArray(p.filters)?p.filters.filter((v:unknown)=>typeof v==='string'):[]}}catch{return {query:'',sort:'occurrence_count:desc',filters:[]}}})
 const {query,sort,filters}=prefs
 const setQuery=(query:string)=>setPrefs(p=>({...p,query})),setSort=(sort:string)=>setPrefs(p=>({...p,sort})),setFilters=(filters:string[])=>setPrefs(p=>({...p,filters}))
 useEffect(()=>{try{localStorage.setItem('bridge-missing-view',JSON.stringify(prefs))}catch{}},[prefs])
 const [lidarrTrack,setLidarrTrack]=useState<MissingTrack|null>(null)
 const [selected,setSelected]=useState<MissingTrack|null>(null),[ignore,setIgnore]=useState<MissingTrack|null>(null),[error,setError]=useState('')
 const [checked,setChecked]=useState<string[]>([]),[bulk,setBulk]=useState<MissingTrack[]|null>(null),[notice,setNotice]=useState('')
 const canSelect=!account?.admin||userFilter===account?.id
 const trackId=(t:MissingTrack)=>JSON.stringify([t.user_id,t.title,t.artist,t.album||''])
 useEffect(()=>{setChecked([])},[query,filters,userFilter])
 useEffect(()=>{setChecked(current=>current.filter(k=>tracks.some(t=>trackId(t)===k)))},[tracks])
 const labels:Record<string,string>={}
 tracks.forEach(t=>t.memberships.forEach(m=>{labels[`playlist:${m.key}`]=`Playlist: ${m.name}`}))
 const sourceFilters=filters.filter(f=>['Spotify','Apple Music','Custom Playlist'].includes(f)),statusFilters=filters.filter(f=>['Missing','LOST'].includes(f)),playlistFilters=filters.filter(f=>f.startsWith('playlist:'))
 const [sortField,direction]=sort.split(':')
 const excluded=filters.filter(f=>f.startsWith('!')).map(f=>f.slice(1))
 const matchesChoice=(t:MissingTrack,f:string)=>f==='Manually matched elsewhere'?!!t.manual_matches_elsewhere?.length:t.memberships.some(m=>f.startsWith('playlist:')?f===`playlist:${m.key}`:['Missing','LOST'].includes(f)?m.statuses?.includes(f):f===(m.source==='text'?'Custom Playlist':m.source==='spotify'?'Spotify':'Apple Music'))
 const rows=ordered(tracks.filter(t=>(!account?.admin||userFilter==='all'||t.user_id===userFilter)&&`${t.title} ${t.artist} ${t.album||''}`.toLowerCase().includes(query.toLowerCase())&&!excluded.some(f=>matchesChoice(t,f))&&(!filters.includes('Manually matched elsewhere')||!!t.manual_matches_elsewhere?.length)&&t.memberships.some(m=>(!sourceFilters.length||sourceFilters.includes(m.source==='text'?'Custom Playlist':m.source==='spotify'?'Spotify':'Apple Music'))&&(!playlistFilters.length||playlistFilters.includes(`playlist:${m.key}`))&&(!statusFilters.length||statusFilters.some(f=>m.statuses?.includes(f))))),t=>(t as any)[sortField],direction)

 useEffect(()=>{if(filters.some(f=>f.replace(/^!/, '')==='Favorites only'))setPrefs({...prefs,filters:filters.filter(f=>f.replace(/^!/, '')!=='Favorites only')})},[])
 const choices=['Spotify','Apple Music','Custom Playlist','Missing','LOST','Manually matched elsewhere',...Object.keys(labels).sort((a,b)=>labels[a].localeCompare(labels[b]))]
 return <section className="panel missing-list"><div className="list-controls"><h2>Missing tracks</h2>{account?.admin&&<label className="missing-user-filter"><span>User</span><select aria-label="Filter missing tracks by user" value={userFilter} onChange={e=>setUserFilter(e.target.value)}><option value="all">All users</option>{missingUsers.map(user=><option key={user.id} value={user.id}>{user.name}</option>)}</select></label>}<div className="actions toolbar-icons"><FilterMenu mode="grouped" choices={choices} value={filters} onChange={setFilters} labels={labels}/><SortMenu choices={{'artist:asc':'Artist','album:asc':'Album','title:asc':'Name A–Z','title:desc':'Name Z–A','last_checked:desc':'Last checked: newest','last_checked:asc':'Last checked: oldest','occurrence_count:desc':'Most occurrences','occurrence_count:asc':'Fewest occurrences','playlist_count:desc':'Most playlists','playlist_count:asc':'Fewest playlists'}} value={sort} onChange={setSort}/><button className="icon-button" title="Refresh missing tracks" aria-label="Refresh missing tracks" onClick={()=>refresh().catch(e=>setError(e.message))}><Icon name="refresh"/></button></div></div><p className="muted">An occurrence is one appearance of a track. Expand the counts to see its playlists. Filters combine across categories; choices within a category include either option.</p><p className="list-count">{rows.length} of {tracks.length} tracks</p><div className="list-secondary"><input aria-label="Filter missing tracks" placeholder="Filter by track, artist or album" value={query} onChange={e=>setQuery(e.target.value)}/></div><FilterChips value={filters} onChange={setFilters} labels={labels}/>{error&&<ErrorNotice error={error}/>} {notice&&<p className="success-notice" role="status">{notice} <a href="#activity">View Jobs</a></p>}{loaded&&!!rows.length&&(!account?.admin||userFilter===account?.id)&&<div className="selection-bar"><label><input type="checkbox" aria-label="Select visible tracks" checked={rows.length>0&&rows.every(t=>checked.includes(trackId(t)))} onChange={e=>setChecked(e.target.checked?rows.map(trackId):[])}/>Select visible</label><span>{checked.length} selected</span><button disabled={!checked.length} onClick={()=>setBulk(rows.filter(t=>checked.includes(trackId(t))))}>Ignore selected</button><button disabled={!checked.length} onClick={()=>setChecked([])}>Clear selection</button></div>}{bulk&&<BulkIgnore tracks={bulk} close={()=>setBulk(null)} done={()=>{setChecked([]);setNotice('Ignore batch queued. Track results will be available in Jobs; Plex changes apply on the next sync.')}}/>}
 {rows.map((t,i)=><article className={`playlist-card missing-track-card ${canSelect?'selectable':''}`} key={`${t.user_id}-${t.title}-${t.artist}`}><div className="panel-head missing-track-head">{canSelect&&<input type="checkbox" aria-label={`Select ${t.title} by ${t.artist}`} checked={checked.includes(trackId(t))} onChange={e=>setChecked(e.target.checked?[...checked,trackId(t)]:checked.filter(k=>k!==trackId(t)))}/>}<div className="missing-track-identity">{t.user_id&&t.user_id!==account?.id?<strong>{t.title}</strong>:<a className="track-title" href={trackLink(t)}>{t.title}</a>}<p>{t.artist} · {t.album||'N/A'}</p>{t.user_name&&<small>User: {t.user_name}</small>}<LidarrRequestStatus track={t}/></div><div className="actions">{(!t.user_id||t.user_id===account?.id)&&queuedMatchFor(matchQueue,t)&&<span className="pill queued">Match queued</span>}<button onClick={()=>setSelected(t)}>{(!t.user_id||t.user_id===account?.id)&&queuedMatchFor(matchQueue,t)?'Change Queued Match':'Review Match'}</button>{(!t.user_id||t.user_id===account?.id)&&<><CanRequest><button onClick={()=>setLidarrTrack(t)}><RequestAlbumLabel/></button></CanRequest><button onClick={()=>setIgnore(t)}>Ignore</button></>}</div></div>{(!t.user_id||t.user_id===account?.id)&&!!t.manual_matches_elsewhere?.length&&<p><a href={trackLink(t)}>Manually matched elsewhere · Review saved selections ({t.manual_matches_elsewhere.length})</a></p>}<small>Last checked by sync/match: {stamp(t.last_checked)}</small><details><summary>{t.occurrence_count} occurrences in {t.playlist_count} playlists</summary>{t.memberships.map(m=><p key={m.key}>{t.user_id&&t.user_id!==account?.id?<span>{m.name}</span>:<a href={link(m.key)}>{m.name}</a>} · {m.count} occurrence(s)</p>)}</details></article>)}{!loaded?<p role="status">Loading missing tracks…</p>:!rows.length&&<div className="empty-state"><p>{query||filters.length?'No missing tracks match your filters.':'No missing tracks. You’re all caught up.'}</p>{(query||filters.length>0)&&<button onClick={()=>{setQuery('');setFilters([])}}>Clear Filters</button>}</div>}
 {selected&&<MatchPicker track={selected} userId={selected.user_id} onClose={()=>setSelected(null)} onSave={async(c,keys,provenance)=>{if(selected.user_id){await request('/api/admin/missing/match',{method:'POST',body:JSON.stringify({target_user_id:selected.user_id,title:selected.title,artist:selected.artist,album:selected.album||'',plex_id:c.plex_id,playlist_keys:keys,provenance:'manual'})});setNotice(`Match and sync queued for ${selected.user_name}.`);window.dispatchEvent(new Event('jobs-refresh'));return}await api.stageMatch({candidate:{title:c.title,artist:c.artist,album:c.album},title:selected.title,artist:selected.artist,album:selected.album||'',plex_id:c.plex_id,provenance,playlist_keys:keys});setError('');void refreshMatchQueue().catch(()=>{});setNotice(`Match queued for ${selected.title}. It is waiting in the Match Queue above.`)}}/>}
 {lidarrTrack&&<AddLidarrAlbum track={lidarrTrack} onClose={()=>setLidarrTrack(null)}/>}
 {ignore&&<IgnoreDialog track={ignore} close={()=>setIgnore(null)} done={async()=>{await refresh();setNotice('Ignore queued. It will be saved after current activity finishes; Plex changes apply on the next sync.')}}/>}
 </section>
}
const detailCache=new Map<string,any>()
function Detail({playlistKey,latest,enqueue,refresh,jobs}:{playlistKey:string;latest?:Playlist;enqueue:Enqueue;refresh:()=>Promise<void>;jobs:Job[]}){
 const matchQueue=useMatchQueue()
 const [lidarrTrack,setLidarrTrack]=useState<any>(null),[ignore,setIgnore]=useState<any>(null),[notice,setNotice]=useState('')
 const [data,setData]=useState<any>(()=>detailCache.get(playlistKey)||null),[error,setError]=useState(''),[query,setQuery]=usePageState('detail-query:'+playlistKey,''),[selected,setSelected]=useState<any>(null),[loading,setLoading]=useState(false)
 const [stage,setStage]=useState<any>({events:[],percent:0}),[trackFilters,setTrackFilters]=usePageState<string[]>('detail-filters:'+playlistKey,[]),[trackSort,setTrackSort]=usePageState('detail-sort:'+playlistKey,'order')
 const mounted=useRef(false), inflight=useRef<Promise<void>|null>(null), reload=useRef(false)
 const load=useCallback(async()=>{
   if(inflight.current){reload.current=true;return inflight.current}
   const work=(async()=>{
     do {
       reload.current=false
       if(mounted.current){setLoading(true);setError('');setStage({events:[],percent:0})}
       try{const result=await api.detail(playlistKey,p=>{if(mounted.current)setStage(p)});if(mounted.current){detailCache.set(playlistKey,result);if(detailCache.size>8)detailCache.delete(detailCache.keys().next().value!);setData(result);setStage((previous:any)=>({...previous,events:[...previous.events,{created_at:new Date().toISOString(),message:`Playlist ready · ${result.tracks.length} source tracks`}]}))}}
       catch(e:any){if(mounted.current)setError(e.message)}
       finally{if(mounted.current)setLoading(false)}
     }while(reload.current&&mounted.current)
   })()
   inflight.current=work
   try{await work}finally{inflight.current=null}
 },[playlistKey])
 useEffect(()=>{const changed=(event:Event)=>{if((event as CustomEvent).detail.path.startsWith('/api/playlists/'+encodeURIComponent(playlistKey)+'/detail'))void load()};window.addEventListener('saved-view-updated',changed);return()=>window.removeEventListener('saved-view-updated',changed)},[playlistKey,load])
 useEffect(()=>{mounted.current=true;if(!inflight.current)void load();return()=>{mounted.current=false}},[load])
 const seenJobs=useRef<Map<string,string>|null>(null)
 useEffect(()=>{
   const signature=(j:Job)=>JSON.stringify([j.status,j.result?.playlists?.filter((p:any)=>p.key===playlistKey).map((p:any)=>[p.key,p.ok])])
   const previous=seenJobs.current
   seenJobs.current=new Map(jobs.map(j=>[j.id,signature(j)]))
   if(previous && jobs.some(j=>previous.get(j.id)!==signature(j) &&
      (j.result?.playlists?.some((p:any)=>p.key===playlistKey)||(['completed','failed','cancelled'].includes(j.status)&&
       ((j.payload||{}).scope==='all'||(j.payload||{}).playlist_keys?.includes(playlistKey)||j.result?.key===playlistKey))))) void load()
 },[jobs,playlistKey,load])
 async function run(action:string){try{await enqueue(action,{scope:'selected',playlist_keys:[playlistKey]})}catch(e:any){setError(e.message)}}
 const visibleTracks=data?.tracks.filter((t:any)=>filterMatches(trackFilters,f=>f==='Needs Attention'?t.needs_attention:f.toLowerCase()===String(t.status).toLowerCase())&&`${t.title} ${t.artist} ${t.album||''} ${t.match?.title||''}`.toLowerCase().includes(query.toLowerCase())).sort((a:any,b:any)=>(trackSort.split(':')[0]==='title'?a.title.localeCompare(b.title):trackSort.split(':')[0]==='artist'?a.artist.localeCompare(b.artist):a.index-b.index)*(trackSort.endsWith(':desc')?-1:1))||[]
 return <section className="panel">{lidarrTrack&&<AddLidarrAlbum track={lidarrTrack} onClose={()=>setLidarrTrack(null)}/>}<a href="#playlists">← Back to playlists</a>{notice&&<p role="status">{notice}</p>}<CanPlaylists>{ignore&&<IgnoreDialog track={{...ignore,memberships:[{key:playlistKey,name:data?.playlist.name||playlistKey}]}} close={()=>setIgnore(null)} done={async()=>{await refresh();setNotice('Ignore queued. Plex changes apply on the next sync.')}}/>}</CanPlaylists>{error&&<><ErrorNotice error={error}/>{/404|missing or inaccessible/i.test(error)&&<CanPlaylists><button onClick={async()=>{if(!confirm("Create a replacement Plex playlist? Your saved matches and schedule will be kept. Only continue if you have checked that the original playlist is gone."))return;try{const r=await fetch(`/api/playlists/${encodeURIComponent(playlistKey)}/recreate`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({confirmed:true})});const v=await r.json();if(!r.ok)throw Error(v.detail);setNotice("Replacement queued. See Activity for progress.")}catch(e:any){setError(e.message)}}}>Recreate Plex Playlist</button></CanPlaylists>}</>}<OperationOutput stage={stage} loading={loading} error={error}/>{data?<><div className="panel-head"><h2>{(data.playlist).name}</h2><div className="actions"><CanPlaylists><PlaylistEditor playlistKey={playlistKey}/><RenamePlaylist playlist={data.playlist}/></CanPlaylists></div></div><p className="muted">Source: {data.playlist.source==='text'?'Custom Playlist':<a href={data.playlist.source_url} target="_blank" rel="noreferrer">{data.metadata.name||data.playlist.source_name||data.playlist.name} ↗</a>}{data.playlist.source_owner?` · ${data.playlist.source_owner}`:''}</p>{data.playlist.source==='text'&&<CanPlaylists><TextPlaylist sourceId={data.playlist.source_id}/></CanPlaylists>}<PlaylistSchedule playlistKey={playlistKey}/><p>{data.metadata.description}</p>{data.source_refresh_required&&<p className="muted">No saved source tracks yet. Sync this playlist to read its current tracks.</p>}<p><RelativeTime value={(data.playlist).last_synced} prefix="Synced"/></p><div className="detail-toolbar actions"><FilterMenu choices={['Needs Attention','Ignored','Manual','Automatic']} value={trackFilters} onChange={setTrackFilters}/><SortMenu choices={{order:'Source order',title:'Track title',artist:'Artist'}} value={trackSort} onChange={setTrackSort}/><button className="icon-button" disabled={loading} title="Refresh tracks" aria-label="Refresh tracks" onClick={load}><Icon name="refresh"/></button><CanPlaylists><button className="primary" onClick={()=>run('sync')}>Sync Now</button></CanPlaylists></div><p className="muted">{(data.playlist).saved_matches} stored mappings · Current tracks: {(data.playlist).match_counts?.automatic||0} Auto · {(data.playlist).match_counts?.manual||0} Manual · {(data.playlist).match_counts?.legacy||0} Saved · {(data.playlist).unresolved} missing · {(data.playlist).lost} LOST · {data.playlist.attention_tracks||0} need attention</p><HealthCard playlist={data.playlist}/><SourceHistory playlistKey={playlistKey} revision={(data.playlist).last_synced||""}/><input className="track-search" placeholder="Filter tracks in this playlist" aria-label="Filter tracks in this playlist" value={query} onChange={e=>setQuery(e.target.value)}/><FilterChips value={trackFilters} onChange={setTrackFilters}/>{visibleTracks.map((t:any)=><div className="missing-row playlist-track-row" key={t.index}><div className="grow"><strong>{t.index+1}. <a href={trackLink(t)}>{t.title}</a></strong><small>{t.artist} · {t.album||'N/A'}</small>{t.catalog_status&&<small>{t.catalog_status}</small>}{t.health_issues?.map((reason:string)=><small key={reason}>{reason}</small>)}<small>Plex: {t.match?`${t.match.title} — ${t.match.artist}`:'No match'}</small></div><div className="playlist-track-status"><LidarrRequestStatus track={t}/>{queuedMatchFor(matchQueue,t,playlistKey)&&<span className="pill queued" title={`Queued for ${queuedMatchFor(matchQueue,t,playlistKey).candidate?.title||'the selected Plex track'}`}>Match queued</span>}<span className="pill">{matchLabel(t.status)}</span></div><div className="playlist-track-actions"><CanPlaylists><button disabled={t.status==='Ignored'} onClick={()=>setSelected(t)}>{queuedMatchFor(matchQueue,t,playlistKey)?'Change Queued Match':t.plex_id?'Fix Match':'Review Match'}</button></CanPlaylists><CanRequest><button onClick={()=>setLidarrTrack(t)}><RequestAlbumLabel/></button></CanRequest><CanPlaylists><button disabled={t.status==='Ignored'} onClick={()=>setIgnore(t)}>Ignore</button></CanPlaylists></div></div>)}{!visibleTracks.length&&<div className="empty-state"><p>No tracks match this filter.</p><button onClick={()=>{setQuery('');setTrackFilters([])}}>Clear Filters</button></div>}</>:!loading&&<button onClick={load}>Retry loading playlist</button>}<CanPlaylists>{selected&&<MatchPicker track={selected} playlistKey={playlistKey} onClose={()=>setSelected(null)} onSave={async(c,keys,provenance)=>{if(selected.user_id){await request('/api/admin/missing/match',{method:'POST',body:JSON.stringify({target_user_id:selected.user_id,title:selected.title,artist:selected.artist,album:selected.album||'',plex_id:c.plex_id,playlist_keys:keys,provenance:'manual'})});setNotice(`Match and sync queued for ${selected.user_name}.`);window.dispatchEvent(new Event('jobs-refresh'));return}await api.stageMatch({candidate:{title:c.title,artist:c.artist,album:c.album},title:selected.title,artist:selected.artist,album:selected.album||'',plex_id:c.plex_id,provenance,playlist_keys:keys,replace_playlist_key:playlistKey});void refreshMatchQueue().catch(()=>{});setNotice(`Match queued for ${selected.title}. Apply it from the Match Queue above.`);void refresh().catch(()=>{})}}/>}</CanPlaylists></section>
}
function Settings({tab,jobs,refresh,setMessage}:{tab:string;jobs:Job[];refresh:()=>Promise<void>;setMessage:(s:string)=>void}){
 const pages:Record<string,string>={general:'General',shared:'Shared Playlist Sources',users:'Users',plex:'Services',metadata:'Metadata', 'file-tools':'Library & Files',tasks:'Jobs & Cache',logs:'Logs',about:'About'}
 const groups:Record<string,string>={lidarr:'plex',lastfm:'plex',musicbrainz:'metadata',fanart:'metadata',matching:'file-tools',database:'tasks',backups:'tasks'}
 const selected=tab==='jobs'?'tasks':tab==='availability'?'plex':tab||'general'
 return <div className={`settings-layout ${tab?'settings-detail':'settings-index'}`}><nav className="settings-navigation" aria-label="Settings sections">{Object.entries(pages).map(([key,label])=><a aria-current={(groups[selected]||selected)===key?'page':undefined} className={(groups[selected]||selected)===key?'active-tab':''} href={`#settings/${key}`} key={key}>{label}</a>)}</nav><div className="settings-content"><nav className="settings-subnavigation" aria-label="Settings category">{Object.entries((groups[selected]||selected)==='plex'?{plex:'Plex',lidarr:'Lidarr',lastfm:'Last.fm'}:(groups[selected]||selected)==='metadata'?{metadata:'Sources',musicbrainz:'MusicBrainz',fanart:'Fanart.tv'}:(groups[selected]||selected)==='file-tools'?{'file-tools':'Library management',matching:'Matching'}:(groups[selected]||selected)==='tasks'?{tasks:'Jobs',database:'Database',backups:'Backups'}:{}).map(([key,label])=><a key={key} href={'#settings/'+key} aria-current={selected===key?'page':undefined}>{label}</a>)}</nav>{selected==='shared'&&<SharedPlaylists mode="sources"/>}{selected==='general'&&<><Appearance/><DiscoverPreferences/><ConsoleSettings/></>}{selected==='plex'&&<><General refresh={refresh} setMessage={setMessage}/ ><AvailabilitySettings service="plex"/></>}{selected==='lidarr'&&<><LidarrSettings/><AvailabilitySettings service="lidarr"/></>}{(selected==='file-tools'||selected==='local-library')&&<FileToolsSettings/>}{selected==='metadata'&&<><MetadataSources/></>}{selected==='musicbrainz'&&<MusicBrainzSettings/>}{selected==='fanart'&&<FanartSettings/>}{selected==='lastfm'&&<LastfmSettings/>}{selected==='matching'&&<Matching/>}{selected==='users'&&<Users/>}{selected==='tasks'&&<JobsPanel jobs={jobs} refresh={refresh}/ >}{selected==='database'&&<DatabaseMaintenance jobs={jobs}/ >}{selected==='backups'&&<Backups jobs={jobs}/ >}{selected==='about'&&<About jobs={jobs}/ >}{selected==='logs'&&<Logs/>}</div></div>
}
function Logs(){
 const [entries,setEntries]=useState<any[]>([]),[facets,setFacets]=useState<any>({operations:[],workers:[],services:[]}),[debug,setDebug]=useState(false),[level,setLevel]=useState(''),[action,setAction]=useState(''),[worker,setWorker]=useState(''),[query,setQuery]=useState(''),[range,setRange]=useState('24'),[error,setError]=useState(''),[clear,setClear]=useState(false),[busy,setBusy]=useState(false),[live,setLive]=useState(true)
 async function load(){setBusy(true);try{const since=range?new Date(Date.now()-Number(range)*3600000).toISOString():'';const data=await api.logs({level,action,worker,q:query.trim(),since});setEntries(data.entries);setFacets(data.facets||facets);setDebug(!!data.debug);if(!data.debug&&level==='DEBUG')setLevel('');setError('')}catch(e:any){setError(e.message)}finally{setBusy(false)}}
 useEffect(()=>{const timer=setTimeout(load,query?350:0);return()=>clearTimeout(timer)},[level,action,worker,query,range])
 useEffect(()=>{if(!live)return;const timer=setInterval(load,3000);return()=>clearInterval(timer)},[live,level,action,worker,query,range])
 return <section className="panel diagnostic-log"><div className="panel-head"><div><h2>Diagnostic Timeline</h2><p className="muted">Application, worker, service, API, and job events share one searchable timeline. Secrets are redacted.</p></div><div className="actions"><label className="inline-choice"><input type="checkbox" checked={live} onChange={e=>setLive(e.target.checked)}/>Live</label><button disabled={busy} onClick={load}>Refresh</button><button onClick={()=>setClear(true)}>Clear</button></div></div>{clear&&<Modal onClose={()=>setClear(false)} busy={busy}><div className="modal" role="dialog" aria-modal="true" aria-label="Clear diagnostic events"><h2>Clear diagnostic events?</h2><p>Job details and results remain available.</p><div className="actions"><button onClick={async()=>{try{await api.clearLogs();setClear(false);await load()}catch(e:any){setError(e.message)}}}>Confirm clear</button><button onClick={()=>setClear(false)}>Cancel</button></div></div></Modal>}<div className="log-filters"><input type="search" placeholder="Search messages, actions, or services" aria-label="Search logs" value={query} onChange={e=>setQuery(e.target.value)}/><select aria-label="Time range" value={range} onChange={e=>setRange(e.target.value)}><option value="1">Last hour</option><option value="24">Last 24 hours</option><option value="168">Last 7 days</option><option value="">All retained</option></select><select aria-label="Log level" value={level} onChange={e=>setLevel(e.target.value)}><option value="">All levels</option>{['ERROR','WARNING','INFO',...(debug?['DEBUG']:[])].map(v=><option key={v}>{v}</option>)}</select><select aria-label="Worker" value={worker} onChange={e=>setWorker(e.target.value)}><option value="">All workers</option>{facets.workers.map((v:string)=><option key={v}>{v}</option>)}</select><select aria-label="Action" value={action} onChange={e=>setAction(e.target.value)}><option value="">All actions</option>{facets.operations.map((v:string)=><option key={v} value={v}>{actionName(v)}</option>)}</select></div>{error&&<ErrorNotice error={error}/>}<p className="muted">Showing {entries.length} newest matching events · up to 100,000 retained.{!debug?' Debug events are disabled.':''}</p><div className="log-view">{entries.map(row=><article className={`log-entry level-${String(row.level).toLowerCase()}`} key={row.id}><small>{stamp(row.created_at)} · {row.level} · {actionName(row.operation)}{row.worker?` · ${row.worker} worker`:''}{row.service?` · ${row.service}`:''}</small><pre>{displayText(row.message)}</pre>{row.job_id&&<a href={`#activity/${row.job_id}`}>Open related job →</a>}</article>)}</div>{busy&&!entries.length?<p role="status">Loading logs…</p>:!entries.length&&<p>No matching events.</p>}</section>
}
