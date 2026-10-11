/** User-initiated job responses share one confirmation, including direct fetch callers. */
export function activityConfirmation(path:string,result:any){
 if(path.startsWith('/api/auth/')||path.startsWith('/api/account/api-key'))return null
 const job=result?.job||result
 const jobs=Array.isArray(result?.jobs)?result.jobs:[]
 const id=job?.job_id||((job?.action||['queued','running','cancelling'].includes(job?.status))?job.id:undefined)||jobs[0]?.id
 if(!id)return null
 const names:Record<string,string>={sync:'Playlist sync',health:'Playlist health check',plex_scan:'Plex library scan',lidarr_scan:'Lidarr library scan',file_audit:'Local file scan',metadata_refresh:'Metadata refresh',library_metadata:'Artist metadata refresh',library_metadata_all:'Library metadata refresh',file_audit_duplicates:'Duplicate verification',album_finalize:'Album refresh',lidarr_add:'Album request',lidarr_search:'Album search',backup:'Backup',finalize_import_album:'Album refresh'}
 const action=names[job.action]||String(job.action||'Activity').replaceAll('_',' ').replace(/^./,c=>c.toUpperCase())
 const message=result.message||(job.status==='cancelling'?`${action}: cancellation requested.`:job.status==='cancelled'?`${action} cancelled.`:`${action} ${job.status==='running'?'started':'queued'}.`)
 return {job_id:String(id),message}
}
