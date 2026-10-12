// Render the actual Activity component with real React, without a browser/network.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const web=path.resolve(__dirname,'../web');
const ts=require(path.join(web,'node_modules/typescript'));
const React=require(path.join(web,'node_modules/react'));
const {renderToStaticMarkup}=require(path.join(web,'node_modules/react-dom/server'));
const code=ts.transpileModule(fs.readFileSync(process.env.ACTIVITY_SOURCE||path.join(web,'src/Activity.tsx'),'utf8'),{
 compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}
}).outputText;
const exportsObject={};
const env={exports:exportsObject,navigator:{},window:{isSecureContext:false},require(name){
 if(name==='react')return React;
 if(name==='react/jsx-runtime')return require(path.join(web,'node_modules/react/jsx-runtime'));
 if(name==='./api')return {api:{},activeJob:job=>['queued','running','cancelling'].includes(job.status)};
 if(name==='./jobLabels'){const labels={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(web,'src/jobLabels.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:labels});return labels;}
 if(name==='./jobActivity')return {currentJobLanes:()=>[]};
 if(name==='./labels')return {displayText:value=>String(value||'')};
 if(name==='./HealthCard')return {stamp:value=>value||''};
 if(name==='./Controls')return {Icon:()=>null};
 if(name==='./LidarrRequests')return {LidarrRequestStatus:()=>null};
 if(name==='./ErrorNotice')return {default:({error})=>React.createElement('p',null,error)};
 throw Error(`Unexpected import ${name}`);
}};
vm.runInNewContext(code,env);
const job={id:'imported-album',action:'album_finalize',status:'completed',payload:{},
 started_at:'2026-10-09T12:44:00-05:00',finished_at:'2026-10-09T12:44:06-05:00',progress:'Complete'};
const render=result=>renderToStaticMarkup(React.createElement(exportsObject.default,{jobs:[{...job,result}],jobId:job.id}));
for(const tracks of [13,0,null,'13',{}]){
 const html=render({tracks,pending:true,summary:'Plex is refreshing; Harmonia will automatically check this album again.'});
 assert.match(html,/Waiting for Plex/);
 assert.doesNotMatch(html,/Track results/);
}
const ignored=render({tracks:[{title:'Track one',artist:'Artist',ok:true},{title:'Track two',artist:'Artist',ok:false,error:'Failed match'}]});
assert.match(ignored,/Track results/);assert.match(ignored,/Track one/);assert.match(ignored,/Ignored/);assert.match(ignored,/Failed match/);
for(const playlists of [7,null,'7',{}])assert.doesNotMatch(render({playlists}),/Playlist results/);
assert.match(render({playlists:[{key:'p1',name:'Playlist one',ok:true}]}),/Playlist one/);
assert.match(render({albums:[{album_id:'rg',ok:true,result:{tracks:13,pending:true}}],summary:'Finalized 1 imported album.'}),/Finalize Imported Album/);
console.log('Passed: numeric/empty/legacy track counts, real track results, playlist result shapes, album batches.');
