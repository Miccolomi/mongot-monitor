# 🚀 MONGOT Ultimate Monitor

Un cruscotto Enterprise avanzato e standalone per il monitoraggio dei nodi di ricerca MongoDB Search (`mongot`) deployati su Kubernetes tramite il MongoDB Kubernetes Operator (CRD `MongoDBSearch`).

Questo tool nasce per andare oltre le classiche metriche Prometheus. Incrocia in tempo reale i dati del database (Oplog, stato degli indici) con lo stato dell'infrastruttura (Kubernetes Events, PVC, CPU Limits, Live Logs) per fornire una vista unificata dello stack di ricerca e un vero e proprio **SRE Advisor automatico**.

![MONGOT Ultimate Monitor](https://raw.githubusercontent.com/Miccolomi/mongot-monitor/main/screenshot.png) *(Aggiungi uno screenshot della dashboard nel repo e aggiorna questo link!)*

## ✨ Caratteristiche Principali

- 🧠 **SRE Advisor Integrato**: Analizza la configurazione in tempo reale e segnala violazioni delle Best Practice ufficiali MongoDB (es. Spazio disco insufficiente per i rebuild, colli di bottiglia I/O, CPU sottodimensionata per i QPS, indici non consolidati).
- 📊 **Prometheus Triplo Fallback**: Scarica le metriche Prometheus dei pod `mongot`. Se eseguito da fuori dal cluster (es. dal tuo Mac), bypassa i limiti di rete usando un tunnel dinamico tramite le API K8s (Proxy o Exec `wget`).
- 🔎 **Monitoraggio Indici Intelligente**: Rileva sia indici Full-Text (`$search`) che Vector Search (`$vectorSearch`), aggirando i limiti delle API native di MongoDB per contare i documenti indicizzati in tempo reale e correggendo gli stati "fantasma" del database.
- ⏱️ **Global Oplog Tracking**: Monitora la "Oplog Head" del cluster MongoDB per capire immediatamente se i nodi di ricerca stanno accumulando *Replication Lag*.
- 📜 **Live Logs Persistenti**: Un terminale integrato nella UI per visualizzare in tempo reale i log dei singoli pod Kubernetes senza usare la riga di comando.
- 🚨 **Rilevamento OOMKilled & K8s Warnings**: Mostra immediatamente se un pod è andato in *Out Of Memory* o se ci sono eventi di Warning a livello di scheduling Kubernetes.

## 📋 Requisiti

- **Python 3.8+**
- Accesso al cluster Kubernetes configurato (`~/.kube/config` valido o ServiceAccount se in-cluster)
- Stringa di connessione a MongoDB (con permessi di lettura sul DB `local` per l'oplog e per leggere le collection).

## 🛠️ Installazione

1. Clona il repository:
   ```bash
   git clone [https://github.com/Miccolomi/mongot-monitor.git](https://github.com/Miccolomi/mongot-monitor.git)
   cd mongot-monitor
Crea un virtual environment (consigliato):Bashpython3 -m venv venv
source venv/bin/activate
Installa le dipendenze:Bashpip install pymongo flask flask-cors kubernetes requests
(Puoi anche salvare queste dipendenze in un file requirements.txt ed eseguire pip install -r requirements.txt)🚀 UtilizzoIl monitor è un'applicazione Flask zero-configuration lato frontend (HTML/JS/CSS sono serviti direttamente dal backend).1. Esecuzione Standalone (dal tuo Mac / PC Locale)Se hai kubectl già configurato per puntare al tuo cluster, lo script userà automaticamente il tuo Kubeconfig locale.Bashpython mongot_monitor.py \
  --uri "mongodb://<USER>:<PASSWORD>@<HOSTS>/?replicaSet=<RS>&tls=true&authSource=admin" \
  --namespace mongodb
Sostituisci --namespace mongodb con il namespace K8s dove risiedono i tuoi pod mongot.Apri il browser all'indirizzo: http://localhost:50502. Esecuzione In-Cluster (come Pod K8s)Se vuoi deployare questo monitor stabilmente all'interno del tuo cluster Kubernetes, usa il flag --in-cluster. In questo modo lo script utilizzerà il ServiceAccount del pod per interrogare le API di K8s.Bashpython mongot_monitor.py \
  --uri "mongodb://..." \
  --namespace mongodb \
  --in-cluster
(Nota: Il ServiceAccount associato al pod dovrà avere un Role/ClusterRole con permessi di lettura su pods, pods/log, pods/exec, events, services, persistentvolumeclaims e sui CRD mongodbsearch e deployments).⚙️ Parametri CLIParametroDescrizioneDefault--uriStringa di connessione a MongoDB.None (Solo metriche K8s)--portPorta su cui esporre la dashboard web.5050--hostInterfaccia di binding per Flask.0.0.0.0--namespaceNamespace Kubernetes da scansionare.Auto-discover su tutti--in-clusterAttiva l'autenticazione K8s via ServiceAccount.False🧠 Come funziona il SRE Advisor?Il pannello Compliance & Best Practices calcola automaticamente i seguenti indicatori:Spazio Disco (Regola del 125%): Verifica che ci sia abbastanza spazio libero (1.25x dell'usato) per permettere a Lucene di fare il rebuild degli indici in background.Consolidamento Indici: Avvisa se ci sono troppi indici frammentati sulla stessa collection (anti-pattern).Collo di Bottiglia I/O: Incrocia la Disk Queue Length con l'Oplog Lag per capire se i dischi Kubernetes (PVC) stanno soffocando l'indicizzazione.Rapporto CPU/QPS: Verifica che ci sia almeno 1 Core allocato ogni 10 Queries Per Second in base al traffico rilevato dal profiler di MongoDB.