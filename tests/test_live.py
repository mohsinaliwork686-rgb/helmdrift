import time

import pytest

from helmdrift.live import KubeReader, LiveError, StaticReader

UNREACHABLE = """
apiVersion: v1
kind: Config
clusters:
  - name: nowhere
    cluster: {server: "https://127.0.0.1:1", insecure-skip-tls-verify: true}
users:
  - name: nobody
    user: {token: not-a-token}
contexts:
  - name: nowhere
    context: {cluster: nowhere, user: nobody}
current-context: nowhere
"""


def test_an_unreachable_cluster_is_a_clear_error_not_a_traceback(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text(UNREACHABLE)
    started = time.monotonic()
    with pytest.raises(LiveError) as caught:
        KubeReader(kubeconfig=str(kubeconfig))
    assert "could not reach the cluster (context nowhere)" in str(caught.value)
    assert time.monotonic() - started < 60


def test_a_missing_kubeconfig_is_a_clear_error(tmp_path):
    with pytest.raises(LiveError, match="could not load the kubeconfig"):
        KubeReader(kubeconfig=str(tmp_path / "does-not-exist"))


def test_static_reader_matches_kind_group_and_namespace():
    objects = [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "a", "namespace": "x"},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "a", "namespace": "y"},
        },
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "a", "namespace": "x"}},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "r"},
        },
    ]
    reader = StaticReader(objects)
    assert reader.get("apps/v1", "Deployment", "a", "x")["metadata"]["namespace"] == "x"
    assert reader.get("apps/v1", "Deployment", "missing", "x") is None
    assert len(reader.list("apps/v1", "Deployment", None)) == 2
    assert reader.get("rbac.authorization.k8s.io/v1", "ClusterRole", "r", None) is not None
    assert reader.namespaced("v1", "ConfigMap") and not reader.namespaced("v1", "Namespace")
