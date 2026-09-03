import singer

from target_wayfair.client import WayfairSink
from target_wayfair.export_errors import general_export_error, structured_export_error

LOGGER = singer.get_logger()


class ProductsSink(WayfairSink):
    """Sink for the 'Products' stream.

    Each record maps to one proposed product addition in the Wayfair Product
    Catalog API (productAddition.submitV2).

    Expected record shape:

        {
            "productId":  "<your internal SKU / reference>",
            "classId":    "<Wayfair taxonomyCategoryId, e.g. '12' for Beds>",
            "attributes": [
                {"attributeId": "core::productName", "value": "...", "parentRank": 1, "rank": 1},
                {"attributeId": "shippingAndFulfillment::depth", "value": "12.0",
                 "parentRank": 1, "rank": 1, "attributeInstance": 1},
                ...
            ],
            # Optional overrides (default to config / en-US / UNITED_STATES / WAYFAIR):
            "marketContext": {"locale": "en-US", "country": "UNITED_STATES", "brand": "WAYFAIR"},
            "jobContext":    {"productAdditionRequestId": null, "hasMoreProducts": false}
        }

    The target submits each record as its own single-product batch and polls
    submissionsV2 for the validation result.  ERROR-level flaws raise
    InvalidPayloadError; WARNING-level flaws are logged and ignored.
    """

    name = "Products"

    def preprocess_record(self, record: dict, context: dict) -> dict:
        """Validate required fields and drop attributes Wayfair does not support."""
        missing = [f for f in ("productId", "classId", "attributes") if not record.get(f)]
        if missing:
            raise general_export_error(
                f"Wayfair Products record is missing required fields: {', '.join(missing)}"
            )

        attributes = record["attributes"]
        if not isinstance(attributes, list):
            raise general_export_error(
                "Wayfair Products record 'attributes' must be a list"
            )

        supported = self.get_supported_attributes(
            record["classId"], self.resolve_market_context(record)
        )
        kept = []
        dropped = []
        for attr in attributes:
            attr_id = attr.get("attributeId")
            if attr_id is not None and str(attr_id) in supported:
                kept.append(attr)
            else:
                dropped.append(attr_id)

        if dropped:
            LOGGER.info(
                "Dropped %d unsupported attribute(s) for product %s classId %s: %s",
                len(dropped),
                record.get("productId"),
                record.get("classId"),
                dropped,
            )

        if not kept:
            raise general_export_error(
                f"Wayfair Products record for {record.get('productId')} has no "
                f"attributes supported by classId {record.get('classId')}"
            )

        record["attributes"] = kept
        for i, attr in enumerate(record["attributes"]):
            for field in ("attributeId", "value", "parentRank", "rank"):
                if attr.get(field) is None:
                    raise general_export_error(
                        f"attributes[{i}] is missing required field '{field}'"
                    )

        attribute_errors: dict[str, list[str]] = {}
        for attr in record["attributes"]:
            spec = supported[str(attr["attributeId"])]
            attr["value"] = self.normalize_attribute_value(attr["value"], spec)
            issue = self.describe_attribute_value_issue(
                attr["attributeId"], attr.get("value"), spec
            )
            if issue:
                attribute_errors.setdefault(str(attr["attributeId"]), []).append(issue)

        if attribute_errors:
            raise structured_export_error(attribute_errors)

        return record
