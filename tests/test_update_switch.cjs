const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict'), ts = require('../web/node_modules/typescript');
const jsx = (type, props) => ({type, props});
let slot = 0, data = {enabled: false, channel: 'beta'}, saved = [], errors = [];
const api = {updateSettings: async enabled => { saved.push(enabled); return {...data, enabled}; }};
const deps = {react: {useEffect(){}, useState(value){const index=slot++; return [index===0?data:value, next=>{if(index===0)data=next;if(index===1)errors.push(next);}];}}, 'react/jsx-runtime': {jsx, jsxs: jsx}, './api': {api}, './Controls': {Icon:()=>null}, './HealthCard': {stamp:v=>v}};
const env = {exports: {}, require: name=>deps[name]||{}, console};
vm.runInNewContext(ts.transpileModule(fs.readFileSync('web/src/SettingsTools.tsx', 'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS, jsx:ts.JsxEmit.ReactJSX}}).outputText, env);
function render(){slot=0; return env.exports.About({jobs:[]});}
function walk(node, predicate){if(!node||typeof node!=='object')return null;if(predicate(node))return node;for(const child of [node.props?.children].flat(Infinity)){const found=walk(child,predicate);if(found)return found;}return null;}
(async()=>{
 let tree=render(), checkbox=walk(tree,n=>n.type==='input'&&n.props.type==='checkbox'), button=walk(tree,n=>n.type==='button');
 assert.equal(checkbox.props.checked,false);assert.equal(button.props.disabled,true);
 await checkbox.props.onChange({target:{checked:true}});
 assert.deepEqual(saved,[true]);tree=render();assert.equal(walk(tree,n=>n.type==='button').props.disabled,false);
 api.updateSettings=async()=>{throw Error('Save failed');};
 await walk(tree,n=>n.type==='input').props.onChange({target:{checked:false}});
 assert.equal(data.enabled,true);assert(errors.includes('Save failed'));
 console.log('Update switch, disabled manual checks and failed-save recovery passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
