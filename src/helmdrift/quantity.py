"""Kubernetes quantities: `500m` CPU, `1Gi` of memory, `1e3`.

The API server rewrites quantities into a canonical form, so a chart that says `cpu: 0.5` finds
`cpu: 500m` in the cluster. Comparing them as text would report drift that is not there; comparing
them as numbers does not. Decimal arithmetic keeps it exact: `0.1` is one tenth, not a float
near it.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_QUANTITY = re.compile(
    r"^(?P<number>[+-]?(?:\d+\.?\d*|\.\d+))"
    r"(?P<suffix>Ki|Mi|Gi|Ti|Pi|Ei|[eE][+-]?\d+|n|u|m|k|M|G|T|P|E)?$"
)

_BINARY = {"Ki": 1, "Mi": 2, "Gi": 3, "Ti": 4, "Pi": 5, "Ei": 6}
_DECIMAL = {"n": -9, "u": -6, "m": -3, "": 0, "k": 3, "M": 6, "G": 9, "T": 12, "P": 15, "E": 18}


def parse_quantity(value: object) -> Decimal | None:
    """The exact amount a quantity stands for, or None when it is not a quantity."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # repr is the shortest text that round-trips: 0.1 becomes "0.1", not 0.1000000000000000055
        return parse_quantity(repr(value))
    if not isinstance(value, str):
        return None
    match = _QUANTITY.match(value.strip())
    if not match:
        return None
    try:
        number = Decimal(match.group("number"))
    except InvalidOperation:
        return None
    suffix = match.group("suffix") or ""
    if suffix in _BINARY:
        return number * (Decimal(1024) ** _BINARY[suffix])
    if suffix in _DECIMAL:
        return number.scaleb(_DECIMAL[suffix])
    return number.scaleb(int(suffix[1:]))


def same_quantity(a: object, b: object) -> bool | None:
    """True or False when both are quantities, None when either is not."""
    left, right = parse_quantity(a), parse_quantity(b)
    if left is None or right is None:
        return None
    return left == right
