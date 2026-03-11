import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state
from mongot_monitor import create_app

_EMPTY_METRICS = {
    "mongot_pods": [],
    "search_indexes": [],
    "mongo_connected": False,
    "global_errors": [],
    "k8s_version": "N/A",
    "operator": {},
    "mongodbsearch_crds": [],
    "mongot_pvcs": [],
    "mongot_services": [],
    "pod_metrics": {},
    "oplog_info": {"head_time": None, "tail_time": None, "window_hours": 0, "head_timestamp": 0},
    "mongo_vitals": {"connections_active": 0, "connections_available": 0, "active_writers": 0,
                     "ops_insert": 0, "ops_update": 0, "ops_delete": 0,
                     "ops_insert_sec": 0, "ops_update_sec": 0, "ops_delete_sec": 0},
    "helm_releases": [],
    "search_perf": {"queries_per_sec": 0, "total_queries_5m": 0},
    "mongot_prometheus": {},
    "timestamp": "2026-01-01T00:00:00+00:00",
    "_collect_ms": 0,
    "_cached": False,
}


@pytest.fixture
def client():
    """Flask test client — cache empty, no K8s/MongoDB configured."""
    app = create_app()
    app.config["TESTING"] = True
    with state.cache_lock:
        state.metrics_cache["data"] = None
        state.metrics_cache["timestamp"] = 0
        state.metrics_cache["advisor"] = None
    with app.test_client() as c:
        yield c
    with state.cache_lock:
        state.metrics_cache["data"] = None
        state.metrics_cache["timestamp"] = 0
        state.metrics_cache["advisor"] = None


@pytest.fixture
def metrics_client():
    """Flask test client with pre-populated metrics cache."""
    app = create_app()
    app.config["TESTING"] = True
    import time
    with state.cache_lock:
        state.metrics_cache["data"] = _EMPTY_METRICS.copy()
        state.metrics_cache["timestamp"] = time.time()
    with app.test_client() as c:
        yield c
    with state.cache_lock:
        state.metrics_cache["data"] = None
        state.metrics_cache["timestamp"] = 0
