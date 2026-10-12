import {formatDuration} from './trackDuration'
import ErrorNotice from './ErrorNotice'
import { useEffect, useRef, useState } from 'react'
import { api, Candidate, MissingTrack, Playlist } from './api'
import Modal from './Modal'
import SongPreview from './SongPreview'

export default function MatchPicker({track,playlistKey,onClose,onSave}:{
  track: Pick<MissingTrack,'title'|'artist'|'album'|'duration'>; playlistKey?:string;
  onClose:()=>void; onSave:(candidate:Candidate,keys?:string[],provenance?:string)=>Promise<void>
}) {
  const [candidates,setCandidates]=useState<Candidate[]>([])
  const [automatic,setAutomatic]=useState<Candidate|null>(null)
  const [provenance,setProvenance]=useState('manual')
 const [choice,setChoice]=useState<Candidate|null>(null)
  const [query,setQuery]=useState('')
  const [loading,setLoading]=useState(true)
  const [saving,setSaving]=useState(false)
  const [error,setError]=useState('')
  const [all,setAll]=useState(true)
  const [keys,setKeys]=useState<string[]>(playlistKey?[playlistKey]:[])
  const [playlists,setPlaylists]=useState<Playlist[]>([])
  const dialog=useRef<HTMLDivElement>(null)
  const generation=useRef(0)
  const savingRef=useRef(false)
  const closeRef=useRef(onClose)
  closeRef.current=onClose
  useEffect(()=>{
    let active=true
    api.playlists().then(p=>{if(active)setPlaylists(p)}).catch(()=>{})
    retryAutomatic()
    return ()=>{active=false;generation.current++}
  },[])
  async function retryAutomatic(){
    const id=++generation.current
    setLoading(true)
    try{
      const result=await api.automatic(track)
      if(id!==generation.current)return
      setAutomatic(result.candidate)
      if(result.candidate){setChoice(result.candidate);setProvenance('automatic');const rows=await api.candidates({...track,query:''});if(id===generation.current)setCandidates([result.candidate,...rows.filter(c=>c.plex_id!==result.candidate.plex_id)])}else{await search('')}
    }catch(e:any){if(id===generation.current){setError(e.message)}}
    finally{if(id===generation.current)setLoading(false)}
  }
  async function search(text:string){
    const id=++generation.current
    setLoading(true);setError('');setChoice(null);setProvenance('manual')
    try{const rows=await api.candidates({...track,query:text});if(id===generation.current)setCandidates(rows)}
    catch(e:any){if(id===generation.current){setError(e.message);setCandidates([])}}
    finally{if(id===generation.current)setLoading(false)}
  }
  async function save(){
    if(!choice||savingRef.current)return
    savingRef.current=true;setSaving(true);setError('')
    try{await onSave(choice,all?undefined:keys,provenance);onClose()}
    catch(e:any){setError(e.message)}
    finally{savingRef.current=false;setSaving(false)}
  }
  return <Modal onClose={onClose} busy={saving}>
    <div className="modal match-dialog matching-dialog" role="dialog" aria-modal="true" aria-labelledby="match-title" ref={dialog} onClick={e=>e.stopPropagation()}>
      <div className="modal-header"><h2 id="match-title">Choose a match</h2><button disabled={saving} onClick={onClose} aria-label="Close match picker">Close</button></div>
      <div className="modal-body">
        <section className="matching-source"><h3>{track.title}</h3><p>{track.artist} · {track.album||'Album unknown'}</p>
        <p className="muted">Original duration: {formatDuration(track.duration)}</p><SongPreview track={track}/></section><p className="muted">Select a candidate, then queue it for the next playlist sync. Cancel leaves the current match unchanged.</p>
        <label><input type="checkbox" checked={all} disabled={saving} onChange={e=>setAll(e.target.checked)}/> Apply to all matching missing occurrences</label>
        {!all&&playlists.map(p=><label className="playlist-choice" key={p.key}><input type="checkbox" disabled={saving||p.key===playlistKey} checked={keys.includes(p.key)} onChange={e=>setKeys(e.target.checked?[...keys,p.key]:keys.filter(k=>k!==p.key))}/>{p.name}{p.key===playlistKey?' (current playlist)':''}</label>)}
        {playlistKey&&<p className="muted">The current playlist's match will also be replaced. Apply the match queue to sync each affected playlist once.</p>}
        <form className="matching-search" onSubmit={e=>{e.preventDefault();search(query)}}><input aria-label="Search Harmonia Library" placeholder="Search Harmonia Library title, artist or album" value={query} disabled={saving} onChange={e=>setQuery(e.target.value)}/><button disabled={saving||loading}>Search</button></form>
        {error&&<ErrorNotice error={error}/>}
        {loading?<p role="status"><span className="spinner"/> Searching Harmonia Library…</p>:<>
          {!candidates.length&&!automatic&&<p>No candidates found. Try another search.</p>}
          {candidates.map(c=><div key={c.plex_id}><button type="button" className={`candidate ${choice?.plex_id===c.plex_id?'chosen':''}`} aria-pressed={choice?.plex_id===c.plex_id} disabled={saving} key={c.plex_id} onClick={()=>{setChoice(c);setProvenance(c.plex_id===automatic?.plex_id?'automatic':'manual')}}><span><strong>{c.title}</strong><small>{c.track_artist||c.artist} · {c.album||'N/A'}</small><small>{[c.edition,c.disc_number?`Disc ${c.disc_number}`:'',c.track_number?`Track ${c.track_number}`:''].filter(Boolean).join(' · ')}</small><small>Duration: {formatDuration(c.duration)}{c.duration_delta!=null?` · ${c.duration_delta.toFixed(1)}s difference from original`:''}</small>{c.other_entries?.length&&<small>{1+c.other_entries.length} Plex entries for this recording</small>}{c.artist_credit_reason&&<small>{c.artist_credit_reason}</small>}</span><b>{c.score}%{c.plex_id===automatic?.plex_id&&<small>Suggested</small>}</b></button>{!!c.other_entries?.length&&<details><summary>Other Plex entries ({c.other_entries.length})</summary>{c.other_entries.map((other,index)=><button className={`candidate ${choice?.plex_id===other.plex_id?'chosen':''}`} key={other.plex_id} disabled={saving} onClick={()=>{setChoice(other);setProvenance('manual')}}><span><strong>{other.title}</strong><small>{other.track_artist||other.artist} · {other.album} · {formatDuration(other.duration)}</small><small>Alternate entry {index+1}</small></span><b>{other.score}%</b></button>)}</details>}</div>)}
        </>}
      </div>
      <div className="modal-footer"><button disabled={saving} onClick={onClose}>Cancel</button><span role="status">{saving?'Saving to match queue…':choice?`${provenance==='automatic'?'Automatic suggestion':'Selected by you'}: ${choice.title}`:'No changes saved'}</span><button className="primary" disabled={saving||loading||!choice||(!all&&!keys.length)} onClick={save}>{saving?'Queuing…':'Queue Selected Match'}</button></div>
    </div>
  </Modal>
}
