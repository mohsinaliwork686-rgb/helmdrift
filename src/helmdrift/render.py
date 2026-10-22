"""What should be running: the objects a chart renders, or the ones Helm last applied.

Both come from the `helm` binary rather than a reimplementation of its templating, which would be
wrong in a hundred small ways. This module runs it, splits the output into objects, and tidies
them: hooks and tests are dropped (they are not long-lived objects), `kind: List` is flattened,
and namespaced objects without a namespace get the release's.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml

#: Kinds that live outside any namespace. Rendering cannot ask the cluster, so this covers the
#: built-in ones; the cluster reader corrects anything else from API discovery.
CLUSTER_SCOPED = frozenset(
    {
        "APIService",
        "CSIDriver",
        "CSINode",
        "CertificateSigningRequest",
        "ClusterRole",
        "ClusterRoleBinding",
        "CustomResourceDefinition",
        "FlowSchema",
        "IngressClass",
        "MutatingWebhookConfiguration",
        "Namespace",
        "Node",
        "PersistentVolume",
        "PriorityClass",
        "PriorityLevelConfiguration",
        "RuntimeClass",
        "StorageClass",
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
        "ValidatingWebhookConfiguration",
        "VolumeAttachment",
    }
)


class RenderError(Exception):
    """Helm could not produce the objects to compare against."""


@dataclass(frozen=True, order=True)
class Identity:
    """What makes two objects "the same object": API group, kind, namespace and name."""

    group: str
    kind: str
    namespace: str | None
    name: str

    def __str__(self) -> str:
        return f"{self.kind}/{self.name}"

    def describe(self) -> str:
        where = f" in {self.namespace}" if self.namespace else ""
        group = f" ({self.group})" if self.group else ""
        return f"{self.kind}/{self.name}{group}{where}"


@dataclass
class Resource:
    identity: Identity
    body: dict[str, Any] = field(repr=False)

    @property
    def api_version(self) -> str:
        return self.body["apiVersion"]


def group_of(api_version: str) -> str:
    """The API group: "apps" for apps/v1, "" for the core group's plain v1."""
    return api_version.rpartition("/")[0]


def identity_of(body: dict[str, Any]) -> Identity:
    meta = body.get("metadata") or {}
    return Identity(
        group=group_of(body["apiVersion"]),
        kind=body["kind"],
        namespace=meta.get("namespace"),
        name=meta["name"],
    )


def parse_manifest(text: str, namespace: str) -> list[Resource]:
    """Split multi-document YAML into resources, tidied as the module docstring describes."""
    try:
        documents = list(yaml.safe_load_all(text))
    except yaml.YAMLError as error:
        raise RenderError(f"helm produced YAML that could not be read: {error}") from error

    resources = []
    for document in _flatten(documents):
        if not _is_object(document):
            continue
        annotations = (document.get("metadata") or {}).get("annotations") or {}
        if "helm.sh/hook" in annotations:
            continue
        meta = document.setdefault("metadata", {})
        if not meta.get("namespace") and document["kind"] not in CLUSTER_SCOPED:
            meta["namespace"] = namespace
        resources.append(Resource(identity_of(document), document))
    return resources


def _flatten(documents: Sequence[Any]):
    for document in documents:
        if isinstance(document, dict) and document.get("kind") == "List":
            yield from _flatten(document.get("items") or [])
        else:
            yield document


def _is_object(document: Any) -> bool:
    return (
        isinstance(document, dict)
        and isinstance(document.get("apiVersion"), str)
        and isinstance(document.get("kind"), str)
        and isinstance((document.get("metadata") or {}).get("name"), str)
    )


def helm_args(context: str | None, kubeconfig: str | None) -> list[str]:
    args = []
    if context:
        args += ["--kube-context", context]
    if kubeconfig:
        args += ["--kubeconfig", kubeconfig]
    return args


#: The helm binary, or a command that runs it (tests pass ["python", "fake_helm.py"]).
Helm = str | Sequence[str]


def _helm(helm: Helm) -> list[str]:
    return [helm] if isinstance(helm, str) else list(helm)


def render_chart(
    chart: str,
    release: str,
    namespace: str,
    values_files: Sequence[str] = (),
    set_values: Sequence[str] = (),
    helm: Helm = "helm",
) -> list[Resource]:
    """What the chart on disk renders to, with these values. Needs no cluster."""
    command = [*_helm(helm), "template", release, chart, "--namespace", namespace]
    for path in values_files:
        command += ["--values", path]
    for assignment in set_values:
        command += ["--set", assignment]
    return parse_manifest(_run(command), namespace)


def render_release(
    release: str,
    namespace: str,
    context: str | None = None,
    kubeconfig: str | None = None,
    helm: Helm = "helm",
) -> list[Resource]:
    """What Helm applied at the release's last install or upgrade."""
    command = [*_helm(helm), "get", "manifest", release, "--namespace", namespace]
    command += helm_args(context, kubeconfig)
    return parse_manifest(_run(command), namespace)


def _run(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise RenderError(f"{command[0]!r} was not found; install Helm or pass --helm") from None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        verb = next(i for i, part in enumerate(command) if part in ("template", "get"))
        what = " ".join(["helm", *command[verb : verb + (2 if command[verb] == "get" else 1)]])
        raise RenderError(f"{what} failed: {detail}")
    return result.stdout
