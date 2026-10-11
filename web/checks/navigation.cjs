const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict'),ts=require('typescript');
const source=name=>fs.readFileSync('src/'+name,'utf8');
function env(){const events=new Map(),storage=new Map();const location={origin:'http://local',href:'http://local/#library',hash:'#library',pathname:'/'};const window={scrollY:0,scrollTo(){},addEventListener(n,f){if(!events.has(n))events.set(n,new Set());events.get(n).add(f)},removeEventListener(n,f){events.get(n)?.delete(f)},dispatchEvent(e){for(const f of [...events.get(e.type)||[]])f(e)}};return {events,location,window,document:{hidden:false,body:{},documentElement:{scrollHeight:1000},addEventListener(){},removeEventListener(){}},history:{state:null,replaceState(s){this.state=s},back(){this.wentBack=true},scrollRestoration:'auto'},sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},localStorage:{getItem:()=>null,setItem(){}},ResizeObserver:class{observe(){}disconnect(){}},innerHeight:800,requestAnimationFrame:f=>f(),CustomEvent:class{constructor(type,x){this.type=type;this.detail=x?.detail}},Event:class{constructor(type){this.type=type}},URL,URLSearchParams,AbortController,DOMException,setTimeout,clearTimeout,console};}
function moduleFrom(name,e,imports={}){const exports={};vm.runInNewContext(ts.transpileModule(source(name)+(name==='App.tsx'?'\nexport {Detail};':''),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText,{...e,exports,require:n=>imports[n]||{default:()=>null}}, {filename:name});return exports}
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}}
const flush=()=>new Promise(r=>setImmediate(r));
function harness(name,e,imports,props={},component='default'){let cursor=0,values=[],effects=[],tree;const react={useState(initial){const n=cursor++;if(!(n in values))values[n]=typeof initial==='function'?initial():initial;return [values[n],v=>values[n]=typeof v==='function'?v(values[n]):v]},useRef(initial){const n=cursor++;if(!(n in values))values[n]={current:initial};return values[n]},useEffect(fn,deps){const n=cursor++,old=values[n];if(!old||deps.some((v,i)=>v!==old.deps[i])){effects.push(()=>{old?.cleanup?.();values[n]={deps,cleanup:fn()}})}},useCallback(fn){return fn},useSyncExternalStore(subscribe,snapshot){return snapshot()},useLayoutEffect(fn,deps){react.useEffect(fn,deps)}};const jsx={jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})};const m=moduleFrom(name,e,{...imports,react,'react/jsx-runtime':jsx});return {render(next=props){props=next;cursor=0;tree=m[component](props);const pending=effects;effects=[];pending.forEach(f=>f());return tree},values,unmount(){for(const v of values)v?.cleanup?.()},find(pred){let found;function walk(n){if(!n||typeof n!=='object')return;if(pred(n))found=n;for(const c of [n.props?.children].flat(Infinity))walk(c)}walk(tree);return found}}}
(async()=>{
 // H14: a mutation must not reuse or return an older in-flight read.
 const e=env(),first=deferred();let reads=0;e.fetch=async(path,init)=>init?.method==='PUT'?response({ok:true}):++reads===1?first.promise:response({revision:2});
 function response(value){return {ok:true,headers:{get:()=>null},json:async()=>value}}
 const api=moduleFrom('api.ts',e);const old=api.request('/api/library/albums/a');await api.request('/api/library/issues/i/review',{method:'PUT'});const fresh=await api.request('/api/library/albums/a');first.resolve(response({revision:1}));assert.equal(fresh.revision,2);assert.equal((await old).revision,2);assert.equal(api.savedPath('/api/discover?trending=true&q=&refresh=true'),'/api/discover?q=&trending=true');
 // H15: return origins belong to history entries; unrelated navigation cannot inherit them.
 const ne=env(),nav=moduleFrom('Navigation.ts',ne,{react:{}});let route='issues';const dispose=nav.installNavigation(()=>route,()=>{});route='library/album/a';ne.location.hash='#'+route;ne.window.dispatchEvent(new ne.Event('hashchange'));assert.equal(nav.albumOrigin().route,'issues');nav.returnFromAlbum('library');assert.equal(ne.history.wentBack,true);route='library';ne.location.hash='#library';ne.window.dispatchEvent(new ne.Event('hashchange'));route='library/album/b';ne.location.hash='#'+route;ne.window.dispatchEvent(new ne.Event('hashchange'));assert.equal(nav.albumOrigin().route,'library');dispose();
 // H13: exact feed updates and delayed artist responses after returning to Discover.
 const de=env(),calls=[];const req=path=>{if(path.includes('preferences'))return Promise.resolve({});const d=deferred();calls.push({path,...d});return d.promise};const d=harness('Discover.tsx',de,{'./api':{request:req,savedPath:api.savedPath},'./Auth':{useAccount:()=>({admin:true})}});d.render();calls[0].resolve({heading:'Main',rows:[{artist:'A',album:'One'}]});await flush();d.render();de.window.dispatchEvent(new de.CustomEvent('saved-view-updated',{detail:{path:'/api/discover?q=&trending=true'}}));assert.equal(calls.length,1);const link=d.find(n=>n.type==='a'&&n.props.className==='artist-link');link.props.onClick({preventDefault(){}});d.render();const artist=calls.at(-1);const back=d.find(n=>n.type==='button'&&String(n.props.children).includes('Back to Discover'));back.props.onClick();d.render();const main=calls.at(-1);main.resolve({heading:'Main again',rows:[]});await flush();artist.resolve({heading:'Wrong artist',rows:[]});await flush();d.render();assert.equal(d.values[0].heading,'Main again');d.unmount();
 // H10: an earlier artist read cannot overwrite the selected artist.
 const le=env(),lcalls=[];le.location.hash='#library/artist/a';const l=harness('Library.tsx',le,{'./api':{request:path=>{const d=deferred();lcalls.push({path,...d});return d.promise},readGeneration:()=>0,savedPath:api.savedPath},'./Auth':{getAccount:()=>({id:'owner',admin:true})},'./Navigation':{albumOrigin:()=>null,albumReturnLabel:label=>label},'./Controls':{filterMatches:()=>true}}, {initialArtistId:'a'});l.render();le.location.hash='#library/artist/b';l.render({initialArtistId:'b'});const details=id=>({artist:{id,name:id},rows:[],errors:[]});lcalls.find(c=>c.path.endsWith('/b')).resolve(details('b'));await flush();lcalls.find(c=>c.path.endsWith('/a')).resolve(details('a'));await flush();l.render({initialArtistId:'b'});assert.equal(l.find(n=>n.type==='h1').props.children,'b');l.unmount();
 // H12: navigation capabilities and read-only member album actions.
 let member={id:'member',admin:false,can_request:true,can_library_issues:false,lidarr_enabled:true};
 const ae=env(),auth={getAccount:()=>member,useAccount:()=>member};const navigation=moduleFrom('AppNavigation.tsx',ae,{'./Auth':auth});
 assert(!navigation.destinations(false).includes('issues'));assert(!navigation.destinations(false).includes('missing'));
 member={...member,can_library_issues:true};assert(navigation.destinations(false).includes('issues'));
 member={...member,lidarr_enabled:false};assert(!navigation.destinations(false).includes('requests'));
 member={...member,lidarr_enabled:true};const me=env();me.location.hash='#library/album/test';
 const album={album_id:'test',title:'Member album',artist:'Artist',musicbrainz_id:'11111111-1111-1111-1111-111111111111',tracks:[],issues:[],library_state:'Missing'};
 const ml=harness('Library.tsx',me,{'./Auth':auth,'./api':{request:async path=>path.includes('/albums/')?album:{artists:[],rows:[],errors:[],tracks_scanned:true},readGeneration:()=>0,savedPath:api.savedPath},'./Navigation':{albumOrigin:()=>null,albumReturnLabel:label=>label},'./Controls':{filterMatches:()=>true}},{initialAlbumId:'test'});
 ml.render();await flush();ml.render();assert(ml.find(n=>n.type==='button'&&n.props.children==='Request Album'));
 assert(!ml.find(n=>n.type==='button'&&['Change Artwork','Add Artwork','Blocklist Album','Review Link'].includes(n.props.children)));ml.unmount();
 // Library attention counts and extra artist actions are administrator-only.
 const memberDirectory=harness('Library.tsx',env(),{'./Auth':auth,'./api':{request:async()=>({artists:[{id:'a',name:'Member Artist',album_count:3,available_count:2,attention_count:8,mbid:'id'}],rows:[],errors:[],tracks_scanned:true}),readGeneration:()=>0,savedPath:api.savedPath},'./Navigation':{albumOrigin:()=>null},'./Controls':{filterMatches:()=>true}});
 memberDirectory.render();await flush();memberDirectory.render();
 assert(!memberDirectory.find(n=>n.props?.className==='artist-directory-actions'));
 assert(!memberDirectory.find(n=>n.type==='small'&&n.props?.className==='attention'));
 assert(!memberDirectory.find(n=>n.type==='a'&&String(n.props.href).includes('musicbrainz.org')));
 memberDirectory.unmount();
 // Request reads cannot leak across account changes, even when responses arrive late.
 const re=env(),rcalls=[];const store=moduleFrom('LidarrRequests.tsx',re,{'./Auth':auth,'./api':{request:path=>{const d=deferred();rcalls.push({path,...d});return d.promise}},react:{useSyncExternalStore:(subscribe,snapshot)=>snapshot()}});
 const alice=store.refreshLidarrRequests();member={...member,id:'bob',can_downloads:true};const bob=store.refreshLidarrRequests();
 rcalls[1].resolve([{key:'bob'}]);await bob;rcalls[0].resolve([{key:'alice'}]);await alice;assert.equal(store.useLidarrRequests()[0].key,'bob');
 member={...member,lidarr_enabled:false};await store.refreshLidarrRequests();assert.equal(store.useLidarrRequests().length,0);assert.equal(rcalls.length,2);
 // S01: evaluate the rendered Detail tree through the real permission gates.
 const xe=env(),jsx={jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})};
 xe.window.fetch=async()=>({ok:true,json:async()=>({})});
 const gates=moduleFrom('Auth.tsx',xe,{react:{createContext:()=>({}),useContext:()=>member},'react/jsx-runtime':jsx});
 const fakeButton=label=>({default:()=>jsx.jsx('button',{children:label})});
 const testPlaylist={key:'test',name:'Member playlist',source:'text',source_id:'text1',saved_matches:0,unresolved:1,lost:0};
 const testDetail={playlist:testPlaylist,metadata:{name:'Member playlist'},tracks:[{index:0,title:'Song',artist:'Artist',album:'Album',status:'Missing'}]};
 const detail=harness('App.tsx',xe,{'./labels':{matchLabel:value=>value},'./Appearance':{applyTheme:()=>{}},'./Auth':{...gates,useAccount:()=>member},'./PlaylistEditor':fakeButton('Edit Playlist'),'./RenamePlaylist':fakeButton('Rename Playlist'),'./TextPlaylist':fakeButton('Edit Track List'),'./api':{api:{detail:async()=>testDetail}},'./Navigation':{usePageState:(key,value)=>[value,()=>{}]},'./MatchQueue':{useMatchQueue:()=>({rows:[]}),queuedMatchFor:()=>null},'./Controls':{filterMatches:()=>true,trackLink:()=> '#track/test'}}, {playlistKey:'test',jobs:[],enqueue:async()=>{},refresh:async()=>{}},'Detail');
 function visibleButtons(tree){const found=[];function text(n){if(typeof n==='string')return n;if(Array.isArray(n))return n.map(text).join('');if(!n||typeof n!=='object')return '';return text(typeof n.type==='function'?n.type(n.props):n.props?.children)}function walk(n){if(Array.isArray(n)){n.forEach(walk);return}if(!n||typeof n!=='object')return;if(typeof n.type==='function'){walk(n.type(n.props));return}if(n.type==='button')found.push(text(n.props.children));for(const child of [n.props?.children].flat(Infinity))walk(child)}walk(tree);return found}
 member={id:'restricted',admin:false,can_playlists:false,can_request:true,lidarr_enabled:true};detail.render();await flush();let buttons=visibleButtons(detail.render());
 for(const label of ['Edit Playlist','Rename Playlist','Edit Track List','Sync Now','Review Match','Ignore'])assert(!buttons.includes(label),`Restricted member sees ${label}`);
 assert(buttons.includes('Request Album'));
 member={...member,can_playlists:true};buttons=visibleButtons(detail.render());for(const label of ['Edit Playlist','Rename Playlist','Edit Track List','Sync Now','Review Match','Ignore','Request Album'])assert(buttons.includes(label),`Allowed member missing ${label}`);
 member={...member,admin:true,can_playlists:false};buttons=visibleButtons(detail.render());assert(buttons.includes('Sync Now'));assert(buttons.includes('Add Album to Lidarr'));detail.unmount();
 // S02: breadcrumb wording uses the same target-scoped history origin as Back.
 const be=env();be.location.hash='#library/album/a';const breadcrumb=moduleFrom('Navigation.ts',be,{react:{}});
 for(const [route,label]of [['issues','Library Issues'],['requests','Requests'],['library','Library'],['library/artist/1','Linkin Park']]){be.history.state={bridgeAlbumOrigin:{route,target:'library/album/a'}};assert.equal(breadcrumb.albumReturnLabel('Linkin Park'),label)}
 be.history.state={bridgeAlbumOrigin:{route:'issues',target:'library/album/old'}};assert.equal(breadcrumb.albumReturnLabel('Linkin Park'),'Linkin Park');
 // Startup controls persist independently of frequency and preserve existing fields.
 const startupCalls=[],task={id:'plex-scan',action:'plex_scan',scope:'all',name:'Scan Plex Library',hours:1,start_time:'00:15',startup:'missing',worker:'library',configured:true};
 const startupPanel=harness('JobsPanel.tsx',env(),{'./api':{api:{tasks:async()=>({tasks:[task],startup_views:'outdated'}),saveSchedule:async(data,id)=>startupCalls.push({data,id}),saveStartupViews:async(mode)=>startupCalls.push({mode})}}}, {jobs:[],refresh:async()=>{}});
 startupPanel.render();await flush();startupPanel.render();
 startupPanel.find(n=>n.type==='button'&&n.props.children==='Edit').props.onClick();startupPanel.render();
 const startupSelect=startupPanel.find(n=>n.type==='select'&&n.props['aria-label']==='Scan Plex Library at startup');
 assert.equal(startupSelect.props.value,'missing');
 startupSelect.props.onChange({target:{value:'outdated'}});startupPanel.render();await startupPanel.find(n=>n.type==='button'&&n.props.children==='Save Changes').props.onClick();await flush();
 assert.deepEqual(JSON.parse(JSON.stringify(startupCalls[0])),{data:{action:'plex_scan',scope:'all',hours:1,start_time:'00:15',startup:'outdated'},id:'plex-scan'});
 startupPanel.render();const pagesSelect=startupPanel.find(n=>n.type==='select'&&n.props['aria-label']==='Prepare main saved pages at startup');
 assert.equal(pagesSelect.props.value,'outdated');await pagesSelect.props.onChange({target:{value:'never'}});await flush();assert.equal(startupCalls[1].mode,'never');startupPanel.unmount();
 // A member history request retains its original identity when choosing another edition.
 const historySource={history_key:'a'.repeat(64),artist:'Performer',album:'Compilation (Deluxe)'},historyPosts=[];
 const requestPanel=harness('Discover.tsx',env(),{'./api':{request:async(path,init)=>{if(path.endsWith('/search'))return {rows:[{album_id:'selected-album',title:'Selected edition',artist:'Various Artists'}]};historyPosts.push(JSON.parse(init.body));return {message:'Queued',request_id:'selected-album'}}},'./ActionFeedback':{recordAction:()=>{}}},{track:{title:'Song',artist:'Performer',album:'Compilation (Deluxe)',history_source:historySource},onClose:()=>{}},'UserAlbumRequest');
 requestPanel.render();await flush();requestPanel.render();requestPanel.find(n=>n.type==='button'&&n.props.className?.startsWith('candidate')).props.onClick();requestPanel.render();requestPanel.find(n=>n.type==='button'&&n.props.children==='Request selected album').props.onClick();await flush();assert.deepEqual(historyPosts[0].history_source,historySource);assert.equal(historyPosts[0].album_id,'selected-album');requestPanel.unmount();
 // Mirror failures do not repeat the whole lookup, including public fallback.
 const mirrorEnv=env(),attemptBodies=[],statuses=[];
 mirrorEnv.fetch=async(path,init)=>{attemptBodies.push(JSON.parse(init.body));return {ok:false,status:502,json:async()=>({detail:'Connection failed'})}};
 const mbModule=moduleFrom('MusicBrainz.tsx',mirrorEnv),mirrorSettings={server_url:'http://mirror/',capabilities:['search_release-group'],retries:3};
 await assert.rejects(()=>mbModule.mbLookup({album:'Album',artist:'Artist'},3,m=>statuses.push(m),()=>true,mirrorSettings));
 assert.equal(attemptBodies.length,1);assert.equal(attemptBodies[0].attempts,1);assert.equal(attemptBodies[0].retry_delay,0);assert.deepEqual(statuses,['Searching MusicBrainz mirror…']);
 assert.equal(mbModule.mirrorSearch({album:'Album'},mirrorSettings),true);
 assert.equal(mbModule.mirrorSearch({title:'Song',album:''},mirrorSettings),false);
 assert.equal(mbModule.mirrorSearch({fields_search:true,artist:'Artist'},mirrorSettings),true);
 assert.equal(mbModule.mirrorSearch({album:'Album'},{...mirrorSettings,server_url:'https://musicbrainz.org/'}),false);
 assert.equal(mbModule.mirrorSearch({album:'Album'},{...mirrorSettings,capabilities:['lookup']}),false);
 // Queries routed directly to public still honor the existing retry preference.
 let publicAttempts=0;const publicEnv=env();publicEnv.setTimeout=fn=>{fn();return 0};publicEnv.clearTimeout=()=>{};
 publicEnv.fetch=async()=>++publicAttempts===1?{ok:false,status:502,json:async()=>({detail:'Connection failed'})}:{ok:true,json:async()=>({rows:[]})};
 const publicMB=moduleFrom('MusicBrainz.tsx',publicEnv);await publicMB.mbLookup({title:'Song',album:''},1,()=>{},()=>true,mirrorSettings);assert.equal(publicAttempts,2);
 console.log('Passed: mirror single attempts/public retries; history request identity; startup task/page controls; rendered playlist permission gates; album breadcrumb origins; stale-response/navigation checks; member navigation/actions; request account isolation; Plex-only request reads.');
})().catch(e=>{console.error(e);process.exitCode=1});
