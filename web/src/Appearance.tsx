import { useEffect, useState } from 'react'
const key='playlist-bridge-appearance'
type Theme={mode:string;color:string}
export function readTheme():Theme{try{const saved=JSON.parse(localStorage.getItem(key)||'{}');return {mode:['light','dark','system'].includes(saved.mode)?saved.mode:'system',color:/^#[0-9a-f]{6}$/i.test(saved.color)?saved.color:'#287fff'}}catch{return {mode:'system',color:'#287fff'}}}
export function applyTheme(theme=readTheme()){
 const dark=theme.mode==='dark'||theme.mode==='system'&&matchMedia('(prefers-color-scheme: dark)').matches
 document.documentElement.dataset.theme=dark?'dark':'light'
 document.documentElement.style.setProperty('--accent',theme.color)
 const rgb=[1,3,5].map(i=>parseInt(theme.color.slice(i,i+2),16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4)
 document.documentElement.style.setProperty('--accent-text',.2126*rgb[0]+.7152*rgb[1]+.0722*rgb[2]>.179?'#07101b':'#ffffff')
}
export function ThemeListener(){useEffect(()=>{const system=matchMedia('(prefers-color-scheme: dark)');const update=()=>applyTheme();update();system.addEventListener('change',update);window.addEventListener('storage',update);return()=>{system.removeEventListener('change',update);window.removeEventListener('storage',update)}},[]);return null}
export default function Appearance(){const [theme,setTheme]=useState(readTheme),[error,setError]=useState('');function update(next:Theme){setTheme(next);applyTheme(next);try{localStorage.setItem(key,JSON.stringify(next));setError('')}catch{setError('Your browser could not save this preference. It applies for this session.')}}return <section className="panel settings-panel"><h2>Appearance</h2><p className="muted">Choose how Harmonia looks on this device.</p><div className="appearance-row"><label>Display mode</label><div className="segmented">{[['system','Follow System'],['light','Light'],['dark','Dark']].map(([mode,label])=><button key={mode} aria-pressed={theme.mode===mode} onClick={()=>update({...theme,mode})}>{label}</button>)}</div></div><div className="appearance-row"><label>Main color</label><div className="color-options">{['#287fff','#7255dc','#d53c81','#bf4c12','#15825c','#466775'].map(color=><button key={color} className="color-swatch" style={{background:color}} aria-label={`Use ${color} accent`} aria-pressed={theme.color===color} onClick={()=>update({...theme,color})}/>)}<input type="color" title="Custom main color" aria-label="Custom main color" value={theme.color} onChange={e=>update({...theme,color:e.target.value})}/></div></div>{error&&<p role="status">{error}</p>}</section>}
