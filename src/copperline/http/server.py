"""Dependency-free threaded JSON HTTP API for Copperline."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from ..errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    CopperlineError,
    NotFoundError,
    ValidationError,
)
from ..models import InvoiceLine, JournalLine, PaymentAllocation
from ..money import Money
from ..service import Copperline

Handler = Callable[["RequestContext"], Any]


@dataclass(slots=True)
class Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler
    scope: str | None


@dataclass(slots=True)
class RequestContext:
    app: Copperline
    handler: BaseHTTPRequestHandler
    params: dict[str, str]
    query: dict[str, list[str]]
    principal: dict[str, object] | None
    max_body_bytes: int

    def json(self) -> Any:
        length = int(self.handler.headers.get("content-length", "0") or 0)
        if length > self.max_body_bytes:
            raise ValidationError(
                f"request body exceeds {self.max_body_bytes} bytes"
            )
        body = self.handler.rfile.read(length) if length else b""
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"invalid JSON: {exc.msg}") from exc

    def query_one(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[-1] if values else default


class Router:
    def __init__(self) -> None:
        self.routes: list[Route] = []

    def add(
        self,
        method: str,
        path: str,
        handler: Handler,
        *,
        scope: str | None = None,
    ) -> None:
        parts = []
        for segment in path.strip("/").split("/"):
            if segment.startswith("{") and segment.endswith("}"):
                name = segment[1:-1]
                parts.append(fr"(?P<{name}>[^/]+)")
            else:
                parts.append(re.escape(segment))
        regex = re.compile(r"^/" + "/".join(parts) + r"/?$")
        if path == "/":
            regex = re.compile(r"^/$")
        self.routes.append(Route(method.upper(), regex, handler, scope))

    def match(self, method: str, path: str) -> tuple[Route, dict[str, str]] | None:
        for route in self.routes:
            if route.method != method.upper():
                continue
            match = route.pattern.match(path)
            if match:
                return route, match.groupdict()
        return None


class CopperlineHttpServer:
    def __init__(
        self,
        app: Copperline,
        *,
        host: str = "127.0.0.1",
        port: int = 8790,
        max_body_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.max_body_bytes = max_body_bytes
        self.router = Router()
        self._install_routes()
        outer = self

        class RequestHandler(BaseHTTPRequestHandler):
            server_version = "Copperline/0.1"

            def do_GET(self) -> None:
                outer._dispatch(self)

            def do_POST(self) -> None:
                outer._dispatch(self)

            def do_PUT(self) -> None:
                outer._dispatch(self)

            def do_DELETE(self) -> None:
                outer._dispatch(self)

            def log_message(self, format: str, *args: object) -> None:
                # Applications embedding Copperline can provide their own HTTP logging.
                return

        self.httpd = ThreadingHTTPServer((host, port), RequestHandler)

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.httpd.server_address[:2]
        return str(host), int(port)

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _dispatch(self, handler: BaseHTTPRequestHandler) -> None:
        url = urlsplit(handler.path)
        matched = self.router.match(handler.command, url.path)
        if matched is None:
            self._send(handler, HTTPStatus.NOT_FOUND, {
                "error": {"code": "ROUTE_NOT_FOUND", "message": "route not found", "details": {}}
            })
            return
        route, params = matched
        principal = None
        try:
            if route.scope:
                principal = self._authenticate(handler)
                self.app.auth.authorize(principal, route.scope)
            context = RequestContext(
                app=self.app,
                handler=handler,
                params=params,
                query=parse_qs(url.query),
                principal=principal,
                max_body_bytes=self.max_body_bytes,
            )
            result = route.handler(context)
            status = HTTPStatus.OK
            if isinstance(result, tuple) and len(result) == 2:
                status, result = result
            self._send(handler, status, result)
        except AuthenticationError as exc:
            self._send_error(handler, HTTPStatus.UNAUTHORIZED, exc)
        except AuthorizationError as exc:
            self._send_error(handler, HTTPStatus.FORBIDDEN, exc)
        except NotFoundError as exc:
            self._send_error(handler, HTTPStatus.NOT_FOUND, exc)
        except ConflictError as exc:
            self._send_error(handler, HTTPStatus.CONFLICT, exc)
        except ValidationError as exc:
            self._send_error(handler, HTTPStatus.BAD_REQUEST, exc)
        except CopperlineError as exc:
            self._send_error(handler, HTTPStatus.UNPROCESSABLE_ENTITY, exc)
        except Exception as exc:
            self._send(
                handler,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": str(exc),
                        "details": {},
                    }
                },
            )

    def _authenticate(self, handler: BaseHTTPRequestHandler) -> dict[str, object]:
        value = handler.headers.get("authorization", "")
        kind, _, token = value.partition(" ")
        if kind.lower() != "bearer" or not token:
            raise AuthenticationError("missing bearer API key")
        return self.app.auth.authenticate(token)

    @staticmethod
    def _send(
        handler: BaseHTTPRequestHandler,
        status: int | HTTPStatus,
        value: Any,
    ) -> None:
        body = json.dumps(value, default=_json_default, ensure_ascii=False).encode()
        handler.send_response(int(status))
        handler.send_header("content-type", "application/json; charset=utf-8")
        handler.send_header("content-length", str(len(body)))
        handler.send_header("cache-control", "no-store")
        handler.end_headers()
        handler.wfile.write(body)

    def _send_error(
        self,
        handler: BaseHTTPRequestHandler,
        status: HTTPStatus,
        error: CopperlineError,
    ) -> None:
        self._send(handler, status, {"error": error.as_dict()})

    def _install_routes(self) -> None:
        r = self.router
        r.add("GET", "/healthz", lambda ctx: ctx.app.health())
        r.add("GET", "/v1/company", self._company, scope="accounts:read")
        r.add("GET", "/v1/accounts", self._accounts, scope="accounts:read")
        r.add("POST", "/v1/accounts", self._create_account, scope="accounts:write")
        r.add("GET", "/v1/accounts/{id}/balance", self._account_balance, scope="ledger:read")
        r.add("GET", "/v1/journal", self._journal, scope="ledger:read")
        r.add("POST", "/v1/journal", self._post_journal, scope="ledger:write")
        r.add("POST", "/v1/journal/{id}/reverse", self._reverse_journal, scope="ledger:write")
        r.add("GET", "/v1/parties", self._parties, scope="parties:read")
        r.add("POST", "/v1/parties", self._create_party, scope="parties:write")
        r.add("GET", "/v1/invoices", self._invoices, scope="invoices:read")
        r.add("POST", "/v1/invoices", self._create_invoice, scope="invoices:write")
        r.add("GET", "/v1/invoices/{id}", self._invoice, scope="invoices:read")
        r.add("POST", "/v1/invoices/{id}/issue", self._issue_invoice, scope="invoices:write")
        r.add("POST", "/v1/payments", self._payment, scope="payments:write")
        r.add("GET", "/v1/items", self._items, scope="inventory:read")
        r.add("POST", "/v1/items", self._create_item, scope="inventory:write")
        r.add("GET", "/v1/warehouses", self._warehouses, scope="inventory:read")
        r.add("POST", "/v1/warehouses", self._create_warehouse, scope="inventory:write")
        r.add("POST", "/v1/inventory/receive", self._receive_inventory, scope="inventory:write")
        r.add("POST", "/v1/inventory/issue", self._issue_inventory, scope="inventory:write")
        r.add("GET", "/v1/bank/accounts", self._bank_accounts, scope="banking:read")
        r.add("GET", "/v1/bank/accounts/{id}/transactions", self._bank_transactions, scope="banking:read")
        r.add("POST", "/v1/bank/transactions/{id}/match", self._match_bank, scope="banking:write")
        r.add("GET", "/v1/reports/trial-balance", self._trial_balance, scope="reports:read")
        r.add("GET", "/v1/reports/profit-loss", self._profit_loss, scope="reports:read")
        r.add("GET", "/v1/reports/balance-sheet", self._balance_sheet, scope="reports:read")
        r.add("GET", "/v1/reports/aging", self._aging, scope="reports:read")
        r.add("GET", "/v1/reports/inventory", self._inventory_report, scope="reports:read")
        r.add("GET", "/v1/audit", self._audit, scope="audit:read")

    # -- route handlers -------------------------------------------------

    def _company(self, ctx: RequestContext) -> Any:
        return ctx.app.settings.get().to_dict()

    def _accounts(self, ctx: RequestContext) -> Any:
        return [ctx.app.accounts.to_dict(item) for item in ctx.app.accounts.list(active=None)]

    def _create_account(self, ctx: RequestContext) -> Any:
        body = _object(ctx.json())
        item = ctx.app.accounts.create(
            _string(body, "code"),
            _string(body, "name"),
            _string(body, "type"),
            _string(body, "currency"),
            parent_id=_optional_string(body, "parent_id"),
            description=str(body.get("description", "")),
            actor=_actor(ctx),
        )
        return HTTPStatus.CREATED, ctx.app.accounts.to_dict(item)

    def _account_balance(self, ctx: RequestContext) -> Any:
        value = ctx.app.ledger.balance(
            ctx.params["id"],
            as_of=ctx.query_one("as_of"),
        )
        return value.to_dict()

    def _journal(self, ctx: RequestContext) -> Any:
        return [
            ctx.app.ledger.entry_dict(item)
            for item in ctx.app.ledger.list(
                start=ctx.query_one("start"),
                end=ctx.query_one("end"),
                status=ctx.query_one("status"),
                account_id=ctx.query_one("account_id"),
                limit=int(ctx.query_one("limit", "500") or "500"),
            )
        ]

    def _post_journal(self, ctx: RequestContext) -> Any:
        body = _object(ctx.json())
        currency = _string(body, "currency")
        lines = [_journal_line(item, currency) for item in _list(body, "lines")]
        entry = ctx.app.ledger.create_and_post(
            _string(body, "date"),
            _string(body, "description"),
            currency,
            lines,
            reference=str(body.get("reference", "")),
            source=str(body.get("source", "api")),
            metadata=dict(body.get("metadata") or {}),
            actor=_actor(ctx),
        )
        return HTTPStatus.CREATED, ctx.app.ledger.entry_dict(entry)

    def _reverse_journal(self, ctx: RequestContext) -> Any:
        body = _object(ctx.json())
        entry = ctx.app.ledger.reverse(
            ctx.params["id"],
            _string(body, "date"),
            reason=_string(body, "reason"),
            actor=_actor(ctx),
        )
        return ctx.app.ledger.entry_dict(entry)

    def _parties(self, ctx: RequestContext) -> Any:
        return [
            ctx.app.parties.to_dict(item)
            for item in ctx.app.parties.list(
                kind=ctx.query_one("kind"),
                search=ctx.query_one("search"),
            )
        ]

    def _create_party(self, ctx: RequestContext) -> Any:
        body = _object(ctx.json())
        item = ctx.app.parties.create(
            _string(body, "name"),
            _string(body, "kind"),
            _string(body, "currency"),
            email=str(body.get("email", "")),
            phone=str(body.get("phone", "")),
            tax_id=str(body.get("tax_id", "")),
            payment_terms_days=int(body.get("payment_terms_days", 30)),
            receivable_account_id=_optional_string(body, "receivable_account_id"),
            payable_account_id=_optional_string(body, "payable_account_id"),
            metadata=dict(body.get("metadata") or {}),
            actor=_actor(ctx),
        )
        return HTTPStatus.CREATED, ctx.app.parties.to_dict(item)

    def _invoices(self, ctx: RequestContext) -> Any:
        return [
            ctx.app.invoices.to_dict(item)
            for item in ctx.app.invoices.list(
                party_id=ctx.query_one("party_id"),
                status=ctx.query_one("status"),
                kind=ctx.query_one("kind"),
                due_before=ctx.query_one("due_before"),
            )
        ]

    def _create_invoice(self, ctx: RequestContext) -> Any:
        body = _object(ctx.json())
        party = ctx.app.parties.get(_string(body, "party_id"))
        lines = [_invoice_line(item, party.currency) for item in _list(body, "lines")]
        invoice = ctx.app.invoices.create(
            party.id,
            _string(body, "issue_date"),
            lines,
            kind=str(body.get("kind", "sale")),
            number=_optional_string(body, "number"),
            due_date=_optional_string(body, "due_date"),
            notes=str(body.get("notes", "")),
            metadata=dict(body.get("metadata") or {}),
            actor=_actor(ctx),
        )
        return HTTPStatus.CREATED, ctx.app.invoices.to_dict(invoice)

    def _invoice(self, ctx: RequestContext) -> Any:
        return ctx.app.invoices.to_dict(ctx.app.invoices.get(ctx.params["id"]))

    def _issue_invoice(self, ctx: RequestContext) -> Any:
        return ctx.app.invoices.to_dict(
            ctx.app.invoices.issue(ctx.params["id"], actor=_actor(ctx))
        )

    def _payment(self, ctx: RequestContext) -> Any:
        body = _object(ctx.json())
        party = ctx.app.parties.get(_string(body, "party_id"))
        amount = Money(_required(body, "amount"), party.currency)
        allocations = [
            PaymentAllocation(
                invoice_id=_string(_object(item), "invoice_id"),
                amount=Money(_required(_object(item), "amount"), party.currency),
            )
            for item in list(body.get("allocations") or [])
        ]
        payment = ctx.app.payments.record(
            party.id,
            _string(body, "date"),
            amount,
            _string(body, "account_id"),
            kind=_string(body, "kind"),
            allocations=allocations,
            reference=str(body.get("reference", "")),
            actor=_actor(ctx),
        )
        return HTTPStatus.CREATED, ctx.app.payments.to_dict(payment)

    def _items(self, ctx: RequestContext) -> Any:
        return [
            ctx.app.inventory.item_dict(item)
            for item in ctx.app.inventory.list_items(
                kind=ctx.query_one("kind"),
                active=None,
                search=ctx.query_one("search"),
            )
        ]

    def _create_item(self, ctx: RequestContext) -> Any:
        body = _object(ctx.json())
        item = ctx.app.inventory.create_item(
            _string(body, "sku"),
            _string(body, "name"),
            _string(body, "kind"),
            _string(body, "currency"),
            _string(body, "income_account_id"),
            _string(body, "expense_account_id"),
            inventory_account_id=_optional_string(body, "inventory_account_id"),
            default_price=None
            if body.get("default_price") is None
            else Money(body["default_price"], _string(body, "currency")),
            cost_method=str(body.get("cost_method", "fifo")),
            metadata=dict(body.get("metadata") or {}),
            actor=_actor(ctx),
        )
        return HTTPStatus.CREATED, ctx.app.inventory.item_dict(item)

    def _warehouses(self, ctx: RequestContext) -> Any:
        return [
            {"id": item.id, "code": item.code, "name": item.name, "active": item.active}
            for item in ctx.app.inventory.list_warehouses(active=None)
        ]

    def _create_warehouse(self, ctx: RequestContext) -> Any:
        body = _object(ctx.json())
        item = ctx.app.inventory.create_warehouse(
            _string(body, "code"),
            _string(body, "name"),
            actor=_actor(ctx),
        )
        return HTTPStatus.CREATED, {
            "id": item.id,
            "code": item.code,
            "name": item.name,
            "active": item.active,
        }

    def _receive_inventory(self, ctx: RequestContext) -> Any:
        body = _object(ctx.json())
        item = ctx.app.inventory.get_item(_string(body, "item_id"))
        movement = ctx.app.inventory.receive(
            item.id,
            _string(body, "warehouse_id"),
            _string(body, "date"),
            _required(body, "quantity"),
            Money(_required(body, "unit_cost"), item.currency),
            reference=str(body.get("reference", "")),
            actor=_actor(ctx),
        )
        return HTTPStatus.CREATED, ctx.app.inventory.movement_dict(movement)

    def _issue_inventory(self, ctx: RequestContext) -> Any:
        body = _object(ctx.json())
        movement, cost = ctx.app.inventory.issue(
            _string(body, "item_id"),
            _string(body, "warehouse_id"),
            _string(body, "date"),
            _required(body, "quantity"),
            reference=str(body.get("reference", "")),
            actor=_actor(ctx),
        )
        return {
            "movement": ctx.app.inventory.movement_dict(movement),
            "cost": cost.to_dict(),
        }

    def _bank_accounts(self, ctx: RequestContext) -> Any:
        return [ctx.app.banking.account_dict(item) for item in ctx.app.banking.list_accounts()]

    def _bank_transactions(self, ctx: RequestContext) -> Any:
        return [
            ctx.app.banking.transaction_dict(item)
            for item in ctx.app.banking.list_transactions(
                ctx.params["id"],
                status=ctx.query_one("status"),
                start=ctx.query_one("start"),
                end=ctx.query_one("end"),
                search=ctx.query_one("search"),
            )
        ]

    def _match_bank(self, ctx: RequestContext) -> Any:
        body = _object(ctx.json())
        item = ctx.app.reconciliation.match(
            ctx.params["id"],
            _string(body, "entry_id"),
            actor=_actor(ctx),
            allow_amount_difference=bool(body.get("allow_amount_difference", False)),
        )
        return ctx.app.banking.transaction_dict(item)

    def _trial_balance(self, ctx: RequestContext) -> Any:
        return ctx.app.reports.trial_balance(as_of=_required_query(ctx, "as_of"))

    def _profit_loss(self, ctx: RequestContext) -> Any:
        return ctx.app.reports.profit_and_loss(
            start=_required_query(ctx, "start"),
            end=_required_query(ctx, "end"),
        )

    def _balance_sheet(self, ctx: RequestContext) -> Any:
        return ctx.app.reports.balance_sheet(as_of=_required_query(ctx, "as_of"))

    def _aging(self, ctx: RequestContext) -> Any:
        return ctx.app.reports.aging(
            kind=ctx.query_one("kind", "receivable") or "receivable",
            as_of=_required_query(ctx, "as_of"),
        )

    def _inventory_report(self, ctx: RequestContext) -> Any:
        return ctx.app.reports.inventory_valuation(
            warehouse_id=ctx.query_one("warehouse_id")
        )

    def _audit(self, ctx: RequestContext) -> Any:
        return ctx.app.audit.list(
            actor=ctx.query_one("actor"),
            action=ctx.query_one("action"),
            target_prefix=ctx.query_one("target_prefix"),
            limit=int(ctx.query_one("limit", "100") or "100"),
            after_seq=int(ctx.query_one("after_seq", "0") or "0"),
        )


def _json_default(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("request body must be a JSON object")
    return value


def _list(body: dict[str, Any], name: str) -> list[Any]:
    value = body.get(name)
    if not isinstance(value, list):
        raise ValidationError(f"{name} must be an array")
    return value


def _required(body: dict[str, Any], name: str) -> Any:
    if name not in body:
        raise ValidationError(f"{name} is required")
    return body[name]


def _string(body: dict[str, Any], name: str) -> str:
    value = _required(body, name)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_string(body: dict[str, Any], name: str) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    return value.strip() or None


def _required_query(ctx: RequestContext, name: str) -> str:
    value = ctx.query_one(name)
    if not value:
        raise ValidationError(f"query parameter {name} is required")
    return value


def _actor(ctx: RequestContext) -> str:
    if ctx.principal is None:
        return "anonymous"
    return f"api_key:{ctx.principal['id']}"


def _journal_line(value: Any, currency: str) -> JournalLine:
    body = _object(value)
    debit = Money(body.get("debit", 0), currency)
    credit = Money(body.get("credit", 0), currency)
    return JournalLine(
        account_id=_string(body, "account_id"),
        debit=debit,
        credit=credit,
        memo=str(body.get("memo", "")),
        party_id=_optional_string(body, "party_id"),
        item_id=_optional_string(body, "item_id"),
        tax_code=_optional_string(body, "tax_code"),
        dimensions=dict(body.get("dimensions") or {}),
    )


def _invoice_line(value: Any, currency: str) -> InvoiceLine:
    body = _object(value)
    return InvoiceLine(
        description=_string(body, "description"),
        quantity=_required(body, "quantity"),
        unit_price=Money(_required(body, "unit_price"), currency),
        account_id=_string(body, "account_id"),
        item_id=_optional_string(body, "item_id"),
        tax_code=_optional_string(body, "tax_code"),
        discount_percent=body.get("discount_percent", 0),
        dimensions=dict(body.get("dimensions") or {}),
    )
