# Contributing

Copperline is accounting software. Correctness and explainability matter more than reducing a function by a few lines.

## Setup

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
PYTHONPATH=src:. python -m unittest discover -s tests -t . -v
```

## Accounting changes

A change that affects money should normally include tests for:

- zero values
- negative/reversal values
- rounding at minor-unit boundaries
- partial settlement
- locked periods when applicable
- restart persistence

Business documents must not bypass the ledger when they have an accounting effect.

## Schema changes

Migrations are append-only. Never edit a migration that may exist in user databases. Add the next numbered migration and test opening an older database.

## Inventory changes

Costing tests should assert both:

- physical quantity
- general-ledger value

A feature is incomplete if only the stock subledger or only the accounting entry changes.

## Reconciliation changes

Candidate scoring may evolve, but automatic matching must remain conservative. A false negative costs review time; a false positive can silently reconcile the wrong cash movement.

## Pull requests

Keep changes focused and explain accounting consequences. Include migration notes, new CLI/API behavior, and tests in the same change where possible.
