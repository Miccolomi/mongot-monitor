// ── SRE Advisor Panel (thin renderer — logic lives in advisor.py) ──────────

const ADV_CLS  = { pass: 'st-pass', warn: 'st-warn', crit: 'st-crit' };
const ADV_ICON = { pass: '🟢 PASSED', warn: '🟡 WARNING', crit: '🔴 CRIT' };

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

function escapeHtml(s) {
    if (!s) return '';
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}
