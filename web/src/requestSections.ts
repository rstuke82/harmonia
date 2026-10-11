export const requestStatuses=['Needs Action','In Progress','Partially Available','Completed','Cancelled / Failed'] as const
export function requestSections<T>(rows:T[],status:(row:T)=>string){
 return requestStatuses.map(name=>({name,rows:rows.filter(row=>status(row)===name)})).filter(section=>section.rows.length>0)
}

export const requestBatchSize=25
export function requestPage<T>(rows:T[],page:number,size=requestBatchSize){
 size=[5,10,25,50,100].includes(size)?size:requestBatchSize
 const pages=Math.max(1,Math.ceil(rows.length/size))
 const current=Math.min(Math.max(0,page),pages-1)
 return {rows:rows.slice(current*size,(current+1)*size),page:current,pages}
}
export function reuseRequestRows<T extends {key:string}>(previous:T[],next:T[]):T[]{
 const byKey=new Map(previous.map(row=>[row.key,row]))
 const merged=next.map(row=>{const old=byKey.get(row.key);return old&&JSON.stringify(old)===JSON.stringify(row)?old:row})
 return previous.length===merged.length&&merged.every((row,index)=>row===previous[index])?previous:merged
}
