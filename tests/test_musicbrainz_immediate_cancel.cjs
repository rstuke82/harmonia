const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('../web/node_modules/typescript');
const source=ts.transpileModule(fs.readFileSync('web/src/MusicBrainz.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
(async()=>{
 for(const base of [undefined,'/api/requests-user/search-jobs']) {
 let resolveDelete,posting=0,notifyGet;const started=new Promise(resolve=>notifyGet=resolve),calls=[];
 const env={exports:{},require:()=>({}),AbortController,setTimeout,clearTimeout,fetch:async(path,options)=>{
  calls.push(options.method);
  assert(path.startsWith(base || '/api/lidarr/search-jobs'));
  if(options.method==='POST')return {ok:true,json:async()=>({id:'job'+ ++posting})};
  if(options.method==='DELETE')return new Promise(resolve=>resolveDelete=()=>resolve({ok:true,json:async()=>({cancelled:true})}));
  if(path.endsWith('job2'))return {ok:true,json:async()=>({status:'completed',result:{rows:[]}})};
  notifyGet();return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true}));
 }};
 vm.runInNewContext(source,env);
 const controller=new AbortController();let active=true;
 const first=env.exports.mbLookup({artist:'Artist',title:'Song'},0,()=>{},()=>active,null,undefined,controller.signal,base);
 await started;active=false;controller.abort();
 await assert.rejects(first,/aborted|cancelled/);
 assert(calls.includes('DELETE'),'Cancellation must reach the server immediately');
 const second=await env.exports.mbLookup({artist:'Artist',title:'Song'},0,()=>{},()=>true,null,undefined,undefined,base);
 assert.equal(second.rows.length,0);assert.equal(posting,2,'A new lookup must start before the old DELETE completes');
 resolveDelete();
 }
 console.log('Immediate polling abort, server cancellation and replacement search before cancellation acknowledgement passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
