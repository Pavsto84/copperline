"""Company-level settings and default account references."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .db import Database
from .errors import ValidationError
from .money import normalize_currency


@dataclass(slots=True)
class CompanySettings:
    name: str = "My Company"
    base_currency: str = "USD"
    fiscal_start_month: int = 1
    accounts_receivable_id: str | None = None
    accounts_payable_id: str | None = None
    retained_earnings_id: str | None = None
    sales_tax_payable_id: str | None = None
    purchase_tax_recoverable_id: str | None = None
    inventory_asset_id: str | None = None
    cost_of_goods_sold_id: str | None = None
    fx_gain_id: str | None = None
    fx_loss_id: str | None = None
    default_bank_id: str | None = None

    def validate(self) -> None:
        self.base_currency = normalize_currency(self.base_currency)
        if not self.name.strip():
            raise ValidationError("company name may not be empty")
        if not 1 <= self.fiscal_start_month <= 12:
            raise ValidationError("fiscal_start_month must be 1..12")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base_currency": self.base_currency,
            "fiscal_start_month": self.fiscal_start_month,
            "accounts_receivable_id": self.accounts_receivable_id,
            "accounts_payable_id": self.accounts_payable_id,
            "retained_earnings_id": self.retained_earnings_id,
            "sales_tax_payable_id": self.sales_tax_payable_id,
            "purchase_tax_recoverable_id": self.purchase_tax_recoverable_id,
            "inventory_asset_id": self.inventory_asset_id,
            "cost_of_goods_sold_id": self.cost_of_goods_sold_id,
            "fx_gain_id": self.fx_gain_id,
            "fx_loss_id": self.fx_loss_id,
            "default_bank_id": self.default_bank_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CompanySettings":
        fields = cls.__dataclass_fields__
        kwargs = {key: item for key, item in value.items() if key in fields}
        result = cls(**kwargs)
        result.validate()
        return result


class SettingsService:
    KEY = "company"

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self) -> CompanySettings:
        value = self.db.get_setting(self.KEY)
        if value is None:
            settings = CompanySettings()
            settings.validate()
            return settings
        return CompanySettings.from_dict(value)

    def save(self, settings: CompanySettings) -> CompanySettings:
        settings.validate()
        self.db.set_setting(self.KEY, settings.to_dict())
        return settings

    def update(self, **changes: Any) -> CompanySettings:
        settings = self.get()
        for key, value in changes.items():
            if key not in settings.__dataclass_fields__:
                raise ValidationError(f"unknown setting: {key}")
            setattr(settings, key, value)
        return self.save(settings)
