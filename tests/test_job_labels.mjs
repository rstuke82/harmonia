import assert from 'node:assert/strict'
import fs from 'node:fs'
import ts from '../web/node_modules/typescript/lib/typescript.js'
const source=fs.readFileSync(new URL('../web/src/jobLabels.ts',import.meta.url),'utf8')
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText
const {actionName}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))
const jobs=fs.readFileSync(new URL('../harmonia/jobs.py',import.meta.url),'utf8')
const actions=jobs.match(/ACTIONS\s*=\s*\{([\s\S]*?)\}/)[1].match(/'([^']+)'/g).map(v=>v.slice(1,-1))
for(const action of actions){assert.notEqual(actionName(action),action);assert.equal(actionName(action).includes('_'),false)}
assert.equal(actionName('lastfm_refresh'),'Refresh Last.fm Discovery')
assert.equal(actionName('historical_unknown_job'),'Historical Unknown Job')
assert.equal(actionName('/api/library/plex-albums/{id}/image'),'/api/library/plex-albums/{id}/image')
console.log(`Friendly labels passed for ${actions.length} registered job actions`)
