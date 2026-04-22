"""Accounting, receivables, inventory, tax, and budget reports."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .accounts import AccountService
from .budget import BudgetService
from .db import Database
from .invoices import InvoiceService
from .inventory import InventoryService
from .ledger import LedgerService
from .models import AccountType, InvoiceStatus
from .money import Money
from .parties import PartyService
from .settings import SettingsService
from .tax import TaxService
from .timeutil import DateRange, parse_date


class ReportService:
    def __init__(
        self,
        db: Database,
        ledger: LedgerService,
        accounts: AccountService,
        invoices: InvoiceService,
        parties: PartyService,
        inventory: InventoryService,
        budgets: BudgetService,
        taxes: TaxService,
        settings: SettingsService,
    ) -> None:
        self.db = db
        self.ledger = ledger
        self.accounts = accounts
        self.invoices = invoices
        self.parties = parties
        self.inventory = inventory
        self.budgets = budgets
        self.taxes = taxes
        self.settings = settings

    # -- core financial statements -------------------------------------

    def trial_balance(
        self,
        *,
        as_of: str | date,
        include_zero: bool = False,
    ) -> dict[str, Any]:
        day = parse_date(as_of)
        rows = self.ledger.trial_balance(as_of=day, include_zero=include_zero)
        settings = self.settings.get()
        debit_total = Money.zero(settings.base_currency)
        credit_total = Money.zero(settings.base_currency)
        for row in rows:
            if row["debit"]["currency"] == settings.base_currency:
                debit_total += Money(row["debit"]["amount"], settings.base_currency)
                credit_total += Money(row["credit"]["amount"], settings.base_currency)
        return {
            "as_of": day.isoformat(),
            "currency": settings.base_currency,
            "rows": rows,
            "debit_total": debit_total.to_dict(),
            "credit_total": credit_total.to_dict(),
            "balanced": debit_total == credit_total,
        }

    def profit_and_loss(
        self,
        *,
        start: str | date,
        end: str | date,
        dimensions: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        period = DateRange(parse_date(start), parse_date(end))
        settings = self.settings.get()
        revenue_rows: list[dict[str, Any]] = []
        expense_rows: list[dict[str, Any]] = []
        revenue_total = Money.zero(settings.base_currency)
        expense_total = Money.zero(settings.base_currency)

        for account in self.accounts.list(active=None):
            if account.type not in {AccountType.REVENUE, AccountType.EXPENSE}:
                continue
            debit, credit = self._debit_credit_dimensions(
                account.id,
                period.start,
                period.end,
                dimensions,
            )
            if account.type == AccountType.REVENUE:
                amount = credit - debit
                if amount.amount:
                    revenue_rows.append(self._account_amount_row(account, amount))
                    revenue_total += amount
            else:
                amount = debit - credit
                if amount.amount:
                    expense_rows.append(self._account_amount_row(account, amount))
                    expense_total += amount

        return {
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
            "currency": settings.base_currency,
            "revenue": revenue_rows,
            "expenses": expense_rows,
            "revenue_total": revenue_total.to_dict(),
            "expense_total": expense_total.to_dict(),
            "net_income": (revenue_total - expense_total).to_dict(),
            "dimensions": dimensions or {},
        }

    def balance_sheet(self, *, as_of: str | date) -> dict[str, Any]:
        day = parse_date(as_of)
        settings = self.settings.get()
        groups: dict[str, list[dict[str, Any]]] = {
            "assets": [],
            "liabilities": [],
            "equity": [],
        }
        totals = {
            "assets": Money.zero(settings.base_currency),
            "liabilities": Money.zero(settings.base_currency),
            "equity": Money.zero(settings.base_currency),
        }

        for account in self.accounts.list(active=None):
            if account.type == AccountType.ASSET:
                group = "assets"
            elif account.type == AccountType.LIABILITY:
                group = "liabilities"
            elif account.type == AccountType.EQUITY:
                group = "equity"
            else:
                continue
            amount = self.ledger.balance(account.id, as_of=day)
            if amount.amount:
                groups[group].append(self._account_amount_row(account, amount))
                totals[group] += amount

        # Current-period earnings are not necessarily closed into equity yet.
        earliest = self.db.scalar(
            "SELECT MIN(entry_date) FROM journal_entries WHERE status='posted'"
        )
        if earliest:
            pnl = self.profit_and_loss(start=str(earliest), end=day)
            current_earnings = Money(
                pnl["net_income"]["amount"],
                pnl["net_income"]["currency"],
            )
            if current_earnings.amount:
                groups["equity"].append(
                    {
                        "account": {
                            "id": "__current_earnings__",
                            "code": "",
                            "name": "Current earnings",
                            "type": "equity",
                        },
                        "amount": current_earnings.to_dict(),
                    }
                )
                totals["equity"] += current_earnings

        return {
            "as_of": day.isoformat(),
            "currency": settings.base_currency,
            **groups,
            "asset_total": totals["assets"].to_dict(),
            "liability_total": totals["liabilities"].to_dict(),
            "equity_total": totals["equity"].to_dict(),
            "equation_difference": (
                totals["assets"] - totals["liabilities"] - totals["equity"]
            ).to_dict(),
        }

    def cash_flow_indirect(
        self,
        *,
        start: str | date,
        end: str | date,
        cash_account_ids: list[str],
    ) -> dict[str, Any]:
        period = DateRange(parse_date(start), parse_date(end))
        if not cash_account_ids:
            raise ValueError("at least one cash account is required")
        settings = self.settings.get()
        pnl = self.profit_and_loss(start=period.start, end=period.end)
        net_income = Money(pnl["net_income"]["amount"], settings.base_currency)
        cash_change = Money.zero(settings.base_currency)
        for account_id in cash_account_ids:
            start_balance = self.ledger.balance(
                account_id,
                as_of=period.start - timedelta(days=1),
            )
            end_balance = self.ledger.balance(account_id, as_of=period.end)
            cash_change += end_balance - start_balance

        # Derive a transparent "other working capital/investing/financing" residual.
        residual = cash_change - net_income
        return {
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
            "currency": settings.base_currency,
            "net_income": net_income.to_dict(),
            "other_net_cash_adjustments": residual.to_dict(),
            "net_cash_change": cash_change.to_dict(),
        }

    # -- receivable/payable --------------------------------------------

    def aging(
        self,
        *,
        kind: str,
        as_of: str | date,
        buckets: tuple[int, ...] = (30, 60, 90),
    ) -> dict[str, Any]:
        if kind not in {"receivable", "payable"}:
            raise ValueError("aging kind must be receivable or payable")
        day = parse_date(as_of)
        invoice_kind = "sale" if kind == "receivable" else "purchase"
        bucket_edges = tuple(sorted(set(int(value) for value in buckets if value >= 0)))
        labels = self._aging_labels(bucket_edges)
        by_party: dict[str, dict[str, Money]] = {}
        party_names: dict[str, str] = {}
        settings = self.settings.get()

        invoices = self.invoices.list(kind=invoice_kind, limit=100000)
        for invoice in invoices:
            if invoice.issue_date > day:
                continue
            if invoice.status in {InvoiceStatus.DRAFT, InvoiceStatus.VOID}:
                continue
            outstanding = self.invoices.outstanding(invoice.id)
            if outstanding.amount <= 0:
                continue
            party = self.parties.get(invoice.party_id)
            party_names[party.id] = party.name
            age = max(0, (day - invoice.due_date).days)
            label = self._aging_bucket(age, bucket_edges)
            party_buckets = by_party.setdefault(
                party.id,
                {name: Money.zero(invoice.currency) for name in labels},
            )
            party_buckets[label] += outstanding

        rows = []
        grand = {label: Money.zero(settings.base_currency) for label in labels}
        for party_id in sorted(by_party, key=lambda pid: party_names[pid].lower()):
            buckets_value = by_party[party_id]
            total = sum(buckets_value.values(), Money.zero(settings.base_currency))
            for label, amount in buckets_value.items():
                grand[label] += amount
            rows.append(
                {
                    "party_id": party_id,
                    "party_name": party_names[party_id],
                    "buckets": {key: value.to_dict() for key, value in buckets_value.items()},
                    "total": total.to_dict(),
                }
            )
        grand_total = sum(grand.values(), Money.zero(settings.base_currency))
        return {
            "kind": kind,
            "as_of": day.isoformat(),
            "buckets": labels,
            "rows": rows,
            "totals": {key: value.to_dict() for key, value in grand.items()},
            "grand_total": grand_total.to_dict(),
        }

    # -- tax ------------------------------------------------------------

    def tax_summary(
        self,
        *,
        start: str | date,
        end: str | date,
    ) -> dict[str, Any]:
        period = DateRange(parse_date(start), parse_date(end))
        rows = self.db.all(
            """
            SELECT l.tax_code,
                   SUM(l.debit_minor) AS debit_minor,
                   SUM(l.credit_minor) AS credit_minor,
                   l.currency
            FROM journal_lines l
            JOIN journal_entries j ON j.id=l.entry_id
            JOIN tax_rates t
              ON t.code=l.tax_code
             AND t.account_id=l.account_id
            WHERE j.status='posted'
              AND j.entry_date BETWEEN ? AND ?
              AND l.tax_code IS NOT NULL
            GROUP BY l.tax_code, l.currency
            ORDER BY l.tax_code
            """,
            (period.start.isoformat(), period.end.isoformat()),
        )
        result = []
        for row in rows:
            tax = self.taxes.get(str(row["tax_code"]))
            debit = Money.from_minor(int(row["debit_minor"] or 0), str(row["currency"]))
            credit = Money.from_minor(int(row["credit_minor"] or 0), str(row["currency"]))
            result.append(
                {
                    "tax_code": tax.code,
                    "name": tax.name,
                    "rate": format(tax.rate, "f"),
                    "debit": debit.to_dict(),
                    "credit": credit.to_dict(),
                    "net_payable": (credit - debit).to_dict(),
                }
            )
        return {
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
            "rows": result,
        }

    # -- inventory ------------------------------------------------------

    def inventory_valuation(
        self,
        *,
        warehouse_id: str | None = None,
        include_zero: bool = False,
    ) -> dict[str, Any]:
        settings = self.settings.get()
        rows = []
        total = Money.zero(settings.base_currency)
        for item in self.inventory.list_items(kind="inventory", active=None):
            quantity = self.inventory.quantity_on_hand(item.id, warehouse_id)
            value = self.inventory.valuation(item.id, warehouse_id)
            if not include_zero and quantity == 0 and value.is_zero():
                continue
            rows.append(
                {
                    "item_id": item.id,
                    "sku": item.sku,
                    "name": item.name,
                    "quantity": format(quantity, "f"),
                    "value": value.to_dict(),
                    "average_unit_cost": (
                        Money.zero(item.currency).to_dict()
                        if quantity == 0
                        else (value / quantity).to_dict()
                    ),
                }
            )
            total += value
        return {
            "warehouse_id": warehouse_id,
            "currency": settings.base_currency,
            "rows": rows,
            "total": total.to_dict(),
        }

    # -- budget ---------------------------------------------------------

    def budget_variance(
        self,
        budget_id: str,
        *,
        start: str | date,
        end: str | date,
    ) -> dict[str, Any]:
        budget = self.budgets.get(budget_id)
        period = DateRange(parse_date(start), parse_date(end))
        budget_totals = self.budgets.aggregate(
            budget.id,
            start=period.start,
            end=period.end,
        )
        account_ids = sorted(set(budget_totals) | {a.id for a in self.accounts.list(active=None)})
        rows = []
        budget_grand = Money.zero(budget.currency)
        actual_grand = Money.zero(budget.currency)

        for account_id in account_ids:
            account = self.accounts.get(account_id)
            if account.type not in {AccountType.REVENUE, AccountType.EXPENSE}:
                continue
            planned = budget_totals.get(account.id, Money.zero(budget.currency))
            debit, credit = self.ledger.raw_debit_credit(
                account.id,
                start=period.start,
                end=period.end,
            )
            actual = (
                credit - debit if account.type == AccountType.REVENUE else debit - credit
            )
            if planned.is_zero() and actual.is_zero():
                continue
            variance = actual - planned
            variance_percent: Decimal | None
            if planned.amount == 0:
                variance_percent = None
            else:
                variance_percent = (
                    variance.amount / abs(planned.amount) * Decimal(100)
                ).quantize(Decimal("0.01"))
            rows.append(
                {
                    "account": AccountService.to_dict(account),
                    "budget": planned.to_dict(),
                    "actual": actual.to_dict(),
                    "variance": variance.to_dict(),
                    "variance_percent": None
                    if variance_percent is None
                    else format(variance_percent, "f"),
                }
            )
            budget_grand += planned
            actual_grand += actual

        return {
            "budget_id": budget.id,
            "budget_name": budget.name,
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
            "rows": rows,
            "budget_total": budget_grand.to_dict(),
            "actual_total": actual_grand.to_dict(),
            "variance_total": (actual_grand - budget_grand).to_dict(),
        }

    # -- helpers --------------------------------------------------------

    def _debit_credit_dimensions(
        self,
        account_id: str,
        start: date,
        end: date,
        dimensions: dict[str, str] | None,
    ) -> tuple[Money, Money]:
        account = self.accounts.get(account_id)
        rows = self.db.all(
            """
            SELECT l.debit_minor, l.credit_minor, l.dimensions_json
            FROM journal_lines l
            JOIN journal_entries j ON j.id=l.entry_id
            WHERE j.status='posted'
              AND l.account_id=?
              AND j.entry_date BETWEEN ? AND ?
            """,
            (account.id, start.isoformat(), end.isoformat()),
        )
        debit_minor = 0
        credit_minor = 0
        from .jsonutil import loads

        for row in rows:
            actual = loads(str(row["dimensions_json"]))
            if dimensions and any(actual.get(k) != v for k, v in dimensions.items()):
                continue
            debit_minor += int(row["debit_minor"])
            credit_minor += int(row["credit_minor"])
        return (
            Money.from_minor(debit_minor, account.currency),
            Money.from_minor(credit_minor, account.currency),
        )

    @staticmethod
    def _account_amount_row(account: Any, amount: Money) -> dict[str, Any]:
        return {
            "account": AccountService.to_dict(account),
            "amount": amount.to_dict(),
        }

    @staticmethod
    def _aging_labels(edges: tuple[int, ...]) -> list[str]:
        labels = ["current"]
        if not edges:
            labels.append("past_due")
            return labels
        previous = 0
        for edge in edges:
            labels.append(f"{previous + 1}-{edge}")
            previous = edge
        labels.append(f"{previous + 1}+")
        return labels

    @staticmethod
    def _aging_bucket(age: int, edges: tuple[int, ...]) -> str:
        if age <= 0:
            return "current"
        if not edges:
            return "past_due"
        previous = 0
        for edge in edges:
            if age <= edge:
                return f"{previous + 1}-{edge}"
            previous = edge
        return f"{previous + 1}+"
