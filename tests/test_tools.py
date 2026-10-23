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
"""tools/list snapshot and error-envelope mapping. No network access."""

import asyncio
import json

import pytest

from qbo_mcp.api_client import QboError
from qbo_mcp.server import INSTRUCTIONS, create_mcp_server
from qbo_mcp.tools.navigation import DOMAINS

# Tool names and required parameters are a public contract: existing agents
# and tenant connections depend on them. Changing this snapshot is a breaking
# change.
EXPECTED_TOOLS: dict[str, set[str]] = {
    "qbo_accounts_create": {"Name"},
    "qbo_accounts_get": {"accountId"},
    "qbo_accounts_list": set(),
    "qbo_accounts_search": {"term"},
    "qbo_accounts_update": {"accountId", "SyncToken", "Name"},
    "qbo_attachables_create": set(),
    "qbo_attachables_get": {"attachableId"},
    "qbo_attachables_list": set(),
    "qbo_attachables_update": {"attachableId", "SyncToken"},
    "qbo_attachables_upload": {"fileName", "contentType", "base64Data"},
    "qbo_bill_payments_create": {"TotalAmt", "PayType", "Line", "VendorRef"},
    "qbo_bill_payments_get": {"billPaymentId"},
    "qbo_bill_payments_list": set(),
    "qbo_bill_payments_update": {"TotalAmt", "billPaymentId", "VendorRef", "Line", "PayType", "SyncToken"},
    "qbo_bills_create": {"Line", "VendorRef"},
    "qbo_bills_get": {"billId"},
    "qbo_bills_list": set(),
    "qbo_bills_search": {"term"},
    "qbo_bills_update": {"Line", "SyncToken", "billId", "VendorRef"},
    "qbo_classes_create": {"Name"},
    "qbo_classes_get": {"classId"},
    "qbo_classes_list": set(),
    "qbo_classes_search": {"term"},
    "qbo_classes_update": {"classId", "SyncToken", "Name"},
    "qbo_company_info_get": {"companyInfoId"},
    "qbo_company_info_list": set(),
    "qbo_credit_memos_create": {"Line", "CustomerRef"},
    "qbo_credit_memos_get": {"creditMemoId"},
    "qbo_credit_memos_list": set(),
    "qbo_credit_memos_update": {"Line", "SyncToken", "CustomerRef", "creditMemoId"},
    "qbo_customers_create": {"DisplayName"},
    "qbo_customers_get": {"customerId"},
    "qbo_customers_list": set(),
    "qbo_customers_search": {"term"},
    "qbo_customers_update": {"SyncToken", "customerId"},
    "qbo_departments_create": {"Name"},
    "qbo_departments_get": {"departmentId"},
    "qbo_departments_list": set(),
    "qbo_departments_search": {"term"},
    "qbo_departments_update": {"SyncToken", "departmentId", "Name"},
    "qbo_deposits_create": {"DepositToAccountRef", "Line"},
    "qbo_deposits_get": {"depositId"},
    "qbo_deposits_list": set(),
    "qbo_deposits_update": {"DepositToAccountRef", "Line", "SyncToken", "depositId"},
    "qbo_employees_create": {"DisplayName"},
    "qbo_employees_get": {"employeeId"},
    "qbo_employees_list": set(),
    "qbo_employees_search": {"term"},
    "qbo_employees_update": {"DisplayName", "SyncToken", "employeeId"},
    "qbo_estimates_create": {"Line", "CustomerRef"},
    "qbo_estimates_get": {"estimateId"},
    "qbo_estimates_list": set(),
    "qbo_estimates_update": {"Line", "SyncToken", "CustomerRef", "estimateId"},
    "qbo_expenses_get_bill": {"billId"},
    "qbo_expenses_get_purchase": {"purchaseId"},
    "qbo_expenses_list_bills": set(),
    "qbo_expenses_list_purchases": set(),
    "qbo_invoices_create": {"Line", "CustomerRef"},
    "qbo_invoices_get": {"invoiceId"},
    "qbo_invoices_list": set(),
    "qbo_invoices_send": {"invoiceId"},
    "qbo_invoices_update": {"invoiceId", "SyncToken"},
    "qbo_invoices_void": {"invoiceId", "SyncToken"},
    "qbo_items_create": {"Name"},
    "qbo_items_get": {"itemId"},
    "qbo_items_list": set(),
    "qbo_items_search": {"term"},
    "qbo_items_update": {"itemId", "SyncToken", "Name"},
    "qbo_journal_entries_create": {"Line"},
    "qbo_journal_entries_get": {"journalEntryId"},
    "qbo_journal_entries_list": set(),
    "qbo_journal_entries_update": {"journalEntryId", "Line", "SyncToken"},
    "qbo_navigate": {"domain"},
    "qbo_payment_methods_create": {"Name"},
    "qbo_payment_methods_get": {"paymentMethodId"},
    "qbo_payment_methods_list": set(),
    "qbo_payment_methods_search": {"term"},
    "qbo_payment_methods_update": {"paymentMethodId", "SyncToken", "Name"},
    "qbo_payments_create": {"TotalAmt", "CustomerRef"},
    "qbo_payments_get": {"paymentId"},
    "qbo_payments_list": set(),
    "qbo_payments_update": {"paymentId", "SyncToken"},
    "qbo_payments_void": {"paymentId", "SyncToken"},
    "qbo_purchase_orders_create": {"Line", "VendorRef"},
    "qbo_purchase_orders_get": {"purchaseOrderId"},
    "qbo_purchase_orders_list": set(),
    "qbo_purchase_orders_update": {"Line", "SyncToken", "purchaseOrderId", "VendorRef"},
    "qbo_purchases_create": {"AccountRef", "Line", "PaymentType"},
    "qbo_purchases_get": {"purchaseId"},
    "qbo_purchases_list": set(),
    "qbo_purchases_update": {"purchaseId", "AccountRef", "Line", "PaymentType", "SyncToken"},
    "qbo_refund_receipts_create": {"DepositToAccountRef", "Line", "CustomerRef"},
    "qbo_refund_receipts_get": {"refundReceiptId"},
    "qbo_refund_receipts_list": set(),
    "qbo_refund_receipts_update": {"DepositToAccountRef", "refundReceiptId", "Line", "CustomerRef", "SyncToken"},
    "qbo_reports_aged_payables": set(),
    "qbo_reports_aged_receivables": set(),
    "qbo_reports_balance_sheet": {"start_date", "end_date"},
    "qbo_reports_cash_flow": {"start_date", "end_date"},
    "qbo_reports_customer_balance": set(),
    "qbo_reports_customer_sales": {"start_date", "end_date"},
    "qbo_reports_general_ledger": {"start_date", "end_date"},
    "qbo_reports_profit_and_loss": {"start_date", "end_date"},
    "qbo_reports_trial_balance": {"start_date", "end_date"},
    "qbo_reports_vendor_expenses": {"start_date", "end_date"},
    "qbo_sales_receipts_create": {"Line", "CustomerRef"},
    "qbo_sales_receipts_get": {"salesReceiptId"},
    "qbo_sales_receipts_list": set(),
    "qbo_sales_receipts_update": {"Line", "SyncToken", "salesReceiptId", "CustomerRef"},
    "qbo_status": set(),
    "qbo_tax_codes_get": {"taxCodeId"},
    "qbo_tax_codes_list": set(),
    "qbo_tax_codes_search": {"term"},
    "qbo_tax_rates_get": {"taxRateId"},
    "qbo_tax_rates_list": set(),
    "qbo_tax_rates_search": {"term"},
    "qbo_terms_create": {"Name"},
    "qbo_terms_get": {"termId"},
    "qbo_terms_list": set(),
    "qbo_terms_search": {"term"},
    "qbo_terms_update": {"termId", "SyncToken", "Name"},
    "qbo_time_activities_create": {"TxnDate", "NameOf"},
    "qbo_time_activities_get": {"timeActivityId"},
    "qbo_time_activities_list": set(),
    "qbo_time_activities_update": {"TxnDate", "SyncToken", "NameOf", "timeActivityId"},
    "qbo_transfers_create": {"ToAccountRef", "Amount", "FromAccountRef"},
    "qbo_transfers_get": {"transferId"},
    "qbo_transfers_list": set(),
    "qbo_transfers_update": {"ToAccountRef", "FromAccountRef", "transferId", "SyncToken", "Amount"},
    "qbo_vendor_credits_create": {"Line", "VendorRef"},
    "qbo_vendor_credits_get": {"vendorCreditId"},
    "qbo_vendor_credits_list": set(),
    "qbo_vendor_credits_update": {"Line", "SyncToken", "VendorRef", "vendorCreditId"},
    "qbo_vendors_create": {"DisplayName"},
    "qbo_vendors_get": {"vendorId"},
    "qbo_vendors_list": set(),
    "qbo_vendors_search": {"term"},
    "qbo_vendors_update": {"DisplayName", "vendorId", "SyncToken"},
}

READ_SUFFIXES = ("_list", "_get", "_search", "_list_purchases", "_list_bills", "_get_purchase", "_get_bill")


def is_read(name: str) -> bool:
    return name.startswith("qbo_reports_") or name in ("qbo_navigate", "qbo_status") or name.endswith(READ_SUFFIXES)


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(create_mcp_server().list_tools())}


def test_tool_names_are_stable(tools):
    assert set(tools) == set(EXPECTED_TOOLS)
    assert len(tools) == 138


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_required_params(tools, name):
    assert set(tools[name].inputSchema.get("required", [])) == EXPECTED_TOOLS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_description_budget(tools, name):
    description = (tools[name].description or "").strip()
    assert description, f"{name} has no description"
    assert len(description) <= 500
    assert len(description.splitlines()[0]) <= 100


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_annotations_match_side_effects(tools, name):
    annotations = tools[name].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is is_read(name)
    assert bool(annotations.destructiveHint) is name.endswith("_void")


@pytest.mark.parametrize("name", sorted(n for n in EXPECTED_TOOLS if not is_read(n)))
def test_write_tools_say_when_to_use_them(tools, name):
    assert "explicitly asks" in (tools[name].description or "")


def test_every_tool_is_in_one_navigation_domain(tools):
    listed = [t for names in DOMAINS.values() for t in names]
    assert len(listed) == len(set(listed))
    assert set(listed) | {"qbo_navigate", "qbo_status"} == set(tools)


def test_update_tools_take_sync_token(tools):
    for name, tool in tools.items():
        if name.endswith(("_update", "_void")):
            assert "SyncToken" in tool.inputSchema["required"], name


def test_max_results_is_capped_at_200(tools):
    paged = [t for t in tools.values() if "maxResults" in t.inputSchema["properties"]]
    assert len(paged) == 42
    for tool in paged:
        assert tool.inputSchema["properties"]["maxResults"]["anyOf"][0]["maximum"] == 200, tool.name
    content, _ = asyncio.run(create_mcp_server().call_tool("qbo_customers_list", {"maxResults": 201}))
    error = json.loads(content[0].text)["error"]
    assert error["code"] == "invalid_argument" and "less than or equal to 200" in error["message"]


def test_instructions_budget():
    assert len(INSTRUCTIONS) <= 1500


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "unauthorized", False),
        (403, "unauthorized", False),
        (404, "not_found", False),
        (400, "invalid_argument", False),
        (429, "rate_limited", True),
        (500, "upstream_error", True),
        (0, "upstream_error", True),
    ],
)
def test_error_envelope(status, code, retryable):
    envelope = json.loads(QboError(status, "boom").to_envelope())
    assert envelope == {"error": {"code": code, "message": "boom", "retryable": retryable}}


def test_tool_without_credentials_returns_not_configured():
    content, _ = asyncio.run(create_mcp_server().call_tool("qbo_status", {}))
    assert json.loads(content[0].text)["error"]["code"] == "not_configured"


def test_navigate_needs_no_credentials():
    content, _ = asyncio.run(create_mcp_server().call_tool("qbo_navigate", {"domain": "invoices"}))
    tools = json.loads(content[0].text)["tools"]
    assert "qbo_invoices_send" in tools and "qbo_invoices_void" in tools
