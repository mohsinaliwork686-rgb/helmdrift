"""Turn a DriftReport into text for people or JSON for tools, and pick the exit code.

Exit codes: 0 means no drift, 1 means drift was found, 2 means the check could not run (set by
the CLI). CI can then tell "drifted" from "broken".

The text uses plain ASCII markers (~ changed, + added, - removed) so it reads the same in any
terminal or CI log. Secret values were masked inside the diff already, so neither format can show
one.
"""

from __future__ import annotations

import json
from typing import Any, TextIO

from rich.console import Console
from rich.text import Text

from helmdrift.diff import Difference, DriftReport, ObjectDiff
from helmdrift.ownership import Owner
from helmdrift.paths import format_path
from helmdrift.render import Identity

_MARKER = {"changed": ("~", "yellow"), "added": ("+", "green"), "removed": ("-", "red")}
_MAX_VALUE = 120


def exit_code(report: DriftReport) -> int:
    return 1 if report.has_drift else 0


def render_text(
    report: DriftReport,
    heading: str,
    out: TextIO,
    *,
    color: bool | None = None,
    show_ignored: bool = False,
) -> None:
    console = Console(
        file=out,
        highlight=False,
        soft_wrap=True,
        no_color=color is False,
        force_terminal=True if color else None,
        color_system="standard" if color else ("auto" if color is None else None),
    )
    console.print(Text(heading, style="bold"))

    for obj in report.objects:
        if not obj.differences and not (show_ignored and obj.ignored):
            continue
        console.print()
        console.print(Text(str(obj.identity), style="bold"))
        for difference in obj.differences:
            _print_difference(console, difference)
        if show_ignored:
            for ignored in obj.ignored:
                console.print(
                    Text(f"  . {format_path(ignored.path)}  ignored: {ignored.reason}", style="dim")
                )

    if report.missing or report.extra:
        console.print()
    if report.missing:
        console.print(
            Text("Missing from the cluster:  ", style="bold red") + Text(_names(report.missing))
        )
    if report.extra:
        console.print(
            Text("Not in the chart:          ", style="bold yellow") + Text(_names(report.extra))
        )

    console.print()
    console.print(Text(_summary(report, show_ignored)))


def _print_difference(console: Console, difference: Difference) -> None:
    marker, style = _MARKER[difference.change]
    console.print(Text(f"  {marker} ", style=style) + Text(format_path(difference.path)))
    if difference.change == "changed":
        line = f"chart {_value(difference.chart)}  ->  live {_value(difference.live)}"
    elif difference.change == "added":
        line = f"live {_value(difference.live)}"
    else:
        line = f"chart {_value(difference.chart)}"
    console.print(Text(f"      {line}"))
    console.print(Text(f"      {_who(difference)}", style="dim"))


def _who(difference: Difference) -> str:
    if difference.change == "removed":
        return "not in the cluster; a removal leaves no record of who made it"
    if not difference.owners:
        return "who changed it is not recorded on this object"
    verb = "added by" if difference.change == "added" else "owned by"
    return f"{verb} {_owners(difference.owners)}"


def _owners(owners: tuple[Owner, ...]) -> str:
    described = [owner.describe() for owner in owners]
    return (
        described[0] if len(described) == 1 else ", ".join(described[:-1]) + " and " + described[-1]
    )


def _value(value: Any) -> str:
    if isinstance(value, str):
        text = (
            value if value and value == value.strip() and "\n" not in value else json.dumps(value)
        )
    else:
        text = json.dumps(value, sort_keys=False, default=str)
    return text if len(text) <= _MAX_VALUE else text[: _MAX_VALUE - 3] + "..."


def _names(identities: list[Identity]) -> str:
    return ", ".join(str(identity) for identity in identities)


def _count(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _summary(report: DriftReport, show_ignored: bool) -> str:
    ignored = sum(len(o.ignored) for o in report.objects)
    if not report.has_drift:
        text = f"No drift: {_count(len(report.objects), 'object')} match."
    else:
        differences = sum(len(o.differences) for o in report.drifted)
        parts = [f"{_count(differences, 'difference')} in {_count(len(report.drifted), 'object')}"]
        if report.missing:
            parts.append(f"{len(report.missing)} missing")
        if report.extra:
            parts.append(f"{len(report.extra)} extra")
        text = ", ".join(parts) + "."
    if ignored:
        hint = "" if show_ignored else " (list them with --show-ignored)"
        text += f" {_count(ignored, 'difference')} ignored by rules{hint}."
    return text


def to_json(report: DriftReport, context: dict[str, Any]) -> dict[str, Any]:
    """A stable, machine-readable report. Values are as the diff produced them: Secrets masked."""
    listed = [o for o in report.objects if o.differences or o.ignored]
    return {
        **context,
        "target": report.target,
        "drift": report.has_drift,
        "summary": {
            "compared": len(report.objects),
            "drifted": len(report.drifted),
            "differences": sum(len(o.differences) for o in report.objects),
            "missing": len(report.missing),
            "extra": len(report.extra),
            "ignored": sum(len(o.ignored) for o in report.objects),
        },
        "objects": [_object_json(o) for o in listed],
        "missing": [_identity_json(i) for i in report.missing],
        "extra": [_identity_json(i) for i in report.extra],
    }


def render_json(report: DriftReport, context: dict[str, Any], out: TextIO) -> None:
    json.dump(to_json(report, context), out, indent=2, default=str)
    out.write("\n")


def _object_json(obj: ObjectDiff) -> dict[str, Any]:
    return {
        **_identity_json(obj.identity),
        "differences": [
            {
                "change": d.change,
                "path": format_path(d.path),
                "chart": d.chart,
                "live": d.live,
                "hidden": d.hidden,
                "owners": [_owner_json(o) for o in d.owners],
            }
            for d in obj.differences
        ],
        "ignored": [{"path": format_path(i.path), "reason": i.reason} for i in obj.ignored],
    }


def _owner_json(owner: Owner) -> dict[str, Any]:
    return {
        "manager": owner.manager,
        "operation": owner.operation,
        "subresource": owner.subresource,
        "time": owner.time.isoformat() if owner.time else None,
    }


def _identity_json(identity: Identity) -> dict[str, Any]:
    return {
        "group": identity.group,
        "kind": identity.kind,
        "namespace": identity.namespace,
        "name": identity.name,
    }
