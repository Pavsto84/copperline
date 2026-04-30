"""Cohesive application facade wiring Copperline subsystems together."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .accounts import AccountService, PeriodService
from .audit import AuditService
from .auth import AuthService
from .backup import BackupService
from .banking import BankingService
from .budget import BudgetService
from .db import Database
from .export import ExportService
from .fx import FxService
from .invoices import InvoiceService
from .inventory import InventoryService
from .ledger import LedgerService
from .parties import PartyService
from .payments import PaymentService
from .reconcile import ReconciliationService
from .reports import ReportService
from .settings import CompanySettings, SettingsService
from .tax import TaxService


class Copperline:
    """High-level application container.

    Consumers can use the facade to access focused services while sharing a single
    database connection and transaction boundary.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        timeout: float = 5.0,
        wal: bool = True,
    ) -> None:
        self.db = Database(path, timeout=timeout, wal=wal)
        self.audit = AuditService(self.db)
        self.settings = SettingsService(self.db)
        self.accounts = AccountService(self.db, self.audit)
        self.periods = PeriodService(self.db, self.audit)
        self.ledger = LedgerService(
            self.db,
            accounts=self.accounts,
            periods=self.periods,
            audit=self.audit,
        )
        self.parties = PartyService(self.db, self.audit)
        self.taxes = TaxService(self.db, self.audit)
        self.fx = FxService(self.db, self.audit)
        self.invoices = InvoiceService(
            self.db,
            self.ledger,
            self.parties,
            self.taxes,
            self.settings,
            self.audit,
        )
        self.payments = PaymentService(
            self.db,
            self.ledger,
            self.parties,
            self.invoices,
            self.settings,
            self.audit,
        )
        self.inventory = InventoryService(
            self.db,
            self.ledger,
            self.settings,
            self.audit,
        )
        self.banking = BankingService(self.db, self.audit)
        self.reconciliation = ReconciliationService(
            self.db,
            self.banking,
            self.ledger,
            self.audit,
        )
        self.budgets = BudgetService(self.db, self.accounts, self.audit)
        self.reports = ReportService(
            self.db,
            self.ledger,
            self.accounts,
            self.invoices,
            self.parties,
            self.inventory,
            self.budgets,
            self.taxes,
            self.settings,
        )
        self.auth = AuthService(self.db, self.audit)
        self.backups = BackupService(self.db)
        self.exports = ExportService(
            self.db,
            accounts=self.accounts,
            ledger=self.ledger,
            parties=self.parties,
            invoices=self.invoices,
            payments=self.payments,
            inventory=self.inventory,
            banking=self.banking,
            budgets=self.budgets,
            audit=self.audit,
        )

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Copperline":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def bootstrap(
        self,
        *,
        company_name: str,
        currency: str = "USD",
        fiscal_start_month: int = 1,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Create a practical default chart and company settings.

        The operation is safe on a fresh database and rejects a second bootstrap
        rather than silently duplicating accounts.
        """
        if self.accounts.list(active=None):
            raise RuntimeError("database already contains accounts")
        with self.db.transaction():
            cash = self.accounts.create("1000", "Cash", "asset", currency, actor=actor)
            bank = self.accounts.create("1010", "Bank", "asset", currency, actor=actor)
            ar = self.accounts.create(
                "1100", "Accounts Receivable", "asset", currency, actor=actor
            )
            inventory = self.accounts.create(
                "1200", "Inventory", "asset", currency, actor=actor
            )
            tax_recoverable = self.accounts.create(
                "1300", "Recoverable Tax", "asset", currency, actor=actor
            )
            ap = self.accounts.create(
                "2000", "Accounts Payable", "liability", currency, actor=actor
            )
            sales_tax = self.accounts.create(
                "2100", "Sales Tax Payable", "liability", currency, actor=actor
            )
            equity = self.accounts.create(
                "3000", "Owner Equity", "equity", currency, actor=actor
            )
            retained = self.accounts.create(
                "3100", "Retained Earnings", "equity", currency, actor=actor
            )
            revenue = self.accounts.create(
                "4000", "Sales Revenue", "revenue", currency, actor=actor
            )
            other_revenue = self.accounts.create(
                "4900", "Other Revenue", "revenue", currency, actor=actor
            )
            cogs = self.accounts.create(
                "5000", "Cost of Goods Sold", "expense", currency, actor=actor
            )
            expense = self.accounts.create(
                "6000", "Operating Expense", "expense", currency, actor=actor
            )
            fx_loss = self.accounts.create(
                "6900", "Foreign Exchange Loss", "expense", currency, actor=actor
            )
            fx_gain = self.accounts.create(
                "4950", "Foreign Exchange Gain", "revenue", currency, actor=actor
            )

            settings = CompanySettings(
                name=company_name,
                base_currency=currency,
                fiscal_start_month=fiscal_start_month,
                accounts_receivable_id=ar.id,
                accounts_payable_id=ap.id,
                retained_earnings_id=retained.id,
                sales_tax_payable_id=sales_tax.id,
                purchase_tax_recoverable_id=tax_recoverable.id,
                inventory_asset_id=inventory.id,
                cost_of_goods_sold_id=cogs.id,
                fx_gain_id=fx_gain.id,
                fx_loss_id=fx_loss.id,
                default_bank_id=bank.id,
            )
            self.settings.save(settings)

            self.banking.create_account(
                "Operating Bank",
                currency,
                bank.id,
                institution="",
                actor=actor,
            )

            self.audit.record(
                actor,
                "company.bootstrap",
                "company",
                {
                    "name": company_name,
                    "currency": currency,
                    "fiscal_start_month": fiscal_start_month,
                },
            )

        return {
            "settings": self.settings.get().to_dict(),
            "accounts": {
                account.code: self.accounts.to_dict(account)
                for account in self.accounts.list(active=None)
            },
            "cash_account_id": cash.id,
            "equity_account_id": equity.id,
            "revenue_account_id": revenue.id,
            "other_revenue_account_id": other_revenue.id,
            "expense_account_id": expense.id,
            "cogs_account_id": cogs.id,
        }

    def health(self) -> dict[str, Any]:
        integrity = self.db.integrity_check()
        return {
            "status": "ok" if integrity == ["ok"] else "degraded",
            "integrity": integrity,
            "schema_versions": [
                int(row["version"])
                for row in self.db.all(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ],
            "counts": self.db.table_counts(),
            "company": self.settings.get().to_dict(),
        }
