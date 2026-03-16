"""
Background metrics collector.
Runs in a daemon thread, populates state.metrics_cache at a fixed interval.
The /metrics endpoint reads from cache without ever blocking on external calls.
"""

import logging
import threading
import time
from datetime import datetime, timezone

import state
from advisor import run_advisor
from collectors.kubernetes import (
    discover_mongodbsearch_crds, discover_operator_info, discover_mongot_pods,
    get_mongot_pvcs, get_mongot_services, get_pod_metrics,
    get_k8s_version, get_helm_releases,
)
from collectors.mongodb import (
    get_mongo_vitals, get_oplog_info, get_search_indexes,
    get_search_perf_from_profiler, get_search_server_params,
)
from collectors.prometheus import scrape_mongot_prometheus

log = logging.getLogger("mongot-monitor.collector")


class BackgroundCollector:
    def __init__(self, interval: int = 5):
        self.interval = interval
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="metrics-collector"
        )

    def start(self) -> None:
        self._thread.start()
        log.info(f"✓ Background collector avviato (ogni {self.interval}s)")

    def _loop(self) -> None:
        while True:
            try:
                self._collect()
            except Exception as e:
                log.error(f"Collection cycle error: {e}")
            time.sleep(self.interval)

    def _collect(self) -> None:
        now = time.time()
        global_errors = []

        # ── MongoDB vitals + per-second rates ─────────────────────────────────
        vitals = get_mongo_vitals(global_errors)

        with state.cache_lock:
            last_m = state.metrics_cache["last_mongo"].copy()

        if "time" in last_m:
            dt = now - last_m["time"]
            if dt > 0:
                vitals["ops_insert_sec"] = max(0, int((vitals["ops_insert"] - last_m["ops_insert"]) / dt))
                vitals["ops_update_sec"] = max(0, int((vitals["ops_update"] - last_m["ops_update"]) / dt))
                vitals["ops_delete_sec"] = max(0, int((vitals["ops_delete"] - last_m["ops_delete"]) / dt))
        else:
            vitals["ops_insert_sec"] = vitals["ops_update_sec"] = vitals["ops_delete_sec"] = 0

        # ── Kubernetes + MongoDB collectors ───────────────────────────────────
        res = {
            "k8s_version": get_k8s_version(),
            "operator": discover_operator_info(global_errors),
            "mongodbsearch_crds": discover_mongodbsearch_crds(global_errors),
            "mongot_pods": discover_mongot_pods(global_errors),
            "mongot_pvcs": get_mongot_pvcs(global_errors),
            "mongot_services": get_mongot_services(global_errors),
            "pod_metrics": get_pod_metrics(),
            "oplog_info": get_oplog_info(global_errors),
            "mongo_vitals": vitals,
            "search_indexes": get_search_indexes(global_errors),
            "search_perf": get_search_perf_from_profiler(global_errors),
            "search_server_params": get_search_server_params(global_errors),
            "helm_releases": get_helm_releases(global_errors),
            "global_errors": global_errors,
            "mongo_connected": state.mongo_client is not None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "_collect_ms": 0,
            "_cached": False,
        }

        # ── Prometheus scraping ────────────────────────────────────────────────
        prom_ports = {
            c["name"]: c.get("prometheus_port", 9946)
            for c in res["mongodbsearch_crds"]
            if c.get("prometheus_enabled")
        }

        with state.cache_lock:
            last_scrape_snapshot = state.metrics_cache["last_scrape"].copy()

        prom_metrics = {}
        new_scrape = {}
        for p in res["mongot_pods"]:
            pod_port = p.get("discovered_prom_port") or next(
                (port for cname, port in prom_ports.items() if cname in p["name"]),
                9946,
            )

            pod_prom = scrape_mongot_prometheus(
                p["name"], p["namespace"], p.get("pod_ip", "127.0.0.1"), pod_port, global_errors
            )

            pod_key = p["name"]
            pod_prom.setdefault("categories", {}).setdefault("indexing", {})
            curr_updates = pod_prom["categories"]["indexing"].get("steady_applicable_updates", 0)
            pod_prom["categories"]["indexing"]["steady_applicable_updates_sec"] = 0.0

            last_s = last_scrape_snapshot.get(pod_key)
            if last_s:
                dt = now - last_s["time"]
                du = curr_updates - last_s["applicable_updates"]
                if dt > 0 and du >= 0:
                    pod_prom["categories"]["indexing"]["steady_applicable_updates_sec"] = round(du / dt, 1)

                # ── Search QPS + avg latency ───────────────────────────────────
                sc = pod_prom["categories"].setdefault("search_commands", {})
                if dt > 0:
                    d_search  = sc.get("search_total", 0) - last_s.get("search_total", sc.get("search_total", 0))
                    d_vs      = sc.get("vectorsearch_total", 0) - last_s.get("vectorsearch_total", sc.get("vectorsearch_total", 0))
                    d_sl_sum  = sc.get("search_latency_sum", 0) - last_s.get("search_latency_sum", sc.get("search_latency_sum", 0))
                    d_vsl_sum = sc.get("vectorsearch_latency_sum", 0) - last_s.get("vectorsearch_latency_sum", sc.get("vectorsearch_latency_sum", 0))

                    if d_search >= 0:
                        sc["search_qps"] = round(d_search / dt, 2)
                        if d_search > 0 and d_sl_sum >= 0:
                            sc["search_avg_latency_sec"] = round(d_sl_sum / d_search, 4)
                    if d_vs >= 0:
                        sc["vectorsearch_qps"] = round(d_vs / dt, 2)
                        if d_vs > 0 and d_vsl_sum >= 0:
                            sc["vectorsearch_avg_latency_sec"] = round(d_vsl_sum / d_vs, 4)

                    # ── Scan ratio (text search efficiency) ───────────────────
                    d_cands = sc.get("candidates_examined", 0) - last_s.get("candidates_examined", sc.get("candidates_examined", 0))
                    d_res   = sc.get("results_returned", 0)   - last_s.get("results_returned",   sc.get("results_returned", 0))
                    if d_cands >= 0:
                        sc["zero_results_with_candidates"] = (d_res == 0 and d_cands > 0)
                        # Skip EMA update if too few results — ratio is noisy at low traffic
                        if d_res >= 10:
                            raw_ratio = d_cands / d_res
                            prev_ema  = last_s.get("scan_ratio_ema", raw_ratio)
                            ema       = round(0.3 * raw_ratio + 0.7 * prev_ema, 1)
                            sc["scan_ratio"] = ema

                    # ── Vector scan ratio (vectorSearch efficiency) ────────────
                    d_vcands = sc.get("vector_candidates_examined", 0) - last_s.get("vector_candidates_examined", sc.get("vector_candidates_examined", 0))
                    d_vres   = sc.get("vector_results_returned", 0)   - last_s.get("vector_results_returned",   sc.get("vector_results_returned", 0))
                    if d_vcands >= 0 and d_vres >= 10:
                        raw_vratio  = d_vcands / d_vres
                        prev_vema   = last_s.get("vector_scan_ratio_ema", raw_vratio)
                        sc["vector_scan_ratio"] = round(0.3 * raw_vratio + 0.7 * prev_vema, 1)

            # ── Index Build ETA ────────────────────────────────────────────────
            idx = pod_prom["categories"]["indexing"]
            processed = idx.get("build_docs_processed", 0) or 0
            total     = idx.get("build_docs_total", 0) or 0
            in_prog   = idx.get("initial_sync_in_progress", 0) or 0

            eta_info = {"active": False}
            if in_prog > 0 and total > 0:
                progress_pct = round(processed / total * 100, 1) if total else 0
                last_s   = last_scrape_snapshot.get(pod_key, {})
                dt_s     = now - last_s.get("time", now)
                last_proc = last_s.get("build_docs_processed", processed)
                rate     = round((processed - last_proc) / dt_s, 1) if dt_s > 0 else 0.0
                remaining = max(0, total - processed)
                stalled  = (rate < 100 and dt_s >= 30)
                eta_sec  = round(remaining / rate) if rate >= 100 else None
                eta_info = {
                    "active":        True,
                    "processed":     int(processed),
                    "total":         int(total),
                    "progress_pct":  progress_pct,
                    "docs_per_sec":  rate,
                    "eta_seconds":   eta_sec,
                    "stalled":       stalled,
                }

            idx["eta_info"] = eta_info
            sc_snap = pod_prom["categories"].get("search_commands", {})
            new_scrape[pod_key] = {
                "time": now,
                "applicable_updates": curr_updates,
                "build_docs_processed": processed,
                "search_total":             sc_snap.get("search_total", 0),
                "search_latency_sum":       sc_snap.get("search_latency_sum", 0),
                "vectorsearch_total":       sc_snap.get("vectorsearch_total", 0),
                "vectorsearch_latency_sum": sc_snap.get("vectorsearch_latency_sum", 0),
                "candidates_examined":         sc_snap.get("candidates_examined", 0),
                "results_returned":           sc_snap.get("results_returned", 0),
                "scan_ratio_ema":             sc_snap.get("scan_ratio", 0.0),
                "vector_candidates_examined": sc_snap.get("vector_candidates_examined", 0),
                "vector_results_returned":    sc_snap.get("vector_results_returned", 0),
                "vector_scan_ratio_ema":      sc_snap.get("vector_scan_ratio", 0.0),
            }
            prom_metrics[pod_key] = pod_prom

        res["mongot_prometheus"] = prom_metrics
        res["_collect_ms"] = round((time.time() - now) * 1000, 1)

        # ── SRE Advisor ────────────────────────────────────────────────────────
        advisor_findings = run_advisor(res)

        # ── Atomic cache update ────────────────────────────────────────────────
        with state.cache_lock:
            state.metrics_cache["data"] = res
            state.metrics_cache["advisor"] = advisor_findings
            state.metrics_cache["timestamp"] = now
            state.metrics_cache["last_mongo"] = {
                "time": now,
                "ops_insert": vitals["ops_insert"],
                "ops_update": vitals["ops_update"],
                "ops_delete": vitals["ops_delete"],
            }
            state.metrics_cache["last_scrape"].update(new_scrape)
