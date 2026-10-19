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
"""Expense views: purchases (checks, cash and card expenses) and bills."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import QboClient
from ._common import (
    READ_ONLY,
    ClientFactory,
    EndDate,
    MaxResults,
    StartDate,
    StartPosition,
    date_conditions,
    record_id,
    run_query,
    run_tool,
    select,
    unwrap,
)


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def qbo_expenses_list_purchases(
        startPosition: StartPosition = None,
        maxResults: MaxResults = None,
        startDate: StartDate = None,
        endDate: EndDate = None,
    ) -> str:
        """List expenses paid by cash, check or card (Purchase records), newest first."""

        async def action(client: QboClient) -> dict:
            sql = select(
                "Purchase", date_conditions(startDate, endDate), startPosition, maxResults, order="TxnDate DESC"
            )
            return await run_query(client, "Purchase", sql, startPosition, maxResults)

        return await run_tool("qbo_expenses_list_purchases", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def qbo_expenses_list_bills(
        startPosition: StartPosition = None,
        maxResults: MaxResults = None,
        startDate: StartDate = None,
        endDate: EndDate = None,
    ) -> str:
        """List vendor bills, newest first, with due date and open balance."""

        async def action(client: QboClient) -> dict:
            sql = select("Bill", date_conditions(startDate, endDate), startPosition, maxResults, order="TxnDate DESC")
            return await run_query(client, "Bill", sql, startPosition, maxResults)

        return await run_tool("qbo_expenses_list_bills", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def qbo_expenses_get_purchase(
        purchaseId: Annotated[str, Field(description="Purchase id.", min_length=1)],
    ) -> str:
        """Get one expense (Purchase): payee, account, payment type, lines and total."""

        async def action(client: QboClient) -> Any:
            return unwrap(await client.get(f"purchase/{record_id(purchaseId, 'purchaseId')}"), "Purchase")

        return await run_tool("qbo_expenses_get_purchase", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def qbo_expenses_get_bill(billId: Annotated[str, Field(description="Bill id.", min_length=1)]) -> str:
        """Get one vendor bill: vendor, lines, due date, total and balance."""

        async def action(client: QboClient) -> Any:
            return unwrap(await client.get(f"bill/{record_id(billId, 'billId')}"), "Bill")

        return await run_tool("qbo_expenses_get_bill", client_factory, action)
