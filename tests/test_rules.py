import pytest

from helmdrift.paths import parse_path
from helmdrift.render import Identity
from helmdrift.rules import RulesError, builtin_rules, load_ignore_file

WEB = Identity("apps", "Deployment", "shop", "shop-web")
WORKER = Identity("apps", "Deployment", "shop", "shop-worker")
SVC = Identity("", "Service", "shop", "shop-web")


def p(text):
    return parse_path(text)


def test_builtin_rules_load():
    rules = builtin_rules()
    assert rules.bookkeeping and rules.defaults
    assert rules.list_keys["containers"] == ("name",)
    assert rules.list_keys["ports"] == ("containerPort", "port")


def test_bookkeeping_covers_whole_subtrees():
    rules = builtin_rules()
    assert rules.is_bookkeeping(WEB, p("status.availableReplicas"))
    assert rules.is_bookkeeping(WEB, p('metadata.annotations["meta.helm.sh/release-name"]'))
    assert rules.is_bookkeeping(
        WEB, p('spec.template.metadata.annotations["kubectl.kubernetes.io/restartedAt"]')
    )
    assert not rules.is_bookkeeping(WEB, p('metadata.annotations["example.com/paged-by"]'))
    assert not rules.is_bookkeeping(WEB, p("spec.replicas"))


def test_defaults_respect_kind():
    rules = builtin_rules()
    assert rules.is_default(SVC, p("spec.clusterIP"), "10.43.0.12")
    assert not rules.is_default(WEB, p("spec.clusterIP"), "10.43.0.12")


def test_defaults_match_only_the_default_value():
    rules = builtin_rules()
    grace = p("spec.template.spec.terminationGracePeriodSeconds")
    assert rules.is_default(WEB, grace, 30)
    assert not rules.is_default(WEB, grace, 120)  # someone set this by hand: still drift
    pull = p("spec.template.spec.containers[name=web].imagePullPolicy")
    assert rules.is_default(WEB, pull, "IfNotPresent")
    assert rules.is_default(WEB, pull, "Always")
    assert not rules.is_default(WEB, pull, "Never")
    probe = p("spec.template.spec.containers[name=web].readinessProbe.periodSeconds")
    assert rules.is_default(WEB, probe, 10)
    assert not rules.is_default(WEB, probe, 3)


def test_value_types_must_match():
    # bool is an int in Python; a default of 0 must not match False, nor true match 1.
    rules = builtin_rules()
    assert rules.is_default(WEB, p("spec.template.spec.priority"), 0)
    assert not rules.is_default(WEB, p("spec.template.spec.priority"), False)
    assert not rules.is_default(WEB, p("spec.template.spec.enableServiceLinks"), 1)


def test_every_builtin_rule_has_a_valid_path():
    rules = builtin_rules()
    for rule in rules.bookkeeping + rules.defaults:
        assert rule.pattern


def test_ignore_rules_can_match_a_value(tmp_path):
    path = write(tmp_path, "ignore:\n  - path: data.LOG_LEVEL\n    value: [debug, trace]\n")
    [rule] = load_ignore_file(path)
    cm = Identity("", "ConfigMap", "shop", "shop-config")
    assert rule.matches(cm, p("data.LOG_LEVEL"), "debug")
    assert not rule.matches(cm, p("data.LOG_LEVEL"), "error")


def write(tmp_path, text):
    path = tmp_path / ".helmdrift-ignore.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_ignore_file_rules(tmp_path):
    path = write(
        tmp_path,
        """
ignore:
  - path: metadata.annotations["example.com/*"]
    reason: written by our sync job
  - kind: Deployment
    name: shop-worker
    path: spec.replicas
  - kind: [Deployment, StatefulSet]
    path: spec.template.spec.containers[*].env[name=OTEL_*]
""",
    )
    rules = builtin_rules().with_ignore(load_ignore_file(path))

    hit = rules.ignored_by(WEB, p('metadata.annotations["example.com/paged-by"]'))
    assert hit is not None
    assert hit.reason == "written by our sync job"
    assert hit.source.endswith(".helmdrift-ignore.yaml:3")

    assert rules.ignored_by(WORKER, p("spec.replicas")) is not None
    assert rules.ignored_by(WEB, p("spec.replicas")) is None
    assert rules.ignored_by(SVC, p("spec.replicas")) is None
    otel = p("spec.template.spec.containers[name=web].env[name=OTEL_ENDPOINT]")
    assert rules.ignored_by(WEB, otel) is not None
    assert (
        rules.ignored_by(WEB, p("spec.template.spec.containers[name=web].env[name=DEBUG]")) is None
    )


def test_a_path_copied_from_the_report_works_as_a_rule(tmp_path):
    reported = "spec.template.spec.containers[name=web].env[name=DEBUG]"
    rules = load_ignore_file(write(tmp_path, f"ignore:\n  - path: {reported}\n"))
    assert rules[0].matches(WEB, p(reported))


def test_empty_files_are_fine(tmp_path):
    assert load_ignore_file(write(tmp_path, "")) == []
    assert load_ignore_file(write(tmp_path, "ignore:\n")) == []
    assert load_ignore_file(write(tmp_path, "ignore: []\n")) == []


@pytest.mark.parametrize(
    ("text", "line", "message"),
    [
        ("ignore:\n  - kind: Deployment\n", 2, "needs a 'path'"),
        ("ignore:\n  - path: spec.replicas\n  - path: spec..x\n", 3, "misplaced '.'"),
        ("ignore:\n  - path: spec.replicas\n    resaon: typo\n", 2, "unknown key 'resaon'"),
        ("ignore:\n  - just a string\n", 2, "must be a mapping"),
        ("ignroe:\n  - path: spec\n", 1, "unknown key 'ignroe'"),
        ("ignore: spec.replicas\n", 1, "must be a list"),
        ("ignore:\n  - path: spec\n    kind: 3\n", 2, "'kind' must be"),
        ("- path: spec\n", 1, "expected a mapping"),
        ("ignore:\n  - path: spec\n    value: {a: 1}\n", 2, "'value' must be"),
    ],
)
def test_malformed_rules_name_the_file_and_line(tmp_path, text, line, message):
    path = write(tmp_path, text)
    with pytest.raises(RulesError) as caught:
        load_ignore_file(path)
    assert f"{path}:{line}:" in str(caught.value)
    assert message in str(caught.value)


def test_unreadable_and_invalid_files(tmp_path):
    with pytest.raises(RulesError, match="cannot read"):
        load_ignore_file(tmp_path / "missing.yaml")
    with pytest.raises(RulesError, match="not valid YAML"):
        load_ignore_file(write(tmp_path, "ignore: [unclosed\n"))
