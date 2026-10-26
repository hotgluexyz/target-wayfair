# target-wayfair

`target-wayfair` is a Singer target for [Wayfair](https://www.wayfair.com/), writing product listings to the Wayfair Product Catalog API (GraphQL). Built with the [Hotglue Singer SDK](https://github.com/hotgluexyz/HotglueSingerSDK) for Singer Targets.

---

## Installation

```bash
pip install target-wayfair
```

Or from source:

```bash
pip install git+https://github.com/hotgluexyz/target-wayfair.git
```

---

## Configuration

| Field | Required | Description |
|---|---|---|
| `client_id` | Yes | OAuth2 client ID issued by Wayfair |
| `client_secret` | Yes | OAuth2 client secret issued by Wayfair |
| `supplier_id` | Yes | Wayfair supplier ID (sent as `X-SELECTED-SUPPLIER-ID` header) |
| `locale` | No | Market locale, default `en-US` |
| `country` | No | Market country enum, default `UNITED_STATES` |
| `brand` | No | Brand enum, default `WAYFAIR` |
| `is_sandbox` | No | When `true`, use the sandbox Product Catalog API (`api.wayfair.io/sandbox`). Set this for sandbox supplier credentials. |

hotglue may also pass `access_token`, `refresh_token`, and `expires_in` after an OAuth link in the UI. You do not need to set those manually.

**Client credentials** (manual or internal sandbox app):

```json
{
  "client_id": "your-client-id",
  "client_secret": "your-client-secret",
  "supplier_id": "12345",
  "is_sandbox": true
}
```

**Developer Portal OAuth** (hotglue connect; includes `refresh_token`):

```json
{
  "client_id": "vendor-…",
  "client_secret": "…",
  "supplier_id": "12345",
  "is_sandbox": true,
  "access_token": "…",
  "refresh_token": "…",
  "expires_in": 3600
}
```

---

## Authentication

Wayfair supports two OAuth 2.0 flows. The target picks the flow from config:

| Config | Flow | Token endpoint |
|---|---|---|
| No `refresh_token` | [Client credentials](https://developer.wayfair.io/posts/api-access-tokens) | `https://sso.auth.wayfair.com/oauth/token` (JSON body) |
| `refresh_token` present | Developer Portal authorization code (hotglue UI) | `https://partners.wayfair.com/auth/realms/Developer_Portal/protocol/openid-connect/token` (form body, refresh grant) |

Tokens are refreshed automatically when they are close to expiry. With `TargetWayfair.access_token_support`, jobs can also refresh via the hotglue accesstoken API before calling Wayfair locally.

Catalog requests use production `https://api.wayfair.io/v1/product-catalog-api/graphql` unless `is_sandbox` is `true`.

---

## Supported Streams

| Stream | Description |
|---|---|
| `Products` | Creates product listings in the Wayfair Product Catalog via `productAddition.submitV2` |

### Record format

Records are passed through directly to the Wayfair API without a fixed field mapping, keeping the target flexible for any product category.

```json
{
  "productId": "YOUR-SKU-001",
  "classId": "12",
  "attributes": [
    {"attributeId": "core::supplierPartNumber", "value": "YOUR-SKU-001", "parentRank": 1, "rank": 1},
    {"attributeId": "core::productName", "value": "My Bed", "parentRank": 1, "rank": 1},
    {"attributeId": "price::wholesalePrice", "value": "199.99", "parentRank": 1, "rank": 1},
    {"attributeId": "shippingAndFulfillment::depth", "value": "12.0", "parentRank": 1, "rank": 1, "attributeInstance": 1}
  ],
  "marketContext": {"locale": "en-US", "country": "UNITED_STATES", "brand": "WAYFAIR"},
  "jobContext": {"productAdditionRequestId": null, "hasMoreProducts": false}
}
```

`marketContext` and `jobContext` are optional; they default to the config values (or `en-US / UNITED_STATES / WAYFAIR`) when omitted. Set `hasMoreProducts: true` and reuse `productAdditionRequestId` to send a large catalog in chunks.

`productId` is the Singer/hotglue record key (e.g. your Plytix SKU). Wayfair's `submitV2` mutation also sends a top-level `productId`, which must match the catalog's supplier part number. When `core::supplierPartNumber` is present in `attributes`, the target uses that value as the Wayfair `productId`. Changing only the attribute while leaving the record `productId` as an older SKU will not bypass Wayfair duplicate-part-number validation.

Attribute `1` is Wayfair's class name (e.g. `"Beds"` for `classId` `"12"`). The target always sets it from the class taxonomy based on `classId`, overwriting any value sent in `attributes` — you only need to send `classId` on the root record.

`productAddition.submitV2` creates **new** catalog items only. If a supplier part number already exists in your Wayfair catalog (including from earlier test exports), you must use the catalog update APIs instead of re-submitting it as a new product.

After each submission the target polls `productAddition.submissionsV2` until `submissionStatus` leaves `PROCESSING`. If `validationStatus` is `FAILED`, an `InvalidPayloadError` is raised with the Wayfair flaw messages. Warnings are logged but do not block processing.

---

## Usage

```bash
# Pipe a tap's output directly into the target
tap-mydata --config tap_config.json | target-wayfair --config config.json

# Run a local sample file
cat sample_payload/valid_product.singer | target-wayfair --config .secrets/config.json
```

---

## Developer Resources

```bash
# Create virtual environment and install in editable mode
python -m venv .venv
.venv/bin/pip install -e . ruff pytest

# Verify the CLI
.venv/bin/target-wayfair --version
.venv/bin/target-wayfair --about

# Lint
.venv/bin/ruff check .

# Run tests (requires .secrets/config.json for live tests)
.venv/bin/pytest target_wayfair/tests/
```
