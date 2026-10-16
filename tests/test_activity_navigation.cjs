// Exercise the real navigation component, review persistence, and detail acknowledgement.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const ts=require('../web/node_modules/typescript');
const compile=file=>ts.transpileModule(fs.readFileSync('web/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const jsx=(type,props)=>({type,props});
let values={},storageBlocked=false;
const storage={getItem:key=>{if(storageBlocked)throw Error('Blocked');return values[key]??null},setItem:(key,value)=>{if(storageBlocked)throw Error('Blocked');values[key]=value}};
const listeners=new Map();
const window={addEventListener:(name,fn)=>{if(!listeners.has(name))listeners.set(name,new Set());listeners.get(name).add(fn)},removeEventListener:(name,fn)=>listeners.get(name)?.delete(fn),dispatchEvent:event=>{for(const fn of listeners.get(event.type)||[])fn(event)}};
class CustomEvent{constructor(type,options){this.type=type;this.detail=options?.detail}}
const review={exports:{},localStorage:storage,window,CustomEvent};vm.runInNewContext(compile('jobReview.ts'),review);
const pure={exports:{}};vm.runInNewContext(compile('jobActivity.ts'),pure);
let hash='',state,cleanups=[];
const location={get hash(){return hash},set hash(value){hash=value}};
const activeJob=job=>['queued','running','cancelling'].includes(job.status);
const attention=job=>['failed','interrupted'].includes(job.status)||!!job.result?.partial_success;
function nav(jobs,page='discover'){
 const deps={'react':{useState:initial=>{if(state===undefined)state=typeof initial==='function'?initial():initial;return [state,value=>state=typeof value==='function'?value(state):value]},useEffect:fn=>{cleanups.push(fn())}},'react/jsx-runtime':{jsx,jsxs:jsx},'./api':{activeJob},'./Controls':{Icon:()=>null},'./LidarrRequests':{useLidarrRequests:()=>[]},'./jobActivity':pure.exports,'./jobReview':review.exports,'./Activity':{actionName:action=>action,newestActive:jobs=>jobs.find(j=>activeJob(j)&&!['prepare_views','request_status'].includes(j.action)),jobNeedsAttention:attention,showActivity:id=>location.hash='activity/'+encodeURIComponent(id)}};
 const env={exports:{},require:name=>{if(!(name in deps))throw Error(name);return deps[name]},window,location};vm.runInNewContext(compile('ActivityNav.tsx'),env);
 for(const cleanup of cleanups)cleanup?.();cleanups=[];
 return env.exports.default({jobs,page});
}
const failure=(id='failed-job',finished_at='2026-10-16T10:00:00Z',status='failed')=>({id,action:'sync',status,finished_at});
let jobs=[failure()];let button=nav(jobs,'activity');
assert.match(button.props['aria-label'],/Previous job failed/);assert.doesNotMatch(button.props['aria-label'],/Needs attention/);
assert.equal(Object.keys(values).length,0,'Opening generic Activity does not acknowledge failures');
button.props.onClick();assert.equal(hash,'activity/failed-job');assert.equal(Object.keys(values).length,0,'Navigation alone does not acknowledge an unloaded job');
review.exports.markJobReviewed(jobs[0]);button=nav(jobs);assert.equal(button.props['aria-label'],'Jobs: No activity');
// Each job is independent, including failures with exactly the same finish time.
jobs=[failure('other'),failure()];button=nav(jobs);assert.match(button.props['aria-label'],/Previous job failed/);button.props.onClick();assert.equal(hash,'activity/other');
for(const [status,result,label] of [['interrupted',null,'Previous job interrupted'],['completed',{partial_success:true},'Previous job partially completed']]){
 button=nav([{...failure(status),status,result}]);assert.match(button.props['aria-label'],new RegExp(label));
}
for(const action of ['prepare_views','request_status'])assert.equal(nav([{...failure(action),action}]).props['aria-label'],'Jobs: No activity');
button=nav([{id:'running',action:'sync',status:'running'},failure('other')]);assert.equal(button.props['aria-label'],'Jobs: sync');button.props.onClick();assert.equal(hash,'activity/running');
button=nav([{id:'queued',action:'sync',status:'queued'},failure('other')]);
const badge=button.props.children.find(node=>node?.props?.className?.includes('queued-count'));assert.equal(badge.props.children,1);button.props.onClick();assert.equal(hash,'activity/queued');
values['bridge-account-id']='second-account';state=undefined;assert.match(nav([failure()]).props['aria-label'],/Previous job failed/);
values['harmonia-job-reviews:second-account']='broken';assert.equal(Object.keys(review.exports.reviewedJobs()).length,0);
storageBlocked=true;state=undefined;button=nav([failure('blocked-storage')]);review.exports.markJobReviewed(failure('blocked-storage'));assert.equal(nav([failure('blocked-storage')]).props['aria-label'],'Jobs: No activity');storageBlocked=false;
// Clear the full alert backlog in one event; retain account isolation and new failures.
values['bridge-account-id']='batch-account';state=undefined;
const backlog=Array.from({length:250},(_,i)=>failure('backlog-'+i));
assert.match(nav(backlog).props['aria-label'],/Previous job failed/);
review.exports.markJobsReviewed([...backlog,{id:'active',status:'running',finished_at:backlog[0].finished_at}]);
assert.equal(nav(backlog).props['aria-label'],'Jobs: No activity');
assert.equal(Object.keys(review.exports.reviewedJobs()).length,250);
assert.equal(review.exports.reviewedJobs().active,undefined);
state=undefined;assert.equal(nav(backlog).props['aria-label'],'Jobs: No activity','Clear survives reload');
assert.match(nav([failure('new-failure'),...backlog]).props['aria-label'],/Previous job failed/);
values['bridge-account-id']='other-batch-account';state=undefined;assert.match(nav(backlog).props['aria-label'],/Previous job failed/);
// Run the actual Activity acknowledgement effect with loaded, failed, collapsed, and loading detail states.
function acknowledgement({detail=null,error='',open=true}={}){
 const job=failure('details');let effects=[],i=0,marked=[];
 const slots=['details',open,detail,[],error,0];
 const deps={'react':{useState:initial=>[slots[i++],()=>{}],useRef:value=>({current:value}),useEffect:fn=>effects.push(fn),useLayoutEffect:()=>{}},'react/jsx-runtime':{jsx,jsxs:jsx},'./jobReview':{markJobReviewed:job=>marked.push(job.id)},'./api':{api:{},activeJob},'./jobActivity':{currentJobLanes:()=>[]},'./jobLabels':{actionName:v=>v},'./labels':{displayText:v=>v},'./HealthCard':{stamp:v=>v},'./Controls':{Icon:()=>null},'./LidarrRequests':{},'./ErrorNotice':{default:()=>null}};
 const env={exports:{},require:name=>{if(!(name in deps))throw Error(name);return deps[name]},location,window,navigator:{},Date};vm.runInNewContext(compile('Activity.tsx'),env);
 env.exports.default({jobs:[job],jobId:'details'});effects.at(-1)();return marked;
}
assert.equal(acknowledgement().length,0);
assert.equal(acknowledgement({detail:failure('details'),open:false}).length,0);
assert.equal(acknowledgement({detail:failure('details'),error:'Load failed'}).length,0);
assert.equal(acknowledgement({detail:{...failure('details'),status:'running'}}).length,0);
assert.deepEqual(acknowledgement({detail:failure('details')}),['details']);
console.log('Historical job wording, direct links, per-job/account review, successful-detail acknowledgement, active precedence, and queued-only badges passed.');
