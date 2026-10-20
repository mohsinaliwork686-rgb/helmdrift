import pytest

from helmdrift.paths import (
    ANY_ITEM,
    Field,
    Index,
    Item,
    PathError,
    covers,
    format_path,
    parse_path,
)

CASES = [
    ("spec.replicas", (Field("spec"), Field("replicas"))),
    (
        "spec.template.spec.containers[name=web].image",
        (
            Field("spec"),
            Field("template"),
            Field("spec"),
            Field("containers"),
            Item("name", "web"),
            Field("image"),
        ),
    ),
    (
        'metadata.annotations["app.kubernetes.io/name"]',
        (Field("metadata"), Field("annotations"), Field("app.kubernetes.io/name")),
    ),
    (
        "spec.containers[name=web].command[0]",
        (
            Field("spec"),
            Field("containers"),
            Item("name", "web"),
            Field("command"),
            Index(0),
        ),
    ),
    (
        "spec.ports[port=80].targetPort",
        (
            Field("spec"),
            Field("ports"),
            Item("port", "80"),
            Field("targetPort"),
        ),
    ),
    (
        "volumeMounts[mountPath=/data/cache]",
        (Field("volumeMounts"), Item("mountPath", "/data/cache")),
    ),
    ("data.LOG_LEVEL", (Field("data"), Field("LOG_LEVEL"))),
]


@pytest.mark.parametrize(("text", "path"), CASES)
def test_parse_and_format_round_trip(text, path):
    assert parse_path(text) == path
    assert format_path(path) == text


def test_awkward_values_are_quoted_and_survive_a_round_trip():
    for value in ["a]b", 'say "hi"', "k=v", "", " padded "]:
        path = (Field("env"), Item("name", value))
        assert parse_path(format_path(path)) == path


def test_awkward_keys_are_quoted():
    path = (Field("data"), Field("config.yaml"))
    assert format_path(path) == 'data["config.yaml"]'
    assert parse_path('data["config.yaml"]') == path


def test_a_quoted_key_may_start_a_path():
    assert format_path((Field("a.b"), Field("c"))) == '["a.b"].c'
    assert parse_path('["a.b"].c') == (Field("a.b"), Field("c"))


@pytest.mark.parametrize(
    "text",
    [
        "",
        ".spec",
        "spec.",
        "spec..replicas",
        "spec[",
        "spec[name=web",
        "spec[x]",
        'spec["unclosed]',
        "spec]x",
        "spec replicas",
        "spec.[0]",
        "spec[=web]",
    ],
)
def test_malformed_paths_are_refused(text):
    with pytest.raises(PathError):
        parse_path(text)


def test_wildcards_parse():
    assert parse_path("spec.containers[*].imagePullPolicy")[2] == ANY_ITEM
    assert parse_path("env[name=OTEL_*]")[1] == Item("name", "OTEL_*")


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("spec.replicas", "spec.replicas", True),
        ("spec.replicas", "spec.template", False),
        ("metadata.annotations", 'metadata.annotations["a.b/c"]', True),
        ('metadata.annotations["example.com/*"]', 'metadata.annotations["example.com/x"]', True),
        ('metadata.annotations["example.com/*"]', 'metadata.annotations["other.com/x"]', False),
        ("spec.containers[*].image", "spec.containers[name=web].image", True),
        ("spec.containers[*].image", "spec.containers[0].image", True),
        ("spec.containers[name=web].image", "spec.containers[name=api].image", False),
        ("env[name=OTEL_*]", "env[name=OTEL_EXPORTER]", True),
        ("env[name=OTEL_*]", "env[name=DEBUG]", False),
        ("env[name=*]", "env[0]", False),
        ("spec.*.image", "spec.web.image", True),
        ("spec.containers[name=web].image", "spec.containers", False),
    ],
)
def test_covers(pattern, path, expected):
    assert covers(parse_path(pattern), parse_path(path)) is expected
