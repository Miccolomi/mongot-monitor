"""
MongoDB collectors.
All functions query MongoDB for operational metrics (vitals, oplog, indexes, profiler).
The MongoClient instance lives in state.mongo_client, initialized by init_mongo().
"""

import logging
from datetime import datetime, timezone, timedelta

import state

try:
    from pymongo import MongoClient
except ImportError:
    MongoClient = None

log = logging.getLogger("mongot-monitor.mongodb")


# ── Init ──────────────────────────────────────────────────────────────────────

def init_mongo(uri: str) -> bool:
    """Create a MongoClient and store it in state. Returns True on success."""
    if MongoClient is None:
        log.warning("pymongo not installed — MongoDB features disabled.")
        return False
    try:
        state.mongo_client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        log.info("✓ MongoDB configurato.")
        return True
    except Exception as e:
        log.warning(f"✗ MongoDB error: {e}")
        return False


# ── Collectors ────────────────────────────────────────────────────────────────

def get_mongo_vitals(errors: list = None) -> dict:
    vitals = {
        "connections_active": 0, "connections_available": 0, "active_writers": 0,
        "ops_insert": 0, "ops_update": 0, "ops_delete": 0
    }
    if not state.mongo_client: return vitals
    try:
        status = state.mongo_client.admin.command("serverStatus")
        vitals["connections_active"]   = status.get("connections", {}).get("current", 0)
        vitals["connections_available"] = status.get("connections", {}).get("available", 0)
        vitals["active_writers"]        = status.get("globalLock", {}).get("activeClients", {}).get("writers", 0)
        opc = status.get("opcounters", {})
        vitals["ops_insert"] = opc.get("insert", 0)
        vitals["ops_update"] = opc.get("update", 0)
        vitals["ops_delete"] = opc.get("delete", 0)
    except Exception as e:
        if errors is not None: errors.append(f"MongoDB Error (Reading serverStatus): {str(e)}")
    return vitals


def get_oplog_info(errors: list = None) -> dict:
    info = {"head_time": None, "tail_time": None, "window_hours": 0, "head_timestamp": 0}
    if not state.mongo_client: return info
    try:
        oplog = state.mongo_client["local"]["oplog.rs"]
        head_doc = next(iter(oplog.find().sort("$natural", -1).limit(1)), None)
        tail_doc = next(iter(oplog.find().sort("$natural",  1).limit(1)), None)

        if head_doc and "ts" in head_doc:
            info["head_timestamp"] = head_doc["ts"].time
            info["head_time"] = head_doc["ts"].as_datetime().strftime("%H:%M:%S")
        if tail_doc and "ts" in tail_doc:
            info["tail_time"] = tail_doc["ts"].as_datetime().strftime("%H:%M:%S")
        if head_doc and tail_doc and "ts" in head_doc and "ts" in tail_doc:
            info["window_hours"] = round((head_doc["ts"].time - tail_doc["ts"].time) / 3600, 2)
    except Exception as e:
        log.error(f"Oplog Error: {e}")
        if errors is not None: errors.append(f"MongoDB Error (Reading Oplog for lag): {str(e)}")
    return info


def get_search_indexes(errors: list = None) -> list:
    indexes = []
    if not state.mongo_client: return indexes
    try:
        db_names = [d for d in state.mongo_client.list_database_names()
                    if d not in ("admin", "local", "config")]
        for db_name in db_names:
            db = state.mongo_client[db_name]
            for coll_name in db.list_collection_names():
                try:
                    search_indexes = []
                    try:
                        search_indexes = list(db[coll_name].aggregate([{"$listSearchIndexes": {}}]))
                    except Exception:
                        try:
                            search_indexes = list(db[coll_name].list_search_indexes())
                        except Exception:
                            pass

                    for idx in search_indexes:
                        idx_info = {
                            "name": idx.get("name", "unknown"),
                            "type": "vectorSearch" if idx.get("type") == "vectorSearch" else "fullText",
                            "status": idx.get("status", "READY"),
                            "ns": f"{db_name}.{coll_name}",
                            "queryable": idx.get("queryable", True),
                            "num_docs": None
                        }
                        try:
                            stats = db.command({"aggregate": coll_name, "pipeline": [
                                {"$searchMeta": {"index": idx["name"], "exists": {"path": {"wildcard": "*"}}}}
                            ]})
                            first = (stats.get("cursor", {}).get("firstBatch") or [None])[0]
                            if first and "count" in first:
                                idx_info["num_docs"] = first["count"].get("lowerBound", 0)
                        except Exception:
                            try:
                                idx_info["num_docs"] = db[coll_name].estimated_document_count()
                            except Exception:
                                pass
                        indexes.append(idx_info)
                except Exception as e:
                    if errors is not None:
                        errors.append(f"MongoDB Error (List search indexes in {db_name}.{coll_name}): {str(e)}")
    except Exception as e:
        if errors is not None: errors.append(f"MongoDB Error (Reading database/collections): {str(e)}")
    return indexes


def get_search_server_params(errors: list = None) -> dict:
    """Read mongot-related server parameters from mongod via getParameter."""
    params = {
        "skipAuthenticationToSearchIndexManagementServer": None,
        "searchTLSMode": None,
    }
    if not state.mongo_client:
        return params
    try:
        result = state.mongo_client.admin.command({
            "getParameter": 1,
            "skipAuthenticationToSearchIndexManagementServer": 1,
            "searchTLSMode": 1,
        })
        params["skipAuthenticationToSearchIndexManagementServer"] = result.get(
            "skipAuthenticationToSearchIndexManagementServer"
        )
        params["searchTLSMode"] = result.get("searchTLSMode")
    except Exception as e:
        if errors is not None:
            errors.append(f"MongoDB Error (Reading search server params): {str(e)}")
    return params


def get_search_perf_from_profiler(errors: list = None) -> dict:
    """Extract QPS from system.profile for the SRE Advisor."""
    perf = {"queries_per_sec": 0, "total_queries_5m": 0}
    if not state.mongo_client: return perf
    try:
        db_names = [d for d in state.mongo_client.list_database_names()
                    if d not in ("admin", "local", "config")]
        all_durations = []
        window_sec = 300
        for db_name in db_names:
            db = state.mongo_client[db_name]
            try:
                query = {
                    "ts": {"$gte": datetime.now(timezone.utc) - timedelta(seconds=window_sec)},
                    "$or": [
                        {"command.pipeline": {"$elemMatch": {"$search": {"$exists": True}}}},
                        {"command.pipeline": {"$elemMatch": {"$vectorSearch": {"$exists": True}}}}
                    ]
                }
                for doc in db["system.profile"].find(query).sort("ts", -1).limit(500):
                    all_durations.append(doc.get("millis", 0))
            except Exception:
                pass  # Profiling may be disabled on this db — silently skip
        if all_durations:
            perf["total_queries_5m"] = len(all_durations)
            perf["queries_per_sec"] = round(len(all_durations) / window_sec, 2)
    except Exception as e:
        if errors is not None: errors.append(f"MongoDB Error (Reading system.profile for profiler): {str(e)}")
    return perf
