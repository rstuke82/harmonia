// Execute navigation components with controlled accounts and hooks.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const ts=require('../web/node_modules/typescript');
const compile=f=>ts.transpileModule(fs.readFileSync('web/src/'+f,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const jsx=(type,props)=>({type,props});
const pure={exports:{}};vm.runInNewContext(compile('jobActivity.ts'),pure);
let user={admin:true,lidarr_enabled:true},open=false,effects=[],events={},hash='';
const location={get hash(){return hash},set hash(v){hash=v}};
const Modal=()=>null,Activity=()=>null;
const deps={'react':{useState:()=>[open,v=>open=v],useEffect:f=>effects.push(f)},'react/jsx-runtime':{jsx,jsxs:jsx},'./Auth':{getAccount:()=>user},'./Controls':{Icon:()=>null},'./ActivityNav':{default:Activity},'./Brand':{default:()=>null},'./Modal':{default:Modal},'./jobActivity':pure.exports,'./LidarrRequests':{useLidarrRequests:()=>[]},'./requestPresentation':{requestBadge:()=>({count:0,attention:false})}};
const env={exports:{},require:n=>deps[n]||{},location,window:{addEventListener:(n,f)=>events[n]=f,removeEventListener:n=>delete events[n],dispatchEvent:()=>{}},Event,matchMedia:()=>({matches:false,addEventListener(){},removeEventListener(){}})};
vm.runInNewContext(compile('AppNavigation.tsx'),env);
function nodes(tree){if(!tree||typeof tree!=='object')return [];if(Array.isArray(tree))return tree.flatMap(nodes);if(typeof tree.type==='function'&&!['Icon','ActivityNav'].includes(tree.type.name)&&tree.type!==Activity&&tree.type!==Modal)return nodes(tree.type(tree.props));return [tree,...nodes(tree.props?.children)]}
const props={jobs:[{status:'queued'},{status:'running'},{status:'completed'}],page:'discover',admin:true,health:{}};
let tree=env.exports.MobileNavigation(props),all=nodes(tree);
assert.deepEqual(all.filter(n=>n.type==='nav')[0].props.children.flat().filter(Boolean).length,5);
let more=all.find(n=>n.props?.className?.includes('mobile-more'));
assert.equal(more.props['aria-expanded'],false);assert.match(more.props['aria-label'],/1 queued job/);
more.props.onClick();assert.equal(open,true);
tree=env.exports.MobileNavigation(props);all=nodes(tree);
assert(all.some(n=>n.type===Modal));
const sheet=all.find(n=>n.props?.id==='mobile-more-sheet');assert.equal(sheet.props.role,'dialog');
assert.equal(sheet.props['aria-modal'],'true');
const buttons=all.filter(n=>n.type==='button');
assert(buttons.some(n=>n.props.children?.some?.(c=>c?.props?.children==='Blocklist')));
const block=buttons.find(n=>n.props.children?.some?.(c=>c?.props?.children==='Blocklist'));block.props.onClick();assert.equal(hash,'blocklist');assert.equal(open,false);
user={admin:false,lidarr_enabled:false,can_library_issues:false};
assert.deepEqual(Array.from(env.exports.secondaryDestinations(false)),['activity','blocklist','settings']);
user={admin:false,lidarr_enabled:true,can_library_issues:true};
assert.deepEqual(Array.from(env.exports.secondaryDestinations(false)),['activity','issues','blocklist','settings']);
// Profile navigation is account-only; page links have one navigation home.
const authCode=fs.readFileSync('web/src/Auth.tsx','utf8');const profile=authCode.slice(authCode.indexOf('export function AccountButton')).split('\n')[0];
assert(profile.includes('href="#account"'));assert(profile.includes('href="#account/appearance"'));assert(profile.includes('Sign out'));
for(const link of ['#blocklist','#activity','#issues','#missing','#settings'])assert(!profile.includes(`href="${link}"`));
console.log('Build 90 navigation permissions, sheet selection and queued-only badge passed.');
