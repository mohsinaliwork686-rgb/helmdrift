# Fixtures from a real cluster

Captured on Tuesday 20 October 2026 from a throwaway k3d cluster (Kubernetes 1.36.2, k3s), after
installing `examples/shop` with Helm 4.2.2. Nothing here is hand-written, so the noise in these
files is real noise.

- `manifest.yaml`: `helm get manifest shop -n shop`, what Helm applied.
- `clean/`: every object right after the install, `kubectl get -o yaml --show-managed-fields`.
- `drifted/`: the same objects after this drift, made by hand:

```console
kubectl -n shop scale deploy/shop-web --replicas=4
kubectl -n shop set image deploy/shop-web web=nginx:1.28-alpine
kubectl -n shop set env deploy/shop-web DEBUG=true
kubectl -n shop annotate deploy/shop-web example.com/paged-by=oncall
kubectl -n shop edit deploy/shop-web          # memory limit 128Mi -> 256Mi
kubectl -n shop edit cm/shop-config           # LOG_LEVEL info -> debug
kubectl -n shop patch secret shop-credentials -p '{"stringData":{"password":"changed-at-2am"}}'
kubectl -n shop delete svc/shop-metrics
kubectl -n shop create configmap shop-legacy --from-literal=OLD_SETTING=1
kubectl -n shop annotate cm/shop-legacy meta.helm.sh/release-name=shop meta.helm.sh/release-namespace=shop
```

The worker's replica count also differs from the chart in both states: its HorizontalPodAutoscaler
raised it from 1 to 2. That is not drift, and helmdrift must not report it.

The Secret holds sample values only.
