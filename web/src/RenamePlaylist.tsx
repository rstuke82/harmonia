import {useState} from 'react'
import {api,Playlist} from './api'
import Modal from './Modal'
import ErrorNotice from './ErrorNotice'
export default function RenamePlaylist({playlist}:{playlist:Playlist}){
 const [open,setOpen]=useState(false),[name,setName]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('')
 async function save(restore=false){setBusy(true);setError('');try{const result=await api.updatePlaylist(playlist.key,restore?{restore_source_name:true}:{name});setOpen(false);window.dispatchEvent(new Event('jobs-refresh'));window.dispatchEvent(new CustomEvent('bridge-notice',{detail:{message:'Rename queued. Your source playlist will not change.',job_id:result.job_id||result.id}}))}catch(e:any){setError(e.message)}finally{setBusy(false)}}
 return <><button className="small-button" onClick={()=>{setName(playlist.name);setError('');setOpen(true)}}>Rename Playlist</button>{open&&<Modal onClose={()=>setOpen(false)} busy={busy}><section className="modal" role="dialog" aria-modal="true" aria-label="Rename playlist"><h2>Rename Playlist</h2><p>Updates the name in Harmonia and Plex. Your source playlist is unchanged, and syncs keep your custom name.</p><label>Playlist name<input value={name} maxLength={200} onChange={e=>setName(e.target.value)}/></label>{playlist.source_name&&<p>Source name: {playlist.source_name}</p>}{error&&<ErrorNotice error={error}/>}<div className="actions"><button disabled={busy} onClick={()=>setOpen(false)}>Cancel</button>{playlist.custom_name&&playlist.source_name&&<button disabled={busy} onClick={()=>save(true)}>Use Source Name</button>}<button disabled={busy||!name.trim()} onClick={()=>save()}>Save Name</button></div></section></Modal>}</>
}
