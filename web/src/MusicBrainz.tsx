import ErrorNotice from './ErrorNotice'
import {useEffect,useState} from 'react'
export type MBSettings={preferred_countries:string[];preferred_formats:string[];prefer_official:boolean;server_url:string;enabled:boolean;cache_days:number;release_priority:string[];prefer_studio:boolean;retries:number;cache_entries:number;capabilities:string[]}
export async function mbCall<T>(path:string,method='GET',body?:unknown,signal?:AbortSignal):Promise<T>{
 const controller=new AbortController(),abort=()=>controller.abort(),timer=setTimeout(abort,30000)
 if(signal?.aborted)abort();else signal?.addEventListener('abort',abort,{once:true})
 try{const r=await fetch(path,{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:controller.signal});const data=await r.json();if(!r.ok){const e=Object.assign(new Error(typeof data.detail==='string'?data.detail:'MusicBrainz request failed.'),{status:r.status,retryable:r.status===429||r.status===504||(r.status===502&&/HTTP 503|HTTP 429|Connection failed/.test(data.detail||''))});throw e}return data}finally{clearTimeout(timer);signal?.removeEventListener('abort',abort)}
}
export function mirrorSearch(body:any,settings?:MBSettings|null){
 const server=settings?.server_url?.trim().replace(/\/+$/,'')
 if(!server||server==='https://musicbrainz.org')return false
 const album=String(body.album||'').trim(),known=!!album&&!['n/a','unknown'].includes(album.toLowerCase())
 const entity=known||(body.fields_search&&!String(body.title||'').trim())?'release-group':'recording'
 return !!settings?.capabilities?.includes('search_'+entity)
}
export async function mbLookup<T>(body:unknown,retries:number,onStatus:(message:string)=>void,active:()=>boolean,settings?:MBSettings|null,onRows?:(result:T)=>void,signal?:AbortSignal,base='/api/lidarr/search-jobs'):Promise<T>{
 const isActive=()=>active()&&!signal?.aborted
 const pause=(ms:number)=>new Promise<void>((resolve,reject)=>{const abort=()=>{clearTimeout(timer);reject(new Error('Lookup cancelled'))};const timer=setTimeout(()=>{signal?.removeEventListener('abort',abort);resolve()},ms);if(signal?.aborted)abort();else signal?.addEventListener('abort',abort,{once:true})})
 const mirror=mirrorSearch(body,settings)
 if(mirror)retries=0
 for(let attempt=0;;attempt++){
  if(!isActive())throw new Error('Lookup cancelled')
  onStatus(mirror?'Searching MusicBrainz mirror…':retries?`Searching MusicBrainz · attempt ${attempt+1} of ${retries+1}…`:'Searching MusicBrainz…')
  try{
   const job=await mbCall<{id:string}>(base,'POST',{...(body as object),lookup_session:Date.now().toString(36)+Math.random().toString(36).slice(2)})
   try{
    while(true){
     if(!isActive())throw new Error('Lookup cancelled')
     const state=await mbCall<any>(base+'/'+encodeURIComponent(job.id),'GET',undefined,signal)
     if(!isActive())throw new Error('Lookup cancelled')
     if(state.result?.rows)onRows?.(state.result)
     onStatus(state.progress||'Search queued…')
     if(state.status==='completed')return state.result as T
     if(['failed','cancelled','interrupted'].includes(state.status)){const message=state.error||'Search '+state.status+'. You can search again.';throw Object.assign(new Error(message),{retryable:state.status==='failed'&&/HTTP (?:429|503|504)|timed out|Connection failed/i.test(message)})}
     await pause(1000)
    }
   }finally{if(!isActive())void mbCall(base+'/'+encodeURIComponent(job.id),'DELETE').catch(()=>{})}
  }catch(e:any){
   if(!e.retryable||attempt>=retries)throw e
   for(let seconds=15*(attempt+1);seconds>0;seconds--){if(!isActive())throw new Error('Lookup cancelled');onStatus(`MusicBrainz is busy or unavailable. Retrying in ${seconds}s · attempt ${attempt+2} of ${retries+1}`);await pause(1000)}
  }
 }
}
export default function MusicBrainzSettings(){
 const [value,setValue]=useState<MBSettings|null>(null),[busy,setBusy]=useState(false),[message,setMessage]=useState(''),[error,setError]=useState('')
 useEffect(()=>{let active=true;mbCall<MBSettings>('/api/settings/musicbrainz').then(v=>{if(active)setValue(v)}).catch(e=>{if(active)setError(e.message)});return()=>{active=false}},[])
 async function action(fn:()=>Promise<void>){setBusy(true);setError('');setMessage('');try{await fn()}catch(e:any){setError(e.message)}finally{setBusy(false)}}
 function move(index:number,delta:number){if(!value)return;const order=[...value.release_priority];[order[index],order[index+delta]]=[order[index+delta],order[index]];setValue({...value,release_priority:order})}
 return <section className="panel settings-panel"><h2>MusicBrainz</h2><p className="muted">Find albums for missing tracks. No API key is required.</p>{!value&&!error&&<p>Loading settings…</p>}{value&&<fieldset className="lidarr-fields" disabled={busy}>
 <label className="playlist-choice"><input type="checkbox" checked={value.enabled} onChange={e=>setValue({...value,enabled:e.target.checked})}/> Enable MusicBrainz lookups</label>
 <label>MusicBrainz Mirror URL <span className="muted">(optional)</span><input type="url" value={value.server_url} onChange={e=>setValue({...value,server_url:e.target.value,capabilities:[]})} placeholder="https://musicbrainz.example.net"/></label><button onClick={()=>setValue({...value,server_url:"",capabilities:[]})}>Use Public Service Only</button><p className="muted">Save the URL, then test it. Harmonia detects direct lookup, browse, relationship expansion, and indexed search capabilities. It uses only the capabilities that pass; unavailable operations use public musicbrainz.org. Leave blank to use public MusicBrainz for everything.</p>
 {value.server_url&&<p className="muted"><strong>Detected mirror capabilities:</strong> {value.capabilities?.length?value.capabilities.join(', '):'Not tested yet'}</p>}<h3>Release priority</h3><p className="muted">Applies to both Lidarr and MusicBrainz album results. Every candidate remains available for review.</p>
 {value.release_priority.map((type,i)=><div className="alias-row" key={type}><span>{i+1}. {type}</span><div><button aria-label={`Move ${type} up`} disabled={i===0} onClick={()=>move(i,-1)}>↑</button> <button aria-label={`Move ${type} down`} disabled={i===3} onClick={()=>move(i,1)}>↓</button></div></div>)}
 <label className="playlist-choice"><input type="checkbox" checked={value.prefer_studio} onChange={e=>setValue({...value,prefer_studio:e.target.checked})}/> Prefer studio releases over live, compilation and remix releases</label><p className="muted">This uses MusicBrainz labels; missing labels cannot guarantee a studio recording.</p>
 <h3>Preferred editions</h3><p>Release countries and formats rank editions within an album’s release group. These are preferences, not exclusions, and never change existing file identities. Leftmost values have highest priority.</p><div className="form-grid"><label>Countries (comma separated)<input value={value.preferred_countries.join(',')} onChange={e=>setValue({...value,preferred_countries:e.target.value.split(',')})}/><small>US = United States · XE = Europe · XW = Worldwide</small></label><label>Formats (comma separated)<input value={value.preferred_formats.join(',')} onChange={e=>setValue({...value,preferred_formats:e.target.value.split(',')})}/><small>For example: Digital Media, CD, Vinyl</small></label></div><label className="playlist-choice"><input type="checkbox" checked={value.prefer_official} onChange={e=>setValue({...value,prefer_official:e.target.checked})}/>Prefer official releases</label>
 <h3>Lookups and cache</h3><div className="form-grid"><label>Public service automatic retries<select value={value.retries} onChange={e=>setValue({...value,retries:Number(e.target.value)})}>{[0,1,2,3].map(n=><option key={n} value={n}>{n===0?'Off':`${n} retries`}</option>)}</select></label><label>Keep cached results<select value={value.cache_days} onChange={e=>setValue({...value,cache_days:Number(e.target.value)})}><option value={7}>7 days</option><option value={30}>30 days</option></select></label></div>
 <p className="muted">Mirror searches make one attempt, with public fallback if needed. Public service retries wait 15, 30, then 45 seconds. Retry Once always makes one attempt, without automatic retries. {value.cache_entries} cached lookups.</p>
 <div className="actions"><button onClick={()=>action(async()=>{setMessage('Testing MusicBrainz · one request…');const r=await mbCall<{message:string;capabilities:string[]}>('/api/settings/musicbrainz/test','POST');setValue(v=>v?{...v,capabilities:r.capabilities}:v);setMessage(r.message)})}>Test Connection</button><button onClick={()=>action(async()=>{const r=await mbCall<MBSettings>('/api/settings/musicbrainz/cache','DELETE');setValue(v=>v?{...v,cache_entries:r.cache_entries}:v);setMessage('Metadata cache cleared.')})}>Clear Cache</button></div>
 <div className="settings-save"><button className="primary" onClick={()=>action(async()=>{setValue(await mbCall<MBSettings>('/api/settings/musicbrainz','PUT',value));setMessage('MusicBrainz settings saved.')})}>Save Changes</button></div>
 </fieldset>}{message&&<p role="status">{message}</p>}{error&&<ErrorNotice error={error}/>}</section>
}
