import { useEffect, useState } from 'react'
import { api } from './api'
import ErrorNotice from './ErrorNotice'
export default function ConsoleSettings(){
 const [debug,setDebug]=useState(false),[loaded,setLoaded]=useState(false),[busy,setBusy]=useState(false),[message,setMessage]=useState(''),[error,setError]=useState('')
 async function load(){setError('');await api.consoleSettings().then(s=>{setDebug(s.debug);setLoaded(true)}).catch(e=>setError(e.message))}
 useEffect(()=>{void load()},[])
 return <section className="panel settings-panel"><h2>Debug logging</h2><label className="playlist-choice"><input type="checkbox" checked={debug} disabled={!loaded||busy} onChange={async e=>{const next=e.target.checked;setBusy(true);setError('');setMessage('');try{await api.saveConsole(next);setDebug(next);setMessage('Debug logging updated.')}catch(e:any){setError(e.message)}finally{setBusy(false)}}}/> Save DEBUG events and show them in the server console</label><p className="muted">Off by default. Enable detailed matching diagnostics and successful request logs while troubleshooting. Errors, summaries, and normal job progress remain available.</p>{error&&<ErrorNotice error={error}/>} {!loaded&&error&&<button onClick={load}>Retry loading settings</button>} {!loaded&&!error&&<p role="status">Loading logging settings…</p>}{message&&<p role="status">{message}</p>}</section>
}
