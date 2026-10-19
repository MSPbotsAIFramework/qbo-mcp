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
"""FastMCP factory and the per-request credential middleware."""

import contextvars
import re
import uuid
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_client import Environment, QboClient, QboCredentials
from .config import Settings

HEADER_ACCESS_TOKEN = "X-Qbo-Access-Token"
HEADER_REALM_ID = "X-Qbo-Realm-Id"
HEADER_ENVIRONMENT = "X-Qbo-Environment"
REQUIRED_HEADERS = [HEADER_ACCESS_TOKEN, HEADER_REALM_ID]

_PLACEHOLDER = re.compile(r"^\$\{.*\}$")
_BEARER = re.compile(r"^bearer(\s+|$)", re.IGNORECASE)
_REALM = re.compile(r"^\d{1,32}$")
_ENVIRONMENTS: dict[str, Environment] = {
    "production": "production",
    "prod": "production",
    "sandbox": "sandbox",
    "sbx": "sandbox",
}

# Per-request isolation: the middleware sets these for the duration of one
# HTTP request and resets them in `finally`. Nothing credential-related is
# ever stored at module level.
_credentials_var: contextvars.ContextVar[QboCredentials | None] = contextvars.ContextVar(
    "qbo_credentials", default=None
)
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("qbo_request_id", default="-")

INSTRUCTIONS = (
    "QuickBooks Online is small-business accounting software. This server "
    "works with one QuickBooks company: customers, vendors, items, accounts, "
    "invoices, estimates, payments, bills, expenses (purchases), journal "
    "entries, time activities, attachments and financial reports.\n"
    "Usage:\n"
    "- Tools are named qbo_<records>_<action>; qbo_navigate(domain) lists an "
    "area's tools.\n"
    '- Records reference each other with {"value": "<id>"} objects, e.g. '
    "CustomerRef, VendorRef, ItemRef, AccountRef. Find ids with *_search or "
    "*_list first.\n"
    "- Lists page with startPosition (from 1) and maxResults (default 50, max "
    "200); transaction lists take startDate / endDate (YYYY-MM-DD).\n"
    "- Updates are sparse but need the record's current SyncToken: call *_get "
    "first. Sending Line replaces all lines.\n"
    "- Reports (qbo_reports_*) return QuickBooks report JSON for a date range.\n"
    "Creating, updating, voiding, emailing invoices and uploading files change "
    "the company's books or contact customers: call those tools only when "
    "the user explicitly asks, with the exact values given."
)


def get_client_from_context() -> QboClient | None:
    """The client for the current request, or None outside a request."""
    credentials = _credentials_var.get()
    if credentials is None:
        return None
    return QboClient(credentials)


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or _PLACEHOLDER.match(text):
        return None
    return text


class GatewayCredentialsMiddleware:
    """Reads the QuickBooks headers on /mcp requests into the request context.

    Needs X-Qbo-Access-Token and X-Qbo-Realm-Id; otherwise 401.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        token = _clean(request.headers.get(HEADER_ACCESS_TOKEN))
        if token:
            token = _clean(_BEARER.sub("", token))
        realm = _clean(request.headers.get(HEADER_REALM_ID))
        missing = [n for n, v in ((HEADER_ACCESS_TOKEN, token), (HEADER_REALM_ID, realm)) if not v]
        if missing:
            await JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": f"Missing header(s): {', '.join(missing)}",
                    "required_headers": REQUIRED_HEADERS,
                    "optional_headers": [HEADER_ENVIRONMENT],
                },
                status_code=401,
            )(scope, receive, send)
            return

        env_raw = (_clean(request.headers.get(HEADER_ENVIRONMENT)) or "production").lower()
        environment = _ENVIRONMENTS.get(env_raw)
        problem = None
        if not _REALM.match(realm or ""):
            problem = f"{HEADER_REALM_ID} must be the numeric QuickBooks company (realm) id"
        elif environment is None:
            problem = f"{HEADER_ENVIRONMENT} must be production or sandbox"
        if problem:
            await JSONResponse({"error": "Invalid header", "message": problem}, status_code=400)(scope, receive, send)
            return

        credentials = QboCredentials(
            access_token=token or "", realm_id=realm or "", environment=environment or "production"
        )
        request_id = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:12]
        creds_token = _credentials_var.set(credentials)
        rid_token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(creds_token)
            request_id_var.reset(rid_token)


def create_mcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server and register every tool."""
    mcp = FastMCP(
        name="qbo-mcp",
        instructions=INSTRUCTIONS,
        # The service sits behind the gateway, so Host is never localhost;
        # DNS-rebinding protection would reject those requests with 421.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
    )

    client_factory: Callable[[], QboClient | None] = get_client_from_context

    from .tools import entities, expenses, extras, navigation, reports

    for module in (navigation, entities, extras, expenses, reports):
        module.register(mcp, client_factory)

    return mcp
