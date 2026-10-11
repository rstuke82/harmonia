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
function useRequestBadge(){return requestBadge(useLidarrRequests(),!!getAccount()?.admin)}
export function DesktopNavigation({jobs,page,admin,health}:Props){const badge=useRequestBadge();return <aside className="app-sidebar"><a className="brand harmonia-home" href="#discover" aria-label="Harmonia home"><Brand wordmark/></a><nav aria-label="Main navigation">{destinations(admin).map(p=><Destination key={p} to={p} page={page} badge={p==='requests'?badge.count:0} attention={p==='requests'&&badge.attention}/>)}</nav><div className="sidebar-bottom"><ActivityNav jobs={jobs} page={page}/><nav aria-label="Administration">{admin&&<Destination to="settings/users" page={page}/>}{admin&&<Destination to="settings" page={page}/>}</nav><div className="sidebar-version">{health.release_name||'Harmonia'}{health.update?.available&&<a className="update-badge" href="#settings/about">Update available ↗</a>}</div></div></aside>}
export function MobileNavigation({jobs,page,admin,health}:Props){const badge=useRequestBadge();return <nav className="mobile-nav glass-tabbar" aria-label="Main navigation">{primary.filter(p=>p!=='requests'||getAccount()?.lidarr_enabled!==false).map(p=><Destination key={p} to={p} page={page} badge={p==='requests'?badge.count:0} attention={p==='requests'&&badge.attention}/>)}<ActivityNav jobs={jobs} page={page} mobile/></nav>}
