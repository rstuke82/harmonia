// Exercise the real Activity retention effect, collapsed header, and dismissal.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const ts=require('../web/node_modules/typescript');
const code=ts.transpileModule(fs.readFileSync('web/src/Activity.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const jsx=(type,props)=>({type,props}),now=Date.parse('2026-10-16T19:16:00Z');
const activeJob=j=>['queued','running','cancelling'].includes(j.status);
function render({age=5*3600000,jobId='',status='interrupted',finished_at=new Date(now-age).toISOString()}={}){
 const job={id:'old-health',action:'health',status,started_at:'2026-10-16T14:10:48Z',finished_at,progress:'Health: 1 of 30 finished'};
 let index=0,slots=['old-health',false,job,[],'',now],effects=[],timers=[],cleared=[],writes=[],marked=[];
 const deps={'react':{useState:()=>{const slot=index++;return [slots[slot],v=>writes.push([slot,v])]},useRef:v=>({current:v}),useEffect:fn=>effects.push(fn),useLayoutEffect:()=>{}},'react/jsx-runtime':{jsx,jsxs:jsx},'./jobReview':{markJobReviewed:j=>marked.push(j.id)},'./jobActivity':{currentJobLanes:()=>[]},'./ErrorNotice':{},'./LidarrRequests':{},'./api':{api:{},activeJob},'./Controls':{Icon:()=>null},'./labels':{displayText:v=>v},'./HealthCard':{stamp:v=>v},'./jobLabels':{actionName:()=> 'Health Check'}};
 const env={exports:{},require:n=>{if(!(n in deps))throw Error(n);return deps[n]},location:{hash:'activity'},window:{},navigator:{},Date:{now:()=>now,parse:Date.parse},setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length},clearTimeout:id=>cleared.push(id)};
 vm.runInNewContext(code,env);
 const tree=env.exports.default({jobs:[job],jobId});
 const cleanup=effects.at(-2)();
 return {tree,timers,cleanup,cleared,writes,marked,location:env.location};
}
let view=render();assert.equal(view.timers.length,1);assert.equal(view.timers[0].delay,0,'Five-hour-old automatic activity expires immediately');
view.timers[0].fn();assert.deepEqual(view.writes,[[0,''],[2,null]]);
view=render({age:12000});assert.equal(view.timers[0].delay,18000,'Keep fresh completion visible for at most 30 seconds');view.cleanup();assert.deepEqual(view.cleared,[1]);
for(const status of ['queued','running','cancelling'])assert.equal(render({status}).timers.length,0);
assert.equal(render({finished_at:'invalid'}).timers.length,0);
view=render({jobId:'old-health'});assert.equal(view.timers.length,0,'Explicit job-history views do not disappear');
assert.match(view.tree.props.className,/is-terminal/);
const bar=view.tree.props.children[0],toggle=bar.props.children[0],dismiss=bar.props.children[1];
const header=toggle.props.children[1].props.children[1];assert.match(JSON.stringify(header),/Finished 2026-10-16T/);assert.doesNotMatch(JSON.stringify(header),/Health: 1 of 30/);
dismiss.props.onClick();assert.deepEqual(view.marked,['old-health']);assert.equal(view.location.hash,'activity');
assert.match(fs.readFileSync('web/src/app.css','utf8'),/\.activity-panel\.is-terminal\.is-collapsed\{position:static/);
console.log('Automatic terminal expiry, persistent explicit history, active-job retention, dated headers, unpinned terminal panels and collapsed dismissal passed.');
