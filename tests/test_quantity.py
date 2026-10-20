from decimal import Decimal

import pytest

from helmdrift.quantity import parse_quantity, same_quantity


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("500m", "0.5"),
        ("0.5", "0.5"),
        ("100m", "0.1"),
        ("1", "1"),
        ("1k", "1000"),
        ("1e3", "1000"),
        ("1E3", "1000"),
        ("1Ki", "1024"),
        ("1Mi", "1048576"),
        ("1Gi", "1073741824"),
        ("1G", "1000000000"),
        ("128974848", "128974848"),
        ("129e6", "129000000"),
        ("123Mi", "128974848"),
        ("2.5Gi", "2684354560"),
        ("1u", "0.000001"),
        ("1n", "0.000000001"),
        ("+1", "1"),
        ("-1", "-1"),
        (".5", "0.5"),
        ("5.", "5"),
        ("1E", "1000000000000000000"),
    ],
)
def test_parse(text, amount):
    assert parse_quantity(text) == Decimal(amount)


def test_numbers_from_yaml_parse_exactly():
    assert parse_quantity(2) == Decimal(2)
    assert parse_quantity(0.1) == Decimal("0.1")  # not the float nearest 0.1
    assert parse_quantity(0.5) == parse_quantity("500m")


@pytest.mark.parametrize(
    "value", ["", "abc", "1.2.3", "1Xi", "5 Mi", "1ki", "Mi", True, None, [1], {"a": 1}]
)
def test_non_quantities_are_none(value):
    assert parse_quantity(value) is None


def test_the_same_amount_written_differently_is_equal():
    assert same_quantity(0.5, "500m") is True
    assert same_quantity("1Gi", "1024Mi") is True
    assert same_quantity(0.1, "100m") is True
    assert same_quantity("128974848", "123Mi") is True


def test_decimal_and_binary_suffixes_differ():
    assert same_quantity("1G", "1Gi") is False
    assert same_quantity("128Mi", "256Mi") is False


def test_non_quantities_compare_as_unknown():
    assert same_quantity("info", "debug") is None
    assert same_quantity("1Gi", "lots") is None
