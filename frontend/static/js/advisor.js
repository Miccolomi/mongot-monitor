// ── SRE Advisor — unified panel ───────────────────────────────────────────────

// Per-check extras: official docs link + actionable commands
const ADVISOR_EXTRAS = {
    disk_200_rule: {
        link: 'https://www.mongodb.com/docs/atlas/atlas-search/manage-indexes/',
        link_label: 'Search Storage Requirements',
        commands: ['kubectl exec <pod> -n mongodb -- df -h /var/lib/mongot']
    },
    index_consolidation: {
        link: 'https://www.mongodb.com/docs/atlas/atlas-search/create-index/',
        link_label: 'Search Index Best Practices',
        commands: ['db.collection.getSearchIndexes()']
    },
    io_bottleneck: {
        link: 'https://www.mongodb.com/docs/kubernetes-operator/stable/tutorial/storage/',
        link_label: 'MCK Storage Configuration',
        commands: ['kubectl get pvc -n mongodb', 'kubectl describe pvc <pvc-name> -n mongodb']
    },
    cpu_qps: {
        link: 'https://www.mongodb.com/docs/atlas/atlas-search/tune-search-performance/',
        link_label: 'Tune Search Performance',
        commands: ['kubectl top pods -n mongodb', 'kubectl describe pod <pod> -n mongodb']
    },
    page_faults: {
        link: 'https://www.mongodb.com/docs/atlas/atlas-search/tune-search-performance/#memory',
        link_label: 'Memory Tuning for Search',
        commands: ["kubectl describe pod <pod> -n mongodb | grep -A5 'Limits'"]
    },
    oom_risk: {
        link: 'https://www.mongodb.com/docs/atlas/atlas-search/tune-search-performance/#memory',
        link_label: 'JVM Heap & Memory Configuration',
        commands: [
            "kubectl describe pod <pod> -n mongodb | grep -A10 'Last State'",
            'kubectl edit deployment mongot-doctor -n mongodb  # update memory limits'
        ]
    },
    crd_status: {
        link: 'https://www.mongodb.com/docs/kubernetes-operator/stable/troubleshooting/',
        link_label: 'MCK Troubleshooting Guide',
        commands: [
            'kubectl get mongodbsearch -n mongodb',
            'kubectl describe mongodbsearch <name> -n mongodb',
            'kubectl logs deployment/mongodb-enterprise-operator -n mongodb | tail -50'
        ]
    },
    storage_class: {
        link: 'https://www.mongodb.com/docs/kubernetes-operator/stable/tutorial/storage/',
        link_label: 'Recommended Storage Classes',
        commands: ['kubectl get pvc -n mongodb -o wide', 'kubectl get storageclass']
    },
    versioning: {
        link: 'https://www.mongodb.com/docs/kubernetes-operator/stable/upgrade/',
        link_label: 'MCK Upgrade Guide',
        commands: ["kubectl get deployment -n mongodb -o jsonpath='{.items[*].spec.template.spec.containers[0].image}'"]
    },
    skip_auth_search: {
        link: 'https://www.mongodb.com/docs/manual/reference/parameters/#mongodb-parameter-param.skipAuthenticationToSearchIndexManagementServer',
        link_label: 'skipAuthenticationToSearchIndexManagementServer Docs',
        commands: ['db.adminCommand({ setParameter: 1, skipAuthenticationToSearchIndexManagementServer: false })']
    },
    search_tls_mode: {
        link: 'https://www.mongodb.com/docs/manual/reference/parameters/#mongodb-parameter-param.searchTLSMode',
        link_label: 'searchTLSMode Configuration',
        commands: ["db.adminCommand({ setParameter: 1, searchTLSMode: 'requireTLS' })"]
    },
    oplog_window: {
        link: 'https://www.mongodb.com/docs/manual/core/replica-set-oplog/#oplog-size',
        link_label: 'Oplog Size Configuration',
        commands: [
            'db.getReplicationInfo()  // check current oplog window',
            'db.adminCommand({ replSetResizeOplog: 1, size: 51200 })  // resize to 50 GB'
        ]
    },
    ram_index_ratio: {
        link: 'https://www.mongodb.com/docs/atlas/atlas-vector-search/vector-search-overview/',
        link_label: 'Vector Search Memory Requirements',
        commands: ['kubectl top pods -n mongodb', "kubectl describe pod <pod> -n mongodb | grep -A5 'Limits'"]
    },
    lifecycle_failures: {
        link: 'https://www.mongodb.com/docs/atlas/atlas-search/troubleshoot-atlas-search/',
        link_label: 'Search Troubleshooting Guide',
        commands: [
            "kubectl logs <pod> -n mongodb | grep -iE 'fail|error' | tail -50",
            'kubectl exec <pod> -n mongodb -- df -h  // check disk space'
        ]
    },
    scan_ratio: {
        link: 'https://www.mongodb.com/docs/atlas/atlas-search/tune-search-performance/#improve-query-performance',
        link_label: 'Improve Search Query Performance',
        commands: ['db.collection.getSearchIndexes()  // review index definition and analyzer']
    },
    vector_scan_ratio: {
        link: 'https://www.mongodb.com/docs/atlas/atlas-vector-search/vector-search-type/',
        link_label: 'Vector Search Index Parameters',
        commands: ['db.collection.getSearchIndexes()  // check efSearch, efConstruction, m']
    },
    hnsw_nodes: {
        link: 'https://www.mongodb.com/docs/atlas/atlas-vector-search/vector-search-type/',
        link_label: 'HNSW Graph Parameters',
        commands: ['db.collection.getSearchIndexes()  // review numDimensions, efConstruction, m']
    },
};

// Replaces both buildDiagnosisPanel and buildAdvisorHTML
function buildAdvisorHTML(findings) {
    if (!findings || findings.length === 0) {
        return `<div class="c s4" style="background:#0a0d14;border:1px solid #1a1f2e;padding:20px;">
                  <div style="color:#ffab00;font-size:14px;font-weight:700;letter-spacing:1px;margin-bottom:16px">🏅 SRE ADVISOR</div>
                  <div class="empty">No advisor data yet.</div>
                </div>`;
    }

    const crits  = findings.filter(f => f.status === 'crit');
    const warns  = findings.filter(f => f.status === 'warn');
    const passes = findings.filter(f => f.status === 'pass');
    const health = crits.length > 0 ? 'critical' : warns.length > 0 ? 'degraded' : 'healthy';
    const hColor = health === 'critical' ? '#ff1744' : health === 'degraded' ? '#ffab00' : '#00e676';
    const hIcon  = health === 'critical' ? '🔴' : health === 'degraded' ? '🟡' : '🟢';
    const hBorder = health === 'critical' ? '#ff174433' : health === 'degraded' ? '#ffab0033' : '#00e67633';

    let h = `<div class="c s4" style="background:#0a0d14;border:1px solid ${hBorder};border-top:3px solid ${hColor};padding:20px;">`;

    // ── Header ────────────────────────────────────────────────────────────────
    h += `<div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:20px">
        <div>
          <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:2px;color:#6b7394;margin-bottom:4px">🏅 SRE Advisor</div>
          <div style="font-size:16px;font-weight:700;color:${hColor}">${hIcon} Cluster Health — ${health.toUpperCase()}</div>
        </div>
        <div style="display:flex;gap:20px;font-size:12px;font-weight:700">
          <span style="color:#ff1744">✖ ${crits.length} critical</span>
          <span style="color:#ffab00">⚠ ${warns.length} warnings</span>
          <span style="color:#00e676">✔ ${passes.length} passed</span>
        </div>
      </div>`;

    // ── crit + warn findings — expanded ───────────────────────────────────────
    [...crits, ...warns].forEach(f => {
        const isCrit  = f.status === 'crit';
        const fColor  = isCrit ? '#ff1744' : '#ffab00';
        const fBg     = isCrit ? '#ff174408' : '#ffab0008';
        const fIcon   = isCrit ? '🔴' : '🟡';
        const extras  = ADVISOR_EXTRAS[f.id] || {};
        const cmds    = extras.commands || [];

        h += `<div style="background:${fBg};border:1px solid ${fColor}33;border-left:3px solid ${fColor};border-radius:8px;padding:14px;margin-bottom:12px">`;

        // Title + badge
        h += `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <span style="font-size:13px;font-weight:700;color:${fColor}">${fIcon} ${escapeHtml(f.title)}</span>
            <span style="font-size:10px;background:${fColor}22;color:${fColor};border:1px solid ${fColor}44;border-radius:4px;padding:2px 8px;font-weight:700">${isCrit ? 'CRITICAL' : 'WARNING'}</span>
          </div>`;

        // Detected value
        h += `<div style="font-size:11px;color:#c9d1e0;margin-bottom:10px;line-height:1.5">${escapeHtml(f.value)}</div>`;

        // Why it matters
        h += `<div style="margin-bottom:10px">
            <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#6b7394;margin-bottom:4px">📋 Why it matters</div>
            <div style="font-size:11px;color:#8892a4;line-height:1.6">${escapeHtml(f.doc)}</div>
          </div>`;

        // Recommended actions + commands
        if (cmds.length) {
            h += `<div style="margin-bottom:10px">
              <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#00b0ff;margin-bottom:6px">⚡ Recommended actions</div>`;
            cmds.forEach(cmd => {
                h += `<div style="background:#080b12;border:1px solid #1a1f2e;border-radius:4px;padding:6px 10px;font-size:10px;color:#c9d1e0;font-family:'JetBrains Mono',monospace;margin-bottom:4px;user-select:all">${escapeHtml(cmd)}</div>`;
            });
            h += `</div>`;
        }

        // Docs link
        if (extras.link) {
            h += `<div style="font-size:10px">
              <a href="${extras.link}" target="_blank" rel="noopener" style="color:#00b0ff;text-decoration:none">
                📖 ${escapeHtml(extras.link_label || 'Official Documentation')} ↗
              </a>
            </div>`;
        }

        h += `</div>`;
    });

    // ── pass findings — compact rows ──────────────────────────────────────────
    if (passes.length) {
        h += `<div style="border-top:1px solid #1a1f2e;padding-top:14px;margin-top:4px">
            <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#00e676;margin-bottom:10px">✔ Passing Checks</div>
            <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:4px">`;
        passes.forEach(f => {
            h += `<div style="font-size:11px;color:#00e676;padding:4px 8px;background:#00e67608;border-radius:4px;border:1px solid #00e67622">
                ✔ <span style="font-weight:600">${escapeHtml(f.title)}</span>
                <span style="color:#6b7394;font-size:10px;display:block;padding-left:14px;margin-top:1px">${escapeHtml(f.value)}</span>
              </div>`;
        });
        h += `</div></div>`;
    }

    h += `</div>`;
    return h;
}

// Kept for backwards compatibility — now a no-op (panel merged into buildAdvisorHTML)
function buildDiagnosisPanel() { return ''; }

// ── Log Intelligence ──────────────────────────────────────────────────────────

async function runLogIntelligence() {
    const podSel = document.getElementById('li-pod');
    const winSel = document.getElementById('li-win');
    const results = document.getElementById('li-results');
    if (!podSel || !results) return;

    const pod  = podSel.value;
    const win  = winSel ? winSel.value : '24h';
    const ns   = podSel.options[podSel.selectedIndex]?.dataset?.ns || 'mongodb';

    results.innerHTML = `<span style="color:#b388ff">⏳ Analyzing logs for <b>${escapeHtml(pod)}</b> (${win})…</span>`;

    try {
        const r    = await fetch(`/api/logs/analyze/${encodeURIComponent(ns)}/${encodeURIComponent(pod)}?window=${win}`);
        const data = await r.json();
        results.innerHTML = buildLogAnalysisHTML(data);
    } catch(e) {
        results.innerHTML = `<span style="color:#ff6b6b">Error: ${e.message}</span>`;
    }
}

async function runLogAnalysis(namespace, pod) {
    const panel = document.getElementById(`log-analysis-${pod}`);
    const winEl = document.getElementById(`win-${pod}`);
    if (!panel) return;
    const window = winEl ? winEl.value : '24h';

    panel.style.display = 'block';
    panel.innerHTML = `<div style="background:#0a0d14;border:1px solid #7c4dff44;border-radius:8px;padding:14px;font-size:11px;color:#b388ff">
      ⏳ Analyzing logs (last ${window})…</div>`;

    try {
        const r = await fetch(`/api/logs/analyze/${encodeURIComponent(namespace)}/${encodeURIComponent(pod)}?window=${window}`);
        const data = await r.json();
        panel.innerHTML = buildLogAnalysisHTML(data);
    } catch(e) {
        panel.innerHTML = `<div style="color:#ff6b6b;font-size:11px;padding:8px">Error: ${e.message}</div>`;
    }
}

function buildLogAnalysisHTML(data) {
    if (data.error) {
        return `<div style="background:#ff174411;border:1px solid #ff174444;border-radius:8px;padding:12px;font-size:11px;color:#ff6b6b">
          ✖ Log fetch error: ${escapeHtml(data.error)}</div>`;
    }

    const ICONS = { crit: '🔴', warn: '🟡', info: '🔵' };
    const COLORS = { crit: '#ff1744', warn: '#ffab00', info: '#00b0ff' };
    const windowLabel = { '1h': 'last 1 hour', '24h': 'last 24 hours', '7d': 'last 7 days', '30d': 'last 30 days' };

    const findings = data.findings || [];
    const hasIssues = findings.some(f => f.severity !== 'info');

    let h = `<div style="background:#0a0d14;border:1px solid #7c4dff44;border-radius:8px;padding:16px">`;
    h += `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:8px">
      <div>
        <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:2px;color:#b388ff">🔍 Log Intelligence — ${escapeHtml(data.pod)}</div>
        <div style="font-size:10px;color:#6b7394;margin-top:2px">${escapeHtml(windowLabel[data.window] || data.window)} &bull; ${data.lines_analyzed} JSON lines analyzed</div>
      </div>
      <span style="font-size:11px;font-weight:700;color:${hasIssues ? '#ffab00' : '#00e676'}">${hasIssues ? '⚠ Issues detected' : '✔ No issues detected'}</span>
    </div>`;

    if (!findings.length) {
        h += `<div style="font-size:11px;color:#6b7394;padding:8px 0">No known patterns detected in this time window.</div>`;
    } else {
        findings.forEach(f => {
            const color = COLORS[f.severity] || '#6b7394';
            const icon  = ICONS[f.severity] || '•';
            h += `<div style="margin-bottom:12px;padding-bottom:12px;border-bottom:1px dashed #1e2740">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
                <span style="font-size:12px;font-weight:700;color:${color}">${icon} ${escapeHtml(f.name)}</span>
                <span style="font-size:10px;background:${color}22;color:${color};border:1px solid ${color}44;border-radius:4px;padding:1px 7px;font-weight:700">${f.count}x</span>
              </div>
              <div style="font-size:11px;color:#c9d1e0;margin-bottom:6px">${escapeHtml(f.description)}</div>`;
            if (f.examples && f.examples.length) {
                h += `<div style="background:#080b12;border-radius:4px;padding:6px 8px;font-size:10px;color:#6b7394;font-family:monospace;line-height:1.6">`;
                f.examples.forEach(ex => { h += `<div>${escapeHtml(ex)}</div>`; });
                h += `</div>`;
            }
            h += `</div>`;
        });
    }

    h += `</div>`;
    return h;
}

function escapeHtml(s) {
    if (!s) return '';
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}
