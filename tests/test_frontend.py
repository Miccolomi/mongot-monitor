"""
Phase 1 tests — Frontend extraction.

Verify that:
  1. The dashboard route serves an HTML page via render_template (no inline CSS/JS).
  2. All static assets (CSS + JS files) are referenced in the template.
  3. Every static file is actually served by Flask with the correct content.
  4. Backend API endpoints (metrics, healthcheck, logs, favicon) still work.
"""

JS_FILES = ["utils.js", "logs.js", "advisor.js", "pipeline.js", "render.js"]


# ── Dashboard route ────────────────────────────────────────────────────────────

def test_dashboard_returns_200(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_dashboard_content_type_is_html(client):
    resp = client.get("/")
    assert "text/html" in resp.content_type


def test_dashboard_has_page_title(client):
    resp = client.get("/")
    assert b"Mongot Ultimate Monitor" in resp.data


def test_dashboard_has_no_inline_style_block(client):
    """CSS must come from an external file, not an inline <style> block."""
    resp = client.get("/")
    assert b"<style>" not in resp.data


def test_dashboard_has_no_inline_js(client):
    """JS must come from external files — no inline logic in the HTML."""
    resp = client.get("/")
    html = resp.data
    assert b"const fB=" not in html
    assert b"function render(" not in html
    assert b"function buildAdvisorHTML(" not in html


def test_dashboard_references_css_file(client):
    resp = client.get("/")
    assert b"css/main.css" in resp.data


def test_dashboard_references_all_js_files(client):
    resp = client.get("/")
    html = resp.data
    for js in JS_FILES:
        assert js.encode() in html, f"dashboard.html does not reference {js}"


# ── Static CSS ─────────────────────────────────────────────────────────────────

def test_static_css_served_with_200(client):
    resp = client.get("/static/css/main.css")
    assert resp.status_code == 200


def test_static_css_content_type(client):
    resp = client.get("/static/css/main.css")
    assert "text/css" in resp.content_type


def test_static_css_has_body_rule(client):
    resp = client.get("/static/css/main.css")
    assert b"body{" in resp.data


def test_static_css_has_pipe_box_rule(client):
    """Sync Pipeline specific styles must be present."""
    resp = client.get("/static/css/main.css")
    assert b".pipe-box{" in resp.data


def test_static_css_has_advisor_rules(client):
    resp = client.get("/static/css/main.css")
    assert b".adv-card{" in resp.data


# ── Static JS files ────────────────────────────────────────────────────────────

def test_all_js_files_served_with_200(client):
    for js in JS_FILES:
        resp = client.get(f"/static/js/{js}")
        assert resp.status_code == 200, f"{js} returned {resp.status_code}"


def test_utils_exports_fB(client):
    resp = client.get("/static/js/utils.js")
    assert b"const fB=" in resp.data


def test_utils_exports_fMs(client):
    resp = client.get("/static/js/utils.js")
    assert b"const fMs=" in resp.data


def test_utils_exports_fN(client):
    resp = client.get("/static/js/utils.js")
    assert b"const fN=" in resp.data


def test_utils_exports_row(client):
    resp = client.get("/static/js/utils.js")
    assert b"const row=" in resp.data


def test_utils_has_pill_function(client):
    resp = client.get("/static/js/utils.js")
    assert b"function pill(" in resp.data


def test_utils_has_gaugeRing(client):
    resp = client.get("/static/js/utils.js")
    assert b"function gaugeRing(" in resp.data


def test_utils_has_mgItem(client):
    resp = client.get("/static/js/utils.js")
    assert b"function mgItem(" in resp.data


def test_utils_has_timeSince(client):
    resp = client.get("/static/js/utils.js")
    assert b"function timeSince(" in resp.data


def test_logs_has_openLogs(client):
    resp = client.get("/static/js/logs.js")
    assert b"openLogs" in resp.data


def test_logs_has_toggleLogs(client):
    resp = client.get("/static/js/logs.js")
    assert b"async function toggleLogs(" in resp.data


def test_logs_has_fetchAndUpdateLog(client):
    resp = client.get("/static/js/logs.js")
    assert b"async function fetchAndUpdateLog(" in resp.data


def test_logs_has_promptDownloadLog(client):
    resp = client.get("/static/js/logs.js")
    assert b"function promptDownloadLog(" in resp.data


def test_advisor_has_buildAdvisorHTML(client):
    resp = client.get("/static/js/advisor.js")
    assert b"function buildAdvisorHTML(" in resp.data


def test_advisor_js_is_thin_renderer(client):
    """advisor.js should be a thin renderer — no business logic keywords."""
    resp = client.get("/static/js/advisor.js")
    assert b"function buildAdvisorHTML(" in resp.data
    assert b"escapeHtml" in resp.data
    # Logic now lives in advisor.py, not in the JS
    assert b"200%" not in resp.data
    assert b"OOMKilled" not in resp.data


def test_advisor_api_returns_503_without_data(client):
    """/api/advisor returns 503 when collector hasn't run yet."""
    resp = client.get("/api/advisor")
    assert resp.status_code == 503


def test_pipeline_has_buildPipelineHTML(client):
    resp = client.get("/static/js/pipeline.js")
    assert b"function buildPipelineHTML(" in resp.data


def test_pipeline_uses_mergeThreshold(client):
    resp = client.get("/static/js/pipeline.js")
    assert b"mergeThreshold" in resp.data


def test_render_has_render_function(client):
    resp = client.get("/static/js/render.js")
    assert b"function render(" in resp.data


def test_render_calls_buildAdvisorHTML(client):
    resp = client.get("/static/js/render.js")
    assert b"buildAdvisorHTML(" in resp.data


def test_render_calls_buildPipelineHTML(client):
    resp = client.get("/static/js/render.js")
    assert b"buildPipelineHTML(" in resp.data


def test_render_has_fetchM(client):
    resp = client.get("/static/js/render.js")
    assert b"async function fetchM(" in resp.data


def test_render_has_setR(client):
    resp = client.get("/static/js/render.js")
    assert b"function setR(" in resp.data


def test_render_no_inline_style(client):
    """render.js must not contain CSS — that belongs in main.css."""
    resp = client.get("/static/js/render.js")
    assert b"<style>" not in resp.data


# ── Backend API endpoints ──────────────────────────────────────────────────────

def test_metrics_returns_503_without_data(client):
    """Without a running BackgroundCollector the cache is empty → 503."""
    resp = client.get("/metrics")
    assert resp.status_code == 503


def test_metrics_returns_200_with_populated_cache(metrics_client):
    resp = metrics_client.get("/metrics")
    assert resp.status_code == 200


def test_metrics_returns_json(metrics_client):
    resp = metrics_client.get("/metrics")
    data = resp.get_json()
    assert isinstance(data, dict)


def test_metrics_has_expected_keys(metrics_client):
    resp = metrics_client.get("/metrics")
    data = resp.get_json()
    for key in ("mongot_pods", "search_indexes", "mongo_connected", "global_errors"):
        assert key in data, f"Missing key '{key}' in /metrics response"


def test_metrics_mongo_connected_is_false_without_uri(metrics_client):
    resp = metrics_client.get("/metrics")
    data = resp.get_json()
    assert data["mongo_connected"] is False


def test_metrics_pods_empty_without_k8s(metrics_client):
    resp = metrics_client.get("/metrics")
    data = resp.get_json()
    assert data["mongot_pods"] == []


def test_healthcheck_returns_json(client):
    resp = client.get("/healthcheck")
    data = resp.get_json()
    assert "status" in data
    assert "mongo_ping" in data
    assert "k8s_api" in data


def test_healthcheck_not_configured_without_clients(client):
    resp = client.get("/healthcheck")
    data = resp.get_json()
    assert data["mongo_ping"] == "not_configured"
    assert data["k8s_api"] == "not_configured"


def test_favicon_returns_204(client):
    resp = client.get("/favicon.ico")
    assert resp.status_code == 204


def test_logs_api_returns_500_without_k8s(client):
    resp = client.get("/api/logs/default/some-pod")
    assert resp.status_code == 500
    data = resp.get_json()
    assert "error" in data


def test_download_logs_returns_500_without_k8s(client):
    resp = client.get("/api/download_logs/default/some-pod")
    assert resp.status_code == 500
