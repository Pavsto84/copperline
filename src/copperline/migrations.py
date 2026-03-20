"""SQLite schema migrations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "core accounting schema",
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        );

        CREATE TABLE accounts (
            id TEXT PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            currency TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            parent_id TEXT REFERENCES accounts(id),
            description TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE accounting_periods (
            start_date TEXT PRIMARY KEY,
            end_date TEXT NOT NULL,
            locked INTEGER NOT NULL DEFAULT 0,
            locked_at TEXT,
            locked_by TEXT
        );

        CREATE TABLE journal_entries (
            id TEXT PRIMARY KEY,
            entry_date TEXT NOT NULL,
            description TEXT NOT NULL,
            currency TEXT NOT NULL,
            status TEXT NOT NULL,
            reference TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual',
            source_id TEXT,
            posted_at TEXT,
            voided_by TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE journal_lines (
            entry_id TEXT NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
            line_no INTEGER NOT NULL,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            debit_minor INTEGER NOT NULL,
            credit_minor INTEGER NOT NULL,
            currency TEXT NOT NULL,
            memo TEXT NOT NULL DEFAULT '',
            party_id TEXT,
            item_id TEXT,
            tax_code TEXT,
            dimensions_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(entry_id, line_no)
        );

        CREATE TABLE idempotency_keys (
            namespace TEXT NOT NULL,
            key TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(namespace, key)
        );

        CREATE TABLE audit_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """,
    ),
    Migration(
        2,
        "parties tax invoices payments",
        """
        CREATE TABLE parties (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            currency TEXT NOT NULL,
            email TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            tax_id TEXT NOT NULL DEFAULT '',
            payment_terms_days INTEGER NOT NULL DEFAULT 30,
            receivable_account_id TEXT REFERENCES accounts(id),
            payable_account_id TEXT REFERENCES accounts(id),
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE tax_rates (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            rate TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            inclusive INTEGER NOT NULL DEFAULT 0,
            recoverable_percent TEXT NOT NULL DEFAULT '100',
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE invoices (
            id TEXT PRIMARY KEY,
            number TEXT NOT NULL UNIQUE,
            party_id TEXT NOT NULL REFERENCES parties(id),
            issue_date TEXT NOT NULL,
            due_date TEXT NOT NULL,
            currency TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            journal_entry_id TEXT REFERENCES journal_entries(id),
            credit_of_invoice_id TEXT REFERENCES invoices(id),
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE invoice_lines (
            invoice_id TEXT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
            line_no INTEGER NOT NULL,
            description TEXT NOT NULL,
            quantity TEXT NOT NULL,
            unit_price_minor INTEGER NOT NULL,
            currency TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            item_id TEXT,
            tax_code TEXT REFERENCES tax_rates(code),
            discount_percent TEXT NOT NULL DEFAULT '0',
            dimensions_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(invoice_id, line_no)
        );

        CREATE TABLE payments (
            id TEXT PRIMARY KEY,
            party_id TEXT NOT NULL REFERENCES parties(id),
            payment_date TEXT NOT NULL,
            amount_minor INTEGER NOT NULL,
            currency TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            kind TEXT NOT NULL,
            reference TEXT NOT NULL DEFAULT '',
            journal_entry_id TEXT REFERENCES journal_entries(id)
        );

        CREATE TABLE payment_allocations (
            payment_id TEXT NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
            invoice_id TEXT NOT NULL REFERENCES invoices(id),
            amount_minor INTEGER NOT NULL,
            currency TEXT NOT NULL,
            PRIMARY KEY(payment_id, invoice_id)
        );
        """,
    ),
    Migration(
        3,
        "inventory schema",
        """
        CREATE TABLE items (
            id TEXT PRIMARY KEY,
            sku TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            currency TEXT NOT NULL,
            income_account_id TEXT NOT NULL REFERENCES accounts(id),
            expense_account_id TEXT NOT NULL REFERENCES accounts(id),
            inventory_account_id TEXT REFERENCES accounts(id),
            default_price_minor INTEGER,
            default_price_currency TEXT,
            cost_method TEXT NOT NULL DEFAULT 'fifo',
            active INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE warehouses (
            id TEXT PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE inventory_movements (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL REFERENCES items(id),
            warehouse_id TEXT NOT NULL REFERENCES warehouses(id),
            movement_date TEXT NOT NULL,
            quantity TEXT NOT NULL,
            unit_cost_minor INTEGER NOT NULL,
            currency TEXT NOT NULL,
            kind TEXT NOT NULL,
            reference TEXT NOT NULL DEFAULT '',
            source_id TEXT,
            journal_entry_id TEXT REFERENCES journal_entries(id)
        );

        CREATE TABLE cost_layers (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL REFERENCES items(id),
            warehouse_id TEXT NOT NULL REFERENCES warehouses(id),
            acquired_date TEXT NOT NULL,
            original_quantity TEXT NOT NULL,
            remaining_quantity TEXT NOT NULL,
            unit_cost_minor INTEGER NOT NULL,
            currency TEXT NOT NULL,
            source_movement_id TEXT NOT NULL REFERENCES inventory_movements(id)
        );

        CREATE TABLE inventory_consumptions (
            movement_id TEXT NOT NULL REFERENCES inventory_movements(id),
            layer_id TEXT NOT NULL REFERENCES cost_layers(id),
            quantity TEXT NOT NULL,
            unit_cost_minor INTEGER NOT NULL,
            currency TEXT NOT NULL,
            PRIMARY KEY(movement_id, layer_id)
        );
        """,
    ),
    Migration(
        4,
        "bank reconciliation fx budgets",
        """
        CREATE TABLE bank_accounts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            currency TEXT NOT NULL,
            ledger_account_id TEXT NOT NULL REFERENCES accounts(id),
            institution TEXT NOT NULL DEFAULT '',
            account_mask TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE bank_transactions (
            id TEXT PRIMARY KEY,
            bank_account_id TEXT NOT NULL REFERENCES bank_accounts(id),
            posted_date TEXT NOT NULL,
            amount_minor INTEGER NOT NULL,
            currency TEXT NOT NULL,
            description TEXT NOT NULL,
            external_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'unmatched',
            matched_entry_id TEXT REFERENCES journal_entries(id),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(bank_account_id, external_id)
        );

        CREATE TABLE reconciliation_sessions (
            id TEXT PRIMARY KEY,
            bank_account_id TEXT NOT NULL REFERENCES bank_accounts(id),
            statement_date TEXT NOT NULL,
            statement_balance_minor INTEGER NOT NULL,
            currency TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE fx_quotes (
            base TEXT NOT NULL,
            quote TEXT NOT NULL,
            quote_date TEXT NOT NULL,
            rate TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            PRIMARY KEY(base, quote, quote_date)
        );

        CREATE TABLE budgets (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            currency TEXT NOT NULL,
            fiscal_start_month INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE budget_lines (
            budget_id TEXT NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
            line_no INTEGER NOT NULL,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            period_start TEXT NOT NULL,
            amount_minor INTEGER NOT NULL,
            currency TEXT NOT NULL,
            dimensions_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(budget_id, line_no)
        );
        """,
    ),
    Migration(
        5,
        "auth and reporting indexes",
        """
        CREATE TABLE api_keys (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            salt TEXT NOT NULL,
            scopes_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            revoked_at TEXT
        );

        CREATE INDEX idx_journal_entries_date_status
            ON journal_entries(entry_date, status);
        CREATE INDEX idx_journal_lines_account
            ON journal_lines(account_id, entry_id);
        CREATE INDEX idx_invoices_party_status_due
            ON invoices(party_id, status, due_date);
        CREATE INDEX idx_payments_party_date
            ON payments(party_id, payment_date);
        CREATE INDEX idx_inventory_movements_item_warehouse_date
            ON inventory_movements(item_id, warehouse_id, movement_date);
        CREATE INDEX idx_cost_layers_available
            ON cost_layers(item_id, warehouse_id, acquired_date)
            WHERE remaining_quantity != '0';
        CREATE INDEX idx_bank_transactions_match
            ON bank_transactions(bank_account_id, status, posted_date);
        CREATE INDEX idx_audit_events_created
            ON audit_events(created_at, seq);
        """,
    ),
)
