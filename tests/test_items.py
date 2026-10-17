"""Tests for items.py parsing, priority logic, and form descriptions."""

import pytest
from items import (
    HIGH_PRIORITY_ITEMS,
    ITEM_DESCRIPTIONS,
    get_form_description,
    is_high_priority_filing,
    normalize_item_code,
    parse_items,
)


def test_normalize_item_code():
    assert normalize_item_code("2.02") == "2.02"
    assert normalize_item_code("Item 2.02") == "2.02"
    assert normalize_item_code("  item 5.02  ") == "5.02"


def test_parse_single_item():
    items = parse_items("2.02")
    assert len(items) == 1
    assert items[0].code == "2.02"
    assert items[0].description == "Results of Operations and Financial Condition"
    assert items[0].is_high_priority is True


def test_parse_multiple_items():
    items = parse_items("2.02, 9.01, 8.01")
    assert len(items) == 3
    assert items[0].code == "2.02"
    assert items[0].is_high_priority is True
    assert items[1].code == "9.01"
    assert items[1].is_high_priority is False
    assert items[2].code == "8.01"
    assert items[2].is_high_priority is False


def test_parse_empty_or_none():
    assert parse_items(None) == []
    assert parse_items("") == []
    assert parse_items("   ") == []


def test_parse_unknown_item_code():
    items = parse_items("99.99, 2.02")
    assert len(items) == 2
    assert items[0].code == "99.99"
    assert items[0].description == "Item 99.99 (unrecognized)"
    assert items[0].is_high_priority is False
    assert items[1].code == "2.02"
    assert items[1].is_high_priority is True


def test_high_priority_detection():
    # High priority items per PRD: 2.02, 1.01, 1.03, 4.02, 5.02, 3.01
    for code in ["2.02", "1.01", "1.03", "4.02", "5.02", "3.01"]:
        parsed = parse_items(code)
        assert is_high_priority_filing("8-K", parsed) is True

    # 9.01 alone is NEVER high priority
    parsed_901 = parse_items("9.01")
    assert is_high_priority_filing("8-K", parsed_901) is False

    # 8.01 alone is NOT high priority
    parsed_801 = parse_items("8.01")
    assert is_high_priority_filing("8-K", parsed_801) is False

    # 7.01 alone is NOT high priority
    parsed_701 = parse_items("7.01")
    assert is_high_priority_filing("8-K", parsed_701) is False

    # Mixed with 9.01: 2.02 + 9.01 IS high priority because 2.02 is present
    parsed_mixed = parse_items("2.02,9.01")
    assert is_high_priority_filing("8-K", parsed_mixed) is True


def test_form_descriptions():
    assert "Annual Report" in get_form_description("10-K")
    assert "Quarterly Report" in get_form_description("10-Q")
    assert "Insider" in get_form_description("4")
    assert "13D" in get_form_description("SC 13D")
    assert "13G" in get_form_description("SC 13G")
    assert "Current Report" in get_form_description("8-K")
    assert "XYZ" in get_form_description("XYZ")
