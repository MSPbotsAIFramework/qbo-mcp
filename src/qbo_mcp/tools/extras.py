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
"""Invoice listing with payment status, invoice email, and file upload."""

import base64
import binascii
import re
from datetime import date
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import QboClient, QboError
from ._common import (
    READ_ONLY,
    WRITES,
    ClientFactory,
    EndDate,
    MaxResults,
    StartDate,
    StartPosition,
    compact,
    date_conditions,
    record_id,
    run_query,
    run_tool,
    select,
    unwrap,
)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def qbo_invoices_list(
        startPosition: StartPosition = None,
        maxResults: MaxResults = None,
        status: Annotated[
            Literal["Paid", "Unpaid", "Overdue"] | None,
            Field(description="Paid (no balance), Unpaid (balance due) or Overdue (balance due, past DueDate)."),
        ] = None,
        startDate: StartDate = None,
        endDate: EndDate = None,
    ) -> str:
        """List invoices, newest first, optionally by payment status and TxnDate range."""

        async def action(client: QboClient) -> dict:
            conditions = date_conditions(startDate, endDate)
            if status == "Paid":
                conditions.append("Balance = '0'")
            elif status in ("Unpaid", "Overdue"):
                conditions.append("Balance > '0'")
            if status == "Overdue":
                conditions.append(f"DueDate < '{date.today().isoformat()}'")
            sql = select("Invoice", conditions, startPosition, maxResults, order="TxnDate DESC")
            return await run_query(client, "Invoice", sql, startPosition, maxResults)

        return await run_tool("qbo_invoices_list", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def qbo_invoices_send(
        invoiceId: Annotated[str, Field(description="Id of the invoice to email.", min_length=1)],
        email: Annotated[
            str | None, Field(description="Send to this address instead of the invoice's BillEmail.")
        ] = None,
    ) -> str:
        """Email an invoice to the customer (or to the given address). Only when the user explicitly asks."""

        async def action(client: QboClient) -> dict:
            rid = record_id(invoiceId, "invoiceId")
            target = (email or "").strip() or None
            if target and not _EMAIL.match(target):
                raise ValueError(f"email must be an email address (got {email!r}).")
            payload = await client.request(
                "POST",
                f"invoice/{rid}/send",
                params=compact({"sendTo": target}),
                content=b"",
                content_type="application/octet-stream",
            )
            invoice = unwrap(payload, "Invoice") or {}
            return compact(
                {
                    "status": "sent",
                    "invoiceId": rid,
                    "sentTo": target or (invoice.get("BillEmail") or {}).get("Address")
                    if isinstance(invoice, dict)
                    else target,
                    "EmailStatus": invoice.get("EmailStatus") if isinstance(invoice, dict) else None,
                    "DocNumber": invoice.get("DocNumber") if isinstance(invoice, dict) else None,
                }
            )

        return await run_tool("qbo_invoices_send", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def qbo_attachables_upload(
        fileName: Annotated[str, Field(description="File name, e.g. receipt.pdf.", min_length=1)],
        contentType: Annotated[str, Field(description="MIME type, e.g. application/pdf or image/png.", min_length=1)],
        base64Data: Annotated[str, Field(description="File contents, base64-encoded (max 100 MB).", min_length=1)],
        note: Annotated[str | None, Field(description="Note stored with the attachment.")] = None,
        category: Annotated[str | None, Field(description="Category, e.g. Document, Image, Receipt.")] = None,
        attachToType: Annotated[
            str | None, Field(description="Entity type to attach to, e.g. Invoice, Bill, Customer.")
        ] = None,
        attachToId: Annotated[str | None, Field(description="Id of that record.")] = None,
        includeOnSend: Annotated[
            bool | None, Field(description="Include the file when the invoice / estimate is emailed.")
        ] = None,
    ) -> str:
        """Upload a file and optionally attach it to a record. Only when the user explicitly asks."""

        async def action(client: QboClient) -> Any:
            if bool(attachToType) != bool(attachToId):
                raise ValueError("attachToType and attachToId go together.")
            try:
                data = base64.b64decode(base64Data, validate=True)
            except (binascii.Error, ValueError) as e:
                raise ValueError("base64Data is not valid base64.") from e
            if not data:
                raise ValueError("base64Data decodes to an empty file.")
            if len(data) > MAX_UPLOAD_BYTES:
                raise ValueError("QuickBooks accepts attachments up to 100 MB.")
            metadata: dict[str, Any] = compact(
                {"FileName": fileName, "ContentType": contentType, "Note": note, "Category": category}
            )
            if attachToType and attachToId:
                metadata["AttachableRef"] = [
                    {
                        "EntityRef": {"type": attachToType, "value": record_id(attachToId, "attachToId")},
                        "IncludeOnSend": bool(includeOnSend),
                    }
                ]
            payload = await client.upload(metadata, fileName, contentType, data) or {}
            responses = payload.get("AttachableResponse") if isinstance(payload, dict) else None
            first = responses[0] if isinstance(responses, list) and responses else {}
            if isinstance(first, dict) and isinstance(first.get("Fault"), dict):
                errors = first["Fault"].get("Error") or [{}]
                raise QboError(400, f"QuickBooks rejected the upload: {errors[0].get('Message', 'unknown error')}")
            return first.get("Attachable", payload) if isinstance(first, dict) else payload

        return await run_tool("qbo_attachables_upload", client_factory, action)
