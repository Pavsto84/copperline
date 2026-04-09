"""Inventory catalog, FIFO/moving-average costing, transfers, and COGS posting."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Iterable

from .audit import AuditService
from .db import Database
from .errors import (
    ConflictError,
    CurrencyMismatchError,
    InsufficientStockError,
    NotFoundError,
    ValidationError,
)
from .ids import new_id
from .jsonutil import dumps, loads
from .ledger import LedgerService
from .models import (
    CostLayer,
    CostMethod,
    InventoryMovement,
    Item,
    ItemKind,
    JournalLine,
    Warehouse,
)
from .money import Money, decimal
from .settings import SettingsService
from .timeutil import parse_date


class InventoryService:
    def __init__(
        self,
        db: Database,
        ledger: LedgerService,
        settings: SettingsService,
        audit: AuditService | None = None,
    ) -> None:
        self.db = db
        self.ledger = ledger
        self.settings = settings
        self.audit = audit or AuditService(db)

    # -- catalog ---------------------------------------------------------

    def create_item(
        self,
        sku: str,
        name: str,
        kind: ItemKind | str,
        currency: str,
        income_account_id: str,
        expense_account_id: str,
        *,
        inventory_account_id: str | None = None,
        default_price: Money | None = None,
        cost_method: CostMethod | str = CostMethod.FIFO,
        metadata: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> Item:
        sku = sku.strip()
        name = name.strip()
        if not sku or not name:
            raise ValidationError("item SKU and name are required")
        item_kind = ItemKind(kind)
        method = CostMethod(cost_method)
        income = self.ledger.accounts.get(income_account_id)
        expense = self.ledger.accounts.get(expense_account_id)
        if income.currency != currency or expense.currency != currency:
            raise CurrencyMismatchError("item accounts must match item currency")
        if item_kind == ItemKind.INVENTORY:
            if not inventory_account_id:
                raise ValidationError("inventory item requires inventory asset account")
            inventory_account = self.ledger.accounts.get(inventory_account_id)
            if inventory_account.currency != currency:
                raise CurrencyMismatchError("inventory account currency mismatch")
        elif inventory_account_id:
            self.ledger.accounts.get(inventory_account_id)
        if default_price and default_price.currency != currency:
            raise CurrencyMismatchError("default item price currency mismatch")
        item = Item(
            id=new_id("itm"),
            sku=sku,
            name=name,
            kind=item_kind,
            currency=currency,
            income_account_id=income.id,
            expense_account_id=expense.id,
            inventory_account_id=inventory_account_id,
            default_price=default_price,
            cost_method=method,
            metadata=metadata or {},
        )
        try:
            self.db.execute(
                """
                INSERT INTO items(
                    id, sku, name, kind, currency, income_account_id, expense_account_id,
                    inventory_account_id, default_price_minor, default_price_currency,
                    cost_method, active, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    item.id,
                    item.sku,
                    item.name,
                    item.kind.value,
                    item.currency,
                    item.income_account_id,
                    item.expense_account_id,
                    item.inventory_account_id,
                    None if item.default_price is None else item.default_price.minor_units,
                    None if item.default_price is None else item.default_price.currency,
                    item.cost_method.value,
                    dumps(item.metadata),
                ),
            )
        except Exception as exc:
            if "UNIQUE constraint failed: items.sku" in str(exc):
                raise ConflictError(f"item SKU already exists: {sku}") from exc
            raise
        self.audit.record(actor, "item.create", item.id, self.item_dict(item))
        return item

    def get_item(self, item_id_or_sku: str) -> Item:
        row = self.db.one(
            "SELECT * FROM items WHERE id=? OR sku=?",
            (item_id_or_sku, item_id_or_sku),
        )
        if row is None:
            raise NotFoundError("item", item_id_or_sku)
        return Item(
            id=str(row["id"]),
            sku=str(row["sku"]),
            name=str(row["name"]),
            kind=ItemKind(str(row["kind"])),
            currency=str(row["currency"]),
            income_account_id=str(row["income_account_id"]),
            expense_account_id=str(row["expense_account_id"]),
            inventory_account_id=None
            if row["inventory_account_id"] is None
            else str(row["inventory_account_id"]),
            default_price=None
            if row["default_price_minor"] is None
            else Money.from_minor(
                int(row["default_price_minor"]),
                str(row["default_price_currency"]),
            ),
            cost_method=CostMethod(str(row["cost_method"])),
            active=bool(row["active"]),
            metadata=loads(str(row["metadata_json"])),
        )

    def list_items(
        self,
        *,
        kind: ItemKind | str | None = None,
        active: bool | None = True,
        search: str | None = None,
    ) -> list[Item]:
        clauses = ["1=1"]
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind=?")
            params.append(ItemKind(kind).value)
        if active is not None:
            clauses.append("active=?")
            params.append(1 if active else 0)
        if search:
            clauses.append("(LOWER(name) LIKE ? OR LOWER(sku) LIKE ?)")
            query = f"%{search.lower()}%"
            params.extend([query, query])
        rows = self.db.all(
            f"""
            SELECT id FROM items
            WHERE {' AND '.join(clauses)}
            ORDER BY sku
            """,
            tuple(params),
        )
        return [self.get_item(str(row["id"])) for row in rows]

    def deactivate_item(self, item_id: str, *, actor: str = "system") -> Item:
        item = self.get_item(item_id)
        quantity = self.quantity_on_hand(item.id)
        if quantity != 0:
            raise ConflictError(
                "cannot deactivate item with stock on hand",
                details={"quantity": format(quantity, "f")},
            )
        self.db.execute("UPDATE items SET active=0 WHERE id=?", (item.id,))
        item.active = False
        self.audit.record(actor, "item.deactivate", item.id)
        return item

    def create_warehouse(
        self,
        code: str,
        name: str,
        *,
        actor: str = "system",
    ) -> Warehouse:
        code = code.strip().upper()
        name = name.strip()
        if not code or not name:
            raise ValidationError("warehouse code and name are required")
        warehouse = Warehouse(id=new_id("wh"), code=code, name=name)
        try:
            self.db.execute(
                "INSERT INTO warehouses(id, code, name, active) VALUES (?, ?, ?, 1)",
                (warehouse.id, warehouse.code, warehouse.name),
            )
        except Exception as exc:
            if "UNIQUE constraint failed: warehouses.code" in str(exc):
                raise ConflictError(f"warehouse code already exists: {code}") from exc
            raise
        self.audit.record(
            actor,
            "warehouse.create",
            warehouse.id,
            {"code": code, "name": name},
        )
        return warehouse

    def get_warehouse(self, warehouse_id_or_code: str) -> Warehouse:
        row = self.db.one(
            "SELECT * FROM warehouses WHERE id=? OR code=?",
            (warehouse_id_or_code, warehouse_id_or_code.upper()),
        )
        if row is None:
            raise NotFoundError("warehouse", warehouse_id_or_code)
        return Warehouse(
            id=str(row["id"]),
            code=str(row["code"]),
            name=str(row["name"]),
            active=bool(row["active"]),
        )

    def list_warehouses(self, *, active: bool | None = True) -> list[Warehouse]:
        rows = (
            self.db.all("SELECT * FROM warehouses ORDER BY code")
            if active is None
            else self.db.all(
                "SELECT * FROM warehouses WHERE active=? ORDER BY code",
                (1 if active else 0,),
            )
        )
        return [
            Warehouse(
                id=str(row["id"]),
                code=str(row["code"]),
                name=str(row["name"]),
                active=bool(row["active"]),
            )
            for row in rows
        ]

    # -- quantity / valuation -------------------------------------------

    def quantity_on_hand(
        self,
        item_id: str,
        warehouse_id: str | None = None,
        *,
        as_of: str | date | None = None,
    ) -> Decimal:
        item = self.get_item(item_id)
        clauses = ["item_id=?"]
        params: list[Any] = [item.id]
        if warehouse_id:
            clauses.append("warehouse_id=?")
            params.append(self.get_warehouse(warehouse_id).id)
        if as_of:
            clauses.append("movement_date<=?")
            params.append(parse_date(as_of).isoformat())
        rows = self.db.all(
            f"SELECT quantity FROM inventory_movements WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        return sum((Decimal(str(row["quantity"])) for row in rows), Decimal(0))

    def valuation(
        self,
        item_id: str,
        warehouse_id: str | None = None,
    ) -> Money:
        item = self.get_item(item_id)
        if item.kind != ItemKind.INVENTORY:
            return Money.zero(item.currency)
        clauses = ["item_id=?", "CAST(remaining_quantity AS REAL) > 0"]
        params: list[Any] = [item.id]
        if warehouse_id:
            clauses.append("warehouse_id=?")
            params.append(self.get_warehouse(warehouse_id).id)
        rows = self.db.all(
            f"""
            SELECT remaining_quantity, unit_cost_minor, currency
            FROM cost_layers
            WHERE {' AND '.join(clauses)}
            """,
            tuple(params),
        )
        total = Money.zero(item.currency)
        for row in rows:
            quantity = Decimal(str(row["remaining_quantity"]))
            unit = Money.from_minor(int(row["unit_cost_minor"]), str(row["currency"]))
            total += Money(unit.amount * quantity, item.currency)
        return total

    def average_unit_cost(
        self,
        item_id: str,
        warehouse_id: str,
    ) -> Money:
        item = self.get_item(item_id)
        quantity = self.quantity_on_hand(item.id, warehouse_id)
        if quantity <= 0:
            return Money.zero(item.currency)
        return self.valuation(item.id, warehouse_id) / quantity

    # -- movements ------------------------------------------------------

    def receive(
        self,
        item_id: str,
        warehouse_id: str,
        day: str | date,
        quantity: Decimal | str | int | float,
        unit_cost: Money,
        *,
        reference: str = "",
        source_id: str | None = None,
        offset_account_id: str | None = None,
        actor: str = "system",
    ) -> InventoryMovement:
        item = self.get_item(item_id)
        warehouse = self.get_warehouse(warehouse_id)
        qty = decimal(quantity)
        if item.kind != ItemKind.INVENTORY:
            raise ValidationError("only inventory items can be received into stock")
        if qty <= 0:
            raise ValidationError("received quantity must be positive")
        if unit_cost.amount < 0:
            raise ValidationError("unit cost may not be negative")
        if unit_cost.currency != item.currency:
            raise CurrencyMismatchError("unit cost currency differs from item")
        if not item.active or not warehouse.active:
            raise ValidationError("item and warehouse must be active")
        settings = self.settings.get()
        asset_id = item.inventory_account_id or settings.inventory_asset_id
        if not asset_id:
            raise ValidationError("inventory asset account is not configured")
        offset = (
            offset_account_id
            or settings.accounts_payable_id
            or item.expense_account_id
        )
        total_cost = Money(unit_cost.amount * qty, item.currency)
        with self.db.transaction():
            entry = self.ledger.create_and_post(
                day,
                f"Inventory receipt {item.sku} into {warehouse.code}",
                item.currency,
                [
                    JournalLine(
                        account_id=asset_id,
                        debit=total_cost,
                        credit=Money.zero(item.currency),
                        memo=reference or f"Receive {item.sku}",
                        item_id=item.id,
                    ),
                    JournalLine(
                        account_id=offset,
                        debit=Money.zero(item.currency),
                        credit=total_cost,
                        memo=reference or f"Receive {item.sku}",
                        item_id=item.id,
                    ),
                ],
                reference=reference,
                source="inventory_receipt",
                source_id=source_id,
                actor=actor,
            )
            movement = self._insert_movement(
                item,
                warehouse,
                day,
                qty,
                unit_cost,
                "receipt",
                reference=reference,
                source_id=source_id,
                journal_entry_id=entry.id,
            )
            self._create_layer(item, warehouse, movement, qty, unit_cost)
            self.audit.record(actor, "inventory.receive", movement.id, self.movement_dict(movement))
            return movement

    def issue(
        self,
        item_id: str,
        warehouse_id: str,
        day: str | date,
        quantity: Decimal | str | int | float,
        *,
        reference: str = "",
        source_id: str | None = None,
        expense_account_id: str | None = None,
        actor: str = "system",
    ) -> tuple[InventoryMovement, Money]:
        item = self.get_item(item_id)
        warehouse = self.get_warehouse(warehouse_id)
        qty = decimal(quantity)
        if item.kind != ItemKind.INVENTORY:
            raise ValidationError("only inventory items can be issued from stock")
        if qty <= 0:
            raise ValidationError("issued quantity must be positive")
        available = self.quantity_on_hand(item.id, warehouse.id)
        if available < qty:
            raise InsufficientStockError(
                f"insufficient stock for {item.sku}: need {qty}, have {available}",
                details={
                    "item_id": item.id,
                    "warehouse_id": warehouse.id,
                    "required": format(qty, "f"),
                    "available": format(available, "f"),
                },
            )
        settings = self.settings.get()
        asset_id = item.inventory_account_id or settings.inventory_asset_id
        expense_id = expense_account_id or item.expense_account_id or settings.cost_of_goods_sold_id
        if not asset_id or not expense_id:
            raise ValidationError("inventory/COGS accounts are not configured")

        with self.db.transaction():
            consumption = self._cost_issue(item, warehouse, qty)
            total_cost = sum(
                (
                    Money(unit.amount * consumed_qty, item.currency)
                    for _layer_id, consumed_qty, unit in consumption
                ),
                Money.zero(item.currency),
            )
            entry = self.ledger.create_and_post(
                day,
                f"Inventory issue {item.sku} from {warehouse.code}",
                item.currency,
                [
                    JournalLine(
                        account_id=expense_id,
                        debit=total_cost,
                        credit=Money.zero(item.currency),
                        memo=reference or f"Issue {item.sku}",
                        item_id=item.id,
                    ),
                    JournalLine(
                        account_id=asset_id,
                        debit=Money.zero(item.currency),
                        credit=total_cost,
                        memo=reference or f"Issue {item.sku}",
                        item_id=item.id,
                    ),
                ],
                reference=reference,
                source="inventory_issue",
                source_id=source_id,
                actor=actor,
            )
            # Store movement unit cost as weighted cost of this issue.
            unit_cost = total_cost / qty if qty else Money.zero(item.currency)
            movement = self._insert_movement(
                item,
                warehouse,
                day,
                -qty,
                unit_cost,
                "issue",
                reference=reference,
                source_id=source_id,
                journal_entry_id=entry.id,
            )
            self.db.executemany(
                """
                INSERT INTO inventory_consumptions(
                    movement_id, layer_id, quantity, unit_cost_minor, currency
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        movement.id,
                        layer_id,
                        format(consumed_qty, "f"),
                        unit.minor_units,
                        unit.currency,
                    )
                    for layer_id, consumed_qty, unit in consumption
                ],
            )
            self.audit.record(actor, "inventory.issue", movement.id, self.movement_dict(movement))
            return movement, total_cost

    def transfer(
        self,
        item_id: str,
        from_warehouse_id: str,
        to_warehouse_id: str,
        day: str | date,
        quantity: Decimal | str | int | float,
        *,
        reference: str = "",
        actor: str = "system",
    ) -> tuple[InventoryMovement, InventoryMovement]:
        item = self.get_item(item_id)
        source = self.get_warehouse(from_warehouse_id)
        destination = self.get_warehouse(to_warehouse_id)
        if source.id == destination.id:
            raise ValidationError("transfer source and destination must differ")
        qty = decimal(quantity)
        if qty <= 0:
            raise ValidationError("transfer quantity must be positive")
        if self.quantity_on_hand(item.id, source.id) < qty:
            raise InsufficientStockError("insufficient stock for transfer")
        with self.db.transaction():
            consumption = self._cost_issue(item, source, qty)
            total_cost = sum(
                (Money(unit.amount * q, item.currency) for _layer, q, unit in consumption),
                Money.zero(item.currency),
            )
            weighted = total_cost / qty
            out_move = self._insert_movement(
                item,
                source,
                day,
                -qty,
                weighted,
                "transfer_out",
                reference=reference,
                source_id=None,
                journal_entry_id=None,
            )
            self.db.executemany(
                """
                INSERT INTO inventory_consumptions(
                    movement_id, layer_id, quantity, unit_cost_minor, currency
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (out_move.id, layer, format(q, "f"), unit.minor_units, unit.currency)
                    for layer, q, unit in consumption
                ],
            )
            in_move = self._insert_movement(
                item,
                destination,
                day,
                qty,
                weighted,
                "transfer_in",
                reference=reference,
                source_id=out_move.id,
                journal_entry_id=None,
            )
            self._create_layer(item, destination, in_move, qty, weighted)
            self.audit.record(
                actor,
                "inventory.transfer",
                out_move.id,
                {
                    "item_id": item.id,
                    "from": source.id,
                    "to": destination.id,
                    "quantity": format(qty, "f"),
                    "cost": total_cost.to_dict(),
                },
            )
            return out_move, in_move

    def adjust(
        self,
        item_id: str,
        warehouse_id: str,
        day: str | date,
        delta_quantity: Decimal | str | int | float,
        *,
        unit_cost: Money | None = None,
        reason: str,
        adjustment_account_id: str | None = None,
        actor: str = "system",
    ) -> tuple[InventoryMovement, Money]:
        delta = decimal(delta_quantity)
        if delta == 0:
            raise ValidationError("inventory adjustment delta may not be zero")
        if not reason.strip():
            raise ValidationError("inventory adjustment reason is required")
        item = self.get_item(item_id)
        if delta > 0:
            cost = unit_cost or self.average_unit_cost(item.id, warehouse_id)
            movement = self.receive(
                item.id,
                warehouse_id,
                day,
                delta,
                cost,
                reference=f"Adjustment: {reason}",
                offset_account_id=adjustment_account_id or item.expense_account_id,
                actor=actor,
            )
            return movement, Money(cost.amount * delta, item.currency)
        movement, cost = self.issue(
            item.id,
            warehouse_id,
            day,
            -delta,
            reference=f"Adjustment: {reason}",
            expense_account_id=adjustment_account_id or item.expense_account_id,
            actor=actor,
        )
        return movement, cost

    # -- internal costing ------------------------------------------------

    def _cost_issue(
        self,
        item: Item,
        warehouse: Warehouse,
        quantity: Decimal,
    ) -> list[tuple[str, Decimal, Money]]:
        if item.cost_method == CostMethod.FIFO:
            return self._fifo_consume(item, warehouse, quantity)
        return self._moving_average_consume(item, warehouse, quantity)

    def _fifo_consume(
        self,
        item: Item,
        warehouse: Warehouse,
        quantity: Decimal,
    ) -> list[tuple[str, Decimal, Money]]:
        remaining = quantity
        result: list[tuple[str, Decimal, Money]] = []
        rows = self.db.all(
            """
            SELECT * FROM cost_layers
            WHERE item_id=? AND warehouse_id=?
              AND CAST(remaining_quantity AS REAL) > 0
            ORDER BY acquired_date, id
            """,
            (item.id, warehouse.id),
        )
        for row in rows:
            if remaining <= 0:
                break
            available = Decimal(str(row["remaining_quantity"]))
            use = min(remaining, available)
            unit = Money.from_minor(int(row["unit_cost_minor"]), str(row["currency"]))
            new_remaining = available - use
            self.db.execute(
                "UPDATE cost_layers SET remaining_quantity=? WHERE id=?",
                (format(new_remaining, "f"), row["id"]),
            )
            result.append((str(row["id"]), use, unit))
            remaining -= use
        if remaining > 0:
            raise InsufficientStockError("cost layers do not cover quantity on hand")
        return result

    def _moving_average_consume(
        self,
        item: Item,
        warehouse: Warehouse,
        quantity: Decimal,
    ) -> list[tuple[str, Decimal, Money]]:
        rows = self.db.all(
            """
            SELECT * FROM cost_layers
            WHERE item_id=? AND warehouse_id=?
              AND CAST(remaining_quantity AS REAL) > 0
            ORDER BY acquired_date, id
            """,
            (item.id, warehouse.id),
        )
        total_qty = sum((Decimal(str(row["remaining_quantity"])) for row in rows), Decimal(0))
        if total_qty < quantity:
            raise InsufficientStockError("cost layers do not cover quantity on hand")
        total_value = Money.zero(item.currency)
        for row in rows:
            q = Decimal(str(row["remaining_quantity"]))
            unit = Money.from_minor(int(row["unit_cost_minor"]), str(row["currency"]))
            total_value += Money(q * unit.amount, item.currency)
        average = total_value / total_qty
        # Proportionally consume layers to preserve their remaining total while pricing
        # the outbound movement at the current moving average.
        remaining = quantity
        result: list[tuple[str, Decimal, Money]] = []
        for index, row in enumerate(rows):
            available = Decimal(str(row["remaining_quantity"]))
            if index == len(rows) - 1:
                use = remaining
            else:
                use = min(
                    available,
                    (quantity * available / total_qty).quantize(
                        Decimal("0.000001"), rounding=ROUND_HALF_EVEN
                    ),
                )
            if use <= 0:
                continue
            self.db.execute(
                "UPDATE cost_layers SET remaining_quantity=? WHERE id=?",
                (format(available - use, "f"), row["id"]),
            )
            result.append((str(row["id"]), use, average))
            remaining -= use
        if remaining != 0:
            # Rounding residual is applied to the newest layer with capacity.
            layer_id, used, unit = result[-1]
            row = self.db.one("SELECT remaining_quantity FROM cost_layers WHERE id=?", (layer_id,))
            assert row is not None
            current = Decimal(str(row["remaining_quantity"]))
            self.db.execute(
                "UPDATE cost_layers SET remaining_quantity=? WHERE id=?",
                (format(current - remaining, "f"), layer_id),
            )
            result[-1] = (layer_id, used + remaining, unit)
        return result

    def _create_layer(
        self,
        item: Item,
        warehouse: Warehouse,
        movement: InventoryMovement,
        quantity: Decimal,
        unit_cost: Money,
    ) -> CostLayer:
        layer = CostLayer(
            id=new_id("layer"),
            item_id=item.id,
            warehouse_id=warehouse.id,
            acquired_date=movement.date,
            original_quantity=quantity,
            remaining_quantity=quantity,
            unit_cost=unit_cost,
            source_movement_id=movement.id,
        )
        self.db.execute(
            """
            INSERT INTO cost_layers(
                id, item_id, warehouse_id, acquired_date, original_quantity,
                remaining_quantity, unit_cost_minor, currency, source_movement_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                layer.id,
                layer.item_id,
                layer.warehouse_id,
                layer.acquired_date.isoformat(),
                format(layer.original_quantity, "f"),
                format(layer.remaining_quantity, "f"),
                layer.unit_cost.minor_units,
                layer.unit_cost.currency,
                layer.source_movement_id,
            ),
        )
        return layer

    def _insert_movement(
        self,
        item: Item,
        warehouse: Warehouse,
        day: str | date,
        quantity: Decimal,
        unit_cost: Money,
        kind: str,
        *,
        reference: str,
        source_id: str | None,
        journal_entry_id: str | None,
    ) -> InventoryMovement:
        movement = InventoryMovement(
            id=new_id("mov"),
            item_id=item.id,
            warehouse_id=warehouse.id,
            date=parse_date(day),
            quantity=quantity,
            unit_cost=unit_cost,
            kind=kind,
            reference=reference,
            source_id=source_id,
            journal_entry_id=journal_entry_id,
        )
        self.db.execute(
            """
            INSERT INTO inventory_movements(
                id, item_id, warehouse_id, movement_date, quantity, unit_cost_minor,
                currency, kind, reference, source_id, journal_entry_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                movement.id,
                movement.item_id,
                movement.warehouse_id,
                movement.date.isoformat(),
                format(movement.quantity, "f"),
                movement.unit_cost.minor_units,
                movement.unit_cost.currency,
                movement.kind,
                movement.reference,
                movement.source_id,
                movement.journal_entry_id,
            ),
        )
        return movement

    def movements(
        self,
        *,
        item_id: str | None = None,
        warehouse_id: str | None = None,
        start: str | date | None = None,
        end: str | date | None = None,
        limit: int = 5000,
    ) -> list[InventoryMovement]:
        clauses = ["1=1"]
        params: list[Any] = []
        if item_id:
            clauses.append("item_id=?")
            params.append(self.get_item(item_id).id)
        if warehouse_id:
            clauses.append("warehouse_id=?")
            params.append(self.get_warehouse(warehouse_id).id)
        if start:
            clauses.append("movement_date>=?")
            params.append(parse_date(start).isoformat())
        if end:
            clauses.append("movement_date<=?")
            params.append(parse_date(end).isoformat())
        params.append(max(1, min(limit, 10000)))
        return [
            InventoryMovement(
                id=str(row["id"]),
                item_id=str(row["item_id"]),
                warehouse_id=str(row["warehouse_id"]),
                date=parse_date(str(row["movement_date"])),
                quantity=Decimal(str(row["quantity"])),
                unit_cost=Money.from_minor(int(row["unit_cost_minor"]), str(row["currency"])),
                kind=str(row["kind"]),
                reference=str(row["reference"]),
                source_id=None if row["source_id"] is None else str(row["source_id"]),
                journal_entry_id=None
                if row["journal_entry_id"] is None
                else str(row["journal_entry_id"]),
            )
            for row in self.db.all(
                f"""
                SELECT * FROM inventory_movements
                WHERE {' AND '.join(clauses)}
                ORDER BY movement_date, id
                LIMIT ?
                """,
                tuple(params),
            )
        ]

    def layers(self, item_id: str, warehouse_id: str) -> list[CostLayer]:
        item = self.get_item(item_id)
        warehouse = self.get_warehouse(warehouse_id)
        return [
            CostLayer(
                id=str(row["id"]),
                item_id=str(row["item_id"]),
                warehouse_id=str(row["warehouse_id"]),
                acquired_date=parse_date(str(row["acquired_date"])),
                original_quantity=Decimal(str(row["original_quantity"])),
                remaining_quantity=Decimal(str(row["remaining_quantity"])),
                unit_cost=Money.from_minor(int(row["unit_cost_minor"]), str(row["currency"])),
                source_movement_id=str(row["source_movement_id"]),
            )
            for row in self.db.all(
                """
                SELECT * FROM cost_layers
                WHERE item_id=? AND warehouse_id=?
                ORDER BY acquired_date, id
                """,
                (item.id, warehouse.id),
            )
        ]

    @staticmethod
    def item_dict(item: Item) -> dict[str, Any]:
        return {
            "id": item.id,
            "sku": item.sku,
            "name": item.name,
            "kind": item.kind.value,
            "currency": item.currency,
            "income_account_id": item.income_account_id,
            "expense_account_id": item.expense_account_id,
            "inventory_account_id": item.inventory_account_id,
            "default_price": None if item.default_price is None else item.default_price.to_dict(),
            "cost_method": item.cost_method.value,
            "active": item.active,
            "metadata": item.metadata,
        }

    @staticmethod
    def movement_dict(movement: InventoryMovement) -> dict[str, Any]:
        return {
            "id": movement.id,
            "item_id": movement.item_id,
            "warehouse_id": movement.warehouse_id,
            "date": movement.date.isoformat(),
            "quantity": format(movement.quantity, "f"),
            "unit_cost": movement.unit_cost.to_dict(),
            "kind": movement.kind,
            "reference": movement.reference,
            "source_id": movement.source_id,
            "journal_entry_id": movement.journal_entry_id,
        }
