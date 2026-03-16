// ── Main Dashboard Renderer ───────────────────────────
function render(d) {
  const pods=d.mongot_pods||[], crds=d.mongodbsearch_crds||[], op=d.operator||{};
  const pvcs=d.mongot_pvcs||[], svcs=d.mongot_services||[], idxs=d.search_indexes||[];
  const promAll=d.mongot_prometheus||{};
  const anyPod=pods.length>0, allOk=anyPod&&pods.every(p=>p.phase==='Running'&&p.all_ready);

  const sp=$('pill'); sp.className='pill '+(allOk?'p-ok':anyPod?'p-w':'p-e');
  sp.innerHTML=`<span class="pill-d"></span>${allOk?'ALL OK':anyPod?'WARN':'NO PODS'}`;

  // 0. CONNECTION BANNER (above grid)
  const connBanner = document.getElementById('connection-banner');
  if (connBanner) {
      if (d.global_errors && d.global_errors.length > 0) {
          let errs = d.global_errors.map(e => `<li>${escapeHtml(e)}</li>`).join('');
          connBanner.innerHTML = `<div style="background:#ff174411;border:1px solid #ff174466;border-left:4px solid #ff1744;border-radius:10px;padding:12px 16px;margin-bottom:10px;">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px"><span>🚨</span><span style="color:#ff6b6b;font-weight:700;font-size:12px;text-transform:uppercase;letter-spacing:1px">Diagnostic &amp; Connection Errors</span></div>
            <div style="font-size:11px;color:#c9d1e0;margin-bottom:8px">The Python backend detected network or permission failures. Some metrics may be missing:</div>
            <ul style="margin:0;padding:0 0 0 18px;font-size:11px;color:#ffb4b4;line-height:1.8;font-family:monospace">${errs}</ul>
          </div>`;
      } else {
          connBanner.innerHTML = `<div style="background:#00e67611;border:1px solid #00e67644;border-left:4px solid #00e676;border-radius:10px;padding:12px 16px;margin-bottom:10px;display:flex;align-items:center;gap:10px;">
            <span style="font-size:16px">✅</span>
            <div>
              <div style="color:#00e676;font-weight:700;font-size:12px;text-transform:uppercase;letter-spacing:1px">No Errors Detected (All Systems Operational)</div>
              <div style="color:#c9d1e0;font-size:11px;margin-top:2px">All connections (K8s API, MongoDB Auth, Prometheus Scraping) are active and functioning.</div>
            </div>
          </div>`;
      }
  }

  let h='';

  // 1. OPLOG & K8s DISCOVERY
  h+=`<div class="c s2"><div class="c-h"><span>🌍</span><span class="c-t">Global DB Status</span></div>`;
  if(d.oplog_info && d.oplog_info.head_time) {
      h+=row('Oplog Head (Last Write)', `<span style="color:#00e676">${d.oplog_info.head_time}</span>`);
      h+=row('Oplog Window (Max Lag)', `<span class="${d.oplog_info.window_hours<6?'red':d.oplog_info.window_hours<24?'ylw':'grn'}">${d.oplog_info.window_hours} hours</span>`);
  } else h+=row('Oplog Info', '<span style="color:#ffab00">Not available</span>');
  h+=row('MongoDB Conn.', d.mongo_connected?'<span class="grn">Connected</span>':'<span class="red">N/A</span>');
  h+=row('K8s API Conn.', (pods.length||crds.length||op.name)?'<span class="grn">Connected</span>':'<span class="red">N/A</span>');
  h+=row('Collection time',`${d._collect_ms||'?'} ms`);
  h+=`</div>`;

  h+=`<div class="c s2"><div class="c-h"><span>📋</span><span class="c-t">K8s Discovery</span></div>`;
  h+=row('K8s Cluster',`<span class="cyn">${d.k8s_version||'N/A'}</span>`);
  if(op.name) {
      const opVer = op.image && op.image.includes(':') ? op.image.split(':').pop() : 'N/A';
      h+=row('Operator Ver.',`<span class="pur">${opVer}</span>`);
      h+=row('Operator Pod',`${op.name} (${op.replicas||0}/${op.desired||1})`);
      const rpod = op.pod_name || op.name;
      const isop=openLogs.has(rpod);
      h+=`<div style="margin-top:6px;margin-bottom:6px;display:flex;gap:6px">
             <button id="btn-log-${rpod}" class="btn" style="flex:1;font-size:10px;padding:4px" onclick="toggleLogs('${op.namespace}', '${rpod}')">${isop?'▼ Hide Operator Logs':'▶ Show Live Operator Logs'}</button>
             <button onclick="promptDownloadLog('${op.namespace}', '${rpod}')" class="btn" style="padding:4px 8px;font-size:10px;background:#1e3a8a;color:#93c5fd;border-radius:4px;display:flex;align-items:center;">⬇️ Download (.txt)</button>
          </div>`;
      h+=`<pre id="log-${rpod}" class="term" style="display:${isop?'block':'none'};margin-top:4px">${logCache[rpod]||'Loading...'}</pre>`;
  }
  h+=row('CRDs Found',`<span class="pur">${crds.length}</span>`);
  h+=row('mongot Pods',`<span class="blu">${pods.length}</span>`);
  h+=row('Search Indexes',`<span class="grn">${idxs.length}</span>`);
  h+=row('PVC',`${pvcs.length}`) + row('Services',`${svcs.length}`);
  const helm=d.helm_releases||[];
  if (helm.length > 0) {
      helm.forEach(r => {
          const stColor = r.status === 'deployed' ? '#00e676' : '#ff1744';
          h += row(`Helm: ${r.namespace}`, `<span style="color:${stColor}" title="Updated: ${r.modifiedAt_str}">${r.name} (Rev ${r.revision}) - ${r.status}</span>`);
      });
  }
  h+=`</div>`;

  // 2. DIAGNOSIS PANEL + SRE ADVISOR
  h += buildDiagnosisPanel(d._advisor_findings || []);
  h += buildAdvisorHTML(d._advisor_findings || []);

  // 3. PODS & PROMETHEUS METRICS
  const pm=d.pod_metrics||{};

  pods.forEach(p => {
    const isOOM = p.containers.some(c => c.last_reason === 'OOMKilled');
    const m=pm[p.name]||{}, prom=promAll[p.name]||{}, cat=prom.categories||{};
    const sc=cat.search_commands||{}, jvm=cat.jvm||{}, proc=cat.process||{}, mem=cat.memory||{}, dsk=cat.disk||{}, net=cat.network||{}, idx=cat.indexing||{}, luc=cat.lucene_merge||{}, lc=cat.lifecycle||{};

    h+=`<div class="c s4"><div class="c-h"><span>🔍</span><span class="c-t">Pod: ${p.name}</span></div>`;
    h+=`<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px">`;
    h+=`<div class="pod-meta">Node: ${p.node||'—'} &bull; IP: ${p.pod_ip||'—'} &bull; NS: ${p.namespace}</div>`;
    const pTag = isOOM ? '<span class="tag tag-fail">OOMKILLED</span>' : (p.phase==='Running'?'<span class="tag tag-run">Running</span>':'<span class="tag tag-fail">'+p.phase+'</span>');
    h+=`<div style="display:flex;gap:6px">${pTag} ${p.all_ready?pill('READY'):pill('NOT READY')}</div></div>`;

    h+=`<div class="mg">`;
    h+=mgItem(p.start_time?timeSince(p.start_time):'—','Uptime','#00e676');
    h+=mgItem(p.total_restarts,'Restart',p.total_restarts>5?'#ff6b6b':p.total_restarts>0?'#ffab00':'#00e676');
    if(m.cpu_millicores!=null)h+=mgItem(m.cpu_millicores.toFixed(0)+'m','CPU (actual)','#00b0ff');
    if(m.memory_bytes!=null)h+=mgItem(fB(m.memory_bytes),'RAM (actual)','#b388ff');
    if(proc.cpu_usage)h+=mgItem((proc.cpu_usage*100).toFixed(1)+'%','JVM CPU','#00e5ff');
    if(lc.indexes_initialized)h+=mgItem(fN(lc.indexes_initialized),'Init Indexes','#00e676');
    h+=`</div>`;

    if(p.warnings && p.warnings.length > 0) {
        h += `<div class="warn-box"><strong style="color:#ffab00">⚠️ Latest K8s Events:</strong><br>`;
        p.warnings.forEach(w => { h += `&bull; <b>${w.reason}</b>: ${w.message} <i style="color:#6b7394">(${w.count}x)</i><br>`; });
        h += `</div>`;
    }

    // Live Logs + Log Intelligence
    const isLogOpen = openLogs.has(p.name);
    h += `<div style="display:flex;gap:6px;margin-top:10px;flex-wrap:wrap">
            <button id="btn-log-${p.name}" class="btn" style="flex:1;min-width:140px" onclick="toggleLogs('${p.namespace}', '${p.name}')">${isLogOpen ? '▼ Hide Logs' : '▶ Show Live Pod Logs'}</button>
            <button onclick="promptDownloadLog('${p.namespace}', '${p.name}')" class="btn" style="padding:6px 12px;background:#1e3a8a;color:#93c5fd;border-radius:4px;">⬇️ Download</button>
            <div style="display:flex;gap:4px;align-items:center">
              <select id="win-${p.name}" style="padding:5px 8px;font-size:11px;font-weight:600;border-radius:6px;border:1px solid #7c4dff44;background:#7c4dff18;color:#b388ff;cursor:pointer;font-family:'JetBrains Mono',monospace;outline:none">
                <option value="1h">Last 1h</option>
                <option value="24h" selected>Last 24h</option>
                <option value="7d">Last 7d</option>
                <option value="30d">Last 30d</option>
              </select>
              <button onclick="runLogAnalysis('${p.namespace}','${p.name}')" class="btn" style="background:#7c4dff18;border:1px solid #7c4dff44;color:#b388ff;white-space:nowrap">🔍 Analyze Logs</button>
            </div>
          </div>`;
    h += `<pre id="log-${p.name}" class="term" style="display:${isLogOpen ? 'block' : 'none'}">${logCache[p.name] || 'Loading...'}</pre>`;
    h += `<div id="log-analysis-${p.name}" style="display:none;margin-top:10px"></div>`;

    if(!prom.available){
      h+=`<div style="margin-top:14px;font-size:11px;color:#ff6b6b">No Prometheus metrics found. Fallbacks (Net, Proxy, Exec) failed.</div></div>`;
      return;
    }

    h+=`<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:14px">`;

    // Search Commands
    h+=`<div style="background:#0a0d14;border-radius:8px;padding:12px;border:1px solid #1a1f2e">`;
    h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#00b0ff;margin-bottom:8px">🔎 Search Commands</div>`;
    // QPS prominently
    const sqps=sc.search_qps||0, vsqps=sc.vectorsearch_qps||0;
    h+=`<div style="display:flex;gap:8px;margin-bottom:8px">`;
    h+=mgItem(sqps.toFixed(2)+' /s','$search QPS',sqps>0?'#00e676':'#6b7394');
    h+=mgItem(vsqps.toFixed(2)+' /s','$vecSearch QPS',vsqps>0?'#00e5ff':'#6b7394');
    h+=`</div>`;
    h+=row('$search avg lat.',`<span class="${sc.search_avg_latency_sec>0.5?'red':sc.search_avg_latency_sec>0.1?'ylw':'grn'}">${fMs(sc.search_avg_latency_sec)}</span>`);
    h+=row('$search max lat.',`<span class="${sc.search_latency_sec>0.5?'red':sc.search_latency_sec>0.1?'ylw':'grn'}">${fMs(sc.search_latency_sec)}</span>`);
    h+=row('$search failures',`<span class="${sc.search_failures>0?'red':'grn'}">${fN(sc.search_failures)}</span>`);
    h+=row('$vecSearch avg lat.',`<span class="${sc.vectorsearch_avg_latency_sec>1?'red':sc.vectorsearch_avg_latency_sec>0.3?'ylw':'grn'}">${fMs(sc.vectorsearch_avg_latency_sec)}</span>`);
    h+=row('$vecSearch max lat.',`<span class="${sc.vectorsearch_latency_sec>1?'red':sc.vectorsearch_latency_sec>0.3?'ylw':'grn'}">${fMs(sc.vectorsearch_latency_sec)}</span>`);
    h+=row('$vecSearch fail',`<span class="${sc.vectorsearch_failures>0?'red':'grn'}">${fN(sc.vectorsearch_failures)}</span>`);
    h+=row('getMores latency',`<span class="blu">${fMs(sc.getmores_latency_sec)}</span>`);
    h+=row('manageIndex lat.',`<span class="blu">${fMs(sc.manage_index_latency_sec)}</span>`);
    // Search Efficiency (scan ratio + vector + HNSW)
    const sr   = sc.scan_ratio        || 0;
    const vsr  = sc.vector_scan_ratio || 0;
    const hnsw = sc.hnsw_visited_nodes || 0;
    const hasEfficiency = sr > 0 || vsr > 0 || hnsw > 0;
    if (hasEfficiency) {
        h += `<div style="border-top:1px solid #1a1f2e;margin:6px 0 4px"></div>`;
        h += `<div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#6b7394;margin-bottom:4px">Index Efficiency (EMA)</div>`;
        if (sr > 0) {
            const srColor = sr > 500 ? '#ff1744' : sr > 50 ? '#ffab00' : '#00e676';
            const srLabel = sr > 500 ? 'CRITICAL' : sr > 50 ? 'Inefficient' : sr > 5 ? 'Normal' : 'Excellent';
            h += row('$search scan ratio', `<span style="color:${srColor};font-weight:700">${sr.toFixed(1)}:1</span> <span style="color:${srColor};font-size:10px">(${srLabel})</span>`);
        }
        if (vsr > 0) {
            const vsrColor = vsr > 500 ? '#ff1744' : vsr > 50 ? '#ffab00' : '#00e676';
            const vsrLabel = vsr > 500 ? 'CRITICAL' : vsr > 50 ? 'Inefficient' : vsr > 5 ? 'Normal' : 'Excellent';
            h += row('$vectorSearch ratio', `<span style="color:${vsrColor};font-weight:700">${vsr.toFixed(1)}:1</span> <span style="color:${vsrColor};font-size:10px">(${vsrLabel})</span>`);
        }
        if (hnsw > 0) {
            const hnswColor = hnsw > 5000 ? '#ff1744' : hnsw > 1000 ? '#ffab00' : '#00e676';
            const hnswLabel = hnsw > 5000 ? 'ANN inefficient' : hnsw > 1000 ? 'Costly' : hnsw > 200 ? 'Normal' : 'Excellent';
            h += row('HNSW visited nodes', `<span style="color:${hnswColor};font-weight:700">${fN(Math.round(hnsw))}</span> <span style="color:${hnswColor};font-size:10px">(${hnswLabel})</span>`);
        }
        if (sc.zero_results_with_candidates) {
            h += `<div style="font-size:10px;color:#ffab00;margin-top:4px">⚠ Zero results with candidates examined — check post-search $match or scoring threshold</div>`;
        }
    }
    h+=`</div>`;

    // JVM Heap
    const heapPct=jvm.heap_max_bytes>0?(jvm.heap_used_bytes/jvm.heap_max_bytes)*100:0;
    h+=`<div style="background:#0a0d14;border-radius:8px;padding:12px;border:1px solid #1a1f2e">`;
    h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#b388ff;margin-bottom:8px">☕ JVM Heap &amp; GC</div>`;
    h+=`<div style="display:flex;justify-content:center;margin-bottom:6px">${gaugeRing(heapPct,'Heap Used',heapPct>85?'#ff1744':heapPct>65?'#ffab00':'#b388ff',70)}</div>`;
    h+=row('Used',`<span class="pur">${fB(jvm.heap_used_bytes)}</span>`);
    h+=row('Max',fB(jvm.heap_max_bytes));
    h+=row('GC pause max',`<span class="${jvm.gc_pause_seconds_max>0.5?'red':jvm.gc_pause_seconds_max>0.1?'ylw':'grn'}">${fMs(jvm.gc_pause_seconds_max)}</span>`);
    h+=row('Buffer used',fB(jvm.buffer_used_bytes));
    h+=`</div>`;

    // Indexing Pipeline
    h+=`<div style="background:#0a0d14;border-radius:8px;padding:12px;border:1px solid #1a1f2e">`;
    h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#00e676;margin-bottom:8px">📥 Indexing Pipeline</div>`;
    h+=row('Indexes in catalog',`<span class="grn">${fN(idx.indexes_in_catalog)}</span>`);
    h+=row('Applied CS updates',`<span class="grn">${fN(idx.steady_applicable_updates)}</span>`);
    h+=row('Batches in progress',`<span class="cyn">${fN(idx.steady_batches_in_progress)}</span>`);
    h+=row('Oplog Lag',`<span class="${idx.change_stream_lag_sec>5?'red':'grn'}">${fN(idx.change_stream_lag_sec)} s</span>`);
    h+=row('Unexpected failures',`<span class="${idx.steady_unexpected_failures>0?'red':'grn'}">${fN(idx.steady_unexpected_failures)}</span>`);
    h+=row('Active initial syncs',`<span class="blu">${fN(idx.initial_sync_in_progress)}</span>`);
    h+=`</div>`;

    // Index Build ETA (shown only during active initial sync)
    const eta = idx.eta_info || {};
    if (eta.active) {
        const pct = eta.progress_pct || 0;
        const barColor = eta.stalled ? '#ff1744' : pct > 75 ? '#00e676' : '#ffab00';
        const etaLabel = eta.stalled
            ? '<span style="color:#ff1744;font-weight:700">⚠ INDEX BUILD STALLED (rate &lt; 100 docs/s)</span>'
            : eta.eta_seconds != null
                ? `<span style="color:#00e676">ETA: ${fEta(eta.eta_seconds)}</span>`
                : '<span style="color:#ffab00">Calculating ETA…</span>';
        h += `<div style="background:#0a0d14;border-radius:8px;padding:12px;border:2px solid ${barColor}44;grid-column:span 3">`;
        h += `<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:${barColor};margin-bottom:8px">⚙️ Index Build in Progress</div>`;
        h += `<div style="background:#1a1f2e;border-radius:4px;height:10px;margin-bottom:8px;overflow:hidden">`;
        h += `<div style="background:${barColor};width:${pct}%;height:100%;border-radius:4px;transition:width .5s"></div></div>`;
        h += `<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">`;
        h += `<div style="font-size:11px;color:#c9d1e0">${fN(eta.processed)} / ${fN(eta.total)} docs &nbsp;<span style="color:#6b7394">(${pct}%)</span></div>`;
        h += `<div style="font-size:11px">${etaLabel}</div>`;
        h += `<div style="font-size:11px;color:#6b7394">${fN(eta.docs_per_sec)} docs/s</div>`;
        h += `</div></div>`;
    }

    // System Disk
    const diskPct=dsk.data_path_total_bytes>0?((dsk.data_path_total_bytes-dsk.data_path_free_bytes)/dsk.data_path_total_bytes)*100:0;
    h+=`<div style="background:#0a0d14;border-radius:8px;padding:12px;border:1px solid #1a1f2e">`;
    h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#ffab00;margin-bottom:8px">💾 Disk (data path)</div>`;
    h+=`<div style="display:flex;justify-content:center;margin-bottom:6px">${gaugeRing(diskPct,'Disk Used',diskPct>90?'#ff1744':diskPct>75?'#ffab00':'#00e676',70)}</div>`;
    h+=row('Used',`<span class="ylw">${fB(dsk.data_path_total_bytes-dsk.data_path_free_bytes)}</span>`);
    h+=row('Total',fB(dsk.data_path_total_bytes));
    h+=row('Read I/O',fB(dsk.read_bytes));
    h+=row('Write I/O',fB(dsk.write_bytes));
    h+=row('Queue len',`<span class="${dsk.queue_length>5?'red':dsk.queue_length>1?'ylw':'grn'}">${fN(dsk.queue_length)}</span>`);
    h+=`</div>`;

    // Lucene Merge Scheduler
    h+=`<div style="background:#0a0d14;border-radius:8px;padding:12px;border:1px solid #1a1f2e">`;
    h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#00e5ff;margin-bottom:8px">🔀 Lucene Merges</div>`;
    h+=row('Active merges',`<span class="cyn">${fN(luc.running_merges)}</span>`);
    h+=row('Merging docs',`<span class="blu">${fN(luc.merging_docs)}</span>`);
    h+=row('Total merges',fN(luc.total_merges));
    h+=row('Merge time max',`<span class="ylw">${fMs(luc.merge_time_sec_max)}</span>`);
    h+=row('Discarded merges',`<span class="${luc.discarded_merges>0?'ylw':'grn'}">${fN(luc.discarded_merges)}</span>`);
    h+=`</div>`;

    // System Memory + Network
    const memPct=mem.phys_total_bytes>0?(mem.phys_inuse_bytes/mem.phys_total_bytes)*100:0;
    h+=`<div style="background:#0a0d14;border-radius:8px;padding:12px;border:1px solid #1a1f2e">`;
    h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#ff6b6b;margin-bottom:8px">🖥 System &amp; Network</div>`;
    h+=row('RAM used',`<span class="${memPct>90?'red':memPct>75?'ylw':'grn'}">${fB(mem.phys_inuse_bytes)} (${memPct.toFixed(0)}%)</span>`);
    h+=row('Swap used',fB(mem.swap_inuse_bytes));
    h+=`<div style="border-top:1px solid #1a1f2e;margin:4px 0;padding-top:4px"></div>`;
    h+=row('Net recv',`<span class="blu">${fB(net.bytes_recv)}</span>`);
    h+=row('Net sent',`<span class="grn">${fB(net.bytes_sent)}</span>`);
    h+=row('Net errors',`<span class="${(net.in_errors+net.out_errors)>0?'red':'grn'}">${fN(net.in_errors+net.out_errors)}</span>`);
    h+=`</div>`;
    h+=`</div>`;

    // Atlas Search Sync Pipeline Analyzer
    h += buildPipelineHTML(p, promAll, d);

    h+=`</div>`; // close .c.s4 pod card
  });

  if(!pods.length) h+=`<div class="c s4"><div class="empty">No mongot pod found</div></div>`;

  // Search Indexes table
  h+=`<div class="c s4"><div class="c-h"><span>📑</span><span class="c-t">Search Indexes (${idxs.length})</span></div>`;
  if(idxs.length){h+=`<table><thead><tr><th>Name</th><th>Collection</th><th>Type</th><th>Status</th><th>Queryable</th><th>Documents</th></tr></thead><tbody>`;
  idxs.forEach(i=>{const v=i.type==='vectorSearch';h+=`<tr><td style="font-weight:600;color:#e8ecf4">${i.name}</td><td style="font-size:11px">${i.ns}</td><td><span class="tag ${v?'tag-v':'tag-f'}">${v?'VECTOR':'FULL-TEXT'}</span></td><td>${pill(i.status)}</td><td>${i.queryable?'<span class="grn">✓</span>':'<span class="red">✗</span>'}</td><td>${i.num_docs!=null?fN(i.num_docs):'—'}</td></tr>`});
  h+=`</tbody></table>`}else{h+=`<div class="empty">No search index found in the database</div>`}
  h+=`</div>`;

  // PVC & Services
  if(pvcs.length||svcs.length){
    h+=`<div class="c s4"><div class="c-h"><span>💾</span><span class="c-t">Storage &amp; Services</span></div>`;
    pvcs.forEach(p=>{h+=`<div style="display:flex;justify-content:space-between;font-size:11px;padding:3px 0;border-bottom:1px solid #111827"><span style="color:#e8ecf4">📦 ${p.name} <span style="color:#6b7394;margin-left:8px">(SC: ${p.storage_class || 'N/A'})</span></span><span>${pill(p.status)} <span class="blu">${p.capacity}</span></span></div>`});
    svcs.forEach(s=>{const pts=(s.ports||[]).map(p=>`${p.port}`).join(',');h+=`<div style="display:flex;justify-content:space-between;font-size:11px;padding:3px 0;border-bottom:1px solid #111827"><span style="color:#e8ecf4">🔗 SVC: ${s.name}</span><span><span class="tag tag-v">${s.type}</span> Port(s) :${pts}</span></div>`});
    h+=`</div>`;
  }

  $('grid').innerHTML=h;

  // Refresh open log panels
  openLogs.forEach(pod => {
      let p = pods.find(x => x.name === pod);
      if(!p && op.pod_name && op.pod_name === pod) p = op;
      else if(!p && op.name === pod) p = op;
      if(p) fetchAndUpdateLog(p.namespace, p.name || p.pod_name);
  });
}

// ── Health Banner ─────────────────────────────────────
function renderHealthBanner(findings) {
    const banner = document.getElementById('health-banner');
    if (!banner) return;
    if (!findings || findings.length === 0) { banner.innerHTML = ''; return; }

    const crits  = findings.filter(f => f.status === 'crit');
    const warns  = findings.filter(f => f.status === 'warn');
    const passes = findings.filter(f => f.status === 'pass');

    const health  = crits.length > 0 ? 'critical' : warns.length > 0 ? 'degraded' : 'healthy';
    const cls     = `hb hb-${health}`;
    const icon    = health === 'critical' ? '🔴' : health === 'degraded' ? '🟡' : '🟢';
    const color   = health === 'critical' ? '#ff1744' : health === 'degraded' ? '#ffab00' : '#00e676';
    const label   = health.toUpperCase();

    const recs = [...crits, ...warns].slice(0, 3)
        .map(f => `<span>→ ${escapeHtml(f.doc)}</span>`).join('');
    const recsHtml = recs ? `<div class="hb-recs">${recs}</div>` : '';

    banner.innerHTML = `
      <div class="${cls}">
        <div class="hb-left">
          <span class="hb-icon">${icon}</span>
          <div>
            <div class="hb-title" style="color:${color}">Cluster Health — ${label}</div>
            <div class="hb-sub">Automatic Search Diagnosis &bull; ${findings.length} checks run</div>
          </div>
        </div>
        <div class="hb-counts">
          <span style="color:#ff1744">✖ ${crits.length} critical</span>
          <span style="color:#ffab00">⚠ ${warns.length} warnings</span>
          <span style="color:#00e676">✔ ${passes.length} passed</span>
        </div>
        ${recsHtml}
      </div>`;
}

// ── Polling ───────────────────────────────────────────
let iv;
function setR(){if(iv)clearInterval(iv);iv=setInterval(fetchM,+$('rr').value*1000)}

async function fetchM(){
    try{
        const [metricsResp, advisorResp] = await Promise.all([
            fetch('/metrics'),
            fetch('/api/advisor')
        ]);
        const d = await metricsResp.json();
        if(d.error) {
            $('err').style.display='block';
            $('err').textContent='⚠ Error Backend: ' + d.error;
            return;
        }
        if(advisorResp.ok) {
            d._advisor_findings = await advisorResp.json();
        }
        $('err').style.display='none';
        render(d);
    }catch(e){
        $('err').style.display='block';
        $('err').textContent='⚠ Network / Connection failed: Unable to contact the Python server ('+e.message+')';
    }
}

fetchM();
setR();
