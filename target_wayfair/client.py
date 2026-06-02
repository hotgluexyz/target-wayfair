import json
import time
from typing import Dict, List, Optional

import requests
import singer
from hotglue_etl_exceptions import InvalidCredentialsError, InvalidPayloadError
from hotglue_singer_sdk.exceptions import FatalAPIError, RetriableAPIError
from hotglue_singer_sdk.plugin_base import PluginBase
from hotglue_singer_sdk.target_sdk.client import HotglueSink

from target_wayfair.auth import WayfairAuth

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
            return
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
            statuses = (
                body.get("data", {})
                .get("productAddition", {})
                .get("submissionsV2", {})
                .get("productAdditionStatus", [])
            )
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
                    "; ".join(f"{w['attributeId']}: {w['flaw']}" for w in warnings),
                )

            if validation_status == "FAILED":
                error_msg = "; ".join(f"{e['attributeId']}: {e['flaw']}" for e in errors) if errors else (
                    "Submission failed with no specific ERROR flaws listed"
                )
                raise InvalidPayloadError(
                    f"Wayfair product validation failed: {error_msg}"
                )

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
            f'"{job_context["productAdditionRequestId"]}"'
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

    def upsert_record(self, record: dict, context: dict):
        """Submit one product and wait for validation to complete."""
        product_id = record["productId"]
        class_id = record["classId"]
        attributes = record["attributes"]

        market_context = record.get("marketContext") or {
            "locale": self.config.get("locale", "en-US"),
            "country": self.config.get("country", "UNITED_STATES"),
            "brand": self.config.get("brand", "WAYFAIR"),
        }
        job_context = record.get("jobContext") or {
            "productAdditionRequestId": None,
            "hasMoreProducts": False,
        }

        mutation = self._build_submit_mutation(
            product_id, class_id, attributes, market_context, job_context
        )
        body = self._graphql(mutation)
        submit_result = (
            body.get("data", {})
            .get("productAddition", {})
            .get("submitV2", {})
        )
        request_id = submit_result.get("productAdditionRequestId")
        if not request_id:
            raise FatalAPIError(
                f"Wayfair submitV2 returned no productAdditionRequestId for product {product_id}. "
                f"Response: {submit_result}"
            )
        LOGGER.info(
            "Submitted product %s → batchId=%s requestId=%s",
            product_id,
            submit_result.get("batchId"),
            request_id,
        )

        status = self.poll_submission_status(request_id)
        LOGGER.info(
            "Product %s validated: validationStatus=%s",
            product_id,
            status.get("validationStatus"),
        )
        return product_id, True, {}
