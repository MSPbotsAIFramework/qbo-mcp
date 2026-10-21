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
"""Shared argument types, query helpers, the tool factory and the error wrapper."""

import inspect
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped, error_envelope
from ..api_client import QboClient, QboError
from ..config import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

logger = logging.getLogger("qbo_mcp.tools")

ClientFactory = Callable[[], QboClient | None]

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
WRITES_IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

NOT_CONFIGURED = error_envelope(
    "not_configured",
    "QuickBooks credentials are missing for this request (X-Qbo-Access-Token and X-Qbo-Realm-Id).",
    False,
)

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID = re.compile(r"^[A-Za-z0-9-]{1,32}$")

StartPosition = Annotated[int | None, Field(description="First result to return, from 1 (default 1).", ge=1)]
MaxResults = Annotated[
    int | None,
    Field(description=f"Results to return (default {DEFAULT_PAGE_SIZE}, max {MAX_PAGE_SIZE}).", ge=1, le=MAX_PAGE_SIZE),
]
StartDate = Annotated[str | None, Field(description="Only transactions on or after this TxnDate (YYYY-MM-DD).")]
EndDate = Annotated[str | None, Field(description="Only transactions on or before this TxnDate (YYYY-MM-DD).")]


def compact(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def record_id(value: Any, what: str) -> str:
    """QuickBooks ids are short numeric strings; they go into URL paths."""
    text = str(value).strip()
    if not _ID.match(text):
        raise ValueError(f"{what} must be a QuickBooks record id (got {value!r}).")
    return text


def qbo_date(value: str | None, what: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    if not _DATE.match(text):
        raise ValueError(f"{what} must be a date in YYYY-MM-DD format (got {value!r}).")
    return text


def quote(value: str) -> str:
    """A string literal for the QuickBooks query language (backslash escapes)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def select(entity: str, conditions: list[str], start: int | None, size: int | None, order: str | None = None) -> str:
    sql = f"SELECT * FROM {entity}"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    if order:
        sql += f" ORDERBY {order}"
    return f"{sql} STARTPOSITION {start or 1} MAXRESULTS {size or DEFAULT_PAGE_SIZE}"


def date_conditions(start_date: str | None, end_date: str | None, field: str = "TxnDate") -> list[str]:
    conditions = []
    if (start := qbo_date(start_date, "startDate")) is not None:
        conditions.append(f"{field} >= '{start}'")
    if (end := qbo_date(end_date, "endDate")) is not None:
        conditions.append(f"{field} <= '{end}'")
    return conditions


async def run_query(client: QboClient, entity: str, sql: str, start: int | None, size: int | None) -> dict:
    """Run a SELECT and return {items, count, startPosition, maxResults, has_more}."""
    payload = await client.query(sql) or {}
    response = payload.get("QueryResponse") if isinstance(payload, dict) else None
    response = response if isinstance(response, dict) else {}
    items = response.get(entity)
    if not isinstance(items, list):
        items = next((v for v in response.values() if isinstance(v, list)), [])
    first, limit = start or 1, size or DEFAULT_PAGE_SIZE
    more = len(items) >= limit
    return compact(
        {
            "items": items,
            "count": len(items),
            "startPosition": first,
            "maxResults": limit,
            "has_more": more,
            "next_startPosition": first + limit if more else None,
        }
    )


def unwrap(payload: Any, entity: str) -> Any:
    """{"Invoice": {...}, "time": ...} -> the Invoice record."""
    if isinstance(payload, dict) and isinstance(payload.get(entity), dict):
        return payload[entity]
    return payload


def add_tool(
    mcp: FastMCP,
    name: str,
    description: str,
    annotations: ToolAnnotations,
    params: list[tuple[str, Any, Any]],
    handler: Callable[[QboClient, dict[str, Any]], Awaitable[Any]],
    client_factory: ClientFactory,
) -> None:
    """Register a tool from a parameter list [(name, annotation, default)].

    Used for the table-driven entity tools, whose parameter names mirror the
    QuickBooks field names.
    """

    async def tool(**kwargs: Any) -> str:
        return await run_tool(name, client_factory, lambda client: handler(client, kwargs))

    tool.__name__ = name
    tool.__doc__ = description
    tool.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [inspect.Parameter(p, inspect.Parameter.KEYWORD_ONLY, default=d, annotation=a) for p, a, d in params],
        return_annotation=str,
    )
    mcp.tool(name=name, description=description, annotations=annotations)(tool)


async def run_tool(name: str, client_factory: ClientFactory, action: Callable[[QboClient], Awaitable[Any]]) -> str:
    """Resolve the caller's client, run ``action``, and always return a string.

    Errors never escape as exceptions: they become the standard error envelope.
    """
    client = client_factory()
    if client is None:
        return NOT_CONFIGURED
    if client.header_error:
        return error_envelope("invalid_argument", client.header_error, False)
    started = time.monotonic()
    try:
        result = await action(client)
    except QboError as e:
        logger.info("tool=%s outcome=error status=%s ms=%d", name, e.status_code, _ms(started))
        return e.to_envelope()
    except ValueError as e:
        logger.info("tool=%s outcome=invalid_argument ms=%d", name, _ms(started))
        return error_envelope("invalid_argument", str(e), False)
    except Exception as e:  # noqa: BLE001 - tools must never raise
        logger.warning("tool=%s outcome=unexpected error=%s ms=%d", name, type(e).__name__, _ms(started))
        return error_envelope("upstream_error", f"Unexpected error: {type(e).__name__}", True)
    logger.info("tool=%s outcome=ok ms=%d", name, _ms(started))
    if result is None:
        result = {"result": None, "note": "QuickBooks returned an empty response."}
    return result if isinstance(result, str) else dump_json_capped(result)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
