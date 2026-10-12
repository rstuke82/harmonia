import {useEffect,useState} from 'react'
import Modal from './Modal'
import {activityBadgeCount} from './jobActivity'
import {Job} from './api'
import {Icon} from './Controls'
import ActivityNav from './ActivityNav'
import Brand from './Brand'
import {getAccount} from './Auth'
import {useLidarrRequests} from './LidarrRequests'

import {requestBadge} from './requestPresentation'

type Props={jobs:Job[];page:string;admin:boolean;health:any}
const primary=['discover','library','playlists','requests']
export const destinations=(admin:boolean)=>{const user=getAccount();return ['discover','library',...(admin||user?.can_library_issues?['issues']:[]),'playlists',...(user?.lidarr_enabled!==false?['requests']:[]),...(admin&&user?.lidarr_enabled!==false?['missing']:[]),'blocklist']}
const label=(p:string)=>p==='issues'?'Issues':p==='settings/users'?'Users':p[0].toUpperCase()+p.slice(1)
const selected=(p:string,page:string)=>p==='playlists'?['playlists','playlist'].includes(page):p==='settings/users'?location.hash.startsWith('#settings/users'):p==='settings'?page==='settings'&&!location.hash.startsWith('#settings/users'):p===page
function Destination({to,page,onNavigate,badge=0,attention=false}:{to:string;page:string;onNavigate?:()=>void;badge?:number;attention?:boolean}){const active=selected(to,page);return <button className={`${active?'active':''} ${attention?'nav-attention':''}`} aria-current={active?'page':undefined} onClick={()=>{onNavigate?.();if(to==='library')window.dispatchEvent(new Event('library-home'));location.hash=to}}><Icon name={to==='issues'?'missing':to==='settings/users'?'users':to}/><span>{label(to)}</span>{badge>0&&<b className="nav-count" aria-label={`${badge} ${attention?'requests need attention':'active downloads'}`}>{badge}</b>}</button>}
export const secondaryDestinations=(admin:boolean)=>['activity',...destinations(admin).filter(p=>!primary.includes(p)),...(admin?['settings/users']:[]),'settings']
function useRequestBadge(){return requestBadge(useLidarrRequests(),!!getAccount()?.admin)}
export function DesktopNavigation({jobs,page,admin,health}:Props){const badge=useRequestBadge();return <aside className="app-sidebar"><a className="brand harmonia-home" href="#discover" aria-label="Harmonia home"><Brand wordmark/></a><nav aria-label="Main navigation">{destinations(admin).map(p=><Destination key={p} to={p} page={page} badge={p==='requests'?badge.count:0} attention={p==='requests'&&badge.attention}/>)}</nav><div className="sidebar-bottom"><ActivityNav jobs={jobs} page={page}/><nav aria-label={admin?"Administration":"Settings"}>{admin&&<Destination to="settings/users" page={page}/>}<Destination to="settings" page={page}/></nav><div className="sidebar-version">{health.release_name||'Harmonia'}{health.update?.available&&<a className="update-badge" href="#settings/about">Update available ↗</a>}</div></div></aside>}
export function MobileNavigation({jobs,page,admin}:Props){
 const badge=useRequestBadge(),[open,setOpen]=useState(false)
 const queued=activityBadgeCount(jobs),close=()=>setOpen(false)
 const moreSelected=secondaryDestinations(admin).some(p=>selected(p,page))
 useEffect(()=>{close()},[page])
 useEffect(()=>{const changed=()=>close();window.addEventListener('hashchange',changed);const desktop=matchMedia('(min-width: 761px)');const resized=()=>{if(desktop.matches)close()};desktop.addEventListener('change',resized);return()=>{window.removeEventListener('hashchange',changed);desktop.removeEventListener('change',resized)}},[])
 return <><nav className="mobile-nav glass-tabbar" aria-label="Main navigation">{primary.filter(p=>p!=='requests'||getAccount()?.lidarr_enabled!==false).map(p=><Destination key={p} to={p} page={page} badge={p==='requests'?badge.count:0} attention={p==='requests'&&badge.attention}/>)}<button className={`mobile-more ${moreSelected?'active':''}`} aria-label={queued?`More, ${queued} queued job${queued===1?'':'s'}`:'More'} aria-haspopup="dialog" aria-expanded={open} aria-controls={open?'mobile-more-sheet':undefined} onClick={()=>setOpen(true)}><Icon name="more"/><span>More</span>{queued>0&&<b className="nav-count queued-count" aria-label={`${queued} queued job${queued===1?'':'s'}`}>{queued}</b>}</button></nav>{open&&<Modal onClose={close}><section className="mobile-more-sheet" id="mobile-more-sheet" role="dialog" aria-modal="true" aria-labelledby="mobile-more-title"><div className="mobile-more-heading"><h2 id="mobile-more-title">More</h2><button className="icon-button" onClick={close} aria-label="Close More menu"><span aria-hidden="true">×</span></button></div><nav aria-label="More navigation" onClick={close}><ActivityNav jobs={jobs} page={page} mobile/>{secondaryDestinations(admin).filter(p=>p!=='activity').map(p=><Destination key={p} to={p} page={page} onNavigate={close}/>)}</nav></section></Modal>}</>
}
