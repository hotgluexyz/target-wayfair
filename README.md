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

Example `config.json`:

```json
{
  "client_id": "your-client-id",
  "client_secret": "your-client-secret",
  "supplier_id": "12345"
}
```

---

## Source Authentication and Authorization

The target uses OAuth2 client credentials. Wayfair issues a `client_id` and `client_secret` through their supplier portal. The target exchanges them for a Bearer token at `https://sso.auth.wayfair.com/oauth/token` and refreshes automatically when the 24-hour token expires.

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
