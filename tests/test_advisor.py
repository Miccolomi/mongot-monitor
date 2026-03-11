"""
Unit tests for the SRE Advisor engine (advisor.py).
Each check is tested in isolation with minimal synthetic snapshots.
"""

import pytest
from advisor import run_advisor, _fmt_bytes


# ── Helpers ───────────────────────────────────────────────────────────────────

def _snap(**kwargs):
    """Build a minimal metrics snapshot with optional overrides."""
    base = {
        "mongot_pods": [],
        "mongot_prometheus": {},
        "search_indexes": [],
        "mongot_pvcs": [],
        "mongodbsearch_crds": [],
        "operator": {},
        "k8s_version": "v1.28.0",
        "search_perf": {"queries_per_sec": 0, "total_queries_5m": 0},
        "oplog_info": {},
    }
    base.update(kwargs)
    return base


def _pod(name="pod-0", cpu_limit=2.0, mem_limit=4 * 1024 ** 3, containers=None):
    return {
        "name": name,
        "cpu_limit_cores": cpu_limit,
        "memory_limit_bytes": mem_limit,
        "containers": containers or [],
    }


def _prom(pod_name, disk=None, jvm=None, process=None, indexing=None, memory=None):
    return {pod_name: {"categories": {
        "disk":    disk    or {},
        "jvm":     jvm     or {},
        "process": process or {},
        "indexing": indexing or {},
        "memory":  memory  or {},
    }}}


def _find(findings, id_):
    return next((f for f in findings if f["id"] == id_), None)


# ── run_advisor() ─────────────────────────────────────────────────────────────

def test_run_advisor_returns_list():
    findings = run_advisor(_snap())
    assert isinstance(findings, list)


def test_run_advisor_findings_have_required_keys():
    findings = run_advisor(_snap())
    for f in findings:
        for key in ("id", "title", "status", "value", "doc"):
            assert key in f, f"Missing key '{key}' in finding {f}"


def test_run_advisor_status_values_are_valid():
    findings = run_advisor(_snap())
    for f in findings:
        assert f["status"] in ("pass", "warn", "crit"), f"Invalid status: {f['status']}"


def test_run_advisor_sorted_crit_first():
    pod = _pod()
    prom = _prom("pod-0", disk={"data_path_total_bytes": 100, "data_path_free_bytes": 5})
    snap = _snap(mongot_pods=[pod], mongot_prometheus=prom,
                 mongodbsearch_crds=[{"name": "s", "namespace": "ns", "phase": "Failed"}])
    findings = run_advisor(snap)
    statuses = [f["status"] for f in findings]
    order = {"crit": 0, "warn": 1, "pass": 2}
    assert statuses == sorted(statuses, key=lambda s: order[s])


# ── Disk check ────────────────────────────────────────────────────────────────

def test_disk_pass_no_pods():
    f = _find(run_advisor(_snap()), "disk_200_rule")
    assert f["status"] == "pass"


def test_disk_warn_insufficient_headroom():
    pod = _pod()
    # used=80, free=20 → free < 200% of used (need 160)
    prom = _prom("pod-0", disk={"data_path_total_bytes": 100, "data_path_free_bytes": 20})
    f = _find(run_advisor(_snap(mongot_pods=[pod], mongot_prometheus=prom)), "disk_200_rule")
    assert f["status"] == "warn"


def test_disk_crit_at_90_percent():
    pod = _pod()
    # 92% used
    prom = _prom("pod-0", disk={"data_path_total_bytes": 100, "data_path_free_bytes": 8})
    f = _find(run_advisor(_snap(mongot_pods=[pod], mongot_prometheus=prom)), "disk_200_rule")
    assert f["status"] == "crit"


def test_disk_pass_ample_headroom():
    pod = _pod()
    # used=10, free=90 → free >> 200% of used
    prom = _prom("pod-0", disk={"data_path_total_bytes": 100, "data_path_free_bytes": 90})
    f = _find(run_advisor(_snap(mongot_pods=[pod], mongot_prometheus=prom)), "disk_200_rule")
    assert f["status"] == "pass"


# ── Index consolidation ───────────────────────────────────────────────────────

def test_index_consolidation_pass_single_index():
    indexes = [{"ns": "db.coll", "name": "idx1"}]
    f = _find(run_advisor(_snap(search_indexes=indexes)), "index_consolidation")
    assert f["status"] == "pass"


def test_index_consolidation_warn_multiple_fulltext_same_ns():
    """Two fullText indexes on the same collection → warn."""
    indexes = [
        {"ns": "db.coll", "name": "idx1", "type": "fullText"},
        {"ns": "db.coll", "name": "idx2", "type": "fullText"},
    ]
    f = _find(run_advisor(_snap(search_indexes=indexes)), "index_consolidation")
    assert f["status"] == "warn"
    assert "db.coll" in f["value"]


def test_index_consolidation_pass_different_collections():
    indexes = [{"ns": "db.coll1", "name": "idx1"}, {"ns": "db.coll2", "name": "idx2"}]
    f = _find(run_advisor(_snap(search_indexes=indexes)), "index_consolidation")
    assert f["status"] == "pass"


def test_index_consolidation_pass_hybrid_search_pattern():
    """One vectorSearch + one fullText on the same collection is valid (Hybrid Search)."""
    indexes = [
        {"ns": "db.coll", "name": "vec_idx", "type": "vectorSearch"},
        {"ns": "db.coll", "name": "txt_idx", "type": "fullText"},
    ]
    f = _find(run_advisor(_snap(search_indexes=indexes)), "index_consolidation")
    assert f["status"] == "pass"


def test_index_consolidation_warn_multiple_vector_same_ns():
    """Two vectorSearch indexes on the same collection → warn."""
    indexes = [
        {"ns": "db.coll", "name": "vec1", "type": "vectorSearch"},
        {"ns": "db.coll", "name": "vec2", "type": "vectorSearch"},
    ]
    f = _find(run_advisor(_snap(search_indexes=indexes)), "index_consolidation")
    assert f["status"] == "warn"


# ── I/O Bottleneck ────────────────────────────────────────────────────────────

def test_io_bottleneck_pass():
    f = _find(run_advisor(_snap()), "io_bottleneck")
    assert f["status"] == "pass"


def test_io_bottleneck_crit_high_queue_and_lag():
    pod = _pod()
    prom = _prom("pod-0", disk={"queue_length": 15}, indexing={"change_stream_lag_sec": 10})
    f = _find(run_advisor(_snap(mongot_pods=[pod], mongot_prometheus=prom)), "io_bottleneck")
    assert f["status"] == "crit"


def test_io_bottleneck_pass_only_high_queue_no_lag():
    pod = _pod()
    prom = _prom("pod-0", disk={"queue_length": 15}, indexing={"change_stream_lag_sec": 0})
    f = _find(run_advisor(_snap(mongot_pods=[pod], mongot_prometheus=prom)), "io_bottleneck")
    assert f["status"] == "pass"


# ── CPU & QPS ─────────────────────────────────────────────────────────────────

def test_cpu_qps_pass():
    f = _find(run_advisor(_snap()), "cpu_qps")
    assert f["status"] == "pass"


def test_cpu_qps_crit_over_80_percent():
    pod = _pod(cpu_limit=2.0)
    prom = _prom("pod-0", process={"cpu_usage": 0.85})
    f = _find(run_advisor(_snap(mongot_pods=[pod], mongot_prometheus=prom)), "cpu_qps")
    assert f["status"] == "crit"


def test_cpu_qps_warn_high_qps_low_cores():
    pod = _pod(cpu_limit=1.0)
    snap = _snap(mongot_pods=[pod], search_perf={"queries_per_sec": 50, "total_queries_5m": 0})
    f = _find(run_advisor(snap), "cpu_qps")
    assert f["status"] == "warn"


# ── Page faults ───────────────────────────────────────────────────────────────

def test_page_faults_pass():
    f = _find(run_advisor(_snap()), "page_faults")
    assert f["status"] == "pass"


def test_page_faults_crit():
    pod = _pod()
    prom = _prom("pod-0", memory={"major_page_faults_sec": 1500})
    f = _find(run_advisor(_snap(mongot_pods=[pod], mongot_prometheus=prom)), "page_faults")
    assert f["status"] == "crit"


def test_page_faults_warn():
    pod = _pod()
    prom = _prom("pod-0", memory={"major_page_faults_sec": 700})
    f = _find(run_advisor(_snap(mongot_pods=[pod], mongot_prometheus=prom)), "page_faults")
    assert f["status"] == "warn"


# ── OOM risk ──────────────────────────────────────────────────────────────────

def test_oom_pass_no_pods():
    f = _find(run_advisor(_snap()), "oom_risk")
    assert f["status"] == "pass"


def test_oom_crit_oomkilled_container():
    pod = _pod(containers=[{"last_reason": "OOMKilled"}])
    f = _find(run_advisor(_snap(mongot_pods=[pod])), "oom_risk")
    assert f["status"] == "crit"
    assert "OOMKilled" in f["value"]


def test_oom_crit_heap_exceeds_90pct_limit():
    limit = 4 * 1024 ** 3
    pod = _pod(mem_limit=limit)
    prom = _prom("pod-0", jvm={"heap_max_bytes": int(limit * 0.95)})
    f = _find(run_advisor(_snap(mongot_pods=[pod], mongot_prometheus=prom)), "oom_risk")
    assert f["status"] == "crit"


def test_oom_warn_heap_over_60pct():
    limit = 4 * 1024 ** 3
    pod = _pod(mem_limit=limit)
    prom = _prom("pod-0", jvm={"heap_max_bytes": int(limit * 0.7)})
    f = _find(run_advisor(_snap(mongot_pods=[pod], mongot_prometheus=prom)), "oom_risk")
    assert f["status"] == "warn"


# ── CRD status ────────────────────────────────────────────────────────────────

def test_crd_status_pass_no_crds():
    f = _find(run_advisor(_snap()), "crd_status")
    assert f["status"] == "pass"


def test_crd_status_pass_running():
    crds = [{"name": "s", "namespace": "ns", "phase": "Running"}]
    f = _find(run_advisor(_snap(mongodbsearch_crds=crds)), "crd_status")
    assert f["status"] == "pass"


def test_crd_status_crit_non_running():
    crds = [{"name": "s", "namespace": "ns", "phase": "ReconcileFailed"}]
    f = _find(run_advisor(_snap(mongodbsearch_crds=crds)), "crd_status")
    assert f["status"] == "crit"


# ── Storage class ─────────────────────────────────────────────────────────────

def test_storage_pass_no_pvcs():
    f = _find(run_advisor(_snap()), "storage_class")
    assert f["status"] == "pass"


def test_storage_warn_slow_class():
    pvcs = [{"name": "pvc-0", "storage_class": "standard"}]
    f = _find(run_advisor(_snap(mongot_pvcs=pvcs)), "storage_class")
    assert f["status"] == "warn"


def test_storage_pass_fast_class():
    pvcs = [{"name": "pvc-0", "storage_class": "gp3"}]
    f = _find(run_advisor(_snap(mongot_pvcs=pvcs)), "storage_class")
    assert f["status"] == "pass"


# ── Versioning ────────────────────────────────────────────────────────────────

def test_versioning_pass_no_operator():
    f = _find(run_advisor(_snap()), "versioning")
    assert f["status"] == "pass"


def test_versioning_warn_latest_tag():
    f = _find(run_advisor(_snap(operator={"image": "mongodb/mck:latest"})), "versioning")
    assert f["status"] == "warn"


def test_versioning_pass_exact_tag():
    f = _find(run_advisor(_snap(operator={"image": "mongodb/mck:1.25.0"})), "versioning")
    assert f["status"] == "pass"


# ── Oplog window ─────────────────────────────────────────────────────────────

def test_oplog_not_present_without_timestamp():
    findings = run_advisor(_snap(oplog_info={"window_hours": 10}))
    assert _find(findings, "oplog_window") is None


def test_oplog_pass_small_lag():
    oplog = {"head_timestamp": 1, "window_hours": 10}
    pod = _pod()
    prom = _prom("pod-0", indexing={"change_stream_lag_sec": 100})  # ~0.028h << 40% of 10h
    f = _find(run_advisor(_snap(oplog_info=oplog, mongot_pods=[pod], mongot_prometheus=prom)), "oplog_window")
    assert f["status"] == "pass"


def test_oplog_warn_moderate_lag():
    oplog = {"head_timestamp": 1, "window_hours": 2}
    pod = _pod()
    # lag = 0.5h = 1800s → 25% of 2h = 40% threshold → warn
    prom = _prom("pod-0", indexing={"change_stream_lag_sec": 3000})  # 0.83h > 40% of 2h
    f = _find(run_advisor(_snap(oplog_info=oplog, mongot_pods=[pod], mongot_prometheus=prom)), "oplog_window")
    assert f["status"] in ("warn", "crit")


def test_oplog_crit_severe_lag():
    oplog = {"head_timestamp": 1, "window_hours": 1}
    pod = _pod()
    prom = _prom("pod-0", indexing={"change_stream_lag_sec": 3000})  # 0.83h > 70% of 1h
    f = _find(run_advisor(_snap(oplog_info=oplog, mongot_pods=[pod], mongot_prometheus=prom)), "oplog_window")
    assert f["status"] == "crit"


# ── Skip Auth check ───────────────────────────────────────────────────────────

def test_skip_auth_not_present_when_param_unavailable():
    """When search_server_params is absent/None, skip_auth_search finding is not emitted."""
    findings = run_advisor(_snap())
    assert _find(findings, "skip_auth_search") is None


def test_skip_auth_pass_when_false():
    params = {"skipAuthenticationToSearchIndexManagementServer": False, "searchTLSMode": None}
    f = _find(run_advisor(_snap(search_server_params=params)), "skip_auth_search")
    assert f is not None
    assert f["status"] == "pass"


def test_skip_auth_crit_when_true():
    params = {"skipAuthenticationToSearchIndexManagementServer": True, "searchTLSMode": None}
    f = _find(run_advisor(_snap(search_server_params=params)), "skip_auth_search")
    assert f is not None
    assert f["status"] == "crit"


# ── Search TLS mode check ─────────────────────────────────────────────────────

def test_search_tls_not_present_when_param_unavailable():
    """When search_server_params is absent/None, search_tls_mode finding is not emitted."""
    findings = run_advisor(_snap())
    assert _find(findings, "search_tls_mode") is None


def test_search_tls_pass_require_tls():
    params = {"skipAuthenticationToSearchIndexManagementServer": None, "searchTLSMode": "requireTLS"}
    f = _find(run_advisor(_snap(search_server_params=params)), "search_tls_mode")
    assert f is not None
    assert f["status"] == "pass"


def test_search_tls_crit_disabled():
    params = {"skipAuthenticationToSearchIndexManagementServer": None, "searchTLSMode": "disabled"}
    f = _find(run_advisor(_snap(search_server_params=params)), "search_tls_mode")
    assert f is not None
    assert f["status"] == "crit"


def test_search_tls_warn_allow_tls():
    params = {"skipAuthenticationToSearchIndexManagementServer": None, "searchTLSMode": "allowTLS"}
    f = _find(run_advisor(_snap(search_server_params=params)), "search_tls_mode")
    assert f is not None
    assert f["status"] == "warn"


def test_search_tls_warn_prefer_tls():
    params = {"skipAuthenticationToSearchIndexManagementServer": None, "searchTLSMode": "preferTLS"}
    f = _find(run_advisor(_snap(search_server_params=params)), "search_tls_mode")
    assert f is not None
    assert f["status"] == "warn"


def test_search_tls_none_for_unknown_value():
    """An unknown searchTLSMode value returns None (finding not emitted)."""
    params = {"skipAuthenticationToSearchIndexManagementServer": None, "searchTLSMode": "someUnknown"}
    findings = run_advisor(_snap(search_server_params=params))
    assert _find(findings, "search_tls_mode") is None


# ── _fmt_bytes helper ─────────────────────────────────────────────────────────

def test_fmt_bytes_zero():
    assert _fmt_bytes(0) == "—"


def test_fmt_bytes_gb():
    assert "GB" in _fmt_bytes(2 * 1024 ** 3)


def test_fmt_bytes_mb():
    assert "MB" in _fmt_bytes(5 * 1024 ** 2)


def test_fmt_bytes_kb():
    assert "KB" in _fmt_bytes(2 * 1024)
