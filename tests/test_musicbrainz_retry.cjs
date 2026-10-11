const fs=require('fs'),vm=require('vm'),assert=require('assert');
const ts=require(process.cwd()+'/web/node_modules/typescript');
const src=fs.readFileSync('web/src/MusicBrainz.tsx','utf8');
const js=ts.transpileModule(src,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.React}}).outputText;
let calls=0,queue=[];
const box={exports:{},require:()=>({}),AbortController,setTimeout:(fn,ms)=>{if(ms<=1000)queueMicrotask(fn);return 1},clearTimeout:()=>{},fetch:async(path,opts={})=>{if(opts.method==='POST'){calls++;const status=queue.shift()??200;return {ok:status===200,status,json:async()=>status===200?{id:'job'}:{detail:'MusicBrainz returned HTTP 503'}}}return {ok:true,status:200,json:async()=>({status:'completed',result:{rows:[]}})}}};
vm.runInNewContext(js,box);
(async()=>{
 queue=[502,502,200];let messages=[];await box.exports.mbLookup({},2,m=>messages.push(m),()=>true);assert.equal(calls,3);assert(messages.some(m=>m.includes('Retrying in 15s')));assert(messages.some(m=>m.includes('Retrying in 30s')));
 calls=0;queue=[502,200];await assert.rejects(box.exports.mbLookup({},0,()=>{},()=>true));assert.equal(calls,1);
 calls=0;queue=[422,200];await assert.rejects(box.exports.mbLookup({},3,()=>{},()=>true));assert.equal(calls,1);
 calls=0;queue=[502,200];let active=true;await assert.rejects(box.exports.mbLookup({},3,m=>{if(m.includes('Retrying'))active=false},()=>active));assert.equal(calls,1);
 console.log('4 retry checks passed: automatic count, manual single attempt, nonretryable error, closed-dialog cancellation');
})().catch(e=>{console.error(e);process.exitCode=1});
