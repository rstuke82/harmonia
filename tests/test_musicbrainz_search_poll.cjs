const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('../web/node_modules/typescript');
const source=ts.transpileModule(fs.readFileSync('web/src/MusicBrainz.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
async function run(cancel){
 const calls=[],status=[],partial=[],module={exports:{}};let active=true,count=0;
 const sandbox={module,exports:module.exports,require:()=>({}),AbortController,setTimeout:(fn,ms)=>ms===1000?setTimeout(fn,0):0,clearTimeout:()=>{},fetch:async(path,opts={})=>{
  calls.push([path,opts.method||'GET']);let data;
  if(opts.method==='POST')data={id:'job'};
  else if(opts.method==='DELETE')data={cancelled:true};
  else{count++;data=count===1?{status:'running',progress:'Finding albums',result:{rows:[{album_id:'first'}]}}:{status:'completed',result:{rows:[{album_id:'first'},{album_id:'second'}]}};if(cancel)active=false}
  return {ok:true,json:async()=>data};
 }};
 vm.runInNewContext(source,sandbox);
 const task=module.exports.mbLookup({title:'Song',artist:'Artist',provider:'musicbrainz'},0,m=>status.push(m),()=>active,null,r=>partial.push(r.rows.length));
 if(cancel){await assert.rejects(task,/cancelled/);assert(calls.some(c=>c[1]==='DELETE'));assert.equal(partial.length,0)}
 else{const result=await task;assert.equal(result.rows.length,2);assert.deepEqual(partial,[1,2]);assert.equal(calls.filter(c=>c[1]==='POST').length,1);assert(status.includes('Finding albums'))}
}
(async()=>{await run(false);await run(true);console.log('Background lookup polling, incremental results and cancellation passed.')})().catch(e=>{console.error(e);process.exitCode=1});
