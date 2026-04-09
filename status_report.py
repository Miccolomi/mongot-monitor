"""
Report builder — three output formats:
  text     → proprietary ASCII (Slack, terminal, support tickets)
  markdown → GitHub issues, Confluence, Notion
  json     → integrations, CI pipelines, alerting tools
"""

from datetime import datetime, timezone


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _health(findings: list) -> tuple[str, int, int, int]:
    crits  = [f for f in findings if f.get("status") == "crit"]
    warns  = [f for f in findings if f.get("status") == "warn"]
    passes = [f for f in findings if f.get("status") == "pass"]
    h = "CRITICAL" if crits else "DEGRADED" if warns else "HEALTHY"
    return h, len(crits), len(warns), len(passes)


def _ms(sec) -> str:
    if not sec:
        return "—"
    return f"{round(float(sec) * 1000)}ms"


def _mb(b) -> str:
    if not b:
        return "—"
    return f"{round(float(b) / 1024 / 1024)}MB"


def _pct(used, total) -> str:
    if not total:
        return "—"
    return f"{round(float(used) / float(total) * 100)}%"


def _cats(prom: dict) -> dict:
    return prom.get("categories", {})


# ── Text (proprietary) ────────────────────────────────────────────────────────

def build_text(data: dict, findings: list) -> str:
    W   = 58
    bar = "━" * W

    def sec(title):
        return f"\n── {title} " + "─" * (W - len(title) - 4)

    health, nc, nw, np_ = _health(findings)
    icon = {"CRITICAL": "🔴", "DEGRADED": "🟡", "HEALTHY": "🟢"}[health]

    L = []
    L += [bar, "  MONGODB SEARCH — STATUS REPORT", f"  {_ts()}", bar]
    L += [f"\nCLUSTER HEALTH: {icon} {health}  ({nc} critical · {nw} warnings · {np_} passed)"]

    # Pods
    pods = (data or {}).get("mongot_pods") or []
    L.append(sec("PODS"))
    if pods:
        for p in pods:
            restarts = p.get("total_restarts", 0)
            ready    = "READY" if p.get("all_ready") else "NOT READY"
            flag     = " ⚠" if restarts > 0 else ""
            L.append(f"  {p['name']:<42} {p.get('phase','?'):<10} {ready}  Restarts: {restarts}{flag}")
            if p.get("node"):
                L.append(f"    Node: {p['node']}")
    else:
        L.append("  No pods found.")

    # Per-pod metrics sections
    prom_all = (data or {}).get("mongot_prometheus") or {}
    for pod_name, prom in prom_all.items():
        cats = _cats(prom)
        sc   = cats.get("search_commands", {})
        idx  = cats.get("indexing", {})
        jvm  = cats.get("jvm", {})
        luc  = cats.get("lucene_merge", {})
        proc = cats.get("process", {})
        sync = (idx.get("initial_sync_in_progress") or 0) > 0

        L.append(sec(f"SEARCH COMMANDS — {pod_name}"))
        L.append(f"  $search QPS:              {sc.get('search_qps', 0):.2f}/s")
        L.append(f"  $search avg latency:      {_ms(sc.get('search_avg_latency_sec'))}")
        L.append(f"  $search max latency:      {_ms(sc.get('search_latency_sec'))}")
        L.append(f"  $search failures:         {int(sc.get('search_failures', 0))}")
        L.append(f"  $vectorSearch QPS:        {sc.get('vectorsearch_qps', 0):.2f}/s")
        L.append(f"  $vectorSearch avg lat.:   {_ms(sc.get('vectorsearch_avg_latency_sec'))}")
        L.append(f"  $vectorSearch failures:   {int(sc.get('vectorsearch_failures', 0))}")
        L.append(f"  Scan ratio ($search):     {sc.get('scan_ratio', 0):.1f}:1")
        L.append(f"  Scan ratio ($vecSearch):  {sc.get('vector_scan_ratio', 0):.1f}:1")
        L.append(f"  HNSW visited nodes:       {int(sc.get('hnsw_visited_nodes', 0))}")
        L.append(f"  Indexing lag:             {idx.get('change_stream_lag_sec', 0):.1f}s")
        L.append(f"  Initial sync active:      {'Yes ⚠' if sync else 'No'}")

        L.append(sec(f"JVM HEAP — {pod_name}"))
        used  = jvm.get("heap_used_bytes", 0)
        total = jvm.get("heap_max_bytes", 0)
        comm  = jvm.get("heap_committed_bytes", 0)
        L.append(f"  Heap used:        {_mb(used)}  ({_pct(used, total)} of max)")
        L.append(f"  Heap committed:   {_mb(comm)}")
        L.append(f"  Heap max:         {_mb(total)}")
        L.append(f"  GC pause max:     {_ms(jvm.get('gc_pause_seconds_max'))}")
        L.append(f"  Buffer memory:    {_mb(jvm.get('buffer_used_bytes'))}")
        cpu = proc.get("cpu_usage", 0)
        L.append(f"  JVM CPU usage:    {round(float(cpu) * 100, 1)}%")

        L.append(sec(f"LUCENE MERGES — {pod_name}"))
        L.append(f"  Running merges:   {int(luc.get('running_merges', 0))}")
        L.append(f"  Merging docs:     {int(luc.get('merging_docs', 0)):,}")
        L.append(f"  Total merges:     {int(luc.get('total_merges', 0)):,}")
        L.append(f"  Merge time max:   {_ms(luc.get('merge_time_sec_max'))}")
        L.append(f"  Discarded merges: {int(luc.get('discarded_merges', 0))}")

    if not prom_all:
        L.append(sec("SEARCH METRICS"))
        L.append("  No Prometheus data available.")

    # Oplog
    oplog = (data or {}).get("oplog_info") or {}
    if oplog.get("window_hours"):
        L.append(sec("OPLOG"))
        L.append(f"  Window: {oplog['window_hours']}h  ·  Head: {oplog.get('head_time', '—')}")

    # SRE Advisor
    crits  = [f for f in findings if f.get("status") == "crit"]
    warns  = [f for f in findings if f.get("status") == "warn"]
    passes = [f for f in findings if f.get("status") == "pass"]
    L.append(sec("SRE ADVISOR"))
    for f in crits:
        L.append(f"  ✖ [CRIT] {f['title']} — {f['value']}")
    for f in warns:
        L.append(f"  ⚠ [WARN] {f['title']} — {f['value']}")
    for f in passes:
        L.append(f"  ✔ {f['title']}")
    if not findings:
        L.append("  No advisor data yet.")

    # Search indexes
    idxs = (data or {}).get("search_indexes") or []
    L.append(sec("SEARCH INDEXES"))
    if idxs:
        for idx in idxs:
            status = idx.get("status", "?")
            sflag  = " ⚠" if status not in ("READY", "ready") else ""
            qflag  = "  (not queryable ✖)" if not idx.get("queryable", True) else ""
            L.append(f"  {idx['ns']} / {idx['name']} [{idx.get('type','?')}] {status}{sflag}{qflag}")
    else:
        L.append("  No Search indexes found.")

    # Recommendations
    recs = [f["doc"] for f in crits + warns if f.get("doc")]
    if recs:
        L.append(sec("RECOMMENDATIONS"))
        for r in recs:
            L.append(f"  → {r}")

    # Errors
    errors = (data or {}).get("global_errors") or []
    if errors:
        L.append(sec("ERRORS DETECTED"))
        for e in errors:
            L.append(f"  ! {e}")

    L += [f"\n{bar}", "  mongot-doctor · github.com/Miccolomi/mongot-doctor", bar]
    return "\n".join(L)


# ── Markdown ──────────────────────────────────────────────────────────────────

def build_markdown(data: dict, findings: list) -> str:
    health, nc, nw, np_ = _health(findings)
    icon = {"CRITICAL": "🔴", "DEGRADED": "🟡", "HEALTHY": "🟢"}[health]

    crits  = [f for f in findings if f.get("status") == "crit"]
    warns  = [f for f in findings if f.get("status") == "warn"]
    passes = [f for f in findings if f.get("status") == "pass"]

    L = []
    L += [f"# MongoDB Search — Status Report", f"*Generated: {_ts()}*\n"]
    L += [f"## {icon} Cluster Health: {health}",
          f"> **{nc} critical** · **{nw} warnings** · **{np_} passed**\n"]

    # Pods
    pods = (data or {}).get("mongot_pods") or []
    L.append("## Pods\n")
    if pods:
        L += ["| Pod | Phase | Ready | Restarts | Node |",
              "|:----|:------|:------|:---------|:-----|"]
        for p in pods:
            restarts = p.get("total_restarts", 0)
            r_str    = f"{restarts} ⚠" if restarts > 0 else str(restarts)
            L.append(f"| `{p['name']}` | {p.get('phase','?')} | {'✔' if p.get('all_ready') else '✖'} | {r_str} | {p.get('node','—')} |")
    else:
        L.append("*No pods found.*")
    L.append("")

    # Per-pod metrics
    prom_all = (data or {}).get("mongot_prometheus") or {}
    for pod_name, prom in prom_all.items():
        cats = _cats(prom)
        sc   = cats.get("search_commands", {})
        idx  = cats.get("indexing", {})
        jvm  = cats.get("jvm", {})
        luc  = cats.get("lucene_merge", {})
        proc = cats.get("process", {})
        sync = (idx.get("initial_sync_in_progress") or 0) > 0
        used  = jvm.get("heap_used_bytes", 0)
        total = jvm.get("heap_max_bytes", 0)

        L += [f"### 🔎 Search Commands — `{pod_name}`\n",
              "| Metric | Value |", "|:-------|:------|",
              f"| `$search` QPS | `{sc.get('search_qps', 0):.2f}/s` |",
              f"| `$search` avg latency | `{_ms(sc.get('search_avg_latency_sec'))}` |",
              f"| `$search` max latency | `{_ms(sc.get('search_latency_sec'))}` |",
              f"| `$search` failures | `{int(sc.get('search_failures', 0))}` |",
              f"| `$vectorSearch` QPS | `{sc.get('vectorsearch_qps', 0):.2f}/s` |",
              f"| `$vectorSearch` avg latency | `{_ms(sc.get('vectorsearch_avg_latency_sec'))}` |",
              f"| `$vectorSearch` failures | `{int(sc.get('vectorsearch_failures', 0))}` |",
              f"| Scan ratio (`$search`) | `{sc.get('scan_ratio', 0):.1f}:1` |",
              f"| Scan ratio (`$vectorSearch`) | `{sc.get('vector_scan_ratio', 0):.1f}:1` |",
              f"| HNSW visited nodes | `{int(sc.get('hnsw_visited_nodes', 0))}` |",
              f"| Indexing lag | `{idx.get('change_stream_lag_sec', 0):.1f}s` |",
              f"| Initial sync active | `{'Yes ⚠' if sync else 'No'}` |", ""]

        L += [f"### 🧠 JVM Heap — `{pod_name}`\n",
              "| Metric | Value |", "|:-------|:------|",
              f"| Heap used | `{_mb(used)}` ({_pct(used, total)} of max) |",
              f"| Heap committed | `{_mb(jvm.get('heap_committed_bytes'))}` |",
              f"| Heap max | `{_mb(total)}` |",
              f"| GC pause max | `{_ms(jvm.get('gc_pause_seconds_max'))}` |",
              f"| Buffer memory | `{_mb(jvm.get('buffer_used_bytes'))}` |",
              f"| JVM CPU usage | `{round(float(proc.get('cpu_usage', 0)) * 100, 1)}%` |", ""]

        L += [f"### 🔀 Lucene Merges — `{pod_name}`\n",
              "| Metric | Value |", "|:-------|:------|",
              f"| Running merges | `{int(luc.get('running_merges', 0))}` |",
              f"| Merging docs | `{int(luc.get('merging_docs', 0)):,}` |",
              f"| Total merges | `{int(luc.get('total_merges', 0)):,}` |",
              f"| Merge time max | `{_ms(luc.get('merge_time_sec_max'))}` |",
              f"| Discarded merges | `{int(luc.get('discarded_merges', 0))}` |", ""]

    if not prom_all:
        L.append("*No Prometheus data available.*\n")

    # Oplog
    oplog = (data or {}).get("oplog_info") or {}
    if oplog.get("window_hours"):
        L += ["## Oplog\n",
              f"- Window: **{oplog['window_hours']}h**",
              f"- Head: `{oplog.get('head_time','—')}`\n"]

    # SRE Advisor
    L.append("## SRE Advisor\n")
    for f in crits:
        L.append(f"- 🔴 **[CRIT] {f['title']}** — {f['value']}")
    for f in warns:
        L.append(f"- 🟡 **[WARN] {f['title']}** — {f['value']}")
    for f in passes:
        L.append(f"- ✅ {f['title']}")
    if not findings:
        L.append("*No advisor data yet.*")
    L.append("")

    # Search indexes
    idxs = (data or {}).get("search_indexes") or []
    L.append("## Search Indexes\n")
    if idxs:
        L += ["| Collection | Index | Type | Status | Queryable |",
              "|:-----------|:------|:-----|:-------|:----------|"]
        for idx in idxs:
            status = idx.get("status", "?")
            s_str  = f"⚠ {status}" if status not in ("READY", "ready") else status
            q      = "✔" if idx.get("queryable", True) else "✖"
            L.append(f"| `{idx['ns']}` | `{idx['name']}` | {idx.get('type','?')} | {s_str} | {q} |")
    else:
        L.append("*No Search indexes found.*")
    L.append("")

    # Recommendations
    recs = [f["doc"] for f in crits + warns if f.get("doc")]
    if recs:
        L.append("## Recommendations\n")
        for r in recs:
            L.append(f"- {r}")
        L.append("")

    # Errors
    errors = (data or {}).get("global_errors") or []
    if errors:
        L += ["## ⚠ Errors Detected\n"] + [f"- `{e}`" for e in errors] + [""]

    L += ["---", "*Generated by [mongot-doctor](https://github.com/Miccolomi/mongot-doctor)*"]
    return "\n".join(L)


# ── JSON ──────────────────────────────────────────────────────────────────────

def build_json(data: dict, findings: list) -> dict:
    health, nc, nw, np_ = _health(findings)
    pods     = (data or {}).get("mongot_pods") or []
    prom_all = (data or {}).get("mongot_prometheus") or {}
    idxs     = (data or {}).get("search_indexes") or []
    oplog    = (data or {}).get("oplog_info") or {}

    metrics_out = {}
    for pod_name, prom in prom_all.items():
        cats = _cats(prom)
        sc   = cats.get("search_commands", {})
        idx  = cats.get("indexing", {})
        jvm  = cats.get("jvm", {})
        luc  = cats.get("lucene_merge", {})
        proc = cats.get("process", {})
        metrics_out[pod_name] = {
            "search_commands": {
                "search_qps":                  sc.get("search_qps", 0),
                "search_avg_latency_sec":      sc.get("search_avg_latency_sec", 0),
                "search_max_latency_sec":      sc.get("search_latency_sec", 0),
                "search_failures":             int(sc.get("search_failures", 0)),
                "vectorsearch_qps":            sc.get("vectorsearch_qps", 0),
                "vectorsearch_avg_latency_sec":sc.get("vectorsearch_avg_latency_sec", 0),
                "vectorsearch_failures":       int(sc.get("vectorsearch_failures", 0)),
                "scan_ratio":                  sc.get("scan_ratio", 0),
                "vector_scan_ratio":           sc.get("vector_scan_ratio", 0),
                "hnsw_visited_nodes":          int(sc.get("hnsw_visited_nodes", 0)),
            },
            "indexing": {
                "lag_sec":             idx.get("change_stream_lag_sec", 0),
                "initial_sync_active": (idx.get("initial_sync_in_progress") or 0) > 0,
            },
            "jvm": {
                "heap_used_bytes":      jvm.get("heap_used_bytes", 0),
                "heap_committed_bytes": jvm.get("heap_committed_bytes", 0),
                "heap_max_bytes":       jvm.get("heap_max_bytes", 0),
                "heap_used_pct":        round(float(jvm.get("heap_used_bytes", 0)) / float(jvm.get("heap_max_bytes", 1)) * 100, 1) if jvm.get("heap_max_bytes") else None,
                "gc_pause_max_sec":     jvm.get("gc_pause_seconds_max", 0),
                "buffer_used_bytes":    jvm.get("buffer_used_bytes", 0),
                "cpu_usage_pct":        round(float(proc.get("cpu_usage", 0)) * 100, 1),
            },
            "lucene_merges": {
                "running_merges":   int(luc.get("running_merges", 0)),
                "merging_docs":     int(luc.get("merging_docs", 0)),
                "total_merges":     int(luc.get("total_merges", 0)),
                "merge_time_max_sec": luc.get("merge_time_sec_max", 0),
                "discarded_merges": int(luc.get("discarded_merges", 0)),
            },
        }

    return {
        "generated_at":   _ts(),
        "schema_version": "1",
        "health":         health.lower(),
        "pods": [
            {
                "name":           p["name"],
                "phase":          p.get("phase"),
                "all_ready":      p.get("all_ready"),
                "total_restarts": p.get("total_restarts", 0),
                "node":           p.get("node"),
                "namespace":      p.get("namespace"),
            }
            for p in pods
        ],
        "per_pod_metrics": metrics_out,
        "oplog": {
            "window_hours": oplog.get("window_hours"),
            "head_time":    oplog.get("head_time"),
        },
        "advisor": {
            "health":   health.lower(),
            "summary":  {"crit": nc, "warn": nw, "pass": np_},
            "findings": [
                {
                    "status":         f.get("status"),
                    "title":          f.get("title"),
                    "detail":         f.get("value"),
                    "recommendation": f.get("doc"),
                }
                for f in findings
            ],
        },
        "search_indexes": [
            {
                "ns":        idx.get("ns"),
                "name":      idx.get("name"),
                "type":      idx.get("type"),
                "status":    idx.get("status"),
                "queryable": idx.get("queryable", True),
                "num_docs":  idx.get("num_docs"),
            }
            for idx in idxs
        ],
        "global_errors": (data or {}).get("global_errors") or [],
        "source":        "mongot-doctor",
    }
