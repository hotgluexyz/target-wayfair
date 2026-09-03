"""Structured export errors for Hotglue export details."""

from __future__ import annotations

import json
from typing import Any

from hotglue_etl_exceptions import InvalidPayloadError

STRUCTURED_EXPORT_ERROR_PREFIX = "Wayfair export errors: "


def structured_export_error(errors: dict[str, list[str]]) -> InvalidPayloadError:
    """Raise an export failure with a JSON payload Plytix can parse reliably."""
    return InvalidPayloadError(STRUCTURED_EXPORT_ERROR_PREFIX + json.dumps(errors))


def general_export_error(message: str) -> InvalidPayloadError:
    """Raise a non-attribute export failure."""
    return structured_export_error({"_general": [message]})
