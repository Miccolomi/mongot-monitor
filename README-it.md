> 🇬🇧 **English documentation available:** [README.md](README.md)

# 🔬 MongoDB Search Diagnostics

**mongot-monitor è uno strumento open-source di diagnostica per i nodi MongoDB Atlas Search.**
Rileva automaticamente problemi di performance, lag di indicizzazione e anomalie di configurazione.

Progettato per **SRE**, **operatori MongoDB** e **platform engineer** che gestiscono Atlas Search su Kubernetes.

![Dashboard Screenshot](dashboard.png)

---

## Cosa fa?

- **Rileva** nodi search bloccati, lag di indicizzazione, OOMKilled e configuration drift
- **Analizza** in real-time l'efficienza delle query search, scan ratio e traversal del grafo HNSW
- **Avvisa** prima che i problemi diventino outage — oplog window predittivo, cardinality warning, stall detection
- **SRE Advisor integrato** esegue 15 check automatici a ogni ciclo e ordina i risultati per severità

Nessun agent da installare. Nessuna infrastruttura aggiuntiva. Punta il tool al tuo cluster e vai.

---

## 📋 Indice

- [✨ Caratteristiche Principali](#-caratteristiche-principali)
- [🚀 Installazione e Avvio](#-installazione-e-avvio)
  - [Modalità 1 — Locale](#modalità-1--locale-mac--pc)
  - [Modalità 2 — Kubernetes](#modalità-2--kubernetes-in-cluster)
- [🔌 Endpoint API](#-endpoint-api)
- [🏗️ Struttura del Progetto](#️-struttura-del-progetto)
- [🧪 Esecuzione dei Test](#-esecuzione-dei-test)
- [🔬 SRE Advisor — Approfondimento](#-sre-advisor--approfondimento)

---

## ✨ Caratteristiche Principali

- 🧠 **SRE Advisor** — 15 check automatici, ordinati per severità (crit → warn → pass), serviti via `/api/advisor` — [vedi approfondimento](#-sre-advisor--approfondimento)
- 📡 **Search QPS & Latenza Real-Time** — calcolo delta tra cicli Prometheus, separato per `$search` e `$vectorSearch`
- 🎯 **Search Efficiency (Scan Ratio)** — EMA-smoothed `candidates_examined / results_returned`, ratio separati per text e vector search, con cardinality detection
- 🧬 **HNSW Visited Nodes** — early warning per saturazione CPU su ANN, prima che la latenza diventi visibile
- ⏳ **Index Build ETA** — barra di avanzamento animata, docs/sec, rilevamento stall, ETA dinamica
- 🔍 **Pod Discovery Robusta** — gerarchia a 4 livelli, resistente a upgrade MCK e variazioni di naming
- 🌊 **Sync Pipeline Analyzer** — visualizzazione real-time del flusso `DB → Change Stream → RAM → Lucene` con identificazione del collo di bottiglia
- ⏱️ **Oplog Window Predittivo** — warn al 40%, crit al 70% della finestra consumata per prevenire Initial Sync forzati
- 🩺 **Diagnostica K8s Universale** — installazioni Helm, versioni MCK/K8s, PVC, OOMKilled, log live in streaming
- 📜 **Log Management & Export** — terminale live, download filtrato per finestra temporale e severità
- ⚡ **Background Collector & Rate Engine** — thread daemon, risposta API < 1ms dalla cache in memoria, sicuro ai counter reset
- 🔌 **API Stabile Versionata** — `/api/v1/search_metrics` con schema fisso, sicura per consumer esterni
- 🔒 **Sicurezza** — Basic Auth opzionale, CSP headers, validazione input K8s, CORS configurabile

---

## 🚀 Installazione e Avvio

> **Prerequisiti**: `kubectl` configurato e puntato al tuo cluster. Stringa di connessione MongoDB con accesso in lettura su `local` (oplog) e sulle collection target.

---

### Modalità 1 — Locale (Mac / PC)

Usa questa modalità per sviluppo, demo o quando preferisci girare il monitor fuori dal cluster.

**1. Clona e installa**

```bash
git clone https://github.com/Miccolomi/mongot-monitor.git
cd mongot-monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**2. Avvia**

```bash
python3 mongot_monitor.py \
  --uri "mongodb://USER:PASSWORD@HOST:PORT/admin?replicaSet=RS&authSource=admin&authMechanism=SCRAM-SHA-256" \
  --namespace mongodb \
  --port 5050
```

Apri il browser su: **http://localhost:5050**

**Opzioni comuni**

| Parametro | Default | Descrizione |
|:---|:---|:---|
| `--uri` | — | Stringa di connessione MongoDB |
| `--namespace` | tutti | Namespace Kubernetes da monitorare |
| `--port` | `5050` | Porta HTTP della dashboard |
| `--interval` | `5` | Intervallo di raccolta in secondi |
| `--auth` | — | Basic Auth — formato `user:password` |
| `--in-cluster` | `false` | Auth K8s via ServiceAccount (solo in-cluster) |
| `--host` | `0.0.0.0` | Indirizzo di binding Flask |
| `--allowed-origins` | localhost | Origini CORS permesse (spazio-separate) |

---

### Modalità 2 — Kubernetes (in-cluster)

Usa questa modalità per un deployment permanente nel cluster. Il monitor gira come pod e usa un ServiceAccount con RBAC per accedere all'API Kubernetes.

**1. Build dell'immagine Docker**

```bash
docker build -t mongot-monitor:latest .
```

Per un registry privato (Docker Hub, ECR, GCR):

```bash
docker build -t <tuo-registry>/mongot-monitor:1.0.0 .
docker push <tuo-registry>/mongot-monitor:1.0.0
```

Aggiorna `image:` in `k8s/deployment.yaml` con il tag corretto.

> ⚠️ **Importante**: dopo ogni aggiornamento del codice, rifai il build e riavvia il deployment:
> ```bash
> docker build -t mongot-monitor:latest .
> kubectl rollout restart deployment/mongot-monitor -n mongodb
> ```

**2. Configura la URI MongoDB**

La connessione a **mongod** è necessaria per oplog, indici e check di compliance.
**mongot** è sempre scoperto automaticamente via Kubernetes — nessuna URI necessaria per esso.

Edita `k8s/secret.yaml` in base a dove si trova il tuo mongod:

```bash
# Scenario A — mongod dentro il cluster (MCK): usa il DNS interno del Service
kubectl get svc -n mongodb   # cerca il ClusterIP su porta 27017
```

```yaml
# Scenario A — in-cluster (MCK)
stringData:
  MONGODB_URI: "mongodb://USER:PASSWORD@<rs-name>-svc.<namespace>.svc.cluster.local/admin?replicaSet=<RS>&tls=true&tlsAllowInvalidCertificates=true&authSource=admin&authMechanism=SCRAM-SHA-256"

# Scenario B — Atlas (SRV)
# MONGODB_URI: "mongodb+srv://USER:PASSWORD@cluster0.xxxxx.mongodb.net/admin?authSource=admin&authMechanism=SCRAM-SHA-256"

# Scenario C — Replica set esterno con hostname DNS
# MONGODB_URI: "mongodb://USER:PASSWORD@host1:27017,host2:27017/admin?replicaSet=RS&tls=true&authSource=admin&authMechanism=SCRAM-SHA-256"
```

> `authMechanism=SCRAM-SHA-256` è richiesto da MongoDB 7+ con MCK.

**3. Applica i manifest**

```bash
kubectl apply -f k8s/rbac.yaml        # ServiceAccount + ClusterRole
kubectl apply -f k8s/secret.yaml      # URI MongoDB
kubectl apply -f k8s/deployment.yaml  # Deployment
kubectl apply -f k8s/service.yaml     # NodePort
```

| File | Descrizione |
|:---|:---|
| `k8s/rbac.yaml` | ServiceAccount + ClusterRole con permessi minimi (incluso `pods/proxy`) |
| `k8s/secret.yaml` | MongoDB URI come K8s Secret |
| `k8s/deployment.yaml` | Deployment con probe liveness e readiness su `/healthz` |
| `k8s/service.yaml` | NodePort per esporre la dashboard |

> **Namespace**: tutti i manifest usano `mongodb` come default. Modifica `namespace:` in tutti e 4 i file se il tuo è diverso.

**4. Accedi alla dashboard**

```bash
kubectl get svc mongot-monitor -n mongodb
# Esempio: 5050:31855/TCP  →  NodePort = 31855
```

- **Docker Desktop**: `http://localhost:<NODE_PORT>`
- **Cluster remoto** (GKE, EKS, on-prem): `http://<NODE_IP>:<NODE_PORT>` (vedi `kubectl get nodes -o wide`)

> Su Docker Desktop con MCK, il DNS interno (`<rs>-svc.mongodb.svc.cluster.local`) è raggiungibile direttamente dal pod. Non usare hostname da `/etc/hosts` del Mac — non sono risolvibili dall'interno del cluster.

---

## 🔌 Endpoint API

| Endpoint | Descrizione |
|:---|:---|
| `/` | Dashboard HTML |
| `/metrics` | Snapshot completo JSON (dalla cache) |
| `/api/v1/search_metrics` | API stabile versionata — schema fisso per consumer esterni |
| `/api/advisor` | Findings SRE in JSON (crit → warn → pass) |
| `/healthz` | Liveness probe — risponde sempre 200 se Flask è attivo |
| `/healthcheck` | Stato dettagliato (MongoDB ping, K8s API, età cache) |
| `/api/logs/<ns>/<pod>` | Ultimi 50 log del pod |
| `/api/download_logs/<ns>/<pod>` | Download log (`?time=1h&level=error`) |

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
      advisor.js         # Advisor renderer
      pipeline.js        # Sync Pipeline Analyzer
      render.js          # Main renderer + polling

tests/
  conftest.py
  test_advisor.py        # test — ogni check SRE
  test_background.py     # test — collector e cache
  test_frontend.py       # test — dashboard, CSS, JS, API
  test_security.py       # test — validazione, headers, auth
```

---

## 🧪 Esecuzione dei Test

```bash
source venv/bin/activate
python3 -m pytest tests/ -v
```

---

## 🔬 SRE Advisor — Approfondimento

Ogni ciclo di raccolta esegue in Python una serie di check sullo stato del cluster e dell'indice. I finding vengono ordinati per severità (crit → warn → pass) e serviti via `/api/advisor`.

### Tabella dei check

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

### ⚡ Background Collector & Rate Engine

La raccolta dati avviene su un thread daemon separato a intervallo configurabile. L'endpoint `/metrics` risponde sempre in < 1ms dalla cache in memoria — la dashboard non blocca mai su chiamate esterne.

Tutta la logica di calcolo delta/rate è isolata nel modulo `engine/rate_calculator.py`, separato dal loop di raccolta:

- **`background.py`** è un thin orchestrator: scrape → `compute_pod_rates()` → aggiornamento cache
- **`engine/rate_calculator.py`** contiene QPS, latenza media, scan ratio EMA, HNSW, ETA — testabile indipendentemente
- **Counter reset safety**: `_safe_delta()` restituisce `None` su delta negativo (reset contatori dopo restart del pod mongot); spike guard scarta QPS > 50.000/s; primo ciclo (`last_s=None`) salta tutto silenziosamente — nessun falso spike all'avvio

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
