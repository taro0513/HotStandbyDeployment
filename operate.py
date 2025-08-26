import os
import typing as t
import kopf
import requests
import kubernetes
from copy import deepcopy
from kubernetes import client
from kubernetes.config import load_kube_config, load_incluster_config
from kubernetes.client import AppsV1Api, CoreV1Api
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException

GROUP = "apps.paia.tech"
VERSION = "v1alpha1"
PLURAL = "hotstandbydeployments"

DEFAULT_BUSY_ANN = "paia.tech/busy"
HTTP_DEFAULTS = {
    "port": 8080,
    "path": "/busy",
    "successIsBusy": True,
    "timeoutSeconds": 1,
    "periodSeconds": 10,
}

@kopf.on.startup()
def init_clients(memo: kopf.Memo, **_):
    try:
        if os.getenv("KUBERNETES_SERVICE_HOST"):
            load_incluster_config()
        else:
            load_kube_config()
    except ConfigException:
        load_kube_config()

    memo.apps: AppsV1Api = client.AppsV1Api()
    memo.v1: CoreV1Api = client.CoreV1Api()

def _merge_labels(*dicts: t.Dict[str, str]) -> t.Dict[str, str]:
    out: dict[str, str] = {}
    for d in dicts:
        if d:
            out.update(d)
    return out


def _pods_by_selector(v1: CoreV1Api, namespace: str, match_labels: dict) -> list[client.V1Pod]:
    label_sel = ",".join([f"{k}={v}" for k, v in (match_labels or {}).items()])
    pods = v1.list_namespaced_pod(namespace, label_selector=label_sel).items
    return [p for p in pods if not p.metadata.deletion_timestamp]


def _is_pod_busy_by_annotation(pod: client.V1Pod, ann_key: str) -> bool:
    anns = pod.metadata.annotations or {}
    return str(anns.get(ann_key, "false")).lower() == "true"


def _is_pod_busy_by_http(pod: client.V1Pod, http_cfg: dict) -> bool:
    if not pod.status or not pod.status.pod_ip:
        return False
    if pod.status.phase != "Running":
        return False

    port = http_cfg.get("port", HTTP_DEFAULTS["port"])
    path = http_cfg.get("path", HTTP_DEFAULTS["path"])
    timeout = http_cfg.get("timeoutSeconds", HTTP_DEFAULTS["timeoutSeconds"])
    success_is_busy = http_cfg.get("successIsBusy", HTTP_DEFAULTS["successIsBusy"])

    url = f"http://{pod.status.pod_ip}:{port}{path}"
    try:
        resp = requests.get(url, timeout=timeout)
        ok = 200 <= resp.status_code < 300
        return bool(ok) if success_is_busy else (not ok)
    except requests.RequestException:
        return False


def _count_busy_idle(
    v1: CoreV1Api,
    namespace: str,
    match_labels: dict,
    mode: str,
    ann_key: str,
    http_cfg: dict,
) -> tuple[int, int]:
    pods = _pods_by_selector(v1, namespace, match_labels)
    busy = 0
    if mode == "http":
        for p in pods:
            if _is_pod_busy_by_http(p, http_cfg):
                busy += 1
    else:
        for p in pods:
            if _is_pod_busy_by_annotation(p, ann_key):
                busy += 1
    idle = max(0, len(pods) - busy)
    return busy, idle


def _pod_template_from_spec(pod_template_spec: dict, merged_labels: dict) -> dict:
    tmpl = deepcopy(pod_template_spec) if pod_template_spec else {"metadata": {}, "spec": {}}
    meta = tmpl.setdefault("metadata", {})
    labels = meta.setdefault("labels", {})
    meta["labels"] = _merge_labels(labels, merged_labels)
    return tmpl


def _ensure_child_deployment(
    apps: AppsV1Api,
    owner_body: dict,
    name: str,
    namespace: str,
    match_labels: dict,
    pod_template_spec: dict,
    initial_replicas: int,
) -> client.V1Deployment:
    tmpl = _pod_template_from_spec(pod_template_spec, match_labels)

    try:
        dep = apps.read_namespaced_deployment(name=name, namespace=namespace)
        exists = True
    except ApiException as e:
        if e.status == 404:
            exists = False
            dep = None
        else:
            raise

    if not exists:
        dep_body = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {"hsd.paia.tech/name": owner_body["metadata"]["name"]},
                "ownerReferences": [{
                    "apiVersion": owner_body["apiVersion"],
                    "kind": owner_body["kind"],
                    "name": owner_body["metadata"]["name"],
                    "uid": owner_body["metadata"]["uid"],
                    "controller": True,
                    "blockOwnerDeletion": True,
                }],
            },
            "spec": {
                "replicas": int(initial_replicas),
                "selector": {"matchLabels": dict(match_labels)},
                "template": tmpl,
            },
        }
        apps.create_namespaced_deployment(namespace=namespace, body=dep_body)
        return apps.read_namespaced_deployment(name=name, namespace=namespace)

    patch_body = {
        "spec": {
            "selector": {"matchLabels": dict(match_labels)},
            "template": tmpl,
        }
    }
    apps.patch_namespaced_deployment(name=name, namespace=namespace, body=patch_body)
    return apps.read_namespaced_deployment(name=name, namespace=namespace)


def _scale_deployment(apps: AppsV1Api, name: str, namespace: str, replicas: int) -> None:
    apps.patch_namespaced_deployment(
        name=name,
        namespace=namespace,
        body={"spec": {"replicas": int(replicas)}},
    )


def _get_probe_conf(spec: dict) -> tuple[str, str, dict]:
    probe = spec.get("busyProbe") or {}
    mode = (probe.get("mode") or "annotation").lower()
    ann_key = probe.get("annotationKey") or DEFAULT_BUSY_ANN
    http_cfg = {**HTTP_DEFAULTS, **(probe.get("http") or {})}
    return mode, ann_key, http_cfg


def _desired_replicas(busy: int, idle_target: int, min_r: t.Optional[int], max_r: t.Optional[int]) -> int:
    desired = int(busy) + int(idle_target)
    if min_r is not None:
        desired = max(desired, int(min_r))
    if max_r is not None:
        desired = min(desired, int(max_r))
    return desired


def _sync_once(
    memo: kopf.Memo,
    body: dict,
    spec: dict,
    status: dict,
    meta: dict,
) -> dict:
    namespace = meta["namespace"]
    name = meta["name"]
    dep_name = f"{name}-workload"

    # read spec
    idle_target = int(spec.get("idleTarget", 0))
    min_r = spec.get("minReplicas")
    max_r = spec.get("maxReplicas")
    min_r = int(min_r) if min_r is not None else None
    max_r = int(max_r) if max_r is not None else None

    selector = (spec.get("selector") or {}).get("matchLabels") or {}
    pod_template = spec.get("podTemplate") or {}

    mode, ann_key, http_cfg = _get_probe_conf(spec)

    # ensure child deployment
    initial = idle_target if (min_r is None) else max(idle_target, min_r)
    dep = _ensure_child_deployment(
        apps=memo.apps,
        owner_body=body,
        name=dep_name,
        namespace=namespace,
        match_labels=selector,
        pod_template_spec=pod_template,
        initial_replicas=initial,
    )

    # busy / idle
    busy, idle = _count_busy_idle(
        v1=memo.v1,
        namespace=namespace,
        match_labels=selector,
        mode=mode,
        ann_key=ann_key,
        http_cfg=http_cfg,
    )

    # scale
    cur = int(dep.spec.replicas or 0)
    desired = _desired_replicas(busy, idle_target, min_r, max_r)
    if cur != desired:
        _scale_deployment(memo.apps, dep_name, namespace, desired)

    # status
    return {
        "busyCount": int(busy),
        "idleCount": int(idle),
        "desiredReplicas": int(desired),
        "observedGeneration": int(meta.get("generation", 0)),
    }

@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
@kopf.on.resume(GROUP, VERSION, PLURAL)
def reconcile(spec, status, meta, body, memo: kopf.Memo, **_):
    return _sync_once(memo, body, spec, status or {}, meta)


@kopf.timer(GROUP, VERSION, PLURAL, interval=10.0)
def periodic(spec, status, meta, body, memo: kopf.Memo, **_):
    try:
        return _sync_once(memo, body, spec, status or {}, meta)
    except Exception as e:
        kopf.info(body, reason="ReconcileError", message=f"timer reconcile failed: {e}")
        return None
