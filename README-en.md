# 🚀 MONGOT Ultimate Monitor

An advanced, standalone Enterprise dashboard for monitoring MongoDB Search (`mongot`) nodes deployed on Kubernetes via the MongoDB Kubernetes Operator (`MongoDBSearch` CRD).

This tool is built to go beyond standard Prometheus metrics. It correlates real-time database data (Oplog, index status) with infrastructure state (Kubernetes Events, PVC, CPU Limits, Live Logs) to provide a unified view of your search stack and a built-in **automatic SRE Advisor**.

![MONGOT Ultimate Monitor](https://raw.githubusercontent.com/Miccolomi/mongot-monitor/main/screenshot.png)

## ✨ Key Features

* 🧠 **Integrated SRE Advisor**: Analyzes your configuration in real-time and flags violations of Best Practices (e.g. Insufficient disk space, I/O bottlenecks, CPU under-provisioning, MMap OOMKilled Risk).
* 🌊 **Atlas Search Sync Pipeline Analyzer**: End-to-end visualization of the active Change Stream pipeline (`DB ➔ Change Stream ➔ RAM ➔ Lucene`), computing the actual real-time Replication Lag between MongoDB components.
* ⏱️ **Predictive SRE (Oplog Window)**: Actively monitors the MongoDB Oplog Replication Window to detect unacceptable `mongot` lag and prevent catastrophic `Initial Sync` scenarios before they occur.
* 🩺 **Universal K8s Diagnostics**: Instantly discovers MongoDB Helm Charts, tracks Kubernetes and Operator versions in use, and dynamically maps all relevant PVCs, Services, and Pods.
* 📜 **Log Management & Export**: Built-in Live terminal for streaming Operator and `mongot` pod logs, featuring direct complete archive downloads with time and severity (error) filtering.
* 🚨 **Global Error Handling**: Proactive UI alerts that gracefully intercept and explicitly highlight any K8s RBAC permission issues, network timeouts, or MongoDB auth failures on the dashboard.
* 📊 **Prometheus Triple-Fallback Scraper**: Fetches Prometheus metrics from pods bypassing network restrictions out-of-cluster using dynamic K8s API tunnels (Proxy or Exec `wget`).
* 🔎 **Smart Index Monitoring**: Auto-detects both `$search` and `$vectorSearch` indexes. It actively bypasses native MongoDB limitations to accurately count indexed documents and fixes "ghost" states.

## 📋 Requirements

* **Python 3.8+**
* Configured Kubernetes access (a valid `~/.kube/config` or a ServiceAccount if running in-cluster)
* MongoDB Connection String (must have read privileges on the `local` DB for oplog tracking and read access to your target collections).

## 🛠️ Installation

1. Clone the repository:
   ```bash
   git clone [https://github.com/Miccolomi/mongot-monitor.git](https://github.com/Miccolomi/mongot-monitor.git)
   cd mongot-monitor
   ```

2. Create a virtual environment (recommended):
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## 🚀 Usage

The monitor acts as a zero-configuration Flask application for the frontend (HTML/JS/CSS are served directly by the backend).

### 1. Standalone Execution (Local Mac / PC)

If `kubectl` is already configured to point to your cluster, the script will automatically pick up your local Kubeconfig.

```bash
python mongot_monitor.py \
  --uri "mongodb://<USER>:<PASSWORD>@<HOSTS>/?replicaSet=<RS>&tls=true&authSource=admin" \
  --namespace mongodb
```

*Replace `--namespace mongodb` with the actual K8s namespace where your `mongot` pods are deployed.*

Open your browser at: **http://localhost:5050**

### 2. In-Cluster Execution (as a K8s Pod)

If you want to deploy this monitor permanently inside your Kubernetes cluster, use the `--in-cluster` flag. The script will authenticate using the pod's injected ServiceAccount to query the K8s APIs.

```bash
python mongot_monitor.py \
  --uri "mongodb://..." \
  --namespace mongodb \
  --in-cluster
```

*(Note: The pod's ServiceAccount must be bound to a Role/ClusterRole granting read permissions for `pods`, `pods/log`, `pods/exec`, `events`, `services`, `persistentvolumeclaims`, as well as `mongodbsearch` CRDs and `deployments`).*

## ⚙️ CLI Parameters

| Parameter | Description | Default | 
| :--- | :--- | :--- | 
| `--uri` | MongoDB Connection String. | `None` (K8s metrics only) | 
| `--port` | Port to expose the web dashboard. | `5050` | 
| `--host` | Flask binding interface. | `0.0.0.0` | 
| `--namespace` | Target Kubernetes namespace to scan. | Auto-discover on all namespaces | 
| `--in-cluster` | Enables K8s authentication via ServiceAccount. | `False` | 

## 🧠 How does the SRE Advisor work?

The **Compliance & Best Practices** panel automatically calculates the following indicators:

* **Disk Space (125% Rule)**: Ensures there is enough free disk space (1.25x the current usage) to allow Lucene to safely rebuild indexes in the background.
* **Index Consolidation**: Alerts you if multiple fragmented indexes are defined on the same collection (an anti-pattern).
* **I/O Bottleneck**: Correlates *Disk Queue Length* with *Oplog Lag* to determine if your K8s storage volumes (PVCs) are bottlenecking the indexing process.
* **CPU/QPS Ratio**: Checks that at least 1 Core is allocated for every 10 Queries Per Second, based on traffic detected by the MongoDB profiler.
