> 🇮🇹 **Documentazione in italiano disponibile:** [README-it.md](README-it.md)

# 🔬 MongoDB Search Diagnostics

**mongot-monitor is an open-source diagnostic tool for MongoDB Atlas Search nodes.**
It detects performance issues, indexing lag, and configuration problems automatically.

Designed for **SRE**, **MongoDB operators**, and **platform engineers** running Atlas Search on Kubernetes.

![Dashboard Screenshot](dashboard.png)

---

## What does it do?

- **Detects** stuck search nodes, indexing lag, OOMKilled events, and configuration drift
- **Analyzes** search query efficiency, scan ratios, and HNSW graph traversal in real time
- **Alerts** you before problems become outages — predictive oplog window, cardinality warnings, stall detection
- **Built-in SRE Advisor** runs 15 automated checks every collection cycle and ranks findings by severity
- **Automatic Search Diagnosis** interprets cluster health instantly — Health Summary, Warnings, Recommendations in one panel
- **Log Intelligence** parses mongot JSON logs automatically and detects errors, failures, and connection issues across configurable time windows

No agents to install. No extra infrastructure. Just point it at your cluster and go.

---

## 📋 Table of Contents

- [✨ Key Features](#-key-features)
- [🚀 Installation & Setup](#-installation--setup)
  - [Mode 1 — Local](#mode-1--local-mac--pc)
  - [Mode 2 — Kubernetes](#mode-2--kubernetes-in-cluster)
- [🔌 API Endpoints](#-api-endpoints)
- [🏗️ Project Structure](#️-project-structure)
- [🧪 Running Tests](#-running-tests)
- [🔬 SRE Advisor — Deep Dive](#-sre-advisor--deep-dive)
- [🩻 Automatic Search Diagnosis](#-automatic-search-diagnosis)
- [🪵 Log Intelligence](#-log-intelligence)

---

## ✨ Key Features

- 🧠 **SRE Advisor** — 15 automated checks, severity-ranked (crit → warn → pass), served via `/api/advisor` — [see deep dive below](#-sre-advisor--deep-dive)
- 📡 **Real-time Search QPS & Latency** — delta-based computation across Prometheus scrape cycles, separate for `$search` and `$vectorSearch`
- 🎯 **Search Efficiency (Scan Ratio)** — EMA-smoothed `candidates_examined / results_returned`, separate ratio for text and vector search, with cardinality detection
- 🧬 **HNSW Visited Nodes** — early warning for ANN CPU saturation before latency becomes visible
- ⏳ **Index Build ETA** — animated progress bar, docs/sec speed, stall detection, dynamic ETA
- 🔍 **Robust Pod Discovery** — 4-level hierarchy resilient to MCK upgrades and naming variations
- 🌊 **Sync Pipeline Analyzer** — real-time `DB → Change Stream → RAM → Lucene` pipeline visualization with bottleneck identification
- ⏱️ **Predictive Oplog Window** — warn at 40%, crit at 70% window consumed to prevent forced Initial Sync
- 🩺 **Universal K8s Diagnostics** — Helm releases, MCK/K8s versions, PVCs, OOMKilled events, live log streaming
- 📜 **Log Management & Export** — live terminal, download filtered by time window and severity
- ⚡ **Background Collector & Rate Engine** — daemon thread, < 1ms API response from in-memory cache, counter-reset safe
- 🔌 **Stable Versioned API** — `/api/v1/search_metrics` with fixed schema, safe for external consumers
- 🔒 **Security** — optional Basic Auth, CSP headers, K8s name input validation, configurable CORS
- 🩻 **Automatic Search Diagnosis** — real-time cluster health panel: Health Summary / Warnings / Recommendations; also available via `/api/diagnose` and `--diagnose` CLI (exit 0/1/2 for CI pipelines)
- 🪵 **Log Intelligence** — on-demand mongot JSON log analysis with configurable time window (1h / 24h / 7d / 30d); detects errors, OOM, TLS/auth issues, connection failures, index failures, change stream problems

---

## 🚀 Installation & Setup

> **Prerequisites**: `kubectl` configured and pointing to your cluster. A MongoDB connection string with read access on `local` (oplog) and your target collections.

---

### Mode 1 — Local (Mac / PC)

Use this mode for development, demos, or when you prefer running the monitor outside the cluster.

**1. Clone and install**

```bash
git clone https://github.com/Miccolomi/mongot-monitor.git
cd mongot-monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**2. Start**

```bash
python3 mongot_monitor.py \
  --uri "mongodb://USER:PASSWORD@HOST:PORT/admin?replicaSet=RS&authSource=admin&authMechanism=SCRAM-SHA-256" \
  --namespace mongodb \
  --port 5050
```

Open your browser at: **http://localhost:5050**

**CLI options**

| Parameter | Default | Description |
|:---|:---|:---|
| `--uri` | — | MongoDB connection string |
| `--namespace` | all | Kubernetes namespace to monitor |
| `--port` | `5050` | HTTP port for the dashboard |
| `--interval` | `5` | Collection interval in seconds |
| `--auth` | — | Basic Auth — format `user:password` |
| `--in-cluster` | `false` | K8s auth via ServiceAccount (in-cluster only) |
| `--host` | `0.0.0.0` | Flask binding address |
| `--allowed-origins` | localhost | CORS allowed origins (space-separated) |

---

### Mode 2 — Kubernetes (in-cluster)

Use this mode for a permanent deployment inside the cluster. The monitor runs as a pod and uses a ServiceAccount with RBAC to access the Kubernetes API.

**1. Build the Docker image**

```bash
docker build -t mongot-monitor:latest .
```

For a private registry (Docker Hub, ECR, GCR):

```bash
docker build -t <your-registry>/mongot-monitor:1.0.0 .
docker push <your-registry>/mongot-monitor:1.0.0
```

Update the `image:` field in `k8s/deployment.yaml` accordingly.

> ⚠️ **Important**: after every code update, rebuild and restart the deployment:
> ```bash
> docker build -t mongot-monitor:latest .
> kubectl rollout restart deployment/mongot-monitor -n mongodb
> ```

**2. Configure the MongoDB URI**

The connection to **mongod** is required for oplog, index, and compliance checks.
**mongot** is always discovered automatically via Kubernetes — no URI needed for it.

Edit `k8s/secret.yaml` based on where your mongod is running:

```bash
# Scenario A — mongod inside the cluster (MCK): use the internal Service DNS
kubectl get svc -n mongodb   # look for a ClusterIP on port 27017
```

```yaml
# Scenario A — in-cluster (MCK)
stringData:
  MONGODB_URI: "mongodb://USER:PASSWORD@<rs-name>-svc.<namespace>.svc.cluster.local/admin?replicaSet=<RS>&tls=true&tlsAllowInvalidCertificates=true&authSource=admin&authMechanism=SCRAM-SHA-256"

# Scenario B — Atlas (SRV)
# MONGODB_URI: "mongodb+srv://USER:PASSWORD@cluster0.xxxxx.mongodb.net/admin?authSource=admin&authMechanism=SCRAM-SHA-256"

# Scenario C — External replica set with DNS-resolvable hostnames
# MONGODB_URI: "mongodb://USER:PASSWORD@host1:27017,host2:27017/admin?replicaSet=RS&tls=true&authSource=admin&authMechanism=SCRAM-SHA-256"
```

> `authMechanism=SCRAM-SHA-256` is required by MongoDB 7+ with MCK.

**3. Apply manifests**

```bash
kubectl apply -f k8s/rbac.yaml        # ServiceAccount + ClusterRole
kubectl apply -f k8s/secret.yaml      # MongoDB URI
kubectl apply -f k8s/deployment.yaml  # Deployment
kubectl apply -f k8s/service.yaml     # NodePort
```

| File | Description |
|:---|:---|
| `k8s/rbac.yaml` | ServiceAccount + ClusterRole with minimal permissions (includes `pods/proxy`) |
| `k8s/secret.yaml` | MongoDB URI as a K8s Secret |
| `k8s/deployment.yaml` | Deployment with liveness and readiness probes on `/healthz` |
| `k8s/service.yaml` | NodePort Service to expose the dashboard |

> **Namespace**: all manifests default to `mongodb`. Update `namespace:` in all 4 files if yours is different.

**4. Access the dashboard**

```bash
kubectl get svc mongot-monitor -n mongodb
# Example: 5050:31855/TCP  →  NodePort = 31855
```

- **Docker Desktop**: `http://localhost:<NODE_PORT>`
- **Remote cluster** (GKE, EKS, on-prem): `http://<NODE_IP>:<NODE_PORT>` (see `kubectl get nodes -o wide`)

> On Docker Desktop with MCK, the internal DNS (`<rs>-svc.mongodb.svc.cluster.local`) is reachable directly from the pod. Do not use hostnames from the host's `/etc/hosts` — they are not resolvable from inside the cluster.

---

## 🔌 API Endpoints

| Endpoint | Description |
|:---|:---|
| `/` | HTML Dashboard |
| `/metrics` | Full JSON snapshot (from cache) |
| `/api/v1/search_metrics` | Stable versioned API — fixed schema for external consumers |
| `/api/advisor` | SRE findings in JSON (crit → warn → pass) |
| `/healthz` | Liveness probe — always returns 200 if Flask is running |
| `/healthcheck` | Detailed status (MongoDB ping, K8s API, cache age) |
| `/api/logs/<ns>/<pod>` | Last 50 lines of pod logs |
| `/api/download_logs/<ns>/<pod>` | Download logs (`?time=1h&level=error`) |
| `/api/diagnose` | Structured diagnosis: health, warnings, recommendations |
| `/api/logs/analyze/<ns>/<pod>` | Log Intelligence — pattern analysis (`?window=1h\|24h\|7d\|30d`) |

---

## 🏗️ Project Structure

```
mongot_monitor.py        # App Factory + CLI entry point
background.py            # BackgroundCollector (thin orchestrator, daemon thread)
advisor.py               # SRE Advisor engine (15 checks, pure Python)
security.py              # Input validation, security headers, Basic Auth
state.py                 # Shared mutable state (clients, cache, lock)

engine/
  rate_calculator.py     # Delta/rate engine: QPS, latency, scan ratio EMA, HNSW, ETA
                         # Counter reset safety, spike guard, first-cycle protection

collectors/
  kubernetes.py          # K8s discovery (pods, CRDs, PVCs, services, helm)
  mongodb.py             # MongoDB collectors (vitals, oplog, indexes)
  prometheus.py          # Prometheus scraper with dual fallback

routes/
  api.py                 # API Blueprint (/metrics, /api/v1/search_metrics, /api/advisor, /api/logs)
  frontend.py            # Frontend Blueprint (/, /favicon.ico)

frontend/
  templates/
    dashboard.html       # Jinja2 template
  static/
    css/main.css
    js/
      utils.js           # Utilities (formatBytes, pill, gaugeRing, …)
      logs.js            # Live log management
      advisor.js         # Advisor renderer
      pipeline.js        # Sync Pipeline Analyzer
      render.js          # Main renderer + polling

tests/
  conftest.py
  test_advisor.py        # tests — every SRE check
  test_background.py     # tests — collector and cache
  test_frontend.py       # tests — dashboard, CSS, JS, API
  test_security.py       # tests — validation, headers, auth
```

---

## 🧪 Running Tests

```bash
source venv/bin/activate
python3 -m pytest tests/ -v
```

---

## 🔬 SRE Advisor — Deep Dive

Every collection cycle runs a set of Python checks against the cluster and index state. Findings are sorted by severity (crit → warn → pass) and served via `/api/advisor`.

### Checks overview

| # | Check | Thresholds |
|:---|:---|:---|
| 1 | **Disk Space (200% Rule)** | warn if free < 200% of used; crit if disk ≥ 90% (mongot enters read-only) |
| 2 | **Index Consolidation** | warn if more than one index of the same type on the same collection (fullText + vectorSearch is valid: Hybrid Search) |
| 3 | **I/O Bottleneck** | crit if disk queue > 10 AND lag > 5s simultaneously |
| 4 | **CPU & QPS** | crit if CPU > 80%; warn if QPS > 10 × cores |
| 5 | **Memory Starvation (Page Faults)** | warn > 500/s; crit > 1000/s |
| 6 | **OOMKilled & MMap Risk** | crit if JVM heap ≥ 90% of pod limit or OOMKilled detected |
| 7 | **CRD Operator Status** | crit if CRD is not in `Running` phase |
| 8 | **Storage Class Performance** | warn if PVC uses `standard`, `hostpath`, or `slow` |
| 9 | **Operator Versioning** | warn if operator image uses `:latest` tag |
| 10 | **Predictive Oplog Window** | warn > 40% consumed; crit > 70% consumed — prevents forced Initial Sync |
| 11 | **Search Auth** | crit if `skipAuthenticationToSearchIndexManagementServer=true` — mongod↔mongot without authentication |
| 12 | **Search TLS Mode** | crit if `searchTLSMode=disabled`; warn if `allowTLS`/`preferTLS`; pass if `requireTLS` |
| 13 | **Search Efficiency (Scan Ratio)** | warn > 50:1; crit > 500:1; predictive warning if high ratio + low latency (cardinality problem) |
| 14 | **Vector Search Efficiency** | same thresholds as scan ratio but computed separately for `$vectorSearch` |
| 15 | **HNSW Visited Nodes** | warn > 1000 nodes/query; crit > 5000 — early warning for ANN CPU saturation |

### 📡 Search QPS & Real-Time Latency

The **🔎 Search Commands** panel shows throughput metrics computed as deltas between successive Prometheus scrape cycles:

- **`$search QPS`** and **`$vectorSearch QPS`** displayed prominently (requests/second)
- **Average latency** computed as `Δlatency_sum / Δcount` — actual per-query latency, not a peak
- **Max latency** — historical peak from the Prometheus counter
- **Failure counters** for `$search` and `$vectorSearch`

QPS values activate from the second collection cycle onward (a time delta is required).

### 🎯 Search Efficiency — Scan Ratio (EMA-smoothed)

`scan_ratio = candidates_examined / results_returned` is the true indicator of search query efficiency. Latency alone is not enough: a 50ms query with 200k candidates examined will become a timeout as the dataset grows.

Two **separate ratios** are computed: one for `$search` (`mongot_query_candidates_examined_total` with fallback to `mongot_query_documents_scanned`) and one dedicated for `$vectorSearch` (`mongot_vector_query_candidates_examined_total`).

To avoid false positives under low traffic (e.g. 1 result / 500 candidates from a single query), the ratio is **EMA-smoothed** (α = 0.3) with a guard: if `Δresults < 10` the EMA is not updated.

| Ratio | Meaning |
|:---|:---|
| < 5 | Excellent — highly selective index |
| 5 – 50 | Normal |
| 50 – 500 | Inefficient query — review index or analyzer |
| > 500 | Critical — index or query is seriously problematic |

**Predictive cardinality detection:** if `scan_ratio > 50` but `latency < 100ms`, the Advisor emits a warning — the index is non-selective but the dataset is still small enough to hide the cost. This signal is not provided by Ops Manager.

**Zero-results anti-pattern:** if `results_returned = 0` but `candidates_examined > 0`, a specific warning is raised. Common causes: post-search `$match` too restrictive, scoring threshold too high, misconfigured pipeline.

### 🧬 HNSW Visited Nodes — Early Warning CPU Saturation

`mongot_vector_search_hnsw_visited_nodes` (fallback: `mongot_vector_search_graph_nodes_visited`) measures how many nodes in the HNSW graph are traversed per `$vectorSearch` query. It is an **early warning for CPU saturation**: load increases before latency becomes visible.

| Visited nodes | Meaning |
|:---|:---|
| < 200 | Excellent |
| 200 – 1000 | Normal |
| > 1000 | Costly query — monitor CPU |
| > 5000 | ANN inefficient — CPU saturation imminent |

High values indicate ANN is degrading toward brute-force, typically due to excessive `efSearch`, poor graph connectivity, or oversized embedding dimensions. The check is optional: skipped if the metric is not exposed by the installed mongot version.

### ⏳ Index Build ETA

During an Initial Sync or bulk index build, a dedicated **"⚙️ Index Build in Progress"** panel appears with:

- **Animated progress bar** — green > 75%, orange < 75%, red if stalled
- **Document counter** — processed / total with percentage
- **Speed** in docs/sec (computed as a delta between collection cycles)
- **Dynamic ETA** in h/m/s format or **"INDEX BUILD STALLED"** warning if speed drops below 100 docs/s for at least 30 seconds

The panel is only shown while an Initial Sync is active (`initial_sync_in_progress > 0`).

### 🔍 Robust Pod Discovery (4-level hierarchy)

Pod discovery uses a hierarchy resilient to rolling upgrades, scaling events, and naming variations across MCK versions:

1. **Official MCK label** `app.kubernetes.io/component=search` — most reliable
2. **Container name** `mongot` — stable fallback across MCK versions
3. **Container image** — contains `mongodb-enterprise-search` or `mongot`
4. **Pod name (last resort)** — heuristic, excludes `mongod` and `monitor`

The monitor pod itself is always excluded via `app: mongot-monitor`.

### ⚡ Background Collector & Rate Engine

Data collection runs on a separate daemon thread at a configurable interval. The `/metrics` endpoint always responds in < 1ms from the in-memory cache — the dashboard never blocks on external calls.

All delta/rate computation logic is isolated in `engine/rate_calculator.py`, separated from the collection loop:

- **`background.py`** is a thin orchestrator: scrape → `compute_pod_rates()` → cache update
- **`engine/rate_calculator.py`** contains QPS, average latency, scan ratio EMA, HNSW, ETA — independently testable
- **Counter reset safety**: `_safe_delta()` returns `None` on negative delta (counter reset after mongot pod restart); spike guard discards QPS > 50,000/s; first cycle (`last_s=None`) skips all computation silently — no spurious spikes on startup

### 🔌 Stable API (`/api/v1/search_metrics`)

Versioned JSON endpoint with a fixed schema, decoupled from internal Prometheus metric names:

```json
{
  "schema_version": "1",
  "timestamp": "...",
  "collect_ms": 42,
  "pods": {
    "mongot-pod-0": {
      "pod":        { "namespace", "node", "phase", "all_ready", "total_restarts" },
      "qps":        { "search": 1.5, "vectorsearch": 0.3 },
      "latency_sec":{ "search_avg", "search_max", "vectorsearch_avg", "vectorsearch_max" },
      "failures":   { "search": 0, "vectorsearch": 0 },
      "efficiency": { "search_scan_ratio", "vectorsearch_scan_ratio", "hnsw_visited_nodes", "zero_results_with_candidates" },
      "indexing":   { "replication_lag_sec", "initial_sync_active", "updates_per_sec", "eta" }
    }
  }
}
```

Safe for external consumers (CI performance gates, Grafana dashboards, alerting tools) — the backend can evolve without breaking the API contract.

---

## 🩻 Automatic Search Diagnosis

Every collection cycle, the diagnosis engine interprets the full cluster state and presents it in three columns directly in the dashboard:

- **Health Summary** — all passing checks listed as `✔`
- **Warnings & Critical** — failing checks with detail message
- **Recommendations** — actionable next steps derived from each finding

The health status (`HEALTHY` / `DEGRADED` / `CRITICAL`) is immediately visible at the top of the panel.

### API

```bash
GET /api/diagnose
```

```json
{
  "health": "degraded",
  "summary": { "pass": 12, "warn": 2, "crit": 1 },
  "critical": [{ "title": "OOMKilled & MMap Risk", "detail": "..." }],
  "warnings":  [{ "title": "Disk Space (200% Rule)", "detail": "..." }],
  "healthy":   [{ "title": "CRD Operator Status" }, ...],
  "recommendations": ["Increase memory limit...", "Check disk usage..."]
}
```

### CLI

Run a single diagnostic cycle and exit — useful in CI/CD pipelines:

```bash
python3 mongot_monitor.py --diagnose \
  --uri "mongodb://..." --namespace mongodb
```

Exit codes: `0` = healthy, `1` = degraded, `2` = critical.

---

## 🪵 Log Intelligence

On-demand analysis of mongot JSON logs directly from the dashboard. Parses the structured log format (`{"t":..., "s":..., "n":..., "msg":..., "attr":...}`) and detects known failure patterns.

### Configurable time window

| Window | Description |
|:---|:---|
| `1h` | Last hour — quick triage |
| `24h` | Last 24 hours — default |
| `7d` | Last 7 days — trend analysis |
| `30d` | Last 30 days — long-term issues |

Up to 2,000 JSON lines are analyzed per request (memory guard).

### Detected patterns

| Pattern | Severity | Detection |
|:---|:---|:---|
| Out of Memory | 🔴 crit | `OutOfMemoryError` in `msg` or `attr` |
| Errors & Fatals | 🔴 crit | `s == "ERROR"` or `"FATAL"` |
| TLS / Auth Issues | 🔴 crit | `ssl`/`tls`/`auth`/`certificate` in `msg` + ERROR/WARN |
| MongoDB Connection Issues | 🟡 warn | `org.mongodb.driver` class + `Exception`/`Removing server` |
| Index Failures | 🟡 warn | `index`/`lucene` class + `fail`/`corrupt`/`invalid` |
| Replication / Change Stream | 🟡 warn | `changestream` class + `lag`/`timeout`/`fail` |
| Initial Sync Activity | 🔵 info | `initialsync` class |
| General Warnings | 🟡 warn | `s == "WARN"` |

### API

```bash
GET /api/logs/analyze/<namespace>/<pod>?window=24h
```

```json
{
  "pod": "my-replica-set-search-0",
  "window": "24h",
  "lines_analyzed": 350,
  "findings": [
    {
      "id": "errors",
      "name": "Errors & Fatals",
      "severity": "crit",
      "count": 3,
      "description": "ERROR or FATAL log entries detected...",
      "examples": ["[2026-03-05T14:09:07] Connection refused — ..."]
    }
  ]
}
```
