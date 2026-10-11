const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),ts=require('../web/node_modules/typescript');
const code=ts.transpileModule(fs.readFileSync('web/src/LibraryInitialization.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const tick=()=>new Promise(setImmediate);
function harness(owner=true){
 let cursor=0,values=[],effects=[],cleanups=[],next={id:'scan',status:'queued',progress:'Waiting'},invalidations=0,calls=0;const timers=[],events=[];
 const react={useState(v){let i=cursor++;if(!(i in values))values[i]=v;return [values[i],v=>values[i]=v]},useEffect(f){let i=cursor++;if(!(i in values)){values[i]=true;effects.push(f)}}};
 const env={exports:{},fetch:async()=>{calls++;return {ok:true,json:async()=>({job:next})}},window:{dispatchEvent:e=>events.push(e)},Event,CustomEvent:class{constructor(type,data){this.type=type;this.detail=data?.detail}},setTimeout:f=>{timers.push(f);return timers.length},clearTimeout(){},require:n=>n==='react'?react:n==='react/jsx-runtime'?{jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})}:n==='./Auth'?{useAccount:()=>({id:'1',owner})}:{changedData:path=>{assert.equal(path,'/api/library');invalidations++}}};
 vm.runInNewContext(code,env);
 function render(){cursor=0;const tree=env.exports.default();cleanups.push(...effects.splice(0).map(f=>f()));return tree}
 return {render,timers,events,set:v=>next=v,calls:()=>calls,invalidations:()=>invalidations,cleanup:()=>cleanups.forEach(f=>f?.())};
}
(async()=>{
 let h=harness();h.render();await tick();assert.equal(h.render().props.role,'status');h.set({id:'scan',status:'failed'});await h.timers.shift()();assert(JSON.stringify(h.render()).includes('Settings → Tasks'));h.set({id:'retry',status:'running'});await h.timers.shift()();assert(JSON.stringify(h.render()).includes('#activity/retry'));h.set({id:'retry',status:'completed'});await h.timers.shift()();assert.equal(h.render(),null);assert.equal(h.invalidations(),1);assert(h.events.some(e=>e.type==='jobs-refresh'));assert.equal(h.events.filter(e=>e.type==='saved-view-updated').length,2);assert.equal(h.timers.length,0);h.cleanup();
 h=harness(false);h.render();await tick();assert.equal(h.calls(),0);h.cleanup();
 h=harness();h.render();await tick();h.cleanup();const before=h.calls();await h.timers.shift()();assert.equal(h.calls(),before);assert.equal(h.timers.length,0,'unmounted completion cannot reschedule');assert.equal(h.invalidations(),0);
 console.log('Passed: owner-only initialization, progress/failure, manual retry, completion refresh and unmount guard.');
})().catch(e=>{console.error(e);process.exitCode=1});
