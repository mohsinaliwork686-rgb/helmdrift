import base64

import pytest

from helmdrift.normalise import leaves, normalise
from helmdrift.paths import Field, Index, format_path, parse_path
from helmdrift.quantity import same_quantity
from helmdrift.render import Resource, identity_of
from tests.conftest import load_desired, load_live


def resource(body):
    return Resource(identity_of(body), body)


def flat(tree):
    return {format_path(path): value for path, value in leaves(tree)}


def deployment(**pod_spec):
    return resource(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "web", "namespace": "shop"},
            "spec": {"template": {"spec": pod_spec}},
        }
    )


def test_bookkeeping_is_removed(rules):
    tree = normalise(load_live("clean", "deploy-shop-web"), rules)
    paths = set(flat(tree))
    assert not any(
        p.startswith(("status", "metadata.managedFields", "metadata.uid")) for p in paths
    )
    assert 'metadata.annotations["meta.helm.sh/release-name"]' not in paths
    assert "metadata.name" in paths


def test_list_entries_are_keyed(rules):
    tree = normalise(load_live("clean", "deploy-shop-web"), rules)
    paths = set(flat(tree))
    assert "spec.template.spec.containers[name=web].image" in paths
    assert "spec.template.spec.containers[name=web].env[name=APP_MODE].value" in paths
    assert "spec.template.spec.containers[name=web].ports[containerPort=80].name" in paths


def test_reordering_a_keyed_list_changes_nothing(rules):
    a = deployment(containers=[{"name": "a", "image": "x"}, {"name": "b", "image": "y"}])
    b = deployment(containers=[{"name": "b", "image": "y"}, {"name": "a", "image": "x"}])
    assert normalise(a, rules) == normalise(b, rules)


def test_lists_without_a_key_use_positions(rules):
    tree = normalise(deployment(tolerations=[{"key": "a"}, {"key": "b"}]), rules)
    tolerations = tree[Field("spec")][Field("template")][Field("spec")][Field("tolerations")]
    assert list(tolerations) == [Index(0), Index(1)]


def test_a_repeated_key_falls_back_to_positions(rules):
    tree = normalise(deployment(containers=[{"name": "a"}, {"name": "a"}]), rules)
    containers = tree[Field("spec")][Field("template")][Field("spec")][Field("containers")]
    assert list(containers) == [Index(0), Index(1)]


def test_service_ports_are_keyed_by_port(rules):
    tree = normalise(load_live("clean", "svc-shop-web"), rules)
    assert "spec.ports[port=80].targetPort" in flat(tree)


def test_lists_of_plain_values_stay_whole(rules):
    tree = normalise(deployment(containers=[{"name": "w", "command": ["sh", "-c", "x"]}]), rules)
    assert flat(tree)["spec.template.spec.containers[name=w].command"] == ("sh", "-c", "x")


def test_empty_values_count_as_absent(rules):
    with_empties = deployment(
        containers=[{"name": "w", "args": [], "env": None}], securityContext={}, volumes=[]
    )
    without = deployment(containers=[{"name": "w"}])
    assert normalise(with_empties, rules) == normalise(without, rules)


def test_empty_strings_are_kept(rules):
    tree = normalise(
        deployment(containers=[{"name": "w", "env": [{"name": "X", "value": ""}]}]), rules
    )
    assert flat(tree)["spec.template.spec.containers[name=w].env[name=X].value"] == ""


def test_secret_string_data_compares_as_data(rules):
    chart = resource(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "s"},
            "stringData": {"password": "hunter2"},
        }
    )
    live = resource(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "s"},
            "data": {"password": base64.b64encode(b"hunter2").decode()},
        }
    )
    assert normalise(chart, rules) == normalise(live, rules)


def test_the_real_secret_matches_its_chart(rules):
    chart = normalise(load_desired()["secret-shop-credentials"], rules)
    live = normalise(load_live("clean", "secret-shop-credentials"), rules)
    assert flat(chart)["data.password"] == flat(live)["data.password"]


def test_normalising_does_not_change_the_input(rules):
    secret = resource(
        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "s"}, "stringData": {"a": "b"}}
    )
    normalise(secret, rules)
    assert secret.body["stringData"] == {"a": "b"}


CLEAN = [
    "deploy-shop-web",
    "deploy-shop-worker",
    "svc-shop-web",
    "svc-shop-metrics",
    "cm-shop-config",
    "secret-shop-credentials",
    "sa-shop",
    "hpa-shop-worker",
]


@pytest.mark.parametrize("name", CLEAN)
def test_on_a_clean_install_every_chart_field_matches_the_cluster(rules, name):
    desired = flat(normalise(load_desired()[name], rules))
    live = flat(normalise(load_live("clean", name), rules))
    for path, value in desired.items():
        if path == "spec.replicas" and name == "deploy-shop-worker":
            continue  # the autoscaler owns it: tomorrow's HPA rule
        assert path in live, f"{name}: {path} is missing from the cluster"
        assert value == live[path] or same_quantity(value, live[path]), (
            f"{name}: {path} chart {value!r} live {live[path]!r}"
        )


@pytest.mark.parametrize("name", CLEAN)
def test_on_a_clean_install_the_fallback_list_knows_every_server_default(rules, name):
    """The defaults list, checked against real objects: every live-only field is a default."""
    resource_ = load_live("clean", name)
    desired = flat(normalise(load_desired()[name], rules))
    live_only = {
        path: value
        for path, value in leaves(normalise(resource_, rules))
        if format_path(path) not in desired
    }
    unknown = {
        format_path(path): value
        for path, value in live_only.items()
        if not rules.is_default(resource_.identity, path, value)
    }
    assert unknown == {}


def test_paths_print_and_parse_back(rules):
    for path, _ in leaves(normalise(load_live("drifted", "deploy-shop-web"), rules)):
        assert parse_path(format_path(path)) == path
