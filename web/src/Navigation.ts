import {useEffect,useState} from 'react'
const prefix='bridge-page:'
export function readPage<T>(key:string,fallback:T):T{try{return JSON.parse(sessionStorage.getItem(prefix+key)||'null')??fallback}catch{return fallback}}
export function usePageState<T>(key:string,fallback:T){const [value,setValue]=useState<T>(()=>readPage(key,fallback));useEffect(()=>{try{sessionStorage.setItem(prefix+key,JSON.stringify(value))}catch{}},[key,value]);return [value,setValue] as const}
export function trackOrigin():{route:string;label:string}|null{const origin=history.state?.bridgeOrigin;return origin&&typeof origin.route==='string'&&/^(playlist\/|playlists|missing|search|dashboard)/.test(origin.route)?origin:null}
export function returnFromTrack(){const origin=trackOrigin();if(origin&&history.state?.bridgeTrackEntry){history.back()}else location.hash=origin?.route||'search'}
export function albumOrigin():{route:string;label:string}|null{
 const origin=history.state?.bridgeAlbumOrigin
 return origin&&origin.target===location.hash.slice(1)&&typeof origin.route==='string'&&/^(issues|requests|library(?:\/artist\/[^?#]+)?)$/.test(origin.route)?origin:null
}
export function albumReturnLabel(fallback:string){const origin=albumOrigin();return origin?.route==='issues'?'Issues':origin?.route==='requests'?'Requests':origin?.route==='library'?'Library':fallback}
export function returnFromAlbum(fallback:string){const origin=albumOrigin();if(origin&&history.state?.bridgeAlbumEntry)history.back();else location.hash=origin?.route||fallback}
export function installNavigation(readRoute:()=>string,changed:()=>void){
 let route=readRoute(),pending:{route:string;label:string}|null=null,restoring=false,observer:ResizeObserver|undefined,timer:ReturnType<typeof setTimeout>|undefined
 const previousRestoration=history.scrollRestoration;history.scrollRestoration='manual'
 const save=()=>{if(!restoring)try{sessionStorage.setItem(prefix+'scroll:'+route,String(window.scrollY))}catch{}}
 const stop=()=>{observer?.disconnect();clearTimeout(timer);restoring=false}
 const restore=()=>{stop();const y=Number(readPage('scroll:'+route,0))||0;restoring=true
  const attempt=()=>{window.scrollTo(0,y);if(document.documentElement.scrollHeight-innerHeight>=y)stop()}
  observer=new ResizeObserver(attempt);observer.observe(document.body);timer=setTimeout(stop,180000);requestAnimationFrame(attempt)
 }
 const click=(event:MouseEvent)=>{if(event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;const a=(event.target as Element)?.closest('a');if(!a||a.target==='_blank')return;const url=new URL(a.href,location.href);if(url.origin!==location.origin||url.pathname!==location.pathname)return;save();if(url.hash.startsWith('#track/')&&!route.startsWith('track/'))pending={route,label:route.startsWith('playlist/')?'Back to playlist':route.startsWith('missing')?'Back to Missing':route.startsWith('playlists')?'Back to playlists':route.startsWith('dashboard')?'Back to Dashboard':'Back to Search'} }
 const change=()=>{stop();const previous=route;route=readRoute();if(route.startsWith('library/album/')&&history.state?.bridgeAlbumOrigin?.target!==route){const valid=/^(issues|requests|library(?:\/artist\/[^?#]+)?)$/.test(previous),prior=history.state?.bridgeAlbumOrigin;const origin=valid?{route:previous,target:route,label:previous==='issues'?'Issues':previous==='requests'?'Requests':'Artist'}:prior?.target===previous?{...prior,target:route}:null;history.replaceState({...history.state,bridgeAlbumOrigin:origin,bridgeAlbumEntry:valid},'')}if(pending&&route.startsWith('track/'))history.replaceState({...history.state,bridgeOrigin:pending,bridgeTrackEntry:true},'');pending=null;changed();requestAnimationFrame(restore)}
 const interrupted=()=>{if(restoring)stop()}
 document.addEventListener('click',click,true);window.addEventListener('hashchange',change);window.addEventListener('scroll',save,{passive:true});window.addEventListener('wheel',interrupted,{passive:true});window.addEventListener('touchstart',interrupted,{passive:true});window.addEventListener('keydown',interrupted);requestAnimationFrame(restore)
 return()=>{save();stop();history.scrollRestoration=previousRestoration;document.removeEventListener('click',click,true);window.removeEventListener('hashchange',change);window.removeEventListener('scroll',save);window.removeEventListener('wheel',interrupted);window.removeEventListener('touchstart',interrupted);window.removeEventListener('keydown',interrupted)}
}
