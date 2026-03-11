// ── Live Log Management ───────────────────────────────
let openLogs = new Set();
let logCache = {};

async function toggleLogs(ns, pod) {
  if(openLogs.has(pod)) {
      openLogs.delete(pod);
      if($(`log-${pod}`)) $(`log-${pod}`).style.display = 'none';
      if($(`btn-log-${pod}`)) $(`btn-log-${pod}`).innerText = pod.includes('operator') ? '▶ Show Live Operator Logs' : '▶ Show Live Pod Logs';
  } else {
      openLogs.add(pod);
      if($(`log-${pod}`)) {
          $(`log-${pod}`).style.display = 'block';
          $(`log-${pod}`).innerText = "Loading...";
      }
      if($(`btn-log-${pod}`)) $(`btn-log-${pod}`).innerText = pod.includes('operator') ? '▼ Hide Operator Logs' : '▼ Hide Logs';
      await fetchAndUpdateLog(ns, pod);
  }
}

async function fetchAndUpdateLog(ns, pod) {
  if(!openLogs.has(pod)) return;
  try {
      const r = await fetch(`/api/logs/${ns}/${pod}`);
      const d = await r.json();
      logCache[pod] = d.logs || "No logs available.";
      const el = $(`log-${pod}`);
      if(el) {
          const cTop = el.scrollTop, cH = el.scrollHeight, cClient = el.clientHeight;
          const atBot = cTop + cClient >= cH - 15;
          el.textContent = logCache[pod];
          el.scrollTop = atBot ? el.scrollHeight : cTop;
      }
  } catch(e) {
      if($(`log-${pod}`)) $(`log-${pod}`).innerHTML = `<span style="color:red">Error: ${e.message}</span>`;
  }
}

function promptDownloadLog(ns, pod) {
    let t = prompt(`How many logs do you want to download for ${pod}?\nOptions: 10m (last 10 mins), 1h (last hour), 24h, all\n`, "1h");
    if (!t) return;
    const t_param = ['10m','1h','24h'].includes(t) ? t : 'all';
    let filterErr = confirm(`Do you want to extract ONLY rows containing errors (Error, Fatal, Exception)?\n\n[OK] = Errors Only\n[Cancel] = Full Log`);
    let lvl = filterErr ? 'error' : 'all';
    window.open(`/api/download_logs/${ns}/${pod}?time=${t_param}&level=${lvl}`, '_blank');
}
