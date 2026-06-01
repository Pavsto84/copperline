# Architecture

Copperline is built from domain services that share one transactional SQLite database.

## Layers

### Primitives

`money.py`, `timeutil.py`, `ids.py`, and `jsonutil.py` handle deterministic financial primitives.

Money is always `(Decimal amount, currency)`. Currency precision is applied at object construction so journal lines and persisted minor units agree.

### Ledger

`accounts.py` and `ledger.py` own:

- account validation
- accounting periods
- balanced journals
- posting
- reversal
- account balances
- idempotency

No business service is allowed to directly update account balances.

### Business subledgers

- `invoices.py`
- `payments.py`
- `inventory.py`
- `banking.py`
- `reconcile.py`
- `budget.py`
- `fx.py`

Subledgers keep operational detail. When a change affects accounting value, they post through `LedgerService`.

### Reports

`reports.py` derives financial statements from posted journal lines. It does not trust invoice or inventory summary fields as replacements for the ledger.

### Interfaces

- `cli.py`
- `http/server.py`
- `service.py`

The `Copperline` facade wires these services for embedded Python use.

## Transaction boundaries

A business operation typically runs in one `BEGIN IMMEDIATE` transaction. Nested service calls use SQLite savepoints.

Examples:

- issuing an invoice posts the journal and updates invoice status atomically;
- recording a payment posts cash/control entries and allocations atomically;
- an inventory issue consumes cost layers, records the movement, and posts COGS atomically.

Long-running I/O such as HTTP requests does not occur inside accounting transactions.

## Persistence

SQLite tables retain operational documents and the ledger.

Amounts are stored as integer minor units in all accounting tables. Quantities and rates use decimal strings so binary floating point never becomes a persisted accounting value.

## Extensibility

The current code intentionally keeps SQL in service/repository methods instead of a third-party ORM. This makes transaction behavior explicit and keeps runtime dependencies empty.

A future PostgreSQL adapter would require a persistence abstraction before it could replace SQLite; the current `Database` class is not pretending to be that abstraction.
