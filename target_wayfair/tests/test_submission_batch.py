"""Tests for multi-product submitV2 batch helpers (HGI-11642)."""

from target_wayfair.client import WayfairSink
from target_wayfair.submission_batch import (
    GROUP_REFERENCE_ATTRIBUTE,
    PRIMARY_VARIANT,
    index_submission_rows_by_supplier_part,
    parse_variant_group_ref,
    submission_rows_terminal,
)


def test_index_submission_rows_by_supplier_part():
    rows = [
        {"supplierPartNumber": "SKU-A", "validationStatus": "VALIDATED"},
        {"supplierPartNumber": "SKU-B", "validationStatus": "FAILED"},
    ]
    assert index_submission_rows_by_supplier_part(rows) == {
        "SKU-A": rows[0],
        "SKU-B": rows[1],
    }


def test_submission_rows_terminal():
    assert submission_rows_terminal([]) is False
    assert submission_rows_terminal(
        [{"validationStatus": "PENDING", "submissionStatus": "COMPLETED"}]
    ) is False
    assert submission_rows_terminal(
        [
            {"validationStatus": "VALIDATED", "submissionStatus": "COMPLETED"},
            {"validationStatus": "VALIDATED", "submissionStatus": "COMPLETED"},
        ]
    ) is True


def test_parse_variant_group_ref_primary():
    record = {
        "attributes": [
            {
                "attributeId": "variantGrouping::variantType",
                "value": PRIMARY_VARIANT,
                "parentRank": 1,
                "rank": 1,
            },
            {
                "attributeId": GROUP_REFERENCE_ATTRIBUTE,
                "value": "GRP-1",
                "parentRank": 1,
                "rank": 1,
            },
        ]
    }
    grouping = parse_variant_group_ref(record)
    assert grouping is not None
    assert grouping.group_reference_id == "GRP-1"
    assert grouping.variant_type == PRIMARY_VARIANT


def test_build_submit_v2_variables_shape():
    products = [
        {
            "productId": "SKU-1",
            "classId": "12",
            "attributes": [
                {
                    "attributeId": "core::productName",
                    "value": "Bed",
                    "parentRank": 1,
                    "rank": 1,
                },
            ],
        }
    ]
    variables = WayfairSink.build_submit_v2_variables(
        WayfairSink,
        products,
        {"locale": "en-US", "country": "UNITED_STATES", "brand": "WAYFAIR"},
        job_context={"productAdditionRequestId": None, "hasMoreProducts": False},
    )
    request = variables["request"]
    assert request["marketContext"]["locale"] == "en-US"
    assert request["jobContext"]["hasMoreProducts"] is False
    assert len(request["proposedProductAdditions"]) == 1
    assert request["proposedProductAdditions"][0]["productId"] == "SKU-1"


def test_parse_variant_group_ref_not_variant():
    record = {
        "attributes": [
            {
                "attributeId": "variantGrouping::variantType",
                "value": "Not Variant",
                "parentRank": 1,
                "rank": 1,
            },
        ]
    }
    assert parse_variant_group_ref(record) is None
