const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),ts=require('../web/node_modules/typescript');
const compile=file=>ts.transpileModule(fs.readFileSync(file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const tick=()=>new Promise(setImmediate);
function harness(){
 const timers=[],events=[],location={origin:'https://harmonia.test',hash:'#playlists'},document={hidden:false};
 const base={URL,location,Date,AbortController,DOMException,TypeError,document,window:{dispatchEvent:e=>events.push(e)},CustomEvent:class{constructor(type,options){this.type=type;this.detail=options.detail}},
 setTimeout:(fn,delay)=>{const timer={fn,delay,active:true};timers.push(timer);return timer},clearTimeout:timer=>{if(timer)timer.active=false}};
 const activity={exports:{},...base};vm.runInNewContext(compile('web/src/readActivity.ts'),activity);
 const env={exports:{},...base,require:()=>activity.exports,fetch:(...args)=>fetcher(...args)};
 let fetcher=()=>{throw Error('unexpected fetch')};vm.runInNewContext(compile('web/src/api.ts'),env);
 const response=(body,headers={},ok=true)=>({ok,headers:new Headers(headers),json:async()=>body});
 const run=async delay=>{const timer=timers.find(row=>row.active&&row.delay===delay);assert(timer,`missing ${delay}ms timer`);timer.active=false;await timer.fn();await tick()};
 return {a:activity.exports,api:env.exports.api,location,document,events,timers,response,run,setFetch:fn=>{fetcher=fn}};
}
(async()=>{
 let h=harness(),updates=0;const unsubscribe=h.a.subscribeReads(()=>updates++);
 const done=h.a.beginRead('/api/playlists');assert.equal(h.a.pageReads().reads[0].label,'Loading playlists…');done.update('Queued — library work is running');assert.match(h.a.pageReads().reads[0].label,/Queued/);done();assert.equal(h.a.pageReads().reads.length,0);
 const refresh=h.a.beginRead('/api/playlists');assert.match(h.a.pageReads().reads[0].label,/Refreshing/);h.location.hash='#playlist/other';assert.equal(h.a.pageReads().reads[0].route,'#playlists');refresh();
 for(const path of ['/api/jobs','/api/health','/api/auth/me','/api/account/initial-library','/api/settings/logs','/api/settings/console'])h.a.beginRead(path)();
 h.a.beginRead('/api/playlists','POST')();assert.equal(h.a.pageReads().reads.length,0);
 const old=h.a.beginRead('/api/library');h.a.resetReads();const next=h.a.beginRead('/api/library');old();assert.equal(h.a.pageReads().reads.length,1);next();unsubscribe();assert(updates>0);
 h=harness();h.location.hash='#activity';let reads=0;
 h.setFetch(async path=>path.startsWith('/api/jobs/')?h.response({status:'queued',progress:'Waiting for a Library scan'}):++reads===1?h.response({}, {'X-Saved-View-Pending':'true','X-Saved-View-Job':'prepare'}):h.response([{name:'Ready'}]));
 const first=h.api.playlists();await tick();assert.equal(h.a.pageReads().reads[0].label,'Waiting for a Library scan');await h.run(1000);assert.equal((await first)[0].name,'Ready');assert.equal(h.a.pageReads().reads.length,0);
 h=harness();let release;const blocked=new Promise(resolve=>{release=resolve});let calls=0;
 const stale=()=>h.response([{name:'Old'}],{'X-Saved-View-Stale':'true','X-Saved-View-Updated':'1'});
 h.setFetch(async()=>{calls++;return calls===2?blocked:stale()});await h.api.playlists();const poll=h.run(2000);await tick();await h.api.playlists();assert.equal(h.timers.filter(t=>t.active&&t.delay===2000).length,0,'no second watcher while poll is in flight');
 release(h.response([],{'X-Saved-View-Stale':'false','X-Saved-View-Updated':'2'}));await poll;assert.equal(h.a.pageReads().reads.length,0);assert.equal(h.events.filter(e=>e.type==='saved-view-updated').length,1);
 h=harness();h.setFetch(async()=>stale());await h.api.playlists();h.setFetch(async()=>{throw new TypeError('NetworkError')});await h.run(2000);assert.match(h.a.pageReads().failure.message,/last loaded data/);assert.equal(h.a.pageReads().reads.length,0);h.a.dismissReadFailure();assert.equal(h.a.pageReads().failure,null);
 h=harness();h.setFetch(async path=>path.startsWith('/api/jobs/')?h.response({status:'failed'}):h.response([],{'X-Saved-View-Stale':'true','X-Saved-View-Updated':'1','X-Saved-View-Job':'failed','X-Saved-View-Error':'true'}));await h.api.playlists();await h.run(2000);assert(h.a.pageReads().failure,'failed preparation must end refreshing with a visible failure');assert.equal(h.a.pageReads().reads.length,0);
 h=harness();h.setFetch(async()=>stale());await h.api.playlists();h.a.resetReads();await h.run(2000);assert.equal(h.a.pageReads().failure,null);assert.equal(h.events.length,0,'old account watcher cannot publish into a new session');
 console.log('Passed: read loading/refresh phases, route isolation, session reset, preparation progress, single in-flight watcher, background completion and visible failures.');
})().catch(error=>{console.error(error);process.exitCode=1});
