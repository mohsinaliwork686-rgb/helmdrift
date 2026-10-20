"""Paths into a Kubernetes object, printed the same way the report and the ignore file write them.

    spec.template.spec.containers[name=web].image
    metadata.annotations["app.kubernetes.io/name"]
    spec.template.spec.containers[name=web].command[0]

A path is a tuple of segments:

- `Field("image")`: a key in a map. Printed bare when it is a plain word, quoted in brackets when
  it holds dots or slashes, as label and annotation keys usually do.
- `Item("name", "web")`: one entry of a list, picked by its key, so reordering a list does not
  change the path.
- `Index(0)`: one entry of a list that has no key, picked by position.

Patterns, used by the ignore file, are paths whose segments may contain `*`: `containers[*]`
matches any entry, `env[name=OTEL_*]` any variable whose name starts with `OTEL_`, and
`annotations["example.com/*"]` any annotation under that prefix. A `**` segment matches any number
of segments, so `**.imagePullPolicy` finds the field at any depth.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase

_BARE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


@dataclass(frozen=True, order=True)
class Field:
    name: str

    def __str__(self) -> str:
        return self.name if _BARE.match(self.name) else f"[{json.dumps(self.name)}]"


@dataclass(frozen=True, order=True)
class Item:
    key: str
    value: str

    def __str__(self) -> str:
        value = self.value
        if not value or any(c in value for c in '[]"=') or value != value.strip():
            value = json.dumps(value)
        return f"[{self.key}={value}]"


@dataclass(frozen=True, order=True)
class Index:
    position: int

    def __str__(self) -> str:
        return f"[{self.position}]"


Segment = Field | Item | Index
Path = tuple[Segment, ...]

#: Matches any list entry in a pattern, whether keyed or positional.
ANY_ITEM = Item("*", "*")


class PathError(ValueError):
    """A path or pattern could not be parsed."""


def format_path(path: Path) -> str:
    text = ""
    for segment in path:
        part = str(segment)
        text += part if part.startswith("[") or not text else f".{part}"
    return text


def parse_path(text: str) -> Path:
    """Read a path or pattern back from its printed form."""
    segments: list[Segment] = []
    i, n = 0, len(text)
    expect_field = True  # a bare word may only start the path or follow a dot
    while i < n:
        char = text[i]
        if char == ".":
            if not segments or i + 1 == n or text[i + 1] in ".[":
                raise PathError(f"misplaced '.' at position {i} in {text!r}")
            i += 1
            expect_field = True
            continue
        if char == "[":
            end, segment = _parse_bracket(text, i)
            segments.append(segment)
            i = end
            expect_field = False
            continue
        if not expect_field:
            raise PathError(f"expected '.' or '[' at position {i} in {text!r}")
        match = re.match(r"[A-Za-z0-9_*?-]+", text[i:])
        if not match:
            raise PathError(f"unexpected {char!r} at position {i} in {text!r}")
        segments.append(Field(match.group()))
        i += match.end()
        expect_field = False
    if not segments:
        raise PathError("a path cannot be empty")
    return tuple(segments)


def _parse_bracket(text: str, start: int) -> tuple[int, Segment]:
    i = start + 1
    if text.startswith('"', i):
        value, i = _read_json_string(text, i)
        if not text.startswith("]", i):
            raise PathError(f"expected ']' after a quoted key at position {i} in {text!r}")
        return i + 1, Field(value)
    close = text.find("]", i)
    eq = text.find("=", i)
    if eq != -1 and (close == -1 or eq < close):
        key = text[i:eq]
        if not key:
            raise PathError(f"an entry selector needs a key, at position {i} in {text!r}")
        i = eq + 1
        if text.startswith('"', i):
            value, i = _read_json_string(text, i)
        else:
            close = text.find("]", i)
            if close == -1:
                raise PathError(f"unclosed '[' at position {start} in {text!r}")
            value, i = text[i:close], close
        if not text.startswith("]", i):
            raise PathError(f"expected ']' at position {i} in {text!r}")
        return i + 1, Item(key, value)
    if close == -1:
        raise PathError(f"unclosed '[' at position {start} in {text!r}")
    inner = text[i:close]
    if inner == "*":
        return close + 1, ANY_ITEM
    if inner.isdigit():
        return close + 1, Index(int(inner))
    raise PathError(f"expected an index, '*', key=value or a quoted key, got [{inner}] in {text!r}")


def _read_json_string(text: str, start: int) -> tuple[str, int]:
    try:
        value, end = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError as error:
        raise PathError(f"bad quoted string at position {start} in {text!r}") from error
    if not isinstance(value, str):
        raise PathError(f"expected a quoted string at position {start} in {text!r}")
    return value, end


def segment_matches(pattern: Segment, segment: Segment) -> bool:
    if pattern == ANY_ITEM:
        return isinstance(segment, Item | Index)
    if isinstance(pattern, Field) and isinstance(segment, Field):
        return fnmatchcase(segment.name, pattern.name)
    if isinstance(pattern, Item) and isinstance(segment, Item):
        return pattern.key == segment.key and fnmatchcase(segment.value, pattern.value)
    if isinstance(pattern, Index) and isinstance(segment, Index):
        return pattern.position == segment.position
    return False


#: Matches any number of segments, including none.
ANY_DEPTH = Field("**")


def covers(pattern: Path, path: Path) -> bool:
    """True when the pattern matches the path or one of its parents.

    Ignoring `metadata.annotations` ignores every annotation under it.
    """
    if not pattern:
        return True
    head, rest = pattern[0], pattern[1:]
    if head == ANY_DEPTH:
        return any(covers(rest, path[i:]) for i in range(len(path) + 1))
    return bool(path) and segment_matches(head, path[0]) and covers(rest, path[1:])
