/** Keep upstream diagnostic payloads readable without losing the original detail. */
export function readableError(value:unknown){
 const raw=typeof value==='string'?value:JSON.stringify(value),text=raw||'The operation could not be completed.'
 let summary=text
 const start=text.indexOf('{')
 if(start>=0){try{const data=JSON.parse(text.slice(start));summary=text.slice(0,start)+(data.message||data.error||data.detail||'The service returned an error.')}catch{}}
 summary=summary.replace(/\\u([0-9a-f]{4})/gi,(_,n)=>String.fromCharCode(parseInt(n,16))).split(/\n\s*(?:at |Traceback|File ")|--- End of stack trace|\s+at [\w.]+\(/)[0].trim()
 if(summary.length>420)summary=summary.slice(0,417)+'…'
 const next=/permission|access denied/i.test(summary)?'Check the service permissions before retrying.':/503|502|SkyHook|LidarrAPI/i.test(summary)?'The metadata service may be unavailable. Try again later.':/timed out|timeout|unconfirmed/i.test(summary)?'Check the operation in the service before retrying.':''
 return {summary,details:text,next}
}
export default function ErrorNotice({error}:{error:unknown}){const {summary,details,next}=readableError(error);return <div className="error-notice"><p className="error" role="alert">{summary}</p>{next&&<p className="muted">{next}</p>}{details!==summary&&<details><summary>Technical details</summary><pre>{details}</pre></details>}</div>}
