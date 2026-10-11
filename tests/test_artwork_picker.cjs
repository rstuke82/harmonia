// Run the actual picker effect with controlled API responses; no browser/network.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('node:assert/strict');
const web=path.resolve(__dirname,'../web'),ts=require(path.join(web,'node_modules/typescript'));
const code=ts.transpileModule(fs.readFileSync(path.join(web,'src/ArtworkPicker.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function mount(request){
 const values=[],effects=[];let cursor=0;
 const react={useState(initial){const n=cursor++;values[n]=initial;return [initial,value=>values[n]=value]},useEffect(fn){effects.push(fn)}};
 const env={exports:{},require:name=>name==='react'?react:name==='./api'?{request}:name==='react/jsx-runtime'?{jsx:()=>null,jsxs:()=>null}:{default:()=>null}};
 vm.runInNewContext(code,env);
 env.exports.default({value:{target:'album',id:'123',kind:'cover',name:'Album'},onClose(){},onSaved(){}});
 return {values,cleanup:effects[0]()};
}
(async()=>{
 let calls=[];
 const picker=mount(async(url,options)=>{calls.push([url,options?.method||'GET']);return calls.length===1?{apple_refresh_required:true,candidates:[]}:{candidates:[{provider:'apple',url:'new-cover'}]}});
 await tick();
 assert.deepEqual(calls.map(c=>c[1]),['GET','POST','GET']);
 assert.equal(calls[1][0],'/api/library/albums/123/artwork/apple/refresh');
 assert.equal(picker.values[0].candidates[0].url,'new-cover');
 picker.cleanup();
 calls=[];
 mount(async(url)=>{calls.push(url);return {apple_refresh_required:false,candidates:[{provider:'apple'}]}});
 await tick();assert.equal(calls.length,1,'Saved artwork must not trigger another lookup');
 let complete;
 const closed=mount(()=>new Promise(resolve=>complete=resolve));closed.cleanup();
 complete({apple_refresh_required:false,candidates:[{provider:'apple'}]});await tick();
 assert.equal(closed.values[0],null,'Closing must ignore late results');
 const failed=mount(async(_,options)=>{if(options?.method==='POST')throw Error('Lookup unavailable');return {apple_refresh_required:true,candidates:[]}});
 await tick();assert.equal(failed.values[1],'Lookup unavailable');
 console.log('Passed: automatic recovery, cached artwork, late-response guard, lookup errors.');
})().catch(error=>{console.error(error);process.exitCode=1});
