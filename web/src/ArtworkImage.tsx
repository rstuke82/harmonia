import {ImgHTMLAttributes,ReactNode,useEffect,useRef,useState} from 'react'

// Image elements do not expose HTTP status. Retry local proxy failures twice;
// external missing covers use the placeholder immediately. Never loop forever.
export default function ArtworkImage({src,fallback,...props}:ImgHTMLAttributes<HTMLImageElement>&{fallback?:ReactNode}){
 const [attempt,setAttempt]=useState(0),[failed,setFailed]=useState(false)
 const timer=useRef<ReturnType<typeof setTimeout>|undefined>(undefined)
 const generation=useRef(0)
 useEffect(()=>{generation.current++;setAttempt(0);setFailed(false);clearTimeout(timer.current);return()=>{generation.current++;clearTimeout(timer.current)}},[src])
 if(!src||failed)return fallback||<span className="album-art-placeholder" aria-hidden="true">♫</span>
 const local=src.startsWith('/api/'),url=attempt&&local?`${src}${src.includes('?')?'&':'?'}retry=${attempt}`:src
 return <img {...props} src={url} onLoad={event=>{clearTimeout(timer.current);props.onLoad?.(event)}} onError={event=>{props.onError?.(event);if(!local||attempt>=2){setFailed(true);return}const token=generation.current;clearTimeout(timer.current);timer.current=setTimeout(()=>{if(token===generation.current)setAttempt(value=>value+1)},attempt===0?2000:5000)}}/>
}
