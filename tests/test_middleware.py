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
"""Credential middleware: 401 on missing headers, bad values reported by tools, isolation."""

import asyncio

import pytest
from starlette.testclient import TestClient

from qbo_mcp.__main__ import build_http_app
from qbo_mcp.server import GatewayCredentialsMiddleware, _credentials_var, create_mcp_server, get_client_from_context

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
CREDS = {"X-Qbo-Access-Token": "access-token", "X-Qbo-Realm-Id": "9130350000000001"}


def _client() -> TestClient:
    return TestClient(build_http_app(create_mcp_server()))


def test_health_is_local():
    with _client() as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Qbo-Realm-Id": "123"},
        {"X-Qbo-Access-Token": "t"},
        CREDS | {"X-Qbo-Access-Token": "Bearer "},
        CREDS | {"X-Qbo-Access-Token": "${QBO_ACCESS_TOKEN}"},
    ],
)
def test_missing_credentials_return_401(headers):
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | headers)
    assert resp.status_code == 401
    assert resp.json()["required_headers"] == ["X-Qbo-Access-Token", "X-Qbo-Realm-Id"]


def test_placeholder_headers_still_list_tools():
    # The admin console refreshes the tool catalog with placeholder header values.
    headers = {"X-Qbo-Access-Token": "placeholder", "X-Qbo-Realm-Id": "placeholder", "X-Qbo-Environment": "placeholder"}
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | headers)
    assert resp.status_code == 200
    assert "qbo_accounts_list" in resp.text


@pytest.mark.parametrize(
    "extra,message",
    [
        ({"X-Qbo-Realm-Id": "../company"}, "X-Qbo-Realm-Id must be the numeric QuickBooks company (realm) id"),
        ({"X-Qbo-Environment": "staging"}, "X-Qbo-Environment must be production or sandbox"),
    ],
)
def test_bad_values_are_reported_by_tools(extra, message):
    with _client() as client:
        listed = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS | extra)
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "qbo_accounts_list", "arguments": {}}}
        resp = client.post("/mcp", json=call, headers=MCP_HEADERS | CREDS | extra)
    assert listed.status_code == 200
    assert resp.status_code == 200
    assert '\\"invalid_argument\\"' in resp.text
    assert message in resp.text


def test_tools_list_over_http():
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS)
    assert resp.status_code == 200
    for name in ("qbo_invoices_create", "qbo_reports_profit_and_loss", "qbo_attachables_upload", "qbo_navigate"):
        assert name in resp.text


def _run_middleware(headers: list[tuple[bytes, bytes]]) -> dict:
    seen: dict = {}

    async def app(scope, receive, send):
        seen["credentials"] = _credentials_var.get()
        seen["client"] = get_client_from_context()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    asyncio.run(GatewayCredentialsMiddleware(app)({"type": "http", "path": "/mcp", "headers": headers}, receive, send))
    return seen


@pytest.mark.parametrize(
    "env,host",
    [
        (None, "https://quickbooks.api.intuit.com"),
        (b"production", "https://quickbooks.api.intuit.com"),
        (b"Sandbox", "https://sandbox-quickbooks.api.intuit.com"),
        (b"sbx", "https://sandbox-quickbooks.api.intuit.com"),
        (b"prod", "https://quickbooks.api.intuit.com"),
    ],
)
def test_environment_selects_host(env, host):
    headers = [(b"x-qbo-access-token", b"Bearer tok-1"), (b"x-qbo-realm-id", b"123")]
    if env is not None:
        headers.append((b"x-qbo-environment", env))
    seen = _run_middleware(headers)
    assert seen["credentials"].access_token == "tok-1"
    assert seen["client"].base_url == f"{host}/v3/company/123"
    assert _credentials_var.get() is None


def test_concurrent_requests_do_not_share_credentials():
    observed: list[tuple[str, str]] = []

    async def app(scope, receive, send):
        before = _credentials_var.get().realm_id
        await asyncio.sleep(0.01)
        observed.append((before, _credentials_var.get().realm_id))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    middleware = GatewayCredentialsMiddleware(app)

    def scope(realm: bytes) -> dict:
        return {"type": "http", "path": "/mcp", "headers": [(b"x-qbo-access-token", b"t"), (b"x-qbo-realm-id", realm)]}

    async def both():
        await asyncio.gather(middleware(scope(b"111"), receive, send), middleware(scope(b"222"), receive, send))

    asyncio.run(both())
    assert sorted(observed) == [("111", "111"), ("222", "222")]


def test_credentials_repr_hides_token():
    seen = _run_middleware([(b"x-qbo-access-token", b"very-secret-value"), (b"x-qbo-realm-id", b"123")])
    assert "very-secret-value" not in repr(seen["credentials"])
