"""Tests standard target features using the built-in SDK tests library."""

import json
import os
from typing import Any, Dict

import pytest
from hotglue_singer_sdk.testing import get_standard_target_tests

from target_wayfair.client import WayfairSink
from target_wayfair.target import TargetWayfair

SECRETS_PATH = os.path.join(os.path.dirname(__file__), "..", "..", ".secrets", "config.json")


def test_resolve_submission_product_id_prefers_supplier_part_number():
    record = {
        "productId": "PLY-BED-0004",
        "attributes": [
            {
                "attributeId": "core::supplierPartNumber",
                "value": "PLY-BED-0004-UNIQUE",
                "parentRank": 1,
                "rank": 1,
            }
        ],
    }
    assert WayfairSink.resolve_submission_product_id(record) == "PLY-BED-0004-UNIQUE"


def test_resolve_submission_product_id_falls_back_to_record_product_id():
    record = {"productId": "PLY-BED-0004", "attributes": []}
    assert WayfairSink.resolve_submission_product_id(record) == "PLY-BED-0004"


def test_ensure_class_name_attribute_injects_when_missing():
    attributes = [
        {
            "attributeId": "core::productName",
            "value": "My Bed",
            "parentRank": 1,
            "rank": 1,
        }
    ]
    supported = {
        "1": {
            "taxonomyAttributeId": "1",
            "title": "Class ID",
            "possibleAttributeValues": [{"value": "Beds"}],
        }
    }
    result = WayfairSink.ensure_class_name_attribute(
        WayfairSink, attributes, "12", supported
    )
    assert result[0] == {
        "attributeId": "1",
        "value": "Beds",
        "parentRank": 1,
        "rank": 1,
    }
    assert result[1:] == attributes


def test_ensure_class_name_attribute_overrides_existing():
    attributes = [
        {"attributeId": "1", "value": "Wrong Class", "parentRank": 1, "rank": 1},
        {
            "attributeId": "core::productName",
            "value": "My Bed",
            "parentRank": 1,
            "rank": 1,
        },
    ]
    supported = {
        "1": {
            "taxonomyAttributeId": "1",
            "title": "Class ID",
            "possibleAttributeValues": [{"value": "Beds"}],
        }
    }
    result = WayfairSink.ensure_class_name_attribute(
        WayfairSink, attributes, "12", supported
    )
    assert result == [
        {"attributeId": "1", "value": "Beds", "parentRank": 1, "rank": 1},
        {
            "attributeId": "core::productName",
            "value": "My Bed",
            "parentRank": 1,
            "rank": 1,
        },
    ]


@pytest.fixture
def config() -> Dict[str, Any]:
    if not os.path.exists(SECRETS_PATH):
        pytest.skip("No .secrets/config.json found; skipping live tests")
    with open(SECRETS_PATH) as f:
        return json.load(f)


def test_standard_target_tests(config):
    """Run standard target tests from the SDK."""
    tests = get_standard_target_tests(
        TargetWayfair,
        config=config,
    )
    for test in tests:
        test()
