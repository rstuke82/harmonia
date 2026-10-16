// Run the actual App and API code with controlled navigation and saved responses.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const ts=require('../web/node_modules/typescript');
const compile=file=>ts.transpileModule(fs.readFileSync(file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const tick=()=>new Promise(setImmediate);
function harness(){
 let cursor=0,slots=[],pending=[],tree,navigate,reads=0,next=[{key:'one',name:'Original'}];
 const events=new Map(),documentEvents=new Map(),location={hash:'#playlists'},document={hidden:false,addEventListener:(k,f)=>documentEvents.set(k,f),removeEventListener:k=>documentEvents.delete(k)};
 const window={addEventListener:(k,f)=>events.set(k,f),removeEventListener:k=>events.delete(k)};
 const same=(a,b)=>a&&b&&a.length===b.length&&a.every((v,i)=>Object.is(v,b[i]));
 const react={useState(value){const i=cursor++;if(!(i in slots))slots[i]={value:typeof value==='function'?value():value};return [slots[i].value,v=>slots[i].value=typeof v==='function'?v(slots[i].value):v]},
 useRef(value){const i=cursor++;if(!(i in slots))slots[i]={current:value};return slots[i]},
 useCallback(fn,deps){const i=cursor++;if(!same(slots[i]?.deps,deps))slots[i]={deps,fn};return slots[i].fn},
 useEffect(fn,deps){const i=cursor++;if(!same(slots[i]?.deps,deps)){const old=slots[i];slots[i]={deps};pending.push(()=>{old?.cleanup?.();slots[i].cleanup=fn()})}}};
 const api={playlists:async()=>{reads++;return next},missing:async()=>[],jobs:async()=>[],health:async()=>({})};
 const mocks={'react':react,'react/jsx-runtime':{jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})},
 './api':{api,activeJob:j=>['queued','running'].includes(j.status),activityJob:()=>true},'./Auth':{useAccount:()=>({id:'owner',admin:true})},
 './Appearance':{applyTheme(){}},'./Navigation':{installNavigation:(_route,update)=>{navigate=update;return()=>{navigate=null}},trackOrigin:()=>null},'./LidarrRequests':{refreshLidarrRequests:async()=>{}},'./usePolling':{usePolling(){}}};
 const env={exports:{},require:name=>mocks[name]||{},location,document,window,setTimeout,clearTimeout,requestAnimationFrame:fn=>fn()};
 vm.runInNewContext(compile('web/src/App.tsx'),env);
 function render(){cursor=0;tree=env.exports.default();pending.splice(0).forEach(fn=>fn());return tree}
 function go(hash){location.hash=hash;navigate();render()}
 function playlists(){const find=node=>{if(!node||typeof node!=='object')return null;if(node.props?.playlists&&'loaded' in node.props)return node.props.playlists;for(const child of [node.props?.children].flat(Infinity)){const value=find(child);if(value)return value}return null};return find(render())}
 return {render,go,playlists,reads:()=>reads,set:rows=>{next=rows},events,documentEvents,document,cleanup:()=>slots.forEach(slot=>slot?.cleanup?.())};
}
async function navigation(){
 const h=harness();h.render();await tick();assert.equal(h.reads(),1);assert.equal(h.playlists()[0].name,'Original');
 h.go('#discover');await tick();h.set([{key:'one',name:'Synced',last_synced:'now'}]);h.go('#playlists');await tick();assert.equal(h.reads(),2,'returning to the list must fetch again');assert.equal(h.playlists()[0].last_synced,'now');
 h.go('#playlist/one');await tick();h.set([{key:'one',name:'Renamed'}]);const before=h.reads();h.go('#playlists');await tick();assert.equal(h.reads(),before+1,'detail-to-list navigation must refresh even though both routes are playlists');assert.equal(h.playlists()[0].name,'Renamed');
 h.set([{key:'one',name:'Updated in another tab'}]);h.events.get('focus')();await tick();assert.equal(h.playlists()[0].name,'Updated in another tab');
 h.document.hidden=true;const hidden=h.reads();h.documentEvents.get('visibilitychange')();await tick();assert.equal(h.reads(),hidden);h.document.hidden=false;h.documentEvents.get('visibilitychange')();await tick();assert.equal(h.reads(),hidden+1);
 h.set([{key:'one',name:'Prepared data'}]);h.events.get('saved-view-updated')({detail:{path:'/api/playlists'}});await tick();assert.equal(h.playlists()[0].name,'Prepared data');
 h.cleanup();assert.equal(h.events.size,0);assert.equal(h.documentEvents.size,0);
}
async function savedWatcher(){
 const timers=[],events=[],responses=[{updated:'1',stale:true,body:[{name:'Old'}]},{updated:'2',stale:false,body:[{name:'Fresh'}]},{updated:'2',stale:false,body:[{name:'Fresh'}]}];
 const env={exports:{},require:()=>({beginRead:()=>Object.assign(()=>{},{update(){}}),readEpoch:()=>0,failRead(){}}),location:{origin:'https://harmonia.example'},URL,AbortController,DOMException,document:{hidden:false},window:{dispatchEvent:event=>events.push(event)},CustomEvent:class{constructor(type,options){this.type=type;this.detail=options.detail}},
 setTimeout:(fn,delay)=>{const timer={fn,delay,active:true};timers.push(timer);return timer},clearTimeout:timer=>{if(timer)timer.active=false},fetch:async path=>{assert.equal(path,'/api/playlists');const value=responses.shift();assert(value,'unexpected fetch');return {ok:true,headers:new Headers({'X-Saved-View-Stale':String(value.stale),'X-Saved-View-Updated':value.updated}),json:async()=>value.body}}};
 vm.runInNewContext(compile('web/src/api.ts'),env);
 assert.equal((await env.exports.api.playlists())[0].name,'Old');
 const watcher=timers.find(timer=>timer.active&&timer.delay===2000);assert(watcher,'stale playlist lists must watch for prepared data');await watcher.fn();
 assert.equal(events.length,1);assert.equal(events[0].type,'saved-view-updated');assert.equal(events[0].detail.path,'/api/playlists');
 assert.equal((await env.exports.api.playlists())[0].name,'Fresh');
}
(async()=>{await navigation();await savedWatcher();console.log('Passed: playlist re-entry, detail return, tab focus/visibility, saved-view completion and listener cleanup.');})().catch(error=>{console.error(error);process.exitCode=1});
