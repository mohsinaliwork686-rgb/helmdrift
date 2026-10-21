"""Who last set each field, read from an object's `managedFields`.

Kubernetes records, per object, a list of managers: the tool that wrote (`helm`, `kubectl-edit`,
`kubectl-set`...), how (`Apply` or `Update`), through which subresource (`scale`, `status`), when
(not always recorded), and the fields it owns, in a compact format:

    f:spec:                         a field called spec
      f:replicas: {}                ...and the field replicas under it, owned
    k:{"name":"web"}:               the list entry whose name is web
      .: {}                         ...owned as an entry in its own right
    k:{"containerPort":80,"protocol":"TCP"}:   an entry picked by two keys at once
    i:0                             the list entry at position 0
    v:"value"                       the entry of a list of plain values equal to "value"

Checked on a real cluster on 20 October 2026: a value the API server filled in by default belongs
to no manager, and a field changed or added by hand belongs to the tool that did it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from helmdrift.paths import Field, Index, Item, Path


@dataclass(frozen=True)
class Owner:
    manager: str
    operation: str
    subresource: str | None = None
    #: When the manager last wrote to the object; not to this field, and not always recorded.
    time: datetime | None = None

    def describe(self) -> str:
        via = f" ({self.subresource})" if self.subresource else ""
        when = f", last write {self.time:%Y-%m-%d %H:%M} UTC" if self.time else ""
        return f"{self.manager}{via}{when}"


@dataclass(frozen=True)
class _Entry:
    owner: Owner
    fields: dict[str, Any]


class Ownership:
    def __init__(self, managed_fields: list[dict[str, Any]] | None) -> None:
        self._entries = [
            _Entry(_owner(entry), entry.get("fieldsV1") or {})
            for entry in managed_fields or []
            if entry.get("fieldsType", "FieldsV1") == "FieldsV1"
        ]

    @property
    def known(self) -> bool:
        """False when the object carries no managedFields, so ownership cannot be told apart."""
        return bool(self._entries)

    def owners(self, path: Path) -> list[Owner]:
        """Every manager that owns exactly this field (several, when appliers share a value)."""
        return [e.owner for e in self._entries if _owns(e.fields, path, exactly=True)]

    def owners_within(self, path: Path) -> list[Owner]:
        """Managers owning this field or anything under it.

        Used for fields only the cluster has: a variable added by hand is owned as an entry
        (`k:{"name":"DEBUG"}` with `.`) and through its subfields. Owning the entry *above* a path
        does not count: Helm owns the `web` container as an entry, which says nothing about who
        added a variable inside it.
        """
        return [e.owner for e in self._entries if _owns(e.fields, path, exactly=False)]


def _owner(entry: dict[str, Any]) -> Owner:
    time = entry.get("time")
    parsed = None
    if isinstance(time, str):
        try:
            parsed = datetime.fromisoformat(time.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    elif isinstance(time, datetime):
        parsed = time
    return Owner(
        manager=str(entry.get("manager", "unknown")),
        operation=str(entry.get("operation", "")),
        subresource=entry.get("subresource") or None,
        time=parsed,
    )


def _owns(fields: dict[str, Any], path: Path, *, exactly: bool) -> bool:
    """Walk the path through one manager's fields.

    Exactly: the path itself is owned, as a leaf or an entry marked with `.`. Otherwise: the path
    appears at all, so it or something under it is owned.
    """
    node: Any = fields
    for segment in path:
        if not isinstance(node, dict):
            return False
        child_key = _child(node, segment)
        if child_key is None:
            return False
        node = node[child_key]
    if exactly:
        return node == {} or (isinstance(node, dict) and "." in node)
    return True


def _child(node: dict[str, Any], segment: Any) -> str | None:
    if isinstance(segment, Field):
        key = f"f:{segment.name}"
        return key if key in node else None
    if isinstance(segment, Index):
        key = f"i:{segment.position}"
        return key if key in node else None
    if isinstance(segment, Item):
        for key in node:
            if not key.startswith("k:"):
                continue
            try:
                selector = json.loads(key[2:])
            except json.JSONDecodeError:
                continue
            if segment.key in selector and str(selector[segment.key]) == segment.value:
                return key
    return None
