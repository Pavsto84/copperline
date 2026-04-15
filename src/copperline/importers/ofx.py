"""Small OFX/QFX statement parser supporting SGML and XML-like files."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal
from html import unescape
from typing import Any

from ..errors import ValidationError
from ..money import Money


_TAG_LINE = re.compile(r"(?m)^<([A-Za-z0-9_]+)>([^<\r\n]*\S)\s*$")


def _normalize_sgml(text: str) -> str:
    """Convert common OFX 1.x unclosed scalar tags into XML enough for ElementTree."""
    start = text.upper().find("<OFX>")
    if start < 0:
        raise ValidationError("OFX document has no <OFX> root")
    body = text[start:]
    body = unescape(body)
    body = _TAG_LINE.sub(lambda m: f"<{m.group(1)}>{m.group(2)}</{m.group(1)}>", body)
    # Escape bare ampersands that survived entity decoding.
    body = re.sub(r"&(?!#\d+;|#x[0-9A-Fa-f]+;|[A-Za-z]+;)", "&amp;", body)
    return body


def _text(element: ET.Element, path: str, default: str = "") -> str:
    found = element.find(path)
    return default if found is None or found.text is None else found.text.strip()


def _ofx_date(value: str) -> str:
    value = value.strip()
    if len(value) < 8:
        raise ValidationError(f"invalid OFX date: {value!r}")
    try:
        parsed = datetime.strptime(value[:8], "%Y%m%d").date()
    except ValueError as exc:
        raise ValidationError(f"invalid OFX date: {value!r}") from exc
    return parsed.isoformat()


class OfxImporter:
    def parse(
        self,
        text: str,
        *,
        currency: str | None = None,
        source_name: str = "statement.ofx",
    ) -> dict[str, Any]:
        normalized = _normalize_sgml(text)
        try:
            root = ET.fromstring(normalized)
        except ET.ParseError as exc:
            raise ValidationError(f"malformed OFX document: {exc}") from exc

        currency_code = (
            currency
            or _text(root, ".//CURDEF")
            or _text(root, ".//CURRENCY")
            or "USD"
        ).upper()
        account_id = _text(root, ".//BANKACCTFROM/ACCTID") or _text(
            root, ".//CCACCTFROM/ACCTID"
        )
        bank_id = _text(root, ".//BANKACCTFROM/BANKID")
        transactions = []
        for index, txn in enumerate(root.findall(".//STMTTRN"), start=1):
            posted = _text(txn, "DTPOSTED")
            amount_text = _text(txn, "TRNAMT")
            if not posted or not amount_text:
                continue
            try:
                amount = Decimal(amount_text)
            except Exception as exc:
                raise ValidationError(f"invalid OFX amount: {amount_text!r}") from exc
            fitid = _text(txn, "FITID")
            name = _text(txn, "NAME")
            memo = _text(txn, "MEMO")
            checknum = _text(txn, "CHECKNUM")
            txntype = _text(txn, "TRNTYPE")
            description = " ".join(part for part in (name, memo) if part).strip()
            if not fitid:
                stable = "\0".join(
                    [source_name, str(index), posted, amount_text, description]
                )
                fitid = "ofx_" + hashlib.sha256(stable.encode()).hexdigest()[:24]
            transactions.append(
                {
                    "posted_date": _ofx_date(posted),
                    "amount": Money(amount, currency_code),
                    "description": description or txntype,
                    "external_id": fitid,
                    "metadata": {
                        "source": source_name,
                        "transaction_type": txntype,
                        "check_number": checknum,
                        "name": name,
                        "memo": memo,
                    },
                }
            )

        balance_text = _text(root, ".//LEDGERBAL/BALAMT")
        balance_date = _text(root, ".//LEDGERBAL/DTASOF")
        statement_balance = None
        if balance_text:
            statement_balance = Money(balance_text, currency_code)

        return {
            "currency": currency_code,
            "account_id": account_id,
            "bank_id": bank_id,
            "transactions": transactions,
            "statement_balance": statement_balance,
            "statement_date": _ofx_date(balance_date) if balance_date else None,
        }
