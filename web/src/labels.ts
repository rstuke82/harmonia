/** Presentation labels only; persisted match provenance and API keys stay compatible. */
export const matchLabel=(value:string)=>({automatic:'Auto',manual:'Manual',legacy:'Saved',saved:'Saved',unresolved:'Missing',unmatched:'Missing',missing:'Missing',lost:'LOST',ignored:'Ignored'}[String(value).toLowerCase()]||value)
export const matchStatusKey=(label:string)=>({Auto:'automatic',Manual:'manual',Saved:'legacy',Missing:'unresolved',LOST:'lost',Ignored:'ignored'}[label]||label.toLowerCase())
export const displayText=(value?:string)=>String(value||'').replace(/\bunresolved\b/gi,'missing').replace(/\bunmatched\b/gi,'missing').replace(/\blegacy\b/gi,'saved')
