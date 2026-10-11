import assert from 'node:assert/strict'
import fs from 'node:fs'
import ts from '../web/node_modules/typescript/lib/typescript.js'
const source=fs.readFileSync(new URL('../web/src/jobActivity.ts',import.meta.url),'utf8')
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText
const {currentJobLanes,activityBadgeCount}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))
const job={action:'match_batch',status:'running',activity:{lanes:{}}
}
for(let i=1;i<=7;i++)job.activity.lanes[i]={key:String(i),name:`Playlist ${i}`,mode:'working',playlist_index:i,playlist_total:13}
assert.equal(currentJobLanes(job)[0].playlist_index,7)
job.result={playlists:[{key:'6',ok:true}]}
assert.equal(currentJobLanes(job)[0].playlist_index,7)
assert.equal(currentJobLanes({...job,action:'health'}).length,6)
job.result.playlists.push({key:'7',ok:false})
assert.equal(currentJobLanes(job).length,0)
assert.equal(currentJobLanes({...job,activity:{lanes:{one:{key:'one',mode:'completed'},two:{key:'two',mode:'failed'}}}}).length,0)
assert.equal(activityBadgeCount([{action:'plex_scan',status:'running'}]),0)
assert.equal(activityBadgeCount([{action:'sync',status:'running'}]),1)
assert.equal(activityBadgeCount([{action:'match_batch',status:'cancelling'}]),1)
assert.equal(activityBadgeCount([{action:'plex_scan',status:'running'},{action:'lidarr_scan',status:'running'}]),2)
assert.equal(activityBadgeCount([{action:'plex_scan',status:'running'},{action:'prepare_views',status:'running'},{action:'request_status',status:'running'}]),0)
assert.equal(activityBadgeCount([{action:'sync',status:'queued'}]),0)
console.log('Activity progress and badge checks passed')
