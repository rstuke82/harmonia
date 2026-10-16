// Page reads are separate from jobs: showing progress never navigates away.
export type PageRead = {id:number;path:string;route:string;label:string;started:number}
export type ReadFailure = {path:string;route:string;message:string}
let serial=0,epoch=0
let snapshot:{reads:PageRead[];failure:ReadFailure|null}={reads:[],failure:null}
const listeners=new Set<()=>void>(),seen=new Set<string>()
const publish=()=>{for(const listener of listeners)listener()}
export const subscribeReads=(listener:()=>void)=>{listeners.add(listener);return()=>{listeners.delete(listener)}}
export const pageReads=()=>snapshot
export const readEpoch=()=>epoch
export const resetReads=()=>{epoch++;seen.clear();snapshot={reads:[],failure:null};publish()}
export const dismissReadFailure=()=>{snapshot={...snapshot,failure:null};publish()}
export function failRead(path:string,route:string,message:string){snapshot={...snapshot,failure:{path,route,message}};publish()}
function name(path:string){
 const url=new URL(path,location.origin),p=url.pathname
 if(/^\/api\/playlists(?:\/|$)/.test(p)&&!p.endsWith('/health'))return p==='/api/playlists'?'playlists':'playlist details'
 if(/^\/api\/library(?:\/|$)/.test(p))return 'Library'
 if(/^\/api\/discover(?:\/|$)/.test(p))return p.includes('history-gaps')?'listening history':'Discover'
 if(p==='/api/missing')return 'missing tracks'
 if(p==='/api/lidarr/requests'||p.startsWith('/api/requests-user/'))return 'requests'
 if(p==='/api/search')return 'search results'
 if(p.startsWith('/api/account/matches'))return 'manual matches'
 return ''
}
export function beginRead(path:string,method='GET',refresh=false){
 const noop=Object.assign(()=>{},{update:(_label:string)=>{}})
 if(!['GET','HEAD'].includes(method.toUpperCase()))return noop
 const label=name(path);if(!label)return noop
 const url=new URL(path,location.origin);url.searchParams.delete('refresh');url.searchParams.delete('progress_id')
 const key=url.pathname+url.search,id=++serial,stamp=epoch
 const row:PageRead={id,path:key,route:location.hash||'#discover',label:`${refresh||seen.has(key)?'Refreshing':'Loading'} ${label}…`,started:Date.now()}
 snapshot={reads:[...snapshot.reads,row],failure:snapshot.failure?.route===row.route?null:snapshot.failure};publish()
 return Object.assign(()=>{
  if(stamp!==epoch)return
  seen.add(key);if(seen.size>256)seen.delete(seen.values().next().value!);snapshot={...snapshot,reads:snapshot.reads.filter(value=>value.id!==id)};publish()
 },{update:(label:string)=>{
  if(stamp!==epoch||!label)return
  snapshot={...snapshot,reads:snapshot.reads.map(value=>value.id===id?{...value,label}:value)};publish()
 }})
}
