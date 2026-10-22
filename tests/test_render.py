import shutil
import sys
from pathlib import Path

import pytest

from helmdrift.render import (
    Identity,
    RenderError,
    group_of,
    parse_manifest,
    render_chart,
    render_release,
)

FIXTURES = Path(__file__).parent / "fixtures" / "shop"
CHART = Path(__file__).parents[1] / "examples" / "shop"


def test_the_real_manifest_parses_into_its_objects():
    resources = parse_manifest((FIXTURES / "manifest.yaml").read_text(), namespace="shop")
    assert sorted(str(r.identity) for r in resources) == [
        "ConfigMap/shop-config",
        "Deployment/shop-web",
        "Deployment/shop-worker",
        "HorizontalPodAutoscaler/shop-worker",
        "Secret/shop-credentials",
        "Service/shop-metrics",
        "Service/shop-web",
        "ServiceAccount/shop",
    ]
    web = next(
        r for r in resources if r.identity.name == "shop-web" and r.identity.kind == "Deployment"
    )
    assert web.identity == Identity("apps", "Deployment", "shop", "shop-web")


def test_empty_and_comment_only_documents_are_skipped():
    text = """
---
# just a comment
---
---
apiVersion: v1
kind: ConfigMap
metadata: {name: one}
---
null
"""
    assert [r.identity.name for r in parse_manifest(text, "ns")] == ["one"]


def test_hooks_and_tests_are_dropped():
    text = """
apiVersion: batch/v1
kind: Job
metadata:
  name: migrate
  annotations: {helm.sh/hook: pre-upgrade}
---
apiVersion: v1
kind: Pod
metadata:
  name: test-connection
  annotations: {helm.sh/hook: test}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: kept}
"""
    assert [r.identity.name for r in parse_manifest(text, "ns")] == ["kept"]


def test_lists_are_flattened():
    text = """
apiVersion: v1
kind: List
items:
  - {apiVersion: v1, kind: ConfigMap, metadata: {name: a}}
  - {apiVersion: v1, kind: ConfigMap, metadata: {name: b}}
"""
    assert [r.identity.name for r in parse_manifest(text, "ns")] == ["a", "b"]


def test_namespaces_are_filled_only_for_namespaced_kinds():
    text = """
apiVersion: v1
kind: ConfigMap
metadata: {name: here}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: elsewhere, namespace: other}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: {name: reader}
"""
    found = {r.identity.name: r.identity.namespace for r in parse_manifest(text, "prod")}
    assert found == {"here": "prod", "elsewhere": "other", "reader": None}


def test_documents_that_are_not_objects_are_ignored():
    text = "just a string\n---\n- a\n- list\n---\nkind: Missing\nmetadata: {name: x}\n"
    assert parse_manifest(text, "ns") == []


def test_broken_yaml_is_a_render_error():
    with pytest.raises(RenderError, match="could not be read"):
        parse_manifest("a: [unclosed", "ns")


def test_group_of():
    assert group_of("apps/v1") == "apps"
    assert group_of("v1") == ""
    assert group_of("rbac.authorization.k8s.io/v1") == "rbac.authorization.k8s.io"


def test_a_missing_helm_binary_is_explained():
    with pytest.raises(RenderError, match="was not found"):
        render_chart(str(CHART), "shop", "shop", helm="definitely-not-helm")


def fake_helm(tmp_path, stdout="", stderr="", code=0):
    """A stand-in `helm`, run by the current Python on every OS, that records its arguments."""
    script = tmp_path / "fake_helm.py"
    log = tmp_path / "args.txt"
    script.write_text(
        "import sys\n"
        f"open({str(log)!r}, 'w').write('\\n'.join(sys.argv[1:]))\n"
        f"sys.stdout.write({stdout!r}); sys.stderr.write({stderr!r}); sys.exit({code})\n"
    )
    return [sys.executable, str(script)], log


def test_render_chart_passes_values_and_sets(tmp_path):
    helm, log = fake_helm(tmp_path, stdout="apiVersion: v1\nkind: ConfigMap\nmetadata: {name: c}\n")
    resources = render_chart(
        "./chart", "api", "prod", ["base.yaml", "prod.yaml"], ["a=1"], helm=helm
    )
    assert [r.identity.name for r in resources] == ["c"]
    assert log.read_text().splitlines() == [
        "template",
        "api",
        "./chart",
        "--namespace",
        "prod",
        "--values",
        "base.yaml",
        "--values",
        "prod.yaml",
        "--set",
        "a=1",
    ]


def test_render_release_uses_the_given_cluster(tmp_path):
    helm, log = fake_helm(tmp_path, stdout="")
    render_release("api", "prod", context="prod-eu", kubeconfig="/tmp/kc", helm=helm)
    assert log.read_text().splitlines() == [
        "get",
        "manifest",
        "api",
        "--namespace",
        "prod",
        "--kube-context",
        "prod-eu",
        "--kubeconfig",
        "/tmp/kc",
    ]


def test_helm_failing_carries_its_message(tmp_path):
    helm, _ = fake_helm(tmp_path, stderr="Error: release: not found", code=1)
    with pytest.raises(RenderError, match="helm get manifest failed: Error: release: not found"):
        render_release("ghost", "prod", helm=helm)


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_the_sample_chart_renders_with_real_helm():
    resources = render_chart(str(CHART), "shop", "shop")
    names = sorted(str(r.identity) for r in resources)
    assert "Deployment/shop-web" in names
    assert not any("welcome" in name for name in names)  # the hook is dropped
    without_metrics = render_chart(str(CHART), "shop", "shop", set_values=["metrics.enabled=false"])
    assert "Service/shop-metrics" not in {str(r.identity) for r in without_metrics}
