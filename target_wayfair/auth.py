import json
from datetime import datetime

import requests
from hotglue_etl_exceptions import InvalidCredentialsError
from hotglue_singer_sdk.target_sdk.auth import OAuthAuthenticator

TOKEN_URL = "https://sso.auth.wayfair.com/oauth/token"


class WayfairAuth(OAuthAuthenticator):
    """OAuth2 client-credentials auth for the Wayfair API."""

    def __init__(self, target):
        super().__init__(target, auth_endpoint=TOKEN_URL)

    @property
    def oauth_request_body(self) -> dict:
        return {
            "grant_type": "client_credentials",
            "client_id": self._config["client_id"],
            "client_secret": self._config["client_secret"],
        }

    def _update_access_token_locally(self) -> None:
        """Fetch a token using JSON body (Wayfair does not accept form-encoded)."""
        token_response = requests.post(
            self._auth_endpoint,
            json=self.oauth_request_body,
        )
        try:
            token_response.raise_for_status()
        except Exception as ex:
            raise InvalidCredentialsError(
                f"Wayfair OAuth failed ({token_response.status_code}): "
                f"{token_response.text}. {ex}"
            )
        token_json = token_response.json()
        now = round(datetime.utcnow().timestamp())
        self._config["access_token"] = token_json["access_token"]
        self._config["expires_in"] = int(token_json.get("expires_in", 86400)) + now

        with open(self._config_file_path, "w") as outfile:
            json.dump(self._config, outfile, indent=4)
