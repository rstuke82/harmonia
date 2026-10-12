const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),ts=require('../web/node_modules/typescript');
const jsx=(type,props)=>({type,props});
let user={admin:true},slot=0;
const rows=[{id:'a',name:'Shared mix',subscribed:false}];
const deps={react:{useEffect(){},useRef:v=>({current:v}),useState:v=>[slot++===0?rows:v,()=>{}]},'react/jsx-runtime':{jsx,jsxs:jsx},'./api':{request:()=>Promise.resolve(rows)},'./Auth':{useAccount:()=>user},'./Modal':{default:()=>null}};
const env={exports:{},require:n=>deps[n]};
vm.runInNewContext(ts.transpileModule(fs.readFileSync('web/src/SharedPlaylists.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,env);
function text(n){if(Array.isArray(n))return n.map(text).join(' ');if(n&&typeof n==='object')return text(n.props?.children);return typeof n==='string'?n:''}
function render(mode){slot=0;return text(env.exports.default({mode}))}
for(const admin of [true,false]){user={admin};const browse=render('browse');assert.match(browse,/Shared mix/);assert.match(browse,/Add My Copy/);assert.doesNotMatch(browse,/Publish shared source|Unpublish/);const settings=render('sources');assert.doesNotMatch(settings,/Add My Copy/);if(admin)assert.match(settings,/Publish shared source/);else assert.doesNotMatch(settings,/Publish shared source/)}
const app=fs.readFileSync('web/src/App.tsx','utf8');assert.match(app,/page==='playlists'[^\n]+<SharedPlaylists\/>/);assert.match(app,/selected==='shared'&&<SharedPlaylists mode="sources"\/>/);
console.log('Shared playlist browsing and source administration remain separate for administrators and members.');
