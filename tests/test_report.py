import io
from datetime import UTC, datetime

from helmdrift.diff import Difference, DriftReport, Ignored, ObjectDiff
from helmdrift.ownership import Owner
from helmdrift.paths import parse_path
from helmdrift.render import Identity
from helmdrift.report import exit_code, render_text, to_json

WEB = Identity("apps", "Deployment", "ns", "web")


def text(report, **kwargs):
    out = io.StringIO()
    render_text(report, "heading", out, color=False, **kwargs)
    return out.getvalue()


def report_with(*differences, ignored=(), missing=(), extra=()):
    return DriftReport(
        "ctx", [ObjectDiff(WEB, list(differences), list(ignored))], list(missing), list(extra)
    )


def test_exit_codes():
    assert exit_code(report_with()) == 0
    assert exit_code(report_with(Difference("changed", parse_path("spec.replicas"), 1, 2))) == 1
    assert exit_code(report_with(missing=[WEB])) == 1
    assert exit_code(report_with(extra=[WEB])) == 1


def test_an_ignored_difference_alone_is_not_drift():
    report = report_with(ignored=[Ignored(parse_path("spec.replicas"), "autoscaled")])
    assert exit_code(report) == 0
    out = text(report)
    assert "No drift: 1 object match." in out
    assert "1 difference ignored by rules (list them with --show-ignored)." in out


def test_removed_and_unattributed_lines():
    out = text(
        report_with(
            Difference("removed", parse_path("spec.paused"), chart=True),
            Difference("changed", parse_path("spec.minReadySeconds"), chart=0, live=5),
        )
    )
    assert "  - spec.paused" in out
    assert "      chart true" in out
    assert "a removal leaves no record of who made it" in out
    assert "who changed it is not recorded on this object" in out


def test_several_owners_are_listed():
    owners = (Owner("argocd", "Apply"), Owner("helm", "Apply"))
    out = text(report_with(Difference("changed", parse_path("spec.replicas"), 1, 2, owners)))
    assert "owned by argocd and helm" in out


def test_awkward_and_long_values():
    long = "x" * 500
    out = text(
        report_with(
            Difference("changed", parse_path("data.a"), chart="", live=" padded"),
            Difference("changed", parse_path("data.b"), chart="ok", live=long),
        )
    )
    assert 'chart ""  ->  live " padded"' in out
    assert "x" * 117 + "..." in out
    assert long not in out


def test_json_times_are_iso_strings():
    owner = Owner("kubectl-edit", "Update", None, datetime(2026, 10, 20, 10, 4, tzinfo=UTC))
    data = to_json(
        report_with(Difference("changed", parse_path("spec.replicas"), 1, 2, (owner,))), {}
    )
    assert data["objects"][0]["differences"][0]["owners"][0]["time"] == "2026-10-20T10:04:00+00:00"


def test_plurals():
    two = report_with(
        Difference("changed", parse_path("a"), 1, 2), Difference("changed", parse_path("b"), 1, 2)
    )
    assert "2 differences in 1 object." in text(two)
