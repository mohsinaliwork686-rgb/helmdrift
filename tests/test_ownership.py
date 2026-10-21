from datetime import UTC, datetime

from helmdrift.ownership import Owner, Ownership
from helmdrift.paths import parse_path
from tests.conftest import load_live


def owned_by(ownership, path, within=False):
    owners = (
        ownership.owners_within(parse_path(path)) if within else ownership.owners(parse_path(path))
    )
    return sorted(o.manager for o in owners)


def drifted_web():
    return Ownership(load_live("drifted", "deploy-shop-web").body["metadata"]["managedFields"])


def test_each_hand_edit_belongs_to_the_tool_that_made_it():
    ownership = drifted_web()
    assert owned_by(ownership, "spec.replicas") == ["kubectl"]
    assert owned_by(ownership, "spec.template.spec.containers[name=web].image") == ["kubectl-set"]
    assert owned_by(
        ownership, "spec.template.spec.containers[name=web].resources.limits.memory"
    ) == ["kubectl-edit"]
    assert owned_by(ownership, 'metadata.annotations["example.com/paged-by"]') == [
        "kubectl-annotate"
    ]


def test_fields_the_chart_still_owns_belong_to_helm():
    ownership = drifted_web()
    assert owned_by(
        ownership, "spec.template.spec.containers[name=web].env[name=APP_MODE].value"
    ) == ["helm"]
    assert owned_by(ownership, "spec.template.spec.containers[name=web].resources.limits.cpu") == [
        "helm"
    ]


def test_server_defaults_belong_to_nobody():
    ownership = drifted_web()
    for path in [
        "spec.progressDeadlineSeconds",
        "spec.template.spec.dnsPolicy",
        "spec.template.spec.containers[name=web].imagePullPolicy",
        "spec.template.spec.containers[name=web].readinessProbe.periodSeconds",
    ]:
        assert owned_by(ownership, path) == [], path
        assert owned_by(ownership, path, within=True) == [], path


def test_a_key_made_of_two_parts_still_matches():
    # fieldsV1 keys container ports by containerPort and protocol together.
    ownership = drifted_web()
    assert owned_by(
        ownership, "spec.template.spec.containers[name=web].ports[containerPort=80].name"
    ) == ["helm"]


def test_an_added_entry_is_found_through_its_entry_or_subfields():
    ownership = drifted_web()
    added = "spec.template.spec.containers[name=web].env[name=DEBUG]"
    assert owned_by(ownership, added) == ["kubectl-set"]
    assert owned_by(ownership, added + ".value") == ["kubectl-set"]
    assert owned_by(ownership, added, within=True) == ["kubectl-set"]


def test_owners_carry_subresource_and_time():
    ownership = drifted_web()
    [scale] = ownership.owners(parse_path("spec.replicas"))
    assert scale.subresource == "scale"
    assert scale.time is None  # recorded without a time on the real cluster
    [edit] = ownership.owners(
        parse_path("spec.template.spec.containers[name=web].resources.limits.memory")
    )
    assert edit.operation == "Update"
    assert edit.time is not None and edit.time.tzinfo is not None


def test_describe():
    assert Owner("kubectl", "Update", "scale").describe() == "kubectl (scale)"
    when = datetime(2026, 10, 20, 2, 16, tzinfo=UTC)
    assert Owner("kubectl-edit", "Update", None, when).describe() == (
        "kubectl-edit, last write 2026-10-20 02:16 UTC"
    )


def test_no_managed_fields_means_unknown():
    assert not Ownership(None).known
    assert not Ownership([]).known
    assert Ownership(None).owners(parse_path("spec.replicas")) == []
    assert drifted_web().known


def test_the_autoscaler_owns_the_worker_replicas():
    worker = Ownership(load_live("clean", "deploy-shop-worker").body["metadata"]["managedFields"])
    [owner] = worker.owners(parse_path("spec.replicas"))
    assert (owner.manager, owner.subresource) == ("k3s", "scale")


def test_shared_ownership_lists_every_owner():
    fields = {"f:spec": {"f:replicas": {}}}
    ownership = Ownership(
        [
            {"manager": "helm", "operation": "Apply", "fieldsType": "FieldsV1", "fieldsV1": fields},
            {
                "manager": "argocd",
                "operation": "Apply",
                "fieldsType": "FieldsV1",
                "fieldsV1": fields,
            },
        ]
    )
    assert owned_by(ownership, "spec.replicas") == ["argocd", "helm"]


def test_malformed_entries_do_not_crash():
    ownership = Ownership(
        [
            {
                "manager": "x",
                "operation": "Update",
                "time": "not a time",
                "fieldsV1": {"k:{broken": {}},
            },
        ]
    )
    assert ownership.owners(parse_path("spec.containers[name=web]")) == []
    assert ownership.known
