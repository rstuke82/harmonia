import {useEffect,useState} from 'react'
import {request} from './api'
export default function ImportUsers({done,embedded=false}:{done:()=>void;embedded?:boolean}){
 const [rows,setRows]=useState<any[]|null>(null),[selected,setSelected]=useState<string[]>([]),[busy,setBusy]=useState(false),[message,setMessage]=useState('')
 async function run(fn:()=>Promise<void>){setBusy(true);setMessage('');try{await fn()}catch(e:any){setMessage(e.message)}finally{setBusy(false)}}
 async function load(){await run(async()=>{setRows(await request<any[]>('/api/users/import'));setSelected([])})}
 useEffect(()=>{if(embedded)void load()},[embedded])
 return <section className="import-users">{!embedded&&<button disabled={busy} onClick={load}>Import Plex Users</button>}{busy&&!rows&&<p role="status">Loading Plex users…</p>}{rows&&<><p>Select accounts with access to the music library. Each person still signs in through Plex.</p><div className="import-user-list">{rows.map(u=><label key={u.id}><input type="checkbox" disabled={busy||u.existing} checked={selected.includes(u.id)} onChange={e=>setSelected(e.target.checked?[...selected,u.id]:selected.filter(id=>id!==u.id))}/><span>{u.avatar&&<img src={u.avatar} alt=""/>}<strong>{u.name}</strong>{u.existing&&<small>Already imported</small>}</span></label>)}</div><button className="primary" disabled={busy||!selected.length} onClick={()=>run(async()=>{const result=await request<any>('/api/users/import',{method:'POST',body:JSON.stringify({ids:selected})});setMessage(`${result.imported} users imported`);setRows(null);done()})}>Import Selected</button></>}{message&&<p role="status">{message}</p>}</section>
}
