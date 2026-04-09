"""
API blueprint — all JSON/data endpoints.
/metrics and /healthcheck read from state.metrics_cache (written by BackgroundCollector).
"""

import time

from flask import Blueprint, jsonify, request, Response

import state
from security import is_valid_k8s_name
from status_report import build_text, build_markdown, build_json

api_bp = Blueprint("api", __name__)


# ── Logs ──────────────────────────────────────────────────────────────────────

@api_bp.route("/api/logs/<namespace>/<pod_name>")
def pod_logs(namespace, pod_name):
    if not is_valid_k8s_name(namespace) or not is_valid_k8s_name(pod_name):
        return jsonify({"error": "Invalid namespace or pod name"}), 400
    if not state.k8s_v1:
        return jsonify({"error": "K8s API not available"}), 500
    try:
        logs = state.k8s_v1.read_namespaced_pod_log(
            name=pod_name, namespace=namespace, tail_lines=50
        )
        return jsonify({"logs": logs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_bp.route("/api/download_logs/<namespace>/<pod_name>")
def download_logs(namespace, pod_name):
    if not is_valid_k8s_name(namespace) or not is_valid_k8s_name(pod_name):
        return "Invalid namespace or pod name", 400
    if not state.k8s_v1:
        return "K8s API not available", 500
    try:
        t_param = request.args.get("time", "all")
        lvl_param = request.args.get("level", "all").lower()

        since_sec = {"10m": 600, "1h": 3600, "24h": 86400}.get(t_param)

        if since_sec:
            raw_logs = state.k8s_v1.read_namespaced_pod_log(
                name=pod_name, namespace=namespace, since_seconds=since_sec
            )
        else:
            raw_logs = state.k8s_v1.read_namespaced_pod_log(
                name=pod_name, namespace=namespace
            )

        if lvl_param == "error":
            keywords = ("error", "fatal", "exception", "warning")
            lines = [l for l in raw_logs.splitlines() if any(k in l.lower() for k in keywords)]
            final_log_data = "\n".join(lines) or "No errors detected in this timeframe."
        else:
            final_log_data = raw_logs

        return Response(
            final_log_data,
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename={pod_name}_logs_{t_param}_{lvl_param}.txt"},
        )
    except Exception as e:
        return f"Error: {str(e)}", 500


# ── Advisor ───────────────────────────────────────────────────────────────────

@api_bp.route("/api/advisor")
def advisor():
    with state.cache_lock:
        findings = state.metrics_cache.get("advisor")

    if findings is None:
        return jsonify({"error": "Collector starting, no data yet"}), 503

    return jsonify(findings)


@api_bp.route("/api/diagnose")
def diagnose():
    from advisor import format_diagnosis
    with state.cache_lock:
        findings = state.metrics_cache.get("advisor")

    if findings is None:
        return jsonify({"error": "Collector starting, no data yet"}), 503

    return jsonify(format_diagnosis(findings))


# ── Metrics ───────────────────────────────────────────────────────────────────

@api_bp.route("/metrics")
def metrics():
    with state.cache_lock:
        data = state.metrics_cache["data"]

    if data is None:
        return jsonify({"error": "Collector starting, no data yet"}), 503

    return jsonify(data)


# ── Stable search metrics API (v1) ───────────────────────────────────────────

@api_bp.route("/api/v1/search_metrics")
def search_metrics_v1():
    """
    Stable, versioned JSON API for search performance data.
    Schema is decoupled from internal metric names — safe to consume
    from external tools, dashboards, or CI performance gates.
    """
    with state.cache_lock:
        data = state.metrics_cache["data"]

    if data is None:
        return jsonify({"error": "Collector starting, no data yet"}), 503

    pods_out = {}
    prom_all = data.get("mongot_prometheus") or {}

    for pod in (data.get("mongot_pods") or []):
        name  = pod["name"]
        cats  = (prom_all.get(name) or {}).get("categories", {})
        sc    = cats.get("search_commands", {})
        idx   = cats.get("indexing", {})
        eta   = idx.get("eta_info", {})

        pods_out[name] = {
            "pod": {
                "namespace":      pod.get("namespace"),
                "node":           pod.get("node"),
                "phase":          pod.get("phase"),
                "all_ready":      pod.get("all_ready"),
                "total_restarts": pod.get("total_restarts"),
            },
            "qps": {
                "search":       sc.get("search_qps", 0.0),
                "vectorsearch": sc.get("vectorsearch_qps", 0.0),
            },
            "latency_sec": {
                "search_avg":       sc.get("search_avg_latency_sec", 0.0),
                "search_max":       sc.get("search_latency_sec", 0.0),
                "vectorsearch_avg": sc.get("vectorsearch_avg_latency_sec", 0.0),
                "vectorsearch_max": sc.get("vectorsearch_latency_sec", 0.0),
            },
            "failures": {
                "search":       sc.get("search_failures", 0),
                "vectorsearch": sc.get("vectorsearch_failures", 0),
            },
            "efficiency": {
                "search_scan_ratio":              sc.get("scan_ratio", 0.0),
                "vectorsearch_scan_ratio":        sc.get("vector_scan_ratio", 0.0),
                "hnsw_visited_nodes":             sc.get("hnsw_visited_nodes", 0.0),
                "zero_results_with_candidates":   sc.get("zero_results_with_candidates", False),
            },
            "indexing": {
                "replication_lag_sec":    idx.get("change_stream_lag_sec", 0.0),
                "initial_sync_active":    (idx.get("initial_sync_in_progress", 0) or 0) > 0,
                "updates_per_sec":        idx.get("steady_applicable_updates_sec", 0.0),
                "unexpected_failures":    idx.get("steady_unexpected_failures", 0),
                "eta":                    eta if eta.get("active") else None,
            },
        }

    return jsonify({
        "schema_version": "1",
        "timestamp":      data.get("timestamp"),
        "collect_ms":     data.get("_collect_ms"),
        "pods":           pods_out,
    })


# ── Log Intelligence ──────────────────────────────────────────────────────────

@api_bp.route("/api/logs/analyze/<namespace>/<pod_name>")
def analyze_logs(namespace, pod_name):
    if not is_valid_k8s_name(namespace) or not is_valid_k8s_name(pod_name):
        return jsonify({"error": "Invalid namespace or pod name"}), 400
    if not state.k8s_v1:
        return jsonify({"error": "K8s API not available"}), 500

    from collectors.log_analyzer import analyze_pod_logs, WINDOW_SECONDS
    window = request.args.get("window", "24h")
    if window not in WINDOW_SECONDS:
        return jsonify({"error": f"Invalid window. Valid values: {list(WINDOW_SECONDS.keys())}"}), 400

    result = analyze_pod_logs(pod_name, namespace, state.k8s_v1, window=window)
    return jsonify(result)


# ── Report ────────────────────────────────────────────────────────────────────

@api_bp.route("/api/report")
def report():
    import json as _json

    fmt = request.args.get("format", "text").lower()
    if fmt not in ("text", "markdown", "json"):
        return jsonify({"error": "Invalid format. Use: text, markdown, json"}), 400

    with state.cache_lock:
        data     = state.metrics_cache.get("data")
        findings = state.metrics_cache.get("advisor") or []

    if data is None:
        return jsonify({"error": "Collector starting, no data yet"}), 503

    if fmt == "json":
        return jsonify(build_json(data, findings))

    text = build_markdown(data, findings) if fmt == "markdown" else build_text(data, findings)
    ext  = "md" if fmt == "markdown" else "txt"
    return Response(
        text,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Type": f"text/plain; charset=utf-8"},
    )


# ── Search Index Inspector ────────────────────────────────────────────────────

@api_bp.route("/api/indexes/inspect")
def indexes_inspect():
    if not state.mongo_client:
        return jsonify({"error": "MongoDB not configured"}), 503

    from collectors.index_inspector import inspect_search_indexes, summarize
    reports = inspect_search_indexes(state.mongo_client)
    return jsonify({"summary": summarize(reports), "indexes": reports})


# ── Liveness probe ────────────────────────────────────────────────────────────

@api_bp.route("/healthz")
def healthz():
    """Lightweight liveness probe — returns 200 if Flask is running."""
    return jsonify({"status": "alive"}), 200


# ── Healthcheck ────────────────────────────────────────────────────────────────

@api_bp.route("/healthcheck")
def healthcheck():
    status = {"status": "healthy", "mongo_ping": "ok", "k8s_api": "ok", "metrics_status": "ok"}
    is_unhealthy = False

    if state.mongo_client:
        try:
            t0 = time.time()
            state.mongo_client.admin.command("ping")
            status["mongo_ping"] = f"ok ({round((time.time() - t0) * 1000, 1)}ms)"
        except Exception as e:
            status["mongo_ping"] = f"failed ({e})"
            is_unhealthy = True
    else:
        status["mongo_ping"] = "not_configured"

    if state.k8s_v1:
        try:
            state.k8s_v1.list_namespace(limit=1, _request_timeout=2)
        except Exception as e:
            status["k8s_api"] = f"failed ({e})"
            is_unhealthy = True
    else:
        status["k8s_api"] = "not_configured"

    with state.cache_lock:
        ts = state.metrics_cache["timestamp"]

    now = time.time()
    if ts > 0:
        age = now - ts
        if age > 120:
            status["metrics_status"] = f"stale (last scraped {round(age)}s ago)"
            is_unhealthy = True
        else:
            status["metrics_status"] = f"fresh ({round(age)}s ago)"
    else:
        status["metrics_status"] = "no_data_yet"

    if is_unhealthy:
        status["status"] = "unhealthy"
        return jsonify(status), 503

    return jsonify(status), 200
