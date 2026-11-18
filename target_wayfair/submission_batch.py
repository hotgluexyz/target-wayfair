"""Variant grouping and multi-product submitV2 / submissionsV2 poll helpers."""

from collections.abc import Callable
from dataclasses import dataclass

from target_wayfair.export_errors import structured_export_error

VARIANT_TYPE_ATTRIBUTE = "variantGrouping::variantType"
GROUP_REFERENCE_ATTRIBUTE = "variantGrouping::groupReferenceId"

PRIMARY_VARIANT = "Primary Variant"
NON_PRIMARY_VARIANT = "Non-Primary Variant"

_GROUPED_VARIANT_TYPES = frozenset({PRIMARY_VARIANT, NON_PRIMARY_VARIANT})


def attribute_value(record: dict, attribute_id: str) -> str | None:
    """Return the first matching attribute value, or None if missing or empty."""
    for attr in record.get("attributes") or []:
        if str(attr.get("attributeId")) == attribute_id:
            value = attr.get("value")
            if value is not None and str(value).strip():
                return str(value).strip()
    return None


@dataclass(frozen=True)
class VariantGroupRef:
    """Primary / Non-Primary SKU that shares a groupReferenceId."""

    group_reference_id: str
    variant_type: str


def parse_variant_group_ref(record: dict) -> VariantGroupRef | None:
    """Return grouping metadata when the record participates in a variant listing."""
    variant_type = attribute_value(record, VARIANT_TYPE_ATTRIBUTE)
    if variant_type not in _GROUPED_VARIANT_TYPES:
        return None
    group_ref = attribute_value(record, GROUP_REFERENCE_ATTRIBUTE)
    if not group_ref:
        return None
    return VariantGroupRef(group_reference_id=group_ref, variant_type=variant_type)


def index_submission_rows_by_supplier_part(rows: list) -> dict[str, dict]:
    """Build supplierPartNumber -> status row map from submissionsV2 poll."""
    indexed: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = row.get("supplierPartNumber")
        if key is not None:
            indexed[str(key)] = row
    return indexed


def submission_rows_terminal(rows: list[dict]) -> bool:
    """Return True when every poll row has left PENDING / PROCESSING states."""
    if not rows:
        return False
    for row in rows:
        if row.get("validationStatus") == "PENDING":
            return False
        if row.get("submissionStatus") == "PROCESSING":
            return False
    return True


def attribute_errors_from_status_row(
    row: dict,
    *,
    format_flaw: Callable,
) -> dict[str, list[str]]:
    """Turn validationFlaws on one productAdditionStatus row into export errors."""
    attribute_errors: dict[str, list[str]] = {}
    flaws = row.get("validationFlaws") or []
    for flaw in flaws:
        if flaw.get("flawType") != "ERROR":
            continue
        attr_id = str(flaw.get("attributeId") or "_general")
        attribute_errors.setdefault(attr_id, []).append(format_flaw(flaw))
    if row.get("validationStatus") == "FAILED" and not attribute_errors:
        attribute_errors["_general"] = [
            "Submission failed with no specific ERROR flaws listed"
        ]
    return attribute_errors


def raise_for_failed_status_row(row: dict, *, format_flaw: Callable) -> None:
    """Raise InvalidPayloadError when a poll row failed validation."""
    if row.get("validationStatus") != "FAILED":
        return
    errors = attribute_errors_from_status_row(row, format_flaw=format_flaw)
    raise structured_export_error(errors)
