# 🚀 Mongot Ultimate Monitor

An advanced, standalone Enterprise dashboard for monitoring MongoDB Search (`mongot`) nodes deployed on Kubernetes via the MongoDB Kubernetes Operator (`MongoDBSearch` CRD).

This tool goes beyond standard Prometheus metrics: it correlates real-time database data (Oplog, index status) with infrastructure state (Kubernetes Events, PVC, CPU Limits, Live Logs) to provide a unified view of your search stack and a built-in **Python-backed SRE Advisor**.

---

## ✨ Key Features

- 🧠 **SRE Advisor Backend**: 12 automated Best Practice checks for MongoDB Search (200% disk rule, index consolidation, I/O bottleneck, CPU/QPS, OOMKilled, CRD status, storage class, versioning, predictive oplog window, mongod↔mongot authentication, TLS mode). Logic lives in Python — fully testable.
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

### In-Cluster execution (as a K8s Pod)

```bash
python3 mongot_monitor.py \
  --uri "mongodb://..." \
  --namespace mongodb \
  --in-cluster
```

> The pod's ServiceAccount must be bound to a Role/ClusterRole granting read permissions on `pods`, `pods/log`, `events`, `services`, `persistentvolumeclaims`, `mongodbsearch` CRDs, and `deployments`.

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

## 🔌 API Endpoints

| Endpoint | Method | Description |
|:---|:---|:---|
| `/` | GET | HTML Dashboard |
| `/metrics` | GET | Full JSON snapshot (from cache) |
| `/api/advisor` | GET | SRE findings in JSON |
| `/healthcheck` | GET | Monitor health status |
| `/api/logs/<ns>/<pod>` | GET | Last 50 lines of pod logs |
| `/api/download_logs/<ns>/<pod>` | GET | Download logs (`?time=1h&level=error`) |

---

## 📄 License

MIT
