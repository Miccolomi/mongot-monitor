"""
SRE Advisor engine.
run_advisor(snapshot) takes the full metrics snapshot and returns a list of
structured findings, each with: id, title, status, value, doc.

Status values: "pass" | "warn" | "crit"
"""

from __future__ import annotations

from typing import Any

Snapshot = dict[str, Any]
Finding = dict[str, str]


def _fmt_bytes(b: float) -> str:
    if not b:
        return "—"
    if b > 1e9:
        return f"{b / 1e9:.2f} GB"
    if b > 1e6:
        return f"{b / 1e6:.1f} MB"
    if b > 1e3:
        return f"{b / 1e3:.1f} KB"
    return f"{int(b)} B"


def _finding(id_: str, title: str, status: str, value: str, doc: str) -> Finding:
    return {"id": id_, "title": title, "status": status, "value": value, "doc": doc}


# ── Individual checks ─────────────────────────────────────────────────────────

def _check_disk(pods: list, prom_all: dict) -> Finding:
    status = "pass"
    min_ratio = float("inf")
    worst_pod = ""
    messages = []

    for p in pods:
        dsk = (prom_all.get(p["name"]) or {}).get("categories", {}).get("disk", {})
        total = dsk.get("data_path_total_bytes", 0)
        if not total:
            continue
        free = dsk.get("data_path_free_bytes", 0)
        used = total - free
        pct_used = (used / total) * 100

        if pct_used >= 90:
            status = "crit"
            messages.append(f"Pod {p['name']} disk at {pct_used:.1f}% — mongot IS IN READ-ONLY MODE.")
        elif free < used * 2.0 and status != "crit":
            status = "warn"
            messages.append(
                f"Pod {p['name']}: free ({_fmt_bytes(free)}) < 200% of index size "
                f"({_fmt_bytes(used * 2.0)} required)."
            )

        ratio = free / (used or 1)
        if ratio < min_ratio:
            min_ratio = ratio
            worst_pod = p["name"]

    if status == "pass":
        ratio_pct = f"{min_ratio * 100:.0f}%" if min_ratio != float("inf") else "N/A"
        value = f"All pods have free space > 200% of used size (worst safety ratio: {ratio_pct} on {worst_pod or 'N/A'})."
    else:
        value = " ".join(messages)

    return _finding(
        "disk_200_rule", "Disk Space (200% Rule)", status, value,
        "Allocate double the disk space your index requires. "
        "mongot becomes read-only when disk utilization reaches 90%.",
    )


def _check_index_consolidation(indexes: list) -> Finding:
    # Group by (ns, type): having 1 vectorSearch + 1 fullText on the same
    # collection is the official MongoDB Hybrid Search pattern — not a problem.
    # Only warn when multiple indexes of the SAME type exist on the same collection.
    from collections import defaultdict
    ns_type_counts: dict = defaultdict(int)
    for idx in indexes:
        ns = idx.get("ns", "?")
        idx_type = idx.get("type", "fullText")
        ns_type_counts[(ns, idx_type)] += 1

    bad = [(ns, t, c) for (ns, t), c in ns_type_counts.items() if c > 1]
    if bad:
        summary = ", ".join(f"{ns} ({c}× {t})" for ns, t, c in bad)
        return _finding(
            "index_consolidation", "Index Consolidation", "warn",
            f"Multiple indexes of the same type on: {summary}. "
            "Consolidate same-type indexes into a single dynamic index.",
            "Avoid multiple separate search indexes of the same type on a single collection. "
            "Having one fullText + one vectorSearch index on the same collection is valid (Hybrid Search).",
        )
    return _finding(
        "index_consolidation", "Index Consolidation", "pass",
        "No collection has more than one index of the same type. Optimal.",
        "Avoid multiple separate search indexes of the same type on a single collection. "
        "Having one fullText + one vectorSearch index on the same collection is valid (Hybrid Search).",
    )


def _check_io_bottleneck(pods: list, prom_all: dict) -> Finding:
    for p in pods:
        cats = (prom_all.get(p["name"]) or {}).get("categories", {})
        q_len = cats.get("disk", {}).get("queue_length", 0)
        lag = cats.get("indexing", {}).get("change_stream_lag_sec", 0)
        if q_len > 10 and lag > 5:
            return _finding(
                "io_bottleneck", "I/O Bottleneck & Replica Lag", "crit",
                f"Pod {p['name']}: HIGH disk queue ({q_len:.0f}) and oplog lag ({lag:.0f}s). "
                "Scale storage class / increase PVC IOPS.",
                "If disk I/O queue length is high and replication lag is growing, scale up hardware.",
            )
    return _finding(
        "io_bottleneck", "I/O Bottleneck & Replica Lag", "pass",
        "No I/O bottleneck or replication lag detected.",
        "If disk I/O queue length is high and replication lag is growing, scale up hardware.",
    )


def _check_cpu_qps(pods: list, prom_all: dict, search_perf: dict) -> Finding:
    total_cores = sum(p.get("cpu_limit_cores", 0) for p in pods)
    max_cpu = 0.0
    for p in pods:
        cpu = (prom_all.get(p["name"]) or {}).get("categories", {}).get("process", {}).get("cpu_usage", 0)
        max_cpu = max(max_cpu, cpu)

    if total_cores == 0 and pods:
        cpu_cnt = (prom_all.get(pods[0]["name"]) or {}).get("categories", {}).get("process", {}).get("cpu_count", 1)
        total_cores = (cpu_cnt or 1) * len(pods)
    total_cores = total_cores or 1

    cpu_pct = max_cpu * 100
    qps = search_perf.get("queries_per_sec", 0)

    if cpu_pct > 80:
        return _finding(
            "cpu_qps", "CPU Usage & QPS (80% Rule)", "crit",
            f"CPU at {cpu_pct:.1f}% (above 80% threshold). Node is overloaded — scale up immediately.",
            "If CPU usage is consistently above 80%, scale up. Target: 1 core per 10 QPS.",
        )
    if qps > total_cores * 10:
        return _finding(
            "cpu_qps", "CPU Usage & QPS (80% Rule)", "warn",
            f"{qps} QPS with {total_cores} cores exceeds 1 core/10 QPS guideline. "
            f"CPU is currently {cpu_pct:.1f}% — monitor closely.",
            "If CPU usage is consistently above 80%, scale up. Target: 1 core per 10 QPS.",
        )
    return _finding(
        "cpu_qps", "CPU Usage & QPS (80% Rule)", "pass",
        f"Highest CPU: {cpu_pct:.1f}%. {total_cores} core(s) serving {qps} QPS — within guidelines.",
        "If CPU usage is consistently above 80%, scale up. Target: 1 core per 10 QPS.",
    )


def _check_page_faults(pods: list, prom_all: dict) -> Finding:
    for p in pods:
        pf = (prom_all.get(p["name"]) or {}).get("categories", {}).get("memory", {}).get("major_page_faults_sec", 0)
        if pf > 1000:
            return _finding(
                "page_faults", "Memory Starvation (Page Faults)", "crit",
                f"Pod {p['name']}: {pf:.0f} major page faults/sec — memory starvation. "
                "Increase pod RAM limits immediately.",
                "If Search page faults are consistently over 1000/s, the system needs more memory.",
            )
        if pf > 500:
            return _finding(
                "page_faults", "Memory Starvation (Page Faults)", "warn",
                f"Pod {p['name']}: {pf:.0f} major page faults/sec — monitor memory closely.",
                "If Search page faults are consistently over 1000/s, the system needs more memory.",
            )
    return _finding(
        "page_faults", "Memory Starvation (Page Faults)", "pass",
        "Major page faults per second are well within safe thresholds.",
        "If Search page faults are consistently over 1000/s, the system needs more memory.",
    )


def _check_oom(pods: list, prom_all: dict) -> Finding:
    status = "pass"
    messages: list[str] = []
    has_oomkilled = any(
        c.get("last_reason") == "OOMKilled"
        for p in pods
        for c in p.get("containers", [])
    )

    for p in pods:
        cats = (prom_all.get(p["name"]) or {}).get("categories", {})
        jvm = cats.get("jvm", {})
        sys_mem = cats.get("memory", {})

        limit = p.get("memory_limit_bytes") or sys_mem.get("phys_total_bytes", 0)
        heap_max = jvm.get("heap_max_bytes", 0)

        if heap_max and limit:
            ratio = heap_max / limit
            if ratio >= 0.9:
                status = "crit"
                messages.append(
                    f"Pod {p['name']}: JVM heap max ({_fmt_bytes(heap_max)}) is "
                    f"{ratio * 100:.0f}% of pod limit ({_fmt_bytes(limit)}). "
                    "Set --maxCapacityMB or JVM -Xmx."
                )
            elif ratio > 0.6 and status != "crit":
                status = "warn"
                messages.append(
                    f"Pod {p['name']}: JVM heap is {ratio * 100:.0f}% of pod limit "
                    f"({_fmt_bytes(limit)}). Lucene needs off-heap RAM — limit heap to ≤50%."
                )

    if has_oomkilled:
        status = "crit"
        prefix = "OOMKilled events detected! "
        value = prefix + (" ".join(messages) if messages else
                          "Increase memory limits and ensure JVM heap < 50% of pod limit.")
    elif status == "pass":
        value = "No OOMKilled events. Heap limits within safe parameters for Lucene Mmap."
    else:
        value = " ".join(messages)

    return _finding(
        "oom_risk", "OOMKilled & MMap Risk", status, value,
        "mongot uses memory-mapped files. Pod memory limit MUST be substantially higher "
        "than internal maxCapacityMB. Keep JVM heap ≤ 50% of pod limit.",
    )


def _check_crd_status(crds: list) -> Finding:
    for c in crds:
        if c.get("phase") != "Running":
            return _finding(
                "crd_status", "MongoDB Search CRD Status", "crit",
                f"CRD {c['name']} in ns {c['namespace']} is in state: {c.get('phase', '?')}. "
                "Operator reconciliation failed — check operator logs.",
                "A ReconcileFailed state means the K8s Operator cannot apply the desired spec "
                "(network issue, resource quota, etc.).",
            )
    return _finding(
        "crd_status", "MongoDB Search CRD Status", "pass",
        "All MongoDBSearch CRDs are in Running state.",
        "A ReconcileFailed state means the K8s Operator cannot apply the desired spec.",
    )


def _check_storage_class(pvcs: list) -> Finding:
    slow_keywords = ("hostpath", "standard", "slow")
    if not pvcs:
        return _finding(
            "storage_class", "Storage Class Performance (PVC)", "pass",
            "No PVCs found for search nodes.",
            "MongoDB Search requires high-performance NVMe/SSD disks (e.g. gp3, io2).",
        )
    slow = [p for p in pvcs if any(kw in (p.get("storage_class") or "").lower() for kw in slow_keywords)]
    if slow:
        classes = ", ".join(p["storage_class"] for p in slow)
        return _finding(
            "storage_class", "Storage Class Performance (PVC)", "warn",
            f"Slow/default StorageClasses detected: {classes}. "
            "MongoDB Search requires high-performance disks.",
            "Using standard or hostPath provisioners may cause MMap flushing issues and severe IO wait.",
        )
    classes = ", ".join({p.get("storage_class", "?") for p in pvcs})
    return _finding(
        "storage_class", "Storage Class Performance (PVC)", "pass",
        f"StorageClasses in use: {classes}. Verify they are high-throughput SSD/NVMe.",
        "MongoDB Search requires high-performance NVMe/SSD disks (e.g. gp3, io2).",
    )


def _check_versioning(operator: dict, k8s_version: str) -> Finding:
    image = (operator.get("image") or "") if operator else ""
    if image.endswith(":latest"):
        return _finding(
            "versioning", "K8s Operator Versioning (MCK)", "warn",
            f"Operator image ({image}) uses ':latest'. In production use exact immutable tags.",
            "Using ':latest' on the K8s Operator implies unexpected breaking changes on pod restarts.",
        )
    tag = image.split(":")[-1] if ":" in image else "N/A"
    return _finding(
        "versioning", "K8s Operator Versioning (MCK)", "pass",
        f"Operator uses immutable tag: {tag}. K8s cluster: {k8s_version or 'N/A'}.",
        "Using ':latest' on the K8s Operator implies unexpected breaking changes on pod restarts.",
    )


def _check_skip_auth(server_params: dict) -> Finding | None:
    """Check skipAuthenticationToSearchIndexManagementServer — should be False."""
    value = server_params.get("skipAuthenticationToSearchIndexManagementServer")
    if value is None:
        return None  # Parameter not available (MongoDB < 8.0 or no connection)
    if value is True:
        return _finding(
            "skip_auth_search",
            "Search Auth (skipAuthenticationToSearchIndexManagementServer)",
            "crit",
            "Parameter is TRUE — mongod communicates with mongot WITHOUT authentication. "
            "Any process on the same host can impersonate the search index manager.",
            "Set skipAuthenticationToSearchIndexManagementServer=false (default since MongoDB 8.2). "
            "Use setParameter or add it to mongod.conf under setParameter.",
        )
    return _finding(
        "skip_auth_search",
        "Search Auth (skipAuthenticationToSearchIndexManagementServer)",
        "pass",
        "Authentication between mongod and mongot is enabled (value: false). Secure.",
        "skipAuthenticationToSearchIndexManagementServer should be false (default since MongoDB 8.2).",
    )


def _check_search_tls(server_params: dict) -> Finding | None:
    """Check searchTLSMode — should be requireTLS."""
    value = server_params.get("searchTLSMode")
    if value is None:
        return None  # Parameter not available or MongoDB without external Search
    if value == "disabled":
        return _finding(
            "search_tls_mode",
            "Search TLS Mode (searchTLSMode)",
            "crit",
            "searchTLSMode is 'disabled' — the mongod↔mongot channel is unencrypted. "
            "All data exchanged between mongod and mongot is transmitted in plaintext.",
            "Set searchTLSMode=requireTLS on every mongod in the replica set. "
            "Configure .spec.source.external.tls in the MongoDBSearch CRD with the CA certificate.",
        )
    if value in ("allowTLS", "preferTLS"):
        return _finding(
            "search_tls_mode",
            "Search TLS Mode (searchTLSMode)",
            "warn",
            f"searchTLSMode is '{value}' — TLS is optional, not enforced. "
            "Unencrypted connections are still accepted.",
            "Set searchTLSMode=requireTLS to enforce encrypted communication between mongod and mongot.",
        )
    if value == "requireTLS":
        return _finding(
            "search_tls_mode",
            "Search TLS Mode (searchTLSMode)",
            "pass",
            "searchTLSMode is 'requireTLS' — mongod↔mongot channel is fully encrypted.",
            "requireTLS enforces TLS on every connection between mongod and the mongot search process.",
        )
    return None


def _check_oplog_window(oplog_info: dict, pods: list, prom_all: dict) -> Finding | None:
    if not oplog_info or not oplog_info.get("head_timestamp"):
        return None

    window_h = oplog_info.get("window_hours", 0)
    worst_lag = max(
        (
            (prom_all.get(p["name"]) or {}).get("categories", {}).get("indexing", {}).get("change_stream_lag_sec", 0)
            for p in pods
        ),
        default=0,
    )

    lag_h = worst_lag / 3600
    doc_base = f"Estimated oplog window: {window_h}h. Max current lag: {round(worst_lag)}s."

    if window_h > 0 and lag_h > window_h * 0.7:
        return _finding(
            "oplog_window", "Predictive SRE: Oplog Window", "crit",
            "Mongot lag has consumed >70% of the oplog window! "
            "Risk of losing resume token and forced initial sync.",
            f"{doc_base} Increase MongoDB oplog size or investigate mongot lag immediately.",
        )
    if window_h > 0 and lag_h > window_h * 0.4:
        return _finding(
            "oplog_window", "Predictive SRE: Oplog Window", "warn",
            f"Mongot is heavily lagging in replication ({round(worst_lag)}s lag, {window_h}h window).",
            f"{doc_base} Monitor oplog window closely.",
        )
    return _finding(
        "oplog_window", "Predictive SRE: Oplog Window", "pass",
        f"Ample oplog window ({window_h}h). Max lag: {round(worst_lag)}s.",
        doc_base,
    )


# ── Public entry point ────────────────────────────────────────────────────────

def run_advisor(snapshot: Snapshot) -> list[Finding]:
    """
    Run all SRE checks against a metrics snapshot.
    Returns a list of Finding dicts ordered by severity (crit first).
    """
    pods: list = snapshot.get("mongot_pods") or []
    prom_all: dict = snapshot.get("mongot_prometheus") or {}
    indexes: list = snapshot.get("search_indexes") or []
    pvcs: list = snapshot.get("mongot_pvcs") or []
    crds: list = snapshot.get("mongodbsearch_crds") or []
    operator: dict = snapshot.get("operator") or {}
    k8s_version: str = snapshot.get("k8s_version") or "N/A"
    search_perf: dict = snapshot.get("search_perf") or {}
    oplog_info: dict = snapshot.get("oplog_info") or {}
    server_params: dict = snapshot.get("search_server_params") or {}

    findings: list[Finding] = [
        _check_disk(pods, prom_all),
        _check_index_consolidation(indexes),
        _check_io_bottleneck(pods, prom_all),
        _check_cpu_qps(pods, prom_all, search_perf),
        _check_page_faults(pods, prom_all),
        _check_oom(pods, prom_all),
        _check_crd_status(crds),
        _check_storage_class(pvcs),
        _check_versioning(operator, k8s_version),
    ]

    for optional in (
        _check_skip_auth(server_params),
        _check_search_tls(server_params),
        _check_oplog_window(oplog_info, pods, prom_all),
    ):
        if optional:
            findings.append(optional)

    # Sort: crit → warn → pass
    order = {"crit": 0, "warn": 1, "pass": 2}
    findings.sort(key=lambda f: order.get(f["status"], 3))
    return findings
