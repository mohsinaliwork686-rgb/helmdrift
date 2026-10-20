"""Shared helpers. No test here needs a cluster or the network."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from helmdrift.render import Resource, identity_of, parse_manifest
from helmdrift.rules import builtin_rules

FIXTURES = Path(__file__).parent / "fixtures" / "shop"


def load_live(state: str, name: str) -> Resource:
    """One real object captured from the cluster, e.g. load_live("drifted", "deploy-shop-web")."""
    body = yaml.safe_load((FIXTURES / state / f"{name}.yaml").read_text(encoding="utf-8"))
    return Resource(identity_of(body), body)


def load_desired() -> dict[str, Resource]:
    """What Helm applied, keyed like the fixture files."""
    resources = parse_manifest((FIXTURES / "manifest.yaml").read_text(encoding="utf-8"), "shop")
    short = {
        "Deployment": "deploy",
        "Service": "svc",
        "ConfigMap": "cm",
        "Secret": "secret",
        "ServiceAccount": "sa",
        "HorizontalPodAutoscaler": "hpa",
    }
    return {f"{short[r.identity.kind]}-{r.identity.name}": r for r in resources}


@pytest.fixture
def rules():
    return builtin_rules()
