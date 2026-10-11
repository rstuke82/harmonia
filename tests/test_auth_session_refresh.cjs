// Execute the actual Auth component and fetch wrapper without browser control.
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
const ts=require('../web/node_modules/typescript');
const activityModule={exports:{}};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'../web/src/activityNotice.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,activityModule);
const tick=()=>new Promise(setImmediate);
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve}};
const code=ts.transpileModule(fs.readFileSync(path.join(__dirname,'../web/src/Auth.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
function harness(){
 let cursor=0,values=[],effects=[],cleanups=[],me={user:{id:'alice',name:'Alice'},csrf:'alice-csrf'},reloads=0;
 const events=new Map(),calls=[],storage={'bridge-old-data':'Alice private','playlist-bridge-appearance':'dark'},sessionStorage={clears:0,clear(){this.clears++}};
 const localStorage={getItem:k=>storage[k]??null,setItem:(k,v)=>{storage[k]=String(v);localStorage[k]=String(v)},removeItem:k=>{delete storage[k];delete localStorage[k]}};
 Object.assign(localStorage,storage);
 const react={createContext:()=>({Provider:'provider'}),useContext:()=>null,
  useState(v){const n=cursor++;if(!(n in values))values[n]=v;return [values[n],x=>values[n]=typeof x==='function'?x(values[n]):x]},
  useRef(v){const n=cursor++;if(!(n in values))values[n]={current:v};return values[n]},
  useEffect(f){const n=cursor++;if(!(n in values)){values[n]=true;effects.push(f)}}};
 const response=(body,status=200)=>({headers:new Headers({'Content-Type':'application/json'}),clone:()=>response(body,status),status,ok:status>=200&&status<300,json:async()=>body,text:async()=>'',blob:async()=>null,arrayBuffer:async()=>null,formData:async()=>null});
 let apiHandler=async()=>response({});
 const window={fetch:async(input,init)=>{calls.push({input,init,headers:new Headers(init?.headers),signal:init?.signal});return input==='/api/auth/me'?response(await (typeof me==='function'?me():me)):apiHandler(input,init)},
  addEventListener:(n,f)=>events.set(n,f),removeEventListener:n=>events.delete(n),dispatchEvent:e=>{events.get(e.type)?.(e)},open:()=>null};
 const env={exports:{},window,fetch:(...args)=>window.fetch(...args),location:{href:'http://test/',origin:'http://test',reload:()=>reloads++},localStorage,sessionStorage,Headers,Request,URL,AbortController,DOMException,Event,CustomEvent:class extends Event{constructor(type,options){super(type);this.detail=options.detail}},setTimeout,clearTimeout,
  require:n=>n==='./activityNotice'?activityModule.exports:n==='react'?react:n==='react/jsx-runtime'?{jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})}:{default:()=>null}};
 vm.runInNewContext(code,env);
 const render=()=>{cursor=0;const tree=env.exports.default();cleanups.push(...effects.splice(0).map(f=>f()));return tree};
 return {env,events,calls,storage,values,response,render,setMe:v=>{me=v},setAPI:f=>{apiHandler=f},reloads:()=>reloads,cleanup:()=>cleanups.forEach(f=>f?.())};
}
(async()=>{
 let h=harness();h.render();await tick();assert.equal(h.render().props.value.id,'alice');
 h.events.get('focus')();await tick();assert.equal(h.reloads(),0,'unchanged session should not reload');
 h.setMe({user:null,csrf:''});h.events.get('focus')();await tick();assert.equal(h.env.exports.getAccount(),null);assert.equal(h.values[0],null);assert.equal(h.reloads(),1);assert(!('bridge-account-id' in h.storage));assert.equal(h.storage['playlist-bridge-appearance'],'dark');
 await h.env.window.fetch('/api/mutation',{method:'POST'});assert.equal(h.calls.at(-1).headers.get('X-Bridge-CSRF'),null);h.cleanup();assert.equal(h.events.size,0);
 h=harness();h.render();await tick();const pending=deferred();h.setAPI(()=>pending.promise);const oldRequest=h.env.window.fetch('/api/library');const outcome=oldRequest.then(()=>null,e=>e);const oldSignal=h.calls.at(-1).signal;
 h.setMe({user:{id:'bob',name:'Bob'},csrf:'bob-csrf'});h.events.get('focus')();await tick();assert.equal(h.env.exports.getAccount().id,'bob');assert.equal(h.values[0],null,'old app removed during reload');assert(oldSignal.aborted);assert.equal(h.reloads(),1);
 pending.resolve(h.response({private:'Alice'}));assert.equal((await outcome).name,'AbortError');
 h.setAPI(async()=>h.response({}));await h.env.window.fetch('/api/mutation',{method:'POST'});assert.equal(h.calls.at(-1).headers.get('X-Bridge-CSRF'),'bob-csrf');h.cleanup();
 h=harness();h.render();await tick();h.setMe({user:{id:'alice',name:'Alice'},csrf:'rotated'});h.events.get('focus')();await tick();await h.env.window.fetch('/api/mutation',{method:'POST'});assert.equal(h.calls.at(-1).headers.get('X-Bridge-CSRF'),'rotated');assert.equal(h.reloads(),1);h.cleanup();
 h=harness();h.render();await tick();const first=deferred(),second=deferred();let n=0;h.setMe(()=>++n===1?first.promise:second.promise);h.events.get('focus')();h.events.get('focus')();second.resolve({user:{id:'bob'},csrf:'new'});await tick();first.resolve({user:{id:'alice'},csrf:'old'});await tick();assert.equal(h.env.exports.getAccount().id,'bob');assert.equal(h.reloads(),1);h.cleanup();
 h=harness();h.render();await tick();h.setAPI(async()=>h.response({detail:'Sign in'},401));await h.env.window.fetch('/api/library');assert.equal(h.env.exports.getAccount(),null);assert.equal(h.values[0],null);assert.equal(h.reloads(),1);h.cleanup();
 h=harness();h.render();await tick();const body=deferred();h.setAPI(async()=>({...h.response({}),json:()=>body.promise}));const slow=await h.env.window.fetch('/api/library');const lateBody=slow.json().then(()=>null,e=>e);h.setMe({user:{id:'bob'},csrf:'bob'});h.events.get('focus')();await tick();body.resolve({private:'Alice'});assert.equal((await lateBody).name,'AbortError');h.cleanup();
 h=harness();const late=deferred();h.setMe(()=>late.promise);h.render();h.cleanup();late.resolve({user:{id:'late'},csrf:'late'});await tick();assert.equal(h.env.exports.getAccount(),null);assert.equal(h.values[0],null);
 h=harness();h.render();await tick();const notices=[];h.env.window.addEventListener('bridge-notice',event=>notices.push(event.detail));h.setAPI(async()=>h.response({id:'scan',action:'plex_scan',status:'queued'}));const mutation=await h.env.window.fetch('/api/jobs',{method:'POST'});assert.equal(notices.length,1);assert.equal(notices[0].job_id,'scan');assert.equal((await mutation.json()).id,'scan','notification must not consume the caller body');h.env.window.removeEventListener('bridge-notice');h.cleanup();
 h=harness();h.setMe({user:null,setup_required:true,setup_key_required:false,csrf:''});h.render();await tick();const popup={location:{},close(){}};h.env.window.open=()=>popup;let poll;h.env.setTimeout=f=>{poll=f;return 1};h.env.clearTimeout=()=>{};h.setAPI(async(path,init)=>path==='/api/auth/start'?h.response({url:'https://app.plex.tv/auth'}):init?.method==='POST'?h.response({user:{id:'owner',admin:true},csrf:'owner-csrf'}):h.response({setup_libraries:[{key:'7',title:'Music'},{key:'8',title:'Lossless'}]}));
 const find=(tree,predicate)=>{if(!tree||typeof tree!=='object')return null;if(predicate(tree))return tree;for(const child of [tree.props?.children].flat(Infinity)){const match=find(child,predicate);if(match)return match}return null};
 let tree=h.render();await find(tree,n=>n.props?.className==='plex-login-button').props.onClick();await poll();tree=h.render();const select=find(tree,n=>n.type==='select');assert(select,'library choice appears after Plex authorization');select.props.onChange({target:{value:'8'}});tree=h.render();const form=find(tree,n=>n.type==='form');await form.props.onSubmit({preventDefault(){}});await tick();assert.equal(JSON.parse(h.calls.at(-1).init.body).library_key,'8');assert.equal(h.env.exports.getAccount().id,'owner');h.cleanup();
 console.log('Passed: null session, unchanged focus, account switch, CSRF rotation, old-request abort/rejection, out-of-order refresh, central 401, late body decoding and unmount guards.');
})().catch(e=>{console.error(e);process.exitCode=1});
