import { useEffect, useState } from 'react'
import { api, PlexLibrary } from './api'
import ErrorNotice from './ErrorNotice'
export default function General({setMessage,refresh}:{setMessage:(m:string)=>void,refresh:()=>Promise<void>}) {
  const [url,setUrl]=useState('')
  const [token,setToken]=useState('')
  const [tokenHint,setTokenHint]=useState('')
  const [libraries,setLibraries]=useState<PlexLibrary[]>([])
  const [libraryKey,setLibraryKey]=useState('')
  const [libraryName,setLibraryName]=useState('')
  const [busy,setBusy]=useState(false)
  const [status,setStatus]=useState(''),[error,setError]=useState('')
  const [baseline,setBaseline]=useState<any>(null)
  const dirty=!!baseline&&(url!==baseline.url||libraryKey!==baseline.music_library_key||!!token)

  async function load(){setError('');
    await api.plexSettings().then(settings=>{
      setBaseline(settings)
      setUrl(settings.url||'')
      setTokenHint(settings.token_hint||'')
      setLibraryKey(settings.music_library_key||'')
      setLibraryName(settings.music_library_name||'')
    }).catch(e=>setError(e.message))
  }
  useEffect(()=>{void load()},[])

  async function discover(){
    setBusy(true);setError(''); setStatus('Connecting to Plex…')
    try{
      const result=await api.discoverPlex({url,token})
      setLibraries(result.libraries)
      if (!libraryKey && result.libraries.length===1) setLibraryKey(result.libraries[0].key)
      setStatus(`Connected. Found ${result.libraries.length} music librar${result.libraries.length===1?'y':'ies'}.`)
    }catch(e:any){setStatus('');setError(e.message)}
    finally{setBusy(false)}
  }

  async function save(){
    if(!libraryKey){setStatus('Select a music library first.');return}
    setBusy(true);setError(''); setStatus('Saving Plex settings…')
    try{
      const result=await api.savePlex({url,token,music_library_key:libraryKey})
      setToken('')
      setTokenHint(result.token_hint||'')
      setLibraryName(result.music_library_name||'')
      setStatus(`Connected to Plex · ${result.music_library_name}`)
      setBaseline(result)
      await refresh()
    }catch(e:any){setStatus('');setError(e.message)}
    finally{setBusy(false)}
  }

  return <><section className="panel settings-panel">
    <div className="panel-head"><div><h2>Plex configuration</h2><p className="muted">Configure the Plex server used by Harmonia.</p></div>{libraryName&&<span className="pill on">{libraryName}</span>}</div>
    <fieldset disabled={busy||!baseline} className="form-grid editor-fields">
      <label>Plex server URL<input value={url} onChange={e=>setUrl(e.target.value)} placeholder="http://plex-server:32400"/></label>
      <label>Plex token<input type="password" value={token} onChange={e=>setToken(e.target.value)} placeholder={tokenHint?`Leave blank to keep ${tokenHint}`:'Enter Plex token'}/></label>
      <label>Music library<select value={libraryKey} onChange={e=>setLibraryKey(e.target.value)}><option value="">{libraryName?`${libraryName} (current)`:'Discover libraries first'}</option>{libraries.map(l=><option key={l.key} value={l.key}>{l.name}</option>)}</select></label>
    </fieldset>
    <div className="settings-actions"><button disabled={busy||!baseline||!url} onClick={discover}>Test & Discover Libraries</button></div>
    {dirty&&<div className="settings-save"><button className="primary" disabled={busy||!url||!libraryKey} onClick={save}>Save Changes</button><button disabled={busy} onClick={()=>{setUrl(baseline.url||'');setLibraryKey(baseline.music_library_key||'');setToken('');setStatus('')}}>Cancel</button></div>}
    {error&&<ErrorNotice error={error}/>} {!baseline&&error&&<button onClick={load}>Retry loading settings</button>} {!baseline&&!error&&<p role="status">Loading Plex settings…</p>}{status&&<p role="status" className={status.startsWith('Connected')?'success':'muted'}>{status}</p>}
  </section></>
}
