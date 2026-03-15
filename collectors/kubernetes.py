"""
Kubernetes discovery collectors.
All functions read cluster state from K8s APIs.
Global clients are stored in state.py and initialised by init_k8s().
"""

import logging
from datetime import datetime, timezone

import state

try:
    from kubernetes import client as k8s_client, config as k8s_config
    K8S_AVAILABLE = True
except ImportError:
    k8s_client = None
    k8s_config = None
    K8S_AVAILABLE = False

log = logging.getLogger("mongot-monitor.k8s")


# ── Init ──────────────────────────────────────────────────────────────────────

def init_k8s(in_cluster: bool = False) -> bool:
    """Load K8s config and instantiate API clients into state. Returns True on success."""
    if not K8S_AVAILABLE:
        log.warning("kubernetes package not installed — K8s features disabled.")
        return False
    try:
        k8s_config.load_incluster_config() if in_cluster else k8s_config.load_kube_config()
        state.k8s_v1     = k8s_client.CoreV1Api()
        state.k8s_custom  = k8s_client.CustomObjectsApi()
        state.k8s_apps    = k8s_client.AppsV1Api()
        log.info("✓ K8s configurato.")
        return True
    except Exception as e:
        log.warning(f"✗ K8s error: {e}")
        return False


# ── CRD & Operator ────────────────────────────────────────────────────────────

def discover_mongodbsearch_crds(errors: list = None) -> list:
    if not state.k8s_custom: return []
    crds = []
    try:
        namespaces = (
            [state.TARGET_NAMESPACE] if state.TARGET_NAMESPACE
            else [ns.metadata.name for ns in state.k8s_v1.list_namespace().items]
        )
    except Exception as e:
        if errors is not None: errors.append(f"K8s API Error (Reading namespaces for CRDs): {str(e)}")
        namespaces = [state.TARGET_NAMESPACE] if state.TARGET_NAMESPACE else ["mongodb", "default"]

    for ns in namespaces:
        try:
            res = state.k8s_custom.list_namespaced_custom_object("mongodb.com", "v1", ns, "mongodbsearch")
            for item in res.get("items", []):
                spec, status, meta = item.get("spec", {}), item.get("status", {}), item.get("metadata", {})
                prom_conf = spec.get("prometheus", {}) or {}
                crds.append({
                    "name": meta.get("name", "?"), "namespace": ns,
                    "prometheus_enabled": bool(prom_conf),
                    "prometheus_port": prom_conf.get("port", 9946) if isinstance(prom_conf, dict) else 9946,
                    "phase": status.get("phase", "Unknown"),
                    "log_level": spec.get("logLevel", "INFO")
                })
        except Exception as e:
            if errors is not None: errors.append(f"K8s API Error (MongoDBSearch CRD in ns '{ns}'): {str(e)}")
    return crds


def discover_operator_info(errors: list = None) -> dict:
    if not state.k8s_apps: return {}
    try:
        namespaces = [state.TARGET_NAMESPACE] if state.TARGET_NAMESPACE else ["mongodb", "default", "mongo"]
        for ns in namespaces:
            try:
                deps = state.k8s_apps.list_namespaced_deployment(ns)
                for dep in deps.items:
                    dname = dep.metadata.name.lower()
                    if "mongodb" in dname and ("operator" in dname or "controller" in dname):
                        containers = dep.spec.template.spec.containers or []

                        pod_name = dname
                        if state.k8s_v1:
                            try:
                                pods = state.k8s_v1.list_namespaced_pod(ns)
                                for p in pods.items:
                                    if p.metadata.name.startswith(dname):
                                        pod_name = p.metadata.name
                                        break
                            except Exception as e:
                                if errors is not None: errors.append(f"K8s API Error (Pod list for Operator in ns '{ns}'): {str(e)}")

                        return {
                            "name": dep.metadata.name, "namespace": ns, "pod_name": pod_name,
                            "image": containers[0].image if containers else "?",
                            "replicas": dep.status.ready_replicas or 0, "desired": dep.spec.replicas or 1
                        }
            except Exception as e:
                if errors is not None: errors.append(f"K8s API Error (Deployment list for Operator in ns '{ns}'): {str(e)}")
    except Exception as e:
        if errors is not None: errors.append(f"K8s API Error (Operator Discovery): {str(e)}")
    return {}


# ── Pod Discovery ─────────────────────────────────────────────────────────────

def get_pod_warnings(namespace: str, pod_name: str) -> list:
    if not state.k8s_v1: return []
    warnings = []
    try:
        fs = f"involvedObject.name={pod_name},type=Warning"
        events = state.k8s_v1.list_namespaced_event(namespace, field_selector=fs).items
        events.sort(key=lambda x: x.last_timestamp or x.event_time or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        for e in events[:5]:
            warnings.append({"reason": e.reason, "message": e.message, "count": e.count,
                              "time": e.last_timestamp.isoformat() if e.last_timestamp else None})
    except Exception:
        pass
    return warnings


def discover_mongot_pods(errors: list = None) -> list:
    if not state.k8s_v1: return []
    pods = []
    found_pods = set()
    try:
        res = (state.k8s_v1.list_namespaced_pod(state.TARGET_NAMESPACE)
               if state.TARGET_NAMESPACE else state.k8s_v1.list_pod_for_all_namespaces())
        for pod in res.items:
            pname = pod.metadata.name.lower()
            labels = pod.metadata.labels or {}
            containers = pod.spec.containers or []

            # Always exclude the monitor pod itself
            if labels.get("app") == "mongot-monitor":
                continue

            # 1️⃣ Official MCK label (most reliable, works with scaling + rolling upgrades)
            if labels.get("app.kubernetes.io/component") == "search":
                is_mongot = True

            # 2️⃣ Fallback: container named exactly "mongot" (stable across versions)
            elif any(c.name.lower() == "mongot" for c in containers):
                is_mongot = True

            # 3️⃣ Fallback: container image contains known search image names
            elif any(
                "mongodb-enterprise-search" in (c.image or "").lower() or
                "mongot" in (c.image or "").lower()
                for c in containers
            ):
                is_mongot = True

            # 4️⃣ Last resort: pod name heuristic (fragile, kept as safety net)
            elif "mongot" in pname and "mongod" not in pname and "monitor" not in pname:
                is_mongot = True

            else:
                is_mongot = False

            if not is_mongot or pod.metadata.name in found_pods:
                continue
            found_pods.add(pod.metadata.name)

            cpu_limit_cores = 0.0
            memory_limit_bytes = 0
            discovered_prom_port = None

            for c in (pod.spec.containers or []):
                for p in (c.ports or []):
                    if p.container_port and (p.container_port == 9946 or (p.name and "prom" in p.name.lower())):
                        discovered_prom_port = p.container_port

                if c.resources and c.resources.limits:
                    if "cpu" in c.resources.limits:
                        cpu_str = str(c.resources.limits["cpu"])
                        try:
                            if cpu_str.endswith("m"): cpu_limit_cores += int(cpu_str[:-1]) / 1000.0
                            else: cpu_limit_cores += float(cpu_str)
                        except: pass

                    if "memory" in c.resources.limits:
                        mem_str = str(c.resources.limits["memory"])
                        try:
                            if mem_str.endswith("Gi"): memory_limit_bytes += float(mem_str[:-2]) * 1024 * 1024 * 1024
                            elif mem_str.endswith("Mi"): memory_limit_bytes += float(mem_str[:-2]) * 1024 * 1024
                            elif mem_str.endswith("Ki"): memory_limit_bytes += float(mem_str[:-2]) * 1024
                            elif mem_str.endswith("G"): memory_limit_bytes += float(mem_str[:-1]) * 1000 * 1000 * 1000
                            elif mem_str.endswith("M"): memory_limit_bytes += float(mem_str[:-1]) * 1000 * 1000
                            else: memory_limit_bytes += float(mem_str)
                        except: pass

            containers = []
            for cs in (pod.status.container_statuses or []):
                last_reason = None
                if cs.last_state and cs.last_state.terminated:
                    last_reason = cs.last_state.terminated.reason
                containers.append({
                    "name": cs.name, "ready": cs.ready, "restart_count": cs.restart_count,
                    "state": ("running" if cs.state.running else "waiting" if cs.state.waiting
                              else "terminated" if cs.state.terminated else "unknown"),
                    "last_reason": last_reason
                })

            pods.append({
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "node": pod.spec.node_name,
                "pod_ip": pod.status.pod_ip,
                "phase": pod.status.phase,
                "start_time": pod.status.start_time.isoformat() if pod.status.start_time else None,
                "age": str(int((datetime.now(timezone.utc) - pod.metadata.creation_timestamp).total_seconds() / 86400)) + "d",
                "containers": containers,
                "total_restarts": sum(c["restart_count"] for c in containers),
                "all_ready": all(c["ready"] for c in containers) if containers else False,
                "cpu_limit_cores": round(cpu_limit_cores, 2),
                "memory_limit_bytes": int(memory_limit_bytes),
                "warnings": get_pod_warnings(pod.metadata.namespace, pod.metadata.name),
                "discovered_prom_port": discovered_prom_port
            })
    except Exception as e:
        log.error(f"K8s pod discovery error: {e}")
        if errors is not None: errors.append(f"K8s API Error (Pod Discovery): {str(e)}")
    return pods


# ── PVCs & Services ───────────────────────────────────────────────────────────

def get_mongot_pvcs(errors: list = None) -> list:
    pvcs = []
    if not state.k8s_v1: return pvcs
    try:
        res = (state.k8s_v1.list_namespaced_persistent_volume_claim(state.TARGET_NAMESPACE)
               if state.TARGET_NAMESPACE else state.k8s_v1.list_persistent_volume_claim_for_all_namespaces())
        for pvc in res.items:
            if "search" in pvc.metadata.name.lower() or "mongot" in pvc.metadata.name.lower():
                pvcs.append({
                    "name": pvc.metadata.name, "namespace": pvc.metadata.namespace,
                    "status": pvc.status.phase,
                    "capacity": pvc.status.capacity.get("storage", "?") if pvc.status.capacity else "?",
                    "storage_class": pvc.spec.storage_class_name
                })
    except Exception as e:
        if errors is not None: errors.append(f"K8s API Error (Discovery PVCs '{state.TARGET_NAMESPACE or 'all'}'): {str(e)}")
    return pvcs


def get_mongot_services(errors: list = None) -> list:
    services = []
    if not state.k8s_v1: return services
    try:
        res = (state.k8s_v1.list_namespaced_service(state.TARGET_NAMESPACE)
               if state.TARGET_NAMESPACE else state.k8s_v1.list_service_for_all_namespaces())
        for svc in res.items:
            sname = svc.metadata.name.lower()
            if "search" in sname or "mongot" in sname:
                ports = [{"port": p.port, "target": p.target_port, "protocol": p.protocol}
                         for p in (svc.spec.ports or [])]
                services.append({"name": svc.metadata.name, "namespace": svc.metadata.namespace,
                                  "type": svc.spec.type, "ports": ports})
    except Exception as e:
        if errors is not None: errors.append(f"K8s API Error (Discovery Services '{state.TARGET_NAMESPACE or 'all'}'): {str(e)}")
    return services


# ── Metrics Server & Helm ─────────────────────────────────────────────────────

def get_pod_metrics() -> dict:
    pod_metrics = {}
    if not state.k8s_custom: return pod_metrics
    try:
        res = (state.k8s_custom.list_namespaced_custom_object("metrics.k8s.io", "v1beta1", state.TARGET_NAMESPACE, "pods")
               if state.TARGET_NAMESPACE
               else state.k8s_custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "pods"))
        for item in res.get("items", []):
            name, total_cpu, total_mem = item["metadata"]["name"], 0, 0
            for c in item.get("containers", []):
                cpu_str = c.get("usage", {}).get("cpu", "0")
                mem_str = c.get("usage", {}).get("memory", "0")
                if cpu_str.endswith("n"): total_cpu += int(cpu_str[:-1]) / 1e6
                elif cpu_str.endswith("m"): total_cpu += int(cpu_str[:-1])
                if mem_str.endswith("Ki"): total_mem += int(mem_str[:-2]) * 1024
                elif mem_str.endswith("Mi"): total_mem += int(mem_str[:-2]) * 1024 * 1024
            pod_metrics[name] = {"cpu_millicores": round(total_cpu, 1), "memory_bytes": int(total_mem)}
    except Exception:
        pass
    return pod_metrics


def get_k8s_version() -> str:
    if not K8S_AVAILABLE or not k8s_client: return "N/A"
    try: return k8s_client.VersionApi().get_code().git_version
    except Exception: return "N/A"


def get_helm_releases(errors: list = None) -> list:
    releases = []
    if not state.k8s_v1: return releases
    try:
        res = (state.k8s_v1.list_namespaced_secret(state.TARGET_NAMESPACE, label_selector="owner=helm")
               if state.TARGET_NAMESPACE
               else state.k8s_v1.list_secret_for_all_namespaces(label_selector="owner=helm"))

        latest_rels = {}
        for s in res.items:
            labels = s.metadata.labels or {}
            name = labels.get("name", "unknown")
            if "mongo" not in name.lower(): continue
            try: version = int(labels.get("version", 0))
            except: version = 0
            if name not in latest_rels or latest_rels[name]["revision"] < version:
                latest_rels[name] = {
                    "name": name, "namespace": s.metadata.namespace, "revision": version,
                    "status": labels.get("status", "unknown"),
                    "modifiedAt": labels.get("modifiedAt", "unknown")
                }

        for v in latest_rels.values():
            try:
                if str(v["modifiedAt"]).isdigit():
                    v["modifiedAt_str"] = datetime.fromtimestamp(int(v["modifiedAt"]), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    v["modifiedAt_str"] = str(v["modifiedAt"])
            except:
                v["modifiedAt_str"] = "N/A"
            releases.append(v)

    except Exception as e:
        if errors is not None: errors.append(f"K8s API Error (Helm Release Discovery): {str(e)}")

    return sorted(releases, key=lambda x: x["name"])
