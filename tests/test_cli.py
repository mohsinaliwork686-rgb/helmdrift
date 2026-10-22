import io
import json

import pytest
import yaml

from helmdrift.cli import run
from helmdrift.live import StaticReader
from tests.conftest import FIXTURES

MANIFEST = str(FIXTURES / "manifest.yaml")
SECRETS = [
    "changed-at-2am",
    "not-a-real-password",
    "Y2hhbmdlZC1hdC0yYW0",
    "bm90LWEtcmVhbC1wYXNzd29yZA",
]


def cluster(state):
    bodies = [
        yaml.safe_load(p.read_text(encoding="utf-8"))
        for p in sorted((FIXTURES / state).glob("*.yaml"))
    ]
    return StaticReader(bodies, f"fixtures/{state}")


def helmdrift(capsys, *args, state="drifted"):
    code = run(
        ["--release", "shop", "--namespace", "shop", "--manifest", MANIFEST, *args],
        reader=cluster(state),
    )
    out, err = capsys.readouterr()
    return code, out, err


def test_no_drift_exits_0(capsys):
    code, out, _ = helmdrift(capsys, "--no-color", state="clean")
    assert code == 0
    assert "No drift: 8 objects match." in out


def test_drift_exits_1_and_reads_well(capsys):
    code, out, _ = helmdrift(capsys, "--no-color")
    assert code == 1
    lines = out.splitlines()
    assert lines[0] == (
        "helmdrift: release shop in namespace shop, fixtures/drifted, "
        f"compared with the manifest {MANIFEST}"
    )
    assert "  ~ spec.replicas" in lines
    assert "      chart 2  ->  live 4" in lines
    assert "      owned by kubectl (scale)" in lines
    assert "  + spec.template.spec.containers[name=web].env[name=DEBUG]" in lines
    assert '      live {"name": "DEBUG", "value": "true"}' in lines
    assert "Missing from the cluster:  Service/shop-metrics" in lines
    assert "Not in the chart:          ConfigMap/shop-legacy" in lines
    assert lines[-1].startswith("7 differences in 3 objects, 1 missing, 1 extra.")


def test_json_report(capsys):
    code, out, _ = helmdrift(capsys, "--format", "json")
    assert code == 1
    data = json.loads(out)
    assert data["drift"] is True
    # 7 compared: the eighth object, the metrics Service, is missing from the cluster.
    assert data["summary"] == {
        "compared": 7,
        "drifted": 3,
        "differences": 7,
        "missing": 1,
        "extra": 1,
        "ignored": 1,
    }
    assert [m["name"] for m in data["missing"]] == ["shop-metrics"]
    web = next(o for o in data["objects"] if o["name"] == "shop-web")
    replicas = next(d for d in web["differences"] if d["path"] == "spec.replicas")
    assert replicas == {
        "change": "changed",
        "path": "spec.replicas",
        "chart": 2,
        "live": 4,
        "hidden": False,
        "owners": [
            {"manager": "kubectl", "operation": "Update", "subresource": "scale", "time": None}
        ],
    }
    worker = next(o for o in data["objects"] if o["name"] == "shop-worker")
    assert worker["differences"] == []
    assert worker["ignored"] == [
        {"path": "spec.replicas", "reason": "set by a HorizontalPodAutoscaler"}
    ]


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_secret_values_appear_in_no_format(capsys, fmt):
    _, out, _ = helmdrift(capsys, "--format", fmt, "--no-color")
    assert "data.password" in out
    for secret in SECRETS:
        assert secret not in out


def test_show_ignored_lists_them(capsys):
    _, out, _ = helmdrift(capsys, "--show-ignored", "--no-color")
    assert "  . spec.replicas  ignored: set by a HorizontalPodAutoscaler" in out.splitlines()


def test_no_hpa_rule(capsys):
    code, out, _ = helmdrift(capsys, "--no-hpa-rule", "--format", "json", state="clean")
    assert code == 1
    assert json.loads(out)["summary"]["differences"] == 1


def test_an_ignore_file_silences_a_difference(capsys, tmp_path):
    rules = tmp_path / "ignore.yaml"
    rules.write_text(
        "ignore:\n"
        '  - path: metadata.annotations["example.com/paged-by"]\n'
        "    reason: written by the paging tool\n"
    )
    _, out, _ = helmdrift(capsys, "--ignore-file", str(rules), "--format", "json")
    data = json.loads(out)
    assert data["summary"]["differences"] == 6
    assert data["ignore_files"] == [str(rules)]


def test_the_default_ignore_file_is_picked_up(capsys, tmp_path, monkeypatch):
    (tmp_path / ".helmdrift-ignore.yaml").write_text("ignore:\n  - path: data.LOG_LEVEL\n")
    monkeypatch.chdir(tmp_path)
    _, out, _ = helmdrift(capsys, "--no-color")
    assert "ignore rules from .helmdrift-ignore.yaml" in out.splitlines()[0]
    assert "data.LOG_LEVEL" not in out


def test_a_broken_ignore_file_exits_2(capsys, tmp_path):
    rules = tmp_path / "ignore.yaml"
    rules.write_text("ignore:\n  - kind: Deployment\n")
    code, out, err = helmdrift(capsys, "--ignore-file", str(rules))
    assert code == 2
    assert out == ""
    assert f"{rules}:2: a rule needs a 'path'" in err


def test_a_missing_manifest_exits_2(capsys, tmp_path):
    code = run(
        ["-r", "shop", "-n", "shop", "--manifest", str(tmp_path / "nope.yaml")],
        reader=cluster("clean"),
    )
    assert code == 2
    assert "cannot read" in capsys.readouterr().err


def test_values_without_a_chart_exit_2(capsys):
    code, _, err = helmdrift(capsys, "-f", "values.yaml")
    assert code == 2
    assert "need --chart" in err


def test_the_manifest_can_come_from_stdin(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO((FIXTURES / "manifest.yaml").read_text()))
    code = run(
        ["-r", "shop", "-n", "shop", "--manifest", "-", "--no-color"], reader=cluster("clean")
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "compared with standard input" in out.splitlines()[0]


def test_missing_required_arguments_exit_2(capsys):
    with pytest.raises(SystemExit) as caught:
        run(["--namespace", "shop"])
    assert caught.value.code == 2


def test_chart_and_manifest_cannot_be_combined(capsys):
    with pytest.raises(SystemExit) as caught:
        run(["-r", "x", "-n", "y", "--chart", "c", "--manifest", "m"])
    assert caught.value.code == 2
