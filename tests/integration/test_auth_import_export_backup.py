from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from copperline.auth import AuthorizationError
from copperline.backup import BackupService
from copperline.errors import AuthenticationError, ValidationError
from copperline.importers import CsvBankImporter, CsvBankMapping, OfxImporter
from copperline.money import Money
from tests.support import AppFixture


class AuthImportExportBackupTests(unittest.TestCase):
    def setUp(self):
        self.fx = AppFixture(file_db=True)

    def tearDown(self):
        self.fx.close()

    def test_api_key_authentication_and_scope(self):
        created = self.fx.app.auth.create_key("reader", ["reports:read"])
        principal = self.fx.app.auth.authenticate(created["token"])
        self.fx.app.auth.authorize(principal, "reports:read")
        with self.assertRaises(AuthorizationError):
            self.fx.app.auth.authorize(principal, "ledger:write")

    def test_api_key_revoke(self):
        created = self.fx.app.auth.create_key("reader", ["*"])
        self.assertTrue(self.fx.app.auth.revoke(created["id"]))
        with self.assertRaises(AuthenticationError):
            self.fx.app.auth.authenticate(created["token"])

    def test_csv_amount_import(self):
        mapping = CsvBankMapping("date", "description", amount_column="amount", external_id_column="id")
        parsed = CsvBankImporter(mapping).parse(
            "date,description,amount,id\n2026-01-01,Coffee,-4.25,x1\n",
            currency="USD",
        )
        self.assertEqual(parsed[0]["amount"], Money("-4.25", "USD"))
        self.assertEqual(parsed[0]["external_id"], "x1")

    def test_csv_debit_credit_import(self):
        mapping = CsvBankMapping(
            "date", "description", debit_column="debit", credit_column="credit"
        )
        parsed = CsvBankImporter(mapping).parse(
            "date,description,debit,credit\n2026-01-01,Deposit,,25\n2026-01-02,Fee,5,\n",
            currency="USD",
        )
        self.assertEqual(parsed[0]["amount"], Money("25", "USD"))
        self.assertEqual(parsed[1]["amount"], Money("-5", "USD"))

    def test_csv_missing_columns_rejected(self):
        mapping = CsvBankMapping("date", "description", amount_column="amount")
        with self.assertRaises(ValidationError):
            CsvBankImporter(mapping).parse("date,description\n2026-01-01,x\n", currency="USD")

    def test_ofx_import(self):
        text = """OFXHEADER:100
DATA:OFXSGML
<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<CURDEF>USD
<BANKACCTFROM>
<BANKID>123
<ACCTID>456
</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260101120000
<TRNAMT>-12.34
<FITID>abc
<NAME>Store
<MEMO>Purchase
</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL>
<BALAMT>100.00
<DTASOF>20260102120000
</LEDGERBAL>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>"""
        parsed = OfxImporter().parse(text)
        self.assertEqual(parsed["currency"], "USD")
        self.assertEqual(parsed["account_id"], "456")
        self.assertEqual(parsed["transactions"][0]["amount"], Money("-12.34", "USD"))
        self.assertEqual(parsed["statement_balance"], Money("100", "USD"))

    def test_journal_export_contains_posted_entry(self):
        entry = self.fx.post_cash_sale("25")
        value = self.fx.app.exports.journal_csv()
        self.assertIn(entry.id, value)
        self.assertIn("Cash sale", value)
        self.assertIn("25.00", value)

    def test_snapshot_export_contains_company_data(self):
        self.fx.customer("Snapshot Customer")
        value = self.fx.app.exports.snapshot_json()
        self.assertIn("copperline-snapshot-v1", value)
        self.assertIn("Snapshot Customer", value)
        self.assertIn('"accounts"', value)

    def test_sqlite_backup_opens_and_has_data(self):
        self.fx.post_cash_sale("10")
        target = Path(self.fx.tempdir.name) / "copy.sqlite"
        self.fx.app.backups.sqlite_backup(target)
        import sqlite3
        conn = sqlite3.connect(target)
        try:
            count = conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_archive_backup_verifies(self):
        self.fx.post_cash_sale("10")
        target = Path(self.fx.tempdir.name) / "backup.tar.gz"
        self.fx.app.backups.archive(target, metadata={"environment": "test"})
        report = BackupService.verify_archive(target)
        self.assertTrue(report["valid"])
        self.assertEqual(report["manifest"]["metadata"]["environment"], "test")

    def test_audit_export_jsonl(self):
        self.fx.customer("Audit Customer")
        lines = self.fx.app.exports.audit_jsonl().splitlines()
        self.assertGreaterEqual(len(lines), 1)
        self.assertTrue(all(line.startswith("{") for line in lines))


if __name__ == "__main__":
    unittest.main()
