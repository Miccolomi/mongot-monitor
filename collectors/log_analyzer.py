"""
Mongot Log Intelligence — pattern detection on structured JSON logs.

Parses mongot's JSON log format:
  {"t": "...", "s": "INFO|WARN|ERROR|FATAL", "n": "com.xgen...", "msg": "...", "attr": {...}}

Detection is done by matching against a library of known patterns.
Analysis is on-demand (not in the background loop) — triggered via /api/logs/analyze.
"""

import json
import logging

log = logging.getLogger("mongot-monitor.log_analyzer")

# Hard cap: never analyze more than this many lines regardless of window
_MAX_LINES = 2000

# ── Time windows ──────────────────────────────────────────────────────────────

WINDOW_SECONDS = {
    "1h":  3600,
    "24h": 86400,
    "7d":  604800,
    "30d": 2592000,
}

# ── Pattern matchers ──────────────────────────────────────────────────────────

def _match_oom(e: dict) -> bool:
    msg  = e.get("msg", "")
    attr = str(e.get("attr", ""))
    return "OutOfMemory" in msg or "OutOfMemoryError" in attr

def _match_error(e: dict) -> bool:
    return e.get("s") in ("ERROR", "FATAL") and not _match_oom(e)

def _match_tls_auth(e: dict) -> bool:
    msg = e.get("msg", "").lower()
    return e.get("s") in ("ERROR", "WARN") and any(
        kw in msg for kw in ("ssl", "tls", "certificate", "auth", "unauthorized", "authentication")
    )

def _match_conn_issue(e: dict) -> bool:
    n   = e.get("n", "")
    msg = e.get("msg", "")
    return "org.mongodb.driver" in n and any(
        kw in msg for kw in ("Exception", "Removing server", "Unable", "UNKNOWN")
    )

def _match_index_failure(e: dict) -> bool:
    n   = e.get("n", "").lower()
    msg = e.get("msg", "").lower()
    return ("index" in n or "lucene" in n) and e.get("s") in ("ERROR", "WARN") and any(
        kw in msg for kw in ("fail", "error", "exception", "corrupt", "invalid")
    )

def _match_replication(e: dict) -> bool:
    n   = e.get("n", "").lower()
    msg = e.get("msg", "").lower()
    return "changestream" in n and e.get("s") in ("ERROR", "WARN") and any(
        kw in msg for kw in ("lag", "timeout", "fail", "error", "behind")
    )

def _match_initial_sync(e: dict) -> bool:
    n   = e.get("n", "").lower()
    msg = e.get("msg", "").lower()
    return "initialsync" in n or "initial sync" in msg

def _match_warn(e: dict) -> bool:
    return e.get("s") == "WARN" and not _match_tls_auth(e) and not _match_replication(e)

# ── Pattern library ───────────────────────────────────────────────────────────

PATTERNS = [
    {
        "id":          "oom",
        "name":        "Out of Memory",
        "severity":    "crit",
        "match":       _match_oom,
        "description": "JVM OutOfMemoryError detected — mongot needs more heap or pod memory limit is too low.",
    },
    {
        "id":          "errors",
        "name":        "Errors & Fatals",
        "severity":    "crit",
        "match":       _match_error,
        "description": "ERROR or FATAL log entries detected — check examples for root cause.",
    },
    {
        "id":          "tls_auth",
        "name":        "TLS / Auth Issues",
        "severity":    "crit",
        "match":       _match_tls_auth,
        "description": "TLS or authentication errors — verify searchTLSMode and mongot credentials.",
    },
    {
        "id":          "conn_issues",
        "name":        "MongoDB Connection Issues",
        "severity":    "warn",
        "match":       _match_conn_issue,
        "description": "MongoDB driver detected server removal or failures — check replica set health.",
    },
    {
        "id":          "index_failure",
        "name":        "Index Failures",
        "severity":    "warn",
        "match":       _match_index_failure,
        "description": "Lucene index errors — index may be corrupt or a rebuild may be required.",
    },
    {
        "id":          "replication",
        "name":        "Replication / Change Stream Issues",
        "severity":    "warn",
        "match":       _match_replication,
        "description": "Change stream lag or failures — mongot may be falling behind the oplog.",
    },
    {
        "id":          "initial_sync",
        "name":        "Initial Sync Activity",
        "severity":    "info",
        "match":       _match_initial_sync,
        "description": "Initial sync events found — expected during first index build or after a reset.",
    },
    {
        "id":          "warnings",
        "name":        "General Warnings",
        "severity":    "warn",
        "match":       _match_warn,
        "description": "WARN-level entries — review for potential issues.",
    },
]

_SEVERITY_ORDER = {"crit": 0, "warn": 1, "info": 2}


def analyze_pod_logs(pod_name: str, namespace: str, k8s_v1, window: str = "24h") -> dict:
    """
    Fetch and analyze mongot pod logs for known patterns.

    Args:
        pod_name:  K8s pod name.
        namespace: K8s namespace.
        k8s_v1:    Kubernetes CoreV1Api client.
        window:    One of "1h", "24h", "7d", "30d".

    Returns:
        {
            "pod": str, "window": str, "lines_analyzed": int,
            "findings": [{"id", "name", "severity", "count", "description", "examples"}],
            "error": str | None,
        }
    """
    since_seconds = WINDOW_SECONDS.get(window, WINDOW_SECONDS["24h"])

    try:
        raw = k8s_v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            since_seconds=since_seconds,
            tail_lines=_MAX_LINES,
        )
    except Exception as exc:
        log.warning(f"[log_analyzer] Failed to fetch logs for {pod_name}: {exc}")
        return {
            "pod": pod_name, "window": window,
            "lines_analyzed": 0, "findings": [],
            "error": str(exc),
        }

    # Parse JSON lines — skip non-JSON (e.g. the JVM incubator WARNING line)
    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    findings = []
    for pattern in PATTERNS:
        matched = [e for e in entries if pattern["match"](e)]
        if not matched:
            continue

        examples = []
        for e in matched[:3]:
            ts  = e.get("t", "")[:19]
            msg = e.get("msg", "")
            attr = e.get("attr")
            detail = f"{msg} — {attr}" if attr else msg
            examples.append(f"[{ts}] {detail[:140]}")

        findings.append({
            "id":          pattern["id"],
            "name":        pattern["name"],
            "severity":    pattern["severity"],
            "count":       len(matched),
            "description": pattern["description"],
            "examples":    examples,
        })

    findings.sort(key=lambda f: _SEVERITY_ORDER.get(f["severity"], 3))

    return {
        "pod":            pod_name,
        "window":         window,
        "lines_analyzed": len(entries),
        "findings":       findings,
        "error":          None,
    }
