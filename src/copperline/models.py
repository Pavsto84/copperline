"""Public immutable-ish records used across Copperline services."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from .money import Money, decimal, normalize_currency
from .timeutil import parse_date


class AccountType(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class NormalBalance(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class JournalStatus(StrEnum):
    DRAFT = "draft"
    POSTED = "posted"
    VOID = "void"


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    ISSUED = "issued"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    VOID = "void"


class PartyKind(StrEnum):
    CUSTOMER = "customer"
    VENDOR = "vendor"
    BOTH = "both"


class ItemKind(StrEnum):
    SERVICE = "service"
    INVENTORY = "inventory"
    NON_INVENTORY = "non_inventory"


class CostMethod(StrEnum):
    FIFO = "fifo"
    MOVING_AVERAGE = "moving_average"


class BankTxnStatus(StrEnum):
    UNMATCHED = "unmatched"
    MATCHED = "matched"
    IGNORED = "ignored"


@dataclass(slots=True)
class Account:
    id: str
    code: str
    name: str
    type: AccountType
    currency: str
    active: bool = True
    parent_id: str | None = None
    description: str = ""

    def __post_init__(self) -> None:
        self.currency = normalize_currency(self.currency)
        self.code = self.code.strip()
        self.name = self.name.strip()

    @property
    def normal_balance(self) -> NormalBalance:
        if self.type in {AccountType.ASSET, AccountType.EXPENSE}:
            return NormalBalance.DEBIT
        return NormalBalance.CREDIT


@dataclass(slots=True)
class JournalLine:
    account_id: str
    debit: Money
    credit: Money
    memo: str = ""
    party_id: str | None = None
    item_id: str | None = None
    tax_code: str | None = None
    dimensions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.debit.currency != self.credit.currency:
            raise ValueError("journal line debit and credit currencies differ")
        if self.debit.amount < 0 or self.credit.amount < 0:
            raise ValueError("journal line debit/credit may not be negative")
        if self.debit.amount and self.credit.amount:
            raise ValueError("journal line cannot have both debit and credit")


@dataclass(slots=True)
class JournalEntry:
    id: str
    date: date
    description: str
    currency: str
    lines: list[JournalLine]
    status: JournalStatus = JournalStatus.DRAFT
    reference: str = ""
    source: str = "manual"
    source_id: str | None = None
    posted_at: str | None = None
    voided_by: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.date = parse_date(self.date)
        self.currency = normalize_currency(self.currency)


@dataclass(slots=True)
class Party:
    id: str
    name: str
    kind: PartyKind
    currency: str
    email: str = ""
    phone: str = ""
    tax_id: str = ""
    payment_terms_days: int = 30
    receivable_account_id: str | None = None
    payable_account_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.currency = normalize_currency(self.currency)


@dataclass(slots=True)
class TaxRate:
    code: str
    name: str
    rate: Decimal
    account_id: str
    inclusive: bool = False
    recoverable_percent: Decimal = Decimal("100")
    active: bool = True

    def __post_init__(self) -> None:
        self.rate = decimal(self.rate)
        self.recoverable_percent = decimal(self.recoverable_percent)


@dataclass(slots=True)
class InvoiceLine:
    description: str
    quantity: Decimal
    unit_price: Money
    account_id: str
    item_id: str | None = None
    tax_code: str | None = None
    discount_percent: Decimal = Decimal("0")
    dimensions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.quantity = decimal(self.quantity)
        self.discount_percent = decimal(self.discount_percent)
        if self.quantity < 0:
            raise ValueError("invoice quantity may not be negative")
        if not Decimal(0) <= self.discount_percent <= Decimal(100):
            raise ValueError("discount percent must be 0..100")


@dataclass(slots=True)
class Invoice:
    id: str
    number: str
    party_id: str
    issue_date: date
    due_date: date
    currency: str
    lines: list[InvoiceLine]
    kind: str = "sale"
    status: InvoiceStatus = InvoiceStatus.DRAFT
    notes: str = ""
    journal_entry_id: str | None = None
    credit_of_invoice_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.issue_date = parse_date(self.issue_date)
        self.due_date = parse_date(self.due_date)
        self.currency = normalize_currency(self.currency)


@dataclass(slots=True)
class PaymentAllocation:
    invoice_id: str
    amount: Money


@dataclass(slots=True)
class Payment:
    id: str
    party_id: str
    date: date
    amount: Money
    account_id: str
    kind: str
    reference: str = ""
    allocations: list[PaymentAllocation] = field(default_factory=list)
    journal_entry_id: str | None = None

    def __post_init__(self) -> None:
        self.date = parse_date(self.date)


@dataclass(slots=True)
class Item:
    id: str
    sku: str
    name: str
    kind: ItemKind
    currency: str
    income_account_id: str
    expense_account_id: str
    inventory_account_id: str | None = None
    default_price: Money | None = None
    cost_method: CostMethod = CostMethod.FIFO
    active: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.currency = normalize_currency(self.currency)


@dataclass(slots=True)
class Warehouse:
    id: str
    code: str
    name: str
    active: bool = True


@dataclass(slots=True)
class InventoryMovement:
    id: str
    item_id: str
    warehouse_id: str
    date: date
    quantity: Decimal
    unit_cost: Money
    kind: str
    reference: str = ""
    source_id: str | None = None
    journal_entry_id: str | None = None

    def __post_init__(self) -> None:
        self.date = parse_date(self.date)
        self.quantity = decimal(self.quantity)


@dataclass(slots=True)
class CostLayer:
    id: str
    item_id: str
    warehouse_id: str
    acquired_date: date
    original_quantity: Decimal
    remaining_quantity: Decimal
    unit_cost: Money
    source_movement_id: str


@dataclass(slots=True)
class BankAccount:
    id: str
    name: str
    currency: str
    ledger_account_id: str
    institution: str = ""
    account_mask: str = ""

    def __post_init__(self) -> None:
        self.currency = normalize_currency(self.currency)


@dataclass(slots=True)
class BankTransaction:
    id: str
    bank_account_id: str
    posted_date: date
    amount: Money
    description: str
    external_id: str
    status: BankTxnStatus = BankTxnStatus.UNMATCHED
    matched_entry_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.posted_date = parse_date(self.posted_date)


@dataclass(slots=True)
class BudgetLine:
    account_id: str
    period_start: date
    amount: Money
    dimensions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.period_start = parse_date(self.period_start)


@dataclass(slots=True)
class Budget:
    id: str
    name: str
    currency: str
    lines: list[BudgetLine]
    fiscal_start_month: int = 1
    active: bool = True

    def __post_init__(self) -> None:
        self.currency = normalize_currency(self.currency)


@dataclass(slots=True)
class FxQuote:
    base: str
    quote: str
    date: date
    rate: Decimal
    source: str = "manual"

    def __post_init__(self) -> None:
        self.base = normalize_currency(self.base)
        self.quote = normalize_currency(self.quote)
        self.date = parse_date(self.date)
        self.rate = decimal(self.rate)


def model_dict(value: Any) -> dict[str, Any]:
    return asdict(value)
