"""Rules as data: the built-in ones in defaults.yaml, and the user's ignore file.

Every rule has the same shape, built in or not:

    - path: metadata.annotations["example.com/last-sync"]   # required; the report's syntax
      kind: Deployment            # optional; a kind or a list of kinds
      name: worker-*              # optional; a glob on the object's name
      value: 30                   # optional; match only this value (or any of a list of values)
      reason: written by our sync job

A rule matches a path or anything under it. A malformed rule in a user's file fails the run with
its file and line, rather than being skipped, because a rule that silently does nothing is worse
than no rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from importlib.resources import files
from pathlib import Path as FilePath
from typing import Any

import yaml

from helmdrift.paths import Path, PathError, covers, parse_path
from helmdrift.render import Identity

_RULE_KEYS = {"path", "kind", "name", "value", "reason"}
_ANY = object()


class RulesError(Exception):
    """A rules file could not be used."""


@dataclass(frozen=True)
class Rule:
    pattern: Path
    kinds: frozenset[str] | None = None
    name: str | None = None
    #: When set, the rule only matches a field holding one of these values.
    values: tuple[Any, ...] | None = None
    reason: str | None = None
    source: str = "built-in"

    def matches(self, identity: Identity, path: Path, value: Any = _ANY) -> bool:
        if self.kinds is not None and identity.kind not in self.kinds:
            return False
        if self.name is not None and not fnmatchcase(identity.name, self.name):
            return False
        if self.values is not None and (value is _ANY or not _value_in(value, self.values)):
            return False
        return covers(self.pattern, path)


def _value_in(value: Any, allowed: tuple[Any, ...]) -> bool:
    # bool is an int in Python, so compare types too: a rule for 0 must not match False.
    return any(type(value) is type(option) and value == option for option in allowed)


@dataclass
class RuleSet:
    bookkeeping: list[Rule] = field(default_factory=list)
    defaults: list[Rule] = field(default_factory=list)
    ignore: list[Rule] = field(default_factory=list)
    list_keys: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def is_bookkeeping(self, identity: Identity, path: Path) -> bool:
        return _first(self.bookkeeping, identity, path) is not None

    def is_default(self, identity: Identity, path: Path, value: Any = _ANY) -> bool:
        """True when this live-only field holds what the API server would have filled in."""
        return _first(self.defaults, identity, path, value) is not None

    def ignored_by(self, identity: Identity, path: Path, value: Any = _ANY) -> Rule | None:
        return _first(self.ignore, identity, path, value)

    def with_ignore(self, rules: list[Rule]) -> RuleSet:
        return RuleSet(self.bookkeeping, self.defaults, self.ignore + rules, self.list_keys)


def _first(rules: list[Rule], identity: Identity, path: Path, value: Any = _ANY) -> Rule | None:
    return next((rule for rule in rules if rule.matches(identity, path, value)), None)


def builtin_rules() -> RuleSet:
    text = files("helmdrift").joinpath("defaults.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    return RuleSet(
        bookkeeping=[_rule(entry, "built-in") for entry in data["bookkeeping"]],
        defaults=[_rule(entry, "built-in") for entry in data["defaults"]],
        list_keys={name: tuple(keys) for name, keys in data["list_keys"].items()},
    )


def load_ignore_file(path: str | FilePath) -> list[Rule]:
    """Read a user's ignore file. Every problem names the file and line it is on."""
    path = FilePath(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RulesError(f"{path}: cannot read the ignore file ({error.strerror})") from None
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as error:
        raise RulesError(f"{path}: not valid YAML: {error}") from None
    if root is None:
        return []
    if not isinstance(root, yaml.MappingNode):
        raise RulesError(
            f"{path}:{root.start_mark.line + 1}: expected a mapping with an 'ignore' list"
        )

    entries = None
    for key_node, value_node in root.value:
        if key_node.value != "ignore":
            raise RulesError(
                f"{path}:{key_node.start_mark.line + 1}: unknown key {key_node.value!r}; "
                "the file holds one key, 'ignore'"
            )
        entries = value_node
    if entries is None or (isinstance(entries, yaml.ScalarNode) and entries.value in ("", "null")):
        return []
    if not isinstance(entries, yaml.SequenceNode):
        raise RulesError(f"{path}:{entries.start_mark.line + 1}: 'ignore' must be a list of rules")

    rules = []
    for node in entries.value:
        where = f"{path}:{node.start_mark.line + 1}"
        entry = yaml.safe_load(yaml.serialize(node))
        rules.append(_rule(entry, where))
    return rules


def _rule(entry: Any, where: str) -> Rule:
    if not isinstance(entry, dict):
        raise RulesError(f"{where}: a rule must be a mapping with at least a 'path'")
    unknown = sorted(set(entry) - _RULE_KEYS)
    if unknown:
        raise RulesError(
            f"{where}: unknown key {unknown[0]!r}; a rule takes path, kind, name, reason"
        )
    if not isinstance(entry.get("path"), str):
        raise RulesError(f"{where}: a rule needs a 'path'")
    try:
        pattern = parse_path(entry["path"])
    except PathError as error:
        raise RulesError(f"{where}: {error}") from None

    kinds = entry.get("kind")
    if isinstance(kinds, str):
        kinds = [kinds]
    if kinds is not None and not (
        isinstance(kinds, list) and all(isinstance(k, str) for k in kinds)
    ):
        raise RulesError(f"{where}: 'kind' must be a kind or a list of kinds")
    name = entry.get("name")
    if name is not None and not isinstance(name, str):
        raise RulesError(f"{where}: 'name' must be text")
    values = None
    if "value" in entry:
        raw = entry["value"]
        values = tuple(raw) if isinstance(raw, list) else (raw,)
        if not all(isinstance(v, str | int | float | bool) for v in values):
            raise RulesError(f"{where}: 'value' must be a plain value or a list of them")
    reason = entry.get("reason")
    return Rule(
        pattern=pattern,
        kinds=frozenset(kinds) if kinds is not None else None,
        name=name,
        values=values,
        reason=str(reason) if reason is not None else None,
        source=where,
    )
