type ProgressRecord={status:string;active?:boolean;finalize_active?:boolean;download_status?:string;lidarr_available?:boolean;downloads?:{percent?:number|null}[]}
export function requestProgress(r:ProgressRecord):[number,number]{
 const step=r.finalize_active?5:r.download_status==='Imported into Lidarr'?4:r.downloads?.length?3:/search/.test(r.status)?2:r.lidarr_available?1:0
 const percentages=(r.downloads||[]).map(d=>d.percent).filter((n):n is number=>typeof n==='number'&&Number.isFinite(n))
 return [step,percentages.length?Math.max(0,Math.min(100,Math.max(...percentages))):0]
}
export function compareProgress(a:ProgressRecord,b:ProgressRecord){const ap=requestProgress(a),bp=requestProgress(b);return bp[0]-ap[0]||bp[1]-ap[1]}
export function compareRequestPriority(a:ProgressRecord,b:ProgressRecord,status:string,aGroup:string,bGroup:string){
 const ai=aGroup==='In Progress',bi=bGroup==='In Progress'
 if(status==='All'&&ai!==bi)return ai?-1:1
 if((status==='All'||status==='In Progress')&&ai&&bi)return compareProgress(a,b)
 return 0
}
