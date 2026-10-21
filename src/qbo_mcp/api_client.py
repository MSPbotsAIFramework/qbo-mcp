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
"""Async client for the QuickBooks Online Accounting API (v3)."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any, Literal

import httpx

from ._json import error_envelope
from .config import API_HOSTS, MINOR_VERSION

logger = logging.getLogger("qbo_mcp.api")

_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=5.0)
_MAX_RETRIES = 2
_MAX_BACKOFF_SECONDS = 20.0
# Whole-call budget including retries; the gateway times a tool call out at 120s.
_CALL_BUDGET_SECONDS = 100.0

Environment = Literal["production", "sandbox"]

# One connection pool for the process. It never holds credentials: the
# Authorization header is built per request from the caller's own token, so
# sharing the pool across tenants is safe.
_shared_http_client: httpx.AsyncClient | None = None


class _RejectAllCookies(DefaultCookiePolicy):
    """Never store or send a cookie: the HTTP client is shared by every tenant."""

    def set_ok(self, cookie, request) -> bool:  # noqa: ANN001 - stdlib signature
        return False

    def return_ok(self, cookie, request) -> bool:  # noqa: ANN001 - stdlib signature
        return False


def _no_cookies() -> CookieJar:
    # A plain CookieJar: httpx uses it as is (an httpx.Cookies would be copied into a default jar).
    return CookieJar(policy=_RejectAllCookies())


def _get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None:
        _shared_http_client = httpx.AsyncClient(cookies=_no_cookies(), timeout=_TIMEOUT)
    return _shared_http_client


# status_code -> (error code, retryable). 0 means no HTTP response at all
# (connection error or timeout).
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    422: ("invalid_argument", False),
    429: ("rate_limited", True),
}


def classify_status(status_code: int) -> tuple[str, bool]:
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    if status_code >= 400:
        return "invalid_argument", False
    return "upstream_error", True


class QboError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"QuickBooks API error {status_code}: {message}")

    def to_envelope(self) -> str:
        code, retryable = classify_status(self.status_code)
        return error_envelope(code, self.message, retryable)


@dataclass(frozen=True)
class QboCredentials:
    access_token: str
    realm_id: str
    environment: Environment = "production"

    def __repr__(self) -> str:  # never leak the token into logs or tracebacks
        return f"QboCredentials(realm_id={self.realm_id!r}, environment={self.environment!r}, access_token=<redacted>)"


class QboClient:
    """Thin wrapper over the Accounting API for one company (realm) and token."""

    def __init__(self, credentials: QboCredentials, http_client: httpx.AsyncClient | None = None):
        self._credentials = credentials
        self._http = http_client
        self.realm_id = credentials.realm_id
        self.environment = credentials.environment
        self.base_url = f"{API_HOSTS[credentials.environment]}/v3/company/{credentials.realm_id}"

    def _client(self) -> httpx.AsyncClient:
        return self._http or _get_shared_http_client()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, body: Any = None, params: dict[str, Any] | None = None) -> Any:
        return await self.request("POST", path, params=params, json_body=body)

    async def query(self, sql: str) -> Any:
        """Run a query (SELECT ...). A read, so it is retried like a GET."""
        return await self.request(
            "POST", "query", content=sql.encode("utf-8"), content_type="application/text", read=True
        )

    async def upload(self, metadata: dict[str, Any], file_name: str, content_type: str, data: bytes) -> Any:
        files = {
            "file_metadata_01": (None, json.dumps(metadata).encode("utf-8"), "application/json"),
            "file_content_01": (file_name, data, content_type),
        }
        return await self.request("POST", "upload", files=files)

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        content_type: str | None = None,
        files: dict[str, Any] | None = None,
        read: bool = False,
    ) -> Any:
        url = f"{self.base_url}/{path.strip('/')}"
        query = {"minorversion": MINOR_VERSION} | {k: v for k, v in (params or {}).items() if v is not None}
        headers = {"Authorization": f"Bearer {self._credentials.access_token}", "Accept": "application/json"}
        kwargs: dict[str, Any] = {"params": query, "headers": headers}
        if json_body is not None:
            kwargs["json"] = json_body
        elif content is not None:
            headers["Content-Type"] = content_type or "application/octet-stream"
            kwargs["content"] = content
        elif files is not None:
            kwargs["files"] = files
        is_read = read or method == "GET"
        started = time.monotonic()
        attempt = 0
        while True:
            t0 = time.monotonic()
            resp: httpx.Response | None = None
            error: QboError | None = None
            try:
                resp = await self._client().request(method, url, **kwargs)
                status = resp.status_code
            except httpx.TimeoutException:
                status, error = 0, QboError(0, f"Timed out calling QuickBooks ({method} {path})")
            except httpx.RequestError as e:
                status = 0
                error = QboError(0, f"Network error calling QuickBooks ({method} {path}): {type(e).__name__}")

            logger.info(
                "qbo_api method=%s path=%s status=%s ms=%d attempt=%d intuit_tid=%s",
                method,
                path,
                status,
                (time.monotonic() - t0) * 1000,
                attempt,
                resp.headers.get("intuit_tid", "-") if resp is not None else "-",
            )

            # 429 is refused before processing, so any call may retry it; other
            # failures are retried only for reads, so a write never runs twice.
            retryable = status == 429 or (is_read and (status == 0 or status >= 500))
            if retryable and attempt < _MAX_RETRIES:
                delay = _retry_delay(resp, attempt)
                if time.monotonic() - started + delay < _CALL_BUDGET_SECONDS:
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
            if error is not None:
                raise error
            assert resp is not None
            if resp.status_code >= 400:
                raise QboError(resp.status_code, _error_message(resp, path))
            payload = _parse_body(resp)
            # A request can succeed at HTTP level and still carry a Fault.
            if isinstance(payload, dict) and isinstance(payload.get("Fault"), dict):
                raise QboError(400, _fault_text(payload["Fault"], path))
            return payload


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), _MAX_BACKOFF_SECONDS))
            except ValueError:
                pass
    return min(float(2**attempt) * 2, _MAX_BACKOFF_SECONDS)


def _parse_body(resp: httpx.Response) -> Any:
    if not resp.content or not resp.content.strip():
        return None
    try:
        return resp.json()
    except ValueError:
        return {"raw_response": resp.text[:2000]}


def _fault_text(fault: dict, path: str) -> str:
    parts = []
    for item in fault.get("Error") or []:
        if isinstance(item, dict):
            message = item.get("Message") or ""
            detail = item.get("Detail") or ""
            code = item.get("code")
            text = " - ".join(p for p in (message, detail if detail != message else "") if p)
            parts.append(f"{text} (code {code})" if code else text)
    return f"QuickBooks rejected {path}: {' | '.join(parts)[:600]}" if parts else f"QuickBooks rejected {path}"


def _error_message(resp: httpx.Response, path: str) -> str:
    fallback = f"QuickBooks returned HTTP {resp.status_code} for {path}"
    if resp.status_code == 401:
        return f"{fallback}: the access token is invalid or expired (reconnect QuickBooks)"
    try:
        payload = resp.json()
    except ValueError:
        text = resp.text.strip()
        return f"{fallback}: {text[:300]}" if text else fallback
    if isinstance(payload, dict) and isinstance(payload.get("Fault"), dict):
        return f"{fallback}: {_fault_text(payload['Fault'], path).split(': ', 1)[-1]}"
    return fallback
