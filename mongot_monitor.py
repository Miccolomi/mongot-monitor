#!/usr/bin/env python3
"""
MongoDB Search Node Monitor (mongot) - Ultimate SRE Advisor Edition
===================================================================
Interfaccia completa + Funzionalità Enterprise + Advisor Best Practices + Live Logs Persistenti.
"""

import argparse
import logging
import time
import json
from datetime import datetime, timezone, timedelta
try:
    from bson import Binary, ObjectId, Timestamp
except ImportError:
    Binary = bytes
    ObjectId = type(None)
    Timestamp = type(None)

from flask import Flask, jsonify, Response
from flask_cors import CORS
import requests

# Kubernetes
try:
    from kubernetes import client as k8s_client, config as k8s_config
    from kubernetes.stream import stream
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False

# MongoDB
from pymongo import MongoClient

# ── Config ──────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

class MongoJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, (Binary, bytes)): return str(o)
        if isinstance(o, ObjectId): return str(o)
        if isinstance(o, datetime): return o.isoformat()
        if isinstance(o, Timestamp): return o.as_datetime().isoformat()
        try: return super().default(o)
        except TypeError: return str(o)

app.json_encoder = MongoJSONEncoder
try: app.json.compact = True
except Exception: pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("mongot-monitor")

mongo_client: MongoClient = None
k8s_v1: k8s_client.CoreV1Api = None
k8s_custom: k8s_client.CustomObjectsApi = None
k8s_apps: k8s_client.AppsV1Api = None
TARGET_NAMESPACE = None

# Cache metrics to prevent overloading K8s and MongoDB APIs
metrics_cache = {"data": None, "timestamp": 0}
CACHE_TTL_SEC = 2


# ── Kubernetes Discovery & Events ───────────────────────
def discover_mongodbsearch_crds(errors: list = None) -> list:
    if not k8s_custom: return []
    crds = []
    try:
        namespaces = [TARGET_NAMESPACE] if TARGET_NAMESPACE else [ns.metadata.name for ns in k8s_v1.list_namespace().items]
    except Exception as e:
        if errors is not None: errors.append(f"K8s API Error (Reading namespaces for CRDs): {str(e)}")
        namespaces = [TARGET_NAMESPACE] if TARGET_NAMESPACE else ["mongodb", "default"]
        
    for ns in namespaces:
        try:
            res = k8s_custom.list_namespaced_custom_object("mongodb.com", "v1", ns, "mongodbsearch")
            for item in res.get("items", []):
                spec, status, meta = item.get("spec", {}), item.get("status", {}), item.get("metadata", {})
                prom_conf = spec.get("prometheus", {}) or {}
                crds.append({
                    "name": meta.get("name", "?"), "namespace": ns,
                    "prometheus_enabled": bool(prom_conf),
                    "prometheus_port": prom_conf.get("port", 9946) if isinstance(prom_conf, dict) else 9946,
                    "phase": status.get("phase", "Unknown"),
                    "log_level": spec.get("logLevel", "INFO")
                })
        except Exception as e: 
            if errors is not None: errors.append(f"K8s API Error (MongoDBSearch CRD in ns '{ns}'): {str(e)}")
    return crds

def discover_operator_info(errors: list = None) -> dict:
    if not k8s_apps: return {}
    try:
        namespaces = [TARGET_NAMESPACE] if TARGET_NAMESPACE else ["mongodb", "default", "mongo"]
        for ns in namespaces:
            try:
                deps = k8s_apps.list_namespaced_deployment(ns)
                for dep in deps.items:
                    dname = dep.metadata.name.lower()
                    if "mongodb" in dname and ("operator" in dname or "controller" in dname):
                        containers = dep.spec.template.spec.containers or []
                        
                        # Find the actual pod name for logs
                        pod_name = dname
                        if k8s_v1:
                            try:
                                pods = k8s_v1.list_namespaced_pod(ns)
                                for p in pods.items:
                                    if p.metadata.name.startswith(dname):
                                        pod_name = p.metadata.name
                                        break
                            except Exception as e: 
                                if errors is not None: errors.append(f"K8s API Error (Pod list for Operator in ns '{ns}'): {str(e)}")

                        return {
                            "name": dep.metadata.name, "namespace": ns, "pod_name": pod_name,
                            "image": containers[0].image if containers else "?",
                            "replicas": dep.status.ready_replicas or 0, "desired": dep.spec.replicas or 1
                        }
            except Exception as e: 
                if errors is not None: errors.append(f"K8s API Error (Deployment list for Operator in ns '{ns}'): {str(e)}")
    except Exception as e: 
        if errors is not None: errors.append(f"K8s API Error (Operator Discovery): {str(e)}")
    return {}

def get_pod_warnings(namespace: str, pod_name: str) -> list:
    if not k8s_v1: return []
    warnings = []
    try:
        fs = f"involvedObject.name={pod_name},type=Warning"
        events = k8s_v1.list_namespaced_event(namespace, field_selector=fs).items
        events.sort(key=lambda x: x.last_timestamp or x.event_time or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        for e in events[:5]:
            warnings.append({"reason": e.reason, "message": e.message, "count": e.count, "time": e.last_timestamp.isoformat() if e.last_timestamp else None})
    except Exception: pass
    return warnings

def discover_mongot_pods(errors: list = None) -> list:
    if not k8s_v1: return []
    pods = []
    found_pods = set()
    try:
        res = k8s_v1.list_namespaced_pod(TARGET_NAMESPACE) if TARGET_NAMESPACE else k8s_v1.list_pod_for_all_namespaces()
        for pod in res.items:
            pname = pod.metadata.name.lower()
            labels = pod.metadata.labels or {}
            
            is_mongot = (
                labels.get("app.kubernetes.io/component") == "search" or 
                labels.get("app") == "mongodbsearch" or
                ("mongot" in pname and "mongod" not in pname) or
                ("search" in pname and "operator" not in pname)
            )
            
            if not is_mongot or pod.metadata.name in found_pods: continue
            found_pods.add(pod.metadata.name)

            containers = []
            cpu_limit_cores = 0.0

            for c in (pod.spec.containers or []):
                # Estraiamo i CPU Limits per l'Advisor
                if c.resources and c.resources.limits and "cpu" in c.resources.limits:
                    cpu_str = c.resources.limits["cpu"]
                    try:
                        if cpu_str.endswith("m"): cpu_limit_cores += int(cpu_str[:-1]) / 1000.0
                        else: cpu_limit_cores += float(cpu_str)
                    except: pass

            for cs in (pod.status.container_statuses or []):
                last_reason = None
                if cs.last_state and cs.last_state.terminated: last_reason = cs.last_state.terminated.reason
                containers.append({
                    "name": cs.name, "ready": cs.ready, "restart_count": cs.restart_count,
                    "state": "running" if cs.state.running else "waiting" if cs.state.waiting else "terminated" if cs.state.terminated else "unknown",
                    "last_reason": last_reason
                })

            pods.append({
                "name": pod.metadata.name, "namespace": pod.metadata.namespace,
                "node": pod.spec.node_name, "pod_ip": pod.status.pod_ip,
                "phase": pod.status.phase,
                "start_time": pod.status.start_time.isoformat() if pod.status.start_time else None,
                "containers": containers,
                "total_restarts": sum(c["restart_count"] for c in containers),
                "all_ready": all(c["ready"] for c in containers) if containers else False,
                "cpu_limit_cores": cpu_limit_cores,
                "warnings": get_pod_warnings(pod.metadata.namespace, pod.metadata.name)
            })
    except Exception as e:
        log.error(f"K8s pod discovery error: {e}")
        if errors is not None: errors.append(f"K8s API Error (Pod Discovery): {str(e)}")
    return pods

def get_mongot_pvcs(errors: list = None) -> list:
    pvcs = []
    if not k8s_v1: return pvcs
    try:
        res = k8s_v1.list_namespaced_persistent_volume_claim(TARGET_NAMESPACE) if TARGET_NAMESPACE else k8s_v1.list_persistent_volume_claim_for_all_namespaces()
        for pvc in res.items:
            pname = pvc.metadata.name.lower()
            if "search" in pname or "mongot" in pname:
                pvcs.append({
                    "name": pvc.metadata.name, "namespace": pvc.metadata.namespace, "status": pvc.status.phase,
                    "capacity": pvc.status.capacity.get("storage", "?") if pvc.status.capacity else "?",
                    "storage_class": pvc.spec.storage_class_name
                })
    except Exception as e: 
        if errors is not None: errors.append(f"K8s API Error (Discovery PVCs '{TARGET_NAMESPACE or 'all'}'): {str(e)}")
    return pvcs

def get_mongot_services(errors: list = None) -> list:
    services = []
    if not k8s_v1: return services
    try:
        res = k8s_v1.list_namespaced_service(TARGET_NAMESPACE) if TARGET_NAMESPACE else k8s_v1.list_service_for_all_namespaces()
        for svc in res.items:
            sname = svc.metadata.name.lower()
            if "search" in sname or "mongot" in sname:
                ports = [{"port": p.port, "target": p.target_port, "protocol": p.protocol} for p in (svc.spec.ports or [])]
                services.append({"name": svc.metadata.name, "namespace": svc.metadata.namespace, "type": svc.spec.type, "ports": ports})
    except Exception as e: 
        if errors is not None: errors.append(f"K8s API Error (Discovery Services '{TARGET_NAMESPACE or 'all'}'): {str(e)}")
    return services

def get_pod_metrics() -> dict:
    pod_metrics = {}
    if not k8s_custom: return pod_metrics
    try:
        res = k8s_custom.list_namespaced_custom_object("metrics.k8s.io", "v1beta1", TARGET_NAMESPACE, "pods") if TARGET_NAMESPACE else k8s_custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "pods")
        for item in res.get("items", []):
            name, total_cpu, total_mem = item["metadata"]["name"], 0, 0
            for c in item.get("containers", []):
                cpu_str, mem_str = c.get("usage", {}).get("cpu", "0"), c.get("usage", {}).get("memory", "0")
                if cpu_str.endswith("n"): total_cpu += int(cpu_str[:-1]) / 1e6
                elif cpu_str.endswith("m"): total_cpu += int(cpu_str[:-1])
                if mem_str.endswith("Ki"): total_mem += int(mem_str[:-2]) * 1024
                elif mem_str.endswith("Mi"): total_mem += int(mem_str[:-2]) * 1024 * 1024
            pod_metrics[name] = {"cpu_millicores": round(total_cpu, 1), "memory_bytes": int(total_mem)}
    except Exception: pass
    return pod_metrics


# ── MongoDB & Prometheus ────────────────────────────────
def get_mongo_vitals(errors: list = None) -> dict:
    vitals = {"connections_active": 0, "connections_available": 0, "active_writers": 0, "ops_insert": 0, "ops_update": 0, "ops_delete": 0}
    if not mongo_client: return vitals
    try:
        status = mongo_client.admin.command("serverStatus")
        vitals["connections_active"] = status.get("connections", {}).get("current", 0)
        vitals["connections_available"] = status.get("connections", {}).get("available", 0)
        vitals["active_writers"] = status.get("globalLock", {}).get("activeClients", {}).get("writers", 0)
        opc = status.get("opcounters", {})
        vitals["ops_insert"] = opc.get("insert", 0)
        vitals["ops_update"] = opc.get("update", 0)
        vitals["ops_delete"] = opc.get("delete", 0)
    except Exception as e: 
        if errors is not None: errors.append(f"MongoDB Error (Reading serverStatus): {str(e)}")
    return vitals

def get_oplog_info(errors: list = None) -> dict:
    info = {"head_time": None, "tail_time": None, "window_hours": 0, "head_timestamp": 0}
    if not mongo_client: return info
    try:
        db = mongo_client["local"]
        oplog = db["oplog.rs"]
        
        # HEAD (Ultimo record scritto)
        head_cur = oplog.find().sort("$natural", -1).limit(1)
        # TAIL (Record più vecchio ancora presente)
        tail_cur = oplog.find().sort("$natural", 1).limit(1)
        
        head_doc, tail_doc = None, None
        for doc in head_cur: head_doc = doc
        for doc in tail_cur: tail_doc = doc
        
        if head_doc and "ts" in head_doc:
            info["head_timestamp"] = head_doc["ts"].time
            info["head_time"] = head_doc["ts"].as_datetime().strftime("%H:%M:%S")
            
        if tail_doc and "ts" in tail_doc:
            info["tail_time"] = tail_doc["ts"].as_datetime().strftime("%H:%M:%S")
            
        if head_doc and tail_doc and "ts" in head_doc and "ts" in tail_doc:
            diff_sec = head_doc["ts"].time - tail_doc["ts"].time
            info["window_hours"] = round(diff_sec / 3600, 2)
            
    except Exception as e:
        log.error(f"Oplog Error: {e}")
        if errors is not None: errors.append(f"MongoDB Error (Reading Oplog for lag): {str(e)}")
    return info


def get_search_indexes(errors: list = None) -> list:
    indexes = []
    if not mongo_client: return indexes
    try:
        db_names = [d for d in mongo_client.list_database_names() if d not in ("admin", "local", "config")]
        for db_name in db_names:
            db = mongo_client[db_name]
            for coll_name in db.list_collection_names():
                try:
                    for idx in db[coll_name].list_search_indexes():
                        idx_info = {
                            "name": idx.get("name", "unknown"),
                            "type": "vectorSearch" if idx.get("type") == "vectorSearch" else "fullText",
                            "status": idx.get("status", "READY"), "ns": f"{db_name}.{coll_name}",
                            "queryable": idx.get("queryable", True), "num_docs": None
                        }
                        try:
                            stats = db.command({"aggregate": coll_name, "pipeline": [{"$searchMeta": {"index": idx["name"], "exists": {"path": {"wildcard": "*"}}}}]})
                            first = (stats.get("cursor", {}).get("firstBatch") or [None])[0]
                            if first and "count" in first: idx_info["num_docs"] = first["count"].get("lowerBound", 0)
                        except Exception: 
                            try:
                                idx_info["num_docs"] = db[coll_name].estimated_document_count()
                            except: pass
                        indexes.append(idx_info)
                except Exception as e:
                    if errors is not None: errors.append(f"MongoDB Error (List search indexes in {db_name}.{coll_name}): {str(e)}")
    except Exception as e:
        if errors is not None: errors.append(f"MongoDB Error (Reading database/collections): {str(e)}")
    return indexes

def get_search_perf_from_profiler(errors: list = None) -> dict:
    """Estrae i QPS per l'Advisor."""
    perf = {"queries_per_sec": 0, "total_queries_5m": 0}
    if not mongo_client: return perf
    try:
        db_names = [d for d in mongo_client.list_database_names() if d not in ("admin", "local", "config")]
        all_durations = []
        window_sec = 300
        for db_name in db_names:
            db = mongo_client[db_name]
            try:
                query = {
                    "ts": {"$gte": datetime.now(timezone.utc) - timedelta(seconds=window_sec)},
                    "$or": [{"command.pipeline": {"$elemMatch": {"$search": {"$exists": True}}}},
                            {"command.pipeline": {"$elemMatch": {"$vectorSearch": {"$exists": True}}}}]
                }
                for doc in db["system.profile"].find(query).sort("ts", -1).limit(500):
                    all_durations.append(doc.get("millis", 0))
            except Exception as e:
                pass # Profiling setup may be disabled, ignore silently for db scope
        if all_durations:
            perf["total_queries_5m"] = len(all_durations)
            perf["queries_per_sec"] = round(len(all_durations) / window_sec, 2)
    except Exception as e: 
        if errors is not None: errors.append(f"MongoDB Error (Reading system.profile for profiler): {str(e)}")
    return perf

def scrape_mongot_prometheus(pod_name: str, namespace: str, pod_ip: str, port: int, errors: list = None) -> dict:
    result = {"available": False, "raw_count": 0, "categories": {}}
    text = ""
    scrape_errs = []
    
    # 1. Fallback Rete Diretta
    try:
        resp = requests.get(f"http://{pod_ip}:{port}/metrics", timeout=2)
        if resp.status_code == 200: text = resp.text
        else: raise Exception(f"HTTP {resp.status_code}")
    except Exception as e:
        scrape_errs.append(f"Direct Net: {str(e)}")
        if k8s_v1:
            # 2. Fallback API Proxy
            try:
                text = k8s_v1.connect_get_namespaced_pod_proxy_with_path(
                    name=f"{pod_name}:{port}", namespace=namespace, path="metrics", _request_timeout=5
                )
            except Exception as e2:
                scrape_errs.append(f"API Proxy: {str(e2)}")
                log.debug(f"Proxy scrape failed for {pod_name}: {e2}")

    if not text: 
        if errors is not None: errors.append(f"Network Error (Prometheus scrape failed for {pod_name}:{port}) -> " + " | ".join(scrape_errs))
        return result

    if hasattr(text, "data"): text = text.data
    if isinstance(text, bytes): text = text.decode('utf-8', errors='ignore')

    result["available"] = True
    raw = {}
    for line in text.split("\n"):
        if line.startswith("#") or not line.strip(): continue
        if "{" in line: key, val = line[:line.index("{")], line[line.rindex("}") + 1:].strip()
        else: parts = line.split(); key, val = (parts[0], parts[1]) if len(parts)>1 else ("", "")
        try: 
            import math
            v = float(val)
            if math.isnan(v): v = 0.0
            raw[key] = raw.get(key, 0.0) + v
        except: pass
    
    result["raw_count"] = len(raw)
    g = lambda k, d=0: raw.get(k, d)

    result["categories"] = {
        "search_commands": {
            "search_latency_sec": g("mongot_command_searchCommandTotalLatency_seconds_max"),
            "search_failures": g("mongot_command_searchCommandFailure_total"),
            "vectorsearch_latency_sec": g("mongot_command_vectorSearchCommandTotalLatency_seconds_max"),
            "vectorsearch_failures": g("mongot_command_vectorSearchCommandFailure_total"),
            "getmores_latency_sec": g("mongot_command_getMoresCommandTotalLatency_seconds_max"),
            "manage_index_latency_sec": g("mongot_command_manageSearchIndexCommandTotalLatency_seconds_max"),
        },
        "jvm": {
            "heap_used_bytes": g("mongot_jvm_memory_used_bytes"), "heap_committed_bytes": g("mongot_jvm_memory_committed_bytes"),
            "heap_max_bytes": g("mongot_jvm_memory_max_bytes"), "gc_pause_seconds_max": g("mongot_jvm_gc_pause_seconds_max"),
            "buffer_used_bytes": g("mongot_jvm_buffer_memory_used_bytes"),
        },
        "process": {
            "cpu_usage": g("mongot_process_cpu_usage"), "load_avg_1m": g("mongot_system_load_average_1m"),
            "cpu_count": g("mongot_system_cpu_count", 0),
        },
        "memory": {
            "phys_total_bytes": g("mongot_system_memory_phys_total_bytes"), "phys_inuse_bytes": g("mongot_system_memory_phys_inUse_bytes"),
            "swap_inuse_bytes": g("mongot_system_memory_virt_swap_inUse_bytes"),
        },
        "disk": {
            "data_path_free_bytes": g("mongot_system_disk_space_data_path_free_bytes"), "data_path_total_bytes": g("mongot_system_disk_space_data_path_total_bytes"),
            "read_bytes": g("mongot_system_disk_readBytes_bytes"), "write_bytes": g("mongot_system_disk_writeBytes_bytes"),
            "queue_length": g("mongot_system_disk_currentQueueLength_tasks"),
        },
        "network": {
            "bytes_recv": g("mongot_system_netstat_bytesRecv_bytes"), "bytes_sent": g("mongot_system_netstat_bytesSent_bytes"),
            "in_errors": g("mongot_system_netstat_inErrors_events"), "out_errors": g("mongot_system_netstat_outErrors_events"),
        },
        "indexing": {
            "indexes_in_catalog": g("mongot_configState_indexesInCatalog"), "staged_indexes": g("mongot_configState_stagedIndexes"),
            "indexes_phasing_out": g("mongot_configState_indexesPhasingOut"), "steady_witnessed_updates": g("mongot_indexing_steadyStateChangeStream_witnessedChangeStreamUpdates_total"),
            "steady_applicable_updates": g("mongot_index_stats_replication_steadyState_batchTotalApplicableDocuments_sum"),
            "steady_batches_in_progress": g("mongot_indexing_steadyStateChangeStream_batchesInProgressTotal"),
            "steady_batch_sec_max": g("mongot_indexing_steadyStateChangeStream_batchesInProgressTotalDurations_seconds_max"),
            "steady_unexpected_failures": g("mongot_indexing_steadyStateChangeStream_unexpectedBatchFailures_total"),
            "initial_sync_in_progress": g("mongot_initialsync_dispatcher_inProgressSyncs"), "initial_sync_queued": g("mongot_initialsync_dispatcher_queuedSyncs"),
            "change_stream_lag_sec": g("mongot_index_stats_indexing_replicationLagMs", 0) / 1000.0
        },
        "lucene_merge": {
            "running_merges": g("mongot_mergeScheduler_currentlyRunningMerges"), "merging_docs": g("mongot_mergeScheduler_currentlyMergingDocs"),
            "total_merges": g("mongot_mergeScheduler_numMerges_total"), "merge_time_sec_max": g("mongot_mergeScheduler_mergeTime_seconds_max"),
            "discarded_merges": g("mongot_diskUtilizationAwarenessMergePolicy_discardedMerge_total"),
        },
        "lifecycle": {
            "indexes_initialized": g("mongot_lifecycle_indexesInInitializedState"), "failed_downloads": g("mongot_lifecycle_failedDownloadIndexes_total"),
            "failed_drops": g("mongot_lifecycle_failedDropIndexes_total"), "failed_initializations": g("mongot_lifecycle_failedInitializationIndexes_total"),
        }
    }
    return result



from flask import jsonify, render_template_string, request, Response

# ── API & HTML ──────────────────────────────────────────
@app.route("/api/logs/<namespace>/<pod_name>")
def pod_logs(namespace, pod_name):
    if not k8s_v1: return jsonify({"error": "K8s API not available"}), 500
    try:
        return jsonify({"logs": k8s_v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, tail_lines=50)})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/download_logs/<namespace>/<pod_name>")
def download_logs(namespace, pod_name):
    if not k8s_v1: return "K8s API not available", 500
    try:
        t_param = request.args.get('time', 'all')
        lvl_param = request.args.get('level', 'all').lower()
        
        since_sec = None
        if t_param == '10m': since_sec = 600
        elif t_param == '1h': since_sec = 3600
        elif t_param == '24h': since_sec = 86400
        
        # Call K8s API with or without time bounds
        if since_sec:
            raw_logs = k8s_v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, since_seconds=since_sec)
        else:
            raw_logs = k8s_v1.read_namespaced_pod_log(name=pod_name, namespace=namespace)
            
        # Apply Level Filtering (Text-based searching for error keywords)
        if lvl_param == 'error':
            filtered_lines = []
            for line in raw_logs.splitlines():
                l_lower = line.lower()
                if "error" in l_lower or "fatal" in l_lower or "exception" in l_lower or "warning" in l_lower:
                    filtered_lines.append(line)
            final_log_data = "\n".join(filtered_lines)
            if not final_log_data: final_log_data = "No errors detected in this timeframe."
        else:
            final_log_data = raw_logs
            
        return Response(
            final_log_data,
            mimetype="text/plain",
            headers={"Content-disposition": f"attachment; filename={pod_name}_logs_{t_param}_{lvl_param}.txt"}
        )
    except Exception as e:
        return f"Error: {str(e)}", 500

def get_k8s_version() -> str:
    if not K8S_AVAILABLE or not k8s_client: return "N/A"
    try: return k8s_client.VersionApi().get_code().git_version
    except Exception: return "N/A"

def get_helm_releases(errors: list = None) -> list:
    releases = []
    if not k8s_v1: return releases
    try:
        res = k8s_v1.list_namespaced_secret(TARGET_NAMESPACE, label_selector="owner=helm") if TARGET_NAMESPACE else k8s_v1.list_secret_for_all_namespaces(label_selector="owner=helm")
        
        latest_rels = {}
        for s in res.items:
            labels = s.metadata.labels or {}
            name = labels.get("name", "unknown")
            status = labels.get("status", "unknown")
            # Only track MongoDB related charts to avoid clutter
            if "mongo" not in name.lower(): continue
            
            try: version = int(labels.get("version", 0))
            except: version = 0
            
            if name not in latest_rels or latest_rels[name]["revision"] < version:
                latest_rels[name] = {
                    "name": name,
                    "namespace": s.metadata.namespace,
                    "revision": version,
                    "status": status,
                    "modifiedAt": labels.get("modifiedAt", "unknown")
                }
        
        for k, v in latest_rels.items():
            try:
                if str(v["modifiedAt"]).isdigit():
                    v["modifiedAt_str"] = datetime.utcfromtimestamp(int(v["modifiedAt"])).strftime("%Y-%m-%d %H:%M:%S")
                else: 
                    v["modifiedAt_str"] = str(v["modifiedAt"])
            except:
                v["modifiedAt_str"] = "N/A"
            releases.append(v)
            
    except Exception as e:
        if errors is not None: errors.append(f"K8s API Error (Helm Release Discovery): {str(e)}")
        
    return sorted(releases, key=lambda x: x["name"])

# Cache globale per la dashboard
metrics_cache = {
    "data": {},
    "timestamp": 0,
    "last_scrape": {}, # {pod_name: {"time": float, "applicable_updates": float}}
    "last_mongo": {} # {"time": float, "ops_insert": int, "ops_update": int, "ops_delete": int}
}

@app.route("/metrics")
def metrics():
    global metrics_cache
    now = time.time()
    
    if metrics_cache.get("data") and (now - metrics_cache.get("timestamp", 0)) < CACHE_TTL_SEC:
        return jsonify(metrics_cache["data"])

    t0 = time.time()
    
    # ── Global Error Collector ──
    global_errors = []
    
    # Mongo Vitals Logic & Rate calculation
    vitals = get_mongo_vitals(global_errors)
    last_m = metrics_cache["last_mongo"]
    if "time" in last_m:
        dt = now - last_m["time"]
        if dt > 0:
            vitals["ops_insert_sec"] = max(0, int((vitals["ops_insert"] - last_m["ops_insert"]) / dt))
            vitals["ops_update_sec"] = max(0, int((vitals["ops_update"] - last_m["ops_update"]) / dt))
            vitals["ops_delete_sec"] = max(0, int((vitals["ops_delete"] - last_m["ops_delete"]) / dt))
    else:
        vitals["ops_insert_sec"] = vitals["ops_update_sec"] = vitals["ops_delete_sec"] = 0

    metrics_cache["last_mongo"] = {
        "time": now, "ops_insert": vitals["ops_insert"], 
        "ops_update": vitals["ops_update"], "ops_delete": vitals["ops_delete"]
    }
    
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
        "helm_releases": get_helm_releases(global_errors),
        "global_errors": global_errors, # Collects all errors of this cycle
        "mongo_connected": mongo_client is not None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "_collect_ms": 0,
        "_cached": False
    }
    
    # Map cluster name to its prometheus port based on CRDs
    prom_ports = {}
    for c in res["mongodbsearch_crds"]:
        if c.get("prometheus_enabled"):
            cluster_name = c["name"]
            # Typically pods are named like <cluster_name>-<node_id>
            prom_ports[cluster_name] = c.get("prometheus_port", 9946)
            
    prom_metrics = {}
    for p in res["mongot_pods"]:
        # Find the correct port for this pod based on its cluster name
        pod_port = 9946
        for cluster_name, port in prom_ports.items():
            if cluster_name in p["name"]:
                pod_port = port
                break
                
        metrics = scrape_mongot_prometheus(p["name"], p["namespace"], p.get("pod_ip","127.0.0.1"), pod_port, global_errors)
        
        # Calcolo Rateo / Secondo per le metriche cumulative
        pod_key = p["name"]
        curr_updates = metrics.get("categories", {}).get("indexing", {}).get("steady_applicable_updates", 0)
        metrics["categories"]["indexing"]["steady_applicable_updates_sec"] = 0.0
        
        if pod_key in metrics_cache["last_scrape"]:
            last = metrics_cache["last_scrape"][pod_key]
            dt = now - last["time"]
            du = curr_updates - last["applicable_updates"]
            if dt > 0 and du >= 0:
                metrics["categories"]["indexing"]["steady_applicable_updates_sec"] = round(du / dt, 1)
                
        metrics_cache["last_scrape"][pod_key] = {"time": now, "applicable_updates": curr_updates}
        
        prom_metrics[pod_key] = metrics
    
    res["mongot_prometheus"] = prom_metrics
    res["_collect_ms"] = round((time.time() - t0) * 1000, 1)
    
    metrics_cache["data"] = res
    metrics_cache["timestamp"] = now
    return jsonify(res)

@app.route("/healthcheck")
def healthcheck():
    status = {"status": "healthy", "mongo_ping": "ok", "k8s_api": "ok", "metrics_status": "ok"}
    is_unhealthy = False
    
    # 1. Mongo Ping
    if mongo_client:
        try:
            t0 = time.time()
            mongo_client.admin.command('ping')
            status["mongo_ping"] = f"ok ({round((time.time()-t0)*1000, 1)}ms)"
        except Exception as e:
            status["mongo_ping"] = f"failed ({str(e)})"
            is_unhealthy = True
    else:
        status["mongo_ping"] = "not_configured"
        
    # 2. K8s API
    if k8s_v1:
        try:
            k8s_v1.list_namespace(limit=1, _request_timeout=2)
        except Exception as e:
            status["k8s_api"] = f"failed ({str(e)})"
            is_unhealthy = True
    else:
        status["k8s_api"] = "not_configured"
        
    # 3. Metrics Freshness
    now = time.time()
    last_scrape_time = metrics_cache.get("timestamp", 0)
    if last_scrape_time > 0:
        age = now - last_scrape_time
        if age > 120:  # Older than 2 minutes means the background process is stuck
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

@app.route("/favicon.ico")
def favicon():
    return "", 204

@app.route("/")
def dashboard():
    HTML = r"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Mongot Ultimate Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&family=Space+Grotesk:wght@400;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#080b12;color:#c9d1e0;font-family:'JetBrains Mono','Fira Code',monospace;padding:20px 24px;min-height:100vh}
h1{font-family:'Space Grotesk',sans-serif;font-size:18px;color:#e8ecf4;letter-spacing:-0.5px}
.sub{font-size:10px;color:#4a5578;letter-spacing:1px}
.hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;flex-wrap:wrap;gap:12px}
.hdr-l{display:flex;align-items:center;gap:12px}
.logo{width:36px;height:36px;border-radius:8px;background:linear-gradient(135deg,#7c4dff,#00b0ff);display:flex;align-items:center;justify-content:center;font-size:16px;font-weight:700;color:#fff}
.hdr-r{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
select{padding:6px 12px;font-size:12px;font-weight:700;border-radius:6px;border:1px solid #00e67644;background:#00e67618;color:#00e676;cursor:pointer;font-family:'JetBrains Mono',monospace;outline:none;transition:0.2s}
select:hover{border-color:#00e676;background:#00e67633}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
@media(max-width:1200px){.grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:700px){.grid{grid-template-columns:1fr}}
.c{background:linear-gradient(135deg,#0d1117,#111827);border:1px solid #1e2740;border-radius:12px;padding:18px 20px;position:relative;overflow:hidden}
.c::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,#7c4dff,#00b0ff 50%,transparent);opacity:0.4}
.c-h{display:flex;align-items:center;gap:8px;margin-bottom:14px}
.c-t{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:2px;color:#6b7394}
.s2{grid-column:span 2}.s3{grid-column:span 3}.s4{grid-column:span 4}
.pill{display:inline-flex;align-items:center;gap:5px;border-radius:20px;padding:3px 10px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px}
.pill-d{width:6px;height:6px;border-radius:50%}
.p-ok{background:#00e67618;border:1px solid #00e67644;color:#00e676}.p-ok .pill-d{background:#00e676;box-shadow:0 0 6px #00e676}
.p-w{background:#ffab0018;border:1px solid #ffab0044;color:#ffab00}.p-w .pill-d{background:#ffab00;box-shadow:0 0 6px #ffab00}
.p-e{background:#ff174418;border:1px solid #ff174444;color:#ff1744}.p-e .pill-d{background:#ff1744;box-shadow:0 0 6px #ff1744}
.p-b{background:#7c4dff18;border:1px solid #7c4dff44;color:#b388ff}.p-b .pill-d{background:#b388ff;box-shadow:0 0 6px #b388ff}
.row{display:flex;justify-content:space-between;font-size:12px;padding:3px 0}.row-l{color:#6b7394}.row-v{font-weight:600}
.grn{color:#00e676}.blu{color:#00b0ff}.ylw{color:#ffab00}.red{color:#ff6b6b}.pur{color:#b388ff}.cyn{color:#00e5ff}
table{width:100%;border-collapse:collapse;font-size:12px}
th{padding:6px 10px;text-align:left;font-weight:600;color:#4a5578;font-size:10px;text-transform:uppercase;letter-spacing:1.5px;border-bottom:1px solid #1e2740}
td{padding:8px 10px;border-bottom:1px solid #111827}
.tag{padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600}
.tag-v{background:#7c4dff22;color:#b388ff;border:1px solid #7c4dff44}
.tag-f{background:#00b0ff22;color:#40c4ff;border:1px solid #00b0ff44}
.tag-run{background:#00e67622;color:#00e676;border:1px solid #00e67644}
.tag-fail{background:#ff174422;color:#ff1744;border:1px solid #ff174444}
.pod-meta{font-size:10px;color:#4a5578;margin-top:2px}
.gauge{display:flex;flex-direction:column;align-items:center;gap:4px}
.gauge-v{font-size:18px;font-weight:700}.gauge-u{font-size:11px;opacity:0.6}.gauge-l{font-size:10px;text-transform:uppercase;letter-spacing:1.5px;color:#6b7394;font-weight:600}
.empty{text-align:center;padding:16px;font-size:12px;color:#4a5578}
.mg{display:flex;gap:12px;flex-wrap:wrap}
.mg-item{flex:1;min-width:100px;background:#0a0d14;border-radius:6px;padding:10px;text-align:center;border:1px solid #1a1f2e}
.mg-v{font-size:18px;font-weight:700;display:block}
.mg-l{font-size:9px;text-transform:uppercase;letter-spacing:1px;color:#6b7394;margin-top:3px;display:block}
.warn-box{background:#ffab0011;border-left:3px solid #ffab00;padding:8px;margin-top:8px;font-size:11px}
.btn{background:#2962ff;color:#fff;border:none;padding:6px 12px;border-radius:4px;cursor:pointer;font-family:inherit;font-size:11px;margin-top:10px}
.btn:hover{background:#0039cb}
.term{background:#000;color:#00e676;padding:12px;border-radius:6px;font-size:11px;overflow-x:auto;max-height:250px;margin-top:10px;border:1px solid #333}
.err-b{background:#ff174422;border:1px solid #ff174466;border-radius:8px;padding:10px 16px;margin-bottom:16px;font-size:12px;color:#ff6b6b}

/* Stili specifici per l'Advisor */
.adv-card{margin-bottom:12px; padding-bottom:12px; border-bottom:1px dashed #1e2740}
.adv-title{display:flex; justify-content:space-between; font-weight:600; font-size:12px; margin-bottom:6px}
.adv-val{font-size:11px; color:#c9d1e0; margin-bottom:4px}
.adv-doc{font-size:10px; color:#6b7394; font-style:italic}
.st-pass{color:#00e676} .st-warn{color:#ffab00} .st-crit{color:#ff1744}

/* Sync Pipeline Styles */
.pipe-box{background:#111827;border-radius:12px;padding:24px;border:1px solid #374151;margin-top:20px;box-shadow:inset 0 0 20px #00000080}
.pipe-tit{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:2px;color:#facc15;margin-bottom:20px;display:flex;justify-content:space-between}
.pipe-flow{display:flex;align-items:flex-start;justify-content:space-between;background:#030712;padding:20px;border-radius:8px;position:relative;flex-wrap:nowrap;overflow-x:auto}
.pipe-node-wrapper{display:flex;flex-direction:column;align-items:center;flex:1;min-width:120px;max-width:150px;z-index:2}
.pipe-node{text-align:center;background:#1e293b;padding:12px 10px;border-radius:8px;border:2px solid #475569;width:100%;transition:0.3s all;margin-bottom:8px;position:relative}
.pipe-node:hover{transform:scale(1.03)}
.pipe-lbl{font-size:11px;color:#cbd5e1;text-transform:uppercase;display:block;margin-bottom:6px;letter-spacing:1px}
.pipe-val{font-size:16px;font-weight:bold;display:block}
.pipe-sub{font-size:10px;color:#94a3b8;margin-top:4px;display:block}
.pipe-desc{font-size:9px;color:#64748b;text-align:center;line-height:1.3}
.pipe-line{position:absolute;top:45px;left:0;right:0;height:3px;background:linear-gradient(90deg, #475569 50%, transparent 50%);background-size:12px 3px;z-index:1;animation:flow 1s linear infinite}
@keyframes flow{from{background-position:0 0}to{background-position:12px 0}}
.pn-ok{border-color:#00e676;color:#00e676;box-shadow:0 0 15px #00e67640}
.pn-warn{border-color:#ffab00;color:#ffab00;box-shadow:0 0 15px #ffab0040}
.pn-crit{border-color:#ff1744;color:#ff1744;box-shadow:0 0 15px #ff174460;animation:pulse 1s infinite alternate}
@keyframes pulse{from{box-shadow:0 0 15px #ff174460}to{box-shadow:0 0 35px #ff174490}}
.crit-badge{position:absolute;top:-10px;right:-10px;background:#ef4444;color:white;font-size:9px;font-weight:bold;padding:2px 6px;border-radius:4px;box-shadow:0 2px 4px rgba(0,0,0,0.5);border:1px solid #7f1d1d;animation:pulse 1s infinite alternate;white-space:nowrap;z-index:3}
.crit-val{color:#ef4444;font-size:13px;font-weight:bold;display:block;margin-top:4px;border-top:1px dashed #ef4444;padding-top:4px}
.warn-val{color:#ffab00;font-size:13px;font-weight:bold;display:block;margin-top:4px;border-top:1px dashed #ffab00;padding-top:4px}
</style>
</head>
<body>
<div class="hdr">
  <div class="hdr-l"><div><h1>Mongot Ultimate Monitor</h1><div class="sub">KUBERNETES &bull; PROMETHEUS &bull; SRE ADVISOR</div></div></div>
  <div class="hdr-r">
    <select id="rr" onchange="setR()"><option value="3">3s</option><option value="5" selected>5s</option><option value="15">15s</option></select>
    <span id="pill" class="pill p-ok"><span class="pill-d"></span>OK</span>
  </div>
</div>

<div id="err" class="err-b" style="display:none"></div>
<div class="grid" id="grid"></div>

<script>
const $=id=>document.getElementById(id);
const fB=b=>{if(b==null||b===0)return'—';if(b>1e9)return(b/1e9).toFixed(2)+' GB';if(b>1e6)return(b/1e6).toFixed(1)+' MB';if(b>1e3)return(b/1e3).toFixed(1)+' KB';return b+' B'};
const fMs=s=>s==null||s===0?'—':s<0.001?'< 1ms':s<1?(s*1000).toFixed(1)+' ms':s.toFixed(3)+' s';
const fN=n=>n==null?'—':typeof n==='number'?n.toLocaleString('it-IT'):n;
const row=(l,v)=>`<div class="row"><span class="row-l">${l}</span><span class="row-v">${v}</span></div>`;

function pill(s){
  let c='p-b'; s=String(s).toUpperCase();
  if(['READY','RUNNING','OK','BOUND'].includes(s)) c='p-ok';
  else if(['PENDING','WAITING'].includes(s)) c='p-w';
  else if(['FAILED','ERROR','TERMINATED'].includes(s)) c='p-e';
  return`<span class="pill ${c}"><span class="pill-d"></span>${s||'?'}</span>`
}

function gaugeRing(pct,label,color,size=80){const r=(size-10)/2,circ=2*Math.PI*r,off=circ*(1-Math.min(pct/100,1));return`<div class="gauge"><svg width="${size}" height="${size}" style="transform:rotate(-90deg)"><circle cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke="#1a1f2e" stroke-width="5"/><circle cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke="${color}" stroke-width="5" stroke-linecap="round" stroke-dasharray="${circ}" stroke-dashoffset="${off}" style="transition:stroke-dashoffset 0.8s ease"/></svg><div style="margin-top:${-size/2-8}px;text-align:center;height:${size/2}px;display:flex;flex-direction:column;justify-content:center;position:relative"><span class="gauge-v" style="color:${color}">${pct.toFixed(0)}<span class="gauge-u">%</span></span></div><span class="gauge-l">${label}</span></div>`}
function mgItem(val,label,color){return`<div class="mg-item"><span class="mg-v" style="color:${color}">${val}</span><span class="mg-l">${label}</span></div>`}
function timeSince(iso){const s=Math.floor((Date.now()-new Date(iso).getTime())/1000);if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m';if(s<86400)return Math.floor(s/3600)+'h';return Math.floor(s/86400)+'d'}

// GESTIONE LOGS PERSISTENTI
let openLogs = new Set();
let logCache = {};

async function toggleLogs(ns, pod) {
  if(openLogs.has(pod)) {
      openLogs.delete(pod);
      if($(`log-${pod}`)) $(`log-${pod}`).style.display = 'none';
      if($(`btn-log-${pod}`)) $(`btn-log-${pod}`).innerText = pod.includes('operator') ? '▶ Show Live Operator Logs' : '▶ Show Live Pod Logs';
  } else {
      openLogs.add(pod);
      if($(`log-${pod}`)) {
          $(`log-${pod}`).style.display = 'block';
          $(`log-${pod}`).innerText = "Loading...";
      }
      if($(`btn-log-${pod}`)) $(`btn-log-${pod}`).innerText = pod.includes('operator') ? '▼ Hide Operator Logs' : '▼ Hide Logs';
      await fetchAndUpdateLog(ns, pod);
  }
}

async function fetchAndUpdateLog(ns, pod) {
  if(!openLogs.has(pod)) return;
  try {
      const r = await fetch(`/api/logs/${ns}/${pod}`);
      const d = await r.json();
      logCache[pod] = d.logs || "No logs available.";
      const el = $(`log-${pod}`);
      if(el) {
          const cTop = el.scrollTop, cH = el.scrollHeight, cClient = el.clientHeight;
          const atBot = cTop + cClient >= cH - 15;
          el.textContent = logCache[pod];
          el.scrollTop = atBot ? el.scrollHeight : cTop;
      }
  } catch(e) {
      if($(`log-${pod}`)) $(`log-${pod}`).innerHTML = `<span style="color:red">Error: ${e.message}</span>`;
  }
}

// ADVISOR LOGIC
function buildAdvisorHTML(d, pods, promAll, idxs) {
    let h = `<div class="c s4" style="background:#0a0d14; border:1px solid #1a1f2e; padding:20px;">
             <h3 style="color:#ffab00; margin-bottom:16px; font-size:14px; letter-spacing:1px;">🏅 COMPLIANCE & BEST PRACTICES ADVISOR</h3>`;

    // 1. Regola Spazio Disco (200% Rule + 90% Read-Only)
    let diskStatus = { state: 'PASSED', text: '', val: '' };
    let minHeadroom = 999;
    let worstPod = '';
    pods.forEach(p => {
        const dsk = (promAll[p.name] && promAll[p.name].categories && promAll[p.name].categories.disk) ? promAll[p.name].categories.disk : null;
        if(dsk && dsk.data_path_total_bytes > 0) {
            const used = dsk.data_path_total_bytes - dsk.data_path_free_bytes;
            const requiredFree = used * 2.0;
            const pctUsed = (used / dsk.data_path_total_bytes) * 100;
            if(pctUsed >= 90) {
                diskStatus.state = 'CRITICAL';
                diskStatus.val += `Pod ${p.name} disk is at ${pctUsed.toFixed(1)}%. MONGOT IS IN READ-ONLY MODE.`;
            } else if(dsk.data_path_free_bytes < requiredFree && diskStatus.state !== 'CRITICAL') {
                diskStatus.state = 'WARNING';
                diskStatus.val = `On pod ${p.name}, free space (${fB(dsk.data_path_free_bytes)}) is LESS than 200% of current index size (${fB(requiredFree)} required).`;
            }
            const ratio = dsk.data_path_free_bytes / (used || 1);
            if(ratio < minHeadroom) { minHeadroom = ratio; worstPod = p.name; }
        }
    });
    if(diskStatus.state === 'PASSED') {
        diskStatus.val = `All pods have free space > 200% of the used size (Worst safety ratio: ${(minHeadroom*100).toFixed(0)}% on ${worstPod||'N/A'}).`;
    }

    // 2. Index Consolidation
    let idxStatus = { state: 'PASSED', text: '', val: '' };
    const nsCounts = {};
    idxs.forEach(i => nsCounts[i.ns] = (nsCounts[i.ns]||0)+1);
    const badNs = Object.entries(nsCounts).filter(([ns, c]) => c > 1);
    if(badNs.length > 0) {
        idxStatus.state = 'WARNING';
        idxStatus.val = `Multiple indexes detected on collections: ${badNs.map(([ns,c])=>`${ns} (${c})`).join(', ')}. Action: Consolidate into a single dynamic index.`;
    } else {
        idxStatus.val = `No collection has more than one search index. Optimal.`;
    }

    // 3. I/O Bottleneck
    let ioStatus = { state: 'PASSED', text: '', val: 'No I/O bottleneck and Replica Lag detected on K8s disks.' };
    pods.forEach(p => {
        const cat = (promAll[p.name] && promAll[p.name].categories) || {};
        const qLen = cat.disk ? cat.disk.queue_length : 0;
        const lag = cat.indexing ? cat.indexing.change_stream_lag_sec : 0;
        if(qLen > 10 && lag > 5) {
            ioStatus.state = 'CRITICAL';
            ioStatus.val = `Pod ${p.name}: HIGH disk queue (${qLen}) and Oplog Lag increasing (${lag}s). Action: Scale Storage class / increase PVC IOPS.`;
        }
    });

    // 4. CPU & QPS (Official Sizing: < 80% CPU, 10 QPS / Core)
    let qpsStatus = { state: 'PASSED', text: '', val: '' };
    let totalCores = 0;
    let maxCpuUsage = 0;
    pods.forEach(p => {
        totalCores += p.cpu_limit_cores;
        const prom = promAll[p.name];
        if(prom && prom.categories && prom.categories.process && prom.categories.process.cpu_usage) {
           maxCpuUsage = Math.max(maxCpuUsage, prom.categories.process.cpu_usage);
        }
    });
    if(totalCores === 0 && pods.length > 0) { // Fallback
        const prom0 = promAll[pods[0].name];
        const cpuCnt = (prom0 && prom0.categories && prom0.categories.process) ? prom0.categories.process.cpu_count : 1;
        totalCores = cpuCnt * pods.length;
    }
    totalCores = totalCores || 1;
    const maxCpuPct = maxCpuUsage * 100;
    const qps = (d.search_perf && d.search_perf.queries_per_sec) ? d.search_perf.queries_per_sec : 0;
    
    if (maxCpuPct > 80) {
        qpsStatus.state = 'CRITICAL';
        qpsStatus.val = `CPU Usage is ${maxCpuPct.toFixed(1)}% (above 80% threshold). Node is overloaded, scale up immediately.`;
    } else if(qps > totalCores * 10) {
        qpsStatus.state = 'WARNING';
        qpsStatus.val = `The cluster handles ${qps} QPS with only ${totalCores} cores. You are above the target (1 core per 10 QPS), but CPU is under 80% (${maxCpuPct.toFixed(1)}%).`;
    } else {
        qpsStatus.val = `Highest CPU is ${maxCpuPct.toFixed(1)}%. Allocated ${totalCores} Cores for ${qps} QPS. Ratio within guidelines.`;
    }

    // 4.5 Memory Page Faults (Official Sizing: > 1000/s is starvation)
    let pfStatus = { state: 'PASSED', text: '', val: 'Major Page Faults per second are well within safe thresholds.' };
    pods.forEach(p => {
        const prom = promAll[p.name];
        if(!prom || !prom.categories || !prom.categories.memory) return;
        const pf = prom.categories.memory.major_page_faults_sec || 0;
        if(pf > 1000) {
            pfStatus.state = 'CRITICAL';
            pfStatus.val = `Pod ${p.name} is experiencing ${pf.toFixed(0)} Major Page Faults / sec! This indicates Memory Starvation. Increase pod RAM limits immediately.`;
        } else if (pf > 500 && pfStatus.state !== 'CRITICAL') {
            pfStatus.state = 'WARNING';
            pfStatus.val = `Pod ${p.name} is experiencing ${pf.toFixed(0)} Major Page Faults / sec. Monitor memory usage closely.`;
        }
    });

    // 5. Rischio OOMKilled (Memoria MMap vs Heap)
    let oomStatus = { state: 'PASSED', text: '', val: '' };
    let hasOomKilled = false;
    pods.forEach(p => {
        if(p.containers.some(c => c.last_reason === 'OOMKilled')) hasOomKilled = true;
        const prom = promAll[p.name];
        if(!prom) return;
        const jvm = prom.categories.jvm;
        const sysMem = prom.categories.memory;
        if(jvm && sysMem && sysMem.phys_total_bytes > 0 && jvm.heap_max_bytes > 0) {
            const heapRatio = jvm.heap_max_bytes / sysMem.phys_total_bytes;
            if(heapRatio > 0.6) {
                oomStatus.state = 'WARNING';
                oomStatus.val = `Pod ${p.name}: K8s Max Heap is set to ${(heapRatio*100).toFixed(0)}% of the total RAM limit. Lucene requires significant RAM (Mmap) for off-heap files. It is recommended to limit Heap to 50% or less.`;
            }
        }
    });
    if(hasOomKilled) {
        oomStatus.state = 'CRITICAL';
        oomStatus.val = `OOMKilled events detected! Increase resource requests/limits in the MCK CRD and reduce mongot maxCapacityMB.`;
    } else if(oomStatus.state === 'PASSED') {
        oomStatus.val = `No OOMKilled events detected. Heap limits within safe parameters for allocating RAM to System files (Mmap).`;
    }
    
    // 6. Stato CRD Operator
    let crdStatus = { state: 'PASSED', text: '', val: 'CRDs managed by the K8s Operator are in a correct state (Running).' };
    d.mongodbsearch_crds.forEach(c => {
        if(c.phase !== 'Running') {
            crdStatus.state = 'CRITICAL';
            crdStatus.val = `CRD ${c.name} in namespace ${c.namespace} is in state: ${c.phase}! MCK Operator reconciliation failed. Check operator logs.`;
        }
    });

    // 7. Storage Class Advisor
    let storageStatus = { state: 'PASSED', val: 'No obviously slow StorageClass detected for Search nodes.' };
    if (d.mongot_pvcs && d.mongot_pvcs.length > 0) {
        const slowClasses = d.mongot_pvcs.filter(p => p.storage_class && (p.storage_class.includes('hostpath') || p.storage_class.includes('standard') || p.storage_class.includes('slow')));
        if (slowClasses.length > 0) {
            storageStatus.state = 'WARNING';
            storageStatus.val = `Found PVCs associated with slow or default StorageClasses (${slowClasses.map(p=>p.storage_class).join(', ')}). MongoDB Search requires high-performance NVMe/SSD disks (e.g. gp3, io2) for Lucene I/O.`;
        } else {
            storageStatus.val = `StorageClasses detected in use: ${[...new Set(d.mongot_pvcs.map(p=>p.storage_class))].join(', ')}. Ensure they are high-throughput disks.`;
        }
    }

    // 8. Versioning Advisor
    let verStatus = { state: 'PASSED', val: `Environment up to date or correct versioning. K8s: ${d.k8s_version||'N/A'}` };
    if (d.operator && d.operator.image) {
        if (d.operator.image.endsWith(':latest')) {
            verStatus.state = 'WARNING';
            verStatus.val = `The Operator image (${d.operator.image}) uses the ':latest' tag. In production (MCK), always use exact immutable tags.`;
        } else {
            verStatus.val = `The Operator uses an exact immutable tag: ${d.operator.image.split(':').pop()||'OK'}. K8s Cluster Version: ${d.k8s_version||'N/A'}.`;
        }
    }

    // Builder riga HTML
    const stCls = { 'PASSED': 'st-pass', 'WARNING': 'st-warn', 'CRITICAL': 'st-crit' };
    const stIco = { 'PASSED': '🟢 PASSED', 'WARNING': '🟡 WARNING', 'CRITICAL': '🔴 CRIT' };

    h += `<div class="adv-card">
            <div class="adv-title"><span>Disk Space (200% Rule)</span><span class="${stCls[diskStatus.state]}">${stIco[diskStatus.state]}</span></div>
            <div class="adv-val"><b>Detected:</b> ${diskStatus.val}</div>
            <div class="adv-doc">📖 Doc: "Allocate double the disk space your index requires. mongot becomes read-only when disk utilization reaches 90%."</div>
          </div>`;
          
    h += `<div class="adv-card">
            <div class="adv-title"><span>Index Consolidation</span><span class="${stCls[idxStatus.state]}">${stIco[idxStatus.state]}</span></div>
            <div class="adv-val"><b>Detected:</b> ${idxStatus.val}</div>
            <div class="adv-doc">📖 Doc: "Avoid defining multiple, separate search indexes on a single collection. Each index adds overhead."</div>
          </div>`;

    h += `<div class="adv-card">
            <div class="adv-title"><span>I/O Bottleneck & Replica</span><span class="${stCls[ioStatus.state]}">${stIco[ioStatus.state]}</span></div>
            <div class="adv-val"><b>Detected:</b> ${ioStatus.val}</div>
            <div class="adv-doc">📖 Doc: "If disk I/O queue length is high and replication lag is growing, you need to scale up your hardware."</div>
          </div>`;

    h += `<div class="adv-card">
            <div class="adv-title"><span>MongoDB Search CRD Status</span><span class="${stCls[crdStatus.state]}">${stIco[crdStatus.state]}</span></div>
            <div class="adv-val"><b>Detected:</b> ${crdStatus.val}</div>
            <div class="adv-doc">📖 Doc: "A ReconcileFailed state indicates the Kubernetes Operator cannot apply the desired spec (network issue, resource quota)."</div>
          </div>`;

    h += `<div class="adv-card">
            <div class="adv-title"><span>Storage Class Performance (PVC)</span><span class="${stCls[storageStatus.state]}">${stIco[storageStatus.state]}</span></div>
            <div class="adv-val"><b>Detected:</b> ${storageStatus.val}</div>
            <div class="adv-doc">📖 Doc: "MongoDB requires high performance disks. Using standard or hostPath provisioners might cause MMap flushing issues and severe IO wait."</div>
          </div>`;

    h += `<div class="adv-card">
            <div class="adv-title"><span>K8s Operator Versioning (MCK)</span><span class="${stCls[verStatus.state]}">${stIco[verStatus.state]}</span></div>
            <div class="adv-val"><b>Detected:</b> ${verStatus.val}</div>
            <div class="adv-doc">📖 Doc: "Using the :latest tag on the Kubernetes Operator implies unexpected breaking changes on pod restarts."</div>
          </div>`;

    h += `<div class="adv-card">
            <div class="adv-title"><span>CPU Usage & QPS (80% Rule)</span><span class="${stCls[qpsStatus.state]}">${stIco[qpsStatus.state]}</span></div>
            <div class="adv-val"><b>Detected:</b> ${qpsStatus.val}</div>
            <div class="adv-doc">📖 Doc: "If CPU usage is consistently above 80%, you likely need to scale up. 1 CPU core for every 10 QPS is a starting point."</div>
          </div>`;

    h += `<div class="adv-card">
            <div class="adv-title"><span>Memory Starvation (Page Faults)</span><span class="${stCls[pfStatus.state]}">${stIco[pfStatus.state]}</span></div>
            <div class="adv-val"><b>Detected:</b> ${pfStatus.val}</div>
            <div class="adv-doc">📖 Doc: "If Search Page Faults are consistently over 1000 per second, your system needs more memory."</div>
          </div>`;

    h += `<div class="adv-card" style="border-bottom:none; margin-bottom:0; padding-bottom:0;">
            <div class="adv-title"><span>OOMKilled & MMap Risk</span><span class="${stCls[oomStatus.state]}">${stIco[oomStatus.state]}</span></div>
            <div class="adv-val"><b>Detected:</b> ${oomStatus.val}</div>
            <div class="adv-doc">📖 Doc: "mongot utilizes memory-mapped files. The container memory limit MUST be substantially higher than the internal maxCapacityMB heap."</div>
          </div>`;

    // 4. PREDICTIVE SRE: OPLOG WINDOW
    if (d.oplog_info && d.oplog_info.head_timestamp) {
        let worst_lag_sec = 0;
        pods.forEach(p => {
            const prom = promAll[p.name]||{};
            // Estimating lag using Prometheus change_stream_lag_sec or heuristic fallback
            let p_lag = 0;
            if(prom.categories && prom.categories.indexing && prom.categories.indexing.change_stream_lag_sec) {
                p_lag = prom.categories.indexing.change_stream_lag_sec;
            }
            if(p_lag > worst_lag_sec) worst_lag_sec = p_lag;
        });
        
        let opSt = 'ok'; let opMsg = 'Ample and Safe Oplog Window';
        let opDoc = `Estimated total window: ${d.oplog_info.window_hours}h. Max current lag: ${Math.round(worst_lag_sec)}s`;
        
        if (d.oplog_info.window_hours > 0 && worst_lag_sec > 0) {
            let lag_hours = worst_lag_sec / 3600;
            if (lag_hours > (d.oplog_info.window_hours * 0.7)) {
                opSt = 'crit'; opMsg = 'CRITICAL: Mongot Lag has consumed +70% of the Oplog!';
                opDoc = "⚠️ If this continues, Mongot will lose the Resume Token and crash (forced Initial Sync). Increase MongoDB Oplog size or restart mongot!";
            } else if (lag_hours > (d.oplog_info.window_hours * 0.4)) {
                opSt = 'warn'; opMsg = 'Warning: Mongot heavily lagging in Replication';
            }
        }
        
        h += `<div class="adv-card" style="margin-top:12px; border-top:1px dashed #1e2740; padding-top:12px; border-bottom:none; margin-bottom:0; padding-bottom:0;">
                <div class="adv-title"><span>🔥 Predictive SRE: Oplog Window Exceeded</span><span class="${stCls[opSt]}">${stIco[opSt]}</span></div>
                <div class="adv-val" style="color:${stCls[opSt]}"><b>Status:</b> ${opMsg}</div>
                <div class="adv-doc">📖 ${opDoc}</div>
              </div>`;
    }

    h += `</div>`;
    return h;
}


function render(d) {
  const pods=d.mongot_pods||[], crds=d.mongodbsearch_crds||[], op=d.operator||{};
  const pvcs=d.mongot_pvcs||[], svcs=d.mongot_services||[], idxs=d.search_indexes||[];
  const promAll=d.mongot_prometheus||{};
  const anyPod=pods.length>0, allOk=anyPod&&pods.every(p=>p.phase==='Running'&&p.all_ready);
  
  const sp=$('pill'); sp.className='pill '+(allOk?'p-ok':anyPod?'p-w':'p-e');
  sp.innerHTML=`<span class="pill-d"></span>${allOk?'ALL OK':anyPod?'WARN':'NO PODS'}`;

  let h='';

  // 0. GLOBAL DIAGNOSTIC ERRORS
  if (d.global_errors && d.global_errors.length > 0) {
      h += `<div class="c s4" style="background:#ff174411; border:1px solid #ff174466; border-left:4px solid #ff1744;">
              <div class="c-h" style="border-bottom:none; margin-bottom:8px;"><span>🚨</span><span class="c-t" style="color:#ff6b6b;font-weight:700">DIAGNOSTIC & CONNECTION ERRORS</span></div>
              <div style="font-size:11px; color:#c9d1e0; margin-bottom:10px; padding:0 12px;">The Python backend detected network or permission failures. Some routes or metrics may be missing:</div>
              <ul style="margin:0; padding:0 12px 12px 30px; font-size:11px; color:#ffb4b4; line-height:1.6; font-family:monospace;">`;
      d.global_errors.forEach(err => {
          h += `<li>${err}</li>`;
      });
      h += `  </ul>
            </div>`;
  } else {
      h += `<div class="c s4" style="background:#00e67611; border:1px solid #00e67644; border-left:4px solid #00e676; padding:12px;">
              <div style="display:flex; align-items:center; gap:10px;">
                  <span style="font-size:16px;">✅</span>
                  <div>
                      <div style="color:#00e676; font-weight:700; font-size:12px; margin-bottom:2px; text-transform:uppercase; letter-spacing:1px;">No Errors Detected (All Systems Operational)</div>
                      <div style="color:#c9d1e0; font-size:11px;">All connections (K8s API, MongoDB Auth, Prometheus Scraping) are active and functioning.</div>
                  </div>
              </div>
            </div>`;
  }

  // 1. OPLOG E DISCOVERY
  h+=`<div class="c s2"><div class="c-h"><span>🌍</span><span class="c-t">Global DB Status</span></div>`;
  if(d.oplog_info && d.oplog_info.head_time) {
      h+=row('Oplog Head (Last Write)', `<span style="color:#00e676">${d.oplog_info.head_time}</span>`);
      h+=row('Oplog Window (Max Lag)', `<span class="${d.oplog_info.window_hours<6?'red':d.oplog_info.window_hours<24?'ylw':'grn'}">${d.oplog_info.window_hours} hours</span>`);
  } else h+=row('Oplog Info', '<span style="color:#ffab00">Not available</span>');
  h+=row('MongoDB Conn.', d.mongo_connected?'<span class="grn">Connected</span>':'<span class="red">N/A</span>');
  h+=row('K8s API Conn.', (pods.length||crds.length||op.name)?'<span class="grn">Connected</span>':'<span class="red">N/A</span>');
  h+=row('Collection time',`${d._collect_ms||'?'} ms`);
  h+=`</div>`;

  h+=`<div class="c s2"><div class="c-h"><span>📋</span><span class="c-t">K8s Discovery</span></div>`;
  h+=row('K8s Cluster',`<span class="cyn">${d.k8s_version||'N/A'}</span>`);
  if(op.name) {
      const opVer = op.image && op.image.includes(':') ? op.image.split(':').pop() : 'N/A';
      h+=row('Operator Ver.',`<span class="pur">${opVer}</span>`);
      h+=row('Operator Pod',`${op.name} (${op.replicas||0}/${op.desired||1})`);
      const rpod = op.pod_name || op.name;
      const isop=openLogs.has(rpod);
      h+=`<div style="margin-top:6px;margin-bottom:6px;display:flex;gap:6px">
             <button id="btn-log-${rpod}" class="btn" style="flex:1;font-size:10px;padding:4px" onclick="toggleLogs('${op.namespace}', '${rpod}')">${isop?'▼ Hide Operator Logs':'▶ Show Live Operator Logs'}</button>
             <button onclick="promptDownloadLog('${op.namespace}', '${rpod}')" class="btn" style="padding:4px 8px;font-size:10px;background:#1e3a8a;color:#93c5fd;border-radius:4px;display:flex;align-items:center;">⬇️ Download (.txt)</button>
          </div>`;
      h+=`<pre id="log-${rpod}" class="term" style="display:${isop?'block':'none'};margin-top:4px">${logCache[rpod]||'Loading...'}</pre>`;
  }
  h+=row('CRDs Found',`<span class="pur">${crds.length}</span>`);
  h+=row('mongot Pods',`<span class="blu">${pods.length}</span>`);
  h+=row('Search Indexes',`<span class="grn">${idxs.length}</span>`);
  h+=row('PVC',`${pvcs.length}`) + row('Services',`${svcs.length}`);
  const helm=d.helm_releases||[];
  if (helm.length > 0) {
      helm.forEach(r => {
          const stColor = r.status === 'deployed' ? '#00e676' : '#ff1744';
          h += row(`Helm: ${r.namespace}`, `<span style="color:${stColor}" title="Updated: ${r.modifiedAt_str}">${r.name} (Rev ${r.revision}) - ${r.status}</span>`);
      });
  }
  h+=`</div>`;

  // 2. SRE ADVISOR PANEL
  h += buildAdvisorHTML(d, pods, promAll, idxs);

  // 3. PODS & PROMETHEUS METRICS
  const pm=d.pod_metrics||{};

  pods.forEach(p => {
    const isOOM = p.containers.some(c => c.last_reason === 'OOMKilled');
    const m=pm[p.name]||{}, prom=promAll[p.name]||{}, cat=prom.categories||{};
    const sc=cat.search_commands||{}, jvm=cat.jvm||{}, proc=cat.process||{}, mem=cat.memory||{}, dsk=cat.disk||{}, net=cat.network||{}, idx=cat.indexing||{}, luc=cat.lucene_merge||{}, lc=cat.lifecycle||{};

    h+=`<div class="c s4"><div class="c-h"><span>🔍</span><span class="c-t">Pod: ${p.name}</span></div>`;
    h+=`<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px">`;
    h+=`<div class="pod-meta">Node: ${p.node||'—'} &bull; IP: ${p.pod_ip||'—'} &bull; NS: ${p.namespace}</div>`;
    const pTag = isOOM ? '<span class="tag tag-fail">OOMKILLED</span>' : (p.phase==='Running'?'<span class="tag tag-run">Running</span>':'<span class="tag tag-fail">'+p.phase+'</span>');
    h+=`<div style="display:flex;gap:6px">${pTag} ${p.all_ready?pill('READY'):pill('NOT READY')}</div></div>`;

    h+=`<div class="mg">`;
    h+=mgItem(p.start_time?timeSince(p.start_time):'—','Uptime','#00e676');
    h+=mgItem(p.total_restarts,'Restart',p.total_restarts>5?'#ff6b6b':p.total_restarts>0?'#ffab00':'#00e676');
    if(m.cpu_millicores!=null)h+=mgItem(m.cpu_millicores.toFixed(0)+'m','CPU (actual)','#00b0ff');
    if(m.memory_bytes!=null)h+=mgItem(fB(m.memory_bytes),'RAM (actual)','#b388ff');
    if(proc.cpu_usage)h+=mgItem((proc.cpu_usage*100).toFixed(1)+'%','JVM CPU','#00e5ff');
    if(lc.indexes_initialized)h+=mgItem(fN(lc.indexes_initialized),'Init Indexes','#00e676');
    h+=`</div>`;

    if(p.warnings && p.warnings.length > 0) {
        h += `<div class="warn-box"><strong style="color:#ffab00">⚠️ Latest K8s Events:</strong><br>`;
        p.warnings.forEach(w => { h += `&bull; <b>${w.reason}</b>: ${w.message} <i style="color:#6b7394">(${w.count}x)</i><br>`; });
        h += `</div>`;
    }

    // Live Logs Persistenti
    const isLogOpen = openLogs.has(p.name);
    h += `<div style="display:flex;gap:6px;margin-top:10px">
            <button id="btn-log-${p.name}" class="btn" style="flex:1" onclick="toggleLogs('${p.namespace}', '${p.name}')">${isLogOpen ? '▼ Hide Logs' : '▶ Show Live Pod Logs'}</button>
            <button onclick="promptDownloadLog('${p.namespace}', '${p.name}')" class="btn" style="padding:6px 12px;background:#1e3a8a;color:#93c5fd;border-radius:4px;display:flex;align-items:center;">⬇️ Download Log (.txt)</button>
          </div>`;
    h += `<pre id="log-${p.name}" class="term" style="display:${isLogOpen ? 'block' : 'none'}">${logCache[p.name] || 'Loading...'}</pre>`;

    if(!prom.available){
      h+=`<div style="margin-top:14px;font-size:11px;color:#ff6b6b">No Prometheus metrics found. Fallbacks (Net, Proxy, Exec) failed.</div></div>`;
      return;
    }

    h+=`<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:14px">`;

    // 1. Search Commands
    h+=`<div style="background:#0a0d14;border-radius:8px;padding:12px;border:1px solid #1a1f2e">`;
    h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#00b0ff;margin-bottom:8px">🔎 Search Commands</div>`;
    h+=row('$search latency',`<span class="${sc.search_latency_sec>0.5?'red':sc.search_latency_sec>0.1?'ylw':'grn'}">${fMs(sc.search_latency_sec)}</span>`);
    h+=row('$search failures',`<span class="${sc.search_failures>0?'red':'grn'}">${fN(sc.search_failures)}</span>`);
    h+=row('$vectorSearch lat.',`<span class="${sc.vectorsearch_latency_sec>1?'red':sc.vectorsearch_latency_sec>0.3?'ylw':'grn'}">${fMs(sc.vectorsearch_latency_sec)}</span>`);
    h+=row('$vectorSearch fail',`<span class="${sc.vectorsearch_failures>0?'red':'grn'}">${fN(sc.vectorsearch_failures)}</span>`);
    h+=row('getMores latency',`<span class="blu">${fMs(sc.getmores_latency_sec)}</span>`);
    h+=row('manageIndex lat.',`<span class="blu">${fMs(sc.manage_index_latency_sec)}</span>`);
    h+=`</div>`;

    // 2. JVM Heap
    const heapPct=jvm.heap_max_bytes>0?(jvm.heap_used_bytes/jvm.heap_max_bytes)*100:0;
    h+=`<div style="background:#0a0d14;border-radius:8px;padding:12px;border:1px solid #1a1f2e">`;
    h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#b388ff;margin-bottom:8px">☕ JVM Heap &amp; GC</div>`;
    h+=`<div style="display:flex;justify-content:center;margin-bottom:6px">${gaugeRing(heapPct,'Heap Used',heapPct>85?'#ff1744':heapPct>65?'#ffab00':'#b388ff',70)}</div>`;
    h+=row('Used',`<span class="pur">${fB(jvm.heap_used_bytes)}</span>`);
    h+=row('Max',fB(jvm.heap_max_bytes));
    h+=row('GC pause max',`<span class="${jvm.gc_pause_seconds_max>0.5?'red':jvm.gc_pause_seconds_max>0.1?'ylw':'grn'}">${fMs(jvm.gc_pause_seconds_max)}</span>`);
    h+=row('Buffer used',fB(jvm.buffer_used_bytes));
    h+=`</div>`;

    // 3. Indexing Pipeline
    h+=`<div style="background:#0a0d14;border-radius:8px;padding:12px;border:1px solid #1a1f2e">`;
    h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#00e676;margin-bottom:8px">📥 Indexing Pipeline</div>`;
    h+=row('Indexes in catalog',`<span class="grn">${fN(idx.indexes_in_catalog)}</span>`);
    h+=row('Applied CS updates',`<span class="grn">${fN(idx.steady_applicable_updates)}</span>`);
    h+=row('Batches in progress',`<span class="cyn">${fN(idx.steady_batches_in_progress)}</span>`);
    h+=row('Oplog Lag',`<span class="${idx.change_stream_lag_sec>5?'red':'grn'}">${fN(idx.change_stream_lag_sec)} s</span>`);
    h+=row('Unexpected failures',`<span class="${idx.steady_unexpected_failures>0?'red':'grn'}">${fN(idx.steady_unexpected_failures)}</span>`);
    h+=row('Active initial syncs',`<span class="blu">${fN(idx.initial_sync_in_progress)}</span>`);
    h+=`</div>`;

    // 4. System Disk
    const diskPct=dsk.data_path_total_bytes>0?((dsk.data_path_total_bytes-dsk.data_path_free_bytes)/dsk.data_path_total_bytes)*100:0;
    h+=`<div style="background:#0a0d14;border-radius:8px;padding:12px;border:1px solid #1a1f2e">`;
    h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#ffab00;margin-bottom:8px">💾 Disk (data path)</div>`;
    h+=`<div style="display:flex;justify-content:center;margin-bottom:6px">${gaugeRing(diskPct,'Disk Used',diskPct>90?'#ff1744':diskPct>75?'#ffab00':'#00e676',70)}</div>`;
    h+=row('Used',`<span class="ylw">${fB(dsk.data_path_total_bytes-dsk.data_path_free_bytes)}</span>`);
    h+=row('Total',fB(dsk.data_path_total_bytes));
    h+=row('Read I/O',fB(dsk.read_bytes));
    h+=row('Write I/O',fB(dsk.write_bytes));
    h+=row('Queue len',`<span class="${dsk.queue_length>5?'red':dsk.queue_length>1?'ylw':'grn'}">${fN(dsk.queue_length)}</span>`);
    h+=`</div>`;

    // 5. Lucene Merge Scheduler
    h+=`<div style="background:#0a0d14;border-radius:8px;padding:12px;border:1px solid #1a1f2e">`;
    h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#00e5ff;margin-bottom:8px">🔀 Lucene Merges</div>`;
    h+=row('Active merges',`<span class="cyn">${fN(luc.running_merges)}</span>`);
    h+=row('Merging docs',`<span class="blu">${fN(luc.merging_docs)}</span>`);
    h+=row('Total merges',fN(luc.total_merges));
    h+=row('Merge time max',`<span class="ylw">${fMs(luc.merge_time_sec_max)}</span>`);
    h+=row('Discarded merges',`<span class="${luc.discarded_merges>0?'ylw':'grn'}">${fN(luc.discarded_merges)}</span>`);
    h+=`</div>`;

    // 6. System Memory + Network
    const memPct=mem.phys_total_bytes>0?(mem.phys_inuse_bytes/mem.phys_total_bytes)*100:0;
    h+=`<div style="background:#0a0d14;border-radius:8px;padding:12px;border:1px solid #1a1f2e">`;
    h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#ff6b6b;margin-bottom:8px">🖥 System &amp; Network</div>`;
    h+=row('RAM used',`<span class="${memPct>90?'red':memPct>75?'ylw':'grn'}">${fB(mem.phys_inuse_bytes)} (${memPct.toFixed(0)}%)</span>`);
    h+=row('Swap used',fB(mem.swap_inuse_bytes));
    h+=`<div style="border-top:1px solid #1a1f2e;margin:4px 0;padding-top:4px"></div>`;
    h+=row('Net recv',`<span class="blu">${fB(net.bytes_recv)}</span>`);
    h+=row('Net sent',`<span class="grn">${fB(net.bytes_sent)}</span>`);
    h+=row('Net errors',`<span class="${(net.in_errors+net.out_errors)>0?'red':'grn'}">${fN(net.in_errors+net.out_errors)}</span>`);
    h+=`</div>`;
    h+=`</div>`;

    // 7. THE KILLER FEATURE: ATLAS SEARCH SYNC PIPELINE ANALYZER
    const m_urlParams = new URLSearchParams(window.location.search);
    const mergeThreshold = parseFloat(m_urlParams.get('merge_threshold')) || 3.0;

    const vitals = d.mongo_vitals || {};
    let lag_sec = idx.change_stream_lag_sec || 0; // Estratto direttamente da mongot Prometheus!
    let lag_str = `${lag_sec.toFixed(1)}s`; let lag_color = "#00e676";
    if (lag_sec > 120) { lag_str = `${lag_sec.toFixed(1)}s delay`; lag_color = "#ff1744"; }
    else if (lag_sec > 15) { lag_str = `${lag_sec.toFixed(1)}s delay`; lag_color = "#ffab00"; }
    else if (lag_sec > 0.5) { lag_str = `${lag_sec.toFixed(1)}s`; lag_color = "#ffeb3b"; }

    // Analizziamo i colli di bottiglia (Bottlenecks)
    // 1. Oplog Stream (Mongo -> RAM)
    let stream_cls = (idx.steady_batches_in_progress > 2 || lag_sec > 30) ? 'pn-warn' : 'pn-ok';
    if(lag_sec > 120 && idx.steady_applicable_updates == 0) stream_cls = 'pn-crit'; // Change stream rotto o impiccato

    // 2. RAM Parsing
    let ram_cls = jvm.heap_used_bytes > (jvm.heap_max_bytes * 0.85) ? 'pn-crit' : 'pn-ok';
    let ram_alert_html = '';
    if (idx.steady_batch_sec_max > 2.0) {
        ram_cls = idx.steady_batch_sec_max > 5.0 ? 'pn-crit' : 'pn-warn';
        let av_cls = idx.steady_batch_sec_max > 5.0 ? 'crit-val' : 'warn-val';
        ram_alert_html = `<span class="${av_cls}">⏳ SLOW: ${idx.steady_batch_sec_max.toFixed(1)}s</span>
                          ${idx.steady_batch_sec_max > 5.0 ? '<div class="crit-badge">BOTTLENECK!</div>' : ''}`;
    }

    // 3. Lucene Disk IO
    let disk_cls = luc.running_merges > 0 && dsk.queue_length > 2 ? 'pn-warn' : 'pn-ok';
    let disk_alert_html = '';
    if(luc.merge_time_sec_max > (mergeThreshold * 0.5)) {
        disk_cls = luc.merge_time_sec_max > mergeThreshold ? 'pn-crit' : 'pn-warn';
        let d_cls = luc.merge_time_sec_max > mergeThreshold ? 'crit-val' : 'warn-val';
        disk_alert_html = `<span class="${d_cls}">⏳ SLOW: ${luc.merge_time_sec_max.toFixed(1)}s</span>
                           ${luc.merge_time_sec_max > mergeThreshold ? '<div class="crit-badge">BOTTLENECK!</div>' : ''}`;
    }

    h+=`<div class="pipe-box">
          <div class="pipe-tit">
            <span>🚀 Sync Pipeline Analyzer</span>
            <div>
              <span style="color:#facc15; font-size:10px; margin-right:15px; font-weight:normal; cursor:pointer;" onclick="let t=prompt('Enter new Merge threshold in sec:', '${mergeThreshold}'); if(t) window.location.search='?merge_threshold='+t;">
                Merge alarm threshold: <b>${mergeThreshold}s (edit)</b>
              </span>
              <span style="color:${lag_color}">Lag Search Sync: <b>${lag_str}</b></span>
            </div>
          </div>
          <div class="pipe-flow">
            <div class="pipe-line"></div>
            
            <div class="pipe-node-wrapper" title="Connessioni db: ${vitals.connections_active} / Lock attivi: ${vitals.active_writers}">
              <div class="pipe-node pn-ok">
                <span class="pipe-lbl">MongoDB</span>
                <span class="pipe-val" style="font-size:14px">Oplog</span>
                <span class="pipe-sub">Conn: ${fN(vitals.connections_active)} | Lcks: ${vitals.active_writers}</span>
                <span class="pipe-sub" style="color:#00e676; font-size:10px; margin-top:6px; font-weight:bold;">Write Ops: + ${fN(vitals.ops_insert_sec + vitals.ops_update_sec + vitals.ops_delete_sec)}/s</span>
              </div>
              <div class="pipe-desc">Data origin.<br>Records every database edittion.</div>
            </div>
            
            <div class="pipe-node-wrapper">
              <div class="pipe-node ${stream_cls}">
                <span class="pipe-lbl">Stream</span>
                <span class="pipe-val">${fN(idx.steady_applicable_updates)} <span style="font-size:10px; font-weight:normal; color:#94a3b8">Total</span></span>
                <span class="pipe-sub" style="color:#00e676; font-size:11px; font-weight:bold; margin-top:6px">+ ${fN(idx.steady_applicable_updates_sec || 0)}/s</span>
              </div>
              <div class="pipe-desc">Real-time reading.<br>Captures data from the Oplog.</div>
            </div>
            
            <div class="pipe-node-wrapper">
              <div class="pipe-node ${ram_cls}">
                <span class="pipe-lbl">RAM Parse</span>
                <span class="pipe-val" style="font-size:14px">${fB(jvm.heap_used_bytes)}</span>
                <span class="pipe-sub">on ${fB(jvm.heap_max_bytes)} Heap</span>
                <span class="pipe-sub" style="color:#facc15; font-size:10px; margin-top:6px">${(idx.steady_batch_sec_max * 1000).toFixed(0)} ms lat | CPU: ${(promAll[p.name]?.categories?.process?.cpu_usage || 0).toFixed(1)}%</span>
                ${ram_alert_html}
              </div>
              <div class="pipe-desc">JVM usage.<br>Delays if CPU or RAM saturate.</div>
            </div>
            
            <div class="pipe-node-wrapper" title="Merge: background disk defragmentation. Even under presonre (red), docs may be searchable in RAM segments. Doesn't necessarily mean user-facing lag.">
              <div class="pipe-node ${disk_cls}">
                <span class="pipe-lbl">Lucene Merge</span>
                <span class="pipe-val">${fN(luc.total_merges)}</span>
                <span class="pipe-sub">Total runs</span>
                <span class="pipe-sub" style="color:#00b8d4; font-size:10px; margin-top:6px; font-weight:bold;">Disk Queue: ${fN(promAll[p.name]?.categories?.disk?.queue_length || 0)}</span>
                ${disk_alert_html}
              </div>
              <div class="pipe-desc">Disk write.<br>Merges data into the Lucene index.</div>
            </div>
            
            <div class="pipe-node-wrapper" title="Search Sync Lag: actual time between MongoDB write and search availability. Also spans RAM segments before disk merge!">
              <div class="pipe-node ${lag_sec>30?'pn-warn':'pn-ok'}">
                <span class="pipe-lbl">$search</span>
                <span class="pipe-val" style="font-size:14px">${lag_sec>30?'OLD':'READY'}</span>
                <span class="pipe-sub">Query: ${(promAll[p.name]?.categories?.search_commands?.search_latency_sec * 1000 || 0).toFixed(0)} ms</span>
                <span class="pipe-sub" style="color:#ea15f2; font-size:10px; margin-top:4px; font-weight:bold;">AI Vector: ${(promAll[p.name]?.categories?.search_commands?.vectorsearch_latency_sec * 1000 || 0).toFixed(0)} ms</span>
              </div>
              <div class="pipe-desc">Atlas Search index.<br>Client response times.</div>
            </div>
          </div>
        </div>`;

    h+=`</div>`;
  });

  if(!pods.length) h+=`<div class="c s4"><div class="empty">No mongot pod found</div></div>`;

  // 4. TABELLA INDICI
  h+=`<div class="c s4"><div class="c-h"><span>📑</span><span class="c-t">Search Indexes (${idxs.length})</span></div>`;
  if(idxs.length){h+=`<table><thead><tr><th>Name</th><th>Collection</th><th>Type</th><th>Status</th><th>Queryable</th><th>Documents</th></tr></thead><tbody>`;
  idxs.forEach(i=>{const v=i.type==='vectorSearch';h+=`<tr><td style="font-weight:600;color:#e8ecf4">${i.name}</td><td style="font-size:11px">${i.ns}</td><td><span class="tag ${v?'tag-v':'tag-f'}">${v?'VECTOR':'FULL-TEXT'}</span></td><td>${pill(i.status)}</td><td>${i.queryable?'<span class="grn">✓</span>':'<span class="red">✗</span>'}</td><td>${i.num_docs!=null?fN(i.num_docs):'—'}</td></tr>`});
  h+=`</tbody></table>`}else{h+=`<div class="empty">No search index found in the database</div>`}
  h+=`</div>`;

  // 5. TABELLA PVCS E SERVICES
  if(pvcs.length||svcs.length){
    h+=`<div class="c s4"><div class="c-h"><span>💾</span><span class="c-t">Storage &amp; Services</span></div>`;
    pvcs.forEach(p=>{h+=`<div style="display:flex;justify-content:space-between;font-size:11px;padding:3px 0;border-bottom:1px solid #111827"><span style="color:#e8ecf4">📦 ${p.name} <span style="color:#6b7394;margin-left:8px">(SC: ${p.storage_class || 'N/A'})</span></span><span>${pill(p.status)} <span class="blu">${p.capacity}</span></span></div>`});
    svcs.forEach(s=>{const pts=(s.ports||[]).map(p=>`${p.port}`).join(',');h+=`<div style="display:flex;justify-content:space-between;font-size:11px;padding:3px 0;border-bottom:1px solid #111827"><span style="color:#e8ecf4">🔗 SVC: ${s.name}</span><span><span class="tag tag-v">${s.type}</span> Port(s) :${pts}</span></div>`});
    h+=`</div>`;
  }

  $('grid').innerHTML=h;

  // Richiama asincronamente i log aperti per farli aggiornare
  openLogs.forEach(pod => {
      let p = pods.find(x => x.name === pod);
      if(!p && op.pod_name && op.pod_name === pod) p = op;
      else if(!p && op.name === pod) p = op;
      if(p) fetchAndUpdateLog(p.namespace, p.name || p.pod_name);
  });
}

let iv; function setR(){if(iv)clearInterval(iv);iv=setInterval(fetchM,+$('rr').value*1000)}
async function fetchM(){
    try{
        const r=await fetch('/metrics');
        const d=await r.json();
        if(d.error) {
            $('err').style.display='block';
            $('err').innerHTML='<b>\u26A0 Error Backend:</b> ' + d.error;
            return;
        }
        $('err').style.display='none';
        render(d);
    }catch(e){
        $('err').style.display='block';
        $('err').innerHTML='<b>\u26A0 Network / Connection failed:</b> Unable to contact the Python server ('+e.message+')';
    }
}

function promptDownloadLog(ns, pod) {
    let t = prompt(`How many logs do you want to download for ${pod}?\nOptions: 10m (last 10 mins), 1h (last hour), 24h, all\n`, "1h");
    if (!t) return;
    const t_param = ['10m','1h','24h'].includes(t) ? t : 'all';
    let filterErr = confirm(`Do you want to extract ONLY rows containing errors (Error, Fatal, Exception)?\n\n[OK] = Errors Only\n[Cancel] = Full Log`);
    let lvl = filterErr ? 'error' : 'all';
    window.open(`/api/download_logs/${ns}/${pod}?time=${t_param}&level=${lvl}`, '_blank');
}

fetchM();setR();
</script></body></html>"""
    return Response(HTML, mimetype="text/html")


def main():
    global mongo_client, k8s_v1, k8s_custom, k8s_apps, TARGET_NAMESPACE
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default=None)
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--in-cluster", action="store_true")
    args = parser.parse_args()
    TARGET_NAMESPACE = args.namespace

    if K8S_AVAILABLE:
        try:
            k8s_config.load_incluster_config() if args.in_cluster else k8s_config.load_kube_config()
            k8s_v1, k8s_custom, k8s_apps = k8s_client.CoreV1Api(), k8s_client.CustomObjectsApi(), k8s_client.AppsV1Api()
            log.info("✓ K8s configurato.")
        except Exception as e: log.warning(f"✗ K8s error: {e}")

    if args.uri:
        mongo_client = MongoClient(args.uri, serverSelectionTimeoutMS=5000)
        log.info("✓ MongoDB configurato.")

    log.info(f"🚀 Dashboard Ultimate in esecuzione: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)

if __name__ == "__main__":
    main()