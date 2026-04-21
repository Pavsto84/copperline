"""Budgets, monthly spreading, and variance analysis."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from .accounts import AccountService
from .audit import AuditService
from .db import Database
from .errors import CurrencyMismatchError, NotFoundError, ValidationError
from .ids import new_id
from .jsonutil import dumps, loads
from .models import Budget, BudgetLine
from .money import Money, allocate
from .timeutil import add_months, parse_date


class BudgetService:
    def __init__(
        self,
        db: Database,
        accounts: AccountService,
        audit: AuditService | None = None,
    ) -> None:
        self.db = db
        self.accounts = accounts
        self.audit = audit or AuditService(db)

    def create(
        self,
        name: str,
        currency: str,
        lines: Iterable[BudgetLine] = (),
        *,
        fiscal_start_month: int = 1,
        actor: str = "system",
    ) -> Budget:
        if not name.strip():
            raise ValidationError("budget name may not be empty")
        if not 1 <= fiscal_start_month <= 12:
            raise ValidationError("fiscal_start_month must be 1..12")
        line_list = list(lines)
        for line in line_list:
            account = self.accounts.get(line.account_id)
            if account.currency != currency.upper() or line.amount.currency != currency.upper():
                raise CurrencyMismatchError("budget line currency mismatch")
        budget = Budget(
            id=new_id("bud"),
            name=name.strip(),
            currency=currency,
            lines=line_list,
            fiscal_start_month=fiscal_start_month,
        )
        with self.db.transaction():
            self.db.execute(
                """
                INSERT INTO budgets(id, name, currency, fiscal_start_month, active)
                VALUES (?, ?, ?, ?, 1)
                """,
                (budget.id, budget.name, budget.currency, budget.fiscal_start_month),
            )
            self._replace_lines(budget.id, budget.lines)
            self.audit.record(actor, "budget.create", budget.id, self.to_dict(budget))
        return budget

    def get(self, budget_id: str) -> Budget:
        row = self.db.one("SELECT * FROM budgets WHERE id=?", (budget_id,))
        if row is None:
            raise NotFoundError("budget", budget_id)
        lines = [
            BudgetLine(
                account_id=str(line["account_id"]),
                period_start=parse_date(str(line["period_start"])),
                amount=Money.from_minor(int(line["amount_minor"]), str(line["currency"])),
                dimensions=loads(str(line["dimensions_json"])),
            )
            for line in self.db.all(
                "SELECT * FROM budget_lines WHERE budget_id=? ORDER BY period_start, line_no",
                (budget_id,),
            )
        ]
        return Budget(
            id=str(row["id"]),
            name=str(row["name"]),
            currency=str(row["currency"]),
            lines=lines,
            fiscal_start_month=int(row["fiscal_start_month"]),
            active=bool(row["active"]),
        )

    def list(self, *, active: bool | None = True) -> list[Budget]:
        rows = (
            self.db.all("SELECT id FROM budgets ORDER BY name")
            if active is None
            else self.db.all(
                "SELECT id FROM budgets WHERE active=? ORDER BY name",
                (1 if active else 0,),
            )
        )
        return [self.get(str(row["id"])) for row in rows]

    def replace_lines(
        self,
        budget_id: str,
        lines: Iterable[BudgetLine],
        *,
        actor: str = "system",
    ) -> Budget:
        budget = self.get(budget_id)
        line_list = list(lines)
        for line in line_list:
            account = self.accounts.get(line.account_id)
            if account.currency != budget.currency or line.amount.currency != budget.currency:
                raise CurrencyMismatchError("budget line currency mismatch")
        with self.db.transaction():
            self._replace_lines(budget.id, line_list)
            self.audit.record(
                actor,
                "budget.lines.replace",
                budget.id,
                {"line_count": len(line_list)},
            )
        return self.get(budget.id)

    def _replace_lines(self, budget_id: str, lines: list[BudgetLine]) -> None:
        self.db.execute("DELETE FROM budget_lines WHERE budget_id=?", (budget_id,))
        self.db.executemany(
            """
            INSERT INTO budget_lines(
                budget_id, line_no, account_id, period_start,
                amount_minor, currency, dimensions_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    budget_id,
                    index,
                    line.account_id,
                    date(line.period_start.year, line.period_start.month, 1).isoformat(),
                    line.amount.minor_units,
                    line.amount.currency,
                    dumps(line.dimensions),
                )
                for index, line in enumerate(lines, start=1)
            ],
        )

    def spread_annual(
        self,
        budget_id: str,
        account_id: str,
        fiscal_year_start: str | date,
        annual_amount: Money,
        *,
        weights: Iterable[Decimal | str | int | float] | None = None,
        dimensions: dict[str, str] | None = None,
        actor: str = "system",
    ) -> Budget:
        budget = self.get(budget_id)
        account = self.accounts.get(account_id)
        if annual_amount.currency != budget.currency or account.currency != budget.currency:
            raise CurrencyMismatchError("annual budget currency mismatch")
        start = parse_date(fiscal_year_start)
        start = date(start.year, start.month, 1)
        weight_list = list(weights) if weights is not None else [1] * 12
        if len(weight_list) != 12:
            raise ValidationError("annual budget spreading requires 12 weights")
        allocated = allocate(annual_amount, weight_list)
        new_lines = [
            line
            for line in budget.lines
            if not (
                line.account_id == account.id
                and start <= line.period_start < add_months(start, 12)
                and line.dimensions == (dimensions or {})
            )
        ]
        for offset, amount in enumerate(allocated):
            new_lines.append(
                BudgetLine(
                    account_id=account.id,
                    period_start=add_months(start, offset),
                    amount=amount,
                    dimensions=dict(dimensions or {}),
                )
            )
        return self.replace_lines(budget.id, new_lines, actor=actor)

    def amount_for(
        self,
        budget_id: str,
        account_id: str,
        period_start: str | date,
        *,
        dimensions: dict[str, str] | None = None,
    ) -> Money:
        budget = self.get(budget_id)
        month = parse_date(period_start)
        month = date(month.year, month.month, 1)
        total = Money.zero(budget.currency)
        for line in budget.lines:
            if line.account_id != account_id or line.period_start != month:
                continue
            if dimensions and any(line.dimensions.get(k) != v for k, v in dimensions.items()):
                continue
            total += line.amount
        return total

    def aggregate(
        self,
        budget_id: str,
        *,
        start: str | date,
        end: str | date,
    ) -> dict[str, Money]:
        budget = self.get(budget_id)
        start_date = parse_date(start)
        end_date = parse_date(end)
        totals: dict[str, Money] = defaultdict(lambda: Money.zero(budget.currency))
        for line in budget.lines:
            if start_date <= line.period_start <= end_date:
                totals[line.account_id] = totals[line.account_id] + line.amount
        return dict(totals)

    def deactivate(self, budget_id: str, *, actor: str = "system") -> Budget:
        budget = self.get(budget_id)
        self.db.execute("UPDATE budgets SET active=0 WHERE id=?", (budget.id,))
        budget.active = False
        self.audit.record(actor, "budget.deactivate", budget.id)
        return budget

    @staticmethod
    def to_dict(budget: Budget) -> dict[str, Any]:
        return {
            "id": budget.id,
            "name": budget.name,
            "currency": budget.currency,
            "fiscal_start_month": budget.fiscal_start_month,
            "active": budget.active,
            "lines": [
                {
                    "account_id": line.account_id,
                    "period_start": line.period_start.isoformat(),
                    "amount": line.amount.to_dict(),
                    "dimensions": line.dimensions,
                }
                for line in budget.lines
            ],
        }
