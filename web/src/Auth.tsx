import {beginRead,resetReads} from './readActivity'
import {activityConfirmation} from './activityNotice'
import {Icon} from './Controls'
import {createContext,useContext,useEffect,useRef,useState} from 'react'
import App from './App'
const Account=createContext<any>(null)
export const useAccount=()=>useContext(Account)
let csrf=''
let currentAccount:any=null
export const getAccount=()=>currentAccount
let sessionGeneration=0
const sessionRequests=new Set<AbortController>()
function resetSessionRequests(){resetReads();sessionGeneration++;for(const controller of sessionRequests)controller.abort();sessionRequests.clear()}
function acceptAccount(user:any){
 const previous=localStorage.getItem('bridge-account-id')
 currentAccount=user
 if(previous!==String(user?.id||'')){
  for(const key of Object.keys(localStorage)){if((key.startsWith('bridge-')||key.startsWith('harmonia-'))&&key!=='harmonia-appearance')localStorage.removeItem(key)}
  sessionStorage.clear()
 }
 if(user)localStorage.setItem('bridge-account-id',String(user.id))
 else localStorage.removeItem('bridge-account-id')
}
const originalFetch=window.fetch.bind(window)
window.fetch=async(input,init)=>{
 const url=new URL(typeof input==='string'?input:input instanceof URL?input.href:input.url,location.href)
 if(url.origin!==location.origin||!url.pathname.startsWith('/api/'))return originalFetch(input,init)
 const headers=new Headers(init?.headers||(input instanceof Request?input.headers:undefined))
 const method=init?.method||(input instanceof Request?input.method:'GET')
 const tracked=headers.has('X-Harmonia-Read-Tracked');headers.delete('X-Harmonia-Read-Tracked')
 const finish=tracked?()=>{}:beginRead(url.pathname+url.search,method)
 if(csrf)headers.set('X-Bridge-CSRF',csrf)
 else headers.delete('X-Bridge-CSRF')
 const controller=new AbortController(),signal=init?.signal||(input instanceof Request?input.signal:undefined),generation=sessionGeneration
 const abort=()=>controller.abort()
 if(signal?.aborted)abort();else signal?.addEventListener('abort',abort,{once:true})
 sessionRequests.add(controller)
 try{
  const response=await originalFetch(input,{...init,headers,signal:controller.signal})
  if(generation!==sessionGeneration)throw new DOMException('Session changed','AbortError')
  if(response.status===401&&!url.pathname.startsWith('/api/auth/'))window.dispatchEvent(new Event('session-expired'))
  // Body decoding can finish after the headers and after a session transition.
  for(const method of ['json','text','blob','arrayBuffer','formData'] as const){
   const read=response[method].bind(response)
   response[method]=async()=>{
    if(generation!==sessionGeneration)throw new DOMException('Session changed','AbortError')
    const value=await read()
    if(generation!==sessionGeneration)throw new DOMException('Session changed','AbortError')
    return value
   }
  }
  if(response.ok&&!['GET','HEAD','OPTIONS'].includes(method.toUpperCase())&&response.headers.get('Content-Type')?.includes('application/json')){
   const result=await response.clone().json().catch(()=>null)
   if(generation===sessionGeneration){const notice=activityConfirmation(url.pathname,result);if(notice)window.dispatchEvent(new CustomEvent('bridge-notice',{detail:notice}))}
  }
  return response
 }finally{sessionRequests.delete(controller);finish()}
}
export default function Auth(){
 const [user,setUser]=useState<any>(null),[loading,setLoading]=useState(true),[error,setError]=useState(''),[waiting,setWaiting]=useState(false),[setup,setSetup]=useState(false),[keyRequired,setKeyRequired]=useState(false),[setupKey,setSetupKey]=useState(''),[libraries,setLibraries]=useState<{key:string;title:string}[]>([]),[libraryKey,setLibraryKey]=useState('')
 const timer=useRef<ReturnType<typeof setTimeout>|undefined>(undefined),alive=useRef(true),refreshVersion=useRef(0),session=useRef<string|null>(null)
 function applySession(d:any){
  const token=d.csrf||'',next=JSON.stringify([d.user||null,token]),changed=session.current!==null&&session.current!==next
  csrf=token;acceptAccount(d.user||null);setSetup(!!d.setup_required);setKeyRequired(!!d.setup_key_required);if(d.user)setSetupKey('');setWaiting(false);clearTimeout(timer.current)
  session.current=next
  if(changed){
   resetSessionRequests();setUser(null);setLoading(!!d.user)
   // A new document drops every module cache and late response from the old account.
   location.reload();return
  }
  setUser(d.user||null);setLoading(false)
 }
 useEffect(()=>{
  alive.current=true
  const refresh=async()=>{
   const version=++refreshVersion.current
   try{const response=await fetch('/api/auth/me');if(!response.ok)throw Error('Could not connect to Harmonia.');const d=await response.json();if(alive.current&&version===refreshVersion.current){applySession(d);setError('')}}
   catch(e:any){if(alive.current&&version===refreshVersion.current){setError('Could not connect to Harmonia.');setLoading(false)}}
  }
  const expired=()=>{refreshVersion.current++;applySession({user:null,csrf:''})}
  void refresh();window.addEventListener('lidarr-config-changed',refresh);window.addEventListener('focus',refresh);window.addEventListener('session-expired',expired)
  return()=>{alive.current=false;refreshVersion.current++;clearTimeout(timer.current);window.removeEventListener('lidarr-config-changed',refresh);window.removeEventListener('focus',refresh);window.removeEventListener('session-expired',expired)}
 },[])
 async function login(){
  setError('');setWaiting(true)
  const popup=window.open('about:blank','plex-login','width=700,height=750')
  try{const r=await fetch('/api/auth/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({setup_key:keyRequired?setupKey:''})});const d=await r.json();if(!r.ok)throw Error(d.detail);if(popup)popup.location.href=d.url;else throw Error('Allow pop-ups to sign in with Plex.')
   const deadline=Date.now()+600000
   const poll=async()=>{if(!alive.current)return;try{if(Date.now()>deadline)throw Error('Sign-in expired. Try again.');const r=await fetch('/api/auth/poll');const d=await r.json();if(r.status===429){timer.current=setTimeout(poll,Math.max(2,Math.min(60,Number(r.headers.get('Retry-After'))||2))*1000);return}if(!r.ok)throw Error(d.detail);if(d.setup_libraries){setLibraries(d.setup_libraries);setLibraryKey(d.setup_libraries[0]?.key||'');setWaiting(false);popup.close();return}if(d.user){refreshVersion.current++;applySession(d);popup.close();return}timer.current=setTimeout(poll,2000)}catch(e:any){setError(e.message);setWaiting(false)}}
   timer.current=setTimeout(poll,2000)
  }catch(e:any){popup?.close();setError(e.message);setWaiting(false)}
 }
 async function chooseLibrary(){setWaiting(true);setError('');try{for(let attempt=0;attempt<3;attempt++){const r=await fetch('/api/auth/poll',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({library_key:libraryKey})});const d=await r.json();if(r.status===429||d.pending){await new Promise(resolve=>setTimeout(resolve,Math.max(2,Math.min(60,Number(r.headers.get('Retry-After'))||2))*1000));continue}if(!r.ok)throw Error(d.detail);if(!d.user)throw Error('Plex authorization is incomplete. Restart sign-in.');refreshVersion.current++;applySession(d);return}throw Error('Plex is busy. Try selecting your library again.')}catch(e:any){setError(e.message);setWaiting(false)}}
 if(loading)return <main><p>Loading Harmonia…</p></main>
 if(!user)return <main className="login-screen"><div className="harmonia-login-layout"><div className="harmonia-welcome-art"><img src="/harmonia-wordmark.png" alt="Harmonia"/></div><section className="login-card"><span className="login-kicker">{setup?'First-time setup':'Welcome back'}</span><h1>{setup?'Connect your Plex music library':'Your music, connected.'}</h1><p>{setup?'The Plex server owner signs in first. Harmonia will securely discover the owned server and available music libraries. Choose one to create the administrator account.':'Sign in with the Plex account that has access to this server’s music library.'}</p>{keyRequired&&<label>Setup key<input type="password" autoComplete="off" value={setupKey} disabled={waiting} maxLength={200} onChange={e=>setSetupKey(e.target.value)} aria-describedby="setup-key-help"/><span className="muted" id="setup-key-help">Enter the setup key from your container startup logs. It is required only for first-time setup.</span></label>}{setup&&<ol className="setup-steps"><li>Authorize Harmonia with Plex</li><li>Discover your owned Plex server</li><li>Choose your music library</li><li>Create the first administrator and scan your library</li></ol>}{error&&<p className="error" role="alert">{error}</p>}{libraries.length===0&&<button disabled={waiting||(keyRequired&&!setupKey.trim())} className="plex-login-button" onClick={login}><svg className="plex-signin-mark" viewBox="0 0 24 32" aria-hidden="true"><path fill="currentColor" d="M2 0h9l11 16-11 16H2l11-16z"/></svg><span>{waiting?'Waiting for Plex…':'Sign in with Plex'}</span></button>}{libraries.length>0&&<form onSubmit={e=>{e.preventDefault();void chooseLibrary()}}><label>Plex music library<select value={libraryKey} disabled={waiting} onChange={e=>setLibraryKey(e.target.value)}>{libraries.map(l=><option key={l.key} value={l.key}>{l.title}</option>)}</select></label><button className="primary" disabled={waiting||!libraryKey}>{waiting?'Connecting…':'Use This Library'}</button><button type="button" disabled={waiting} onClick={()=>{setLibraries([]);setLibraryKey('')}}>Restart Sign-in</button></form>}<p className="muted">Managed Plex users are not supported. Harmonia stores the server token only in its private data volume.</p></section></div></main>
 return <Account.Provider value={user}><App/></Account.Provider>
}
async function signOut(){await fetch('/api/auth/logout',{method:'POST'});sessionStorage.clear();location.hash='discover';location.reload()}
export function AccountButton(){const user=useAccount(),menu=useRef<HTMLDetailsElement>(null);const close=()=>{if(menu.current)menu.current.open=false};return <details className="profile-menu" ref={menu}><summary className="account-avatar-button" title="Your account" aria-label={`Open account menu for ${user?.name||'your account'}`}>{user?.avatar?<img src={user.avatar} alt="" referrerPolicy="no-referrer"/>:<Icon name="account"/>}</summary><nav className="profile-menu-popover" aria-label="Account menu"><a href="#account" onClick={close}>Account</a><a href="#account/appearance" onClick={close}>Appearance</a><button onClick={()=>{close();void signOut()}}>Sign out</button></nav></details>}
async function apiKeyCall(path:string,method='GET',payload?:unknown){
 const response=await fetch(path,{method,headers:{'Content-Type':'application/json'},body:payload===undefined?undefined:JSON.stringify(payload)}),body=await response.json().catch(()=>({}))
 if(!response.ok)throw Error(body.detail||'API access could not be updated.')
 return body
}
function ApiAccess(){
 const user=useAccount(),[data,setData]=useState<any>(null),[secret,setSecret]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false),[copied,setCopied]=useState(''),[permissions,setPermissions]=useState<string[]>(['read']),[saved,setSaved]=useState(''),base=location.origin
 useEffect(()=>{let live=true;apiKeyCall('/api/account/api-key').then(async status=>{if(!live)return;if(status.configured){setData(status);setPermissions(status.scopes||['read']);return}const created=await apiKeyCall('/api/account/api-key','POST');if(live){setData(created);setPermissions(created.scopes||['read']);setSecret(created.key||'')}}).catch(e=>live&&setError(e.message));return()=>{live=false}},[])
 async function regenerate(){if(!confirm('Regenerate your API key? The current key will stop working immediately.'))return;setBusy(true);try{const value=await apiKeyCall('/api/account/api-key/regenerate','POST');setData(value);setPermissions(value.scopes||['read']);setSecret(value.key||'');setCopied('');setError('')}catch(e:any){setError(e.message)}finally{setBusy(false)}}
 useEffect(()=>{if(!copied)return;const timer=setTimeout(()=>setCopied(''),3000);return()=>clearTimeout(timer)},[copied])
 async function copy(value:string,label:string){setCopied('');try{await navigator.clipboard.writeText(value);setError('');setCopied(label)}catch{setError('Clipboard access is unavailable. Select and copy the value manually.')}}
 async function savePermissions(){setBusy(true);setSaved('');try{const value=await apiKeyCall('/api/account/api-key/scopes','PUT',{scopes:permissions});setData(value);setPermissions(value.scopes);setSaved('Permissions saved. Your existing key uses these permissions immediately.');setError('')}catch(e:any){setError(e.message)}finally{setBusy(false)}}
 const shown=secret||data?.prefix||'Creating key…'
 return <section className="panel api-access"><div className="panel-head"><div><h2>API Access</h2><p className="muted">Keys start read-only and never expire. Regenerating a key immediately disables the previous key. Administrator settings require a browser session.</p></div><button disabled={busy||!data?.configured} onClick={regenerate}>Regenerate Key</button></div>{error&&<p role="alert">{error}</p>}<label>API key<div className="api-copy-row"><input readOnly value={shown} onFocus={e=>e.currentTarget.select()} aria-label="Harmonia API key"/><button disabled={!secret} onClick={()=>copy(secret,'API key')}>{copied==='API key'?'Copied ✓':'Copy'}</button></div></label>{secret?<p className="warning-notice">Copy this key now. For security, the complete value will not be shown again.</p>:data?.configured&&<p className="muted">Created {data.created_at?new Date(data.created_at*1000).toLocaleString():'previously'}{data.last_used_at?` · Last used ${new Date(data.last_used_at*1000).toLocaleString()}`:''}. Regenerate it if you no longer have the full value.</p>}<p role="status" aria-live="polite">{copied?`${copied} copied to clipboard.`:''}</p><fieldset disabled={busy||!data?.configured}><legend>API permissions</legend><p className="muted">Read access is always enabled. Grant only what your automation needs.</p>{[['playlists:write','Share and create playlists'],['requests:write','Request albums'],...(user?.admin?[['webhooks:lidarr','Receive Lidarr webhooks']]:[])].map(([value,label])=><label className="inline-choice" key={value}><input type="checkbox" checked={permissions.includes(value)} onChange={e=>{setSaved('');setPermissions(v=>e.target.checked?[...v,value]:v.filter(s=>s!==value))}}/>{label}</label>)}<button type="button" onClick={savePermissions}>Save Permissions</button><p role="status" aria-live="polite">{saved}</p></fieldset><div className="api-guide-grid"><article><h3>iPhone or iPad Shortcut</h3><ol><li>Create a Shortcut that accepts URLs from the Share Sheet.</li><li>Add <strong>Get Contents of URL</strong> using <code>{base}/api/v1/share</code>.</li><li>Choose POST with a JSON body containing <code>url</code> set to <strong>Shortcut Input</strong>.</li><li>Add header <code>Authorization</code> with value <code>Bearer YOUR_KEY</code>.</li></ol><p>Enable Share and create playlists above first. Sharing a public Apple Music or Spotify playlist queues it for your account and uses the server sync schedule.</p><button onClick={()=>copy(base+'/api/v1/share','Share endpoint')}>{copied==='Share endpoint'?'Copied ✓':'Copy Share Endpoint'}</button></article>{user?.admin&&<article><h3>Lidarr Webhook</h3><p>Enable Receive Lidarr webhooks above first. In Lidarr, add a Webhook Connection using POST and this URL:</p><div className="api-copy-row"><input readOnly value={base+'/api/v1/webhooks/lidarr'} onFocus={e=>e.currentTarget.select()}/><button onClick={()=>copy(base+'/api/v1/webhooks/lidarr','Webhook URL')}>{copied==='Webhook URL'?'Copied ✓':'Copy'}</button></div><p>Add the advanced header <code>Authorization</code> with value <code>Bearer YOUR_KEY</code>. Enable Import, Upgrade, Rename, Track Retag, Artist Added/Deleted, and Album Deleted.</p></article>}</div><p><a href="/docs" target="_blank" rel="noreferrer">Open API documentation ↗</a></p></section>
}
export function AccountSettings(){const user=useAccount();return <><section className="panel account-settings"><div className="account-profile">{user?.avatar?<img src={user.avatar} alt="" referrerPolicy="no-referrer"/>:<span className="account-avatar-fallback"><Icon name="account"/></span>}<div><h2>{user?.name||'Account'}</h2><p className="muted">Signed in with Plex{user?.admin?' · Administrator':''}</p></div></div><button onClick={signOut}>Sign out</button></section><section className="panel"><h2>Manual Matches</h2><p>Review your saved track selections across playlists.</p><a className="button-link" href="#account/matches">View Manual Matches</a></section><ApiAccess/></>}

export function RequestAlbumLabel(){return <>Request Album</>}

export function CanRequest({children}:{children:any}){const user=useAccount();return user?.lidarr_enabled!==false&&(user?.admin||user?.can_request)?children:null}

export function CanPlaylists({children}:{children:any}){const user=useAccount();return user?.admin||user?.can_playlists!==false?children:null}
