# Operations

## Database integrity

Run:

```bash
copperline --db company.sqlite doctor
```

Healthy SQLite output includes:

```json
{"status":"ok","integrity":["ok"]}
```

## WAL

File databases enable WAL and NORMAL synchronous mode.

Keep `company.sqlite`, `company.sqlite-wal`, and `company.sqlite-shm` on the same local filesystem.

## Backup

Prefer Copperline's online backup command while the service is live.

Test restore procedures, not only backup creation.

## Period close

Suggested month-end sequence:

1. import all bank statements;
2. match or classify transactions;
3. reconcile statements;
4. review AR/AP aging;
5. compare stock valuation to inventory GL;
6. review tax report;
7. review trial balance;
8. clear/resolve draft journals;
9. take backup;
10. lock the accounting period.

## API deployment

Use the built-in server behind a reverse proxy such as nginx, Caddy, HAProxy, or a platform ingress.

The proxy should provide:

- TLS
- request logging
- rate limits
- connection timeouts
- IP policy when applicable

## Restore

1. stop writers;
2. verify backup archive/manifests;
3. restore SQLite;
4. start Copperline;
5. run `doctor`;
6. run a trial balance;
7. compare known statement/control totals.

## Monitoring

At minimum monitor:

- backup age
- disk space
- database integrity
- unmatched bank transaction count
- overdue receivables
- locked-period status
- HTTP 5xx rate at the reverse proxy

## Release review focus

The release checklist should prove that a fresh install can bootstrap a company, pass `doctor`, create a backup archive, verify the archive manifest, and restore enough data to run a trial balance after restart.
