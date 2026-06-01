from hotglue_etl_exceptions import InvalidPayloadError

from target_wayfair.client import WayfairSink


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
        """Validate required fields before submitting to Wayfair."""
        missing = [f for f in ("productId", "classId", "attributes") if not record.get(f)]
        if missing:
            raise InvalidPayloadError(
                f"Wayfair Products record is missing required fields: {', '.join(missing)}"
            )

        attributes = record["attributes"]
        if not isinstance(attributes, list):
            raise InvalidPayloadError(
                "Wayfair Products record 'attributes' must be a list"
            )

        for i, attr in enumerate(attributes):
            for field in ("attributeId", "value", "parentRank", "rank"):
                if attr.get(field) is None:
                    raise InvalidPayloadError(
                        f"attributes[{i}] is missing required field '{field}'"
                    )

        return record
