#!/usr/bin/env bash
# End to end, on a real cluster: install the sample chart, expect no drift, change things by hand
# the way people do, expect exactly that drift, undo it, expect no drift again.
#
# Uses whatever cluster KUBECONFIG points at, in a namespace of its own, which it deletes at the
# end. CI runs it on a throwaway kind cluster. Never point it at a cluster you care about.
set -euo pipefail

NS="${E2E_NAMESPACE:-helmdrift-e2e}"
RELEASE=shop
CHART="$(dirname "$0")/../examples/shop"
WORK="$(mktemp -d)"
trap 'helm uninstall "$RELEASE" -n "$NS" >/dev/null 2>&1 || true; kubectl delete namespace "$NS" --wait=false >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

check() {  # check EXPECTED-EXIT-CODE [helmdrift args...]
  local expected=$1; shift
  set +e
  helmdrift --release "$RELEASE" --namespace "$NS" "$@"
  local code=$?
  set -e
  if [ "$code" -ne "$expected" ]; then
    echo "FAIL: expected exit code $expected, got $code" >&2
    exit 1
  fi
}

echo "== 1. install"
helm install "$RELEASE" "$CHART" --namespace "$NS" --create-namespace --wait --timeout 5m

echo "== 2. a fresh install has no drift"
check 0 --no-color

echo "== 3. change things by hand"
kubectl -n "$NS" scale deploy/"$RELEASE"-web --replicas=4
kubectl -n "$NS" set image deploy/"$RELEASE"-web web=nginx:1.28-alpine
kubectl -n "$NS" set env deploy/"$RELEASE"-web DEBUG=true
kubectl -n "$NS" annotate deploy/"$RELEASE"-web example.com/paged-by=oncall
cat > "$WORK/editor.sh" <<'EDIT'
#!/bin/sh
sed -i.bak -e 's/memory: 128Mi/memory: 256Mi/' -e 's/LOG_LEVEL: info/LOG_LEVEL: debug/' "$1"
EDIT
chmod +x "$WORK/editor.sh"
KUBE_EDITOR="$WORK/editor.sh" kubectl -n "$NS" edit deploy/"$RELEASE"-web
KUBE_EDITOR="$WORK/editor.sh" kubectl -n "$NS" edit cm/"$RELEASE"-config
kubectl -n "$NS" patch secret "$RELEASE"-credentials -p '{"stringData":{"password":"changed-by-hand"}}'
kubectl -n "$NS" delete svc/"$RELEASE"-metrics
kubectl -n "$NS" create configmap "$RELEASE"-legacy --from-literal=OLD_SETTING=1
kubectl -n "$NS" annotate cm/"$RELEASE"-legacy meta.helm.sh/release-name="$RELEASE" meta.helm.sh/release-namespace="$NS"

echo "== 4. exactly that drift is reported, attributed, with the Secret masked"
check 1 --no-color
check 1 --format json > "$WORK/drift.json"
python3 - "$WORK/drift.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
found = {
    (o["kind"], o["name"], d["change"], d["path"], d["owners"][0]["manager"] if d["owners"] else None)
    for o in report["objects"] for d in o["differences"]
}
c = "spec.template.spec.containers[name=web]"
expected = {
    ("Deployment", "shop-web", "changed", f"{c}.image", "kubectl-set"),
    ("Deployment", "shop-web", "added", f"{c}.env[name=DEBUG]", "kubectl-set"),
    ("Deployment", "shop-web", "changed", f"{c}.resources.limits.memory", "kubectl-edit"),
    ("Deployment", "shop-web", "added", 'metadata.annotations["example.com/paged-by"]', "kubectl-annotate"),
    ("ConfigMap", "shop-config", "changed", "data.LOG_LEVEL", "kubectl-edit"),
    ("Secret", "shop-credentials", "changed", "data.password", "kubectl-patch"),
}
# kubectl versions name the scale manager differently, so only its prefix is checked.
scale = [f for f in found if f[3] == "spec.replicas"]
assert len(scale) == 1 and scale[0][4].startswith("kubectl"), scale
assert found - set(scale) == expected, sorted(found ^ expected)
assert [m["name"] for m in report["missing"]] == ["shop-metrics"], report["missing"]
assert [e["name"] for e in report["extra"]] == ["shop-legacy"], report["extra"]
text = open(sys.argv[1]).read()
assert "changed-by-hand" not in text and "Y2hhbmdlZC1ieS1oYW5k" not in text, "a Secret value leaked"
print(f"ok: {len(found)} differences, each attributed; 1 missing; 1 extra; no Secret value shown")
PY

echo "== 5. undo it"
# What kubectl added was never Helm's, so Helm will not remove it: take it out by hand.
kubectl -n "$NS" set env deploy/"$RELEASE"-web DEBUG-
kubectl -n "$NS" annotate deploy/"$RELEASE"-web example.com/paged-by-
kubectl -n "$NS" delete cm/"$RELEASE"-legacy
# What kubectl changed, Helm takes back. Without --force-conflicts, Helm 4's server-side apply
# stops at the first field another tool now owns: the conflict helmdrift reports in advance.
helm upgrade "$RELEASE" "$CHART" --namespace "$NS" --force-conflicts --wait --timeout 5m

echo "== 6. no drift again"
check 0 --no-color
echo "end to end: passed"
