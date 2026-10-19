# QuickBooks Online MCP Server

An [MCP](https://modelcontextprotocol.io) server for the QuickBooks Online Accounting API (v3).
It lets an AI agent read one QuickBooks company's customers, vendors, items, accounts,
invoices, estimates, payments, bills, purchases, journal entries, time activities, attachments
and financial reports and, on explicit request, create, update and void records, email
invoices and upload attachments.

- Transport: MCP Streamable HTTP at `POST /mcp`, health probe at `GET /health`
- Stateless and multi-tenant: the OAuth access token and company (realm) id arrive on every
  request as HTTP headers and are never stored; every tool is listed from the first
  `tools/list`
- Python 3.12, [uv](https://docs.astral.sh/uv/), FastMCP, httpx
- License: Apache-2.0, Copyright 2026 MSPbots.ai

## Authentication headers

| Header | Required | Type | Default | Allowed values | Description | Example |
|---|---|---|---|---|---|---|
| `X-Qbo-Access-Token` | Yes | string | none | - | OAuth 2.0 access token for the company (a leading `Bearer ` is accepted and ignored) | `eyJlbmMiOiJBMTI4Q0JDLUhTMjU2...` |
| `X-Qbo-Realm-Id` | Yes | string | none | digits only | The QuickBooks company id (realm id) | `9130350000000001` |
| `X-Qbo-Environment` | No | string | `production` | `production`, `prod`, `sandbox`, `sbx` | Which QuickBooks API host to call | `sandbox` |

Where to get them: the access token and realm id come from the OAuth 2.0 authorization of a
QuickBooks Online app (Intuit developer portal) with the `com.intuit.quickbooks.accounting`
scope. The realm id is returned with the authorization callback. Access tokens expire after
about an hour; the caller (for example a gateway) refreshes them and sends the current one.
This server never refreshes or stores tokens.

How they are sent to QuickBooks: the token as `Authorization: Bearer <token>` to
`https://quickbooks.api.intuit.com/v3/company/<realm>/...` (or the sandbox host), with
`minorversion=75` on every call. Values that look like unresolved templates (`${...}`) are
treated as missing.

A request to `/mcp` without the required headers gets HTTP 401 naming them; a non-numeric realm
id or an unknown environment gets HTTP 400.

## Tools

All 138 tools return a JSON string. Names follow `qbo_<records>_<action>`.

Generic actions, per area:

| Action | What it does | Parameters (required in **bold**) |
|---|---|---|
| `_list` | Query records, one page | startPosition (from 1), maxResults (default 100, max 1000); transaction areas also startDate, endDate (YYYY-MM-DD, on TxnDate) |
| `_get` | One record by id | **`<record>Id`** |
| `_search` | Records whose name / number contains a term | **term**, startPosition, maxResults |
| `_create` | Create a record (**write**) | the area's required fields plus optional ones |
| `_update` | Sparse update; only given fields change (**write**) | **`<record>Id`**, **SyncToken**, the area's required fields, optional fields |
| `_void` | Void a transaction (**write**, destructive) | **`<record>Id`**, **SyncToken** |

List and search results look like `{items, count, startPosition, maxResults, has_more,
next_startPosition}`.

| Area | Tools |
|---|---|
| accounts, classes, departments, items, payment_methods, terms | list, get, search, create, update |
| customers, vendors, employees | list, get, search, create, update |
| tax_codes, tax_rates | list, get, search |
| company_info | list, get |
| bills | list (dated), get, search (DocNumber), create, update |
| bill_payments, credit_memos, deposits, estimates, journal_entries, purchase_orders, purchases, refund_receipts, sales_receipts, time_activities, transfers, vendor_credits | list (dated), get, create, update |
| payments | list (dated), get, create, update, void |
| invoices | get, create, update, void, plus `qbo_invoices_list` and `qbo_invoices_send` below |
| attachables | list, get, create, update, plus `qbo_attachables_upload` below |

Special tools:

| Tool | Purpose | Parameters (required in **bold**) |
|---|---|---|
| `qbo_navigate` | List the tools of one area (changes nothing) | **domain** |
| `qbo_status` | Check the connection: company name, realm id, environment | - |
| `qbo_invoices_list` | Invoices, newest first, optionally by payment status | status (`Paid`, `Unpaid`, `Overdue`), startPosition, maxResults |
| `qbo_invoices_send` | Email an invoice to the customer (**write**) | **invoiceId**, email (defaults to the invoice's billing email) |
| `qbo_attachables_upload` | Upload a file, optionally attached to a record (**write**) | **fileName**, **contentType**, **base64Data**, attachToType + attachToId (together), includeOnSend, note, category |
| `qbo_expenses_list_purchases` / `_list_bills` | Expenses, newest first | startDate, endDate, startPosition, maxResults |
| `qbo_expenses_get_purchase` / `_get_bill` | One expense | **purchaseId** / **billId** |
| `qbo_reports_profit_and_loss`, `_balance_sheet`, `_trial_balance`, `_general_ledger` | Financial reports | **start_date**, **end_date**, accounting_method (`Cash`, `Accrual`) |
| `qbo_reports_cash_flow`, `_customer_sales`, `_vendor_expenses` | Financial reports | **start_date**, **end_date** |
| `qbo_reports_aged_receivables`, `_aged_payables`, `_customer_balance` | Point-in-time reports | report_date (default today) |

Errors come back as a JSON envelope instead of an exception:

```json
{"error": {"code": "invalid_argument", "message": "QuickBooks rejected customer: Stale Object Error: You and another user were working on this at the same time. (code 5010)", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`,
`rate_limited`, `upstream_error`. A `Fault` returned with HTTP 200 is also reported as an
error. Reads (including queries) are retried with backoff on 429, 5xx and network errors;
writes only on 429.

## Run locally

```bash
uv sync
uv run python -m qbo_mcp          # listens on 0.0.0.0:8080
```

or with Docker:

```bash
docker build --platform linux/amd64 -t qbo-mcp:dev .
docker run --rm -p 8080:8080 qbo-mcp:dev
```

Configuration (environment variables, all optional): `MCP_HTTP_HOST` (default `0.0.0.0`),
`MCP_HTTP_PORT` (default `8080`), `LOG_LEVEL` (default `INFO`). Credentials are never read from
the environment.

## Testing example

```bash
# list tools
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Qbo-Access-Token: <access-token>" \
  -H "X-Qbo-Realm-Id: 9130350000000001" \
  -H "X-Qbo-Environment: sandbox" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# unpaid invoices
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Qbo-Access-Token: <access-token>" \
  -H "X-Qbo-Realm-Id: 9130350000000001" \
  -H "X-Qbo-Environment: sandbox" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"qbo_invoices_list","arguments":{"status":"Unpaid"}}}'
```

Unit tests (no network access needed):

```bash
uv run pytest
```

## Delivery notes

- **Same tool contract.** All 138 tool names and their required parameters are unchanged from
  the older version, and the same three `X-Qbo-*` headers are used.
- **All tools always listed.** `qbo_navigate` remains for compatibility and only describes an
  area's tools.
- **Query escaping.** Search terms are escaped the way the QuickBooks query language expects
  (backslash before `'` and `\`); the older version doubled quotes, which QuickBooks does not
  treat as an escape.
- **Updates.** Updates are always sparse and need the current `SyncToken` (from `_get`); an
  update with no changed fields is rejected before calling QuickBooks.
- **Not included.** The older version's interactive invoice card (an MCP Apps `ui://`
  resource) is not part of this server; invoice tools return plain JSON.
- **Tokens.** The server does not refresh OAuth tokens; send a current access token on every
  request.

## License

Apache License 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE).
