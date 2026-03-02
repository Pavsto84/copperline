# Copperline

Copperline is a self-hosted accounting platform for small businesses, internal finance teams, and developers who need a trustworthy local accounting core without operating a separate database server.

It is written in Python and uses SQLite as its default persistence layer. The repository is a real application rather than a tutorial ledger: it includes double-entry journals, period locks, customers/vendors, sales and purchase invoices, tax calculation, payments and allocations, inventory costing, bank statement imports, bank-to-ledger reconciliation, foreign exchange quotes, budgets, financial reports, API keys, a JSON HTTP API, CSV/OFX import, portable exports, consistent backups, audit records, and a CLI.

Copperline deliberately separates **accounting truth** from presentation. Every business document that affects the books posts a balanced journal entry, and all financial reports are derived from posted journal lines rather than duplicate summary tables.

## Features

### General ledger

- hierarchical chart of accounts
- asset, liability, equity, revenue, and expense accounts
- debit/credit validation
- immutable posted journals
- draft/posted/void lifecycle
- reversals instead of destructive edits
- source/reference metadata
- dimensions on journal lines
- accounting period definitions and locks
- idempotent business operations
- trial balance and account balances
- append-only operational audit records

### Accounts receivable / payable

- customer, vendor, and dual-role parties
- configurable control accounts
- sales invoices and purchase bills
- automatic due dates from payment terms
- invoice discounts
- tax-exclusive and tax-inclusive lines
- recoverable input-tax percentages
- payments and receipts
- explicit allocations
- automatic oldest-due allocation
- partial-payment status
- credit notes
- receivable/payable aging

### Inventory

- service, non-inventory, and inventory items
- multiple warehouses
- FIFO costing
- moving-average costing
- receipts
- issues
- warehouse transfers
- quantity adjustments
- cost layers and layer consumption history
- inventory asset / COGS posting
- as-of quantity
- inventory valuation report

### Banking

- bank account registry
- normalized bank transactions
- duplicate-safe imports
- configurable CSV import
- OFX/QFX import
- bank-to-ledger candidate scoring
- amount/date/text matching
- explicit and automatic matching
- ignore/unmatch workflow
- statement reconciliation sessions

### Finance and planning

- foreign-exchange quote history
- inverse and cross rates
- weighted average FX rates
- monthly budgets
- annual budget spreading with weights
- dimension-specific budget lines
- budget vs actual variance
- profit & loss
- balance sheet
- indirect cash-flow summary
- tax summary
- inventory valuation
- aging reports

### Operations

- scoped API keys using scrypt hashes
- dependency-free threaded HTTP JSON API
- CLI for bookkeeping and administration
- JSON and CSV exports
- canonical snapshot export
- SQLite online backup
- compressed archive backups with SHA-256 manifests
- archive verification
- database integrity checks
- audit export
- health endpoint
- Docker and Compose examples
- Python 3.11–3.13 CI matrix

## Why SQLite?

Copperline targets a deployment class that is often underserved: one business or one internal team running accounting on a single server, desktop, VM, or small container host.

SQLite gives Copperline:

- ACID transactions
- foreign keys
- WAL mode
- crash recovery
- online backup
- no separate database daemon
- a database file that is easy to move and inspect

Copperline enables WAL mode for file databases and uses explicit transactions for accounting operations. It is **not** presented as a globally distributed accounting database. Do not place one SQLite file on an unreliable network filesystem and let multiple hosts write it concurrently.

## Requirements

- Python 3.11 or newer
- no mandatory third-party runtime dependencies

Optional development tooling is pinned in `pyproject.toml`.

## Install

Editable development installation:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Or run directly from a checkout:

```bash
PYTHONPATH=src python -m copperline --help
```

## Quick start

Create a company:

```bash
copperline --db demo.sqlite init \
  --name "Acme Workshop" \
  --currency USD
```

This creates a practical default chart:

```text
1000 Cash
1010 Bank
1100 Accounts Receivable
1200 Inventory
1300 Recoverable Tax
2000 Accounts Payable
2100 Sales Tax Payable
3000 Owner Equity
3100 Retained Earnings
4000 Sales Revenue
4900 Other Revenue
4950 Foreign Exchange Gain
5000 Cost of Goods Sold
6000 Operating Expense
6900 Foreign Exchange Loss
```

Create a customer:

```bash
copperline --db demo.sqlite party create \
  --name "Northwind Cafe" \
  --kind customer
```

Create a 10% sales tax:

```bash
copperline --db demo.sqlite tax create \
  --code TAX10 \
  --name "Sales tax 10%" \
  --rate 10 \
  --account 2100
```

Create and issue an invoice:

```bash
copperline --db demo.sqlite invoice create \
  --party pty_... \
  --date 2026-09-05 \
  --line "Consulting:2:150.00:4000:TAX10" \
  --issue
```

Record payment:

```bash
copperline --db demo.sqlite payment record \
  --party pty_... \
  --date 2026-09-10 \
  --amount 330.00 \
  --account 1010 \
  --kind receipt \
  --allocate inv_...:330.00
```

Run a profit & loss:

```bash
copperline --db demo.sqlite report profit-loss \
  --start 2026-09-01 \
  --end 2026-09-30
```

## Inventory example

Create a warehouse:

```bash
copperline --db demo.sqlite warehouse create \
  --code MAIN \
  --name "Main Warehouse"
```

Create an item:

```bash
copperline --db demo.sqlite item create \
  --sku FILTER-01 \
  --name "Water Filter" \
  --kind inventory \
  --income-account 4000 \
  --expense-account 5000 \
  --inventory-account 1200 \
  --price 39.00 \
  --cost-method fifo
```

Receive stock:

```bash
copperline --db demo.sqlite stock receive \
  --item FILTER-01 \
  --warehouse MAIN \
  --date 2026-09-01 \
  --quantity 20 \
  --unit-cost 12.50
```

Issue stock:

```bash
copperline --db demo.sqlite stock issue \
  --item FILTER-01 \
  --warehouse MAIN \
  --date 2026-09-05 \
  --quantity 3
```

The receipt increases Inventory and credits the configured clearing/payables account. The issue debits COGS and credits Inventory using the item's costing method.

## Bank import and reconciliation

Copperline supports OFX/QFX and mapped CSV.

OFX:

```bash
copperline --db demo.sqlite bank import \
  --bank-account bank_... \
  --file statement.ofx \
  --format ofx
```

CSV:

```bash
copperline --db demo.sqlite bank import \
  --bank-account bank_... \
  --file statement.csv \
  --format csv \
  --date-column Date \
  --description-column Description \
  --amount-column Amount \
  --external-id-column TransactionID
```

See candidate matches:

```bash
copperline --db demo.sqlite bank candidates btx_...
```

Confirm one:

```bash
copperline --db demo.sqlite bank match btx_... --entry je_...
```

Or automatically accept only high-confidence unambiguous candidates:

```bash
copperline --db demo.sqlite bank auto-match \
  --bank-account bank_... \
  --threshold 0.92
```

## Accounting periods

Define:

```bash
copperline --db demo.sqlite period define \
  --start 2026-09-01 \
  --end 2026-09-30
```

Lock:

```bash
copperline --db demo.sqlite period lock --start 2026-09-01
```

A locked period rejects new posting. Unlocking requires an actor and reason and records an audit event:

```bash
copperline --db demo.sqlite period unlock \
  --start 2026-09-01 \
  --actor finance-admin \
  --reason "Auditor-approved correction"
```

## HTTP API

Create an API key:

```bash
copperline --db demo.sqlite key create \
  --label local-admin \
  --scope "*"
```

Start the server:

```bash
copperline --db demo.sqlite serve --host 127.0.0.1 --port 8790
```

Use the returned token:

```bash
curl -H "Authorization: Bearer cl_key_..." \
  http://127.0.0.1:8790/v1/accounts
```

The API is dependency-free and implemented with Python's standard `http.server`. It is suitable behind a TLS reverse proxy for small self-hosted installations.

See [docs/api.md](docs/api.md).

## Backups

Online SQLite copy:

```bash
copperline --db demo.sqlite backup --output backup.sqlite
```

Compressed archive with manifest:

```bash
copperline --db demo.sqlite backup \
  --output copperline-2026-09-05.tar.gz \
  --archive
```

The archive includes an online SQLite backup and a SHA-256 manifest. `BackupService.verify_archive()` validates archive hashes before restore.

Back up encryption/TLS/reverse-proxy configuration separately; Copperline's database backup contains application data, not external infrastructure secrets.

## Exports

```bash
copperline --db demo.sqlite export journal --output journal.csv
copperline --db demo.sqlite export invoices --output invoices.csv
copperline --db demo.sqlite export inventory --output inventory.csv
copperline --db demo.sqlite export audit --output audit.jsonl
copperline --db demo.sqlite export snapshot --output snapshot.json
```

Snapshot export is a portable inspection format, not a transactionally replayable import format.

## Architecture

The package intentionally uses services rather than a monolithic ORM model.

```text
                         CLI / HTTP API
                              |
                        Copperline facade
                              |
      +-----------------------+----------------------+
      |                       |                      |
  Ledger/Periods        Business documents       Reports
      |             invoices/payments/inventory     |
      |                       |                      |
      +-----------------------+----------------------+
                              |
                         SQLite Database
```

Key rules:

1. posted journals are the accounting source of truth;
2. business documents reference their posted journal entries;
3. accounting-period locks are checked at journal posting;
4. financial reports query posted lines;
5. inventory value changes create balanced accounting entries;
6. payment allocations do not rewrite invoice totals;
7. bank matching links statement data to already-posted accounting entries.

See [docs/architecture.md](docs/architecture.md) and [docs/accounting-model.md](docs/accounting-model.md).

## Testing

The project uses standard-library `unittest`, so the runtime test suite needs no third-party package.

```bash
PYTHONPATH=src:. python -m unittest discover -s tests -t . -v
```

The suite covers unit math, journals, locks, taxes, invoices, payments, inventory costing, imports, reconciliation, budgets, reports, API keys, backups, HTTP requests, CLI subprocesses, and restart persistence.

## Release status

`0.1.0` is an alpha-quality release baseline. It is useful for real bookkeeping experiments and internal deployments, but any organization with statutory reporting requirements should review local tax/accounting compliance before relying on it for filings.

Areas intentionally left for future versions include payroll, fixed assets/depreciation, purchase orders, sales orders, multi-entity consolidation, and a network-database adapter.

## Security

See [SECURITY.md](SECURITY.md).

## License

MIT.
