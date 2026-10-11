import {useLayoutEffect,useRef} from 'react'

// Relative sizes preserve browser text scaling; CSS reserves two lines.
export function fitTitle(element:HTMLElement){
 if(!element.clientWidth)return
 for(const size of ['1.125rem','1.0625rem','1rem']){
  element.style.fontSize=size
  if(element.scrollHeight<=element.clientHeight+1)break
 }
}
export default function FittedTitle({title}:{title:string}){
 const ref=useRef<HTMLElement>(null)
 useLayoutEffect(()=>{
  const element=ref.current
  if(!element)return
  let active=true
  const fit=()=>{if(active)fitTitle(element)}
  fit()
  const observer=new ResizeObserver(fit)
  observer.observe(element)
  document.fonts.ready.then(fit)
  document.fonts.addEventListener('loadingdone',fit)
  window.addEventListener('resize',fit)
  return()=>{active=false;observer.disconnect();document.fonts.removeEventListener('loadingdone',fit);window.removeEventListener('resize',fit)}
 },[title])
 return <strong ref={ref} className="recent-request-title" title={title}>{title}</strong>
}
