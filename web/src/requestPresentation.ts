export type RequestState = {
 availability?:string; plex_available?:boolean; lidarr_available?:boolean;
 status?:string; active?:boolean; lidarr_id?:number; download_status?:string;
 error?:string; download_error?:string; download_poll_error?:string;
 removed_from_lidarr?:boolean; downloads?:{error?:string}[];
}
export function downloadGroup(r:RequestState){
 const state=`${r.status||''} ${r.download_status||''}`.toLowerCase()
 if(r.removed_from_lidarr||r.status==='not_in_lidarr'||/failed|unknown|blocked/.test(state)||(r.error&&r.status!=='search_pending')||r.download_error||r.downloads?.some(d=>d.error))return 'Needs Action'
 if(/cancel/.test(state))return 'Cancelled / Failed'
 if(r.availability==='Partially Available')return 'Partially Available'
 if(r.plex_available&&(r.lidarr_available||r.download_status==='Imported into Lidarr'))return 'Completed'
 return 'In Progress'
}
export function requestBadge(requests:RequestState[],admin:boolean){
 const attention=requests.filter(r=>downloadGroup(r)==='Needs Action').length
 const downloads=requests.filter(r=>downloadGroup(r)==='In Progress'&&r.downloads?.length&&r.download_status!=='Imported into Lidarr').length
 return admin&&attention?{count:attention,attention:true}:{count:downloads,attention:false}
}
