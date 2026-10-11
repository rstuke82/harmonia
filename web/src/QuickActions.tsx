import ErrorNotice from './ErrorNotice'
import { useState } from 'react'
import { Job } from './api'
import Modal from './Modal'
import { Icon } from './Controls'
const choices=[
 {id:'sync-all',name:'Sync All Now',action:'sync',payload:{scope:'all'}},
 {id:'health',name:'Health Check',action:'health',payload:{scope:'all'}},
 {id:'backup',name:'Back Up Now',action:'backup',payload:{}},
 {id:'updates',name:'Check for Updates',action:'check_updates',payload:{}},
]
const defaults=['sync-all','health','backup','updates']
function read(){try{const saved=JSON.parse(localStorage.getItem('bridge-quick-actions')||'null');return Array.isArray(saved)?Array.from(new Set(saved.filter((id:string)=>choices.some(c=>c.id===id)))).slice(0,5) as string[]:defaults}catch{return defaults}}
export default function QuickActions({enqueue}:{enqueue:(action:string,payload:any)=>Promise<Job>}){
 const [selected,setSelected]=useState<string[]>(read),[draft,setDraft]=useState<string[]>([]),[editing,setEditing]=useState(false),[error,setError]=useState(''),[pending,setPending]=useState('')
 function move(index:number,delta:number){const next=[...draft];[next[index],next[index+delta]]=[next[index+delta],next[index]];setDraft(next)}
 return <section className="panel"><div className="panel-head"><h2>Quick Actions</h2><button className="icon-button" title="Edit quick actions" aria-label="Edit quick actions" onClick={()=>{setDraft([...selected]);setEditing(true);setError('')}}><Icon name="edit"/></button></div><div className="actions wrap">{selected.map(id=>{const c=choices.find(c=>c.id===id)!;return <button key={id} disabled={!!pending} onClick={async()=>{setPending(id);setError('');try{await enqueue(c.action,c.payload)}catch(e:any){setError(e.message)}finally{setPending('')}}}>{pending===id?'Queuing…':c.name}</button>})}</div>{!selected.length&&<p className="muted">Use the pencil to choose your quick actions.</p>}{error&&!editing&&<ErrorNotice error={error}/>}{editing&&<Modal onClose={()=>setEditing(false)}><div className="modal" role="dialog" aria-modal="true" aria-label="Edit quick actions"><h2>Quick Actions</h2><p>Choose up to five. Your selection and order are saved in this browser.</p>{choices.map(c=><label className="playlist-choice" key={c.id}><input type="checkbox" checked={draft.includes(c.id)} disabled={!draft.includes(c.id)&&draft.length>=5} onChange={e=>setDraft(e.target.checked?[...draft,c.id]:draft.filter(id=>id!==c.id))}/>{c.name}</label>)}{!!draft.length&&<><h3>Display order</h3>{draft.map((id,i)=><div className="quick-order" key={id}><span>{choices.find(c=>c.id===id)?.name}</span><button aria-label={`Move ${choices.find(c=>c.id===id)?.name} up`} disabled={i===0} onClick={()=>move(i,-1)}>↑</button><button aria-label={`Move ${choices.find(c=>c.id===id)?.name} down`} disabled={i===draft.length-1} onClick={()=>move(i,1)}>↓</button></div>)}</>}{error&&<ErrorNotice error={error}/>}<div className="actions"><button onClick={()=>setEditing(false)}>Cancel</button><button className="primary" onClick={()=>{try{localStorage.setItem('bridge-quick-actions',JSON.stringify(draft));setSelected(draft);setEditing(false)}catch{setError('Your browser could not save this preference.')}}}>Save Changes</button></div></div></Modal>}</section>
}
