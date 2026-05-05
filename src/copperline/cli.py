"""Operational command-line interface for Copperline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import CopperlineError
from .http import CopperlineHttpServer
from .importers import CsvBankImporter, CsvBankMapping, OfxImporter
from .models import BudgetLine, InvoiceLine, JournalLine, PaymentAllocation
from .money import Money
from .service import Copperline


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="copperline",
        description="Self-hosted accounting, invoicing, inventory, and reconciliation",
    )
    parser.add_argument("--db", default="copperline.sqlite", help="SQLite database path")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="bootstrap a new company and chart of accounts")
    init.add_argument("--name", required=True)
    init.add_argument("--currency", default="USD")
    init.add_argument("--fiscal-start-month", type=int, default=1)

    health = commands.add_parser("doctor", help="check database integrity and configuration")

    account = commands.add_parser("account", help="chart of accounts")
    account_sub = account.add_subparsers(dest="action", required=True)
    account_list = account_sub.add_parser("list")
    account_list.add_argument("--all", action="store_true")
    account_create = account_sub.add_parser("create")
    account_create.add_argument("--code", required=True)
    account_create.add_argument("--name", required=True)
    account_create.add_argument("--type", required=True)
    account_create.add_argument("--currency")
    account_create.add_argument("--parent")
    account_balance = account_sub.add_parser("balance")
    account_balance.add_argument("account")
    account_balance.add_argument("--as-of")

    period = commands.add_parser("period", help="accounting period locks")
    period_sub = period.add_subparsers(dest="action", required=True)
    define = period_sub.add_parser("define")
    define.add_argument("--start", required=True)
    define.add_argument("--end", required=True)
    lock = period_sub.add_parser("lock")
    lock.add_argument("--start", required=True)
    lock.add_argument("--actor", default="cli")
    unlock = period_sub.add_parser("unlock")
    unlock.add_argument("--start", required=True)
    unlock.add_argument("--actor", default="cli")
    unlock.add_argument("--reason", required=True)
    period_sub.add_parser("list")

    journal = commands.add_parser("journal", help="manual double-entry journals")
    journal_sub = journal.add_subparsers(dest="action", required=True)
    post = journal_sub.add_parser("post")
    post.add_argument("--date", required=True)
    post.add_argument("--description", required=True)
    post.add_argument("--currency")
    post.add_argument("--line", action="append", default=[], metavar="ACCOUNT:DEBIT:CREDIT[:MEMO]")
    post.add_argument("--reference", default="")
    show = journal_sub.add_parser("show")
    show.add_argument("entry")
    list_j = journal_sub.add_parser("list")
    list_j.add_argument("--start")
    list_j.add_argument("--end")
    list_j.add_argument("--status")
    reverse = journal_sub.add_parser("reverse")
    reverse.add_argument("entry")
    reverse.add_argument("--date", required=True)
    reverse.add_argument("--reason", required=True)

    party = commands.add_parser("party", help="customers and vendors")
    party_sub = party.add_subparsers(dest="action", required=True)
    pcreate = party_sub.add_parser("create")
    pcreate.add_argument("--name", required=True)
    pcreate.add_argument("--kind", choices=["customer", "vendor", "both"], required=True)
    pcreate.add_argument("--currency")
    pcreate.add_argument("--email", default="")
    pcreate.add_argument("--terms", type=int, default=30)
    plist = party_sub.add_parser("list")
    plist.add_argument("--kind")
    plist.add_argument("--search")

    tax = commands.add_parser("tax", help="tax codes")
    tax_sub = tax.add_subparsers(dest="action", required=True)
    tcreate = tax_sub.add_parser("create")
    tcreate.add_argument("--code", required=True)
    tcreate.add_argument("--name", required=True)
    tcreate.add_argument("--rate", required=True)
    tcreate.add_argument("--account", required=True)
    tcreate.add_argument("--inclusive", action="store_true")
    tcreate.add_argument("--recoverable", default="100")
    tax_sub.add_parser("list")

    invoice = commands.add_parser("invoice", help="sales and purchase invoices")
    invoice_sub = invoice.add_subparsers(dest="action", required=True)
    inv_create = invoice_sub.add_parser("create")
    inv_create.add_argument("--party", required=True)
    inv_create.add_argument("--date", required=True)
    inv_create.add_argument("--kind", choices=["sale", "purchase"], default="sale")
    inv_create.add_argument("--due")
    inv_create.add_argument("--number")
    inv_create.add_argument(
        "--line",
        action="append",
        default=[],
        metavar="DESCRIPTION:QTY:UNIT_PRICE:ACCOUNT[:TAX]",
    )
    inv_create.add_argument("--issue", action="store_true")
    inv_show = invoice_sub.add_parser("show")
    inv_show.add_argument("invoice")
    inv_issue = invoice_sub.add_parser("issue")
    inv_issue.add_argument("invoice")
    inv_list = invoice_sub.add_parser("list")
    inv_list.add_argument("--party")
    inv_list.add_argument("--status")
    inv_list.add_argument("--kind")

    payment = commands.add_parser("payment", help="customer receipts and vendor payments")
    payment_sub = payment.add_subparsers(dest="action", required=True)
    record = payment_sub.add_parser("record")
    record.add_argument("--party", required=True)
    record.add_argument("--date", required=True)
    record.add_argument("--amount", required=True)
    record.add_argument("--account", required=True)
    record.add_argument("--kind", choices=["receipt", "payment"], required=True)
    record.add_argument("--reference", default="")
    record.add_argument("--allocate", action="append", default=[], metavar="INVOICE:AMOUNT")
    auto = payment_sub.add_parser("auto-allocate")
    auto.add_argument("--party", required=True)
    auto.add_argument("--amount", required=True)
    auto.add_argument("--kind", choices=["receipt", "payment"], required=True)

    item = commands.add_parser("item", help="products/services")
    item_sub = item.add_subparsers(dest="action", required=True)
    icreate = item_sub.add_parser("create")
    icreate.add_argument("--sku", required=True)
    icreate.add_argument("--name", required=True)
    icreate.add_argument("--kind", choices=["service", "inventory", "non_inventory"], required=True)
    icreate.add_argument("--currency")
    icreate.add_argument("--income-account", required=True)
    icreate.add_argument("--expense-account", required=True)
    icreate.add_argument("--inventory-account")
    icreate.add_argument("--price")
    icreate.add_argument("--cost-method", default="fifo", choices=["fifo", "moving_average"])
    item_sub.add_parser("list")

    warehouse = commands.add_parser("warehouse", help="inventory locations")
    warehouse_sub = warehouse.add_subparsers(dest="action", required=True)
    wh_create = warehouse_sub.add_parser("create")
    wh_create.add_argument("--code", required=True)
    wh_create.add_argument("--name", required=True)
    warehouse_sub.add_parser("list")

    stock = commands.add_parser("stock", help="inventory movements")
    stock_sub = stock.add_subparsers(dest="action", required=True)
    receive = stock_sub.add_parser("receive")
    receive.add_argument("--item", required=True)
    receive.add_argument("--warehouse", required=True)
    receive.add_argument("--date", required=True)
    receive.add_argument("--quantity", required=True)
    receive.add_argument("--unit-cost", required=True)
    receive.add_argument("--reference", default="")
    issue = stock_sub.add_parser("issue")
    issue.add_argument("--item", required=True)
    issue.add_argument("--warehouse", required=True)
    issue.add_argument("--date", required=True)
    issue.add_argument("--quantity", required=True)
    issue.add_argument("--reference", default="")
    transfer = stock_sub.add_parser("transfer")
    transfer.add_argument("--item", required=True)
    transfer.add_argument("--from", dest="source", required=True)
    transfer.add_argument("--to", dest="destination", required=True)
    transfer.add_argument("--date", required=True)
    transfer.add_argument("--quantity", required=True)
    stock_sub.add_parser("valuation")

    bank = commands.add_parser("bank", help="bank accounts/import/reconciliation")
    bank_sub = bank.add_subparsers(dest="action", required=True)
    bcreate = bank_sub.add_parser("create")
    bcreate.add_argument("--name", required=True)
    bcreate.add_argument("--currency")
    bcreate.add_argument("--ledger-account", required=True)
    bcreate.add_argument("--institution", default="")
    bcreate.add_argument("--mask", default="")
    bank_sub.add_parser("list")
    bimport = bank_sub.add_parser("import")
    bimport.add_argument("--bank-account", required=True)
    bimport.add_argument("--file", required=True)
    bimport.add_argument("--format", choices=["ofx", "csv"], required=True)
    bimport.add_argument("--date-column")
    bimport.add_argument("--description-column")
    bimport.add_argument("--amount-column")
    bimport.add_argument("--debit-column")
    bimport.add_argument("--credit-column")
    bimport.add_argument("--external-id-column")
    bcandidates = bank_sub.add_parser("candidates")
    bcandidates.add_argument("transaction")
    bmatch = bank_sub.add_parser("match")
    bmatch.add_argument("transaction")
    bmatch.add_argument("--entry", required=True)
    bauto = bank_sub.add_parser("auto-match")
    bauto.add_argument("--bank-account", required=True)
    bauto.add_argument("--threshold", default="0.92")

    budget = commands.add_parser("budget", help="budgets and variance")
    budget_sub = budget.add_subparsers(dest="action", required=True)
    bud_create = budget_sub.add_parser("create")
    bud_create.add_argument("--name", required=True)
    bud_create.add_argument("--currency")
    budget_sub.add_parser("list")
    spread = budget_sub.add_parser("spread")
    spread.add_argument("budget")
    spread.add_argument("--account", required=True)
    spread.add_argument("--start", required=True)
    spread.add_argument("--amount", required=True)
    spread.add_argument("--weights")
    variance = budget_sub.add_parser("variance")
    variance.add_argument("budget")
    variance.add_argument("--start", required=True)
    variance.add_argument("--end", required=True)

    report = commands.add_parser("report", help="financial and operational reports")
    report_sub = report.add_subparsers(dest="action", required=True)
    tb = report_sub.add_parser("trial-balance")
    tb.add_argument("--as-of", required=True)
    pnl = report_sub.add_parser("profit-loss")
    pnl.add_argument("--start", required=True)
    pnl.add_argument("--end", required=True)
    bs = report_sub.add_parser("balance-sheet")
    bs.add_argument("--as-of", required=True)
    aging = report_sub.add_parser("aging")
    aging.add_argument("--kind", choices=["receivable", "payable"], default="receivable")
    aging.add_argument("--as-of", required=True)
    report_sub.add_parser("inventory")
    taxr = report_sub.add_parser("tax")
    taxr.add_argument("--start", required=True)
    taxr.add_argument("--end", required=True)

    key = commands.add_parser("key", help="API keys")
    key_sub = key.add_subparsers(dest="action", required=True)
    kcreate = key_sub.add_parser("create")
    kcreate.add_argument("--label", required=True)
    kcreate.add_argument("--scope", action="append", default=[])
    key_sub.add_parser("list")
    krevoke = key_sub.add_parser("revoke")
    krevoke.add_argument("key")

    export = commands.add_parser("export", help="portable exports")
    export.add_argument("kind", choices=["journal", "invoices", "inventory", "audit", "snapshot"])
    export.add_argument("--output", required=True)
    export.add_argument("--start")
    export.add_argument("--end")

    backup = commands.add_parser("backup", help="consistent SQLite backup")
    backup.add_argument("--output", required=True)
    backup.add_argument("--archive", action="store_true")

    serve = commands.add_parser("serve", help="run JSON HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8790)

    return parser


def _emit(value: Any, *, force_json: bool = False) -> None:
    if force_json or isinstance(value, (dict, list, tuple)):
        print(json.dumps(value, ensure_ascii=False, indent=2, default=_default))
    else:
        print(value)


def _default(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _currency(app: Copperline, explicit: str | None) -> str:
    return explicit or app.settings.get().base_currency


def _journal_line(spec: str, app: Copperline, currency: str) -> JournalLine:
    parts = spec.split(":", 3)
    if len(parts) < 3:
        raise ValueError("--line requires ACCOUNT:DEBIT:CREDIT[:MEMO]")
    account = app.accounts.get(parts[0])
    return JournalLine(
        account_id=account.id,
        debit=Money(parts[1] or 0, currency),
        credit=Money(parts[2] or 0, currency),
        memo=parts[3] if len(parts) == 4 else "",
    )


def _invoice_line(spec: str, app: Copperline, currency: str) -> InvoiceLine:
    parts = spec.split(":")
    if len(parts) < 4:
        raise ValueError("--line requires DESCRIPTION:QTY:UNIT_PRICE:ACCOUNT[:TAX]")
    account = app.accounts.get(parts[3])
    return InvoiceLine(
        description=parts[0],
        quantity=parts[1],
        unit_price=Money(parts[2], currency),
        account_id=account.id,
        tax_code=parts[4] if len(parts) > 4 and parts[4] else None,
    )


def _dispatch(args: argparse.Namespace, app: Copperline) -> Any:
    command = args.command
    action = getattr(args, "action", None)

    if command == "init":
        return app.bootstrap(
            company_name=args.name,
            currency=args.currency,
            fiscal_start_month=args.fiscal_start_month,
            actor="cli",
        )
    if command == "doctor":
        return app.health()

    if command == "account":
        if action == "list":
            return [
                app.accounts.to_dict(a)
                for a in app.accounts.list(active=None if args.all else True)
            ]
        if action == "create":
            account = app.accounts.create(
                args.code,
                args.name,
                args.type,
                _currency(app, args.currency),
                parent_id=None if args.parent is None else app.accounts.get(args.parent).id,
                actor="cli",
            )
            return app.accounts.to_dict(account)
        if action == "balance":
            return app.ledger.balance(args.account, as_of=args.as_of).to_dict()

    if command == "period":
        if action == "define":
            period = app.periods.define(args.start, args.end)
            return {"start": period.start.isoformat(), "end": period.end.isoformat()}
        if action == "lock":
            return app.periods.lock(args.start, actor=args.actor)
        if action == "unlock":
            return app.periods.unlock(args.start, actor=args.actor, reason=args.reason)
        if action == "list":
            return app.periods.list()

    if command == "journal":
        if action == "post":
            currency = _currency(app, args.currency)
            lines = [_journal_line(spec, app, currency) for spec in args.line]
            return app.ledger.entry_dict(
                app.ledger.create_and_post(
                    args.date,
                    args.description,
                    currency,
                    lines,
                    reference=args.reference,
                    source="cli",
                    actor="cli",
                )
            )
        if action == "show":
            return app.ledger.entry_dict(app.ledger.get(args.entry))
        if action == "list":
            return [
                app.ledger.entry_dict(item)
                for item in app.ledger.list(
                    start=args.start,
                    end=args.end,
                    status=args.status,
                )
            ]
        if action == "reverse":
            return app.ledger.entry_dict(
                app.ledger.reverse(
                    args.entry,
                    args.date,
                    reason=args.reason,
                    actor="cli",
                )
            )

    if command == "party":
        if action == "create":
            return app.parties.to_dict(
                app.parties.create(
                    args.name,
                    args.kind,
                    _currency(app, args.currency),
                    email=args.email,
                    payment_terms_days=args.terms,
                    actor="cli",
                )
            )
        if action == "list":
            return [
                app.parties.to_dict(item)
                for item in app.parties.list(kind=args.kind, search=args.search)
            ]

    if command == "tax":
        if action == "create":
            return app.taxes.to_dict(
                app.taxes.create(
                    args.code,
                    args.name,
                    args.rate,
                    app.accounts.get(args.account).id,
                    inclusive=args.inclusive,
                    recoverable_percent=args.recoverable,
                    actor="cli",
                )
            )
        if action == "list":
            return [app.taxes.to_dict(item) for item in app.taxes.list(active=None)]

    if command == "invoice":
        if action == "create":
            party = app.parties.get(args.party)
            lines = [_invoice_line(spec, app, party.currency) for spec in args.line]
            invoice = app.invoices.create(
                party.id,
                args.date,
                lines,
                kind=args.kind,
                number=args.number,
                due_date=args.due,
                actor="cli",
            )
            if args.issue:
                invoice = app.invoices.issue(invoice.id, actor="cli")
            return app.invoices.to_dict(invoice)
        if action == "show":
            return app.invoices.to_dict(app.invoices.get(args.invoice))
        if action == "issue":
            return app.invoices.to_dict(app.invoices.issue(args.invoice, actor="cli"))
        if action == "list":
            return [
                app.invoices.to_dict(item)
                for item in app.invoices.list(
                    party_id=args.party,
                    status=args.status,
                    kind=args.kind,
                )
            ]

    if command == "payment":
        party = app.parties.get(args.party)
        if action == "auto-allocate":
            return [
                {"invoice_id": item.invoice_id, "amount": item.amount.to_dict()}
                for item in app.payments.auto_allocate(
                    party.id,
                    Money(args.amount, party.currency),
                    kind=args.kind,
                )
            ]
        allocations = []
        for spec in args.allocate:
            invoice_id, amount = spec.split(":", 1)
            allocations.append(
                PaymentAllocation(
                    invoice_id=invoice_id,
                    amount=Money(amount, party.currency),
                )
            )
        payment = app.payments.record(
            party.id,
            args.date,
            Money(args.amount, party.currency),
            app.accounts.get(args.account).id,
            kind=args.kind,
            allocations=allocations,
            reference=args.reference,
            actor="cli",
        )
        return app.payments.to_dict(payment)

    if command == "item":
        if action == "create":
            currency = _currency(app, args.currency)
            item = app.inventory.create_item(
                args.sku,
                args.name,
                args.kind,
                currency,
                app.accounts.get(args.income_account).id,
                app.accounts.get(args.expense_account).id,
                inventory_account_id=None
                if args.inventory_account is None
                else app.accounts.get(args.inventory_account).id,
                default_price=None if args.price is None else Money(args.price, currency),
                cost_method=args.cost_method,
                actor="cli",
            )
            return app.inventory.item_dict(item)
        return [
            app.inventory.item_dict(item)
            for item in app.inventory.list_items(active=None)
        ]

    if command == "warehouse":
        if action == "create":
            item = app.inventory.create_warehouse(args.code, args.name, actor="cli")
            return {"id": item.id, "code": item.code, "name": item.name}
        return [
            {"id": item.id, "code": item.code, "name": item.name, "active": item.active}
            for item in app.inventory.list_warehouses(active=None)
        ]

    if command == "stock":
        if action == "receive":
            item = app.inventory.get_item(args.item)
            movement = app.inventory.receive(
                item.id,
                args.warehouse,
                args.date,
                args.quantity,
                Money(args.unit_cost, item.currency),
                reference=args.reference,
                actor="cli",
            )
            return app.inventory.movement_dict(movement)
        if action == "issue":
            movement, cost = app.inventory.issue(
                args.item,
                args.warehouse,
                args.date,
                args.quantity,
                reference=args.reference,
                actor="cli",
            )
            return {"movement": app.inventory.movement_dict(movement), "cost": cost.to_dict()}
        if action == "transfer":
            out_move, in_move = app.inventory.transfer(
                args.item,
                args.source,
                args.destination,
                args.date,
                args.quantity,
                actor="cli",
            )
            return {
                "out": app.inventory.movement_dict(out_move),
                "in": app.inventory.movement_dict(in_move),
            }
        if action == "valuation":
            return app.reports.inventory_valuation()

    if command == "bank":
        if action == "create":
            return app.banking.account_dict(
                app.banking.create_account(
                    args.name,
                    _currency(app, args.currency),
                    app.accounts.get(args.ledger_account).id,
                    institution=args.institution,
                    account_mask=args.mask,
                    actor="cli",
                )
            )
        if action == "list":
            return [app.banking.account_dict(item) for item in app.banking.list_accounts()]
        if action == "import":
            account = app.banking.get_account(args.bank_account)
            text = Path(args.file).read_text(encoding="utf-8")
            if args.format == "ofx":
                parsed = OfxImporter().parse(text, currency=account.currency, source_name=args.file)
                transactions = parsed["transactions"]
            else:
                mapping = CsvBankMapping(
                    date_column=args.date_column or "date",
                    description_column=args.description_column or "description",
                    amount_column=args.amount_column,
                    debit_column=args.debit_column,
                    credit_column=args.credit_column,
                    external_id_column=args.external_id_column,
                )
                transactions = CsvBankImporter(mapping).parse(
                    text,
                    currency=account.currency,
                    source_name=args.file,
                )
            return app.banking.import_transactions(account.id, transactions, actor="cli")
        if action == "candidates":
            return [item.to_dict() for item in app.reconciliation.candidates(args.transaction)]
        if action == "match":
            return app.banking.transaction_dict(
                app.reconciliation.match(args.transaction, args.entry, actor="cli")
            )
        if action == "auto-match":
            return app.reconciliation.auto_match(
                args.bank_account,
                threshold=args.threshold,
                actor="cli",
            )

    if command == "budget":
        if action == "create":
            return app.budgets.to_dict(
                app.budgets.create(
                    args.name,
                    _currency(app, args.currency),
                    actor="cli",
                )
            )
        if action == "list":
            return [app.budgets.to_dict(item) for item in app.budgets.list(active=None)]
        if action == "spread":
            budget = app.budgets.get(args.budget)
            weights = None
            if args.weights:
                weights = [part.strip() for part in args.weights.split(",")]
            result = app.budgets.spread_annual(
                budget.id,
                app.accounts.get(args.account).id,
                args.start,
                Money(args.amount, budget.currency),
                weights=weights,
                actor="cli",
            )
            return app.budgets.to_dict(result)
        if action == "variance":
            return app.reports.budget_variance(
                args.budget,
                start=args.start,
                end=args.end,
            )

    if command == "report":
        if action == "trial-balance":
            return app.reports.trial_balance(as_of=args.as_of)
        if action == "profit-loss":
            return app.reports.profit_and_loss(start=args.start, end=args.end)
        if action == "balance-sheet":
            return app.reports.balance_sheet(as_of=args.as_of)
        if action == "aging":
            return app.reports.aging(kind=args.kind, as_of=args.as_of)
        if action == "inventory":
            return app.reports.inventory_valuation()
        if action == "tax":
            return app.reports.tax_summary(start=args.start, end=args.end)

    if command == "key":
        if action == "create":
            scopes = args.scope or ["*"]
            return app.auth.create_key(args.label, scopes, actor="cli")
        if action == "list":
            return app.auth.list_keys()
        if action == "revoke":
            return {"revoked": app.auth.revoke(args.key, actor="cli")}

    if command == "export":
        path = Path(args.output)
        if args.kind == "journal":
            value = app.exports.journal_csv(start=args.start, end=args.end)
        elif args.kind == "invoices":
            value = app.exports.invoices_csv()
        elif args.kind == "inventory":
            value = app.exports.inventory_csv()
        elif args.kind == "audit":
            value = app.exports.audit_jsonl()
        else:
            value = app.exports.snapshot_json()
        path.write_text(value, encoding="utf-8")
        return {"output": str(path), "bytes": path.stat().st_size}

    if command == "backup":
        path = (
            app.backups.archive(args.output)
            if args.archive
            else app.backups.sqlite_backup(args.output)
        )
        return {"output": str(path), "bytes": path.stat().st_size}

    if command == "serve":
        server = CopperlineHttpServer(app, host=args.host, port=args.port)
        host, port = server.address
        print(f"Copperline API listening on http://{host}:{port}", file=sys.stderr)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()
        return None

    raise RuntimeError(f"unhandled command {command} {action}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with Copperline(args.db) as app:
            result = _dispatch(args, app)
        if result is not None:
            _emit(result, force_json=args.json)
        return 0
    except CopperlineError as exc:
        payload = {"error": exc.as_dict()}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        if args.json:
            print(json.dumps({"error": {"code": "CLI_ERROR", "message": str(exc)}}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
