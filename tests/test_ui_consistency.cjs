// Evaluate the actual components with controlled hooks and service responses.
// No browser, live service, or production account is used.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const ts=require('../web/node_modules/typescript'),root=path.resolve(__dirname,'../web/src');
const text=file=>fs.readFileSync(path.join(root,file),'utf8');
for(const file of fs.readdirSync(root).filter(file=>/\.tsx?$/.test(file))){
 const parsed=ts.createSourceFile(file,text(file),ts.ScriptTarget.Latest,true,file.endsWith('.tsx')?ts.ScriptKind.TSX:ts.ScriptKind.TS);
 assert.equal(parsed.parseDiagnostics.length,0,`${file}: ${parsed.parseDiagnostics.map(d=>ts.flattenDiagnosticMessageText(d.messageText,' ')).join('; ')}`);
}
function moduleCode(file){return ts.transpileModule(text(file),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText}
function pure(file,imports={}){const env={exports:{},require:name=>imports[name]||{}};vm.runInNewContext(moduleCode(file),env);return env.exports}
function expression(code,globals={}){return vm.runInNewContext(ts.transpile(code,{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.None}),globals)}
function ast(file){return ts.createSourceFile(file,text(file),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX)}
function find(file,predicate){const tree=ast(file);let found;function visit(n){if(predicate(n,tree))found=n;ts.forEachChild(n,visit)}visit(tree);assert.ok(found,file);return found.getText(tree)}
function declaration(file,name){return find(file,n=>ts.isVariableDeclaration(n)&&n.name.getText()===name)}
const presentation=pure('requestPresentation.ts');
const covers=pure('AlbumCover.tsx',{'./requestPresentation':presentation});
assert.equal(covers.coverStatus({plex_available:true,availability:'Partially Available',download_status:'Imported into Lidarr'}).tone,'partial');
for(const row of [{error:'Import failed'},{download_error:'Import failed'},{downloads:[{error:'Corrupt file'}]},{status:'search_failed'},{download_status:'Import failed'},{removed_from_lidarr:true}]){
 const r={plex_available:true,lidarr_available:true,...row};assert.equal(presentation.downloadGroup(r),'Needs Action');assert.equal(covers.coverStatus(r).tone,'error');
}
assert.equal(presentation.downloadGroup({status:'cancelled',lidarr_id:1}),'Cancelled / Failed');
assert.equal(covers.coverStatus({plex_available:true,availability:'In Plex'}).tone,'available');
const badge=presentation.requestBadge([{status:'search_failed'},{status:'search_failed'},{status:'search_failed'},{lidarr_id:1,downloads:[{}]}],true);
assert.equal(badge.count,3);assert.equal(badge.attention,true);
assert.equal(presentation.requestBadge([{status:'search_failed'},{lidarr_id:1,downloads:[{}]}],false).count,1);
// Extract the component's sort callback, not a copy of its implementation.
const sortCode=find('LidarrRequests.tsx',(n,tree)=>ts.isVariableDeclaration(n)&&n.name.getText(tree)==='sorted');
const call=ast('LidarrRequests.tsx');let callback;function visit(n){if(ts.isVariableDeclaration(n)&&n.name.getText(call)==='sorted')callback=n.initializer.arguments[0].getText(call);ts.forEachChild(n,visit)}visit(call);
const records=[{title:'Z',artist:'Z',created_at:2,downloads:[{percent:80}],lidarr_id:1},{title:'A',artist:'A',created_at:1,downloads:[{percent:20}],lidarr_id:1}];
for(const field of ['album','artist','requested','updated'])for(const direction of ['asc','desc']){
 const compare=expression('('+callback+')',{sort:field,direction,requestedAt:r=>r.created_at,modifiedAt:r=>r.created_at,downloadGroup:presentation.downloadGroup});
 assert.equal([...records].sort(compare)[0].title,direction==='asc'?'A':'Z',field+' '+direction);
}
const filterTree=ast('Library.tsx');let predicate;function filterVisit(n){if(ts.isVariableDeclaration(n)&&n.name.getText(filterTree)==='rows'&&n.initializer?.expression?.name?.text==='sort')predicate=n.initializer.expression.expression.arguments[0].getText(filterTree);ts.forEachChild(n,filterVisit)}filterVisit(filterTree);
const storage={},globals={artist:'',query:'',statuses:[],releases:[],secondaryTypes:[],releaseType:()=> 'Album',localStorage:{setItem:(k,v)=>storage[k]=v}};
for(const [setter,key] of [['setQuery','query'],['setStatuses','statuses'],['setReleases','releases'],['setSecondaryTypes','secondaryTypes']])globals[setter]=v=>globals[key]=v;
const included=expression('('+predicate+')',globals),compilation={artist:'A',title:'Album',secondary_types:['Compilation']};assert.equal(included(compilation),false);
const clear=find('Library.tsx',n=>ts.isFunctionDeclaration(n)&&n.name?.text==='clearAlbumFilters');expression(clear+';clearAlbumFilters()',globals);
assert.equal(included(compilation),true);assert.deepEqual(JSON.parse(storage['playlist-bridge-secondary-types']),['Live','Compilation','Remix']);

const tick=()=>new Promise(resolve=>setImmediate(resolve));
function mount(file,component,props,imports={}){
 const values=[],effects=[],refs=[];let cursor=0,refCursor=0,tree,id=0;
 const react={useState(initial){const n=cursor++;if(!(n in values))values[n]=typeof initial==='function'?initial():initial;return [values[n],v=>values[n]=typeof v==='function'?v(values[n]):v]},useRef(initial){const n=refCursor++;return refs[n]||(refs[n]={current:initial})},useEffect(fn){effects.push(fn)},useLayoutEffect(){},useId:()=> 'control-'+(++id)};
 const jsx=(type,props)=>({type,props});
 const env={exports:{},require:name=>name==='react'?react:name==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'fragment'}:imports[name]||{default:'mock'}};
 vm.runInNewContext(moduleCode(file),env);
 function render(){cursor=0;refCursor=0;tree=env.exports[component](props);return tree}
 render();return {render,values,effects,get tree(){return tree}};
}
function nodes(tree){if(tree==null||typeof tree!=='object')return [];if(Array.isArray(tree))return tree.flatMap(nodes);return [tree,...nodes(tree.props?.children)]}
function label(node){const walk=v=>Array.isArray(v)?v.map(walk).join(''):typeof v==='object'?walk(v?.props?.children):v==null?'':String(v);return walk(node.props.children)}
function control(h,value){const found=nodes(h.tree).find(n=>n.type==='button'&&label(n)===value);assert.ok(found,value);return found}
function content(tree){return nodes(tree).map(label).join(' ')}
(async()=>{
 // A pending artist search locks switches, results and dismissal.
 let resolveSearch,closed=0,saved;
 const artist=mount('ArtistLinks.tsx','default',{artist:{id:'1',name:'Artist'},initialProvider:'apple',onClose:()=>closed++,onSaved:()=>{}},{'./api':{request:(url,options)=>options?.method==='POST'?new Promise(resolve=>resolveSearch=resolve):options?.method==='PUT'?(saved=JSON.parse(options.body),Promise.resolve({})):Promise.resolve({})}});
 const form=nodes(artist.tree).find(n=>n.type==='form');form.props.onSubmit({preventDefault(){}});artist.render();
 assert.equal(control(artist,'Close').props.disabled,true);
 const switchButton=nodes(artist.tree).filter(n=>n.type==='button'&&label(n)==='Search / Change')[1];assert.equal(switchButton.props.disabled,true);switchButton.props.onClick();artist.tree.props.onClose();assert.equal(closed,0);
 resolveSearch([{artistId:42,artistName:'Artist',artistViewUrl:'https://music.apple.com/artist/42'}]);await tick();artist.render();
 const result=nodes(artist.tree).find(n=>n.type==='button'&&label(n).endsWith('Select'));result.props.onClick();await tick();assert.equal(saved.provider,'apple');assert.equal(saved.value,'42');
 // Failed editor loads keep all mutations behind a disabled fieldset and Save.
 const editor=mount('PlaylistEditor.tsx','default',{playlistKey:'k'},{'./api':{request:async()=>{throw Error('Load failed')}}});control(editor,'Edit Playlist').props.onClick();editor.render();editor.effects.at(-1)();await tick();editor.render();
 assert.equal(control(editor,'Save & Sync').props.disabled,true);assert.equal(nodes(editor.tree).find(n=>n.type==='fieldset').props.disabled,true);assert.ok(control(editor,'Retry loading playlist'));
 // Initial Blocklist loading and errors must never claim the collection is empty.
 let resolveList;const block=mount('DiscoverPreferences.tsx','BlockedArtists',{}, {'./api':{request:()=>new Promise(resolve=>resolveList=resolve)}});
 assert.ok(content(block.tree).includes('Loading blocklist'));assert.ok(!content(block.tree).includes('No blocked artists'));
 const errorBlock=mount('DiscoverPreferences.tsx','BlockedArtists',{}, {'./api':{request:async()=>{throw Error('Offline')}}});errorBlock.effects[0]();await tick();errorBlock.render();assert.ok(!content(errorBlock.tree).includes('No blocked artists'));assert.ok(control(errorBlock,'Retry loading blocklist'));
 // Search capability gates both disabled accounts and optional Lidarr.
 for(const account of [{admin:false,can_request:false,lidarr_enabled:true},{admin:true,lidarr_enabled:false},{admin:true,lidarr_enabled:true}]){
  const search=mount('SearchPage.tsx','default',{}, {'./Auth':{useAccount:()=>account},'./Navigation':{usePageState:()=>['Artist',()=>{}]}});
  assert.equal(control(search,'Find albums to request').props.disabled,account.lidarr_enabled===false||!account.admin&&!account.can_request);
 }
 // Requests shelf distinguishes loading, failure and genuinely empty.
 for(const state of [{loaded:false,error:''},{loaded:false,error:'Offline'},{loaded:true,error:''}]){
  const shelf=mount('DiscoverShelves.tsx','default',{}, {'./Auth':{useAccount:()=>({admin:true})},'./LidarrRequests':{useLidarrRequests:()=>[],useRequestLoadState:()=>state}});
  assert.equal(content(shelf.tree).includes('No requests yet.'),state.loaded&&!state.error);
 }
 const controls=pure('Controls.tsx',{'react':{useId:()=> 'sort'}});
 assert.deepEqual(Array.from(controls.sortOrderLabels('requested')),['Oldest first','Newest first']);assert.deepEqual(Array.from(controls.sortOrderLabels('album')),['A–Z','Z–A']);
 assert.ok(!text('IgnoreDialog.tsx').includes('Settings → Ignored Tracks'));assert.ok(!text('BulkIgnore.tsx').includes('Settings → Ignored Tracks'));
 assert.ok(!text('Auth.tsx').includes('href="#missing">Library Management'));
 assert.ok(text('TrackDetails.tsx').includes('aria-label="Filter playlists"'));assert.ok(text('TextPlaylist.tsx').includes('aria-label="Tracks to parse"'));
 assert.ok(text('JobsPanel.tsx').includes('"lookup"'));
 assert.match(text('pass-20261008.css'),/@media\(max-width:850px\)\{\s*\.shell main>header/);
// Evaluate the imported CSS cascade for the header cells at phone/tablet/desktop widths.
// This is a rule check, not a browser rendering or physical-device test.
const postcss=require('../web/node_modules/postcss');
const sheets=[...text('main.tsx').matchAll(/import '\.\/([^']+\.css)'/g)].map(m=>m[1]);
function headerStyles(target,width){const applied={};let sequence=0;
 for(const file of sheets)postcss.parse(text(file)).walkRules(rule=>{
  let parent=rule.parent,enabled=true;
  while(parent){if(parent.type==='atrule'&&parent.name==='media'){
   const media=parent.params;if(/prefers-|hover|pointer|orientation/.test(media))enabled=false;
   for(const match of media.matchAll(/(max|min)-width:\s*(\d+)px/g))if(match[1]==='max'?width>Number(match[2]):width<Number(match[2]))enabled=false;
  }parent=parent.parent}
  if(!enabled)return;
  const selectors=rule.selector.split(',').map(s=>s.replace(/\s/g,''));
  for(const selector of selectors){if(![`.${target}`,`.shellmain>header>.${target}`].includes(selector))continue;
   const specificity=(selector.match(/\./g)||[]).length;
   rule.walkDecls(decl=>{if(!['display','grid-row','grid-column'].includes(decl.prop))return;const rank=[decl.important?1:0,specificity,++sequence],previous=applied[decl.prop]?.rank;if(!previous||rank[0]>previous[0]||rank[0]===previous[0]&&(rank[1]>previous[1]||rank[1]===previous[1]&&rank[2]>previous[2]))applied[decl.prop]={value:decl.value,rank}});
  }
 });return Object.fromEntries(Object.entries(applied).map(([key,value])=>[key,value.value]));
}
for(const width of [390,760,800,850,1000]){
 const brand=headerStyles('mobile-brand',width),search=headerStyles('header-search',width);
 assert.notEqual(search.display,'none','Search must remain visible at '+width);
 if(width<=850){assert.notEqual(brand.display,'none');assert.notEqual(brand['grid-row'],search['grid-row'],'Brand and search must occupy separate rows at '+width)}else assert.equal(brand.display,'none');
}
 console.log('UI consistency: request sort/status/badge, complete filters, provider/save locks, editor recovery, loading/error states, capability gates, explicit order, labels and tablet breakpoint passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
