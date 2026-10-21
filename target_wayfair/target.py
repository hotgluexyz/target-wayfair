"""Wayfair target class."""

from hotglue_singer_sdk import typing as th
from hotglue_singer_sdk.helpers.capabilities import AlertingLevel
from hotglue_singer_sdk.target_sdk.target import TargetHotglue

from target_wayfair.auth import CLIENT_CREDENTIALS_TOKEN_URL, WayfairAuth
from target_wayfair.sinks import ProductsSink


class TargetWayfair(TargetHotglue):
    """Singer target for the Wayfair Product Catalog API."""

    SINK_TYPES = [
        ProductsSink,
    ]
    name = "target-wayfair"
    alerting_level = AlertingLevel.ERROR

    @classmethod
    def access_token_support(cls, connector=None):
        """Declare token refresh for Hotglue ``--access-token`` and job accesstoken API.

        The returned URL is a placeholder for the SDK; ``WayfairAuth`` picks the real
        endpoint from config (portal refresh vs client credentials).
        """
        return WayfairAuth, CLIENT_CREDENTIALS_TOKEN_URL

    def __init__(
        self,
        config=None,
        parse_env_config: bool = False,
        validate_config: bool = True,
        state: str = None,
    ) -> None:
        self.config_file = config[0]
        super().__init__(
            config=config,
            parse_env_config=parse_env_config,
            validate_config=validate_config,
        )

    config_jsonschema = th.PropertiesList(
        th.Property("client_id", th.StringType, required=True),
        th.Property("client_secret", th.StringType, required=True),
        th.Property("supplier_id", th.StringType, required=True),
        # Optional market defaults (used when not present in the record itself)
        th.Property("locale", th.StringType, required=False),
        th.Property("country", th.StringType, required=False),
        th.Property("brand", th.StringType, required=False),
        th.Property("is_sandbox", th.BooleanType, required=False),
    ).to_dict()


if __name__ == "__main__":
    TargetWayfair.cli()
