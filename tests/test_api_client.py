# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Transport, queries and request shapes against a mocked QuickBooks API."""

import base64
import json
from datetime import date

import httpx
import pytest

from qbo_mcp import api_client
from qbo_mcp.api_client import QboClient, QboCredentials, QboError
from qbo_mcp.server import _credentials_var, create_mcp_server

CREDS = QboCredentials(access_token="tok-1", realm_id="123")
BASE = "/v3/company/123/"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(api_client.asyncio, "sleep", instant)


class Recorder:
    """Mock transport: replies by "METHOD path" or path below /v3/company/{realm}/."""

    def __init__(self, routes=None, default=None):
        self.requests: list[httpx.Request] = []
        self.routes = routes or {}
        self.default = default if default is not None else {"QueryResponse": {}}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix(BASE)
        reply = self.routes.get(f"{request.method} {path}", self.routes.get(path, self.default))
        if callable(reply):
            return reply(request)
        return httpx.Response(200, json=reply)

    def calls(self, path: str, method: str | None = None) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == BASE + path and (method is None or r.method == method)]


def body(request: httpx.Request):
    return json.loads(request.content) if request.content else None


def sql_of(request: httpx.Request) -> str:
    return request.content.decode()


async def _call(monkeypatch, recorder: Recorder, tool: str, args: dict):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    monkeypatch.setattr(api_client, "_get_shared_http_client", lambda: http)
    ctx = _credentials_var.set(CREDS)
    try:
        content, _ = await create_mcp_server().call_tool(tool, args)
    finally:
        _credentials_var.reset(ctx)
    return json.loads(content[0].text)


# ---- transport -------------------------------------------------------------


async def test_bearer_minorversion_and_query_as_text():
    rec = Recorder({"query": {"QueryResponse": {"Customer": [{"Id": "1"}]}}})
    client = QboClient(CREDS, httpx.AsyncClient(transport=httpx.MockTransport(rec)))
    await client.query("SELECT * FROM Customer")
    request = rec.requests[0]
    assert str(request.url).startswith("https://quickbooks.api.intuit.com/v3/company/123/query?minorversion=")
    assert request.headers["Authorization"] == "Bearer tok-1"
    assert request.headers["Content-Type"] == "application/text"
    assert sql_of(request) == "SELECT * FROM Customer"


async def test_fault_in_200_is_an_error(monkeypatch):
    fault = {
        "Fault": {
            "Error": [
                {
                    "Message": "Stale Object Error",
                    "Detail": "You and Jane were working on this at the same time.",
                    "code": "5010",
                }
            ],
            "type": "ValidationFault",
        }
    }
    rec = Recorder({"POST customer": fault})
    result = await _call(
        monkeypatch, rec, "qbo_customers_update", {"customerId": "1", "SyncToken": "0", "GivenName": "Ann"}
    )
    assert result["error"]["code"] == "invalid_argument"
    assert "Stale Object Error" in result["error"]["message"] and "5010" in result["error"]["message"]


async def test_http_fault_and_401(monkeypatch):
    fault = {
        "Fault": {
            "Error": [
                {
                    "Message": "Object Not Found",
                    "Detail": "Object Not Found : Something you're trying to use has been made inactive or deleted.",
                    "code": "610",
                }
            ]
        }
    }
    rec = Recorder(
        {
            "GET invoice/9": lambda r: httpx.Response(400, json=fault),
            "GET companyinfo/123": lambda r: httpx.Response(401, text=""),
        }
    )
    missing = await _call(monkeypatch, rec, "qbo_invoices_get", {"invoiceId": "9"})
    assert missing["error"]["code"] == "invalid_argument" and "Object Not Found" in missing["error"]["message"]
    expired = await _call(monkeypatch, rec, "qbo_status", {})
    assert expired["error"]["code"] == "unauthorized" and "reconnect" in expired["error"]["message"]


async def test_queries_retry_but_writes_do_not():
    replies = iter([httpx.Response(503), httpx.Response(200, json={"QueryResponse": {}})])
    rec = Recorder({"POST query": lambda r: next(replies), "POST invoice": lambda r: httpx.Response(500)})
    client = QboClient(CREDS, httpx.AsyncClient(transport=httpx.MockTransport(rec)))
    await client.query("SELECT * FROM Invoice")
    with pytest.raises(QboError):
        await client.post("invoice", {"Line": []})
    assert len(rec.calls("query")) == 2 and len(rec.calls("invoice")) == 1


# ---- table-driven entity tools --------------------------------------------


async def test_list_with_date_range_and_paging(monkeypatch):
    rec = Recorder(
        {"query": {"QueryResponse": {"Payment": [{"Id": "1"}, {"Id": "2"}], "startPosition": 11, "maxResults": 2}}}
    )
    result = await _call(
        monkeypatch,
        rec,
        "qbo_payments_list",
        {"startPosition": 11, "maxResults": 2, "startDate": "2026-01-01", "endDate": "2026-01-31"},
    )
    assert (
        sql_of(rec.requests[0])
        == "SELECT * FROM Payment WHERE TxnDate >= '2026-01-01' AND TxnDate <= '2026-01-31' STARTPOSITION 11 MAXRESULTS 2"
    )
    assert result == {
        "items": [{"Id": "1"}, {"Id": "2"}],
        "count": 2,
        "startPosition": 11,
        "maxResults": 2,
        "has_more": True,
        "next_startPosition": 13,
    }


async def test_bad_dates_are_rejected(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "qbo_bills_list", {"startDate": "01/02/2026"})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_search_escapes_quotes_with_backslash(monkeypatch):
    rec = Recorder({"query": {"QueryResponse": {}}})
    result = await _call(monkeypatch, rec, "qbo_customers_search", {"term": "O'Brien"})
    assert (
        sql_of(rec.requests[0])
        == "SELECT * FROM Customer WHERE DisplayName LIKE '%O\\'Brien%' STARTPOSITION 1 MAXRESULTS 50"
    )
    assert result["items"] == [] and result["has_more"] is False


async def test_get_unwraps_entity(monkeypatch):
    rec = Recorder({"GET journalentry/7": {"JournalEntry": {"Id": "7", "SyncToken": "2"}, "time": "x"}})
    assert await _call(monkeypatch, rec, "qbo_journal_entries_get", {"journalEntryId": "7"}) == {
        "Id": "7",
        "SyncToken": "2",
    }


async def test_ids_are_validated(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "qbo_vendors_get", {"vendorId": "1/../2"})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_create_sends_only_given_fields(monkeypatch):
    rec = Recorder({"POST invoice": {"Invoice": {"Id": "130"}}})
    args = {
        "CustomerRef": {"value": "1"},
        "Line": [
            {"Amount": 100, "DetailType": "SalesItemLineDetail", "SalesItemLineDetail": {"ItemRef": {"value": "2"}}}
        ],
    }
    result = await _call(monkeypatch, rec, "qbo_invoices_create", args)
    assert body(rec.calls("invoice", "POST")[0]) == args
    assert result == {"Id": "130"}


async def test_update_is_sparse_with_sync_token(monkeypatch):
    rec = Recorder({"POST customer": {"Customer": {"Id": "1", "SyncToken": "3"}}})
    await _call(monkeypatch, rec, "qbo_customers_update", {"customerId": "1", "SyncToken": "2", "Active": False})
    assert body(rec.calls("customer", "POST")[0]) == {"Id": "1", "SyncToken": "2", "sparse": True, "Active": False}
    empty = await _call(monkeypatch, Recorder(), "qbo_customers_update", {"customerId": "1", "SyncToken": "2"})
    assert empty["error"]["code"] == "invalid_argument"


async def test_void_styles(monkeypatch):
    rec = Recorder({"POST invoice": {"Invoice": {"Id": "5"}}, "POST payment": {"Payment": {"Id": "6"}}})
    await _call(monkeypatch, rec, "qbo_invoices_void", {"invoiceId": "5", "SyncToken": "1"})
    await _call(monkeypatch, rec, "qbo_payments_void", {"paymentId": "6", "SyncToken": "0"})
    invoice, payment = rec.calls("invoice", "POST")[0], rec.calls("payment", "POST")[0]
    assert invoice.url.params["operation"] == "void" and "include" not in invoice.url.params
    assert (payment.url.params["operation"], payment.url.params["include"]) == ("update", "void")
    assert body(payment) == {"Id": "6", "SyncToken": "0", "sparse": True}


async def test_company_info_paths(monkeypatch):
    rec = Recorder({"GET companyinfo/123": {"CompanyInfo": {"CompanyName": "Example Co", "Country": "US"}}})
    status = await _call(monkeypatch, rec, "qbo_status", {})
    assert (
        status["companyName"] == "Example Co" and status["realmId"] == "123" and status["environment"] == "production"
    )
    await _call(monkeypatch, rec, "qbo_company_info_list", {})
    assert sql_of(rec.calls("query")[0]).startswith("SELECT * FROM CompanyInfo")


# ---- invoices, attachments, expenses, reports ------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        ("Paid", "Balance = '0'"),
        ("Unpaid", "Balance > '0'"),
        ("Overdue", f"Balance > '0' AND DueDate < '{date.today().isoformat()}'"),
    ],
)
async def test_invoice_list_status(monkeypatch, status, expected):
    rec = Recorder()
    await _call(monkeypatch, rec, "qbo_invoices_list", {"status": status})
    assert (
        sql_of(rec.requests[0])
        == f"SELECT * FROM Invoice WHERE {expected} ORDERBY TxnDate DESC STARTPOSITION 1 MAXRESULTS 50"
    )


async def test_invoice_send(monkeypatch):
    rec = Recorder(
        {
            "POST invoice/130/send": {
                "Invoice": {
                    "Id": "130",
                    "DocNumber": "1037",
                    "EmailStatus": "EmailSent",
                    "BillEmail": {"Address": "a@example.com"},
                }
            }
        }
    )
    result = await _call(monkeypatch, rec, "qbo_invoices_send", {"invoiceId": "130", "email": "billing@example.com"})
    request = rec.requests[0]
    assert request.url.params["sendTo"] == "billing@example.com"
    assert request.headers["Content-Type"] == "application/octet-stream"
    assert result == {
        "status": "sent",
        "invoiceId": "130",
        "sentTo": "billing@example.com",
        "EmailStatus": "EmailSent",
        "DocNumber": "1037",
    }
    bad = await _call(monkeypatch, Recorder(), "qbo_invoices_send", {"invoiceId": "130", "email": "not-an-email"})
    assert bad["error"]["code"] == "invalid_argument"


async def test_attachment_upload_multipart(monkeypatch):
    rec = Recorder({"POST upload": {"AttachableResponse": [{"Attachable": {"Id": "50", "FileName": "r.pdf"}}]}})
    args = {
        "fileName": "r.pdf",
        "contentType": "application/pdf",
        "base64Data": base64.b64encode(b"%PDF-1.4 test").decode(),
        "attachToType": "Invoice",
        "attachToId": "130",
        "includeOnSend": True,
    }
    result = await _call(monkeypatch, rec, "qbo_attachables_upload", args)
    request = rec.requests[0]
    assert request.headers["Content-Type"].startswith("multipart/form-data")
    raw = request.content
    assert (
        b'name="file_metadata_01"' in raw
        and b'name="file_content_01"; filename="r.pdf"' in raw
        and b"%PDF-1.4 test" in raw
    )
    assert b'"EntityRef": {"type": "Invoice", "value": "130"}' in raw and b'"IncludeOnSend": true' in raw
    assert result == {"Id": "50", "FileName": "r.pdf"}


@pytest.mark.parametrize(
    "args",
    [
        {"fileName": "a", "contentType": "text/plain", "base64Data": "***"},
        {"fileName": "a", "contentType": "text/plain", "base64Data": "aGk=", "attachToType": "Invoice"},
    ],
)
async def test_bad_uploads_are_rejected(monkeypatch, args):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "qbo_attachables_upload", args)
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_expenses(monkeypatch):
    rec = Recorder({"GET purchase/4": {"Purchase": {"Id": "4"}}})
    await _call(monkeypatch, rec, "qbo_expenses_list_purchases", {"startDate": "2026-02-01"})
    assert (
        sql_of(rec.calls("query")[0])
        == "SELECT * FROM Purchase WHERE TxnDate >= '2026-02-01' ORDERBY TxnDate DESC STARTPOSITION 1 MAXRESULTS 50"
    )
    assert await _call(monkeypatch, rec, "qbo_expenses_get_purchase", {"purchaseId": "4"}) == {"Id": "4"}


async def test_reports(monkeypatch):
    report = {"Header": {"ReportName": "ProfitAndLoss"}, "Rows": {}}
    rec = Recorder(
        {"GET reports/ProfitAndLoss": report, "GET reports/AgedReceivables": report, "GET reports/CashFlow": report}
    )
    assert (
        await _call(
            monkeypatch,
            rec,
            "qbo_reports_profit_and_loss",
            {"start_date": "2026-01-01", "end_date": "2026-03-31", "accounting_method": "Cash"},
        )
        == report
    )
    params = rec.calls("reports/ProfitAndLoss")[0].url.params
    assert (params["start_date"], params["end_date"], params["accounting_method"]) == (
        "2026-01-01",
        "2026-03-31",
        "Cash",
    )
    await _call(monkeypatch, rec, "qbo_reports_aged_receivables", {})
    assert "report_date" not in rec.calls("reports/AgedReceivables")[0].url.params
    reversed_range = await _call(
        monkeypatch, rec, "qbo_reports_cash_flow", {"start_date": "2026-03-01", "end_date": "2026-01-01"}
    )
    assert reversed_range["error"]["code"] == "invalid_argument"
