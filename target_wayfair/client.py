import json
import time
from typing import Dict, List, Optional, Tuple

import requests
import singer
from hotglue_etl_exceptions import InvalidCredentialsError, InvalidPayloadError
from hotglue_singer_sdk.exceptions import FatalAPIError, RetriableAPIError
from hotglue_singer_sdk.plugin_base import PluginBase
from hotglue_singer_sdk.target_sdk.client import HotglueSink

from target_wayfair.auth import WayfairAuth
from target_wayfair.export_errors import general_export_error, structured_export_error

LOGGER = singer.get_logger()

GRAPHQL_URL = "https://api.wayfair.io/v1/product-catalog-api/graphql"

# Maximum number of attempts when polling for a non-PROCESSING status.
POLL_MAX_ATTEMPTS = 20
POLL_SLEEP_SECONDS = 3


class WayfairSink(HotglueSink):
    """Base sink for the Wayfair Product Catalog GraphQL API."""

    def __init__(
        self,
        target: PluginBase,
        stream_name: str,
        schema: Dict,
        key_properties: Optional[List[str]],
    ) -> None:
        super().__init__(target, stream_name, schema, key_properties)
        self._auth = WayfairAuth(self._target)
        # classId + market → {taxonomyAttributeId: attribute spec}
        self._taxonomy_attr_cache: Dict[Tuple[str, str, str, str], Dict[str, dict]] = {}

    @property
    def base_url(self) -> str:
        return GRAPHQL_URL

    def _graphql(self, query: str) -> dict:
        """Send a raw GraphQL query/mutation string and return the parsed JSON body."""
        supplier_id = self.config.get("supplier_id", "")
        response = requests.post(
            self.base_url,
            headers={
                **self._auth.auth_headers,
                "Content-Type": "application/json",
                "X-SELECTED-SUPPLIER-ID": str(supplier_id),
            },
            json={"query": query},
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
    def _index_taxonomy_attributes(attributes: list) -> Dict[str, dict]:
        indexed: Dict[str, dict] = {}
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

    def _taxonomy_cache_key(self, class_id, market_context: dict) -> Tuple[str, str, str, str]:
        return (
            str(class_id),
            str(market_context.get("brand", "WAYFAIR")),
            str(market_context.get("country", "UNITED_STATES")),
            str(market_context.get("locale", "en-US")),
        )

    def get_supported_attributes(self, class_id, market_context: dict) -> Dict[str, dict]:
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

    def lookup_cached_attribute(self, attribute_id: str) -> Optional[dict]:
        """Find a previously fetched attribute spec by taxonomyAttributeId."""
        attr_id = str(attribute_id)
        for specs in self._taxonomy_attr_cache.values():
            spec = specs.get(attr_id)
            if spec:
                return spec
        return None

    @staticmethod
    def _possible_values(spec: dict) -> List[str]:
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
    def _label_attribute(attribute_id: str, spec: Optional[dict]) -> str:
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

    def _yes_no_labels(self, spec: dict) -> Tuple[str, str]:
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
    def _datatype_issue(value: str, datatype: Optional[str]) -> Optional[str]:
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
    ) -> Optional[str]:
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
        """Poll submissionsV2 until the batch leaves PROCESSING status.

        Returns the first productAdditionStatus entry on completion.
        Raises InvalidPayloadError when validationStatus is FAILED (ERROR-level flaws).
        """
        query = f"""
        query {{
          productAddition {{
            submissionsV2(request: {{ productAdditionRequestId: "{request_id}" }}) {{
              productAdditionStatus {{
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

            status = statuses[0]
            submission_status = status.get("submissionStatus")
            validation_status = status.get("validationStatus")

            LOGGER.info(
                "Submission %s: submissionStatus=%s validationStatus=%s",
                request_id,
                submission_status,
                validation_status,
            )

            # Both submissionStatus=PROCESSING and validationStatus=PENDING are
            # intermediate states; keep polling until both resolve.
            if submission_status == "PROCESSING" or validation_status == "PENDING":
                time.sleep(POLL_SLEEP_SECONDS)
                continue

            # Batch has finished processing.
            flaws = status.get("validationFlaws", [])
            errors = [f for f in flaws if f.get("flawType") == "ERROR"]
            warnings = [f for f in flaws if f.get("flawType") == "WARNING"]

            if warnings:
                LOGGER.warning(
                    "Wayfair validation warnings for %s: %s",
                    request_id,
                    "; ".join(self.format_validation_flaw(w) for w in warnings),
                )

            if validation_status == "FAILED":
                attribute_errors: dict[str, list[str]] = {}
                for flaw in errors:
                    attr_id = str(flaw.get("attributeId") or "_general")
                    attribute_errors.setdefault(attr_id, []).append(
                        self.format_validation_flaw(flaw)
                    )
                if not attribute_errors:
                    attribute_errors["_general"] = [
                        "Submission failed with no specific ERROR flaws listed"
                    ]
                raise structured_export_error(attribute_errors)

            # VALIDATED (or any non-FAILED terminal status).
            return status

        raise RetriableAPIError(
            f"Wayfair submission {request_id} stayed PROCESSING after "
            f"{POLL_MAX_ATTEMPTS} attempts ({POLL_MAX_ATTEMPTS * POLL_SLEEP_SECONDS}s)",
        )

    @staticmethod
    def _attr_to_gql(attr: dict) -> str:
        """Serialize a single attribute entry to an inline GraphQL object literal.

        Uses json.dumps() for string values to handle escaping correctly.
        """
        parts = [
            f"attributeId: {json.dumps(str(attr['attributeId']))}",
            f"value: {json.dumps(str(attr['value']))}",
            f"parentRank: {int(attr['parentRank'])}",
            f"rank: {int(attr['rank'])}",
        ]
        if attr.get("attributeInstance") is not None:
            parts.append(f"attributeInstance: {int(attr['attributeInstance'])}")
        return "{ " + ", ".join(parts) + " }"

    def _build_submit_mutation(
        self,
        product_id: str,
        class_id: str,
        attributes: list,
        market_context: dict,
        job_context: dict,
    ) -> str:
        attrs_gql = "\n              ".join(
            self._attr_to_gql(a) for a in attributes
        )
        locale = json.dumps(market_context.get("locale", "en-US"))
        country = market_context.get("country", "UNITED_STATES")
        brand = market_context.get("brand", "WAYFAIR")
        request_id = (
            json.dumps(job_context["productAdditionRequestId"])
            if job_context.get("productAdditionRequestId")
            else "null"
        )
        has_more = "true" if job_context.get("hasMoreProducts") else "false"
        product_id_gql = json.dumps(str(product_id))
        class_id_gql = json.dumps(str(class_id))

        return f"""
mutation {{
  productAddition {{
    submitV2(request: {{
      marketContext: {{
        locale: {locale}
        country: {country}
        brand: {brand}
      }}
      jobContext: {{
        productAdditionRequestId: {request_id}
        hasMoreProducts: {has_more}
      }}
      proposedProductAdditions: [{{
        productId: {product_id_gql}
        classId: {class_id_gql}
        attributes: [
              {attrs_gql}
        ]
      }}]
    }}) {{
      batchId
      status
      productAdditionRequestId
    }}
  }}
}}
"""

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

    def upsert_record(self, record: dict, context: dict):
        """Submit one product and wait for validation to complete."""
        record_product_id = record["productId"]
        wayfair_product_id = self.resolve_submission_product_id(record)
        class_id = record["classId"]
        attributes = record["attributes"]

        if wayfair_product_id != record_product_id:
            LOGGER.info(
                "Using core::supplierPartNumber %s as Wayfair productId "
                "(record productId is %s)",
                wayfair_product_id,
                record_product_id,
            )

        market_context = self.resolve_market_context(record)
        job_context = record.get("jobContext") or {
            "productAdditionRequestId": None,
            "hasMoreProducts": False,
        }

        mutation = self._build_submit_mutation(
            wayfair_product_id, class_id, attributes, market_context, job_context
        )
        body = self._graphql(mutation)
        submit_result = (
            (body.get("data") or {}).get("productAddition") or {}
        ).get("submitV2") or {}
        request_id = submit_result.get("productAdditionRequestId")
        if not request_id:
            raise FatalAPIError(
                f"Wayfair submitV2 returned no productAdditionRequestId for product "
                f"{record_product_id} (Wayfair productId {wayfair_product_id}). "
                f"Response: {submit_result}"
            )
        LOGGER.info(
            "Submitted product %s (Wayfair productId %s) → batchId=%s requestId=%s",
            record_product_id,
            wayfair_product_id,
            submit_result.get("batchId"),
            request_id,
        )

        status = self.poll_submission_status(request_id)
        LOGGER.info(
            "Product %s (Wayfair productId %s) validated: validationStatus=%s",
            record_product_id,
            wayfair_product_id,
            status.get("validationStatus"),
        )
        return record_product_id, True, {}
