import { ReactNode, useLayoutEffect, useRef } from 'react'
import { createPortal } from 'react-dom'

/** Render above glass/transform containing blocks, with one viewport and focus owner. */
export default function Modal({children,onClose,busy=false}:{children:ReactNode;onClose:()=>void;busy?:boolean}){
 const layer=useRef<HTMLDivElement>(null),close=useRef(onClose),blocked=useRef(busy)
 close.current=onClose;blocked.current=busy
 useLayoutEffect(()=>{
  const previous=document.activeElement as HTMLElement|null,root=document.getElementById('root')
  const previousLayer=Array.from(document.querySelectorAll<HTMLElement>('[data-modal-layer]')).filter(el=>el!==layer.current).at(-1),previousLayerInert=previousLayer?.inert||false
  if(previousLayer)previousLayer.inert=true
  const overflow=document.body.style.overflow,wasInert=root?.inert||false
  document.body.style.overflow='hidden';if(root)root.inert=true
  const dialog=layer.current?.querySelector<HTMLElement>('[role="dialog"]')
  if(dialog)dialog.tabIndex=-1
  const focusables=()=>Array.from(layer.current?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]')||[]).filter(el=>el.getClientRects().length>0)
  ;(focusables()[0]||dialog)?.focus({preventScroll:true})
  const keyboard=(event:KeyboardEvent)=>{
   if(Array.from(document.querySelectorAll('[data-modal-layer]')).at(-1)!==layer.current)return
   if(event.key==='Escape'){event.preventDefault();if(!blocked.current)close.current()}
   if(event.key==='Tab'){
    const items=focusables(),first=items[0],last=items.at(-1)
    if(!first){event.preventDefault();dialog?.focus({preventScroll:true});return}
    if(event.shiftKey&&(document.activeElement===first||!items.includes(document.activeElement as HTMLElement))){event.preventDefault();last?.focus()}
    else if(!event.shiftKey&&(document.activeElement===last||!items.includes(document.activeElement as HTMLElement))){event.preventDefault();first.focus()}
   }
  }
  const viewport=window.visualViewport
  const resize=()=>{if(layer.current&&viewport)Object.assign(layer.current.style,{height:`${viewport.height}px`,width:`${viewport.width}px`,top:`${viewport.offsetTop}px`,left:`${viewport.offsetLeft}px`})}
  resize();viewport?.addEventListener('resize',resize);viewport?.addEventListener('scroll',resize)
  document.addEventListener('keydown',keyboard)
  return()=>{if(previousLayer)previousLayer.inert=previousLayerInert;document.body.style.overflow=overflow;if(root)root.inert=wasInert;document.removeEventListener('keydown',keyboard);viewport?.removeEventListener('resize',resize);viewport?.removeEventListener('scroll',resize);if(previous?.isConnected)previous.focus({preventScroll:true})}
 },[])
 return createPortal(<div data-modal-layer className="modal-backdrop" ref={layer} onClick={e=>{if(e.target===e.currentTarget&&!blocked.current)close.current()}}>{children}</div>,document.body)
}
