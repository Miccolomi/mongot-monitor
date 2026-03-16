# 🔬 MongoDB Search Diagnostics

Un cruscotto Enterprise avanzato e standalone per il monitoraggio dei nodi di ricerca MongoDB Search (`mongot`) deployati su Kubernetes tramite il MongoDB Kubernetes Operator (CRD `MongoDBSearch`).

Questo tool va oltre le classiche metriche Prometheus: incrocia in tempo reale i dati del database (Oplog, stato degli indici) con lo stato dell'infrastruttura (Kubernetes Events, PVC, CPU Limits, Live Logs) per fornire una vista unificata dello stack di ricerca e un **SRE Advisor automatico** basato su Python.

---

## ✨ Caratteristiche Principali

### 🧠 SRE Advisor (15 check automatici)

Ogni ciclo di raccolta esegue in Python una serie di check sullo stato del cluster e dell'indice. I finding vengono ordinati per severità (crit → warn → pass) e serviti via `/api/advisor`.

| # | Check | Soglie |
|:---|:---|:---|
| 1 | **Spazio Disco (Regola 200%)** | warn se libero < 200% dell'usato; crit se disco ≥ 90% (mongot entra in read-only) |
| 2 | **Consolidamento Indici** | warn se più di un indice dello stesso tipo sulla stessa collection (fullText + vectorSearch sulla stessa collection è valido: Hybrid Search) |
| 3 | **Collo di Bottiglia I/O** | crit se disk queue > 10 e lag > 5s contemporaneamente |
| 4 | **CPU & QPS** | crit se CPU > 80%; warn se QPS > 10 × core |
| 5 | **Memory Starvation (Page Faults)** | warn > 500/s; crit > 1000/s |
| 6 | **OOMKilled & MMap Risk** | crit se heap JVM ≥ 90% del limite pod o se OOMKilled rilevato |
| 7 | **Stato CRD Operator** | crit se la CRD non è in fase `Running` |
| 8 | **Storage Class Performance** | warn se PVC usa `standard`, `hostpath` o `slow` |
| 9 | **Versioning Operator** | warn se l'immagine usa il tag `:latest` |
| 10 | **Oplog Window Predittivo** | warn > 40% consumato; crit > 70% consumato — previene Initial Sync forzati |
| 11 | **Search Auth** | crit se `skipAuthenticationToSearchIndexManagementServer=true` — mongod↔mongot senza autenticazione |
| 12 | **Search TLS Mode** | crit se `searchTLSMode=disabled`; warn se `allowTLS`/`preferTLS`; pass se `requireTLS` |
| 13 | **Search Efficiency (Scan Ratio)** | warn > 50:1; crit > 500:1; warning predittivo se ratio alto + latency bassa (cardinality problem) |
| 14 | **Vector Search Efficiency** | stessi threshold del scan ratio ma calcolato su `$vectorSearch` separatamente |
| 15 | **HNSW Visited Nodes** | warn > 1000 nodi/query; crit > 5000 — early warning saturazione CPU su ANN |

### 📡 Search QPS & Latenza Real-Time

Il pannello **🔎 Search Commands** mostra metriche di throughput calcolate tramite delta tra cicli successivi di Prometheus:

- **`$search QPS`** e **`$vectorSearch QPS`** in evidenza (richieste/secondo)
- **Latenza media** calcolata come `Δsomma_latenza / Δconteggio` — la latenza reale per singola query, non il picco
- **Latenza massima** — picco storico dal counter Prometheus
- **Failure counters** per `$search` e `$vectorSearch`

I valori di QPS si attivano dal secondo ciclo di raccolta (è necessario un delta temporale).

### 🎯 Search Efficiency — Scan Ratio (EMA-smoothed)

`scan_ratio = candidates_examined / results_returned` è il vero indicatore di efficienza di una query search. La latency da sola non basta: una query a 50ms con 200k candidates esaminati diventerà un timeout non appena il dataset cresce.

Sono calcolati **due ratio separati**: uno per `$search` (`mongot_query_candidates_examined_total` con fallback su `mongot_query_documents_scanned`) e uno dedicato per `$vectorSearch` (`mongot_vector_query_candidates_examined_total`).

Per evitare falsi positivi su traffico basso (es. 1 risultato / 500 candidati da una singola query), il ratio è **EMA-smoothed** (α = 0.3) con guard: se `Δresults < 10` l'EMA non viene aggiornata.

| Ratio | Interpretazione |
|:---|:---|
| < 5 | Eccellente — indice molto selettivo |
| 5 – 50 | Normale |
| 50 – 500 | Query inefficiente — indice o analyzer da rivedere |
| > 500 | Critico — indice o query seriamente problematici |

**Cardinality problem detection (predittivo):** se `scan_ratio > 50` ma `latency < 100ms`, l'Advisor emette un warning — l'indice è poco selettivo ma il dataset è ancora abbastanza piccolo da nascondere il costo. Questo segnale non è fornito da Ops Manager.

**Anti-pattern zero results:** se `results_returned = 0` ma `candidates_examined > 0`, viene emesso un warning specifico. Cause tipiche: `$match` post-search troppo restrittivo, scoring threshold troppo alto, pipeline mal progettata.

### 🧬 HNSW Visited Nodes — Early Warning CPU Saturation

`mongot_vector_search_hnsw_visited_nodes` (fallback: `mongot_vector_search_graph_nodes_visited`) misura quanti nodi del grafo HNSW vengono attraversati per ogni query `$vectorSearch`. È un **early warning per la saturazione CPU**: il carico cresce prima ancora che la latency diventi visibile.

| Visited nodes | Interpretazione |
|:---|:---|
| < 200 | Eccellente |
| 200 – 1000 | Normale |
| > 1000 | Query costosa — monitorare CPU |
| > 5000 | ANN inefficiente — saturazione CPU imminente |

Valori alti indicano che l'ANN sta degenerando verso brute-force, tipicamente per `efSearch` troppo alto, scarsa connettività del grafo, o embedding di dimensioni eccessive. Il check è opzionale: viene saltato se la metrica non è esposta dalla versione di mongot installata.

### ⏳ Index Build ETA

Durante un Initial Sync o build massivo di un indice, appare un pannello dedicato **"⚙️ Index Build in Progress"** con:

- **Barra di avanzamento animata** — verde > 75%, arancione < 75%, rosso se stalled
- **Contatore** documenti processati / totali con percentuale
- **Velocità** in docs/sec (calcolata tramite delta tra cicli di raccolta)
- **ETA dinamica** in formato h/m/s oppure warning **"INDEX BUILD STALLED"** se la velocità scende sotto 100 docs/s per almeno 30 secondi

Il pannello è visibile solo quando è attivo un Initial Sync (`initial_sync_in_progress > 0`).

### 🔍 Pod Discovery Robusta (gerarchia a 4 livelli)

La discovery dei pod `mongot` usa una gerarchia resistente a upgrade rolling, scaling e variazioni di naming tra versioni MCK:

1. **Label ufficiale MCK** `app.kubernetes.io/component=search` — il metodo più affidabile
2. **Container name** `mongot` — fallback stabile tra versioni MCK
3. **Container image** — contiene `mongodb-enterprise-search` o `mongot`
4. **Nome pod (ultima spiaggia)** — euristica, esclude `mongod` e `monitor`

Il pod del monitor stesso viene sempre escluso tramite `app: mongot-monitor`.

### 🌊 Atlas Search Sync Pipeline Analyzer

Visualizza in tempo reale l'intero flusso dati `DB → Change Stream → RAM → Lucene`, calcolando il lag effettivo tra MongoDB e mongot e identificando il collo di bottiglia nella pipeline di indicizzazione.

### ⏱️ SRE Predittivo — Oplog Window

Monitora la finestra dell'Oplog e la confronta con il lag corrente di mongot. Se il lag supera il 40% o il 70% della finestra disponibile, emette rispettivamente un warn o un crit per prevenire Initial Sync forzati catastrofici prima che accadano.

### 🩺 Diagnostica K8s Universale

Auto-scopre installazioni Helm, traccia le versioni di Kubernetes e dell'Operator MCK, mappa dinamicamente PVC, Servizi e Pod. Rileva OOMKilled, eventi K8s recenti e log live direttamente nella dashboard.

### 📜 Log Management & Export

Terminale live integrato per visualizzare i log di `mongot` e dell'Operator in streaming. Download completo degli archivi di log filtrabili per finestra temporale (`?time=1h`) e severità (`?level=error`).

### 📊 Prometheus Doppio Fallback

Scraping delle metriche dai pod tramite accesso di rete diretto (HTTP) con fallback automatico sul tunnel K8s API Server Proxy — nessuna configurazione aggiuntiva richiesta.

### ⚡ Background Collector & Rate Engine

La raccolta dati avviene su un thread daemon separato a intervallo configurabile. L'endpoint `/metrics` risponde sempre in < 1ms dalla cache in memoria — la dashboard non blocca mai su chiamate esterne.

Tutta la logica di calcolo delta/rate è isolata nel modulo `engine/rate_calculator.py`, separato dal loop di raccolta. Questo significa:

- **`background.py`** è un thin orchestrator: scrape → `compute_pod_rates()` → aggiornamento cache
- **`engine/rate_calculator.py`** contiene QPS, latenza media, scan ratio EMA, HNSW, ETA — testabile indipendentemente
- **Counter reset safety**: `_safe_delta()` restituisce `None` su delta negativo (reset contatori dopo restart del pod mongot); spike guard scarta QPS > 50.000/s (counter reset dove il nuovo valore è > snapshot precedente); primo ciclo (`last_s=None`) salta tutto silenziosamente — nessun falso spike all'avvio

### 🔌 API Stabile (`/api/v1/search_metrics`)

Endpoint JSON versionato con schema fisso, disaccoppiato dai nomi interni delle metriche Prometheus:

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

Sicuro per consumer esterni (CI perf gate, dashboard Grafana, tool di alerting) — il backend può evolvere senza rompere il contratto API.

### 🔒 Sicurezza

HTTP Basic Auth opzionale, security headers (CSP, X-Frame-Options, X-Content-Type-Options), validazione degli input K8s names contro injection, CORS configurabile via CLI.

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

Il pannello **Compliance & Best Practices** esegue automaticamente 15 check in Python ad ogni ciclo di raccolta:

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
background.py            # BackgroundCollector (thin orchestrator, thread daemon)
advisor.py               # SRE Advisor engine (15 check, Python puro)
security.py              # Validazione input, security headers, Basic Auth
state.py                 # Shared mutable state (clients, cache, lock)

engine/
  rate_calculator.py     # Delta/rate engine: QPS, latenza, scan ratio EMA, HNSW, ETA
                         # Counter reset safety, spike guard, first-cycle protection

collectors/
  kubernetes.py          # Discovery K8s (pod, CRD, PVC, services, helm)
  mongodb.py             # Collectors MongoDB (vitals, oplog, indexes)
  prometheus.py          # Prometheus scraper con doppio fallback

routes/
  api.py                 # Blueprint API (/metrics, /api/v1/search_metrics, /api/advisor, /api/logs)
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

## 🐳 Deploy Containerizzato su Kubernetes

### 1. Build dell'immagine Docker

```bash
docker build -t mongot-monitor:latest .
```

Se usi un registry privato (es. Docker Hub, ECR, GCR):

```bash
docker build -t <tuo-registry>/mongot-monitor:1.0.0 .
docker push <tuo-registry>/mongot-monitor:1.0.0
```

Aggiorna `image:` in `k8s/deployment.yaml` con il tag corretto.

### 2. Configura la URI MongoDB

La connessione a **mongod** è necessaria per i check di oplog, indici e compliance (skipAuth, TLS mode).
**mongot** viene scoperto automaticamente tramite Kubernetes — non serve nessuna URI per esso.

Edita `k8s/secret.yaml` in base a dove si trova il tuo mongod:

#### Scenario A — mongod dentro il cluster (installato con MCK)

Usa il nome DNS interno del Service headless del replica set:

```bash
# Trova il nome del Service
kubectl get svc -n <namespace>
# Cerca il Service di tipo ClusterIP con porta 27017 (es. my-replica-set-svc)
```

```yaml
stringData:
  MONGODB_URI: "mongodb://USER:PASSWORD@<replica-set-name>-svc.<namespace>.svc.cluster.local/admin?replicaSet=<RS-name>&tls=true&tlsAllowInvalidCertificates=true&authSource=admin&authMechanism=SCRAM-SHA-256"
```

#### Scenario B — mongod fuori dal cluster (Atlas, on-prem, VM esterna)

Usa la stringa di connessione esterna fornita dal tuo deployment MongoDB:

```yaml
# Atlas (SRV):
stringData:
  MONGODB_URI: "mongodb+srv://USER:PASSWORD@cluster0.xxxxx.mongodb.net/admin?authSource=admin&authMechanism=SCRAM-SHA-256"

# Replica set con hostname DNS-risolvibili:
stringData:
  MONGODB_URI: "mongodb://USER:PASSWORD@host1:27017,host2:27017,host3:27017/admin?replicaSet=RS&tls=true&authSource=admin&authMechanism=SCRAM-SHA-256"
```

> `authMechanism=SCRAM-SHA-256` è richiesto da MongoDB 7+ con MCK. Rimuovilo solo se usi MongoDB ≤ 6 con autenticazione SCRAM-SHA-1.

### 3. Applica i manifest in ordine

```bash
# 1. RBAC (ServiceAccount + ClusterRole + Binding)
kubectl apply -f k8s/rbac.yaml

# 2. Secret MongoDB URI
kubectl apply -f k8s/secret.yaml

# 3. Deployment
kubectl apply -f k8s/deployment.yaml

# 4. Service (NodePort)
kubectl apply -f k8s/service.yaml
```

### 4. Accedi alla dashboard

```bash
# Trova il NodePort assegnato
kubectl get svc mongot-monitor -n mongodb
# Esempio output: 5050:31855/TCP  →  NodePort = 31855
```

**Docker Desktop**: il nodo è `localhost`, quindi:
```
http://localhost:<NODE_PORT>
```

**Cluster remoto** (GKE, EKS, on-prem): usa l'IP di uno dei nodi worker:
```bash
kubectl get nodes -o wide   # colonna INTERNAL-IP o EXTERNAL-IP
http://<NODE_IP>:<NODE_PORT>
```

### Note per ambienti di sviluppo locale (Docker Desktop)

Se stai testando su Docker Desktop con MongoDB già installato nel cluster via MCK, il Service DNS interno (`my-replica-set-svc.mongodb.svc.cluster.local`) è direttamente raggiungibile dal pod — nessuna configurazione aggiuntiva necessaria. Evita di usare hostname definiti in `/etc/hosts` sul Mac (es. `work0.mongodb.local`): non sono risolvibili dall'interno del pod.

### Struttura manifest

| File | Descrizione |
|:---|:---|
| `k8s/rbac.yaml` | ServiceAccount + ClusterRole con permessi minimi |
| `k8s/secret.yaml` | MongoDB URI come K8s Secret |
| `k8s/deployment.yaml` | Deployment con probe liveness (`/healthz`) e readiness (`/healthz`) |
| `k8s/service.yaml` | NodePort per esporre la dashboard |

> **Namespace**: tutti i manifest usano `mongodb` come namespace di default. Modifica il campo `namespace:` in tutti e 4 i file se il tuo namespace è diverso.

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

