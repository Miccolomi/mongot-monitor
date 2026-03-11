"""
Shared mutable application state.
Initialized by main() at startup via collectors.kubernetes.init_k8s()
and collectors.mongodb.init_mongo(). Accessed by all collector modules at runtime.
"""

import threading

mongo_client = None   # pymongo.MongoClient instance
k8s_v1 = None         # kubernetes.client.CoreV1Api
k8s_custom = None     # kubernetes.client.CustomObjectsApi
k8s_apps = None       # kubernetes.client.AppsV1Api
TARGET_NAMESPACE = None  # Optional[str] — K8s namespace filter

# Metrics cache — populated by BackgroundCollector, read by /metrics
cache_lock = threading.Lock()
metrics_cache = {
    "data": None,
    "timestamp": 0,
    "advisor": None,    # list of Finding dicts from advisor.run_advisor() — None until first run
    "last_scrape": {},  # {pod_name: {"time": float, "applicable_updates": float}}
    "last_mongo": {},   # {"time": float, "ops_insert": int, "ops_update": int, "ops_delete": int}
}
