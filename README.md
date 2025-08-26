# Hot-Standby Operator (HotStandbyDeployment)

Keep a constant buffer of **idle hot-standby Pods** for instant capacity.  
When some Pods become **busy**, the operator scales up so that:

> **desiredReplicas = busyPods + idleTarget**

Example: `idleTarget=3`, and 2 Pods turn busy ⇒ scale to **5** total (2 busy + 3 idle).

---

## Features

- **Custom Resource**: `HotStandbyDeployment` (namespaced).
- Two ways to detect “busy”:
  - `busyProbe.mode: annotation` — your app flips an annotation (`paia.tech/busy: "true"|"false"`).
  - `busyProbe.mode: http` — operator probes `http://<pod-ip>:<port><path>`.
- Works like a Deployment: you provide `.spec.selector` + `.spec.podTemplate`.
- Status is reported (`busyCount`, `idleCount`, `desiredReplicas`).

---

## Repo layout (suggested)

```
.
├─ operate.py                  # The Kopf operator (Python)
├─ crd/
│  └─ hsd-crd.yaml            # CRD manifest (apply once)
├─ examples/
│  ├─ hsd-annotation.yaml     # Example HotStandbyDeployment (annotation mode)
│  └─ hsd-http.yaml           # Example HotStandbyDeployment (http mode)
└─ manifests/
   ├─ rbac.yaml               # ServiceAccount/Role/RoleBinding for in-cluster deploy
   └─ operator.yaml           # Deployment for the operator
```

---

## Quickstart (local dev)

**Prereqs**
- Python 3.10+  
- A working kubeconfig (`kubectl get ns` works)  
- `pip install kopf kubernetes requests`

**1) Apply the CRD** (if you don’t already have it):

```bash
kubectl apply -f crd/hsd-crd.yaml
```

**2) Apply an example HSD** (annotation mode shown below):

```yaml
# examples/hsd-annotation.yaml
apiVersion: apps.paia.tech/v1alpha1
kind: HotStandbyDeployment
metadata:
  name: game-ws
  namespace: default
spec:
  idleTarget: 3                   # keep 3 always idle (standby)
  minReplicas: 0
  maxReplicas: 50
  selector:
    matchLabels:
      app: game-ws
  busyProbe:
    mode: annotation
    annotationKey: paia.tech/busy
  podTemplate:
    metadata:
      labels:
        app: game-ws
    spec:
      serviceAccountName: pod-self-annotator   # if your app annotates itself
      containers:
        - name: app
          image: ghcr.io/your-org/game-ws:latest
          ports:
            - name: http
              containerPort: 8080
```

```bash
kubectl apply -f examples/hsd-annotation.yaml
```

**3) Run the operator locally (uses your kubeconfig):**

```bash
# watch only default ns
kopf run --namespace default ./operate.py
# or watch all namespaces
# kopf run --all-namespaces ./operate.py
```

Check it working:

```bash
kubectl get hotstandbydeployments
kubectl describe hsd game-ws
kubectl get deploy game-ws-workload
```

When some Pods are marked busy, you should see the child Deployment scale to `busy + idleTarget`.

---

## CRD (schema summary)

```yaml
# crd/hsd-crd.yaml (minimal)
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: hotstandbydeployments.apps.paia.tech
spec:
  group: apps.paia.tech
  scope: Namespaced
  names:
    kind: HotStandbyDeployment
    plural: hotstandbydeployments
    singular: hotstandbydeployment
    shortNames: [hsd]
  versions:
    - name: v1alpha1
      served: true
      storage: true
      subresources:
        status: {}
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              required: ["idleTarget","selector","podTemplate"]
              properties:
                idleTarget: { type: integer, minimum: 0 }
                minReplicas: { type: integer, minimum: 0 }
                maxReplicas: { type: integer, minimum: 1 }
                selector:
                  type: object
                  properties:
                    matchLabels:
                      type: object
                      additionalProperties: { type: string }
                podTemplate:
                  type: object
                  x-kubernetes-preserve-unknown-fields: true
                busyProbe:
                  type: object
                  properties:
                    mode:
                      type: string
                      enum: ["annotation","http"]
                      default: "annotation"
                    annotationKey:
                      type: string
                      default: "paia.tech/busy"
                    http:
                      type: object
                      properties:
                        port: { type: integer, default: 8080 }
                        path: { type: string, default: "/busy" }
                        successIsBusy: { type: boolean, default: true }
                        timeoutSeconds: { type: integer, default: 1 }
                        periodSeconds: { type: integer, default: 10 }
            status:
              type: object
              properties:
                observedGeneration: { type: integer }
                busyCount:          { type: integer }
                idleCount:          { type: integer }
                desiredReplicas:    { type: integer }
```

---

## How to mark Pods “busy”

### A) Annotation mode (recommended; simplest & robust)

Your app toggles its own Pod annotation:

```python
# busy_marker.py
import os
from kubernetes import client, config

ANNOTATION_KEY = os.getenv("BUSY_KEY", "paia.tech/busy")
POD = os.environ["POD_NAME"]
NS  = os.environ["POD_NAMESPACE"]

def set_busy(is_busy: bool):
    # in cluster: config.load_incluster_config(); locally: load_kube_config()
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    v1 = client.CoreV1Api()
    body = {"metadata": {"annotations": {ANNOTATION_KEY: "true" if is_busy else "false"}}}
    v1.patch_namespaced_pod(POD, NS, body)
```

Add Downward API envs in your pod:

```yaml
env:
  - name: POD_NAME
    valueFrom: { fieldRef: { fieldPath: metadata.name } }
  - name: POD_NAMESPACE
    valueFrom: { fieldRef: { fieldPath: metadata.namespace } }
```

> **RBAC**: give the Pod’s ServiceAccount `get,patch` on `pods` in its namespace.

### B) HTTP mode

Expose an endpoint in your container:

```http
GET /busy -> 200 when busy, 503 when idle   # (configurable via successIsBusy)
```

Configure the CR:

```yaml
busyProbe:
  mode: http
  http:
    port: 8080
    path: /busy
    successIsBusy: true
    timeoutSeconds: 1
    periodSeconds: 10
```

---

## In-cluster deployment

1) **RBAC** (cluster-wide or namespaced). Minimal example:

```yaml
# manifests/rbac.yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: hsd-operator
  namespace: default
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: hsd-operator
  namespace: default
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get","list","watch"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get","list","watch","create","patch"]
  - apiGroups: ["apps.paia.tech"]
    resources: ["hotstandbydeployments"]
    verbs: ["get","list","watch"]
  - apiGroups: ["apps.paia.tech"]
    resources: ["hotstandbydeployments/status"]
    verbs: ["get","patch","update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: hsd-operator
  namespace: default
subjects:
  - kind: ServiceAccount
    name: hsd-operator
    namespace: default
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: hsd-operator
```

2) **Operator Deployment**:

```yaml
# manifests/operator.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: hsd-operator
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels: { app: hsd-operator }
  template:
    metadata:
      labels: { app: hsd-operator }
    spec:
      serviceAccountName: hsd-operator
      containers:
        - name: operator
          image: ghcr.io/your-org/hsd-operator:latest
          imagePullPolicy: IfNotPresent
          command: ["kopf"]
          args:
            - "run"
            - "--namespace=default"        # or --all-namespaces
            - "/app/operate.py"
          ports:
            - containerPort: 8080
          resources:
            requests: { cpu: "50m", memory: "64Mi" }
            limits:   { cpu: "200m", memory: "256Mi" }
```

Build & deploy:

```bash
# build/push your image (example)
docker build -t ghcr.io/your-org/hsd-operator:latest .
docker push ghcr.io/your-org/hsd-operator:latest

kubectl apply -f crd/hsd-crd.yaml
kubectl apply -f manifests/rbac.yaml
kubectl apply -f manifests/operator.yaml
kubectl apply -f examples/hsd-annotation.yaml
```

---

## Operational tips

- **Do not combine** another HPA on the same child Deployment; both would fight over `.spec.replicas`. If you must, use HPA for upper bound protection only (carefully).
- To avoid sending new traffic to busy Pods, drop readiness when busy (or use a custom `readinessGate`).
- For gradual scale-downs, consider implementing a cool-down window in your app or operator (this repo’s reference operator keeps it simple).

---

## Troubleshooting

- `ConfigException: Service host/port is not set.`  
  You ran locally but tried in-cluster config. The provided operator auto-detects; run with:
  ```bash
  kopf run --namespace default ./operate.py
  ```
- `FutureWarning: namespaces or cluster-wide flag will become an error`  
  Add `--namespace default` or `--all-namespaces` to `kopf run`.
- `Forbidden` / RBAC errors when creating/patching Deployments or updating status  
  Ensure the operator ServiceAccount has the permissions from **RBAC** above.
- Windows: `OS signals are ignored`  
  Benign warning from Kopf on Windows; safe to ignore when developing locally.

---

## API recap

```yaml
spec:
  idleTarget: <int>              # keep this many idle pods at all times
  minReplicas: <int>             # optional
  maxReplicas: <int>             # optional
  selector:
    matchLabels: {...}           # labels to select child pods
  podTemplate: {...}             # like Deployment.spec.template (camelCase OK)
  busyProbe:
    mode: annotation|http        # default: annotation
    annotationKey: paia.tech/busy
    http:
      port: 8080
      path: /busy
      successIsBusy: true
      timeoutSeconds: 1
      periodSeconds: 10
status:
  busyCount: <int>
  idleCount: <int>
  desiredReplicas: <int>
  observedGeneration: <int>
```

---

## License

MIT (feel free to adapt to your needs).

---

## Contributing

Issues and PRs welcome!  
If you want a Helm chart or scale-down cool-down logic, open an issue with your use-case.
