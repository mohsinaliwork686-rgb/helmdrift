"""Compare what should be running with what is.

`compare` diffs one object. `check` pairs every object the chart renders with the cluster's copy,
and also finds the ones that are missing from the cluster, and the extra ones the cluster holds
for this release that the chart no longer renders.

A field only the cluster has is reported as added only when a tool other than the chart's owns
it, because a field nobody owns is a default the API server filled in. Objects without
managedFields fall back to the defaults list in the rules.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from helmdrift.live import ClusterReader
from helmdrift.normalise import Tree, leaves, normalise
from helmdrift.ownership import Owner, Ownership
from helmdrift.paths import Field, Index, Item, Path, Segment
from helmdrift.quantity import same_quantity
from helmdrift.render import Identity, Resource, group_of, identity_of
from helmdrift.rules import RuleSet

HIDDEN = "(value hidden)"
REPLICAS: Path = (Field("spec"), Field("replicas"))

#: Fields whose values are Kubernetes quantities, compared by amount rather than as text.
_QUANTITY_PARENTS = frozenset(
    {
        "requests",
        "limits",
        "hard",
        "used",
        "capacity",
        "allocatable",
        "overhead",
        "min",
        "max",
        "default",
        "defaultRequest",
        "maxLimitRequestRatio",
    }
)
_QUANTITY_FIELDS = frozenset({"sizeLimit", "storage"})
_MISSING = object()

Change = Literal["changed", "added", "removed"]


@dataclass(frozen=True)
class Difference:
    change: Change
    path: Path
    chart: Any = None
    live: Any = None
    owners: tuple[Owner, ...] = ()
    hidden: bool = False


@dataclass(frozen=True)
class Ignored:
    path: Path
    reason: str


@dataclass
class ObjectDiff:
    identity: Identity
    differences: list[Difference] = field(default_factory=list)
    ignored: list[Ignored] = field(default_factory=list)


@dataclass
class DriftReport:
    target: str
    objects: list[ObjectDiff]
    missing: list[Identity]
    extra: list[Identity]

    @property
    def drifted(self) -> list[ObjectDiff]:
        return [o for o in self.objects if o.differences]

    @property
    def has_drift(self) -> bool:
        return bool(self.drifted or self.missing or self.extra)


class Comparer:
    def __init__(
        self,
        rules: RuleSet,
        chart_managers: Sequence[str] = ("helm",),
        autoscaled: Iterable[Identity] = (),
    ) -> None:
        self.rules = rules
        self.chart_managers = frozenset(chart_managers)
        self.autoscaled = frozenset(autoscaled)

    def compare(self, desired: Resource, live: Resource) -> ObjectDiff:
        state = _Walk(
            comparer=self,
            identity=desired.identity,
            ownership=Ownership((live.body.get("metadata") or {}).get("managedFields")),
            secret=desired.identity.kind == "Secret" and desired.identity.group == "",
        )
        state.walk(normalise(desired, self.rules), normalise(live, self.rules), ())
        return ObjectDiff(desired.identity, state.differences, state.ignored)


@dataclass
class _Walk:
    comparer: Comparer
    identity: Identity
    ownership: Ownership
    secret: bool
    differences: list[Difference] = field(default_factory=list)
    ignored: list[Ignored] = field(default_factory=list)

    def walk(self, want: Tree, have: Tree, path: Path) -> None:
        keys: list[Segment] = list(want) + [k for k in have if k not in want]
        for key in keys:
            child = (*path, key)
            w, h = want.get(key, _MISSING), have.get(key, _MISSING)
            if isinstance(w, dict) and isinstance(h, dict):
                self.walk(w, h, child)
            elif w is _MISSING:
                self.live_only(h, child)
            elif h is _MISSING:
                self.record("removed", child, chart=w)
            elif not _equal(child, w, h):
                self.record("changed", child, chart=w, live=h)

    def live_only(self, node: Any, path: Path) -> None:
        found = list(leaves(node, path)) if isinstance(node, dict) else [(path, node)]
        reportable = []
        for leaf, value in found:
            if self.skip(leaf, value) or self.server_set(leaf, value):
                continue
            reportable.append((leaf, value))
        if not reportable:
            return
        whole_entry = isinstance(node, dict) and isinstance(path[-1], Item | Index)
        if whole_entry and len(reportable) == len(found):
            self.add(path, _plain(node))  # a list entry someone added: one difference
        else:
            for leaf, value in reportable:
                self.add(leaf, value)

    def server_set(self, path: Path, value: Any) -> bool:
        """True when a live-only field was set by the server or the chart, not by someone else."""
        if self.ownership.known:
            owners = self.ownership.owners_within(path)
            return all(o.manager in self.comparer.chart_managers for o in owners)
        return self.comparer.rules.is_default(self.identity, path, value)

    def skip(self, path: Path, value: Any) -> bool:
        if path == REPLICAS and self.identity in self.comparer.autoscaled:
            self.ignored.append(Ignored(path, "set by a HorizontalPodAutoscaler"))
            return True
        rule = self.comparer.rules.ignored_by(self.identity, path, value)
        if rule is not None:
            self.ignored.append(Ignored(path, rule.reason or f"ignored by {rule.source}"))
            return True
        return False

    def add(self, path: Path, value: Any) -> None:
        owners = self.ownership.owners(path) or self.ownership.owners_within(path)
        self.differences.append(self._difference("added", path, None, value, owners))

    def record(self, change: Change, path: Path, chart: Any = None, live: Any = None) -> None:
        if self.skip(path, live if change == "changed" else chart):
            return
        owners = self.ownership.owners(path) or self.ownership.owners_within(path)
        if change == "removed":
            owners = []  # whoever removed it leaves no record behind
        self.differences.append(self._difference(change, path, chart, live, owners))

    def _difference(self, change: Change, path: Path, chart: Any, live: Any, owners) -> Difference:
        hidden = self.secret and bool(path) and path[0] in (Field("data"), Field("stringData"))
        if hidden:
            chart = None if chart is None else HIDDEN
            live = None if live is None else HIDDEN
        managers = self.comparer.chart_managers
        ordered = sorted(owners, key=lambda o: (o.manager in managers, o.manager))
        return Difference(change, path, chart, live, tuple(ordered), hidden)


def _equal(path: Path, want: Any, have: Any) -> bool:
    if want == have:
        return True
    if _is_quantity_field(path):
        return same_quantity(want, have) is True
    return False


def _is_quantity_field(path: Path) -> bool:
    last = path[-1] if path else None
    parent = path[-2] if len(path) > 1 else None
    if isinstance(last, Field) and last.name in _QUANTITY_FIELDS:
        return True
    return isinstance(parent, Field) and parent.name in _QUANTITY_PARENTS


def _plain(node: Any) -> Any:
    if isinstance(node, dict):
        return {(k.name if isinstance(k, Field) else str(k)): _plain(v) for k, v in node.items()}
    if isinstance(node, tuple):
        return list(node)
    return node


def autoscaled_targets(resources: Iterable[Resource]) -> set[Identity]:
    """The objects a HorizontalPodAutoscaler resizes, so their replica counts are not drift."""
    targets = set()
    for resource in resources:
        if resource.identity.kind != "HorizontalPodAutoscaler":
            continue
        ref = (resource.body.get("spec") or {}).get("scaleTargetRef") or {}
        if ref.get("kind") and ref.get("name"):
            targets.add(
                Identity(
                    group_of(ref.get("apiVersion", "")),
                    ref["kind"],
                    resource.identity.namespace,
                    ref["name"],
                )
            )
    return targets


def check(
    desired: list[Resource],
    reader: ClusterReader,
    release: str,
    namespace: str,
    rules: RuleSet,
    chart_managers: Sequence[str] = ("helm",),
    hpa_rule: bool = True,
) -> DriftReport:
    """Compare a release's desired objects with the cluster's."""
    desired = [_scoped(resource, reader) for resource in desired]
    live: dict[Identity, Resource | None] = {}
    for resource in desired:
        ident = resource.identity
        body = reader.get(resource.api_version, ident.kind, ident.name, ident.namespace)
        live[ident] = None if body is None else Resource(ident, body)

    autoscaled: set[Identity] = set()
    if hpa_rule:
        cluster_hpas = [
            Resource(identity_of(body), body)
            for body in reader.list("autoscaling/v2", "HorizontalPodAutoscaler", namespace)
        ]
        autoscaled = autoscaled_targets([*desired, *cluster_hpas])

    comparer = Comparer(rules, chart_managers, autoscaled)
    objects = [
        comparer.compare(resource, live[resource.identity])
        for resource in desired
        if live[resource.identity] is not None
    ]
    missing = [resource.identity for resource in desired if live[resource.identity] is None]
    extra = _extras(desired, reader, release, namespace)
    return DriftReport(reader.describe(), objects, missing, extra)


def _scoped(resource: Resource, reader: ClusterReader) -> Resource:
    """Drop the namespace from kinds the cluster says are cluster-wide (custom resources)."""
    ident = resource.identity
    if ident.namespace and not reader.namespaced(resource.api_version, ident.kind):
        resource.body.get("metadata", {}).pop("namespace", None)
        return Resource(Identity(ident.group, ident.kind, None, ident.name), resource.body)
    return resource


def _extras(
    desired: list[Resource], reader: ClusterReader, release: str, namespace: str
) -> list[Identity]:
    """Objects in the cluster that belong to this release but that the chart does not render."""
    wanted = {resource.identity for resource in desired}
    kinds = {(resource.api_version, resource.identity.kind) for resource in desired}
    extra = set()
    for api_version, kind in kinds:
        scope = namespace if reader.namespaced(api_version, kind) else None
        for body in reader.list(api_version, kind, scope):
            annotations = (body.get("metadata") or {}).get("annotations") or {}
            if annotations.get("meta.helm.sh/release-name") != release:
                continue
            if annotations.get("meta.helm.sh/release-namespace", namespace) != namespace:
                continue
            ident = identity_of(body)
            if ident not in wanted:
                extra.add(ident)
    return sorted(extra)
