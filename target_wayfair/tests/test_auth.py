"""Tests for Wayfair OAuth and catalog URL resolution."""

import base64
import json
import time
from unittest.mock import MagicMock

from target_wayfair.auth import (
    CLIENT_CREDENTIALS_TOKEN_URL,
    EXPIRES_IN_ABSOLUTE_THRESHOLD,
    PORTAL_TOKEN_URL,
    WayfairAuth,
    token_url_for_config,
    uses_portal_oauth,
)
from target_wayfair.client import (
    GRAPHQL_URL,
    SANDBOX_CATALOG_API_BASE,
    catalog_graphql_url,
)


def test_uses_portal_oauth_when_refresh_token_present():
    assert uses_portal_oauth({"refresh_token": "rt"})
    assert not uses_portal_oauth({"client_id": "a", "client_secret": "b"})


def test_token_url_for_config():
    assert token_url_for_config({"refresh_token": "x"}) == PORTAL_TOKEN_URL
    assert (
        token_url_for_config({"client_id": "a", "client_secret": "b"})
        == CLIENT_CREDENTIALS_TOKEN_URL
    )


def test_oauth_request_body_portal_vs_client_credentials():
    target = MagicMock()
    target._config = {
        "client_id": "cid",
        "client_secret": "sec",
        "refresh_token": "rt",
    }
    auth = WayfairAuth(target)
    assert auth.oauth_request_body["grant_type"] == "refresh_token"
    assert auth.oauth_request_body["refresh_token"] == "rt"

    target._config = {"client_id": "cid", "client_secret": "sec"}
    auth = WayfairAuth(target)
    assert auth.oauth_request_body["grant_type"] == "client_credentials"


def test_is_token_valid_uses_jwt_exp():
    now = int(time.time())
    payload = json.dumps({"exp": now + 3600}).encode()
    segment = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    token = f"hdr.{segment}.sig"
    target = MagicMock()
    target._config = {"access_token": token, "expires_in": 3600}
    auth = WayfairAuth(target)
    assert auth.is_token_valid()


def test_ttl_expires_in_normalized_to_absolute():
    target = MagicMock()
    target._config = {
        "access_token": "not-a-jwt",
        "expires_in": 3600,
    }
    auth = WayfairAuth(target)
    assert auth.is_token_valid()
    exp1 = auth._access_token_expires_at()
    exp2 = auth._access_token_expires_at()
    assert exp1 == exp2
    assert target._config["expires_in"] >= EXPIRES_IN_ABSOLUTE_THRESHOLD


def test_catalog_graphql_url_sandbox_and_production():
    assert catalog_graphql_url({"is_sandbox": True}).startswith(SANDBOX_CATALOG_API_BASE)
    assert catalog_graphql_url({}) == GRAPHQL_URL
    assert catalog_graphql_url({"is_sandbox": False}) == GRAPHQL_URL
