# 🔬 MongoDB Search Diagnostics

Un cruscotto Enterprise avanzato e standalone per il monitoraggio dei nodi di ricerca MongoDB Search (`mongot`) deployati su Kubernetes tramite il MongoDB Kubernetes Operator (CRD `MongoDBSearch`).

Questo tool va oltre le classiche metriche Prometheus: incrocia in tempo reale i dati del database (Oplog, stato degli indici) con lo stato dell'infrastruttura (Kubernetes Events, PVC, CPU Limits, Live Logs) per fornire una vista unificata dello stack di ricerca e un **SRE Advisor automatico** basato su Python.

---

## 🆕 Ultimi Aggiornamenti

### Milestone 1 — Discovery robusta dei pod mongot
La discovery dei pod `mongot` ora usa una gerarchia a 4 livelli, resistente a upgrade rolling, scaling e variazioni di naming:

1. **Label ufficiale MCK** `app.kubernetes.io/component=search` — il metodo più affidabile
2. **Container name** `mongot` — fallback stabile tra versioni MCK
3. **Container image** — contiene `mongodb-enterprise-search` o `mongot`
4. **Nome pod (ultima spiaggia)** — euristica su `mongot` nel nome, esclude `mongod` e `monitor`

Il pod del monitor stesso viene sempre escluso via label `app: mongot-monitor`.

### Milestone 2 — Index Build ETA in tempo reale
Durante un Initial Sync o build massivo di un indice, la dashboard mostra un pannello dedicato **"⚙️ Index Build in Progress"** con:

- **Barra di avanzamento animata** (colore: verde > 75%, arancione < 75%, rosso se stalled)
- **Contatore documenti** processati / totali con percentuale
- **Velocità** in docs/sec (calcolata tramite delta tra cicli di raccolta)
- **ETA dinamica** (`fEta()` — formato h/m/s) oppure warning **"INDEX BUILD STALLED"** se la velocità scende sotto 100 docs/s per almeno 30 secondi

Il pannello è visibile solo quando è attivo un Initial Sync (`initial_sync_in_progress > 0`).

### Milestone 5 — Vector Search: HNSW Visited Nodes + EMA Scan Ratio

**EMA e guard sul traffico basso (anti-rumore)**

Il scan ratio grezzo è rumoroso con traffico basso: `1 risultato / 500 candidati` da una singola query genera un ratio di 500 che è un falso positivo. Soluzioni implementate:

- **Guard `Δresults < 10`**: se il delta di risultati nell'intervallo è inferiore a 10, l'EMA non viene aggiornata — il valore precedente viene mantenuto
- **EMA (Exponential Moving Average)** con α = 0.3: `ema = 0.3 × ratio_corrente + 0.7 × ema_precedente`. Questo smorza i picchi isolati e riflette la tendenza sostenuta nel tempo

**Vector Scan Ratio separato**

Oltre al ratio per `$search`, viene calcolato un ratio dedicato per `$vectorSearch` tramite:

- `mongot_vector_query_candidates_examined_total`
- `mongot_vector_query_results_returned_total`

Il `vector_scan_ratio` è particolarmente rilevante per individuare degrado ANN (Approximate Nearest Neighbor) causato da `efSearch` troppo alto, scarsa connettività del grafo HNSW, o embedding di dimensioni eccessive.

**HNSW Visited Nodes — la metrica più sottovalutata**

`mongot_vector_search_hnsw_visited_nodes` (fallback: `mongot_vector_search_graph_nodes_visited`) misura quanti nodi del grafo HNSW vengono attraversati per ogni query vectorSearch.

| Visited nodes | Interpretazione |
|:---|:---|
| < 200 | Eccellente |
| 200 – 1000 | Normale |
| > 1000 | Query costosa |
| > 5000 | ANN inefficiente — rischio saturazione CPU |

Questa metrica è un **early warning per la saturazione CPU**: il carico aumenta prima ancora che la latency sia visibile. È usata internamente da MongoDB per diagnosticare problemi nei cluster con embedding search di grandi dimensioni.

**Cardinality problem detection (predittivo)**

Se `scan_ratio > 50` ma `latency < 100ms`, l'SRE Advisor emette un warning predittivo:
> "High scan ratio but low latency — index is non-selective, may degrade as dataset grows"

Questo è un segnale che Ops Manager non fornisce.

---

### Milestone 4 — Search Efficiency: Scan Ratio (mongot_query_candidates_examined)

La metrica `mongot_query_candidates_examined` (o `mongot_query_documents_scanned` nelle versioni più recenti di mongot) misura quanti documenti l'indice deve esaminare prima di produrre il risultato finale.

Il rapporto:

```
scan_ratio = candidates_examined / results_returned
```

è il vero indicatore di efficienza di una query search. La latency da sola non basta: una query a 50ms con `candidates_examined = 200k` diventerà un timeout non appena il dataset cresce.

**Interpretazione del ratio:**

| Ratio | Interpretazione |
|:---|:---|
| < 5 | Eccellente — indice molto selettivo |
| 5 – 50 | Normale |
| 50 – 500 | Query inefficiente — indice o analyzer da rivedere |
| > 500 | Critico — indice o query seriamente problematici |

**Anti-pattern rilevato automaticamente:** se `results_returned = 0` ma `candidates_examined > 0`, la dashboard mostra un warning specifico. Cause tipiche: filtro `$match` post-search troppo restrittivo, scoring threshold troppo alto, pipeline mal progettata.

**Come funziona nel sistema:**
- Il collector legge il counter cumulativo da Prometheus (con fallback automatico tra i due nomi di metrica)
- Il Background Collector calcola `scan_ratio` tramite delta tra cicli successivi, esattamente come per QPS
- Il pannello "🔎 Search Commands" mostra la sezione **"Index Efficiency"** con ratio colorato e label testuale
- Il **SRE Advisor** aggiunge automaticamente un finding (pass/warn/crit) — il check si attiva solo se la metrica è esposta dalla versione di mongot installata

**Correlazione con latenza:** la vera potenza è nella combinazione:
- Latency alta + scan ratio basso → problema CPU / IO
- Latency alta + scan ratio alto → problema indice o query

---

### Milestone 3 — Search Query Rate e Latenza in tempo reale
Il pannello **"🔎 Search Commands"** ora mostra metriche di throughput computate tramite delta tra cicli successivi di Prometheus:

- **`$search QPS`** e **`$vectorSearch QPS`** — richieste al secondo, mostrate in evidenza
- **Latenza media** (`avg`) — calcolata come `Δsomma_latenza / Δconteggio` (latenza reale per query)
- **Latenza massima** (`max`) — picco storico dal counter Prometheus
- **Failure counters** per `$search` e `$vectorSearch`

I dati di QPS si attivano al secondo ciclo di raccolta (serve un delta temporale). Prima di allora i valori mostrano `0.00 /s` in grigio.

---

## ✨ Caratteristiche Principali

- 🧠 **SRE Advisor Backend**: 12 check automatici sulle Best Practice MongoDB Search (spazio disco 200%, consolidamento indici, I/O, CPU/QPS, OOMKilled, CRD status, storage class, versioning, finestra oplog predittiva, autenticazione mongod↔mongot, TLS mode). La logica è in Python, completamente testabile.
- 📡 **Search QPS & Latenza Real-Time**: Throughput (`$search`, `$vectorSearch`) e latenza media/massima calcolati in tempo reale dal Background Collector tramite delta di counter Prometheus.
- 🎯 **Search Efficiency (Scan Ratio EMA)**: Calcola in tempo reale il rapporto `candidates_examined / results_returned` (EMA-smoothed, guard anti-rumore su traffico basso) — il vero indicatore di efficienza dell'indice. Ratio separato per `$search` e `$vectorSearch`. Rileva automaticamente l'anti-pattern "zero results con candidates esaminati" e il "cardinality problem" predittivo (ratio alto + latency bassa).
- 🧬 **HNSW Visited Nodes**: Early warning per saturazione CPU su vectorSearch — misura il numero di nodi attraversati nel grafo HNSW per query. Individua il degrado ANN verso brute-force prima che la latency sia visibile.
- ⏳ **Index Build ETA**: Pannello live durante initial sync con barra di avanzamento animata, docs/s e countdown ETA. Rileva automaticamente uno stallo del build.
- 🔍 **Pod Discovery Robusta**: Gerarchia a 4 livelli (label MCK → container name → image → nome pod) per scoperta affidabile in ogni scenario MCK.
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

