"""Wayfair OAuth for the target (portal refresh and client credentials)."""

import base64
import json
import time
from typing import Optional

import requests
from hotglue_etl_exceptions import InvalidCredentialsError
from hotglue_singer_sdk.target_sdk.auth import OAuthAuthenticator

CLIENT_CREDENTIALS_TOKEN_URL = "https://sso.auth.wayfair.com/oauth/token"

PORTAL_TOKEN_URL = (
    "https://partners.wayfair.com/auth/realms/Developer_Portal/"
    "protocol/openid-connect/token"
)

# Hotglue connect often stores OAuth ``expires_in`` as TTL seconds; after refresh we store Unix time.
EXPIRES_IN_ABSOLUTE_THRESHOLD = 1_000_000_000

TOKEN_REFRESH_BUFFER_SECONDS = 120


def uses_portal_oauth(config: dict) -> bool:
    """True when hotglue linked the connection via Developer Portal (has refresh_token)."""
    return bool(config.get("refresh_token"))


def token_url_for_config(config: dict) -> str:
    """Token endpoint for this config: portal OpenID URL vs supplier client-credentials URL."""
    if uses_portal_oauth(config):
        return PORTAL_TOKEN_URL
    return CLIENT_CREDENTIALS_TOKEN_URL


def _utc_now_ts() -> int:
    """Current time as Unix seconds (UTC)."""
    return round(time.time())


class WayfairAuth(OAuthAuthenticator):
    """Obtain and refresh Bearer tokens for Wayfair API calls.

    Portal-linked connections (``refresh_token`` in config) use form-encoded refresh
    against the Developer Portal token URL. Manual supplier credentials use JSON
    client_credentials against ``sso.auth.wayfair.com``. Jobs may refresh via the
    Hotglue accesstoken API when ``TargetWayfair.access_token_support`` is used.
    """

    def __init__(self, target, state=None, auth_endpoint=None):
        """Bind to target config; token URL is always derived from config shape."""
        if state is None:
            state = {}
        # ``auth_endpoint`` from ``access_token_support`` is a placeholder only.
        endpoint = token_url_for_config(target._config)
        super().__init__(target, state=state, auth_endpoint=endpoint)
        self._auth_endpoint = endpoint

    @property
    def oauth_request_body(self) -> dict:
        """Body for local token refresh (grant type depends on portal vs M2M config)."""
        if uses_portal_oauth(self._config):
            return {
                "grant_type": "refresh_token",
                "client_id": self._config["client_id"],
                "client_secret": self._config["client_secret"],
                "refresh_token": self._config["refresh_token"],
            }
        return {
            "grant_type": "client_credentials",
            "client_id": self._config["client_id"],
            "client_secret": self._config["client_secret"],
        }

    def is_token_valid(self) -> bool:
        """True if ``access_token`` exists and is not within the refresh buffer of expiry."""
        access_token = self._config.get("access_token")
        if not access_token:
            return False

        expires_at = self._access_token_expires_at()
        if expires_at is None:
            return False

        return (expires_at - _utc_now_ts()) >= TOKEN_REFRESH_BUFFER_SECONDS

    def _access_token_expires_at(self) -> Optional[int]:
        """Resolve access token expiry as Unix seconds.

        Prefer JWT ``exp`` for portal tokens. Otherwise interpret ``expires_in`` as
        absolute expiry if large, or as remaining TTL seconds (hotglue connect shape).
        """
        token = self._config.get("access_token")
        if isinstance(token, str) and token.count(".") == 2:
            try:
                segment = token.split(".")[1]
                segment += "=" * (-len(segment) % 4)
                payload = json.loads(base64.urlsafe_b64decode(segment))
                exp = payload.get("exp")
                if exp is not None:
                    return int(exp)
            except (ValueError, json.JSONDecodeError, TypeError):
                pass

        expires_in = self._config.get("expires_in")
        if expires_in is None:
            return None

        expires_in = int(expires_in)
        now = _utc_now_ts()
        if expires_in >= EXPIRES_IN_ABSOLUTE_THRESHOLD:
            return expires_in
        return now + expires_in

    def _update_access_token_locally(self) -> None:
        """POST to Wayfair token URL; portal uses form body, M2M uses JSON."""
        body = self.oauth_request_body
        if uses_portal_oauth(self._config):
            token_response = requests.post(
                self._auth_endpoint,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=300,
            )
        else:
            token_response = requests.post(
                self._auth_endpoint,
                json=body,
                timeout=300,
            )
        try:
            token_response.raise_for_status()
        except requests.HTTPError as ex:
            raise InvalidCredentialsError(
                f"Wayfair OAuth failed ({token_response.status_code}): "
                f"{token_response.text}. {ex}"
            )

        token_json = token_response.json()
        now = _utc_now_ts()
        self._config["access_token"] = token_json["access_token"]
        self._config["expires_in"] = int(token_json.get("expires_in", 3600)) + now

        # Keycloak may omit a new refresh_token; keep the existing one.
        new_refresh = token_json.get("refresh_token")
        if new_refresh:
            self._config["refresh_token"] = new_refresh

        if self._config_file_path:
            with open(self._config_file_path, "w") as outfile:
                json.dump(self._config, outfile, indent=4)
