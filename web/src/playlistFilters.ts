import { Playlist } from './api'
export const FILTER_STORAGE='bridge-playlist-filters'
export const playlistPredicates:Record<string,(p:Playlist)=>boolean>={
 'Ready to Sync':p=>!!p.ready_to_sync,
 'Needs Attention':p=>!!p.health_attempt?.error||!!p.health&&!p.health.healthy||p.unresolved>0||p.lost>0,
 '100% Matched':p=>!!p.fully_matched,
 'Has Manual Matches':p=>(p.match_counts?.manual||0)>0,
 'Server Schedule':p=>!p.schedule||p.schedule.mode==='inherit','Custom Schedule':p=>p.schedule?.mode==='custom','Manual Only':p=>p.schedule?.mode==='disabled',
 'Never Synced':p=>!p.last_synced,
}
export function openPlaylistView(filters:string[]=[]){
 const view={query:'',filters:normalizePlaylistFilters(filters),sort:'name'}
 try{localStorage.setItem(FILTER_STORAGE,JSON.stringify(view))}catch{}
 // The route carries intent even if browser storage is unavailable.
 location.hash=`playlists/view/${encodeURIComponent(JSON.stringify(view))}`
}

export function normalizePlaylistFilters(filters:string[]){return [...new Set(filters.map(f=>['Health Drift','Health Errors','Has Missing','Has LOST','Unhealthy'].includes(f)?'Needs Attention':f))].filter(f=>f.replace(/^!/, '') in playlistPredicates)}

export function playlistSyncSelection(rows:Pick<Playlist,'key'>[],selected:string[],filters:string[],query:string){
 const visible=new Set(rows.map(p=>p.key)),chosen=selected.filter(key=>visible.has(key))
 return {keys:chosen.length?chosen:rows.map(p=>p.key),label:chosen.length?'Sync selected':filters.length||query.trim()?'Sync filtered':'Sync all'}
}
