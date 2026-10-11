// Execute the actual setup UI, including retry handling, without browser control.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),ts=require('../web/node_modules/typescript');
const code=ts.transpileModule(fs.readFileSync('web/src/Auth.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const tick=()=>new Promise(setImmediate);
function harness(required=true){
 let cursor=0,values=[],effects=[],cleanups=[],polls=0;const timers=[],calls=[],popup={location:{},close(){}};
 const response=(body,status=200,headers={})=>({ok:status<400,status,headers:new Headers(headers),json:async()=>body,text:async()=>'',blob:async()=>null,arrayBuffer:async()=>null,formData:async()=>null});
 const window={fetch:async(path,init)=>{calls.push({path,init});if(path==='/api/auth/me')return response({user:null,setup_required:required,setup_key_required:required});if(path==='/api/auth/start')return response({url:'https://app.plex.tv/auth'});if(path==='/api/auth/poll')return ++polls===1?response({detail:'Wait'},429,{'Retry-After':'4'}):response({user:{id:'1',owner:true,admin:true},csrf:'yes'});throw Error(path)},open:()=>popup,addEventListener(){},removeEventListener(){}};
 const react={createContext:()=>({Provider:'provider'}),useContext:()=>null,useState(v){let i=cursor++;if(!(i in values))values[i]=v;return [values[i],v=>values[i]=typeof v==='function'?v(values[i]):v]},useRef(v){let i=cursor++;if(!(i in values))values[i]={current:v};return values[i]},useEffect(f){let i=cursor++;if(!(i in values)){values[i]=true;effects.push(f)}}};
 const storage={getItem:()=>null,setItem(){},removeItem(){}};
 const env={exports:{},window,fetch:(...args)=>window.fetch(...args),location:{href:'http://test',origin:'http://test',reload(){}},localStorage:storage,sessionStorage:{clear(){}},Headers,Request,URL,AbortController,DOMException,Event,Date,setTimeout:(f,delay)=>{timers.push({f,delay});return timers.length},clearTimeout(){},require:n=>n==='react'?react:n==='react/jsx-runtime'?{jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})}:{default:()=>null}};
 vm.runInNewContext(code,env);
 function render(){cursor=0;const tree=env.exports.default();cleanups.push(...effects.splice(0).map(f=>f()));return tree}
 function nodes(tree){if(!tree||typeof tree!=='object')return [];return [tree,...[tree.props?.children].flat(Infinity).flatMap(nodes)]}
 return {render,nodes,calls,timers,values,cleanup:()=>cleanups.forEach(f=>f?.())};
}
(async()=>{
 let h=harness();h.render();await tick();let nodes=h.nodes(h.render()),button=nodes.find(x=>x.type==='button');assert(button.props.disabled);const input=nodes.find(x=>x.type==='input');assert.equal(input.props.type,'password');assert.equal(input.props.autoComplete,'off');input.props.onChange({target:{value:'operator-setup-key'}});
 nodes=h.nodes(h.render());button=nodes.find(x=>x.type==='button');assert(!button.props.disabled);await button.props.onClick();assert.equal(JSON.parse(h.calls.find(c=>c.path==='/api/auth/start').init.body).setup_key,'operator-setup-key');assert.equal(h.timers[0].delay,2000);await h.timers.shift().f();assert.equal(h.timers[0].delay,4000,'429 retries using Retry-After instead of aborting login');await h.timers.shift().f();assert.equal(h.values[6],'','setup key cleared after login');h.cleanup();
 h=harness(false);h.render();await tick();nodes=h.nodes(h.render());assert(!nodes.some(x=>x.type==='input'));await nodes.find(x=>x.type==='button').props.onClick();assert.equal(JSON.parse(h.calls.find(c=>c.path==='/api/auth/start').init.body).setup_key,'');h.cleanup();
 console.log('Passed: setup key gate, private input, key submission/clearing, 429 recovery and existing-install sign-in.');
})().catch(e=>{console.error(e);process.exitCode=1});
