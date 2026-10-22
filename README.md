# helmdrift

[![ci](https://github.com/mohsinaliwork686-rgb/helmdrift/actions/workflows/ci.yml/badge.svg)](https://github.com/mohsinaliwork686-rgb/helmdrift/actions/workflows/ci.yml)

**What is actually running in your cluster, versus what your Helm chart says should be running,
and who changed it.**

Someone runs `kubectl edit` at 2am to stop an outage and never puts the fix into the chart.
helmdrift finds that change, tells you which tool made it, and exits non-zero, so it can run as a
scheduled CI check. It is read-only: it never changes the cluster.

---

## The problem

A raw diff of a chart against the live objects is useless. A live object carries dozens of fields
its manifest never set: bookkeeping (`resourceVersion`, `managedFields`), defaults the API server
filled in (`imagePullPolicy`, a Service's `clusterIP`), and values written another way (`cpu: 0.5`
in the chart is `500m` in the cluster). Hundreds of differences, none of which matter.

And with Helm 4, drift is no longer silently wiped by the next deploy. Helm 4 applies charts with
server-side apply, so a field someone changed by hand now **blocks** the next `helm upgrade`:

```text
Error: UPGRADE FAILED: conflict occurred while applying object shop/shop-config /v1, Kind=ConfigMap:
Apply failed with 1 conflict: conflict with "kubectl-edit" using v1: .data.LOG_LEVEL && conflict
occurred while applying object shop/shop-web apps/v1, Kind=Deployment: Apply failed with 1
conflict: conflict with "kubectl" with subresource "scale" using apps/v1: .spec.replicas
```

helmdrift tells you about those fields, and who owns them, before the deploy does.

## What it looks like

Install the sample chart on a throwaway cluster, change three things by hand, and run it. This is
real output:

```console
$ helm install shop examples/shop --namespace shop --create-namespace --wait
$ kubectl -n shop scale deploy/shop-web --replicas=4
$ kubectl -n shop set env deploy/shop-web DEBUG=true
$ kubectl -n shop edit cm/shop-config          # LOG_LEVEL: info -> debug
$ helmdrift --release shop --namespace shop
helmdrift: release shop in namespace shop, context k3d-helmdrift-dev, compared with what Helm applied at the last install or upgrade

ConfigMap/shop-config
  ~ data.LOG_LEVEL
      chart info  ->  live debug
      owned by kubectl-edit, last write 2026-10-22 09:59 UTC

Deployment/shop-web
  ~ spec.replicas
      chart 2  ->  live 4
      owned by kubectl (scale)
  + spec.template.spec.containers[name=web].env[name=DEBUG]
      live {"name": "DEBUG", "value": "true"}
      added by kubectl-set, last write 2026-10-22 09:59 UTC

3 differences in 2 objects.
$ echo $?
1
```

Three changes, three differences. None of the dozens of server-filled fields on those objects
show up, and the worker Deployment, whose replica count its autoscaler changed, is not reported.

## How it tells real changes from noise

1. **Bookkeeping is removed**: `status`, `uid`, `resourceVersion`, Helm's own annotations, and so on.
2. **The same value written two ways is equal.** Quantities compare by amount (`0.5` = `500m`,
   `1Gi` = `1024Mi`, but `1G` is not `1Gi`), only in fields that hold quantities. A Secret's
   `stringData` compares as the base64 `data` the cluster stores. Empty and absent are the same.
3. **List entries are matched by key**, not position: containers and environment variables by
   name, volume mounts by path. Reordering is not drift.
4. **Fields only the cluster has are judged by who owns them.** Kubernetes records, in
   `managedFields`, which tool last set each field. Tested on a real cluster: a value the API
   server filled in by default belongs to no tool, while a field added by hand belongs to whatever
   added it. So helmdrift reports a live-only field only when a tool other than Helm owns it.
   Objects without that record fall back to a list of known defaults, matched by value:
   `terminationGracePeriodSeconds: 30` is a default, `120` is somebody's decision.
5. **A replica count set by a HorizontalPodAutoscaler is not drift**, whether the autoscaler is
   in the chart or only in the cluster. `--no-hpa-rule` turns this off.
6. **Secret values are never printed**, in any format. They are masked inside the diff itself.

The owner shown is the tool's last write to the object, which Kubernetes records per tool, not per
field. `kubectl scale` records no time at all.

## Install

```console
$ pip install "git+https://github.com/mohsinaliwork686-rgb/helmdrift"
```

It needs Python 3.11+, the `helm` binary on your PATH, and a kubeconfig.

## Usage

```console
# Drift since the last deploy: compare with what Helm applied then.
$ helmdrift --release api --namespace prod

# Compare with the chart on disk and this environment's values.
$ helmdrift --release api --namespace prod --chart ./charts/api -f values-prod.yaml

# A pipeline that already renders the chart can pipe it in.
$ helm template api ./charts/api -f values-prod.yaml | helmdrift -r api -n prod --manifest -

# For CI: a specific cluster, JSON out.
$ helmdrift -r api -n prod --context prod-eu --format json > drift.json
```

| Exit code | Meaning |
|-----------|---------|
| 0 | No drift |
| 1 | Drift found: changed or added fields, missing objects, or extra objects that belong to the release but that the chart no longer renders |
| 2 | Could not check: bad arguments, `helm` failed, or the cluster could not be read |

Other options: `--set` for chart values, `--kubeconfig`, `--manager NAME` for another tool that
counts as the chart (repeatable), `--show-ignored`, `--no-color`, and `--helm` to pick the binary.

## Ignoring differences

Every cluster has operators adding their own annotations. Rules go in `.helmdrift-ignore.yaml`
(picked up from the working directory) or any file passed with `--ignore-file`:

```yaml
ignore:
  - path: metadata.annotations["example.com/last-sync"]
    reason: written by our sync job

  - kind: Deployment
    name: worker
    path: spec.replicas
    reason: scaled by KEDA, not by an HPA

  - path: spec.template.spec.containers[*].env[name=OTEL_*]
    reason: injected by the tracing operator
```

**The path syntax is the one the report prints**, so silencing a difference means copying its
path. `*` matches any one segment or part of a key, `**` any depth. `kind` and `name` narrow a
rule; `value` makes it match only certain values. A malformed rule stops the run with its file and
line, rather than being skipped.

## Permissions

helmdrift only calls `get` and `list`. A namespace-scoped, read-only Role is enough for most
charts:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: helmdrift-reader
  namespace: prod
rules:
  - apiGroups: ["*"]
    resources: ["*"]
    verbs: ["get", "list"]
```

It needs to read Secrets: to compare the chart's Secrets, and because Helm keeps its release
record in one. A chart with cluster-wide objects (a ClusterRole, a CRD) needs the same verbs in a
ClusterRole.

## What it does not do

- **It never changes the cluster.** No "fix it" mode.
- **No continuous watching.** It is a command; run it in CI or on a schedule.
- **Not a replacement for Argo CD or Flux.** Teams running GitOps controllers already have drift
  detection.

## Development

```console
$ python -m venv .venv && source .venv/bin/activate
$ pip install -e ".[dev]"
$ pytest
$ ruff check . && ruff format --check .
```

No unit test needs a cluster: the fixtures in `tests/fixtures/shop` are real objects captured from
a cluster before and after drift, and the cluster is read through an interface that tests replace.
CI also runs `scripts/e2e.sh` on a real kind cluster: install, no drift, change things by hand,
exactly that drift, undo it, no drift again.

The design and the reasoning behind it are in [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

## License

MIT. See [LICENSE](LICENSE).
