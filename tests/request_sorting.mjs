import assert from 'node:assert/strict'
import fs from 'node:fs'
import ts from '../web/node_modules/typescript/lib/typescript.js'
const source=fs.readFileSync(new URL('../web/src/requestSorting.ts',import.meta.url),'utf8')
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText
const {compareProgress,compareRequestPriority}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))
const imported={status:'search_completed',download_status:'Imported into Lidarr'}
const downloading={status:'search_completed',downloads:[{percent:90}]}
const waiting={status:'search_completed'}
assert(compareProgress(imported,downloading)<0)
assert(compareProgress(downloading,waiting)<0)
assert(compareProgress(downloading,{...downloading,downloads:[{percent:40}]})<0)
assert.equal(compareProgress({...downloading,downloads:[{percent:null}]},{...downloading,downloads:[{percent:0}]}),0)
assert(compareRequestPriority(waiting,imported,'All','In Progress','Completed')<0)
assert.equal(compareRequestPriority(waiting,imported,'Completed','Completed','Completed'),0)
assert(compareRequestPriority(downloading,waiting,'In Progress','In Progress','In Progress')<0)
console.log('Request progress sorting checks passed')
