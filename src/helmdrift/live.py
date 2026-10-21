"""What is running: objects read from the cluster. Read-only by construction.

`ClusterReader` is the interface the rest of the tool uses. `KubeReader` implements it with the
official client's dynamic API, so any kind works, custom resources included, and only ever calls
`get` and `list`. `StaticReader` serves objects already in hand: tests use it, and so can anyone
checking a saved snapshot of a cluster.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from helmdrift.render import CLUSTER_SCOPED, group_of


class LiveError(Exception):
    """The cluster could not be read."""


class ClusterReader(Protocol):
    def get(self, api_version: str, kind: str, name: str, namespace: str | None) -> dict | None:
        """One object, or None when it does not exist."""

    def list(self, api_version: str, kind: str, namespace: str | None) -> list[dict]:
        """Every object of a kind, in one namespace or (for None) the whole cluster."""

    def namespaced(self, api_version: str, kind: str) -> bool:
        """Whether objects of this kind live in a namespace."""

    def describe(self) -> str:
        """Which cluster this reads, for the first line of a report."""


class KubeReader:
    def __init__(self, context: str | None = None, kubeconfig: str | None = None) -> None:
        from kubernetes import config, dynamic
        from kubernetes.config.config_exception import ConfigException

        try:
            api_client = config.new_client_from_config(config_file=kubeconfig, context=context)
        except (ConfigException, OSError) as error:
            raise LiveError(f"could not load the kubeconfig: {error}") from None
        self._context = context or _current_context(kubeconfig)
        try:
            # Building the client asks the API server what it serves, so this is where an
            # unreachable or still-starting cluster first shows up.
            self._client = dynamic.DynamicClient(api_client)
        except Exception as error:
            raise LiveError(
                f"could not reach the cluster ({self.describe()}): {_reason(error)}"
            ) from None
        self._resources: dict[tuple[str, str], Any] = {}

    def describe(self) -> str:
        return f"context {self._context}" if self._context else "the current context"

    def _resource(self, api_version: str, kind: str) -> Any | None:
        from kubernetes.dynamic.exceptions import ResourceNotFoundError

        key = (api_version, kind)
        if key not in self._resources:
            try:
                self._resources[key] = self._client.resources.get(
                    api_version=api_version, kind=kind
                )
            except ResourceNotFoundError:
                self._resources[key] = None
            except Exception as error:  # discovery talks to the API server
                raise LiveError(f"could not look up {kind} ({api_version}): {error}") from None
        return self._resources[key]

    def namespaced(self, api_version: str, kind: str) -> bool:
        resource = self._resource(api_version, kind)
        return kind not in CLUSTER_SCOPED if resource is None else bool(resource.namespaced)

    def get(self, api_version: str, kind: str, name: str, namespace: str | None) -> dict | None:
        from kubernetes.dynamic.exceptions import NotFoundError

        resource = self._resource(api_version, kind)
        if resource is None:
            return None  # the kind itself is not installed, so neither is the object
        try:
            found = resource.get(name=name, namespace=namespace if resource.namespaced else None)
        except NotFoundError:
            return None
        except Exception as error:
            raise LiveError(f"could not read {kind}/{name}: {_reason(error)}") from None
        return found.to_dict()

    def list(self, api_version: str, kind: str, namespace: str | None) -> list[dict]:
        resource = self._resource(api_version, kind)
        if resource is None:
            return []
        try:
            found = resource.get(namespace=namespace if resource.namespaced else None)
        except Exception as error:
            raise LiveError(f"could not list {kind}: {_reason(error)}") from None
        return [item.to_dict() for item in found.items]


class StaticReader:
    """Serves a fixed set of objects, as if they were a cluster."""

    def __init__(self, objects: Iterable[dict], description: str = "a static snapshot") -> None:
        self._objects = list(objects)
        self._description = description

    def describe(self) -> str:
        return self._description

    def namespaced(self, api_version: str, kind: str) -> bool:
        return kind not in CLUSTER_SCOPED

    def get(self, api_version: str, kind: str, name: str, namespace: str | None) -> dict | None:
        for body in self.list(api_version, kind, namespace):
            if body["metadata"]["name"] == name:
                return body
        return None

    def list(self, api_version: str, kind: str, namespace: str | None) -> list[dict]:
        group = group_of(api_version)
        return [
            body
            for body in self._objects
            if body.get("kind") == kind
            and group_of(body.get("apiVersion", "")) == group
            and (namespace is None or body.get("metadata", {}).get("namespace") == namespace)
        ]


def _reason(error: Exception) -> str:
    status = getattr(error, "status", None)
    if status == 403:
        return "forbidden; helmdrift needs get and list on this kind (see the README's Role)"
    if status == 401:
        return "not authenticated; check the kubeconfig"
    if status == 503:
        return "the API server is not ready yet"
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__


def _current_context(kubeconfig: str | None) -> str | None:
    try:
        from kubernetes import config

        _, active = config.list_kube_config_contexts(config_file=kubeconfig)
        return active.get("name")
    except Exception:
        return None
