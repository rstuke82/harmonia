import assert from 'node:assert/strict'
import fs from 'node:fs'
import ts from '../web/node_modules/typescript/lib/typescript.js'
const source=fs.readFileSync(new URL('../web/src/playlistFilters.ts',import.meta.url),'utf8')
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText
const {playlistSyncSelection}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))
const rows=[{key:'one'},{key:'two'}]
assert.deepEqual(playlistSyncSelection(rows,[],[],''),{keys:['one','two'],label:'Sync all'})
assert.deepEqual(playlistSyncSelection(rows,[],['Ready to Sync'],''),{keys:['one','two'],label:'Sync filtered'})
assert.deepEqual(playlistSyncSelection(rows,[],[],'Apple'),{keys:['one','two'],label:'Sync filtered'})
assert.deepEqual(playlistSyncSelection(rows,[],[],'   '),{keys:['one','two'],label:'Sync all'})
assert.deepEqual(playlistSyncSelection(rows,['two'],[],''),{keys:['two'],label:'Sync selected'})
assert.deepEqual(playlistSyncSelection([rows[0]],['two'],['Ready to Sync'],''),{keys:['one'],label:'Sync filtered'})
assert.deepEqual(playlistSyncSelection([],['two'],[],''),{keys:[],label:'Sync all'})
console.log('Playlist sync label and target checks passed')
