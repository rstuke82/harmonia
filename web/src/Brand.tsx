/** Compact emblem with the lettering derived from the original logo. */
export default function Brand({wordmark=false}:{wordmark?:boolean}){
 return <span className={`bridge-brand ${wordmark?'with-wordmark':''}`}><img className="bridge-logo harmonia-logo" src="/harmonia-mark.png" alt={wordmark?'':'Harmonia'}/>{wordmark&&<img className="harmonia-nav-wordmark" src="/harmonia-sidebar-wordmark.png" alt="Harmonia"/>}</span>
}
