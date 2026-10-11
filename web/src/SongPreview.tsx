import {RequestAlbumLabel} from './Auth'
import ErrorNotice from './ErrorNotice'
import { useEffect, useId, useRef, useState } from 'react'
import { api } from './api'
import {AddLidarrAlbum} from './Lidarr'
export default function SongPreview({track}:{track:{title:string;artist:string;album?:string}}){
 const [open,setOpen]=useState(false),[rows,setRows]=useState<any[]|null>(null),[selected,setSelected]=useState<any>(null),[busy,setBusy]=useState(false),[error,setError]=useState('')
 const [lidarr,setLidarr]=useState<any>(null)
 const generation=useRef(0),panel=useRef<HTMLDivElement>(null),id=useId()
 useEffect(()=>{generation.current++;setOpen(false);setRows(null);setSelected(null);setBusy(false);setError('');return()=>{generation.current++}},[track.title,track.artist,track.album])
 async function find(){const current=++generation.current;setBusy(true);setError('');try{const result=await api.applePreview(track);if(current===generation.current)setRows(result.rows)}catch(e:any){if(current===generation.current)setError(e.message)}finally{if(current===generation.current)setBusy(false)}}
 function close(){generation.current++;setOpen(false);setSelected(null);setBusy(false)}
 return <section className="song-preview">{lidarr&&<AddLidarrAlbum track={lidarr} sourceTrack={track} onClose={()=>setLidarr(null)}/>}<button type="button" aria-expanded={open} aria-controls={id} onClick={()=>{if(open)close();else{setOpen(true);if(!rows)void find()}}}>{open?'Close Song Preview':'Preview Song'}</button>
 {open&&<div id={id} className="song-preview-panel" ref={panel}><p className="muted">Choose a recording to hear. Listening does not change your match.</p>{busy&&<p role="status">Finding song previews…</p>}{error&&<ErrorNotice error={error}/>}
 {selected&&<div className="song-preview-player"><iframe key={selected.id} title={`Apple Music preview: ${selected.title}`} src={selected.embed_url} allow="encrypted-media" sandbox="allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox"/><p><a href={selected.store_url} target="_blank" rel="noreferrer">Open {selected.title} on Apple Music ↗</a> · <button type="button" className="small-button" onClick={()=>setSelected(null)}>Stop Preview</button> <button type="button" disabled={!selected.album} onClick={()=>setLidarr({...selected})}><RequestAlbumLabel/></button></p></div>}
 {rows&&!rows.length&&<p>No Apple catalog previews found.</p>}
 <div className="song-preview-choices">{rows?.map(r=><button type="button" className={`candidate ${selected?.id===r.id?'chosen':''}`} key={r.id} onClick={()=>{setSelected(r);panel.current?.scrollIntoView({block:'nearest'})}} aria-pressed={selected?.id===r.id}><span><strong>{r.title}</strong><small>{r.artist} · {r.album}</small></span><span>Preview</span></button>)}</div>
 <button type="button" className="small-button" disabled={busy} onClick={find}>Retry Preview Search</button></div>}
 </section>
}
