// Execute the actual history section and shared store with browser API stubs.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('../web/node_modules/typescript');
const compile=path=>ts.transpileModule(fs.readFileSync(path,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const helpers={exports:{}};vm.runInNewContext(compile('web/src/requestSections.ts'),helpers);
const {requestPage,reuseRequestRows}=helpers.exports;
const records=Array.from({length:721},(_,i)=>({key:String(i),title:'Album '+i,status:'added',active:false,downloads:[],sources:[]}));
assert.equal(requestPage(records,0).rows.length,25);
assert.equal(requestPage(records,0).pages,29);
assert.equal(requestPage(records,28).rows.length,21);
assert.equal(requestPage(records,999).page,28);
assert.equal(requestPage(records,-1).page,0);
assert.equal(requestPage([],5).page,0);
assert.equal(reuseRequestRows(records,JSON.parse(JSON.stringify(records))),records);
const updated=JSON.parse(JSON.stringify(records));updated[1].active=true;
const merged=reuseRequestRows(records,updated);assert.equal(merged[0],records[0]);assert.notEqual(merged[1],records[1]);
assert.equal(reuseRequestRows(records,[records[1],records[0]])[0],records[1]);
assert.equal(reuseRequestRows(records,[]).length,0);

function harness(){
 const values=[],observers=[];let cursor=0,effects=[],cleanup=[],account={id:'owner',admin:true},response=[];
 const react={memo:fn=>fn,useState(initial){const n=cursor++;if(!(n in values))values[n]=typeof initial==='function'?initial():initial;return [values[n],v=>values[n]=typeof v==='function'?v(values[n]):v]},useRef(initial){const n=cursor++;return values[n]||(values[n]={current:initial})},useEffect(fn){effects.push(fn)},useSyncExternalStore:(subscribe,snapshot)=>snapshot()};
 const jsx=(type,props)=>({type,props});
 class Observer{constructor(callback,options){this.callback=callback;this.options=options;observers.push(this)}observe(node){this.node=node}disconnect(){this.disconnected=true}}
 const env={exports:{},IntersectionObserver:Observer,require:name=>name==='react'?react:name==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'fragment'}:name==='./requestSections'?helpers.exports:name==='./Auth'?{getAccount:()=>account}:name==='./api'?{request:async()=>response}: {}};
 vm.runInNewContext(compile('web/src/LidarrRequests.tsx')+'\nexports.TestSection=RequestSection;exports.TestCard=MemoDownloadCard;',env);
 const nodes=tree=>tree==null||typeof tree!=='object'?[]:Array.isArray(tree)?tree.flatMap(nodes):[tree,...nodes(tree.props?.children)];
 function render(props){for(const fn of cleanup)fn?.();cleanup=[];cursor=0;effects=[];const tree=env.exports.TestSection(props);for(const n of nodes(tree))if(n.props?.ref)n.props.ref.current={node:n};for(const fn of effects)cleanup.push(fn());return tree}
 return {env,observers,render,nodes,cards:tree=>nodes(tree).filter(n=>n.type===env.exports.TestCard),setResponse:v=>response=v,setAccount:v=>account=v};
}
const h=harness(),props={name:'Completed',records,albumKey:''};
let tree=h.render(props);assert.equal(h.cards(tree).length,0);assert.equal(h.nodes(tree).find(n=>n.props?.className==='pill').props.children,721);
const toggle=tree=>h.nodes(tree).find(n=>n.props?.className==='request-section-toggle');
assert.equal(toggle(tree).props['aria-expanded'],false);toggle(tree).props.onClick();tree=h.render(props);assert.equal(h.cards(tree).length,25);assert.equal(toggle(tree).props['aria-expanded'],true);
const next=tree=>h.nodes(tree).find(n=>String(n.props?.['aria-label']||'').startsWith('Next '));
const previous=tree=>h.nodes(tree).find(n=>String(n.props?.['aria-label']||'').startsWith('Previous '));
assert.equal(previous(tree).props.disabled,true);next(tree).props.onClick();tree=h.render(props);assert.equal(h.cards(tree).length,25);assert.equal(h.cards(tree)[0].props.record.key,'25');
previous(tree).props.onClick();tree=h.render(props);assert.equal(h.cards(tree)[0].props.record.key,'0');
for(let i=0;i<28;i++){next(tree).props.onClick();tree=h.render(props)}
assert.equal(h.cards(tree).length,21);assert.equal(next(tree).props.disabled,true);assert.equal(h.cards(tree)[0].props.record.key,'700');
toggle(tree).props.onClick();tree=h.render(props);assert.equal(h.cards(tree).length,0);assert.equal(h.observers.length,0);
for(const name of ['In Progress','Needs Action','Partially Available']){const active=harness();assert.equal(active.cards(active.render({...props,name})).length,25)}
const focus=harness();tree=focus.render({...props,albumKey:'700'});assert.equal(focus.cards(tree).length,21);assert.equal(focus.cards(tree)[0].props.focused,true);assert.equal(focus.nodes(tree).find(n=>n.props?.className==='request-section-toggle').props['aria-expanded'],true);
const sizeHarness=harness();let sized=sizeHarness.render({...props,initiallyOpen:true});
const sizeSelect=tree=>sizeHarness.nodes(tree).find(n=>String(n.props?.['aria-label']||'').endsWith('results per page'));
assert.deepEqual(sizeHarness.nodes(sizeSelect(sized)).filter(n=>n.type==='option').map(n=>n.props.value),[5,10,25,50,100]);
for(const size of [5,10,25,50,100]){sizeSelect(sized).props.onChange({target:{value:String(size)}});sized=sizeHarness.render({...props,initiallyOpen:true});assert.equal(sizeHarness.cards(sized).length,size);assert.equal(requestPage(records,0,size).rows.length,size)}
const explicit=harness();assert.equal(explicit.cards(explicit.render({...props,initiallyOpen:true})).length,25);
const filtered=harness();assert.equal(filtered.cards(filtered.render({...props,initiallyOpen:true,records:records.slice(700)})).length,21);
const sorted=harness();tree=sorted.render({...props,initiallyOpen:true,records:[...records].reverse()});assert.equal(sorted.cards(tree)[0].props.record.key,'720');
const cancelled=harness();assert.equal(cancelled.cards(cancelled.render({...props,name:'Cancelled / Failed'})).length,25);
const changing=harness();tree=changing.render({...props,albumKey:'700'});tree=changing.render({...props,records:records.slice(0,10),albumKey:''});assert.equal(changing.cards(tree).length,10);
(async()=>{
 const store=harness();let original=store.env.exports.useLidarrRequests();store.setResponse([]);await store.env.exports.refreshLidarrRequests();let next=store.env.exports.useLidarrRequests();assert.notEqual(next,original,'Loaded-empty state must notify React');assert.equal(store.env.exports.useRequestLoadState().loaded,true);
 store.setResponse(records);await store.env.exports.refreshLidarrRequests();original=store.env.exports.useLidarrRequests();store.setResponse(JSON.parse(JSON.stringify(records)));await store.env.exports.refreshLidarrRequests();assert.equal(store.env.exports.useLidarrRequests(),original,'Unchanged polling data must keep the snapshot stable');
 store.setAccount({id:'member',admin:false});store.setResponse([]);await store.env.exports.refreshLidarrRequests();assert.equal(store.env.exports.useLidarrRequests().length,0,'Account changes must discard the previous history');
 console.log('Request history: 721 completed requests → zero mounted cards when collapsed; 25 per page, page boundaries, active visibility, deep links, filters/sorting, stable polling and account isolation passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
