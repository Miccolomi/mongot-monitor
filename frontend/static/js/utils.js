// ── Shared UI helpers ─────────────────────────────────
const $=id=>document.getElementById(id);
const fB=b=>{if(b==null||b===0)return'—';if(b>1e9)return(b/1e9).toFixed(2)+' GB';if(b>1e6)return(b/1e6).toFixed(1)+' MB';if(b>1e3)return(b/1e3).toFixed(1)+' KB';return b+' B'};
const fMs=s=>s==null||s===0?'—':s<0.001?'< 1ms':s<1?(s*1000).toFixed(1)+' ms':s.toFixed(3)+' s';
const fN=n=>n==null?'—':typeof n==='number'?n.toLocaleString('it-IT'):n;
const fEta=s=>{if(s==null||s<0)return'—';const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;return h>0?`${h}h ${m}m`:m>0?`${m}m ${sec}s`:`${sec}s`};
const row=(l,v)=>`<div class="row"><span class="row-l">${l}</span><span class="row-v">${v}</span></div>`;

function pill(s){
  let c='p-b'; s=String(s).toUpperCase();
  if(['READY','RUNNING','OK','BOUND'].includes(s)) c='p-ok';
  else if(['PENDING','WAITING'].includes(s)) c='p-w';
  else if(['FAILED','ERROR','TERMINATED'].includes(s)) c='p-e';
  return`<span class="pill ${c}"><span class="pill-d"></span>${s||'?'}</span>`
}

function gaugeRing(pct,label,color,size=80){const r=(size-10)/2,circ=2*Math.PI*r,off=circ*(1-Math.min(pct/100,1));return`<div class="gauge"><svg width="${size}" height="${size}" style="transform:rotate(-90deg)"><circle cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke="#1a1f2e" stroke-width="5"/><circle cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke="${color}" stroke-width="5" stroke-linecap="round" stroke-dasharray="${circ}" stroke-dashoffset="${off}" style="transition:stroke-dashoffset 0.8s ease"/></svg><div style="margin-top:${-size/2-8}px;text-align:center;height:${size/2}px;display:flex;flex-direction:column;justify-content:center;position:relative"><span class="gauge-v" style="color:${color}">${pct.toFixed(0)}<span class="gauge-u">%</span></span></div><span class="gauge-l">${label}</span></div>`}

function mgItem(val,label,color){return`<div class="mg-item"><span class="mg-v" style="color:${color}">${val}</span><span class="mg-l">${label}</span></div>`}

function timeSince(iso){const s=Math.floor((Date.now()-new Date(iso).getTime())/1000);if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m';if(s<86400)return Math.floor(s/3600)+'h';return Math.floor(s/86400)+'d'}
