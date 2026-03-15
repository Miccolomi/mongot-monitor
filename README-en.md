# 🚀 Mongot Ultimate Monitor

An advanced, standalone Enterprise dashboard for monitoring MongoDB Search (`mongot`) nodes deployed on Kubernetes via the MongoDB Kubernetes Operator (`MongoDBSearch` CRD).

This tool goes beyond standard Prometheus metrics: it correlates real-time database data (Oplog, index status) with infrastructure state (Kubernetes Events, PVC, CPU Limits, Live Logs) to provide a unified view of your search stack and a built-in **Python-backed SRE Advisor**.

---

## 🆕 What's New

### Milestone 1 — Robust mongot pod discovery
Pod discovery now uses a 4-level hierarchy, resilient to rolling upgrades, scaling events, and naming variations:

1. **Official MCK label** `app.kubernetes.io/component=search` — the most reliable method
2. **Container name** `mongot` — stable fallback across MCK versions
3. **Container image** — contains `mongodb-enterprise-search` or `mongot`
4. **Pod name (last resort)** — heuristic on `mongot` in the name, excluding `mongod` and `monitor`

The monitor pod itself is always excluded via the `app: mongot-monitor` label.

### Milestone 2 — Real-time Index Build ETA
During an Initial Sync or a bulk index build, the dashboard shows a dedicated **"⚙️ Index Build in Progress"** panel with:

- **Animated progress bar** (color: green > 75%, orange < 75%, red if stalled)
- **Document counter** — processed / total with percentage
- **Speed** in docs/sec (computed as a delta between collection cycles)
- **Dynamic ETA** (`fEta()` — h/m/s format) or **"INDEX BUILD STALLED"** warning if the rate drops below 100 docs/s for at least 30 seconds

The panel is only visible while an Initial Sync is active (`initial_sync_in_progress > 0`).

### Milestone 4 — Search Efficiency: Scan Ratio (mongot_query_candidates_examined)

The metric `mongot_query_candidates_examined` (or `mongot_query_documents_scanned` in newer mongot versions) measures how many documents the index must examine before producing the final result set.

The ratio:

```
scan_ratio = candidates_examined / results_returned
```

is the true indicator of search query efficiency. Latency alone is not enough: a 50ms query with `candidates_examined = 200k` will become a timeout as soon as the dataset grows.

**Ratio interpretation:**

| Ratio | Meaning |
|:---|:---|
| < 5 | Excellent — highly selective index |
| 5 – 50 | Normal |
| 50 – 500 | Inefficient query — review index or analyzer |
| > 500 | Critical — index or query is seriously problematic |

**Automatically detected anti-pattern:** if `results_returned = 0` but `candidates_examined > 0`, the dashboard shows a specific warning. Common causes: post-search `$match` filter too restrictive, scoring threshold too high, misconfigured pipeline.

**How it works:**
- The collector reads the cumulative counter from Prometheus (with automatic fallback between the two metric names depending on mongot version)
- The Background Collector computes `scan_ratio` as a delta between successive cycles, exactly like QPS
- The **"🔎 Search Commands"** panel shows an **"Index Efficiency"** section with a color-coded ratio and text label
- The **SRE Advisor** automatically generates a finding (pass/warn/crit) — the check is skipped if the metric is not exposed by the installed mongot version

**Correlation with latency — the real power:**
- High latency + low scan ratio → CPU / IO problem
- High latency + high scan ratio → index or query problem

---

### Milestone 3 — Search Query Rate & Latency in real time
The **"🔎 Search Commands"** panel now shows throughput metrics computed as deltas between successive Prometheus scrape cycles:

- **`$search QPS`** and **`$vectorSearch QPS`** — requests per second, displayed prominently
- **Average latency** (`avg`) — computed as `Δlatency_sum / Δcount` (actual per-query latency)
- **Max latency** (`max`) — historical peak from the Prometheus counter
- **Failure counters** for `$search` and `$vectorSearch`

QPS data activates from the second collection cycle onward (a time delta is required). Until then, values show `0.00 /s` in grey.

---

## ✨ Key Features

- 🧠 **SRE Advisor Backend**: 12 automated Best Practice checks for MongoDB Search (200% disk rule, index consolidation, I/O bottleneck, CPU/QPS, OOMKilled, CRD status, storage class, versioning, predictive oplog window, mongod↔mongot authentication, TLS mode). Logic lives in Python — fully testable.
- 📡 **Search QPS & Real-Time Latency**: Throughput (`$search`, `$vectorSearch`) and average/max latency computed in real time by the Background Collector via Prometheus counter deltas.
- 🎯 **Search Efficiency (Scan Ratio)**: Computes `candidates_examined / results_returned` in real time — the true indicator of index efficiency. Anticipates scalability problems before they appear in latency. Automatically detects the "zero results with candidates examined" anti-pattern.
- ⏳ **Index Build ETA**: Live panel during initial sync with animated progress bar, docs/sec speed, and ETA countdown. Automatically detects a stalled build.
- 🔍 **Robust Pod Discovery**: 4-level hierarchy (MCK label → container name → image → pod name) for reliable discovery in every MCK scenario.
- 🌊 **Atlas Search Sync Pipeline Analyzer**: End-to-end real-time visualization of the active data pipeline (`DB → Change Stream → RAM → Lucene`), computing actual replication lag between MongoDB and mongot.
- ⏱️ **Predictive SRE (Oplog Window)**: Monitors the MongoDB Oplog window to detect critical `mongot` replication lag and prevent catastrophic forced `Initial Sync` before it happens.
- 🩺 **Universal K8s Diagnostics**: Auto-discovers Helm releases, tracks Kubernetes and MCK Operator versions, dynamically maps PVCs, Services, and Pods.
- 📜 **Log Management & Export**: Built-in live terminal to stream `mongot` and Operator pod logs, with full archive download filtered by time window and severity.
- 🚨 **Global Error Handling**: Proactively intercepts and displays every K8s RBAC error, network timeout, and MongoDB auth failure directly on the dashboard.
- 📊 **Prometheus Dual-Fallback Scraper**: Fetches metrics from pods via direct access or K8s API Server Proxy tunnel.
- ⚡ **Background Collector**: Data collection runs on a separate daemon thread — `/metrics` always responds in < 1ms from cache.
- 🔒 **Security**: Optional HTTP Basic Auth, security headers (CSP, X-Frame-Options, X-Content-Type-Options), K8s name input validation, configurable CORS.

---

## 📋 Requirements

- **Python 3.9+**
- Configured Kubernetes access (a valid `~/.kube/config`, or a ServiceAccount if running in-cluster)
- MongoDB connection string (read access on `local` DB for oplog tracking and on your target collections)

---

## 🛠️ Installation

### 1. Clone the repository

```bash
git clone https://github.com/Miccolomi/mongot-monitor.git
cd mongot-monitor
```

### 2. Create the virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

> ⚠️ **Important**: always activate the venv before running the monitor. Your prompt will show `(venv)`.

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

---

## 🚀 Usage

### Quick start (Mac / local PC)

If `kubectl` is already configured to point to your cluster, the script will automatically pick up your local Kubeconfig.

```bash
source venv/bin/activate

python3 mongot_monitor.py \
  --uri "mongodb://<USER>:<PASSWORD>@<HOST1>:<PORT1>,<HOST2>:<PORT2>/<DB>?replicaSet=<RS>&tls=true&tlsAllowInvalidCertificates=true&authSource=admin" \
  --namespace mongodb \
  --port 5050
```

Open your browser at: **http://localhost:5050**

### Real-world example

```bash
python3 mongot_monitor.py \
  --uri "mongodb://mdb-admin:password@work0.mongodb.local:30017,work1.mongodb.local:30018,work2.mongodb.local:30019/admin?replicaSet=my-replica-set&tls=true&tlsAllowInvalidCertificates=true&authSource=admin" \
  --namespace mongodb \
  --port 5051
```

### With Basic Auth (access protection)

```bash
python3 mongot_monitor.py \
  --uri "mongodb://..." \
  --namespace mongodb \
  --auth admin:strong_password
```

---

## ⚙️ CLI Parameters

| Parameter | Default | Description |
|:---|:---|:---|
| `--uri` | — | MongoDB connection string |
| `--port` | `5050` | HTTP port for the dashboard |
| `--host` | `0.0.0.0` | Flask binding address |
| `--namespace` | all | Kubernetes namespace to monitor |
| `--in-cluster` | `false` | K8s authentication via ServiceAccount |
| `--interval` | `5` | Background Collector interval (seconds) |
| `--auth` | — | Enable Basic Auth. Format: `user:password` |
| `--allowed-origins` | localhost | CORS allowed origins (space-separated) |

---

## 🧠 How does the SRE Advisor work?

The **Compliance & Best Practices** panel runs 12 Python checks automatically on every collection cycle:

| # | Check | Thresholds |
|:---|:---|:---|
| 1 | **Disk Space (200% Rule)** | warn if free < 200% of used; crit if disk ≥ 90% (read-only mode) |
| 2 | **Index Consolidation** | warn if more than one index of the same type on the same collection (vectorSearch + fullText on the same collection is valid: Hybrid Search) |
| 3 | **I/O Bottleneck** | crit if disk queue > 10 AND lag > 5s simultaneously |
| 4 | **CPU & QPS** | crit if CPU > 80%; warn if QPS > 10 × cores |
| 5 | **Memory Starvation (Page Faults)** | warn > 500/s; crit > 1000/s |
| 6 | **OOMKilled & MMap Risk** | crit if JVM heap ≥ 90% of pod limit or OOMKilled detected |
| 7 | **CRD Operator Status** | crit if CRD is not in `Running` phase |
| 8 | **Storage Class Performance** | warn if PVC uses `standard`, `hostpath`, or `slow` |
| 9 | **Operator Versioning** | warn if the operator image uses `:latest` tag |
| 10 | **Predictive Oplog Window** | warn > 40% consumed; crit > 70% consumed |
| 11 | **Search Auth** (`skipAuthenticationToSearchIndexManagementServer`) | crit if `true` — mongod↔mongot without authentication |
| 12 | **Search TLS Mode** (`searchTLSMode`) | crit if `disabled`; warn if `allowTLS`/`preferTLS`; pass if `requireTLS` |

Findings are sorted by severity (crit → warn → pass) and served via the `/api/advisor` endpoint.

---

## 🏗️ Project Structure

```
mongot_monitor.py        # App Factory + CLI entry point
background.py            # BackgroundCollector (daemon thread)
advisor.py               # SRE Advisor engine (9 checks, pure Python)
security.py              # Input validation, security headers, Basic Auth
state.py                 # Shared mutable state (clients, cache, lock)

collectors/
  kubernetes.py          # K8s discovery (pods, CRDs, PVCs, services, helm)
  mongodb.py             # MongoDB collectors (vitals, oplog, indexes)
  prometheus.py          # Prometheus scraper with dual fallback

routes/
  api.py                 # API Blueprint (/metrics, /healthcheck, /api/advisor, /api/logs)
  frontend.py            # Frontend Blueprint (/, /favicon.ico)

frontend/
  templates/
    dashboard.html       # Jinja2 template
  static/
    css/main.css
    js/
      utils.js           # Utilities (formatBytes, pill, gaugeRing, …)
      logs.js            # Live log management
      advisor.js         # Thin renderer (logic lives in advisor.py)
      pipeline.js        # Sync Pipeline Analyzer
      render.js          # Main renderer + polling

tests/
  conftest.py
  test_advisor.py        # 52 tests — every SRE check
  test_background.py     # 6 tests — collector and cache
  test_frontend.py       # 47 tests — dashboard, CSS, JS, API
  test_security.py       # 37 tests — validation, headers, auth
```

---

## 🧪 Running Tests

```bash
source venv/bin/activate
python3 -m pytest tests/ -v
```

Expected output: **142 tests, all green**.

---

## 🐳 Containerized Deployment on Kubernetes

### 1. Build the Docker image

```bash
docker build -t mongot-monitor:latest .
```

For a private registry (Docker Hub, ECR, GCR, etc.):

```bash
docker build -t <your-registry>/mongot-monitor:1.0.0 .
docker push <your-registry>/mongot-monitor:1.0.0
```

Update the `image:` field in `k8s/deployment.yaml` accordingly.

### 2. Configure the MongoDB URI

The connection to **mongod** is required for oplog, index, and compliance checks (skipAuth, TLS mode).
**mongot** is always discovered automatically via Kubernetes — no URI needed for it.

Edit `k8s/secret.yaml` based on where your mongod is running:

#### Scenario A — mongod inside the cluster (installed with MCK)

Use the internal K8s DNS name of the replica set headless service:

```bash
# Find the service name
kubectl get svc -n <namespace>
# Look for a ClusterIP service on port 27017 (e.g. my-replica-set-svc)
```

```yaml
stringData:
  MONGODB_URI: "mongodb://USER:PASSWORD@<replica-set-name>-svc.<namespace>.svc.cluster.local/admin?replicaSet=<RS-name>&tls=true&tlsAllowInvalidCertificates=true&authSource=admin&authMechanism=SCRAM-SHA-256"
```

#### Scenario B — mongod outside the cluster (Atlas, on-prem, external VM)

Use the external connection string provided by your MongoDB deployment:

```yaml
# Atlas (SRV):
stringData:
  MONGODB_URI: "mongodb+srv://USER:PASSWORD@cluster0.xxxxx.mongodb.net/admin?authSource=admin&authMechanism=SCRAM-SHA-256"

# Replica set with DNS-resolvable hostnames:
stringData:
  MONGODB_URI: "mongodb://USER:PASSWORD@host1:27017,host2:27017,host3:27017/admin?replicaSet=RS&tls=true&authSource=admin&authMechanism=SCRAM-SHA-256"
```

> `authMechanism=SCRAM-SHA-256` is required by MongoDB 7+ with MCK. Remove it only if you are using MongoDB ≤ 6 with SCRAM-SHA-1 authentication.

### 3. Apply manifests in order

```bash
# 1. RBAC (ServiceAccount + ClusterRole + Binding)
kubectl apply -f k8s/rbac.yaml

# 2. MongoDB URI Secret
kubectl apply -f k8s/secret.yaml

# 3. Deployment
kubectl apply -f k8s/deployment.yaml

# 4. Service (NodePort)
kubectl apply -f k8s/service.yaml
```

### 4. Access the dashboard

```bash
# Find the assigned NodePort
kubectl get svc mongot-monitor -n mongodb
# Example output: 5050:31855/TCP  →  NodePort = 31855
```

**Docker Desktop**: the node is `localhost`, so open:
```
http://localhost:<NODE_PORT>
```

**Remote cluster** (GKE, EKS, on-prem): use the IP of any worker node:
```bash
kubectl get nodes -o wide   # INTERNAL-IP or EXTERNAL-IP column
http://<NODE_IP>:<NODE_PORT>
```

### Note for local development (Docker Desktop)

If you are testing on Docker Desktop with MongoDB already installed in the cluster via MCK, the internal service DNS (`my-replica-set-svc.mongodb.svc.cluster.local`) is directly reachable from the pod — no additional configuration needed. Avoid using hostnames defined in `/etc/hosts` on the host machine (e.g. `work0.mongodb.local`): they are not resolvable from inside the pod.

### Manifest overview

| File | Description |
|:---|:---|
| `k8s/rbac.yaml` | ServiceAccount + ClusterRole with minimal permissions |
| `k8s/secret.yaml` | MongoDB URI as a K8s Secret |
| `k8s/deployment.yaml` | Deployment with liveness (`/healthz`) and readiness (`/healthz`) probes |
| `k8s/service.yaml` | NodePort Service to expose the dashboard |

> **Namespace**: all manifests default to the `mongodb` namespace. Update the `namespace:` field in all 4 files if yours is different.

---

## 🔌 API Endpoints

| Endpoint | Method | Description |
|:---|:---|:---|
| `/` | GET | HTML Dashboard |
| `/metrics` | GET | Full JSON snapshot (from cache) |
| `/api/advisor` | GET | SRE findings in JSON |
| `/healthcheck` | GET | Monitor health status |
| `/api/logs/<ns>/<pod>` | GET | Last 50 lines of pod logs |
| `/api/download_logs/<ns>/<pod>` | GET | Download logs (`?time=1h&level=error`) |

