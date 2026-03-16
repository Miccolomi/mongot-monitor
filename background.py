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
from engine.rate_calculator import compute_pod_rates

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

            pod_key  = p["name"]
            last_s   = last_scrape_snapshot.get(pod_key)  # None on first cycle → safe

            pod_prom, new_scrape[pod_key] = compute_pod_rates(pod_key, pod_prom, last_s, now)
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
