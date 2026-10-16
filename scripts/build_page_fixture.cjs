const path=require('node:path'),fs=require('node:fs');
const root=path.resolve(__dirname,'..'),output=process.argv[2],esbuild=require(path.join(root,'web/node_modules/esbuild'));
const source=`import './app.css';
import React from 'react';
import {createRoot} from 'react-dom/client';
import PageLoading from './PageLoading';
import {beginRead,resetReads} from './readActivity';
import {api} from './api';












window.mockMode='stale';window.location.hash='playlists';
window.fetch=async path=>{
 if(window.mockMode==='fail')throw new TypeError('Connection lost');
 if(path==='/api/jobs?background=true')return new Response('[]',{headers:{'Content-Type':'application/json'}});
 if(path.startsWith('/api/jobs/'))return new Response(JSON.stringify({status:'queued',progress:'Waiting for the active library job to finish'}),{headers:{'Content-Type':'application/json'}});
 const fresh=window.mockMode==='fresh';return new Response(JSON.stringify([{name:fresh?'Fresh playlist':'Saved playlist'}]),{headers:{'Content-Type':'application/json','X-Saved-View-Updated':fresh?'2':'1','X-Saved-View-Stale':String(!fresh),'X-Saved-View-Job':'prepare'}});
};
let finish=()=>{};
function Fixture(){const [route,setRoute]=React.useState(location.hash),[content,setContent]=React.useState('Saved playlist');
 React.useEffect(()=>{const update=()=>setContent('Fresh playlist');window.addEventListener('saved-view-updated',update);return()=>window.removeEventListener('saved-view-updated',update)},[]);
 return <main><h1>Playlists</h1>{route.startsWith('#activity')&&<PageLoading/>}<section className="panel"><h2>{content}</h2><button onClick={()=>{finish=beginRead('/api/library')}}>Start loading</button><button onClick={()=>finish()}>Finish loading</button><button onClick={()=>void api.playlists()}>Start refresh</button><button onClick={()=>{location.hash='activity';setRoute(location.hash)}}>Open Activity</button><button onClick={()=>{location.hash='playlists';setRoute(location.hash)}}>Return to playlists</button><button onClick={()=>resetReads()}>Reset session</button><p>{route}</p></section></main>};
createRoot(document.getElementById('root')).render(<Fixture/>);`;
esbuild.buildSync({stdin:{contents:source,resolveDir:path.join(root,'web/src'),sourcefile:'page-fixture.tsx',loader:'tsx'},bundle:true,define:{'process.env.NODE_ENV':'"production"'},format:'esm',outfile:path.join(output,'fixture.js'),jsx:'automatic',nodePaths:[path.join(root,'web/node_modules')],loader:{'.png':'dataurl','.woff2':'dataurl'}});
fs.writeFileSync(path.join(output,'index.html'),'<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>');
