# Security policy

## Reporting

Report vulnerabilities privately to project maintainers before publishing exploit details.

## Data sensitivity

A Copperline database can contain:

- customer/vendor names and contact information
- tax identifiers
- invoices
- payment history
- bank statement descriptions
- journal descriptions
- operational audit records

Treat the SQLite database and backups as confidential finance data.

## API keys

API tokens are generated with high entropy. Copperline stores a scrypt hash plus random salt, not the plaintext token.

Plaintext is returned only at creation time.

Scopes support:

- exact permissions, for example `reports:read`
- domain wildcards, for example `ledger:*`
- global `*`

The built-in HTTP server expects TLS termination at a trusted reverse proxy when exposed beyond localhost.

## SQLite

Use filesystem permissions to restrict database and WAL files. Do not run the database from an untrusted shared directory.

## Imports

CSV and OFX inputs are untrusted data. Importers parse values into normalized records and do not evaluate formulas or execute embedded code.

## Backup archives

Archive verification rejects unsafe extraction paths and validates SHA-256 file hashes against the manifest.

## Audit

The operational audit table is append-only through Copperline's service API, but it is not a cryptographically immutable ledger. An administrator with raw SQLite write access can change it.

## HTTP

The standard-library API enforces a body-size limit and structured JSON validation. Put rate limiting, TLS, IP policy, and request logging at the reverse proxy for internet-facing use.
