# 🚀 Mongot Ultimate Monitor

Un cruscotto Enterprise avanzato e standalone per il monitoraggio dei nodi di ricerca MongoDB Search (`mongot`) deployati su Kubernetes tramite il MongoDB Kubernetes Operator (CRD `MongoDBSearch`).

Questo tool va oltre le classiche metriche Prometheus: incrocia in tempo reale i dati del database (Oplog, stato degli indici) con lo stato dell'infrastruttura (Kubernetes Events, PVC, CPU Limits, Live Logs) per fornire una vista unificata dello stack di ricerca e un **SRE Advisor automatico** basato su Python.

---

## ✨ Caratteristiche Principali

- 🧠 **SRE Advisor Backend**: 12 check automatici sulle Best Practice MongoDB Search (spazio disco 200%, consolidamento indici, I/O, CPU/QPS, OOMKilled, CRD status, storage class, versioning, finestra oplog predittiva, autenticazione mongod↔mongot, TLS mode). La logica è in Python, completamente testabile.
- 🌊 **Atlas Search Sync Pipeline Analyzer**: Visualizza e monitora in tempo reale l'intero flusso dati (`DB → Change Stream → RAM → Lucene`), calcolando il Lag effettivo tra MongoDB e mongot.
- ⏱️ **SRE Predittivo (Oplog Window)**: Monitora la finestra dell'Oplog per individuare ritardi critici nella replication di `mongot` e prevenire `Initial Sync` catastrofici prima che accadano.
- 🩺 **Diagnostica K8s Universale**: Auto-scopre installazioni Helm, verifica versioni Kubernetes e Operator MCK, mappa dinamicamente PVC, Servizi e Pod.
- 📜 **Log Management & Export**: Terminale live integrato per visualizzare i log di mongot e dell'Operator, con download completo filtrato per finestra temporale e severità.
- 🚨 **Global Error Handling**: Intercetta e mostra ogni errore K8s RBAC, timeout di rete o fallimento di autenticazione MongoDB direttamente nella UI.
- 📊 **Prometheus Doppio Fallback**: Scarica le metriche dai pod tramite accesso diretto o tunnel via K8s API Server Proxy.
- ⚡ **Background Collector**: La raccolta dati avviene su un thread daemon separato — `/metrics` risponde sempre in < 1ms dalla cache.
- 🔒 **Sicurezza**: HTTP Basic Auth opzionale, security headers (CSP, X-Frame-Options, X-Content-Type-Options), validazione input K8s names, CORS configurabile.

---

## 📋 Requisiti

- **Python 3.9+**
- Accesso al cluster Kubernetes configurato (`~/.kube/config` valido, oppure ServiceAccount se in-cluster)
- Stringa di connessione a MongoDB (con permessi di lettura su `local` per l'oplog e sulle collection target)

---

## 🛠️ Installazione

### 1. Clona il repository

```bash
git clone https://github.com/Miccolomi/mongot-monitor.git
cd mongot-monitor
```

### 2. Crea il virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

> ⚠️ **Importante**: attiva sempre il venv prima di eseguire il monitor. Il prompt diventerà `(venv)`.

### 3. Installa le dipendenze

```bash
pip install -r requirements.txt
```

---

## 🚀 Utilizzo

### Avvio rapido (Mac / PC locale)

Se hai `kubectl` configurato per puntare al tuo cluster, lo script userà automaticamente il Kubeconfig locale.

```bash
source venv/bin/activate

python3 mongot_monitor.py \
  --uri "mongodb://<USER>:<PASSWORD>@<HOST1>:<PORT1>,<HOST2>:<PORT2>/<DB>?replicaSet=<RS>&tls=true&tlsAllowInvalidCertificates=true&authSource=admin" \
  --namespace mongodb \
  --port 5050
```

Apri il browser su: **http://localhost:5050**

### Esempio reale

```bash
python3 mongot_monitor.py \
  --uri "mongodb://mdb-admin:password@work0.mongodb.local:30017,work1.mongodb.local:30018,work2.mongodb.local:30019/admin?replicaSet=my-replica-set&tls=true&tlsAllowInvalidCertificates=true&authSource=admin" \
  --namespace mongodb \
  --port 5051
```

### Avvio In-Cluster (come Pod K8s)

```bash
python3 mongot_monitor.py \
  --uri "mongodb://..." \
  --namespace mongodb \
  --in-cluster
```

> Il ServiceAccount del pod dovrà avere un Role con permessi di lettura su `pods`, `pods/log`, `events`, `services`, `persistentvolumeclaims`, CRD `mongodbsearch` e `deployments`.

### Con Basic Auth (protezione accesso)

```bash
python3 mongot_monitor.py \
  --uri "mongodb://..." \
  --namespace mongodb \
  --auth admin:password_sicura
```

---

## ⚙️ Parametri CLI

| Parametro | Default | Descrizione |
|:---|:---|:---|
| `--uri` | — | Stringa di connessione MongoDB |
| `--port` | `5050` | Porta HTTP della dashboard |
| `--host` | `0.0.0.0` | Indirizzo di binding Flask |
| `--namespace` | tutti | Namespace Kubernetes da monitorare |
| `--in-cluster` | `false` | Autenticazione K8s via ServiceAccount |
| `--interval` | `5` | Intervallo del Background Collector (secondi) |
| `--auth` | — | Attiva Basic Auth. Formato: `user:password` |
| `--allowed-origins` | localhost | Origini CORS permesse (spazio-separate) |

---

## 🧠 Come funziona il SRE Advisor?

Il pannello **Compliance & Best Practices** esegue automaticamente 11 check in Python ad ogni ciclo di raccolta:

| # | Check | Soglie |
|:---|:---|:---|
| 1 | **Spazio Disco (Regola 200%)** | warn se libero < 200% dell'usato; crit se disco ≥ 90% (read-only) |
| 2 | **Consolidamento Indici** | warn se più di un indice dello stesso tipo sulla stessa collection (vectorSearch + fullText sulla stessa collection è valido: Hybrid Search) |
| 3 | **Collo di Bottiglia I/O** | crit se disk queue > 10 e lag > 5s contemporaneamente |
| 4 | **CPU & QPS** | crit se CPU > 80%; warn se QPS > 10 × core |
| 5 | **Memory Starvation (Page Faults)** | warn > 500/s; crit > 1000/s |
| 6 | **OOMKilled & MMap Risk** | crit se heap JVM ≥ 90% del limite pod o se OOMKilled rilevato |
| 7 | **Stato CRD Operator** | crit se la CRD non è in fase `Running` |
| 8 | **Storage Class Performance** | warn se PVC usa `standard`, `hostpath` o `slow` |
| 9 | **Versioning Operator** | warn se l'immagine usa il tag `:latest` |
| 10 | **Oplog Window Predittivo** | warn > 40% consumato; crit > 70% consumato |
| 11 | **Search Auth** (`skipAuthenticationToSearchIndexManagementServer`) | crit se `true` — mongod↔mongot senza autenticazione |
| 12 | **Search TLS Mode** (`searchTLSMode`) | crit se `disabled`; warn se `allowTLS`/`preferTLS`; pass se `requireTLS` |

I finding sono ordinati per severità (crit → warn → pass) e serviti tramite l'endpoint `/api/advisor`.

---

## 🏗️ Struttura del Progetto

```
mongot_monitor.py        # App Factory + CLI entry point
background.py            # BackgroundCollector (thread daemon)
advisor.py               # SRE Advisor engine (9 check, Python puro)
security.py              # Validazione input, security headers, Basic Auth
state.py                 # Shared mutable state (clients, cache, lock)

collectors/
  kubernetes.py          # Discovery K8s (pod, CRD, PVC, services, helm)
  mongodb.py             # Collectors MongoDB (vitals, oplog, indexes)
  prometheus.py          # Prometheus scraper con doppio fallback

routes/
  api.py                 # Blueprint API (/metrics, /healthcheck, /api/advisor, /api/logs)
  frontend.py            # Blueprint frontend (/, /favicon.ico)

frontend/
  templates/
    dashboard.html       # Template Jinja2
  static/
    css/main.css
    js/
      utils.js           # Utility (formatBytes, pill, gaugeRing, …)
      logs.js            # Live log management
      advisor.js         # Renderer thin (logica in advisor.py)
      pipeline.js        # Sync Pipeline Analyzer
      render.js          # Main renderer + polling

tests/
  conftest.py
  test_advisor.py        # 52 test — ogni check SRE
  test_background.py     # 6 test — collector e cache
  test_frontend.py       # 47 test — dashboard, CSS, JS, API
  test_security.py       # 37 test — validazione, headers, auth
```

---

## 🧪 Esecuzione dei Test

```bash
source venv/bin/activate
python3 -m pytest tests/ -v
```

Output atteso: **142 test, tutti verdi**.

---

## 🔌 Endpoint API

| Endpoint | Metodo | Descrizione |
|:---|:---|:---|
| `/` | GET | Dashboard HTML |
| `/metrics` | GET | Snapshot completo JSON (dalla cache) |
| `/api/advisor` | GET | Findings SRE in JSON |
| `/healthcheck` | GET | Stato di salute del monitor |
| `/api/logs/<ns>/<pod>` | GET | Ultimi 50 log del pod |
| `/api/download_logs/<ns>/<pod>` | GET | Download log (parametri `?time=1h&level=error`) |

---

## 📄 Licenza

MIT
