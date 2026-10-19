# helmdrift

**What is actually running in your cluster, versus what your Helm chart says should be running,
and who changed it.**

> Week 5 of a 10-week build. In progress: see [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)
> for the design.

---

## The problem

Someone runs `kubectl edit` at 2am to stop an outage and never puts the fix into the chart. The
next `helm upgrade` silently wipes it, and the outage comes back. The cluster had drifted from the
chart, and nothing said so.

Diffing the chart's output against the live objects does not help: a live object carries dozens of
fields its manifest never set (bookkeeping, server defaults, the same value written another way),
so a raw diff shows hundreds of differences and none of them matter.

## What it does

- **Strips the noise.** Bookkeeping fields, server-filled defaults, and values that only differ in
  form (`0.5` CPU is `500m`) are not drift. What is left is what someone changed.
- **Says who changed it.** Kubernetes records which tool last set each field. `helmdrift` reads
  that record and reports *last set by kubectl-edit, 02:16 UTC* next to each difference.
- **Matches list items by name,** so reordering containers is not drift, and a changed environment
  variable is reported by its name, not by its position.
- **Never prints Secret values,** in any output format.
- **Exits non-zero on drift,** so it runs as a scheduled CI check. Read-only: it never changes the
  cluster.
- **Takes ignore rules as data.** The path syntax in the report is the path syntax in the ignore
  file, so silencing a difference means copying a line.

```console
$ helmdrift --release api --namespace prod --chart ./charts/api -f values-prod.yaml
```

## Status

| Day | Work | |
|-----|------|-|
| Mon | Plan and README | done |
| Tue | A real throwaway cluster and real fixtures; rendering, rules and normalisation | |
| Wed | Who changed what, the diff, reading the cluster | |
| Thu | Output, the CLI, CI including a real-cluster job | |
| Fri | Demo, README with real output, publish | |

## Non-goals

- **It never changes the cluster.** No "fix it" mode.
- **No continuous watching.** It is a command; run it in CI or on a schedule.
- **Not a replacement for Argo CD or Flux.** Teams running GitOps controllers already have this.

## License

MIT. See [LICENSE](LICENSE).
