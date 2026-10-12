const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict'),ts=require('../web/node_modules/typescript');
let values=[],cursor=0,effectDeps,cleanup,effects=[],timers=new Map(),nextTimer=1;
const react={useState(initial){const index=cursor++;if(!(index in values))values[index]=initial;return [values[index],v=>values[index]=typeof v==='function'?v(values[index]):v]},useRef(initial){const index=cursor++;if(!(index in values))values[index]={current:initial};return values[index]},useEffect(fn,deps){if(!effectDeps||deps.some((v,i)=>v!==effectDeps[i])){cleanup?.();effectDeps=deps;effects.push(()=>{cleanup=fn()})}}};
const jsx=(type,props)=>({type,props}),moduleOut={exports:{}};
const code=ts.transpileModule(fs.readFileSync('web/src/ArtworkImage.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
vm.runInNewContext(code,{exports:moduleOut.exports,require:n=>n==='react'?react:{jsx,jsxs:jsx},setTimeout:(fn,ms)=>{const id=nextTimer++;timers.set(id,{fn,ms});return id},clearTimeout:id=>timers.delete(id)});
const component=moduleOut.exports.default;
function render(src,extra={}){cursor=0;let tree=component({src,...extra});while(effects.length)effects.shift()();cursor=0;return component({src,...extra})}
function fire(){const [id,timer]=timers.entries().next().value;timers.delete(id);timer.fn()}
let image=render('/api/cover?v=selected');image.props.onError({});assert.equal([...timers.values()][0].ms,2000);fire();image=render('/api/cover?v=selected');assert.equal(image.props.src,'/api/cover?v=selected&retry=1');
image.props.onError({});assert.equal([...timers.values()][0].ms,5000);fire();image=render('/api/cover?v=selected');assert.equal(image.props.src,'/api/cover?v=selected&retry=2');image.props.onError({});assert.equal(render('/api/cover?v=selected').type,'span');assert.equal(timers.size,0);
image=render('/api/new-cover');image.props.onError({});assert.equal(timers.size,1);image=render('/api/other-cover');assert.equal(timers.size,0);assert.equal(image.props.src,'/api/other-cover');
image.props.onError({});image.props.onLoad({});assert.equal(timers.size,0);
image=render('https://external/cover');image.props.onError({});assert.equal(render('https://external/cover').type,'span');assert.equal(timers.size,0);
image=render('/api/cover');image.props.onError({});cleanup();assert.equal(timers.size,0);
console.log('Artwork retry, source-change cancellation, success and unmount checks passed');
