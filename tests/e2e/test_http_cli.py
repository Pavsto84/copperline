from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from copperline.http import CopperlineHttpServer
from tests.support import AppFixture


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.fx = AppFixture(file_db=True)
        created = self.fx.app.auth.create_key("http", ["*"])
        self.token = created["token"]
        self.server = CopperlineHttpServer(self.fx.app, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.address
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.fx.close()

    def request(self, method, path, body=None, auth=True):
        data = None if body is None else json.dumps(body).encode()
        headers = {}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read().decode())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_healthz_is_public(self):
        status, body = self.request("GET", "/healthz", auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_protected_endpoint_requires_key(self):
        status, body = self.request("GET", "/v1/accounts", auth=False)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "AUTHENTICATION_ERROR")

    def test_account_list(self):
        status, body = self.request("GET", "/v1/accounts")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(body), 10)

    def test_create_party_and_invoice(self):
        status, party = self.request(
            "POST",
            "/v1/parties",
            {
                "name": "HTTP Customer",
                "kind": "customer",
                "currency": "USD",
                "receivable_account_id": self.fx.account("1100").id,
            },
        )
        self.assertEqual(status, 201)
        status, invoice = self.request(
            "POST",
            "/v1/invoices",
            {
                "party_id": party["id"],
                "issue_date": "2026-01-01",
                "lines": [
                    {
                        "description": "Consulting",
                        "quantity": 1,
                        "unit_price": "100",
                        "account_id": self.fx.account("4000").id,
                    }
                ],
            },
        )
        self.assertEqual(status, 201)
        status, issued = self.request("POST", f"/v1/invoices/{invoice['id']}/issue")
        self.assertEqual(status, 200)
        self.assertEqual(issued["status"], "issued")

    def test_manual_journal_route(self):
        status, entry = self.request(
            "POST",
            "/v1/journal",
            {
                "date": "2026-01-01",
                "description": "Opening capital",
                "currency": "USD",
                "lines": [
                    {"account_id": self.fx.account("1000").id, "debit": "1000"},
                    {"account_id": self.fx.account("3000").id, "credit": "1000"},
                ],
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(entry["status"], "posted")

    def test_invalid_body_returns_400(self):
        status, body = self.request("POST", "/v1/accounts", {"code": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "VALIDATION_ERROR")

    def test_report_route(self):
        self.fx.post_cash_sale("25")
        status, report = self.request(
            "GET", "/v1/reports/profit-loss?start=2026-01-01&end=2026-01-31"
        )
        self.assertEqual(status, 200)
        self.assertEqual(report["net_income"]["amount"], "25.00")

    def test_inventory_routes(self):
        status, wh = self.request(
            "POST", "/v1/warehouses", {"code": "HTTP", "name": "HTTP Warehouse"}
        )
        self.assertEqual(status, 201)
        status, item = self.request(
            "POST",
            "/v1/items",
            {
                "sku": "HTTP-ITEM",
                "name": "HTTP Item",
                "kind": "inventory",
                "currency": "USD",
                "income_account_id": self.fx.account("4000").id,
                "expense_account_id": self.fx.account("5000").id,
                "inventory_account_id": self.fx.account("1200").id,
            },
        )
        self.assertEqual(status, 201)
        status, movement = self.request(
            "POST",
            "/v1/inventory/receive",
            {
                "item_id": item["id"],
                "warehouse_id": wh["id"],
                "date": "2026-01-01",
                "quantity": "5",
                "unit_cost": "3",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(movement["quantity"], "5")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.fx = AppFixture(file_db=True)
        self.db = self.fx.path
        # Close application so subprocesses get their own connection.
        self.fx.app.close()

    def tearDown(self):
        self.fx.tempdir.cleanup()

    def run_cli(self, *args):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
        return subprocess.run(
            [sys.executable, "-m", "copperline.cli", "--db", self.db, "--json", *args],
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
        )

    def test_doctor_command(self):
        result = self.run_cli("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(result.stdout)
        self.assertEqual(body["status"], "ok")

    def test_account_list_command(self):
        result = self.run_cli("account", "list", "--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(len(json.loads(result.stdout)), 10)

    def test_report_command(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
        # Seed with direct subprocess journal posting.
        post = subprocess.run(
            [
                sys.executable, "-m", "copperline.cli", "--db", self.db, "--json",
                "journal", "post",
                "--date", "2026-01-01",
                "--description", "CLI sale",
                "--line", "1000:25:0",
                "--line", "4000:0:25",
            ],
            text=True, capture_output=True, env=env, timeout=10,
        )
        self.assertEqual(post.returncode, 0, post.stderr)
        result = self.run_cli(
            "report", "profit-loss", "--start", "2026-01-01", "--end", "2026-01-31"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["net_income"]["amount"], "25.00")


if __name__ == "__main__":
    unittest.main()
