import {useEffect,useState} from 'react'
import {api} from './api'
import ErrorNotice from './ErrorNotice'
export default function PlaylistScheduleTasks({revision}:{revision:string}){
 const [rows,setRows]=useState<any[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState('')
 useEffect(()=>{fetch('/api/playlist-schedules').then(async r=>{if(!r.ok)throw new Error('Could not load playlist schedules');setRows(await r.json())}).catch(e=>setError(e.message))},[revision])
 return <details className="panel"><summary>Playlist schedules ({rows.filter(r=>r.mode==='custom').length} custom)</summary>{error&&<ErrorNotice error={error}/ >}{rows.map(r=><article className="playlist-schedule-row" key={r.key}><a href={`#playlist/${encodeURIComponent(r.key)}`}>{r.name}</a><span>{r.mode==='custom'?`${r.days.map((d:number)=>['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][d]).join(', ')} at ${String(r.hour).padStart(2,'0')}:${String(r.minute).padStart(2,'0')} (${r.timezone})`:'Manual only'}</span><span>Next: {r.next_run?new Date(r.next_run).toLocaleString():'—'}<small>{r.last_event}</small>{r.last_job&&<a href={`#activity/${r.last_job}`}>Last scheduled job</a>}</span><button disabled={busy===r.key} onClick={async()=>{setBusy(r.key);try{await api.enqueue('sync',{scope:'selected',playlist_keys:[r.key]});window.dispatchEvent(new Event('jobs-refresh'))}catch(e:any){setError(e.message)}finally{setBusy('')}}}>Run Now</button></article>)}{!rows.length&&<p>Custom schedules can be configured in Playlist Details.</p>}</details>
}
