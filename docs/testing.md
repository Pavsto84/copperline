# Testing

Copperline's runtime test suite uses `unittest`.

```bash
PYTHONPATH=src:. python -m unittest discover -s tests -t . -v
```

## Coverage areas

### Unit

- money rounding and allocation
- exchange rates
- date ranges
- account hierarchy
- journal validation
- reversals
- period locks
- idempotency

### Integration

- sales and purchase invoices
- tax-inclusive/exclusive calculations
- credit notes
- payment allocations
- FIFO
- moving average
- warehouse transfer
- bank statement import
- candidate matching
- reconciliation sessions
- budgets
- financial statements
- API keys
- backups
- CSV/OFX import

### End-to-end

- HTTP server with real local socket
- CLI subprocesses with file SQLite
- close/reopen persistence
- invoice + inventory + payment + financial report lifecycle

## Regression rule

Every accounting bug should receive a test that asserts the ledger consequence, not only an object field.
