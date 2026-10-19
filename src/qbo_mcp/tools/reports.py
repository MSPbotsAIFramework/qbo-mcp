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
"""Financial reports (GET /reports/{name})."""

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import QboClient
from ._common import READ_ONLY, ClientFactory, compact, qbo_date, run_tool

StartDateReq = Annotated[str, Field(description="Report start date (YYYY-MM-DD).")]
EndDateReq = Annotated[str, Field(description="Report end date (YYYY-MM-DD).")]
Method = Annotated[
    Literal["Cash", "Accrual"] | None, Field(description="Accounting basis (default: the company setting).")
]
AsOf = Annotated[str | None, Field(description="Report as-of date (YYYY-MM-DD, default today).")]


async def _report(client: QboClient, name: str, params: dict[str, Any]) -> Any:
    return await client.get(f"reports/{name}", compact(params))


def _range(start_date: str, end_date: str) -> dict[str, str]:
    start, end = qbo_date(start_date, "start_date"), qbo_date(end_date, "end_date")
    if not start or not end:
        raise ValueError("start_date and end_date are required (YYYY-MM-DD).")
    if start > end:
        raise ValueError("start_date must not be after end_date.")
    return {"start_date": start, "end_date": end}


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    def dated(tool_name: str, report: str, summary: str, with_method: bool) -> None:
        if with_method:

            async def tool(start_date: StartDateReq, end_date: EndDateReq, accounting_method: Method = None) -> str:
                async def action(client: QboClient) -> Any:
                    return await _report(
                        client, report, _range(start_date, end_date) | {"accounting_method": accounting_method}
                    )

                return await run_tool(tool_name, client_factory, action)

        else:

            async def tool(start_date: StartDateReq, end_date: EndDateReq) -> str:  # type: ignore[misc]
                async def action(client: QboClient) -> Any:
                    return await _report(client, report, _range(start_date, end_date))

                return await run_tool(tool_name, client_factory, action)

        tool.__name__ = tool_name
        mcp.tool(name=tool_name, description=summary, annotations=READ_ONLY)(tool)

    def as_of(tool_name: str, report: str, summary: str) -> None:
        async def tool(report_date: AsOf = None) -> str:
            async def action(client: QboClient) -> Any:
                return await _report(client, report, {"report_date": qbo_date(report_date, "report_date")})

            return await run_tool(tool_name, client_factory, action)

        tool.__name__ = tool_name
        mcp.tool(name=tool_name, description=summary, annotations=READ_ONLY)(tool)

    dated("qbo_reports_profit_and_loss", "ProfitAndLoss", "Profit and loss (income statement) for a date range.", True)
    dated(
        "qbo_reports_balance_sheet",
        "BalanceSheet",
        "Balance sheet for a date range (assets, liabilities, equity).",
        True,
    )
    dated("qbo_reports_cash_flow", "CashFlow", "Statement of cash flows for a date range.", False)
    dated(
        "qbo_reports_trial_balance",
        "TrialBalance",
        "Trial balance (debit / credit per account) for a date range.",
        True,
    )
    dated(
        "qbo_reports_general_ledger", "GeneralLedger", "General ledger transactions per account for a date range.", True
    )
    dated("qbo_reports_customer_sales", "CustomerSales", "Sales totals per customer for a date range.", False)
    dated("qbo_reports_vendor_expenses", "VendorExpenses", "Expense totals per vendor for a date range.", False)
    as_of("qbo_reports_aged_receivables", "AgedReceivables", "Accounts receivable aging per customer as of a date.")
    as_of("qbo_reports_aged_payables", "AgedPayables", "Accounts payable aging per vendor as of a date.")
    as_of("qbo_reports_customer_balance", "CustomerBalance", "Open balance per customer as of a date.")
