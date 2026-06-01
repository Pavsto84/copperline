# HTTP API

Start:

```bash
copperline --db company.sqlite serve --host 127.0.0.1 --port 8790
```

Create an API key with the CLI first.

Authenticated requests use:

```http
Authorization: Bearer cl_key_...
```

## Public

### `GET /healthz`

Database integrity, schema versions, counts, and company settings.

## Accounts and ledger

- `GET /v1/company`
- `GET /v1/accounts`
- `POST /v1/accounts`
- `GET /v1/accounts/{id}/balance?as_of=YYYY-MM-DD`
- `GET /v1/journal`
- `POST /v1/journal`
- `POST /v1/journal/{id}/reverse`

## Parties/invoices/payments

- `GET /v1/parties`
- `POST /v1/parties`
- `GET /v1/invoices`
- `POST /v1/invoices`
- `GET /v1/invoices/{id}`
- `POST /v1/invoices/{id}/issue`
- `POST /v1/payments`

## Inventory

- `GET /v1/items`
- `POST /v1/items`
- `GET /v1/warehouses`
- `POST /v1/warehouses`
- `POST /v1/inventory/receive`
- `POST /v1/inventory/issue`

## Banking

- `GET /v1/bank/accounts`
- `GET /v1/bank/accounts/{id}/transactions`
- `POST /v1/bank/transactions/{id}/match`

## Reports

- `GET /v1/reports/trial-balance?as_of=...`
- `GET /v1/reports/profit-loss?start=...&end=...`
- `GET /v1/reports/balance-sheet?as_of=...`
- `GET /v1/reports/aging?kind=receivable&as_of=...`
- `GET /v1/reports/inventory`
- `GET /v1/audit`

## Errors

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "description is required",
    "details": {}
  }
}
```

HTTP mapping:

- authentication -> 401
- authorization -> 403
- not found -> 404
- conflict -> 409
- validation -> 400
- other domain error -> 422

## Smoke test

Before exposing the API, test the health check, unauthorized requests, scoped API keys, account and invoice writes, report reads, and representative validation failures through real HTTP requests.
