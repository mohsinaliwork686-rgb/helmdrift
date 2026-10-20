"""Turn an object into a tree that compares cleanly.

- Bookkeeping fields (from the rules) are removed.
- Maps become dicts keyed by `Field`. Lists of maps become dicts keyed by `Item` when the list has
  a key in the rules (containers by name, volume mounts by path), or by `Index` when it has none.
  Lists of plain values (a command, its arguments) stay whole, as tuples: inserting one argument
  is one change, not a shift of every argument after it.
- Empty values (`null`, `{}`, `[]`) count as absent, on both sides.
- A Secret's `stringData` becomes the base64 `data` the cluster stores, which is how the API server
  treats it, so the two compare equal.

Values are left as they are. Equal-but-different forms, such as `0.5` and `500m` CPU, are decided
when comparing, so the report can still show each side the way it was written.
"""

from __future__ import annotations

import base64
import copy
from collections.abc import Iterator
from typing import Any

from helmdrift.paths import Field, Index, Item, Path, Segment
from helmdrift.render import Identity, Resource
from helmdrift.rules import RuleSet

Tree = dict[Segment, Any]


def normalise(resource: Resource, rules: RuleSet) -> Tree:
    body = copy.deepcopy(resource.body)
    if resource.identity.kind == "Secret" and resource.identity.group == "":
        _merge_string_data(body)
    tree = _convert(body, (), resource.identity, rules)
    return tree if isinstance(tree, dict) else {}


def leaves(tree: Tree, prefix: Path = ()) -> Iterator[tuple[Path, Any]]:
    """Every leaf of a normalised tree, with its full path."""
    for segment, value in tree.items():
        path = (*prefix, segment)
        if isinstance(value, dict):
            yield from leaves(value, path)
        else:
            yield path, value


def _merge_string_data(body: dict[str, Any]) -> None:
    string_data = body.pop("stringData", None) or {}
    if not string_data:
        return
    data = dict(body.get("data") or {})
    for key, value in string_data.items():
        data[key] = base64.b64encode(str(value).encode("utf-8")).decode("ascii")
    body["data"] = data


def _convert(node: Any, path: Path, identity: Identity, rules: RuleSet) -> Any:
    if isinstance(node, dict):
        out: Tree = {}
        for key, value in node.items():
            child = (*path, Field(str(key)))
            if rules.is_bookkeeping(identity, child):
                continue
            converted = _convert(value, child, identity, rules)
            if not _empty(converted):
                out[Field(str(key))] = converted
        return out
    if isinstance(node, list):
        if all(not isinstance(item, dict | list) for item in node):
            return tuple(node)
        key = _list_key(path, node, rules)
        out = {}
        for position, item in enumerate(node):
            segment: Segment = Item(key, str(item[key])) if key else Index(position)
            child = (*path, segment)
            if rules.is_bookkeeping(identity, child):
                continue
            converted = _convert(item, child, identity, rules)
            if not _empty(converted):
                out[segment] = converted
        return out
    return node


def _list_key(path: Path, items: list[Any], rules: RuleSet) -> str | None:
    """The first configured key that every entry has, as a plain value, with no repeats."""
    if not path or not isinstance(path[-1], Field):
        return None
    for key in rules.list_keys.get(path[-1].name, ()):
        values = [item.get(key) if isinstance(item, dict) else None for item in items]
        if all(isinstance(v, str | int) and not isinstance(v, bool) for v in values) and len(
            set(map(str, values))
        ) == len(values):
            return key
    return None


def _empty(value: Any) -> bool:
    return value is None or value == {} or value == ()
