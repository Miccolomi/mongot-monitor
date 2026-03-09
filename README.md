# 🚀 MONGOT Ultimate Monitor

Un cruscotto Enterprise avanzato e standalone per il monitoraggio dei nodi di ricerca MongoDB Search (`mongot`) deployati su Kubernetes tramite il MongoDB Kubernetes Operator (CRD `MongoDBSearch`).

Questo tool nasce per andare oltre le classiche metriche Prometheus. Incrocia in tempo reale i dati del database (Oplog, stato degli indici) con lo stato dell'infrastruttura (Kubernetes Events, PVC, CPU Limits, Live Logs) per fornire una vista unificata dello stack di ricerca e un vero e proprio **SRE Advisor automatico**.

![MONGOT Ultimate Monitor](https://github.com/Miccolomi/mongot-monitor/blob/main/Screenshot.png) 

## ✨ Caratteristiche Principali

- 🧠 **SRE Advisor Integrato**: Analizza la configurazione in tempo reale e segnala violazioni delle Best Practice (es. Spazio disco insufficiente, colli di bottiglia I/O, CPU sottodimensionata, MMap OOMKilled Risk).
- 🌊 **Atlas Search Sync Pipeline Analyzer**: Disegna e monitora in tempo reale l'intero flusso dati (`DB ➔ Change Stream ➔ RAM ➔ Lucene`), calcolando l'effettivo Lag temporale tra i due sistemi.
- ⏱️ **SRE Predittivo (Oplog Window)**: Monitora costantemente la finestra dell'Oplog di MongoDB per individuare in ritardi critici la Replication di `mongot` e sventare in tempo `Initial Sync` letali prima che accadano.
- 🩺 **Diagnostica K8s Universale**: Auto-scopre le installazioni Helm Chart relative a MongoDB, verifica le versioni in uso di Kubernetes e dell'Operator MCK, e mappa dinamicamente PVC, Servizi e Pods.
- 📜 **Log Management & Export**: Terminale Live integrato nella UI per visualizzare i log in tempo reale di mongot e dell'Operator, con la possibilità di scaricare gli archivi completi formattati testuali usando filtri temporali e di severità (es. solo errori).
- 🚨 **Global Error Handling**: Un sistema proattivo che intercetta e mostra chiaramente sulla UI ogni fallimento di permessi K8s (RBAC), timeout di rete o errori di autenticazione MongoDB.
- 📊 **Prometheus Triplo Fallback**: Scarica le metriche dai pod bypassando i limiti di rete usando tunnel veloci via API Server K8s (Proxy o Exec `wget`).
- 🔎 **Monitoraggio Indici Intelligente**: Rileva sia indici `$search` che `$vectorSearch`, contando documenti in tempo reale aggirando incastri e bug noti di MongoDB.

## 📋 Requisiti

- **Python 3.8+**
- Accesso al cluster Kubernetes configurato (`~/.kube/config` valido o ServiceAccount se in-cluster)
- Stringa di connessione a MongoDB (con permessi di lettura sul DB `local` per l'oplog e per leggere le collection).

## 🛠️ Installazione

1. Clona il repository:
   ```bash
   git clone https://github.com/Miccolomi/mongot-monitor.git
   cd mongot-monitor
   ```

2. Crea un virtual environment (consigliato):
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. Installa le dipendenze:
   ```bash
   pip install -r requirements.txt
   ```

## 🚀 Utilizzo

Il monitor è un'applicazione Flask zero-configuration lato frontend (HTML/JS/CSS sono serviti direttamente dal backend).

### 1. Esecuzione Standalone (dal tuo Mac / PC Locale)

Se hai `kubectl` già configurato per puntare al tuo cluster, lo script userà automaticamente il tuo Kubeconfig locale.

```bash
python mongot_monitor.py \
  --uri "mongodb://<USER>:<PASSWORD>@<HOSTS>/?replicaSet=<RS>&tls=true&authSource=admin" \
  --namespace mongodb
```

*Sostituisci `--namespace mongodb` con il namespace K8s dove risiedono i tuoi pod `mongot`.*

Apri il browser all'indirizzo: **http://localhost:5050**

### 2. Esecuzione In-Cluster (come Pod K8s)

Se vuoi deployare questo monitor stabilmente all'interno del tuo cluster Kubernetes, usa il flag `--in-cluster`. In questo modo lo script utilizzerà il ServiceAccount del pod per interrogare le API di K8s.

```bash
python mongot_monitor.py \
  --uri "mongodb://..." \
  --namespace mongodb \
  --in-cluster
```

*(Nota: Il ServiceAccount associato al pod dovrà avere un Role/ClusterRole con permessi di lettura su `pods`, `pods/log`, `pods/exec`, `events`, `services`, `persistentvolumeclaims` e sui CRD `mongodbsearch` e `deployments`).*

## ⚙️ Parametri CLI

| Parametro | Descrizione | Default |
| :--- | :--- | :--- |
| `--uri` | Stringa di connessione a MongoDB. | `None` (Solo metriche K8s) |
| `--port` | Porta su cui esporre la dashboard web. | `5050` |
| `--host` | Interfaccia di binding per Flask. | `0.0.0.0` |
| `--namespace` | Namespace Kubernetes da scansionare. | Auto-discover su tutti |
| `--in-cluster` | Attiva l'autenticazione K8s via ServiceAccount. | `False` |

## 🧠 Come funziona il SRE Advisor?

Il pannello **Compliance & Best Practices** calcola automaticamente i seguenti indicatori:

* **Spazio Disco (Regola del 125%)**: Verifica che ci sia abbastanza spazio libero (1.25x dell'usato) per permettere a Lucene di fare il rebuild degli indici in background.
* **Consolidamento Indici**: Avvisa se ci sono troppi indici frammentati sulla stessa collection (anti-pattern).
* **Collo di Bottiglia I/O**: Incrocia la *Disk Queue Length* con l'*Oplog Lag* per capire se i dischi Kubernetes (PVC) stanno soffocando l'indicizzazione.
* **Rapporto CPU/QPS**: Verifica che ci sia almeno 1 Core allocato ogni 10 Queries Per Second in base al traffico rilevato dal profiler di MongoDB.
