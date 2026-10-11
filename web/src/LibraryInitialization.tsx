import {useEffect,useState} from 'react'
import {useAccount} from './Auth'
import {changedData} from './api'

export default function LibraryInitialization(){
 const user=useAccount(),[job,setJob]=useState<any>(null)
 useEffect(()=>{
  if(!user?.owner)return
  let stopped=false,seenIncomplete=false,timer:ReturnType<typeof setTimeout>
  async function poll(){
   if(stopped)return
   try{
    const response=await fetch('/api/account/initial-library')
    if(!response.ok)throw Error('Library status unavailable')
    const result=await response.json()
    if(stopped)return
    setJob(result.job)
    if(!result.job)return
    if(result.job.status!=='completed')seenIncomplete=true
    if(result.job.status==='completed'){
     if(seenIncomplete){
      changedData('/api/library')
      window.dispatchEvent(new Event('jobs-refresh'))
      for(const path of ['/api/library/recently-added','/api/library?view=directory&limit=2000'])window.dispatchEvent(new CustomEvent('saved-view-updated',{detail:{path}}))
     }
     return
    }
   }catch{if(stopped)return}
   timer=setTimeout(poll,3000)
  }
  void poll();return()=>{stopped=true;clearTimeout(timer)}
 },[user?.id,user?.owner])
 if(!job||job.status==='completed')return null
 const active=['queued','running','cancelling'].includes(job.status)
 return <section className={active?'notice':'warning-notice'} role="status"><strong>{active?'Preparing your music library':'Initial library scan needs attention'}</strong><p>{active?'Your first Plex scan is running in the background. Albums will appear as saved library data becomes ready.':'Open the scan in Jobs for its error details. You can retry the Plex Library Scan from Settings → Tasks.'}</p>{active&&job.progress&&<p className="muted">{job.progress}</p>}<a href={`#activity/${job.id}`}>View library scan</a></section>
}
