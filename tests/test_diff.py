import copy
import json

import yaml

from helmdrift.diff import HIDDEN, Comparer, autoscaled_targets, check
from helmdrift.live import StaticReader
from helmdrift.paths import format_path, parse_path
from helmdrift.render import Resource, identity_of, parse_manifest
from helmdrift.rules import Rule
from tests.conftest import FIXTURES


def cluster(state, drop=(), extra_objects=()):
    """A StaticReader over every object captured in one state."""
    bodies = []
    for path in sorted((FIXTURES / state).glob("*.yaml")):
        if path.stem in drop:
            continue
        bodies.append(yaml.safe_load(path.read_text(encoding="utf-8")))
    return StaticReader([*bodies, *extra_objects], f"fixtures/{state}")


def desired(**overrides):
    return parse_manifest((FIXTURES / "manifest.yaml").read_text(encoding="utf-8"), "shop")


def summary(report):
    """Every reported difference as (object, change, path, owner)."""
    rows = set()
    for obj in report.objects:
        for d in obj.differences:
            owner = d.owners[0].manager if d.owners else None
            rows.add((str(obj.identity), d.change, format_path(d.path), owner))
    return rows


def test_a_clean_install_has_no_drift(rules):
    report = check(desired(), cluster("clean"), "shop", "shop", rules)
    assert summary(report) == set()
    assert report.missing == [] and report.extra == []
    assert not report.has_drift


def test_the_real_drift_is_reported_exactly_and_attributed(rules):
    report = check(desired(), cluster("drifted"), "shop", "shop", rules)
    web = "Deployment/shop-web"
    containers = "spec.template.spec.containers[name=web]"
    assert summary(report) == {
        (web, "changed", "spec.replicas", "kubectl"),
        (web, "changed", f"{containers}.image", "kubectl-set"),
        (web, "added", f"{containers}.env[name=DEBUG]", "kubectl-set"),
        (web, "changed", f"{containers}.resources.limits.memory", "kubectl-edit"),
        (web, "added", 'metadata.annotations["example.com/paged-by"]', "kubectl-annotate"),
        ("ConfigMap/shop-config", "changed", "data.LOG_LEVEL", "kubectl-edit"),
        ("Secret/shop-credentials", "changed", "data.password", "kubectl-patch"),
    }
    assert [str(i) for i in report.missing] == ["Service/shop-metrics"]
    assert [str(i) for i in report.extra] == ["ConfigMap/shop-legacy"]
    assert report.has_drift


def test_values_are_reported_as_each_side_wrote_them(rules):
    report = check(desired(), cluster("drifted"), "shop", "shop", rules)
    web = next(o for o in report.objects if str(o.identity) == "Deployment/shop-web")
    by_path = {format_path(d.path): d for d in web.differences}
    assert (by_path["spec.replicas"].chart, by_path["spec.replicas"].live) == (2, 4)
    memory = by_path["spec.template.spec.containers[name=web].resources.limits.memory"]
    assert (memory.chart, memory.live) == ("128Mi", "256Mi")
    debug = by_path["spec.template.spec.containers[name=web].env[name=DEBUG]"]
    assert debug.live == {"name": "DEBUG", "value": "true"}
    assert debug.chart is None


def test_secret_values_never_appear(rules):
    report = check(desired(), cluster("drifted"), "shop", "shop", rules)
    secret = next(o for o in report.objects if o.identity.kind == "Secret")
    [d] = secret.differences
    assert d.hidden and d.chart == HIDDEN and d.live == HIDDEN
    text = repr(report)
    for leaked in [
        "changed-at-2am",
        "not-a-real-password",
        "Y2hhbmdlZC1hdC0yYW0",
        "bm90LWEtcmVhbC1wYXNzd29yZA",
    ]:
        assert leaked not in text


def test_without_the_hpa_rule_the_autoscaled_replicas_show(rules):
    report = check(desired(), cluster("clean"), "shop", "shop", rules, hpa_rule=False)
    assert summary(report) == {("Deployment/shop-worker", "changed", "spec.replicas", "k3s")}


def test_an_hpa_only_in_the_cluster_still_counts(rules):
    chart_without_hpa = [r for r in desired() if r.identity.kind != "HorizontalPodAutoscaler"]
    report = check(chart_without_hpa, cluster("clean"), "shop", "shop", rules)
    assert summary(report) == set()


def test_an_hpa_for_another_deployment_hides_nothing(rules):
    reader = cluster("clean", drop={"hpa-shop-worker"})
    chart = [r for r in desired() if r.identity.kind != "HorizontalPodAutoscaler"]
    other = {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": "other", "namespace": "shop"},
        "spec": {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "other"}
        },
    }
    report = check([*chart, Resource(identity_of(other), other)], reader, "shop", "shop", rules)
    assert ("Deployment/shop-worker", "changed", "spec.replicas", "k3s") in summary(report)


def test_ignored_differences_are_kept_aside_with_their_reason(rules):
    rule = Rule(parse_path('metadata.annotations["example.com/*"]'), reason="our paging tool")
    report = check(desired(), cluster("drifted"), "shop", "shop", rules.with_ignore([rule]))
    web = next(o for o in report.objects if str(o.identity) == "Deployment/shop-web")
    assert 'metadata.annotations["example.com/paged-by"]' not in {
        format_path(d.path) for d in web.differences
    }
    assert [(format_path(i.path), i.reason) for i in web.ignored] == [
        ('metadata.annotations["example.com/paged-by"]', "our paging tool")
    ]


def test_without_managed_fields_the_defaults_list_takes_over(rules):
    stripped = []
    for path in sorted((FIXTURES / "drifted").glob("*.yaml")):
        body = yaml.safe_load(path.read_text(encoding="utf-8"))
        body["metadata"].pop("managedFields", None)
        stripped.append(body)
    report = check(desired(), StaticReader(stripped), "shop", "shop", rules)
    paths = {(c, p) for _, c, p, _ in summary(report)}
    assert ("added", "spec.template.spec.containers[name=web].env[name=DEBUG]") in paths
    assert ("added", 'metadata.annotations["example.com/paged-by"]') in paths
    # No server default turns into noise, and nothing can be attributed.
    assert not any("imagePullPolicy" in p or "dnsPolicy" in p for _, p in paths)
    assert all(owner is None for *_, owner in summary(report))


def deployment(**spec):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "web", "namespace": "ns"},
        "spec": spec,
    }


def compare(rules, want, have):
    return Comparer(rules).compare(
        Resource(identity_of(want), want), Resource(identity_of(have), have)
    )


def test_a_field_removed_from_the_cluster(rules):
    result = compare(rules, deployment(replicas=2, paused=True), deployment(replicas=2))
    [d] = result.differences
    assert (d.change, format_path(d.path), d.chart, d.owners) == (
        "removed",
        "spec.paused",
        True,
        (),
    )


def test_quantities_compare_by_amount_only_where_they_are_quantities(rules):
    want = deployment(
        template={"spec": {"containers": [{"name": "w", "resources": {"limits": {"cpu": 0.5}}}]}}
    )
    have = deployment(
        template={"spec": {"containers": [{"name": "w", "resources": {"limits": {"cpu": "500m"}}}]}}
    )
    assert compare(rules, want, have).differences == []
    want = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "c"},
        "data": {"SIZE": "1k"},
    }
    have = copy.deepcopy(want) | {"data": {"SIZE": "1000"}}
    [d] = compare(rules, want, have).differences
    assert (d.chart, d.live) == ("1k", "1000")


def test_a_type_change_is_a_change(rules):
    [d] = compare(rules, deployment(selector={"a": "b"}), deployment(selector="oops")).differences
    assert d.change == "changed"


def test_autoscaled_targets():
    hpa = {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": "h", "namespace": "ns"},
        "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "StatefulSet", "name": "db"}},
    }
    [target] = autoscaled_targets([Resource(identity_of(hpa), hpa)])
    assert (target.group, target.kind, target.namespace, target.name) == (
        "apps",
        "StatefulSet",
        "ns",
        "db",
    )


def test_extras_need_this_release_in_this_namespace(rules):
    def legacy(name, release, ns):
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": name,
                "namespace": "shop",
                "annotations": {
                    "meta.helm.sh/release-name": release,
                    "meta.helm.sh/release-namespace": ns,
                },
            },
        }

    reader = cluster(
        "clean",
        extra_objects=[
            legacy("ours", "shop", "shop"),
            legacy("other-release", "blog", "shop"),
            legacy("other-namespace", "shop", "staging"),
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "unlabelled", "namespace": "shop"},
            },
        ],
    )
    report = check(desired(), reader, "shop", "shop", rules)
    assert [str(i) for i in report.extra] == ["ConfigMap/ours"]


def test_reports_are_json_safe(rules):
    report = check(desired(), cluster("drifted"), "shop", "shop", rules)
    for obj in report.objects:
        for d in obj.differences:
            json.dumps([d.chart, d.live])
