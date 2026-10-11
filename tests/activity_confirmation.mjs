import assert from 'node:assert/strict'
import fs from 'node:fs'
import ts from '../web/node_modules/typescript/lib/typescript.js'
const source=fs.readFileSync(new URL('../web/src/activityNotice.ts',import.meta.url),'utf8')
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText
const {activityConfirmation:notice}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))
assert.deepEqual(notice('/api/jobs',{id:'sync-id',action:'sync',status:'queued'}),{job_id:'sync-id',message:'Playlist sync queued.'})
assert.equal(notice('/api/library/file-audit',{id:'scan',action:'file_audit',status:'running'}).message,'Local file scan started.')
assert.equal(notice('/api/lidarr/requests/x/finalize',{id:'finalize',status:'queued'}).job_id,'finalize')
assert.equal(notice('/api/action',{job_id:'nested',message:'Refresh queued.'}).message,'Refresh queued.')
assert.equal(notice('/api/action',{job:{id:'nested',action:'backup',status:'queued'}}).message,'Backup queued.')
assert.equal(notice('/api/action',{jobs:[{id:'first'},{id:'second'}]}).job_id,'first')
assert.equal(notice('/api/jobs/x/cancel',{id:'cancel',action:'sync',status:'cancelled'}).message,'Playlist sync cancelled.')
assert.equal(notice('/api/lidarr/preview',{preview_id:'not-a-job',id:'album'}),null)
assert.equal(notice('/api/account/api-key',{id:'credential',status:'queued',key:'secret'}),null)
assert.equal(notice('/api/auth/poll',{setup_libraries:[{key:'7',title:'Music'}]}),null)
assert.equal(notice('/api/settings',{configured:true}),null)
console.log('Activity confirmation checks passed')
