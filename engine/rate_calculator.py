"""
Rate & delta calculator for mongot Prometheus metrics.

Takes raw scraped data + last_scrape snapshot, returns updated pod_prom
and a new snapshot entry for the next cycle.

Isolated from the collection loop so it can be tested independently —
background.py becomes a thin orchestrator with no rate logic inside.

Counter-reset safety:
  - Negative deltas (counter reset to 0 after pod restart) are silently
    skipped via _safe_delta() returning None.
  - Implausibly large QPS spikes (> _MAX_PLAUSIBLE_QPS) are discarded
    and logged at DEBUG level — they indicate a counter reset where the
    new value happens to be higher than the old snapshot (e.g. rapid
    restart + immediate traffic).
  - On the very first cycle (last_s is None) all delta computation is
    skipped and seeded values of 0 are kept — no spurious spikes.
"""

import logging
from typing import Optional

log = logging.getLogger("mongot-monitor.rate_calculator")

# Physical ceiling: no real mongot will serve more than this many QPS.
# Anything above signals a Prometheus counter reset, not real traffic.
_MAX_PLAUSIBLE_QPS = 50_000


def _safe_delta(current: float, prev: float):
    """Return current - prev, or None on negative delta (counter reset)."""
    d = current - prev
    return None if d < 0 else d


def compute_pod_rates(
    pod_key: str,
    pod_prom: dict,
    last_s: Optional[dict],
    now: float,
) -> tuple[dict, dict]:
    """
    Compute all delta-based rates for a single pod.

    Args:
        pod_key:  Pod name (used for logging only).
        pod_prom: Raw Prometheus data from scrape_mongot_prometheus().
        last_s:   Previous scrape snapshot for this pod, or None on first cycle.
        now:      Current epoch timestamp (seconds).

    Returns:
        (updated pod_prom, new_scrape_entry)
        The caller stores new_scrape_entry in last_scrape[pod_key].
    """
    pod_prom.setdefault("categories", {}).setdefault("indexing", {})
    sc  = pod_prom["categories"].setdefault("search_commands", {})
    idx = pod_prom["categories"]["indexing"]

    curr_updates = idx.get("steady_applicable_updates", 0)
    idx.setdefault("steady_applicable_updates_sec", 0.0)

    processed = idx.get("build_docs_processed", 0) or 0

    # ── Build snapshot entry ────────────────────────────────────────────────
    # Always built (even first cycle) so the next cycle has a baseline.
    new_entry: dict = {
        "time":                       now,
        "applicable_updates":         curr_updates,
        "build_docs_processed":       processed,
        "search_total":               sc.get("search_total", 0),
        "search_latency_sum":         sc.get("search_latency_sum", 0),
        "vectorsearch_total":         sc.get("vectorsearch_total", 0),
        "vectorsearch_latency_sum":   sc.get("vectorsearch_latency_sum", 0),
        "candidates_examined":        sc.get("candidates_examined", 0),
        "results_returned":           sc.get("results_returned", 0),
        "scan_ratio_ema":             sc.get("scan_ratio", 0.0),
        "vector_candidates_examined": sc.get("vector_candidates_examined", 0),
        "vector_results_returned":    sc.get("vector_results_returned", 0),
        "vector_scan_ratio_ema":      sc.get("vector_scan_ratio", 0.0),
    }

    # First cycle: no previous data → skip delta computation, compute ETA only
    if last_s is None:
        _compute_eta(idx, now, None)
        return pod_prom, new_entry

    dt = now - last_s["time"]
    if dt <= 0:
        _compute_eta(idx, now, last_s)
        return pod_prom, new_entry

    # ── Indexing: applicable updates / sec ─────────────────────────────────
    du = _safe_delta(curr_updates, last_s.get("applicable_updates", curr_updates))
    if du is not None:
        idx["steady_applicable_updates_sec"] = round(du / dt, 1)

    # ── Search QPS + avg latency ────────────────────────────────────────────
    d_search  = _safe_delta(sc.get("search_total", 0),          last_s.get("search_total", sc.get("search_total", 0)))
    d_vs      = _safe_delta(sc.get("vectorsearch_total", 0),     last_s.get("vectorsearch_total", sc.get("vectorsearch_total", 0)))
    d_sl_sum  = _safe_delta(sc.get("search_latency_sum", 0),     last_s.get("search_latency_sum", sc.get("search_latency_sum", 0)))
    d_vsl_sum = _safe_delta(sc.get("vectorsearch_latency_sum", 0), last_s.get("vectorsearch_latency_sum", sc.get("vectorsearch_latency_sum", 0)))

    if d_search is not None:
        qps = round(d_search / dt, 2)
        if qps <= _MAX_PLAUSIBLE_QPS:
            sc["search_qps"] = qps
            if d_search > 0 and d_sl_sum is not None:
                sc["search_avg_latency_sec"] = round(d_sl_sum / d_search, 4)
        else:
            log.debug(f"[{pod_key}] search_qps spike ({qps:.0f}/s) — counter reset detected, skipping")

    if d_vs is not None:
        vqps = round(d_vs / dt, 2)
        if vqps <= _MAX_PLAUSIBLE_QPS:
            sc["vectorsearch_qps"] = vqps
            if d_vs > 0 and d_vsl_sum is not None:
                sc["vectorsearch_avg_latency_sec"] = round(d_vsl_sum / d_vs, 4)
        else:
            log.debug(f"[{pod_key}] vectorsearch_qps spike ({vqps:.0f}/s) — counter reset detected, skipping")

    # ── Scan ratio (text search) — EMA with low-traffic guard ──────────────
    d_cands = _safe_delta(sc.get("candidates_examined", 0), last_s.get("candidates_examined", sc.get("candidates_examined", 0)))
    d_res   = _safe_delta(sc.get("results_returned", 0),   last_s.get("results_returned",   sc.get("results_returned", 0)))

    if d_cands is not None:
        sc["zero_results_with_candidates"] = (d_res == 0 and d_cands > 0)
        # Guard: skip EMA update if too few results — ratio is noisy at low traffic
        if d_res is not None and d_res >= 10:
            raw_ratio = d_cands / d_res
            prev_ema  = last_s.get("scan_ratio_ema", raw_ratio)
            sc["scan_ratio"] = round(0.3 * raw_ratio + 0.7 * prev_ema, 1)

    # ── Vector scan ratio — EMA ─────────────────────────────────────────────
    d_vcands = _safe_delta(sc.get("vector_candidates_examined", 0), last_s.get("vector_candidates_examined", sc.get("vector_candidates_examined", 0)))
    d_vres   = _safe_delta(sc.get("vector_results_returned", 0),   last_s.get("vector_results_returned",   sc.get("vector_results_returned", 0)))

    if d_vcands is not None and d_vres is not None and d_vres >= 10:
        raw_vratio = d_vcands / d_vres
        prev_vema  = last_s.get("vector_scan_ratio_ema", raw_vratio)
        sc["vector_scan_ratio"] = round(0.3 * raw_vratio + 0.7 * prev_vema, 1)

    # ── Index Build ETA ─────────────────────────────────────────────────────
    _compute_eta(idx, now, last_s)

    # Persist computed EMA values back into the snapshot for next cycle
    new_entry["scan_ratio_ema"]        = sc.get("scan_ratio", 0.0)
    new_entry["vector_scan_ratio_ema"] = sc.get("vector_scan_ratio", 0.0)
    new_entry["build_docs_processed"]  = idx.get("build_docs_processed", 0) or 0

    return pod_prom, new_entry


def _compute_eta(idx: dict, now: float, last_s: Optional[dict]) -> None:
    """Compute Index Build ETA and write it into idx['eta_info']."""
    processed = idx.get("build_docs_processed", 0) or 0
    total     = idx.get("build_docs_total", 0) or 0
    in_prog   = idx.get("initial_sync_in_progress", 0) or 0

    eta_info: dict = {"active": False}
    if in_prog > 0 and total > 0:
        last_s    = last_s or {}
        dt_s      = now - last_s.get("time", now)
        last_proc = last_s.get("build_docs_processed", processed)
        rate      = round((processed - last_proc) / dt_s, 1) if dt_s > 0 else 0.0
        remaining = max(0, total - processed)
        stalled   = rate < 100 and dt_s >= 30
        eta_sec   = round(remaining / rate) if rate >= 100 else None
        eta_info  = {
            "active":       True,
            "processed":    int(processed),
            "total":        int(total),
            "progress_pct": round(processed / total * 100, 1),
            "docs_per_sec": rate,
            "eta_seconds":  eta_sec,
            "stalled":      stalled,
        }

    idx["eta_info"] = eta_info
