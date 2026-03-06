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
from datetime import datetime, timezone
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
MONGOT_PROMETHEUS_PORT = 9946


# ── Kubernetes Discovery & Events ───────────────────────
def discover_mongodbsearch_crds() -> list:
    if not k8s_custom: return []
    crds = []
    namespaces = [TARGET_NAMESPACE] if TARGET_NAMESPACE else [ns.metadata.name for ns in k8s_v1.list_namespace().items]
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
        except Exception: pass
    return crds

def discover_operator_info() -> dict:
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
                        return {
                            "name": dep.metadata.name, "namespace": ns,
                            "image": containers[0].image if containers else "?",
                            "replicas": dep.status.ready_replicas or 0, "desired": dep.spec.replicas or 1
                        }
            except Exception: pass
    except Exception: pass
    return {}

def get_pod_warnings(namespace: str, pod_name: str) -> list:
    if not k8s_v1: return []
    warnings = []
    try:
        fs = f"involvedObject.name={pod_name},type=Warning"
        events = k8s_v1.list_namespaced_event(namespace, field_selector=fs).items
        events.sort(key=lambda x: x.last_timestamp or x.event_time or datetime.min, reverse=True)
        for e in events[:5]:
            warnings.append({"reason": e.reason, "message": e.message, "count": e.count, "time": e.last_timestamp.isoformat() if e.last_timestamp else None})
    except Exception: pass
    return warnings

def discover_mongot_pods() -> list:
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
    except Exception as e: log.error(f"Errore discovery pod K8s: {e}")
    return pods

def get_mongot_pvcs() -> list:
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
    except Exception: pass
    return pvcs

def get_mongot_services() -> list:
    services = []
    if not k8s_v1: return services
    try:
        res = k8s_v1.list_namespaced_service(TARGET_NAMESPACE) if TARGET_NAMESPACE else k8s_v1.list_service_for_all_namespaces()
        for svc in res.items:
            sname = svc.metadata.name.lower()
            if "search" in sname or "mongot" in sname:
                ports = [{"port": p.port, "target": p.target_port, "protocol": p.protocol} for p in (svc.spec.ports or [])]
                services.append({"name": svc.metadata.name, "namespace": svc.metadata.namespace, "type": svc.spec.type, "ports": ports})
    except Exception: pass
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
def get_oplog_info() -> dict:
    info = {"latest_oplog_time": None, "oplog_size_mb": 0}
    if not mongo_client: return info
    try:
        db = mongo_client["local"]
        last_op = db["oplog.rs"].find().sort("$natural", -1).limit(1)
        for doc in last_op:
            if "ts" in doc: info["latest_oplog_time"] = doc["ts"].as_datetime().strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception: pass
    return info

def get_search_indexes() -> list:
    indexes = []
    if not mongo_client: return indexes
    try:
        db_names = [d for d in mongo_client.list_database_names() if d not in ("admin", "local", "config")]
        for db_name in db_names:
            db = mongo_client[db_name]
            for coll_name in db.list_collection_names():
                try:
                    for idx in db[coll_name].list_search_indexes():
                        idx_type = idx.get("type", "search")
                        idx_info = {
                            "name": idx.get("name", "unknown"),
                            "type": "vectorSearch" if idx_type == "vectorSearch" else "fullText",
                            "status": idx.get("status", "UNKNOWN"), 
                            "ns": f"{db_name}.{coll_name}",
                            "queryable": idx.get("queryable", False), 
                            "num_docs": None
                        }
                        
                        if idx_info["type"] == "fullText":
                            # Conteggio Ufficiale per Full-Text
                            try:
                                stats = db.command({
                                    "aggregate": coll_name,
                                    "pipeline": [{
                                        "$searchMeta": {
                                            "index": idx["name"],
                                            "exists": { "path": "_id" },
                                            "count": { "type": "total" }
                                        }
                                    }],
                                    "cursor": {}
                                })
                                first = (stats.get("cursor", {}).get("firstBatch") or [None])[0]
                                if first and "count" in first: 
                                    idx_info["num_docs"] = first["count"].get("total", 0)
                            except Exception: pass
                        else:
                            # HACK ENTERPRISE per Vector Search
                            try:
                                # 1. Peschiamo la definizione dell'indice
                                definition = idx.get("latestDefinition", idx.get("definition", {}))
                                fields = definition.get("fields", [])
                                vector_field = None
                                
                                # 2. Troviamo il nome esatto del campo vettoriale
                                for f in fields:
                                    if f.get("type") == "vector":
                                        vector_field = f.get("path")
                                        break
                                
                                # 3. Contiamo i documenti che hanno quel campo
                                if vector_field:
                                    idx_info["num_docs"] = db[coll_name].count_documents({vector_field: {"$exists": True}})
                                else:
                                    # Fallback finale: stimiamo i documenti totali della collection
                                    idx_info["num_docs"] = db[coll_name].estimated_document_count()
                            except Exception: pass

                        # --- AUTO-CORREZIONE DELLO STATO FANTASMA ---
                        # Se abbiamo trovato dei documenti, l'indice è matematicamente pronto
                        if idx_info["num_docs"] is not None and isinstance(idx_info["num_docs"], int) and idx_info["num_docs"] >= 0:
                            if not idx_info["status"] or idx_info["status"] == "UNKNOWN":
                                idx_info["status"] = "READY (Auto)"
                            idx_info["queryable"] = True
                            
                        indexes.append(idx_info)
                except Exception: pass
    except Exception: pass
    return indexes
    indexes = []
    if not mongo_client: return indexes
    try:
        db_names = [d for d in mongo_client.list_database_names() if d not in ("admin", "local", "config")]
        for db_name in db_names:
            db = mongo_client[db_name]
            for coll_name in db.list_collection_names():
                try:
                    for idx in db[coll_name].list_search_indexes():
                        idx_type = idx.get("type", "search")
                        idx_info = {
                            "name": idx.get("name", "unknown"),
                            "type": "vectorSearch" if idx_type == "vectorSearch" else "fullText",
                            "status": idx.get("status", "UNKNOWN"), "ns": f"{db_name}.{coll_name}",
                            "queryable": idx.get("queryable", False), "num_docs": None
                        }
                        
                        if idx_info["type"] == "fullText":
                            # Conteggio Ufficiale per Full-Text
                            try:
                                stats = db.command({
                                    "aggregate": coll_name,
                                    "pipeline": [{
                                        "$searchMeta": {
                                            "index": idx["name"],
                                            "exists": { "path": "_id" },
                                            "count": { "type": "total" }
                                        }
                                    }],
                                    "cursor": {}
                                })
                                first = (stats.get("cursor", {}).get("firstBatch") or [None])[0]
                                if first and "count" in first: 
                                    idx_info["num_docs"] = first["count"].get("total", 0)
                            except Exception: pass
                        else:
                            # HACK ENTERPRISE per Vector Search
                            try:
                                # 1. Peschiamo la definizione dell'indice
                                definition = idx.get("latestDefinition", idx.get("definition", {}))
                                fields = definition.get("fields", [])
                                vector_field = None
                                
                                # 2. Troviamo il nome esatto del campo vettoriale
                                for f in fields:
                                    if f.get("type") == "vector":
                                        vector_field = f.get("path")
                                        break
                                
                                # 3. Contiamo i documenti che hanno quel campo
                                if vector_field:
                                    idx_info["num_docs"] = db[coll_name].count_documents({vector_field: {"$exists": True}})
                                else:
                                    # Fallback finale: stimiamo i documenti totali della collection
                                    idx_info["num_docs"] = db[coll_name].estimated_document_count()
                            except Exception: pass
                            
                        indexes.append(idx_info)
                except Exception: pass
    except Exception: pass
    return indexes
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
                            "status": idx.get("status", "UNKNOWN"), "ns": f"{db_name}.{coll_name}",
                            "queryable": idx.get("queryable", False), "num_docs": None
                        }
                        
                        # --- QUERY $searchMeta DEFINITIVA ---
                        try:
                            stats = db.command({
                                "aggregate": coll_name,
                                "pipeline": [{
                                    "$searchMeta": {
                                        "index": idx["name"],
                                        "exists": { "path": "_id" },
                                        "count": { "type": "total" }
                                    }
                                }],
                                "cursor": {}
                            })
                            first = (stats.get("cursor", {}).get("firstBatch") or [None])[0]
                            if first and "count" in first: 
                                idx_info["num_docs"] = first["count"].get("total", 0)
                        except Exception: 
                            pass # Ignoriamo silenziosamente se l'indice non è ancora pronto
                            
                        indexes.append(idx_info)
                except Exception: pass
    except Exception: pass
    return indexes
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
                            "status": idx.get("status", "UNKNOWN"), "ns": f"{db_name}.{coll_name}",
                            "queryable": idx.get("queryable", False), "num_docs": None
                        }
                        
                        # --- ECCO LA QUERY CORRETTA ---
                        try:
                            stats = db.command({
                                "aggregate": coll_name,
                                "pipeline": [{
                                    "$searchMeta": {
                                        "index": idx["name"],
                                        "count": {"type": "total"}
                                    }
                                }],
                                "cursor": {}
                            })
                            first = (stats.get("cursor", {}).get("firstBatch") or [None])[0]
                            if first and "count" in first: 
                                idx_info["num_docs"] = first["count"].get("total", 0)
                        except Exception as e: 
                            pass # Se l'indice è davvero rotto, ignoriamo
                            
                        indexes.append(idx_info)
                except Exception: pass
    except Exception: pass
    return indexes
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
                            "status": idx.get("status", "UNKNOWN"), "ns": f"{db_name}.{coll_name}",
                            "queryable": idx.get("queryable", False), "num_docs": None
                        }
                        try:
                            stats = db.command({"aggregate": coll_name, "pipeline": [{"$searchMeta": {"index": idx["name"], "exists": {"path": {"wildcard": "*"}}}}]})
                            first = (stats.get("cursor", {}).get("firstBatch") or [None])[0]
                            if first and "count" in first: idx_info["num_docs"] = first["count"].get("lowerBound", 0)
                        except Exception: pass
                        indexes.append(idx_info)
                except Exception: pass
    except Exception: pass
    return indexes

def get_search_perf_from_profiler() -> dict:
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
                    "ts": {"$gte": datetime.utcfromtimestamp(time.time() - window_sec)},
                    "$or": [{"command.pipeline": {"$elemMatch": {"$search": {"$exists": True}}}},
                            {"command.pipeline": {"$elemMatch": {"$vectorSearch": {"$exists": True}}}}]
                }
                for doc in db["system.profile"].find(query).sort("ts", -1).limit(500):
                    all_durations.append(doc.get("millis", 0))
            except Exception: pass
        if all_durations:
            perf["total_queries_5m"] = len(all_durations)
            perf["queries_per_sec"] = round(len(all_durations) / window_sec, 2)
    except Exception: pass
    return perf

def scrape_mongot_prometheus(pod_name: str, namespace: str, pod_ip: str, port: int) -> dict:
    result = {"available": False, "raw_count": 0, "categories": {}}
    text = ""
    
    # 1. Fallback Rete Diretta
    try:
        resp = requests.get(f"http://{pod_ip}:{port}/metrics", timeout=2)
        if resp.status_code == 200: text = resp.text
        else: raise Exception("HTTP Failed")
    except Exception:
        if k8s_v1:
            # 2. Fallback API Proxy
            try:
                text = k8s_v1.connect_get_namespaced_pod_proxy_with_path(
                    name=f"{pod_name}:{port}", namespace=namespace, path="metrics"
                )
            except Exception:
                # 3. Fallback Exec 
                try:
                    text = stream(k8s_v1.connect_get_namespaced_pod_exec,
                                  pod_name, namespace,
                                  command=["wget", "-qO-", f"http://localhost:{port}/metrics"],
                                  stderr=False, stdin=False, stdout=True, tty=False)
                except Exception as e:
                    log.debug(f"Scrape fallito per {pod_name}: {e}")

    if not text: return result

    if hasattr(text, "data"): text = text.data
    if isinstance(text, bytes): text = text.decode('utf-8', errors='ignore')

    result["available"] = True
    raw = {}
    for line in text.split("\n"):
        if line.startswith("#") or not line.strip(): continue
        if "{" in line: key, val = line[:line.index("{")], line[line.rindex("}") + 1:].strip()
        else: parts = line.split(); key, val = (parts[0], parts[1]) if len(parts)>1 else ("", "")
        try: raw[key] = float(val)
        except: pass
    
    result["raw_count"] = len(raw)
    g = lambda k, d=0: raw.get(k, d)

    result["categories"] = {
        "search_commands": {
            "search_latency_sec": g("mongot_command_searchCommandTotalLatency_seconds_max"),
            "search_failures": g("mongot_command_searchCommandFailure_total"),
            "vectorsearch_latency_sec": g("mongot_command_vectorSearchCommandTotalLatency_seconds_max"),
            "vectorsearch_failures": g("mongot_command_vectorSearchCommandFailure_total"),
            "getmore_latency_sec": g("mongot_command_getMoreCommandTotalLatency_seconds_max"),
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
            "steady_applicable_updates": g("mongot_indexing_steadyStateChangeStream_applicableChangeStreamUpdates_total"),
            "steady_batches_in_progress": g("mongot_indexing_steadyStateChangeStream_batchesInProgressTotal"),
            "steady_batch_sec_max": g("mongot_indexing_steadyStateChangeStream_batchesInProgressTotalDurations_seconds_max"),
            "steady_unexpected_failures": g("mongot_indexing_steadyStateChangeStream_unexpectedBatchFailures_total"),
            "initial_sync_in_progress": g("mongot_initialsync_dispatcher_inProgressSyncs"), "initial_sync_queued": g("mongot_initialsync_dispatcher_queuedSyncs"),
            "change_stream_lag_sec": g("mongot_indexing_steadyStateChangeStream_lag_seconds", 0)
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



# ── API & HTML ──────────────────────────────────────────
@app.route("/api/logs/<namespace>/<pod_name>")
def pod_logs(namespace, pod_name):
    if not k8s_v1: return jsonify({"error": "K8s API non disponibile"}), 500
    try:
        return jsonify({"logs": k8s_v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, tail_lines=50)})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/metrics")
def metrics():
    t0 = time.time()
    res = {
        "operator": discover_operator_info(),
        "mongodbsearch_crds": discover_mongodbsearch_crds(),
        "mongot_pods": discover_mongot_pods(),
        "mongot_pvcs": get_mongot_pvcs(),
        "mongot_services": get_mongot_services(),
        "pod_metrics": get_pod_metrics(),
        "oplog_info": get_oplog_info(),
        "search_indexes": get_search_indexes(),
        "search_perf": get_search_perf_from_profiler(),
        "mongo_connected": mongo_client is not None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "_collect_ms": 0
    }
    prom_port = 9946
    for c in res["mongodbsearch_crds"]:
        if c.get("prometheus_enabled"): prom_port = c.get("prometheus_port", 9946)
    
    prom_metrics = {}
    for p in res["mongot_pods"]:
        prom_metrics[p["name"]] = scrape_mongot_prometheus(p["name"], p["namespace"], p.get("pod_ip","127.0.0.1"), prom_port)
    res["mongot_prometheus"] = prom_metrics
    res["_collect_ms"] = round((time.time() - t0) * 1000, 1)
    return jsonify(res)

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
select{padding:5px 8px;font-size:10px;border-radius:6px;border:1px solid #1e2740;background:#0d1117;color:#6b7394;font-family:'JetBrains Mono',monospace}
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
      if($(`btn-log-${pod}`)) $(`btn-log-${pod}`).innerText = '▶ Mostra Live Logs del Pod';
  } else {
      openLogs.add(pod);
      if($(`log-${pod}`)) {
          $(`log-${pod}`).style.display = 'block';
          $(`log-${pod}`).innerText = "Caricamento in corso...";
      }
      if($(`btn-log-${pod}`)) $(`btn-log-${pod}`).innerText = '▼ Nascondi Logs';
      await fetchAndUpdateLog(ns, pod);
  }
}

async function fetchAndUpdateLog(ns, pod) {
  if(!openLogs.has(pod)) return;
  try {
      const r = await fetch(`/api/logs/${ns}/${pod}`);
      const d = await r.json();
      logCache[pod] = d.logs || "Nessun log disponibile.";
      if($(`log-${pod}`)) $(`log-${pod}`).textContent = logCache[pod];
  } catch(e) {
      if($(`log-${pod}`)) $(`log-${pod}`).innerHTML = `<span style="color:red">Errore: ${e.message}</span>`;
  }
}

// ADVISOR LOGIC
function buildAdvisorHTML(d, pods, promAll, idxs) {
    let h = `<div class="c s4" style="background:#0a0d14; border:1px solid #1a1f2e; padding:20px;">
             <h3 style="color:#ffab00; margin-bottom:16px; font-size:14px; letter-spacing:1px;">🏅 COMPLIANCE & BEST PRACTICES ADVISOR</h3>`;

    // 1. Regola Spazio Disco (125%)
    let diskStatus = { state: 'PASSED', text: '', val: '' };
    let minHeadroom = 999;
    let worstPod = "";
    pods.forEach(p => {
        const dsk = (promAll[p.name] && promAll[p.name].categories.disk) || {};
        if(dsk.data_path_total_bytes > 0) {
            const used = dsk.data_path_total_bytes - dsk.data_path_free_bytes;
            const requiredFree = used * 1.25;
            const ratio = dsk.data_path_free_bytes / requiredFree;
            if(ratio < minHeadroom) { minHeadroom = ratio; worstPod = p.name; }
            if(dsk.data_path_free_bytes < requiredFree) {
                diskStatus.state = 'CRITICAL';
                diskStatus.val = `Sul pod ${p.name}, lo spazio libero (${fB(dsk.data_path_free_bytes)}) è INFERIORE al 125% della dimensione indici attuale (${fB(requiredFree)} richiesti). Rischio blocco!`;
            }
        }
    });
    if(diskStatus.state === 'PASSED') {
        diskStatus.val = `Tutti i pod hanno spazio libero > 125% della dimensione usata (Indice di sicurezza peggiore: ${(minHeadroom*100).toFixed(0)}% su ${worstPod||'N/A'}).`;
    }

    // 2. Consolidamento Indici
    let idxStatus = { state: 'PASSED', text: '', val: '' };
    const nsCounts = {};
    idxs.forEach(i => nsCounts[i.ns] = (nsCounts[i.ns]||0)+1);
    const badNs = Object.entries(nsCounts).filter(([ns, c]) => c > 1);
    if(badNs.length > 0) {
        idxStatus.state = 'WARNING';
        idxStatus.val = `Rilevati indici multipli sulle collection: ${badNs.map(([ns,c])=>`${ns} (${c})`).join(', ')}. Azione: Unificali in un singolo indice dinamico.`;
    } else {
        idxStatus.val = `Nessuna collection possiede più di un indice di ricerca. Ottimo.`;
    }

    // 3. I/O Bottleneck
    let ioStatus = { state: 'PASSED', text: '', val: 'Nessun collo di bottiglia I/O e Replica Lag rilevato sui dischi k8s.' };
    pods.forEach(p => {
        const cat = (promAll[p.name] && promAll[p.name].categories) || {};
        const qLen = cat.disk ? cat.disk.queue_length : 0;
        const lag = cat.indexing ? cat.indexing.change_stream_lag_sec : 0;
        if(qLen > 10 && lag > 5) {
            ioStatus.state = 'CRITICAL';
            ioStatus.val = `Pod ${p.name}: Coda disco ALTA (${qLen}) e Oplog Lag in crescita (${lag}s). Azione: Scala classe Storage / aumenta IOPS PVC.`;
        }
    });

    // 4. CPU / QPS
    let qpsStatus = { state: 'PASSED', text: '', val: '' };
    let totalCores = 0;
    pods.forEach(p => totalCores += p.cpu_limit_cores);
    if(totalCores === 0) { // Fallback su process metrics se K8s limits assenti
        pods.forEach(p => {
            const prom = promAll[p.name];
            if(prom && prom.categories.process && prom.categories.process.cpu_count) totalCores = Math.max(totalCores, prom.categories.process.cpu_count);
        });
    }
    totalCores = totalCores || 1;
    const qps = (d.search_perf && d.search_perf.queries_per_sec) ? d.search_perf.queries_per_sec : 0;
    if(qps > totalCores * 10) {
        qpsStatus.state = 'WARNING';
        qpsStatus.val = `Il cluster gestisce ${qps} QPS con soli ${totalCores} core allocati. Sei sopra il target (1 core per 10 QPS).`;
    } else {
        qpsStatus.val = `Allocati ${totalCores} Core per ${qps} QPS totali. Rapporto entro le linee guida.`;
    }

    // Builder riga HTML
    const stCls = { 'PASSED': 'st-pass', 'WARNING': 'st-warn', 'CRITICAL': 'st-crit' };
    const stIco = { 'PASSED': '🟢 PASSED', 'WARNING': '🟡 WARNING', 'CRITICAL': '🔴 CRIT' };

    h += `<div class="adv-card">
            <div class="adv-title"><span>Spazio Disco (Regola del 125%)</span><span class="${stCls[diskStatus.state]}">${stIco[diskStatus.state]}</span></div>
            <div class="adv-val"><b>Rilevato:</b> ${diskStatus.val}</div>
            <div class="adv-doc">📖 Doc: "Always ensure you have at least 125% of your current index size available as free disk space to accommodate rebuilds."</div>
          </div>`;
          
    h += `<div class="adv-card">
            <div class="adv-title"><span>Consolidamento Indici</span><span class="${stCls[idxStatus.state]}">${stIco[idxStatus.state]}</span></div>
            <div class="adv-val"><b>Rilevato:</b> ${idxStatus.val}</div>
            <div class="adv-doc">📖 Doc: "Avoid defining multiple, separate search indexes on a single collection. Each index adds overhead."</div>
          </div>`;

    h += `<div class="adv-card">
            <div class="adv-title"><span>Collo di Bottiglia I/O & Replica</span><span class="${stCls[ioStatus.state]}">${stIco[ioStatus.state]}</span></div>
            <div class="adv-val"><b>Rilevato:</b> ${ioStatus.val}</div>
            <div class="adv-doc">📖 Doc: "If disk I/O queue length is high and replication lag is growing, you need to scale up your hardware."</div>
          </div>`;

    h += `<div class="adv-card" style="border-bottom:none; margin-bottom:0; padding-bottom:0;">
            <div class="adv-title"><span>Rapporto CPU / QPS</span><span class="${stCls[qpsStatus.state]}">${stIco[qpsStatus.state]}</span></div>
            <div class="adv-val"><b>Rilevato:</b> ${qpsStatus.val}</div>
            <div class="adv-doc">📖 Doc: "A general starting point is 1 CPU core for every 10 QPS."</div>
          </div>`;

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

  // 1. OPLOG E DISCOVERY
  h+=`<div class="c s2"><div class="c-h"><span>🌍</span><span class="c-t">Global DB Status</span></div>`;
  if(d.oplog_info && d.oplog_info.latest_oplog_time) h+=row('Ultima scrittura DB (Oplog Head)', `<span style="color:#00e676">${new Date(d.oplog_info.latest_oplog_time).toLocaleTimeString()}</span>`);
  else h+=row('Oplog Head', '<span style="color:#ffab00">Non disponibile</span>');
  h+=row('MongoDB Conn.', d.mongo_connected?'<span class="grn">Connesso</span>':'<span class="red">N/A</span>');
  h+=row('K8s API Conn.', (pods.length||crds.length||op.name)?'<span class="grn">Connesso</span>':'<span class="red">N/A</span>');
  h+=row('Tempo raccolta',`${d._collect_ms||'?'} ms`);
  h+=`</div>`;

  h+=`<div class="c s2"><div class="c-h"><span>📋</span><span class="c-t">K8s Discovery</span></div>`;
  if(op.name) h+=row('Operator',`${op.name} (${op.replicas||0}/${op.desired||1})`);
  h+=row('CRDs Trovati',`<span class="pur">${crds.length}</span>`);
  h+=row('Pod mongot',`<span class="blu">${pods.length}</span>`);
  h+=row('Indici di Ricerca',`<span class="grn">${idxs.length}</span>`);
  h+=row('PVC',`${pvcs.length}`) + row('Services',`${svcs.length}`);
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
    if(lc.indexes_initialized)h+=mgItem(fN(lc.indexes_initialized),'Indici Init','#00e676');
    h+=`</div>`;

    if(p.warnings && p.warnings.length > 0) {
        h += `<div class="warn-box"><strong style="color:#ffab00">⚠️ Ultimi Eventi K8s:</strong><br>`;
        p.warnings.forEach(w => { h += `&bull; <b>${w.reason}</b>: ${w.message} <i style="color:#6b7394">(${w.count}x)</i><br>`; });
        h += `</div>`;
    }

    // Live Logs Persistenti
    const isLogOpen = openLogs.has(p.name);
    h += `<button id="btn-log-${p.name}" class="btn" onclick="toggleLogs('${p.namespace}', '${p.name}')">${isLogOpen ? '▼ Nascondi Logs' : '▶ Mostra Live Logs del Pod'}</button>`;
    h += `<pre id="log-${p.name}" class="term" style="display:${isLogOpen ? 'block' : 'none'}">${logCache[p.name] || 'Caricamento in corso...'}</pre>`;

    if(!prom.available){
      h+=`<div style="margin-top:14px;font-size:11px;color:#ff6b6b">Nessuna metrica Prometheus trovata. I fallback (Rete, Proxy, Exec) hanno fallito.</div></div>`;
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
    h+=row('getMore latency',`<span class="blu">${fMs(sc.getmore_latency_sec)}</span>`);
    h+=row('manageIndex lat.',`<span class="blu">${fMs(sc.manage_index_latency_sec)}</span>`);
    h+=`</div>`;

    // 2. JVM Heap
    const heapPct=jvm.heap_max_bytes>0?(jvm.heap_used_bytes/jvm.heap_max_bytes)*100:0;
    h+=`<div style="background:#0a0d14;border-radius:8px;padding:12px;border:1px solid #1a1f2e">`;
    h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#b388ff;margin-bottom:8px">☕ JVM Heap &amp; GC</div>`;
    h+=`<div style="display:flex;justify-content:center;margin-bottom:6px">${gaugeRing(heapPct,'Heap Used',heapPct>85?'#ff1744':heapPct>65?'#ffab00':'#b388ff',70)}</div>`;
    h+=row('Usata',`<span class="pur">${fB(jvm.heap_used_bytes)}</span>`);
    h+=row('Max',fB(jvm.heap_max_bytes));
    h+=row('GC pause max',`<span class="${jvm.gc_pause_seconds_max>0.5?'red':jvm.gc_pause_seconds_max>0.1?'ylw':'grn'}">${fMs(jvm.gc_pause_seconds_max)}</span>`);
    h+=row('Buffer used',fB(jvm.buffer_used_bytes));
    h+=`</div>`;

    // 3. Indexing Pipeline
    h+=`<div style="background:#0a0d14;border-radius:8px;padding:12px;border:1px solid #1a1f2e">`;
    h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#00e676;margin-bottom:8px">📥 Indexing Pipeline</div>`;
    h+=row('Indici in catalogo',`<span class="grn">${fN(idx.indexes_in_catalog)}</span>`);
    h+=row('CS updates applicati',`<span class="grn">${fN(idx.steady_applicable_updates)}</span>`);
    h+=row('Batch in corso',`<span class="cyn">${fN(idx.steady_batches_in_progress)}</span>`);
    h+=row('Oplog Lag',`<span class="${idx.change_stream_lag_sec>5?'red':'grn'}">${fN(idx.change_stream_lag_sec)} s</span>`);
    h+=row('Failures imprevisti',`<span class="${idx.steady_unexpected_failures>0?'red':'grn'}">${fN(idx.steady_unexpected_failures)}</span>`);
    h+=row('Initial sync attivi',`<span class="blu">${fN(idx.initial_sync_in_progress)}</span>`);
    h+=`</div>`;

    // 4. System Disk
    const diskPct=dsk.data_path_total_bytes>0?((dsk.data_path_total_bytes-dsk.data_path_free_bytes)/dsk.data_path_total_bytes)*100:0;
    h+=`<div style="background:#0a0d14;border-radius:8px;padding:12px;border:1px solid #1a1f2e">`;
    h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#ffab00;margin-bottom:8px">💾 Disco (data path)</div>`;
    h+=`<div style="display:flex;justify-content:center;margin-bottom:6px">${gaugeRing(diskPct,'Disk Used',diskPct>90?'#ff1744':diskPct>75?'#ffab00':'#00e676',70)}</div>`;
    h+=row('Usato',`<span class="ylw">${fB(dsk.data_path_total_bytes-dsk.data_path_free_bytes)}</span>`);
    h+=row('Totale',fB(dsk.data_path_total_bytes));
    h+=row('Read I/O',fB(dsk.read_bytes));
    h+=row('Write I/O',fB(dsk.write_bytes));
    h+=row('Queue len',`<span class="${dsk.queue_length>5?'red':dsk.queue_length>1?'ylw':'grn'}">${fN(dsk.queue_length)}</span>`);
    h+=`</div>`;

    // 5. Lucene Merge Scheduler
    h+=`<div style="background:#0a0d14;border-radius:8px;padding:12px;border:1px solid #1a1f2e">`;
    h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#00e5ff;margin-bottom:8px">🔀 Lucene Merges</div>`;
    h+=row('Merge attivi',`<span class="cyn">${fN(luc.running_merges)}</span>`);
    h+=row('Docs in merge',`<span class="blu">${fN(luc.merging_docs)}</span>`);
    h+=row('Merge totali',fN(luc.total_merges));
    h+=row('Merge time max',`<span class="ylw">${fMs(luc.merge_time_sec_max)}</span>`);
    h+=row('Merge scartati',`<span class="${luc.discarded_merges>0?'ylw':'grn'}">${fN(luc.discarded_merges)}</span>`);
    h+=`</div>`;

    // 6. System Memory + Network
    const memPct=mem.phys_total_bytes>0?(mem.phys_inuse_bytes/mem.phys_total_bytes)*100:0;
    h+=`<div style="background:#0a0d14;border-radius:8px;padding:12px;border:1px solid #1a1f2e">`;
    h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#ff6b6b;margin-bottom:8px">🖥 Sistema &amp; Rete</div>`;
    h+=row('RAM usata',`<span class="${memPct>90?'red':memPct>75?'ylw':'grn'}">${fB(mem.phys_inuse_bytes)} (${memPct.toFixed(0)}%)</span>`);
    h+=row('Swap usata',fB(mem.swap_inuse_bytes));
    h+=`<div style="border-top:1px solid #1a1f2e;margin:4px 0;padding-top:4px"></div>`;
    h+=row('Net recv',`<span class="blu">${fB(net.bytes_recv)}</span>`);
    h+=row('Net sent',`<span class="grn">${fB(net.bytes_sent)}</span>`);
    h+=row('Net errors',`<span class="${(net.in_errors+net.out_errors)>0?'red':'grn'}">${fN(net.in_errors+net.out_errors)}</span>`);
    h+=`</div>`;

    h+=`</div></div>`;
  });

  if(!pods.length) h+=`<div class="c s4"><div class="empty">Nessun pod mongot trovato</div></div>`;

  // 4. TABELLA INDICI
  h+=`<div class="c s4"><div class="c-h"><span>📑</span><span class="c-t">Search Indexes (${idxs.length})</span></div>`;
  if(idxs.length){h+=`<table><thead><tr><th>Nome</th><th>Collection</th><th>Tipo</th><th>Stato</th><th>Queryable</th><th>Documenti</th></tr></thead><tbody>`;
  idxs.forEach(i=>{const v=i.type==='vectorSearch';h+=`<tr><td style="font-weight:600;color:#e8ecf4">${i.name}</td><td style="font-size:11px">${i.ns}</td><td><span class="tag ${v?'tag-v':'tag-f'}">${v?'VECTOR':'FULL-TEXT'}</span></td><td>${pill(i.status)}</td><td>${i.queryable?'<span class="grn">✓</span>':'<span class="red">✗</span>'}</td><td>${i.num_docs!=null?fN(i.num_docs):'—'}</td></tr>`});
  h+=`</tbody></table>`}else{h+=`<div class="empty">Nessun indice search trovato nel database</div>`}
  h+=`</div>`;

  // 5. TABELLA PVCS E SERVICES
  if(pvcs.length||svcs.length){
    h+=`<div class="c s4"><div class="c-h"><span>💾</span><span class="c-t">Storage &amp; Services</span></div>`;
    pvcs.forEach(p=>{h+=`<div style="display:flex;justify-content:space-between;font-size:11px;padding:3px 0;border-bottom:1px solid #111827"><span style="color:#e8ecf4">📦 PVC: ${p.name}</span><span>${pill(p.status)} <span class="blu">${p.capacity}</span></span></div>`});
    svcs.forEach(s=>{const pts=(s.ports||[]).map(p=>`${p.port}`).join(',');h+=`<div style="display:flex;justify-content:space-between;font-size:11px;padding:3px 0;border-bottom:1px solid #111827"><span style="color:#e8ecf4">🔗 SVC: ${s.name}</span><span><span class="tag tag-v">${s.type}</span> Port(s) :${pts}</span></div>`});
    h+=`</div>`;
  }

  $('grid').innerHTML=h;

  // Richiama asincronamente i log aperti per farli aggiornare fluida mente
  openLogs.forEach(pod => {
      const p = pods.find(x => x.name === pod);
      if(p) fetchAndUpdateLog(p.namespace, p.name);
  });
}

let iv; function setR(){if(iv)clearInterval(iv);iv=setInterval(fetchM,+$('rr').value*1000)}
async function fetchM(){try{const r=await fetch('/metrics');$('err').style.display='none';render(await r.json())}catch(e){$('err').style.display='block';$('err').textContent='\u26A0 '+e.message}}
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