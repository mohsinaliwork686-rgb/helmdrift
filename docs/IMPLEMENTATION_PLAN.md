# Implementation plan — helmdrift

> Written Monday 19 October 2026, before any code. Week 5 of the 10-week programme.

## 1. Problem

A Helm chart, plus its values file, is supposed to be the truth about what runs in a cluster. In
practice the cluster drifts away from it:

- someone runs `kubectl edit` at 2am to stop an outage, and never puts the fix into the chart;
- someone runs `kubectl scale` or `kubectl set image` "just for now";
- another team's tool adds an annotation or an environment variable.

Nobody finds out until the next `helm upgrade` silently wipes the manual fix, and the outage
comes back.

The obvious check is to diff the chart's output against the live objects. It does not work: a
live object carries dozens of fields its manifest never set. Some are bookkeeping
(`resourceVersion`, `uid`, `managedFields`), some are defaults the API server filled in
(`imagePullPolicy`, `terminationGracePeriodSeconds`, a Service's `clusterIP`), and some are the
same value written differently (`cpu: 0.5` in the chart is `cpu: 500m` in the cluster). A raw diff
shows hundreds of differences and none of them matter.

`helmdrift` strips all of that and prints only the differences that mean something, **and who made
each one**. It exits non-zero when it finds drift, so it works as a scheduled CI check.

## 2. What already exists

Being honest about the neighbours keeps the scope sharp.

| Tool | What it answers | Why it is not this |
|------|-----------------|--------------------|
| Argo CD, Flux | Continuous drift detection and correction | Only if you have adopted GitOps and run their controllers in the cluster |
| `helm diff` plugin | What would my next upgrade change? | Built around the chart and Helm's release record. The question here is about the live objects |
| `kubectl diff` | What would applying these files change? | Prints whole objects as text, and uses a server-side dry run, which needs permission to patch every object |

`helmdrift` is for teams that deploy with plain Helm and want one read-only command, runnable from
CI on a schedule, that answers: *what in this release has been changed by hand, and by whom?*

## 3. Non-goals

- **It never changes the cluster.** Read-only: it only gets and lists objects. No "fix it" mode.
- **No continuous watching.** It is a command. Run it in CI or on a schedule.
- **No multi-cluster view.** One context per run.
- **No web interface.**
- **Not a replacement for Argo CD or Flux.** Teams with GitOps already have this.

## 4. What the user types

```console
# Compare the live release with the chart on disk and the values for this environment.
$ helmdrift --release api --namespace prod --chart ./charts/api -f values-prod.yaml

# Without --chart: compare with what Helm recorded at the last install or upgrade.
$ helmdrift --release api --namespace prod

# For CI: machine-readable output, a specific cluster, extra ignore rules.
$ helmdrift --release api --namespace prod --context prod-eu --format json \
    --ignore-file .helmdrift-ignore.yaml
```

The design target for the output (Friday replaces this with real output):

```text
Deployment/api
  ~ spec.replicas                               chart 3            live 7
      owned by kubectl (scale), no time recorded
  ~ spec.template.spec.containers[name=api].image
                                                chart api:1.4.0    live api:1.3.9-hotfix
      owned by kubectl-set, last write 2026-10-19 02:15 UTC
  + spec.template.spec.containers[name=api].env[name=DEBUG]
                                                live value "true"
      added by kubectl-edit, last write 2026-10-19 02:16 UTC

ConfigMap/api-config
  ~ data.LOG_LEVEL                              chart info         live debug

Missing from the cluster:  Service/api-metrics
Not in the chart:          ConfigMap/api-legacy

4 differences in 2 resources, 1 missing, 1 extra.
```

**Exit codes:** `0` no drift, `1` drift found, `2` could not check (bad arguments, `helm` failed,
cluster unreachable). CI can then tell "drifted" apart from "broken".

## 5. Where "what should be running" comes from

| Mode | Source | Use it when |
|------|--------|-------------|
| `--chart PATH` | `helm template` on the chart with the given values | You want to know whether the cluster matches the chart as it is *now* |
| no `--chart` | `helm get manifest`: what Helm actually applied at the last install or upgrade | You want pure drift: what changed by hand since the last deploy, independent of chart edits since |
| `--manifest FILE` (or `-`) | An already rendered manifest. Added on Thursday | A pipeline already runs `helm template`; pipe it in. It also makes the command testable without a cluster |

Both shell out to the `helm` binary rather than reimplementing templating, which would be wrong
in a hundred small ways. Rendering then:

- splits the multi-document YAML, skipping empty documents and flattening `kind: List`;
- drops hooks and tests (anything annotated `helm.sh/hook`), which are not long-lived objects;
- fills in the namespace for namespaced kinds that do not set one.

## 6. Normalisation — the heart of it

The rules, in the order they run. **Each one is data in a YAML file shipped with the tool**,
except where noted, and each has its own tests.

1. **Bookkeeping is removed.** `status`, `metadata.managedFields` (after it has been read, see
   section 7), `uid`, `resourceVersion`, `generation`, `creationTimestamp`, and annotations such
   as `kubectl.kubernetes.io/last-applied-configuration`, `deployment.kubernetes.io/revision`,
   `meta.helm.sh/release-name` and `meta.helm.sh/release-namespace`.
2. **The same value written two ways compares equal.** Kubernetes quantities are parsed, so
   `0.5` equals `500m` and `1Gi` equals `1024Mi`, while `1G` and `1Gi` still differ. A Secret's
   `stringData` is compared as the base64 `data` the cluster stores. An empty map or list, or a
   `null`, counts the same as the field being absent. *(Code, not data: these are about meaning.)*
3. **List items are matched by their key, not their position.** Containers by `name`, environment
   variables by `name`, volumes by `name`, volume mounts by `mountPath`, container ports by
   `containerPort`, Service ports by `port`. Reordering two containers is not drift; changing one
   is reported against `containers[name=api]`. Lists with no key fall back to position. The key
   table is data.
4. **Fields the chart sets are compared exactly.** If the chart says it, the cluster must match.
5. **Fields only the cluster has are the hard part.** Most are defaults the API server filled in,
   and they must be ignored. Some were added by a person, and they are exactly the drift worth
   finding. `managedFields` tells them apart, as tested on Tuesday (below): a defaulted field
   belongs to no tool at all, while a field someone added belongs to whatever added it. So a
   live-only field is reported only when a tool other than the chart's owns it.
   **The fallback, kept either way:** a list of known defaults per kind (data), used when an
   object has no `managedFields`. Where a default has a known value, the rule matches only that
   value: `terminationGracePeriodSeconds: 30` is a default, `120` is someone's decision.
6. **The ignore file is applied last** (section 8), built-in rules first, then the user's.

Two special cases, decided now:

- **An autoscaled replica count is not drift.** When a HorizontalPodAutoscaler targets a
  Deployment or StatefulSet, in the chart or in the cluster, `spec.replicas` is not compared.
  Without this, every autoscaled workload reports drift all day. `--no-hpa-rule` turns it off.
- **Secret values are never printed**, in any format. A changed Secret key is reported as
  `data.password  changed (value hidden)`. A drift report pasted into a CI log must not leak
  credentials. This is tested, not just intended.

### Settled on Tuesday, on a real cluster

Kubernetes 1.36.2 on k3s, the `examples/shop` chart installed with Helm 4.2.2, then changed by hand.
The fixtures in `tests/fixtures/shop/` are those objects, unedited.

- **Helm 4 used server-side apply.** Its manager is `helm`, operation `Apply`.
- **All 20 server defaults on the web Deployment belong to no manager**: `imagePullPolicy`,
  `dnsPolicy`, the probe timings, `terminationGracePeriodSeconds`, the strategy, and so on.
- **Each hand edit belongs to the tool that made it:**

  | What was run | Manager recorded | What it now owns |
  |--------------|------------------|------------------|
  | `kubectl scale` | `kubectl`, through the `scale` subresource, **with no time** | `spec.replicas` |
  | `kubectl set image`, `kubectl set env` | `kubectl-set` | the image; the added `DEBUG` variable |
  | `kubectl annotate` | `kubectl-annotate` | the added annotation |
  | `kubectl edit` | `kubectl-edit` | the memory limit; the ConfigMap's `LOG_LEVEL` |
  | `kubectl patch` | `kubectl-patch` | the Secret's password |
  | the autoscaler | `k3s` (on other clusters, `kube-controller-manager`), through `scale`, no time | the worker's `spec.replicas` |

- **The controller owns bookkeeping too**: `deployment.kubernetes.io/revision` belongs to `k3s`,
  so that annotation stays on the bookkeeping list.
- **With Helm 4, drift no longer gets silently wiped; it blocks the next deploy** (found on
  Thursday). A plain `helm upgrade` on the drifted release failed: "conflicts with
  `kubectl-set`: `.spec.template.spec.containers[name="web"].image`", and, for the autoscaled
  worker, "conflict with `k3s` with subresource `scale`: `.spec.replicas`". The managers named in
  that error are the ones helmdrift reports, so it shows what will block a deploy before it does.
  `--force-conflicts` takes changed fields back, but fields another tool *added* stay: they were
  never Helm's to remove. helmdrift still reports them afterwards.
- **A manager's time is when it last wrote to the object, not to that field.** `kubectl-set` set
  the image and the variable within a second of each other and records one time for both. The
  report says "last write by", not "changed at", and shows no time when none is recorded.

## 7. Who changed it

Kubernetes records, on every object, which tool last set each field and when: the
`managedFields` list. Each entry names a *manager* (such as `helm` or `kubectl-edit`), an
operation, a time, and the set of fields it owns, in a compact
format where `f:` marks a field, `k:{...}` a list item picked by its key, `v:` a list item picked
by value, and `i:` a list item picked by position.

For each difference, `helmdrift` walks that structure to find the manager that owns the path, and
reports it: *owned by kubectl (scale)*, or *owned by kubectl-edit, last write 02:16 UTC*. This turns "something changed" into "someone
ran `kubectl scale` at 2am", which is what an on-call engineer actually needs. A field removed by
hand has no owner left to report, and says so.

The managers that count as "the chart" default to `helm` (what Helm 4 records, checked on
Tuesday) and can be extended with `--manager`.

## 8. The ignore file

The Monday decision from the roadmap: **ignore rules are data, not code.** Every cluster has
operators adding their own annotations, and if silencing one meant editing Python, nobody would.

```yaml
# .helmdrift-ignore.yaml
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

**The path syntax is exactly the syntax the report prints**, so silencing a difference means
copying its path from the output. `*` matches any one segment or any part of a key. `kind` and
`name` narrow a rule to some resources. A malformed rule fails the run with the line that is
wrong, exit code 2, rather than being skipped.

## 9. Architecture

```
  --chart / --release                 cluster (read-only)
        |                                    |
        v                                    v
   render.py  ---- desired objects     live.py ---- live objects
        |                                    |
        +----------------+   +---------------+
                         v   v
                     normalise.py   <- rules.py + defaults.yaml + the user's ignore file
                         |
                     ownership.py   (who owns each path, from managedFields)
                         |
                      diff.py       changed / added / removed, per resource; missing, extra
                         |
                     report.py      text or JSON, and the exit code
```

| Module | Job |
|--------|-----|
| `render.py` | Run `helm template` or `helm get manifest`; split, flatten, drop hooks, fill namespaces |
| `live.py` | A `ClusterReader` interface; the real one uses the official client's dynamic API, so any kind works, CRDs included. Finds extras by listing each kind for objects carrying the release's annotation |
| `paths.py` | The path model and its printing and parsing, shared by the report and the ignore file |
| `quantity.py` | Parse and compare Kubernetes quantities |
| `rules.py`, `defaults.yaml` | Load the built-in and user rules; match a path against them |
| `normalise.py` | Apply section 6 |
| `ownership.py` | Parse `managedFields` and answer "who owns this path?" |
| `diff.py` | Compare one desired object with one live object; pair resources by identity |
| `report.py` | Text with `rich`, JSON, exit codes |
| `cli.py` | Arguments and wiring only |

**The diff is hand-written**, not `deepdiff`. Matching list items by Kubernetes merge keys,
printing paths in the ignore-file syntax, and attaching an owner to each difference all happen
during the walk, and fitting them around a general-purpose library would be harder than the walk.

Resource identity is API group, kind, namespace and name. Python 3.11+, with `kubernetes`,
`PyYAML` and `rich` as the only dependencies.

## 10. Safety

- **Read-only by construction.** `live.py` only calls `get` and `list`. The RBAC it needs is
  `get` and `list` on the kinds in the chart, plus reading Helm's release Secrets when `--chart`
  is left out. The README will give a ready-made read-only Role.
- **The context is explicit.** `--context` picks the cluster; the run's first line names the
  context and namespace being checked, so a check against the wrong cluster is obvious.
- **Development never touches another cluster.** This machine already runs a cluster for other
  work (`k3d-zeroops`). The throwaway cluster for this project is created with its own kubeconfig
  file and without switching the default context, every development command names that file, and
  it is deleted when done. Tests need no cluster at all.

## 11. Build order

| Day | Work |
|-----|------|
| **Mon** | This plan and the README pitch, committed first. Then packaging |
| **Tue** | Create the throwaway cluster, install a sample chart with Helm 4, cause drift by hand (`kubectl edit`, `scale`, `set image`, `set env`) and capture the real live objects as test fixtures. **Settle the managedFields bet.** Then `paths.py`, `quantity.py`, `render.py`, `rules.py` with `defaults.yaml`, `normalise.py`, with tests |
| **Wed** | `ownership.py`, `diff.py` with keyed lists, `live.py` with the real reader and a fake, missing and extra resources, the HPA rule |
| **Thu** | `report.py` (text and JSON), `cli.py`, exit codes, the ignore file end to end, Secret masking end to end. CI on three OSes, plus a job that creates a real cluster, makes drift and checks `helmdrift` reports exactly it |
| **Fri** | A demo script on a throwaway cluster, the README with real output, publish, then the portfolio and roadmap |

Alongside, about 30–45 minutes a day: the first open-source contribution (roadmap, week 5 side
task). That work is mine to write; this plan does not cover it.

## 12. Test plan

**No unit test needs a cluster or the network.** The live side sits behind `ClusterReader`, and
fixtures are pairs of files (rendered YAML, live YAML) captured from a real cluster on Tuesday,
so the noise in them is real noise, not noise I imagined.

| Area | Must work | Must not break |
|------|-----------|----------------|
| render | multi-document charts split correctly; namespaces filled in | empty and comment-only documents; `kind: List`; hooks and tests dropped; `helm` failing is exit 2 with its message |
| quantities | `500m` = `0.5`, `1Gi` = `1024Mi`, `1e3` = `1k` | `1G` ≠ `1Gi`; garbage is compared as text, not crashed on |
| normalise | each bookkeeping rule on its own; Secret `stringData` vs `data`; empty vs absent | a chart that sets a field to its default value still compares it |
| lists | reordered containers are not drift; a changed env var is reported by name | lists with no key compare by position |
| ownership | the owner of a changed field; `k:`, `v:`, `i:` paths | a removed field has no owner; objects with no `managedFields` |
| diff | identical objects give nothing; one changed field gives exactly one difference | missing resource; extra resource; cluster-scoped kinds |
| HPA | autoscaled replicas are ignored, whether the HPA is in the chart or only in the cluster | an HPA targeting a different Deployment does not hide this one's replicas |
| ignore file | rules honoured; a path copied from the output silences it; wildcards; `kind` and `name` filters | a malformed rule is exit 2 naming it |
| secrets | a changed Secret is reported | its value appears nowhere in text or JSON output |
| report | text output; stable JSON | exit codes 0, 1 and 2, each in its own test |
| end to end (CI) | a real cluster, real drift, exactly that drift reported | after the drift is undone, exit 0 |

## 13. Risks

| Risk | Mitigation |
|------|------------|
| The managedFields bet fails, or holds only for some kinds | It is tested on Tuesday, before anything depends on it. The defaults list exists either way |
| Offline rendering differs from install-time rendering (charts that check `.Capabilities` or use `lookup`) | The no-`--chart` mode compares with what Helm actually applied |
| Helm 4 and Helm 3 record different manager names or apply modes | Checked on a real cluster; the chart's managers are configurable with `--manager` |
| A mutating webhook (a sidecar injector) adds fields during Helm's own request, so they look like Helm's | Those fields are not reported as drift, which is right. Documented |
| Credentials leaking into CI logs | Secret values are masked in every format, with a test |
| Checking, or touching, the wrong cluster | Read-only code; the context is printed first; development uses its own kubeconfig file |
| Fitting the week | Scope is fixed by this plan. The cluster work comes first because the fixtures and the bet decide the rest |

## 14. Definition of done

Shipped Thursday 22 October 2026, a day early: Friday was a day off.

- [x] Public repo, description and topics
- [x] This plan, committed before the code
- [x] README with the problem, real output from a real cluster, the ignore file, the read-only Role, and non-goals
- [x] Every row of the test plan covered; unit tests need no cluster
- [x] CI green on three OSes, plus the real-cluster job
- [x] At least 10 commits, one logical change each
