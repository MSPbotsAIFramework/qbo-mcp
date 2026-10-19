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
"""Tool discovery by area and the connection check."""

from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .._json import compact as to_json
from ..api_client import QboClient
from ._common import LOCAL, READ_ONLY, ClientFactory, run_tool, unwrap
from .entity_table import ENTITIES

_EXTRA_TOOLS = {
    "invoices": ["qbo_invoices_list", "qbo_invoices_send"],
    "attachables": ["qbo_attachables_upload"],
}
_HANDWRITTEN = {
    "expenses": [
        "qbo_expenses_list_purchases",
        "qbo_expenses_list_bills",
        "qbo_expenses_get_purchase",
        "qbo_expenses_get_bill",
    ],
    "reports": [
        "qbo_reports_profit_and_loss",
        "qbo_reports_balance_sheet",
        "qbo_reports_cash_flow",
        "qbo_reports_trial_balance",
        "qbo_reports_general_ledger",
        "qbo_reports_customer_sales",
        "qbo_reports_vendor_expenses",
        "qbo_reports_aged_receivables",
        "qbo_reports_aged_payables",
        "qbo_reports_customer_balance",
    ],
}


def _entity_tools(e) -> list[str]:
    ops = (["list"] if e.listable else []) + ["get"]
    ops += (["create"] if e.create else []) + (["update"] if e.update else [])
    ops += (["void"] if e.void else []) + (["search"] if e.search else [])
    return [f"{e.prefix}_{op}" for op in ops] + _EXTRA_TOOLS.get(e.domain, [])


DOMAINS: dict[str, list[str]] = {e.domain: _entity_tools(e) for e in ENTITIES} | _HANDWRITTEN

Domain = Literal[
    "accounts",
    "attachables",
    "bill_payments",
    "bills",
    "classes",
    "company_info",
    "credit_memos",
    "customers",
    "departments",
    "deposits",
    "employees",
    "estimates",
    "invoices",
    "items",
    "journal_entries",
    "payment_methods",
    "payments",
    "purchase_orders",
    "purchases",
    "refund_receipts",
    "sales_receipts",
    "tax_codes",
    "tax_rates",
    "terms",
    "time_activities",
    "transfers",
    "vendor_credits",
    "vendors",
    "expenses",
    "reports",
]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=LOCAL)
    async def qbo_navigate(
        domain: Annotated[Domain, Field(description="Area to describe, e.g. invoices or reports.")],
    ) -> str:
        """List the tools of one QuickBooks area. All tools are always callable; this changes nothing."""
        return to_json({"domain": domain, "tools": DOMAINS[domain]})

    @mcp.tool(annotations=READ_ONLY)
    async def qbo_status() -> str:
        """Check the QuickBooks connection: company name, realm id, environment and tool areas."""

        async def action(client: QboClient) -> dict:
            info = unwrap(await client.get(f"companyinfo/{client.realm_id}"), "CompanyInfo") or {}
            return {
                "connected": True,
                "environment": client.environment,
                "realmId": client.realm_id,
                "companyName": info.get("CompanyName") if isinstance(info, dict) else None,
                "country": info.get("Country") if isinstance(info, dict) else None,
                "domains": sorted(DOMAINS),
            }

        return await run_tool("qbo_status", client_factory, action)
