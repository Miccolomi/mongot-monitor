"""
Prometheus scraper for mongot pods.
Implements triple-fallback: direct HTTP → K8s API Proxy → (future: kubectl exec wget).
"""

import math
import logging

import requests
import state

log = logging.getLogger("mongot-doctor.prometheus")


def scrape_mongot_prometheus(pod_name: str, namespace: str, pod_ip: str, port: int,
                              errors: list = None) -> dict:
    result = {"available": False, "raw_count": 0, "categories": {}}
    text = ""
    scrape_errs = []

    # 1. Direct network access
    try:
        resp = requests.get(f"http://{pod_ip}:{port}/metrics", timeout=2)
        if resp.status_code == 200:
            text = resp.text
        else:
            raise Exception(f"HTTP {resp.status_code}")
    except Exception as e:
        scrape_errs.append(f"Direct Net: {str(e)}")
        if state.k8s_v1:
            # 2. K8s API Server proxy
            try:
                text = state.k8s_v1.connect_get_namespaced_pod_proxy_with_path(
                    name=f"{pod_name}:{port}", namespace=namespace, path="metrics", _request_timeout=5
                )
            except Exception as e2:
                scrape_errs.append(f"API Proxy: {str(e2)}")
                log.debug(f"Proxy scrape failed for {pod_name}: {e2}")

    if not text:
        if errors is not None:
            errors.append(f"Network Error (Prometheus scrape failed for {pod_name}:{port}) -> " + " | ".join(scrape_errs))
        return result

    if hasattr(text, "data"): text = text.data
    if isinstance(text, bytes): text = text.decode("utf-8", errors="ignore")

    result["available"] = True
    raw = {}
    for line in text.split("\n"):
        if line.startswith("#") or not line.strip(): continue
        if "{" in line:
            key = line[:line.index("{")]
            val = line[line.rindex("}") + 1:].strip()
        else:
            parts = line.split()
            key, val = (parts[0], parts[1]) if len(parts) > 1 else ("", "")
        try:
            v = float(val)
            if math.isnan(v): v = 0.0
            raw[key] = raw.get(key, 0.0) + v
        except Exception:
            pass

    result["raw_count"] = len(raw)
    g = lambda k, d=0: raw.get(k, d)

    result["categories"] = {
        "search_commands": {
            # Latency (max) — existing
            "search_latency_sec":          g("mongot_command_searchCommandTotalLatency_seconds_max"),
            "vectorsearch_latency_sec":    g("mongot_command_vectorSearchCommandTotalLatency_seconds_max"),
            "getmores_latency_sec":        g("mongot_command_getMoresCommandTotalLatency_seconds_max"),
            "manage_index_latency_sec":    g("mongot_command_manageSearchIndexCommandTotalLatency_seconds_max"),
            # Counters — used to compute QPS and avg latency via delta
            "search_total":                g("mongot_command_searchCommandTotalLatency_seconds_count"),
            "search_latency_sum":          g("mongot_command_searchCommandTotalLatency_seconds_sum"),
            "vectorsearch_total":          g("mongot_command_vectorSearchCommandTotalLatency_seconds_count"),
            "vectorsearch_latency_sum":    g("mongot_command_vectorSearchCommandTotalLatency_seconds_sum"),
            "getmores_total":              g("mongot_command_getMoresCommandTotalLatency_seconds_count"),
            # Failures (cumulative)
            "search_failures":             g("mongot_command_searchCommandFailure_total"),
            "vectorsearch_failures":       g("mongot_command_vectorSearchCommandFailure_total"),
            # Candidates examined / results returned (text search scan ratio)
            # Metric name varies by mongot version — try both
            "candidates_examined": (
                g("mongot_query_candidates_examined_total") or
                g("mongot_query_documents_scanned")
            ),
            "results_returned":        g("mongot_query_results_returned_total"),
            # Vector search candidates / results (separate ratio)
            "vector_candidates_examined": (
                g("mongot_vector_query_candidates_examined_total") or 0
            ),
            "vector_results_returned": g("mongot_vector_query_results_returned_total"),
            # HNSW graph traversal — ANN efficiency proxy
            # High values → ANN degenerating toward brute-force
            "hnsw_visited_nodes": (
                g("mongot_vector_search_hnsw_visited_nodes") or
                g("mongot_vector_search_graph_nodes_visited")
            ),
            # Rates — computed by background collector, seeded to 0 here
            "search_qps":           0.0,
            "search_avg_latency_sec": 0.0,
            "vectorsearch_qps":     0.0,
            "vectorsearch_avg_latency_sec": 0.0,
            "scan_ratio":           0.0,
            "vector_scan_ratio":    0.0,
            "zero_results_with_candidates": False,
        },
        "jvm": {
            "heap_used_bytes":      g("mongot_jvm_memory_used_bytes"),
            "heap_committed_bytes": g("mongot_jvm_memory_committed_bytes"),
            "heap_max_bytes":       g("mongot_jvm_memory_max_bytes"),
            "gc_pause_seconds_max": g("mongot_jvm_gc_pause_seconds_max"),
            "buffer_used_bytes":    g("mongot_jvm_buffer_memory_used_bytes"),
        },
        "process": {
            "cpu_usage":   g("mongot_process_cpu_usage"),
            "load_avg_1m": g("mongot_system_load_average_1m"),
            "cpu_count":   g("mongot_system_cpu_count", 0),
        },
        "memory": {
            "phys_total_bytes": g("mongot_system_memory_phys_total_bytes"),
            "phys_inuse_bytes": g("mongot_system_memory_phys_inUse_bytes"),
            "swap_inuse_bytes": g("mongot_system_memory_virt_swap_inUse_bytes"),
            "major_page_faults_sec": (
                g("mongot_system_memory_pageFaults_pageFaultsPS") or
                g("mongot_process_major_faults_total") or 0
            ),
        },
        "disk": {
            "data_path_free_bytes":  g("mongot_system_disk_space_data_path_free_bytes"),
            "data_path_total_bytes": g("mongot_system_disk_space_data_path_total_bytes"),
            "read_bytes":            g("mongot_system_disk_readBytes_bytes"),
            "write_bytes":           g("mongot_system_disk_writeBytes_bytes"),
            "queue_length":          g("mongot_system_disk_currentQueueLength_tasks"),
        },
        "network": {
            "bytes_recv": g("mongot_system_netstat_bytesRecv_bytes"),
            "bytes_sent": g("mongot_system_netstat_bytesSent_bytes"),
            "in_errors":  g("mongot_system_netstat_inErrors_events"),
            "out_errors": g("mongot_system_netstat_outErrors_events"),
        },
        "indexing": {
            "indexes_in_catalog":          g("mongot_configState_indexesInCatalog"),
            "staged_indexes":              g("mongot_configState_stagedIndexes"),
            "indexes_phasing_out":         g("mongot_configState_indexesPhasingOut"),
            "steady_witnessed_updates":    g("mongot_indexing_steadyStateChangeStream_witnessedChangeStreamUpdates_total"),
            "steady_applicable_updates":   g("mongot_index_stats_replication_steadyState_batchTotalApplicableDocuments_sum"),
            "steady_batches_in_progress":  g("mongot_indexing_steadyStateChangeStream_batchesInProgressTotal"),
            "steady_batch_sec_max":        g("mongot_indexing_steadyStateChangeStream_batchesInProgressTotalDurations_seconds_max"),
            "steady_unexpected_failures":  g("mongot_indexing_steadyStateChangeStream_unexpectedBatchFailures_total"),
            "initial_sync_in_progress":    g("mongot_initialsync_dispatcher_inProgressSyncs"),
            "initial_sync_queued":         g("mongot_initialsync_dispatcher_queuedSyncs"),
            "change_stream_lag_sec":       g("mongot_index_stats_indexing_replicationLagMs", 0) / 1000.0,
            # Index build progress — used for ETA calculation
            # mongot exposes these during initial sync / bulk index build
            "build_docs_processed": (
                g("mongot_index_documents_processed") or
                g("mongot_initialsync_reporter_progress_current") or
                g("mongot_index_stats_initialSync_totalProcessedDocuments_sum")
            ),
            "build_docs_total": (
                g("mongot_index_documents_total") or
                g("mongot_initialsync_reporter_progress_total") or
                g("mongot_index_stats_initialSync_totalDocuments_sum")
            ),
        },
        "lucene_merge": {
            "running_merges":   g("mongot_mergeScheduler_currentlyRunningMerges"),
            "merging_docs":     g("mongot_mergeScheduler_currentlyMergingDocs"),
            "total_merges":     g("mongot_mergeScheduler_numMerges_total"),
            "merge_time_sec_max": g("mongot_mergeScheduler_mergeTime_seconds_max"),
            "discarded_merges": g("mongot_diskUtilizationAwarenessMergePolicy_discardedMerge_total"),
        },
        "lifecycle": {
            "indexes_initialized":    g("mongot_lifecycle_indexesInInitializedState"),
            "failed_downloads":       g("mongot_lifecycle_failedDownloadIndexes_total"),
            "failed_drops":           g("mongot_lifecycle_failedDropIndexes_total"),
            "failed_initializations": g("mongot_lifecycle_failedInitializationIndexes_total"),
        }
    }
    return result
