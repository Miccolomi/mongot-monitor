// ── SRE Advisor Panel (thin renderer — logic lives in advisor.py) ──────────

const ADV_CLS  = { pass: 'st-pass', warn: 'st-warn', crit: 'st-crit' };
const ADV_ICON = { pass: '🟢 PASSED', warn: '🟡 WARNING', crit: '🔴 CRIT' };

function buildDiagnosisPanel(findings) {
    if (!findings || findings.length === 0) return '';

    const crits  = findings.filter(f => f.status === 'crit');
    const warns  = findings.filter(f => f.status === 'warn');
    const passes = findings.filter(f => f.status === 'pass');
    const health = crits.length > 0 ? 'critical' : warns.length > 0 ? 'degraded' : 'healthy';
    const color  = health === 'critical' ? '#ff1744' : health === 'degraded' ? '#ffab00' : '#00e676';
    const icon   = health === 'critical' ? '🔴' : health === 'degraded' ? '🟡' : '🟢';

    let h = `<div class="c s4" style="background:#0a0d14;border:1px solid #1a1f2e;padding:20px;">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:18px">
        <div>
          <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:2px;color:#6b7394;margin-bottom:4px">🔬 Automatic Search Diagnosis</div>
          <div style="font-size:15px;font-weight:700;color:${color}">${icon} Cluster Health — ${health.toUpperCase()}</div>
        </div>
        <div style="display:flex;gap:16px;font-size:12px;font-weight:600">
          <span style="color:#ff1744">✖ ${crits.length} critical</span>
          <span style="color:#ffab00">⚠ ${warns.length} warnings</span>
          <span style="color:#00e676">✔ ${passes.length} passed</span>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px">`;

    // Health Summary column
    h += `<div>
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#00e676;margin-bottom:10px;border-bottom:1px solid #1e2740;padding-bottom:6px">Health Summary</div>`;
    passes.forEach(f => {
        h += `<div style="font-size:11px;color:#00e676;padding:3px 0">✔ ${escapeHtml(f.title)}</div>`;
    });
    if (!passes.length) h += `<div style="font-size:11px;color:#6b7394">—</div>`;
    h += `</div>`;

    // Warnings column
    h += `<div>
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#ffab00;margin-bottom:10px;border-bottom:1px solid #1e2740;padding-bottom:6px">Warnings${crits.length ? ' &amp; Critical' : ''}</div>`;
    crits.forEach(f => {
        h += `<div style="margin-bottom:8px">
          <div style="font-size:11px;color:#ff1744;font-weight:600">✖ ${escapeHtml(f.title)}</div>
          <div style="font-size:10px;color:#c9d1e0;margin-top:2px;padding-left:14px">${escapeHtml(f.value)}</div>
        </div>`;
    });
    warns.forEach(f => {
        h += `<div style="margin-bottom:8px">
          <div style="font-size:11px;color:#ffab00;font-weight:600">⚠ ${escapeHtml(f.title)}</div>
          <div style="font-size:10px;color:#c9d1e0;margin-top:2px;padding-left:14px">${escapeHtml(f.value)}</div>
        </div>`;
    });
    if (!crits.length && !warns.length) h += `<div style="font-size:11px;color:#6b7394">No warnings detected.</div>`;
    h += `</div>`;

    // Recommendations column
    const actionable = [...crits, ...warns];
    h += `<div>
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#00b0ff;margin-bottom:10px;border-bottom:1px solid #1e2740;padding-bottom:6px">Recommendations</div>`;
    actionable.forEach(f => {
        h += `<div style="font-size:11px;color:#c9d1e0;padding:3px 0;line-height:1.5">→ ${escapeHtml(f.doc)}</div>`;
    });
    if (!actionable.length) h += `<div style="font-size:11px;color:#6b7394">All checks passed — no action needed.</div>`;
    h += `</div>`;

    h += `</div></div>`;
    return h;
}

function buildAdvisorHTML(findings) {
    if (!findings || findings.length === 0) {
        return `<div class="c s4" style="background:#0a0d14; border:1px solid #1a1f2e; padding:20px;">
                  <h3 style="color:#ffab00; margin-bottom:16px; font-size:14px; letter-spacing:1px;">🏅 COMPLIANCE &amp; BEST PRACTICES ADVISOR</h3>
                  <div class="empty">No advisor data yet.</div>
                </div>`;
    }

    let h = `<div class="c s4" style="background:#0a0d14; border:1px solid #1a1f2e; padding:20px;">
             <h3 style="color:#ffab00; margin-bottom:16px; font-size:14px; letter-spacing:1px;">🏅 COMPLIANCE &amp; BEST PRACTICES ADVISOR</h3>`;

    const last = findings.length - 1;
    findings.forEach((f, i) => {
        const cls = ADV_CLS[f.status] || 'st-pass';
        const ico = ADV_ICON[f.status] || ADV_ICON.pass;
        const style = i === last ? 'border-bottom:none; margin-bottom:0; padding-bottom:0;' : '';
        h += `<div class="adv-card" style="${style}">
                <div class="adv-title">
                  <span>${escapeHtml(f.title)}</span>
                  <span class="${cls}">${ico}</span>
                </div>
                <div class="adv-val"><b>Detected:</b> ${escapeHtml(f.value)}</div>
                <div class="adv-doc">📖 Doc: ${escapeHtml(f.doc)}</div>
              </div>`;
    });

    h += `</div>`;
    return h;
}

// ── Log Intelligence ──────────────────────────────────────────────────────────

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
