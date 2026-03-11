// ── Atlas Search Sync Pipeline Analyzer ──────────────
function buildPipelineHTML(p, promAll, d) {
    const prom = promAll[p.name] || {};
    const cat = prom.categories || {};
    const idx = cat.indexing || {};
    const jvm = cat.jvm || {};
    const dsk = cat.disk || {};
    const luc = cat.lucene_merge || {};

    const m_urlParams = new URLSearchParams(window.location.search);
    const mergeThreshold = parseFloat(m_urlParams.get('merge_threshold')) || 3.0;

    const vitals = d.mongo_vitals || {};
    let lag_sec = idx.change_stream_lag_sec || 0;
    let lag_str = `${lag_sec.toFixed(1)}s`; let lag_color = "#00e676";
    if (lag_sec > 120) { lag_str = `${lag_sec.toFixed(1)}s delay`; lag_color = "#ff1744"; }
    else if (lag_sec > 15) { lag_str = `${lag_sec.toFixed(1)}s delay`; lag_color = "#ffab00"; }
    else if (lag_sec > 0.5) { lag_str = `${lag_sec.toFixed(1)}s`; lag_color = "#ffeb3b"; }

    // 1. Oplog Stream bottleneck
    let stream_cls = (idx.steady_batches_in_progress > 2 || lag_sec > 30) ? 'pn-warn' : 'pn-ok';
    if(lag_sec > 120 && idx.steady_applicable_updates == 0) stream_cls = 'pn-crit';

    // 2. RAM Parsing bottleneck
    let ram_cls = jvm.heap_used_bytes > (jvm.heap_max_bytes * 0.85) ? 'pn-crit' : 'pn-ok';
    let ram_alert_html = '';
    if (idx.steady_batch_sec_max > 2.0) {
        ram_cls = idx.steady_batch_sec_max > 5.0 ? 'pn-crit' : 'pn-warn';
        let av_cls = idx.steady_batch_sec_max > 5.0 ? 'crit-val' : 'warn-val';
        ram_alert_html = `<span class="${av_cls}">⏳ SLOW: ${idx.steady_batch_sec_max.toFixed(1)}s</span>
                          ${idx.steady_batch_sec_max > 5.0 ? '<div class="crit-badge">BOTTLENECK!</div>' : ''}`;
    }

    // 3. Lucene Disk IO bottleneck
    let disk_cls = luc.running_merges > 0 && dsk.queue_length > 2 ? 'pn-warn' : 'pn-ok';
    let disk_alert_html = '';
    if(luc.merge_time_sec_max > (mergeThreshold * 0.5)) {
        disk_cls = luc.merge_time_sec_max > mergeThreshold ? 'pn-crit' : 'pn-warn';
        let d_cls = luc.merge_time_sec_max > mergeThreshold ? 'crit-val' : 'warn-val';
        disk_alert_html = `<span class="${d_cls}">⏳ SLOW: ${luc.merge_time_sec_max.toFixed(1)}s</span>
                           ${luc.merge_time_sec_max > mergeThreshold ? '<div class="crit-badge">BOTTLENECK!</div>' : ''}`;
    }

    return `<div class="pipe-box">
          <div class="pipe-tit">
            <span>🚀 Sync Pipeline Analyzer</span>
            <div>
              <span style="color:#facc15; font-size:10px; margin-right:15px; font-weight:normal; cursor:pointer;" onclick="let t=prompt('Enter new Merge threshold in sec:', '${mergeThreshold}'); if(t) window.location.search='?merge_threshold='+t;">
                Merge alarm threshold: <b>${mergeThreshold}s (edit)</b>
              </span>
              <span style="color:${lag_color}">Lag Search Sync: <b>${lag_str}</b></span>
            </div>
          </div>
          <div class="pipe-flow">
            <div class="pipe-line"></div>

            <div class="pipe-node-wrapper" title="Connessioni db: ${vitals.connections_active} / Lock attivi: ${vitals.active_writers}">
              <div class="pipe-node pn-ok">
                <span class="pipe-lbl">MongoDB</span>
                <span class="pipe-val" style="font-size:14px">Oplog</span>
                <span class="pipe-sub">Conn: ${fN(vitals.connections_active)} | Lcks: ${vitals.active_writers || 0}</span>
                <span class="pipe-sub" style="color:#00e676; font-size:10px; margin-top:6px; font-weight:bold;">Write Ops: + ${fN(vitals.ops_insert_sec + vitals.ops_update_sec + vitals.ops_delete_sec)}/s</span>
              </div>
              <div class="pipe-desc">Data origin.<br>Records every database edittion.</div>
            </div>

            <div class="pipe-node-wrapper">
              <div class="pipe-node ${stream_cls}">
                <span class="pipe-lbl">Stream</span>
                <span class="pipe-val">${fN(idx.steady_applicable_updates)} <span style="font-size:10px; font-weight:normal; color:#94a3b8">Total</span></span>
                <span class="pipe-sub" style="color:#00e676; font-size:11px; font-weight:bold; margin-top:6px">+ ${fN(idx.steady_applicable_updates_sec || 0)}/s</span>
              </div>
              <div class="pipe-desc">Real-time reading.<br>Captures data from the Oplog.</div>
            </div>

            <div class="pipe-node-wrapper">
              <div class="pipe-node ${ram_cls}">
                <span class="pipe-lbl">RAM Parse</span>
                <span class="pipe-val" style="font-size:14px">${fB(jvm.heap_used_bytes)}</span>
                <span class="pipe-sub">on ${fB(jvm.heap_max_bytes)} Heap</span>
                <span class="pipe-sub" style="color:#facc15; font-size:10px; margin-top:6px">${(idx.steady_batch_sec_max * 1000).toFixed(0)} ms lat | CPU: ${(promAll[p.name]?.categories?.process?.cpu_usage || 0).toFixed(1)}%</span>
                ${ram_alert_html}
              </div>
              <div class="pipe-desc">JVM usage.<br>Delays if CPU or RAM saturate.</div>
            </div>

            <div class="pipe-node-wrapper" title="Merge: background disk defragmentation. Even under presonre (red), docs may be searchable in RAM segments. Doesn't necessarily mean user-facing lag.">
              <div class="pipe-node ${disk_cls}">
                <span class="pipe-lbl">Lucene Merge</span>
                <span class="pipe-val">${fN(luc.total_merges)}</span>
                <span class="pipe-sub">Total runs</span>
                <span class="pipe-sub" style="color:#00b8d4; font-size:10px; margin-top:6px; font-weight:bold;">Disk Queue: ${fN(promAll[p.name]?.categories?.disk?.queue_length || 0)}</span>
                ${disk_alert_html}
              </div>
              <div class="pipe-desc">Disk write.<br>Merges data into the Lucene index.</div>
            </div>

            <div class="pipe-node-wrapper" title="Search Sync Lag: actual time between MongoDB write and search availability. Also spans RAM segments before disk merge!">
              <div class="pipe-node ${lag_sec>30?'pn-warn':'pn-ok'}">
                <span class="pipe-lbl">$search</span>
                <span class="pipe-val" style="font-size:14px">${lag_sec>30?'OLD':'READY'}</span>
                <span class="pipe-sub">Query: ${(promAll[p.name]?.categories?.search_commands?.search_latency_sec * 1000 || 0).toFixed(0)} ms</span>
                <span class="pipe-sub" style="color:#ea15f2; font-size:10px; margin-top:4px; font-weight:bold;">AI Vector: ${(promAll[p.name]?.categories?.search_commands?.vectorsearch_latency_sec * 1000 || 0).toFixed(0)} ms</span>
              </div>
              <div class="pipe-desc">Atlas Search index.<br>Client response times.</div>
            </div>
          </div>
        </div>`;
}
