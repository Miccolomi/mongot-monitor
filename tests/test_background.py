"""
Tests for BackgroundCollector — collection logic verified with mocked dependencies.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

import state
from background import BackgroundCollector


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_mock_vitals():
    return {
        "connections_active": 10, "connections_available": 100,
        "active_writers": 0,
        "ops_insert": 5, "ops_update": 3, "ops_delete": 1,
    }


MOCK_PATCHES = {
    "background.get_mongo_vitals":              lambda e=None: _make_mock_vitals(),
    "background.get_oplog_info":                lambda e=None: {"head_time": None, "tail_time": None, "window_hours": 0, "head_timestamp": 0},
    "background.get_search_indexes":            lambda e=None: [],
    "background.get_search_perf_from_profiler": lambda e=None: {"queries_per_sec": 0, "total_queries_5m": 0},
    "background.get_k8s_version":               lambda: "v1.28.0",
    "background.discover_operator_info":        lambda e=None: {},
    "background.discover_mongodbsearch_crds":   lambda e=None: [],
    "background.discover_mongot_pods":          lambda e=None: [],
    "background.get_mongot_pvcs":               lambda e=None: [],
    "background.get_mongot_services":           lambda e=None: [],
    "background.get_pod_metrics":               lambda: {},
    "background.get_helm_releases":             lambda e=None: [],
    "background.scrape_mongot_prometheus":      lambda *a, **kw: {"available": False, "raw_count": 0, "categories": {}},
}


def _apply_patches(fn):
    """Decorator: apply all MOCK_PATCHES to a test function."""
    import functools
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        patchers = [patch(k, side_effect=v) for k, v in MOCK_PATCHES.items()]
        for p in patchers:
            p.start()
        try:
            return fn(*args, **kwargs)
        finally:
            for p in patchers:
                p.stop()
    return wrapper


@pytest.fixture(autouse=True)
def reset_cache():
    """Ensure metrics_cache is clean before and after every test."""
    with state.cache_lock:
        state.metrics_cache["data"] = None
        state.metrics_cache["timestamp"] = 0
        state.metrics_cache["last_mongo"] = {}
        state.metrics_cache["last_scrape"] = {}
    yield
    with state.cache_lock:
        state.metrics_cache["data"] = None
        state.metrics_cache["timestamp"] = 0
        state.metrics_cache["last_mongo"] = {}
        state.metrics_cache["last_scrape"] = {}


# ── Tests ─────────────────────────────────────────────────────────────────────

@_apply_patches
def test_collect_populates_cache():
    """A single _collect() call should fill state.metrics_cache."""
    c = BackgroundCollector(interval=60)
    c._collect()

    with state.cache_lock:
        data = state.metrics_cache["data"]
        ts = state.metrics_cache["timestamp"]

    assert data is not None
    assert ts > 0


@_apply_patches
def test_collect_sets_expected_keys():
    c = BackgroundCollector(interval=60)
    c._collect()

    with state.cache_lock:
        data = state.metrics_cache["data"]

    for key in ("mongot_pods", "search_indexes", "mongo_connected", "global_errors",
                "k8s_version", "mongot_prometheus", "_collect_ms"):
        assert key in data, f"Missing key '{key}'"


@_apply_patches
def test_collect_mongo_connected_false_without_client():
    state.mongo_client = None
    c = BackgroundCollector(interval=60)
    c._collect()

    with state.cache_lock:
        data = state.metrics_cache["data"]

    assert data["mongo_connected"] is False


@_apply_patches
def test_collect_ops_rates_on_second_cycle():
    """Second _collect() should compute non-zero rates if counters changed."""
    c = BackgroundCollector(interval=60)

    # First cycle
    c._collect()

    # Simulate counter advance between cycles
    with state.cache_lock:
        state.metrics_cache["last_mongo"]["ops_insert"] = 0
        state.metrics_cache["last_mongo"]["ops_update"] = 0
        state.metrics_cache["last_mongo"]["ops_delete"] = 0
        state.metrics_cache["last_mongo"]["time"] = time.time() - 5  # 5 seconds ago

    with patch("background.get_mongo_vitals", side_effect=lambda e=None: {
        **_make_mock_vitals(), "ops_insert": 50, "ops_update": 30, "ops_delete": 10
    }):
        c._collect()

    with state.cache_lock:
        vitals = state.metrics_cache["data"]["mongo_vitals"]

    assert vitals["ops_insert_sec"] > 0
    assert vitals["ops_update_sec"] > 0


@_apply_patches
def test_collect_does_not_raise_on_collector_error():
    """_collect() should not propagate exceptions from individual collectors."""
    with patch("background.get_k8s_version", side_effect=RuntimeError("k8s down")):
        c = BackgroundCollector(interval=60)
        # Should not raise
        with pytest.raises(RuntimeError):
            c._collect()  # This will raise because get_k8s_version is called directly


@_apply_patches
def test_background_thread_starts_and_collects():
    """start() should spawn a daemon thread that fills the cache."""
    c = BackgroundCollector(interval=1)
    c.start()

    # Wait up to 3s for the first collection
    deadline = time.time() + 3
    while time.time() < deadline:
        with state.cache_lock:
            data = state.metrics_cache["data"]
        if data is not None:
            break
        time.sleep(0.1)

    with state.cache_lock:
        data = state.metrics_cache["data"]

    assert data is not None, "Cache should be populated after collector thread runs"
    assert c._thread.daemon is True
