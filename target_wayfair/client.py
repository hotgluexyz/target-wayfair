import json
import time

import requests
import singer
from hotglue_etl_exceptions import InvalidCredentialsError
from hotglue_singer_sdk.exceptions import FatalAPIError, RetriableAPIError
from hotglue_singer_sdk.plugin_base import PluginBase
from hotglue_singer_sdk.target_sdk.client import HotglueSink

from target_wayfair.auth import WayfairAuth
from target_wayfair.export_errors import general_export_error
from target_wayfair.submission_batch import (
    index_submission_rows_by_supplier_part,
    raise_for_failed_status_row,
    submission_rows_terminal,
)

LOGGER = singer.get_logger()

DEFAULT_CATALOG_API_BASE = "https://api.wayfair.io"
SANDBOX_CATALOG_API_BASE = "https://api.wayfair.io/sandbox"
CATALOG_GRAPHQL_PATH = "/v1/product-catalog-api/graphql"

GRAPHQL_URL = f"{DEFAULT_CATALOG_API_BASE}{CATALOG_GRAPHQL_PATH}"


def catalog_graphql_url(config: dict) -> str:
    """Resolve Product Catalog GraphQL URL from target config (sandbox vs production)."""
    if config.get("is_sandbox"):
        return f"{SANDBOX_CATALOG_API_BASE}{CATALOG_GRAPHQL_PATH}"
    return GRAPHQL_URL


# Maximum number of attempts when polling for a non-PROCESSING status.
POLL_MAX_ATTEMPTS = 20
POLL_SLEEP_SECONDS = 3

SUBMIT_V2_MUTATION = """
mutation SubmitProductAdditions($request: SubmitProductAdditionsRequestV2!) {
  productAddition {
    submitV2(request: $request) {
      batchId
      status
      productAdditionRequestId
    }
  }
}
"""


class WayfairSink(HotglueSink):
    """Base sink for the Wayfair Product Catalog GraphQL API."""

    def __init__(
        self,
        target: PluginBase,
        stream_name: str,
        schema: dict,
        key_properties: list[str] | None,
    ) -> None:
        super().__init__(target, stream_name, schema, key_properties)
        self._auth = WayfairAuth(self._target)
        # classId + market → {taxonomyAttributeId: attribute spec}
        self._taxonomy_attr_cache: dict[tuple[str, str, str, str], dict[str, dict]] = {}

    @property
    def base_url(self) -> str:
        return catalog_graphql_url(self.config)

    def _graphql(self, query: str, variables: dict | None = None) -> dict:
        """Send a GraphQL operation and return the parsed JSON body."""
        supplier_id = self.config.get("supplier_id", "")
        payload: dict = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        response = requests.post(
            self.base_url,
            headers={
                **self._auth.auth_headers,
                "Content-Type": "application/json",
                "X-SELECTED-SUPPLIER-ID": str(supplier_id),
            },
            json=payload,
            timeout=30,
        )
        self.validate_response(response)
        return response.json()

    def resolve_market_context(self, record: dict) -> dict:
        """Return the record's marketContext, falling back to config / defaults."""
        return record.get("marketContext") or {
            "locale": self.config.get("locale", "en-US"),
            "country": self.config.get("country", "UNITED_STATES"),
            "brand": self.config.get("brand", "WAYFAIR"),
        }

    _ATTRIBUTE_FIELDS = """
      taxonomyAttributeId
      title
      description
      requirement
      valueFormat {
        canValueBeCustomized
        canValueBeSetToUnavailable
        canValueBeSetToNotApplicable
        datatype
      }
      possibleAttributeValues {
        value
        definition
      }
      parentAttributeId
    """

    @staticmethod
    def _attributes_from_filter_result(result) -> list:
        """Normalize attributesByFilter, which may be an object or a list of objects."""
        if isinstance(result, list):
            attributes = []
            for item in result:
                if not isinstance(item, dict):
                    continue
                if "attributes" in item:
                    attributes.extend(item.get("attributes") or [])
                else:
                    attributes.append(item)
            return attributes
        if isinstance(result, dict):
            return result.get("attributes") or []
        return []

    @staticmethod
    def _index_taxonomy_attributes(attributes: list) -> dict[str, dict]:
        indexed: dict[str, dict] = {}
        for attr in attributes or []:
            if not isinstance(attr, dict):
                continue
            attr_id = attr.get("taxonomyAttributeId")
            if attr_id is not None:
                indexed[str(attr_id)] = attr
            indexed.update(
                WayfairSink._index_taxonomy_attributes(attr.get("childAttributes") or [])
            )
        return indexed

    def _taxonomy_cache_key(self, class_id, market_context: dict) -> tuple[str, str, str, str]:
        return (
            str(class_id),
            str(market_context.get("brand", "WAYFAIR")),
            str(market_context.get("country", "UNITED_STATES")),
            str(market_context.get("locale", "en-US")),
        )

    def get_supported_attributes(self, class_id, market_context: dict) -> dict[str, dict]:
        """Return the full Wayfair attribute spec map for this class + market.

        Results are cached for the lifetime of the sink so products that share
        a classId do not re-query attributesByFilter.
        """
        cache_key = self._taxonomy_cache_key(class_id, market_context)
        if cache_key in self._taxonomy_attr_cache:
            return self._taxonomy_attr_cache[cache_key]

        locale = market_context.get("locale", "en-US")
        country = market_context.get("country", "UNITED_STATES")
        brand = market_context.get("brand", "WAYFAIR")
        try:
            class_id_gql = int(class_id)
        except (TypeError, ValueError):
            raise general_export_error(
                f"Wayfair classId must be an integer, got {class_id!r}"
            )

        query = f"""
query GetTaxonomyAttributesByFilter {{
  attributesByFilter(
    input: {{
      classId: {class_id_gql}
      marketContext: {{
        brand: {brand}
        country: {country}
        locale: {json.dumps(locale)}
      }}
    }}
  ) {{
    classId
    attributes {{
      {self._ATTRIBUTE_FIELDS}
      childAttributes {{
        {self._ATTRIBUTE_FIELDS}
      }}
    }}
  }}
}}
"""
        body = self._graphql(query)
        result = (body.get("data") or {}).get("attributesByFilter")
        supported = self._index_taxonomy_attributes(
            self._attributes_from_filter_result(result)
        )
        if not supported:
            raise FatalAPIError(
                f"Wayfair attributesByFilter returned no attributes for classId {class_id}"
            )

        LOGGER.info(
            "Cached %d supported Wayfair attributes for classId %s",
            len(supported),
            class_id,
        )
        self._taxonomy_attr_cache[cache_key] = supported
        return supported

    def lookup_cached_attribute(self, attribute_id: str) -> dict | None:
        """Find a previously fetched attribute spec by taxonomyAttributeId."""
        attr_id = str(attribute_id)
        for specs in self._taxonomy_attr_cache.values():
            spec = specs.get(attr_id)
            if spec:
                return spec
        return None

    # Wayfair taxonomy attribute "1" is the product class name (title: "Class ID").
    CLASS_NAME_ATTRIBUTE_ID = "1"

    def ensure_class_name_attribute(
        self, attributes: list, class_id, supported: dict[str, dict]
    ) -> list:
        """Set attributeId 1 (class name) from taxonomy, always overriding any value.

        ``classId`` on the root record already identifies the class; Wayfair still
        requires attribute ``1`` with the class display name. That name is the sole
        possible value on the class-filtered taxonomy for attribute ``1``.
        """
        spec = supported.get(self.CLASS_NAME_ATTRIBUTE_ID)
        if not spec:
            return attributes

        possible = self._possible_values(spec)
        if len(possible) != 1:
            raise general_export_error(
                f"Wayfair class attribute ({self.CLASS_NAME_ATTRIBUTE_ID}) for "
                f"classId {class_id} must have exactly one possible value; "
                f"got {possible!r}"
            )

        class_name = possible[0]
        class_attr = {
            "attributeId": self.CLASS_NAME_ATTRIBUTE_ID,
            "value": class_name,
            "parentRank": 1,
            "rank": 1,
        }
        others = [
            attr
            for attr in attributes
            if str(attr.get("attributeId")) != self.CLASS_NAME_ATTRIBUTE_ID
        ]
        LOGGER.info(
            "Setting class name attribute %s=%r for classId %s",
            self.CLASS_NAME_ATTRIBUTE_ID,
            class_name,
            class_id,
        )
        return [class_attr, *others]

    @staticmethod
    def _possible_values(spec: dict) -> list[str]:
        values = []
        for entry in spec.get("possibleAttributeValues") or []:
            if isinstance(entry, dict) and entry.get("value") is not None:
                values.append(str(entry["value"]))
            elif isinstance(entry, str):
                values.append(entry)
        return values

    @staticmethod
    def _format_valid_values(spec: dict) -> str:
        parts = []
        for entry in spec.get("possibleAttributeValues") or []:
            if isinstance(entry, str):
                parts.append(entry)
                continue
            if not isinstance(entry, dict):
                continue
            value = entry.get("value")
            if value is None:
                continue
            definition = entry.get("definition")
            if definition and str(definition) != str(value):
                parts.append(f"{value} ({definition})")
            else:
                parts.append(str(value))
        return ", ".join(parts)

    @staticmethod
    def _label_attribute(attribute_id: str, spec: dict | None) -> str:
        title = (spec or {}).get("title")
        return f"{attribute_id} ({title})" if title else str(attribute_id)

    def _compose_attribute_issue(self, attribute_id: str, spec: dict, reason: str) -> str:
        parts = [f"{self._label_attribute(attribute_id, spec)}: {reason}"]
        description = spec.get("description")
        if description:
            parts.append(description)
        valid_values = self._format_valid_values(spec)
        datatype = (spec.get("valueFormat") or {}).get("datatype")
        if valid_values:
            parts.append(f"Valid values: {valid_values}")
        elif datatype:
            parts.append(f"Expected datatype: {datatype}")
        return " ".join(parts)

    @staticmethod
    def _matches_unset_value(value: str, value_format: dict) -> bool:
        lower = value.lower()
        if value_format.get("canValueBeSetToNotApplicable") and lower in {
            "does not apply",
            "n/a",
        }:
            return True
        return bool(
            value_format.get("canValueBeSetToUnavailable") and lower == "unavailable"
        )

    _TRUE_VALUES = {"true", "1", "yes"}
    _FALSE_VALUES = {"false", "0", "no"}
    _YES_NO_EXTRAS = {"does not apply", "unavailable", "n/a", "not applicable"}
    _NOT_APPLICABLE_ALIASES = {"not applicable", "n/a"}
    _REGION_ALIASES = {
        "united states": "US",
        "united states of america": "US",
        "usa": "US",
        "u.s.": "US",
        "u.s.a.": "US",
        "america": "US",
        "united kingdom": "UK",
        "great britain": "UK",
        "britain": "UK",
        "england": "UK",
        "european union": "EU",
        "europe": "EU",
        "canada": "CA",
    }

    def _yes_no_labels(self, spec: dict) -> tuple[str, str]:
        yes, no = "Yes", "No"
        for value in self._possible_values(spec):
            if value.lower() == "yes":
                yes = value
            elif value.lower() == "no":
                no = value
        return yes, no

    def _is_yes_no_attribute(self, spec: dict) -> bool:
        value_format = spec.get("valueFormat") or {}
        if (value_format.get("datatype") or "").upper() == "BOOLEAN":
            return True
        possible = {value.lower() for value in self._possible_values(spec)}
        extras = possible - {"yes", "no"} - self._YES_NO_EXTRAS
        return {"yes", "no"}.issubset(possible) and not extras

    def normalize_boolean_value(self, value, spec: dict):
        """Convert boolean-like values to Wayfair Yes/No when the spec expects that."""
        if not self._is_yes_no_attribute(spec):
            return value
        if isinstance(value, bool):
            truthy = value
        elif isinstance(value, (int, float)) and value in (0, 1):
            truthy = bool(value)
        else:
            lowered = str(value).strip().lower()
            if lowered in self._TRUE_VALUES:
                truthy = True
            elif lowered in self._FALSE_VALUES:
                truthy = False
            else:
                return value
        yes, no = self._yes_no_labels(spec)
        return yes if truthy else no

    def _does_not_apply_label(self, spec: dict) -> str:
        for value in self._possible_values(spec):
            if value.lower() == "does not apply":
                return value
        return "Does Not Apply"

    def normalize_not_applicable_value(self, value, spec: dict):
        """Map 'Not Applicable' / 'N/A' to Wayfair's 'Does Not Apply'."""
        target = self._does_not_apply_label(spec)

        def _map_part(part):
            if str(part).strip().lower() in self._NOT_APPLICABLE_ALIASES:
                return target
            return part

        if isinstance(value, str) and ";" in value:
            return "; ".join(_map_part(part.strip()) for part in value.split(";"))
        return _map_part(value)

    def normalize_region_value(self, value, spec: dict):
        """Map full region/country names to codes when the spec expects codes."""
        possible = self._possible_values(spec)
        if not possible:
            return value
        possible_by_lower = {item.lower(): item for item in possible}

        def _map_part(part):
            text = str(part).strip()
            if text in possible:
                return text
            if text.lower() in possible_by_lower:
                return possible_by_lower[text.lower()]
            code = self._REGION_ALIASES.get(text.lower())
            if code and code.lower() in possible_by_lower:
                return possible_by_lower[code.lower()]
            return part

        if isinstance(value, str) and ";" in value:
            return "; ".join(_map_part(part.strip()) for part in value.split(";"))
        return _map_part(value)

    def normalize_attribute_value(self, value, spec: dict):
        value = self.normalize_not_applicable_value(value, spec)
        value = self.normalize_region_value(value, spec)
        return self.normalize_boolean_value(value, spec)

    @staticmethod
    def _datatype_issue(value: str, datatype: str | None) -> str | None:
        if datatype == "BOOLEAN" and value.lower() not in {
            "true",
            "false",
            "0",
            "1",
            "yes",
            "no",
        }:
            return f"value {value!r} is not a boolean"
        if datatype == "INTEGER":
            try:
                int(value.strip())
            except (TypeError, ValueError):
                return f"value {value!r} is not an integer"
        if datatype == "DECIMAL":
            try:
                float(value.strip())
            except (TypeError, ValueError):
                return f"value {value!r} is not a decimal number"
        return None

    def describe_attribute_value_issue(
        self, attribute_id: str, value, spec: dict
    ) -> str | None:
        """Return a detailed error if `value` is not valid for this attribute spec."""
        value_str = str(value)
        value_format = spec.get("valueFormat") or {}
        datatype = value_format.get("datatype")
        customizable = bool(value_format.get("canValueBeCustomized"))
        possible_values = self._possible_values(spec)

        if self._matches_unset_value(value_str, value_format):
            return None

        if datatype == "MULTI_CHOICE":
            parts = [part.strip() for part in value_str.split(";") if part.strip()]
            if not parts:
                return self._compose_attribute_issue(
                    attribute_id, spec, f"value {value_str!r} is empty"
                )
            invalid = [part for part in parts if part not in possible_values]
            if invalid and possible_values and not customizable:
                return self._compose_attribute_issue(
                    attribute_id,
                    spec,
                    f"value {value_str!r} contains invalid entries {invalid}",
                )
            if not invalid:
                return None
        elif value_str in possible_values:
            return None
        elif possible_values and not customizable:
            return self._compose_attribute_issue(
                attribute_id, spec, f"value {value_str!r} is not an allowed value"
            )

        datatype_issue = self._datatype_issue(value_str, datatype)
        if datatype_issue:
            return self._compose_attribute_issue(attribute_id, spec, datatype_issue)
        return None

    def format_validation_flaw(self, flaw: dict) -> str:
        """Explain a Wayfair validationFlaw using cached taxonomy metadata."""
        attr_id = str(flaw.get("attributeId") or "")
        spec = self.lookup_cached_attribute(attr_id) if attr_id else None
        label = self._label_attribute(attr_id, spec) if attr_id else "unknown attribute"
        flaw_text = flaw.get("flaw") or "validation failed"
        if not spec:
            return f"{label}: {flaw_text}"
        extras = []
        description = spec.get("description")
        if description:
            extras.append(description)
        valid_values = self._format_valid_values(spec)
        if valid_values:
            extras.append(f"Valid values: {valid_values}")
        extra = f" {' '.join(extras)}" if extras else ""
        return f"{label}: {flaw_text}.{extra}"

    def validate_response(self, response: requests.Response) -> None:
        """Raise the appropriate exception based on HTTP status."""
        if response.status_code == 401:
            raise InvalidCredentialsError(
                f"Wayfair returned 401 Unauthorized: {response.text}"
            )
        if response.status_code == 429 or 500 <= response.status_code < 600:
            raise RetriableAPIError(
                f"Wayfair returned {response.status_code}: {response.text}",
                response,
            )
        if 400 <= response.status_code < 500:
            try:
                body = response.json()
                errors = body.get("errors", [])
                msg = "; ".join(e.get("message", "") for e in errors) or response.text
            except Exception:
                msg = response.text
            raise FatalAPIError(f"Wayfair API error ({response.status_code}): {msg}")

        # GraphQL returns 200 even for errors; surface them as fatal.
        try:
            body = response.json()
        except Exception:
            raise FatalAPIError(f"Wayfair invalid JSON response: {response.text}")
        gql_errors = body.get("errors")
        if gql_errors:
            msg = "; ".join(e.get("message", "") for e in gql_errors)
            raise FatalAPIError(f"Wayfair GraphQL error: {msg}")

    def poll_submission_status(self, request_id: str) -> dict:
        """Poll submissionsV2 until the batch completes; return the first status row."""
        rows = self.poll_submission_statuses(request_id)
        if not rows:
            raise FatalAPIError(
                f"Wayfair submission {request_id} returned no productAdditionStatus rows"
            )
        return rows[0]

    def poll_submission_statuses(self, request_id: str) -> list[dict]:
        """Poll submissionsV2 until every row is terminal; return all status rows."""
        query = f"""
        query {{
          productAddition {{
            submissionsV2(request: {{ productAdditionRequestId: "{request_id}" }}) {{
              productAdditionStatus {{
                supplierPartNumber
                classId
                submissionStatus
                validationStatus
                validationFlaws {{
                  validationFlawId
                  attributeId
                  flawType
                  flaw
                }}
              }}
            }}
          }}
        }}
        """
        for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
            body = self._graphql(query)
            product_addition = (body.get("data") or {}).get("productAddition") or {}
            statuses = (product_addition.get("submissionsV2") or {}).get("productAdditionStatus") or []
            if not statuses:
                LOGGER.warning(
                    "submissionsV2 returned empty productAdditionStatus for %s "
                    "(attempt %d/%d)",
                    request_id,
                    attempt,
                    POLL_MAX_ATTEMPTS,
                )
                time.sleep(POLL_SLEEP_SECONDS)
                continue

            if not submission_rows_terminal(statuses):
                summary = ", ".join(
                    f"{row.get('supplierPartNumber')}:{row.get('validationStatus')}"
                    for row in statuses
                )
                LOGGER.info(
                    "Submission %s still processing (%d row(s)): %s",
                    request_id,
                    len(statuses),
                    summary,
                )
                time.sleep(POLL_SLEEP_SECONDS)
                continue

            for row in statuses:
                flaws = row.get("validationFlaws") or []
                warnings = [f for f in flaws if f.get("flawType") == "WARNING"]
                if warnings:
                    LOGGER.warning(
                        "Wayfair validation warnings for %s (%s): %s",
                        request_id,
                        row.get("supplierPartNumber"),
                        "; ".join(self.format_validation_flaw(w) for w in warnings),
                    )
            return statuses

        raise RetriableAPIError(
            f"Wayfair submission {request_id} stayed PROCESSING after "
            f"{POLL_MAX_ATTEMPTS} attempts ({POLL_MAX_ATTEMPTS * POLL_SLEEP_SECONDS}s)",
        )

    @staticmethod
    def _attribute_to_input(attr: dict) -> dict:
        """Map a Singer attribute entry to Wayfair AttributeInput."""
        entry: dict = {
            "attributeId": str(attr["attributeId"]),
            "value": str(attr["value"]),
            "parentRank": int(attr["parentRank"]),
            "rank": int(attr["rank"]),
        }
        if attr.get("attributeInstance") is not None:
            entry["attributeInstance"] = int(attr["attributeInstance"])
        return entry

    def build_submit_v2_variables(
        self,
        proposed_products: list[dict],
        market_context: dict,
        job_context: dict | None = None,
    ) -> dict:
        """Build GraphQL variables for productAddition.submitV2."""
        if not proposed_products:
            raise FatalAPIError("submitV2 requires at least one proposedProductAddition")

        job_context = job_context or {
            "productAdditionRequestId": None,
            "hasMoreProducts": False,
        }
        additions = []
        for product in proposed_products:
            additions.append(
                {
                    "productId": str(product["productId"]),
                    "classId": str(product["classId"]),
                    "attributes": [
                        self._attribute_to_input(a) for a in product["attributes"]
                    ],
                }
            )
        request_id = job_context.get("productAdditionRequestId")
        return {
            "request": {
                "marketContext": {
                    "locale": market_context.get("locale", "en-US"),
                    "country": market_context.get("country", "UNITED_STATES"),
                    "brand": market_context.get("brand", "WAYFAIR"),
                },
                "jobContext": {
                    "productAdditionRequestId": request_id,
                    "hasMoreProducts": bool(job_context.get("hasMoreProducts")),
                },
                "proposedProductAdditions": additions,
            }
        }

    @staticmethod
    def resolve_submission_product_id(record: dict) -> str:
        """Return the productId sent to Wayfair submitV2.

        Wayfair treats the submission productId as the supplier part number for
        catalog uniqueness. When core::supplierPartNumber is present, use it so
        the top-level productId matches the attribute value Plytix mapped.
        """
        for attr in record.get("attributes") or []:
            if str(attr.get("attributeId")) == "core::supplierPartNumber":
                value = attr.get("value")
                if value is not None and str(value).strip():
                    return str(value).strip()
        return str(record["productId"])

    def submit_records(
        self,
        records: list[dict],
        *,
        job_context: dict | None = None,
    ) -> tuple[str, list[dict]]:
        """Submit one or more preprocessed records in a single submitV2 and poll."""
        if not records:
            raise FatalAPIError("submit_records called with no records")

        market_context = self.resolve_market_context(records[0])
        proposed = []
        for record in records:
            wayfair_product_id = self.resolve_submission_product_id(record)
            if wayfair_product_id != record["productId"]:
                LOGGER.info(
                    "Using core::supplierPartNumber %s as Wayfair productId "
                    "(record productId is %s)",
                    wayfair_product_id,
                    record["productId"],
                )
            proposed.append(
                {
                    "productId": wayfair_product_id,
                    "classId": record["classId"],
                    "attributes": record["attributes"],
                }
            )

        variables = self.build_submit_v2_variables(
            proposed, market_context, job_context=job_context
        )
        body = self._graphql(SUBMIT_V2_MUTATION, variables)
        submit_result = (
            (body.get("data") or {}).get("productAddition") or {}
        ).get("submitV2") or {}
        request_id = submit_result.get("productAdditionRequestId")
        if not request_id:
            raise FatalAPIError(
                f"Wayfair submitV2 returned no productAdditionRequestId. "
                f"Response: {submit_result}"
            )
        LOGGER.info(
            "Submitted %d product(s) → batchId=%s requestId=%s",
            len(records),
            submit_result.get("batchId"),
            request_id,
        )
        statuses = self.poll_submission_statuses(request_id)
        return request_id, statuses

    def upsert_record(self, record: dict, context: dict):
        """Submit one product and wait for validation to complete."""
        record_product_id = record["productId"]
        wayfair_product_id = self.resolve_submission_product_id(record)
        job_context = record.get("jobContext") or {
            "productAdditionRequestId": None,
            "hasMoreProducts": False,
        }

        request_id, statuses = self.submit_records(
            [record], job_context=job_context
        )
        indexed = index_submission_rows_by_supplier_part(statuses)
        status = indexed.get(wayfair_product_id)
        if not status:
            raise FatalAPIError(
                f"No submissionsV2 status row for Wayfair productId {wayfair_product_id} "
                f"(requestId {request_id})"
            )
        raise_for_failed_status_row(
            status, format_flaw=self.format_validation_flaw
        )
        LOGGER.info(
            "Product %s (Wayfair productId %s) validated: validationStatus=%s",
            record_product_id,
            wayfair_product_id,
            status.get("validationStatus"),
        )
        return record_product_id, True, {}
