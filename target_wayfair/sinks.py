from typing import List, Optional

import singer
from hotglue_singer_sdk.exceptions import FatalAPIError

from target_wayfair.client import WayfairSink
from target_wayfair.export_errors import general_export_error, structured_export_error
from target_wayfair.submission_batch import (
    attribute_errors_from_status_row,
    index_submission_rows_by_supplier_part,
    parse_variant_group_ref,
)

LOGGER = singer.get_logger()


class ProductsSink(WayfairSink):
    """Sink for the 'Products' stream.

    Standalone records are one ``productAddition.submitV2`` each. Primary /
    Non-Primary variant SKUs that share a ``groupReferenceId`` are buffered and
    submitted in a single ``submitV2``. The stream must list each variant group
    contiguously.

    Expected record shape::

        {
            "productId": "<your internal SKU / reference>",
            "classId": "<Wayfair taxonomyCategoryId, e.g. '12' for Beds>",
            "attributes": [
                {
                    "attributeId": "core::supplierPartNumber",
                    "value": "YOUR-SKU-001",
                    "parentRank": 1,
                    "rank": 1,
                },
                {
                    "attributeId": "core::productName",
                    "value": "My Bed",
                    "parentRank": 1,
                    "rank": 1,
                },
                {
                    "attributeId": "variantGrouping::variantType",
                    "value": "Primary Variant",
                    "parentRank": 1,
                    "rank": 1,
                },
                {
                    "attributeId": "variantGrouping::groupReferenceId",
                    "value": "GRP-001",
                    "parentRank": 1,
                    "rank": 1,
                },
            ],
            "marketContext": {
                "locale": "en-US",
                "country": "UNITED_STATES",
                "brand": "WAYFAIR",
            },
        }
    """

    name = "Products"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Custom: defer Primary / Non-Primary rows until the variant group is flushed.
        self._variant_batch: List[dict] = []
        self._variant_group_key: Optional[str] = None

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
        attributes = self.ensure_class_name_attribute(
            attributes, record["classId"], supported
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

    def process_record(self, record: dict, context: dict) -> None:
        """Process one Singer RECORD (override of ``HotglueSink.process_record``).

        The SDK calls ``upsert_record`` on every record as soon as it passes
        preprocess and dedup checks. Wayfair variant listings need at least two
        SKUs in the same ``submitV2``, so we cannot upsert immediately for
        Primary / Non-Primary rows.

        Flow: run the same prepare path as the SDK, then either buffer variant
        SKUs (contiguous ``groupReferenceId``) or flush the buffer and submit.
        Standalone products use ``_submit_single_record`` (SDK upsert + bookmark).
        A trailing variant group is flushed from ``clean_up`` at end of stream.
        """
        # --- HotglueSink.process_record (unchanged semantics) -----------------
        if not self.latest_state:
            self.init_state()

        snapshot_field_values = context.pop(
            self.TARGET_STATE_FIELD_VALUES_CONTEXT_KEY, None
        )
        if snapshot_field_values is None and self._target_state_fields:
            snapshot_field_values = self.capture_target_state_field_values(record)
        external_id = None
        external_id_key = self._target.EXTERNAL_ID_KEY

        try:
            if self.name not in self.allows_externalid and (
                record.get(external_id_key) or record.get(external_id_key.lower())
            ):
                external_id = record.pop(external_id_key, None) or record.pop(
                    external_id_key.lower(), None
                )

            record = self.preprocess_record(record, context)

            if record and external_id:
                record[self._target.EXTERNAL_ID_KEY] = external_id
        except Exception as e:
            self.logger.exception(f"Preprocess record error {str(e)}")
            self.update_state(
                self._build_record_error_state(
                    e,
                    record=record,
                    external_id=external_id,
                ),
                record=record,
            )
            return

        record_hash = self.build_record_hash(record)

        if record_hash in self.processed_hashes:
            self.logger.info(
                f"Record of type {self.name} already exists with hash: {record_hash}"
            )
            return

        existing_state = self.get_existing_state(record_hash)

        if self.name in self.allows_externalid:
            external_id = record.get(external_id_key) or record.get(
                external_id_key.lower()
            )
        else:
            external_id = record.pop(external_id_key, None) or record.pop(
                external_id_key.lower(), None
            )

        if existing_state:
            return self.update_state(
                existing_state,
                is_duplicate=True,
                record=record,
            )
        # --- End SDK prepare path; SDK would call upsert_record next. --------

        # Custom: buffer variant SKUs; flush when group id changes or before standalone.
        grouping = parse_variant_group_ref(record)
        if grouping:
            if (
                self._variant_group_key
                and self._variant_group_key != grouping.group_reference_id
            ):
                self._flush_variant_batch()

            self._variant_group_key = grouping.group_reference_id
            self._variant_batch.append(
                {
                    "record": record,
                    "record_hash": record_hash,
                    "external_id": external_id,
                    "snapshot_field_values": snapshot_field_values,
                }
            )
            LOGGER.info(
                "Buffered variant SKU %s in group %s (%d in batch)",
                record.get("productId"),
                grouping.group_reference_id,
                len(self._variant_batch),
            )
            return  # No upsert yet; wayfair call happens on flush / clean_up.

        self._flush_variant_batch()
        self._submit_single_record(
            record,
            record_hash=record_hash,
            external_id=external_id,
            snapshot_field_values=snapshot_field_values,
        )

    def clean_up(self) -> None:
        """Flush a trailing variant group (SDK calls this at end of stdin)."""
        try:
            self._flush_variant_batch()
        except Exception as e:
            self.logger.exception(
                "Error flushing buffered variant group on clean_up: %s", str(e)
            )
            for item in list(self._variant_batch):
                self.update_state(
                    self._product_error_state(
                        e,
                        record=item["record"],
                        external_id=item.get("external_id"),
                        record_hash=item.get("record_hash"),
                    ),
                    record=item["record"],
                )
            self._clear_variant_buffer()

    def _flush_variant_batch(self) -> None:
        """One ``submitV2`` for the open variant group; one bookmark per buffered SKU."""
        if not self._variant_batch:
            return

        batch = self._variant_batch
        group_key = self._variant_group_key
        # Clear before API call so a failed clean_up cannot submit the same SKUs twice.
        self._clear_variant_buffer()
        records = [item["record"] for item in batch]
        LOGGER.info(
            "Submitting variant group %s with %d SKU(s)",
            group_key,
            len(records),
        )

        try:
            request_id, statuses = self.submit_records(records)
        except Exception as e:
            self.logger.exception(
                "Variant group submit failed for %s: %s", group_key, str(e)
            )
            for item in batch:
                self.update_state(
                    self._product_error_state(
                        e,
                        record=item["record"],
                        external_id=item.get("external_id"),
                        record_hash=item.get("record_hash"),
                    ),
                    record=item["record"],
                )
            return

        # Custom: map each poll row to a Singer bookmark (SDK assumes one row per upsert).
        indexed = index_submission_rows_by_supplier_part(statuses)
        for item in batch:
            record = item["record"]
            wayfair_product_id = self.resolve_submission_product_id(record)
            status_row = indexed.get(wayfair_product_id)
            if not status_row:
                self.update_state(
                    self._product_error_state(
                        FatalAPIError(
                            f"No submissionsV2 row for {wayfair_product_id} "
                            f"(requestId {request_id})"
                        ),
                        record=record,
                        external_id=item.get("external_id"),
                        record_hash=item["record_hash"],
                    ),
                    record=record,
                )
                continue

            if status_row.get("validationStatus") == "FAILED":
                errors = attribute_errors_from_status_row(
                    status_row,
                    format_flaw=self.format_validation_flaw,
                )
                self.update_state(
                    self._product_error_state(
                        structured_export_error(errors),
                        record=record,
                        external_id=item.get("external_id"),
                        record_hash=item["record_hash"],
                    ),
                    record=record,
                )
                continue

            state = {
                "success": True,
                "hash": item["record_hash"],
                "id": record["productId"],
            }
            if item.get("external_id"):
                state["externalId"] = item["external_id"]
            self.logger.info(
                "Variant group %s — product %s validated: %s",
                group_key,
                record["productId"],
                status_row.get("validationStatus"),
            )
            self.update_state(
                state,
                record=record,
                snapshot_field_values=item.get("snapshot_field_values"),
            )

    def _clear_variant_buffer(self) -> None:
        """Drop any buffered variant group after flush completes."""
        self._variant_batch = []
        self._variant_group_key = None

    def _product_error_state(
        self,
        error: Exception,
        *,
        record: dict,
        external_id=None,
        record_hash: Optional[str] = None,
    ) -> dict:
        """Failed bookmark with ``id`` set to ``productId`` (Products stream convention)."""
        state = self._build_record_error_state(
            error,
            record=record,
            external_id=external_id,
            record_hash=record_hash,
            identifiers={"id": str(record["productId"])},
        )
        return state

    def _submit_single_record(
        self,
        record: dict,
        *,
        record_hash: str,
        external_id,
        snapshot_field_values,
    ) -> None:
        """Tail of ``HotglueSink.process_record``: ``upsert_record`` then ``update_state``."""
        try:
            record_id, success, state_updates = self.upsert_record(record, {})
        except Exception as e:
            self.logger.exception(f"Upsert record error {str(e)}")
            self.update_state(
                self._product_error_state(
                    e,
                    record=record,
                    external_id=external_id,
                    record_hash=record_hash,
                ),
                record=record,
            )
            return

        if success:
            self.logger.info(f"{self.name} processed id: {record_id}")

        state = {"success": success, "hash": record_hash}
        if record_id:
            state["id"] = record_id
        if external_id:
            state["externalId"] = external_id
        if state_updates and isinstance(state_updates, dict):
            state = dict(state, **state_updates)

        self.update_state(
            state,
            record=record,
            snapshot_field_values=snapshot_field_values,
        )
