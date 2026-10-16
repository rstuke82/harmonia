import {beginRead,failRead,readEpoch} from './readActivity'
export type Job = {
  worker?:string;owner_id?:string; owner_name?:string;activity?:any;id:string;action:string;status:string;progress:string;error?:string;payload:any;result:any;created_at:string;started_at?:string;finished_at?:string}
export const activityJob = (job:Job) => !['prepare_views','request_status'].includes(job.action)
export const activeJob = (job:Job) => ['queued','running','cancelling'].includes(job.status)
export type Playlist = {
  key: string
  name: string
  source: string
  source_url: string
  settings_pending?: boolean; ready_to_sync?: boolean; schedule?: {mode:string;next_run?:string}; source_name?:string; source_owner?:string; custom_name?:string
  added_at?: string | null
  last_synced?: string | null
  health?: PlaylistHealth
  health_attempt?: {attempted_at:string; error:string|null}
  match_counts?: {automatic:number;manual:number;legacy:number}
  fully_matched?: boolean
  saved_matches: number
  unresolved: number
  lost: number
  ignored: number
}

export type PlaylistHealth = {
  key: string
  name: string
  source: string
  source_tracks: number
  plex_playlist_tracks: number
  matched_in_library: number
  unresolved: number
  ignored: number
  missing_from_plex_playlist: number
  extra_in_plex_playlist: number
  source_added_since_last_sync: number
  source_removed_since_last_sync: number
  healthy: boolean
  checked_at: string
  drift_details?: Record<string, any[]>
  read_only: boolean
}

export type PlexSettings = {
  url: string
  token_configured: boolean
  token_hint: string
  music_library_key: string
  music_library_name: string
  connected?: boolean
}

export type PlexLibrary = {
  key: string
  name: string
}

export type MissingTrack = {
  user_id?:string
  user_name?:string
  title: string
  artist: string
  album?: string
  duration?: number
  occurrence_count: number
  playlist_count: number
  lost_occurrence_count?: number
  playlists: string[]
  last_checked?: string | null
  manual_matches_elsewhere?: {key:string;name:string}[]
  memberships: {key:string;name:string;count:number;last_checked?:string;source?:string;favorite?:boolean;statuses?:string[]}[]
}

export type Candidate = {
  track_artist?:string; album_artist?:string; track_number?:number; disc_number?:number; edition?:string; other_entries?:Candidate[]
  plex_id: string
  title: string
  artist: string
  album: string
  score: number
  duration?: number
  source_duration?: number
  duration_delta?: number
  title_score: number
  artist_credit_reason?: string
  artist_score: number
  album_score?: number | null
  album_penalty: number
  title_variant_penalty: number
  release_intent_penalty: number
}

let generation=0
export const readGeneration=()=>generation
export function savedPath(path:string){const url=new URL(path,location.origin);url.searchParams.delete('refresh');url.searchParams.delete('progress_id');url.searchParams.sort();return url.pathname+url.search}
export function changedData(path:string){
 if(!/^\/api\/(library|discover|requests-user|lidarr|settings|account)/.test(path))return
 generation++
 window.dispatchEvent(new CustomEvent('saved-data-changed',{detail:{path,generation}}))
}
const trackedHeaders={'Content-Type':'application/json','X-Harmonia-Read-Tracked':'true'}
async function readPhase(response:Response,finish:ReturnType<typeof beginRead>,signal:AbortSignal){
 const id=response.headers.get('X-Saved-View-Job');if(!id)return
 const controller=new AbortController(),abort=()=>controller.abort()
 if(signal.aborted)abort();else signal.addEventListener('abort',abort,{once:true})
 const timer=setTimeout(abort,5000)
 try{
  const state=await fetch('/api/jobs/'+encodeURIComponent(id),{headers:trackedHeaders,signal:controller.signal})
  if(!state.ok)return
  const job=await state.json()
  if(['failed','cancelled','interrupted'].includes(job.status))return true
  if(job.status==='queued')finish.update(job.progress||'Waiting for page preparation…')
  else if(job.status==='running')finish.update(job.progress||'Preparing page data…')
  else if(job.status==='cancelling')finish.update('Page preparation is stopping…')
 }catch{}finally{clearTimeout(timer);signal.removeEventListener('abort',abort)}
}
const watching = new Map<string,{epoch:number;timer:ReturnType<typeof setTimeout>;finish:ReturnType<typeof beginRead>}>()
function watchSaved(path:string,updated:string){
 const query=new URL(path,location.origin);query.searchParams.delete('refresh');query.searchParams.delete('progress_id');path=query.pathname+query.search
 const epoch=readEpoch(),existing=watching.get(path)
 if(existing?.epoch===epoch)return
 if(existing){clearTimeout(existing.timer);existing.finish();watching.delete(path)}
 const route=location.hash||'#discover',finish=beginRead(path,'GET',true)
 let count=0
 const stop=()=>{finish();if(watching.get(path)===watch)watching.delete(path)}
 const poll=async()=>{
  if(readEpoch()!==epoch||document.hidden){stop();return}
  if(++count>100){stop();failRead(path,route,'Your last loaded data is still shown. Retry when the server is ready.');return}
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),30000)
  try{
   const response=await fetch(path,{headers:trackedHeaders,signal:controller.signal})
   if(readEpoch()!==epoch){stop();return}
   if(!response.ok)throw Error('Refresh unavailable')
   if(response.headers.get('X-Saved-View-Updated')!==updated&&!response.headers.get('X-Saved-View-Pending')){
    stop();window.dispatchEvent(new CustomEvent('saved-view-updated',{detail:{path}}));return
   }
   if(response.headers.get('X-Saved-View-Stale')!=='true'){stop();return}
   if(await readPhase(response,finish,controller.signal))throw Error('Page preparation stopped')
   if(readEpoch()!==epoch){stop();return}
   watch.timer=setTimeout(poll,3000)
  }catch{
   stop();if(readEpoch()===epoch)failRead(path,route,'Your last loaded data is still shown. Check the connection and retry.')
  }finally{clearTimeout(timer)}
 }
 const watch={epoch,timer:setTimeout(poll,2000),finish}
 watching.set(path,watch)
}
const pending = new Map<string, Promise<any>>()
async function send<T>(path:string, init?:RequestInit):Promise<T> {
  const finish=beginRead(path,init?.method||'GET')
  const controller = new AbortController()
  const timer = setTimeout(()=>controller.abort(), path.startsWith('/api/discover')||path.startsWith('/api/requests-user/')||path.startsWith('/api/library')||path.startsWith('/api/playlists')||path==='/api/lidarr/requests' ? 120000 : path.includes('/detail?') ? 65000 : 30000)
  let preparing=false
  const readURL=new URL(path,location.origin);readURL.searchParams.delete('refresh');readURL.searchParams.delete('progress_id');const pollPath=readURL.pathname+readURL.search
  try {
    let response = await fetch(path, {...init, signal:controller.signal,
      headers:{...trackedHeaders,...init?.headers}})
    let lastPhase=0
    while(response.headers.get('X-Saved-View-Pending')==='true'){
      preparing=true
      if(!lastPhase)finish.update('Preparing page data…')
      if(Date.now()-lastPhase>=3000){if(await readPhase(response,finish,controller.signal))throw new Error('Page preparation stopped. Refresh this page to retry or open Jobs for details.');lastPhase=Date.now()}
      await new Promise(resolve=>setTimeout(resolve,1000))
      if(controller.signal.aborted)throw new DOMException('Saved view is still preparing','AbortError')
      response=await fetch(pollPath,{...init,signal:controller.signal,headers:{...trackedHeaders,...init?.headers}})
    }
    if(response.headers.get('X-Saved-View-Stale')==='true')watchSaved(path,response.headers.get('X-Saved-View-Updated')||'0')
    if (!response.ok) {
      const body = await response.json().catch(()=>({}))
      throw new Error(typeof body.detail === 'string' ? body.detail : `Request failed (${response.status}). Check Settings → Logs.`)
    }
    const result = await response.json()
    if (init?.method && init.method !== 'GET') {changedData(path);window.dispatchEvent(new Event('jobs-refresh'))}
    return result
  } catch (error:any) {
    if (error.name === 'AbortError'&&preparing) {watchSaved(pollPath,'0');throw new Error('Saved data is still preparing in Activity. You can keep browsing and return shortly.')}
    if (error.name === 'AbortError') throw new Error(path==='/api/requests-user/add'?'Request timed out. Check Requests before trying again.':'Request timed out. Check service connectivity and retry.')
    if(error instanceof TypeError)throw new Error('Connection to Harmonia was interrupted. Retry when the connection returns.')
    throw error
  } finally { clearTimeout(timer);finish() }
}
export function request<T>(path:string, init?:RequestInit):Promise<T> {
  if (init?.method && init.method !== 'GET') return send<T>(path,init)
  // Share in-flight reads, including React StrictMode's setup/cleanup/setup cycle.
  const stamp=generation,key=path+'@'+stamp
  const existing = pending.get(key)
  if (existing) return existing
  const promise = send<T>(path,init).then(value=>stamp===generation?value:request<T>(path,init),error=>{if(stamp!==generation)return request<T>(path,init);throw error}).finally(()=>pending.delete(key))
  pending.set(key,promise)
  return promise
}

export const api = {
  applePreview:(track:any)=>request<any>('/api/tracks/apple-preview',{method:'POST',body:JSON.stringify(track)}),
  stageMatch:(data:any)=>request<any>('/api/matches/queue',{method:'POST',body:JSON.stringify(data)}),
  stageMatchPreview:(preview_id:string)=>request<any>('/api/matches/queue/preview',{method:'POST',body:JSON.stringify({preview_id})}),
  matchQueue:()=>request<any>('/api/matches/queue'),
  applyMatchQueue:()=>request<Job>('/api/matches/queue/apply',{method:'POST'}),
  discardMatch:(id:string)=>request<any>(`/api/matches/queue/${id}`,{method:'DELETE'}),
  consoleSettings:()=>request<{debug:boolean}>('/api/settings/console'),
  saveConsole:(debug:boolean)=>request<{debug:boolean}>('/api/settings/console',{method:'PUT',body:JSON.stringify({debug})}),
  saveStartupViews:(startup:string)=>request<any>('/api/tasks/startup-views',{method:'PUT',body:JSON.stringify({startup})}),
  tasks:()=>request<any>('/api/tasks'),
  restoreTaskDefaults:(action='')=>request<any>(`/api/tasks/restore-defaults${action?`?action=${encodeURIComponent(action)}`:''}`,{method:'POST'}),
  history:(offset=0)=>request<any>(`/api/job-history?offset=${offset}`),
  autoSync:(playlist_keys:string[],auto_sync:boolean)=>request<any>('/api/playlists/auto-sync',{method:'POST',body:JSON.stringify({playlist_keys,auto_sync})}),
  backups:()=>request<any>('/api/backups'),
  backupSettings:(retention:number)=>request<any>('/api/backups/settings',{method:'PUT',body:JSON.stringify({retention})}),
  restoreBackup:(name:string)=>request<Job>('/api/backups/restore',{method:'POST',body:JSON.stringify({name})}),
  database:(check=false)=>request<any>(`/api/database${check?'?check=true':''}`),
  clearDatabaseCache:(group:string)=>request<any>('/api/database/cache',{method:'POST',body:JSON.stringify({group})}),
  cleanupDatabase:(job_days:number,log_days:number)=>request<Job>('/api/database/cleanup',{method:'POST',body:JSON.stringify({job_days,log_days})}),
  factoryReset:(confirmation:string)=>request<any>('/api/database/factory-reset',{method:'POST',body:JSON.stringify({confirmation})}),
  matching:()=>request<any>('/api/settings/matching'),
  saveAlias:(canonical:string,aliases:string[])=>request<any>('/api/settings/matching/alias',{method:'PUT',body:JSON.stringify({canonical,aliases})}),
  trackDetails: (track:any)=>request<any>('/api/tracks/details',{method:'POST',body:JSON.stringify(track)}),
  trackPreview: (data:any)=>request<any>('/api/tracks/preview',{method:'POST',body:JSON.stringify(data)}),
  updates: ()=>request<any>('/api/updates'),
  updateSettings: (enabled:boolean)=>request<any>('/api/settings/updates',{method:'PUT',body:JSON.stringify({enabled})}),
  checkUpdates: ()=>request<any>('/api/updates/check',{method:'POST'}),
  live: (id:string,after=0) => request<{job:Job;events:any[]}>(`/api/jobs/${id}/live?after=${after}`),
  jobLog: async (id:string) => {const response=await fetch(`/api/jobs/${id}/log`);if(!response.ok)throw new Error('Could not load log');return response.text()},
  job: (id:string) => request<Job>(`/api/jobs/${id}`),
  events: (id:string,after=0) => request<any[]>(`/api/jobs/${id}/events?after=${after}`),
  removePlaylist: (key:string) => request<any>(`/api/playlists/${encodeURIComponent(key)}`,{method:'DELETE'}),
  recreatePlaylist: (key:string) => request<Job>(`/api/playlists/${encodeURIComponent(key)}/recreate`,{method:'POST',body:JSON.stringify({confirmed:true})}),
  automatic: (track:any,userId?:string) => request<any>('/api/missing/automatic'+(userId?'?user_id='+encodeURIComponent(userId):''),{method:'POST',body:JSON.stringify(track)}),
  jobs: () => request<Job[]>('/api/jobs'),
  enqueue: async(action:string,payload:any={}) => {
    const bytes=crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128
    const hex=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('')
    const submission_id=[hex.slice(0,8),hex.slice(8,12),hex.slice(12,16),hex.slice(16,20),hex.slice(20)].join('-')
    const accepted=(job:Job)=>{if(action==='sync')window.dispatchEvent(new CustomEvent('bridge-sync-started',{detail:{job_id:job.id,count:payload.scope==='selected'&&Array.isArray(payload.playlist_keys)?new Set(payload.playlist_keys).size:null,status:job.status}}));return job}
    try{return accepted(await request<Job>('/api/jobs',{method:'POST',body:JSON.stringify({action,payload,submission_id})}))}
    catch(e:any){
      if(!/timed out|Failed to fetch|NetworkError|Load failed/i.test(e.message))throw e
      // Read only: a lost response must never trigger a second mutation.
      for(let attempt=0;attempt<2;attempt++){
        if(attempt)await new Promise(resolve=>setTimeout(resolve,1500))
        try{return accepted(await request<Job>('/api/jobs/submission/'+submission_id))}catch{}
      }
      throw new Error('Could not confirm whether this activity was queued. Check Activity when the connection returns before submitting again.')
    }
  },
  cancel: (id:string) => request<Job>(`/api/jobs/${id}/cancel`,{method:'POST'}),
  cancelPending: () => request<{cancelled:number;message:string}>('/api/jobs/cancel-pending',{method:'POST'}),
  schedules: () => request<any[]>('/api/schedules'),
  saveSchedule: (data:any,id?:string) => request<any>(`/api/schedules${id?`/${id}`:''}`,{method:id?'PUT':'POST',body:JSON.stringify(data)}),
  deleteSchedule: (id:string) => request<any>(`/api/schedules/${id}`,{method:'DELETE'}),
  runSchedule: (id:string) => request<Job>(`/api/schedules/${id}/run`,{method:'POST'}),
  ignore: (data:any) => request<any>('/api/missing/ignore',{method:'POST',body:JSON.stringify(data)}),
  ignored: () => request<any[]>('/api/ignored'),
  restoreIgnore: (data:any) => request<any>('/api/ignored/restore',{method:'POST',body:JSON.stringify(data)}),
  search: (q:string) => request<any>(`/api/search?q=${encodeURIComponent(q)}`),
  clearLogs: () => request<any>('/api/settings/logs',{method:'DELETE'}),
  logs: (filters:{level?:string;action?:string;worker?:string;q?:string;since?:string;job_id?:string}={}) => {
    const query=new URLSearchParams({limit:'500'});Object.entries(filters).forEach(([key,value])=>{if(value)query.set(key,value)})
    return request<{entries:any[];retention:number;debug:boolean;facets:{operations:string[];workers:string[];services:string[]}}>(`/api/settings/logs?${query}`)
  },
  detail: async (key: string, onProgress?:(progress:any)=>void) => {
    const id=globalThis.crypto?.randomUUID?.()||`${Date.now()}-${Math.random().toString(36).slice(2)}`
    let done=false
    let timer:ReturnType<typeof setTimeout>|undefined
    const poll=async()=>{
      try{const state=await request<any>(`/api/detail-progress/${id}`);if(!done)onProgress?.(state)}catch{}
      if(!done)timer=setTimeout(poll,750)
    }
    timer=setTimeout(poll,250)
    try{return await request<any>(`/api/playlists/${encodeURIComponent(key)}/detail?progress_id=${id}`)}
    finally{done=true;if(timer)clearTimeout(timer)}
  },
  health: () => request<any>('/api/health'),
  playlists: (userId?:string) => request<Playlist[]>('/api/playlists'+(userId?'?user_id='+encodeURIComponent(userId):'')),
  playlistHealth: (key: string) => request<PlaylistHealth>(`/api/playlists/${encodeURIComponent(key)}/health`, {method:'POST'}),
  updatePlaylist: (key: string, body: {name?:string;restore_source_name?:boolean}) =>
    request<any>(`/api/playlists/${encodeURIComponent(key)}`, {
      method: 'PATCH', body: JSON.stringify(body),
    }),
  analyzePlaylist: (url: string) => request<any>('/api/playlists/analyze', {
    method: 'POST', body: JSON.stringify({ url }),
  }),
  addPlaylist: (body: { url: string; favorite: boolean; settings_pending?: boolean; ready_to_sync?: boolean; auto_sync: boolean }) =>
    request<Playlist>('/api/playlists', { method: 'POST', body: JSON.stringify(body) }),
  syncAll: () => request<any>('/api/sync/all', { method: 'POST' }),
  syncFavorites: () => request<any>('/api/sync/favorites', { method: 'POST' }),
  syncOne: (key: string) => request<any>(`/api/sync/${encodeURIComponent(key)}`, { method: 'POST' }),
  missing: (scope: 'all' | 'favorites' = 'all') => request<MissingTrack[]>(`/api/missing?scope=${scope}`),
  candidates: (track: Pick<MissingTrack, 'title' | 'artist' | 'album'> & { query?: string }, userId?:string) =>
    request<Candidate[]>('/api/missing/candidates'+(userId?'?user_id='+encodeURIComponent(userId):''), { method: 'POST', body: JSON.stringify(track) }),
  saveMatch: (track: MissingTrack, plex_id: string, options: {playlist_keys?: string[]; replace_playlist_key?: string} = {}) =>
    request<Job>('/api/missing/match', {
      method: 'POST',
      body: JSON.stringify({ title: track.title, artist: track.artist, album: track.album || '', plex_id, ...options }),
    }),
  plexSettings: () => request<PlexSettings>('/api/settings/plex'),
  discoverPlex: (body: { url: string; token: string }) =>
    request<{ libraries: PlexLibrary[] }>('/api/settings/plex/discover', {
      method: 'POST', body: JSON.stringify(body),
    }),
  savePlex: (body: { url: string; token: string; music_library_key: string }) =>
    request<PlexSettings>('/api/settings/plex', {
      method: 'PUT', body: JSON.stringify(body),
    }),
}
