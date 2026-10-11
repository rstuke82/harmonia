const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('../web/node_modules/typescript');
const env={exports:{}};vm.runInNewContext(ts.transpileModule(fs.readFileSync('web/src/albumSearchResults.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,env);
const rows=[{title:'Compilation',type:'Album',secondary_types:['Compilation'],other_release:true},{title:'Single',type:'Single',secondary_types:[],other_release:false},{title:'Studio',type:'Album',secondary_types:[],other_release:false},{title:'Unrelated',type:'Album',secondary_types:[],other_release:true}];
assert.equal(env.exports.albumSearchResults(rows,false).map(r=>r.title).join(','),'Studio,Single');
const all=env.exports.albumSearchResults(rows,true);assert.equal(all.map(r=>r.title).join(','),'Studio,Single,Unrelated,Compilation');assert.equal(all.length,4);assert.equal(rows[0].title,'Compilation');
console.log('Artist studio releases first, other releases hidden until expanded, combined page ordering and immutable input passed.');
