import { useEffect, useRef } from 'react'

// Schedule after completion; retries back off without overlapping requests.
export function usePolling(task:()=>Promise<void>, delay:()=>number, event:string, connection?:(unavailable:boolean)=>void) {
  const latest=useRef({task,delay,connection});latest.current={task,delay,connection}
  useEffect(()=>{
    let stopped=false, generation=0, failures=0, timer:ReturnType<typeof setTimeout>|undefined
    let running:Promise<void>|undefined
    async function tick(){
      const ticket=++generation
      clearTimeout(timer)
      if(!running) running=latest.current.task().then(()=>{failures=0;latest.current.connection?.(false)}).catch(()=>{failures++;if(failures>=3)latest.current.connection?.(true)}).finally(()=>{running=undefined})
      await running
      if(!stopped && ticket===generation) timer=setTimeout(tick,Math.min(120000,latest.current.delay()*Math.pow(2,Math.min(failures,4))))
    }
    const wake=()=>{void tick()}
    window.addEventListener(event,wake)
    void tick()
    return ()=>{stopped=true;++generation;clearTimeout(timer);window.removeEventListener(event,wake)}
  },[event])
}
