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
"""Table-driven list / get / create / update / void / search tools for each entity."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import QboClient
from ._common import (
    DESTRUCTIVE,
    READ_ONLY,
    WRITES,
    WRITES_IDEMPOTENT,
    ClientFactory,
    EndDate,
    MaxResults,
    StartDate,
    StartPosition,
    add_tool,
    compact,
    date_conditions,
    quote,
    record_id,
    run_query,
    select,
    unwrap,
)
from .entity_table import ENTITIES, FIELD_DOCS, Entity, F

_PY_TYPES: dict[str, Any] = {
    "string": str,
    "number": float,
    "boolean": bool,
    "object": dict[str, Any],
    "array": list[dict[str, Any]],
}

SyncToken = Annotated[
    str, Field(description="The record's current SyncToken (from *_get); stale tokens are rejected.", min_length=1)
]


def _field_param(f: F, required: bool) -> tuple[str, Any, Any]:
    base = _PY_TYPES[f.type]
    doc = FIELD_DOCS[f.name]
    if required:
        return f.name, Annotated[base, Field(description=doc)], ...
    return f.name, Annotated[base | None, Field(description=doc)], None


def _fields_body(fields: tuple[F, ...], args: dict[str, Any]) -> dict[str, Any]:
    return compact({f.name: args.get(f.name) for f in fields})


def _id_param(e: Entity, verb: str) -> tuple[str, Any, Any]:
    return e.id_param, Annotated[str, Field(description=f"Id of the {e.label} to {verb}.", min_length=1)], ...


def _register_entity(mcp: FastMCP, e: Entity, client_factory: ClientFactory) -> None:
    words = e.domain.replace("_", " ")

    if e.listable:
        params: list[tuple[str, Any, Any]] = [("startPosition", StartPosition, None), ("maxResults", MaxResults, None)]
        if e.list_dated:
            params += [("startDate", StartDate, None), ("endDate", EndDate, None)]

        async def list_handler(client: QboClient, args: dict[str, Any], e: Entity = e) -> dict:
            conditions = date_conditions(args.get("startDate"), args.get("endDate")) if e.list_dated else []
            start, size = args.get("startPosition"), args.get("maxResults")
            return await run_query(client, e.entity, select(e.entity, conditions, start, size), start, size)

        add_tool(
            mcp,
            f"{e.prefix}_list",
            f"List {words} with paging{', optionally within a TxnDate range' if e.list_dated else ''}.",
            READ_ONLY,
            params,
            list_handler,
            client_factory,
        )

    async def get_handler(client: QboClient, args: dict[str, Any], e: Entity = e) -> Any:
        rid = record_id(args[e.id_param], e.id_param)
        return unwrap(await client.get(f"{e.path}/{rid}"), e.entity)

    add_tool(
        mcp,
        f"{e.prefix}_get",
        f"Get one {e.label} by id, including the SyncToken needed for updates.",
        READ_ONLY,
        [_id_param(e, "fetch")],
        get_handler,
        client_factory,
    )

    if e.create:

        async def create_handler(client: QboClient, args: dict[str, Any], e: Entity = e) -> Any:
            return unwrap(await client.post(e.path, _fields_body(e.create, args)), e.entity)

        add_tool(
            mcp,
            f"{e.prefix}_create",
            f"Create a {e.label} in QuickBooks. Only when the user explicitly asks.",
            WRITES,
            [_field_param(f, f.required) for f in e.create],
            create_handler,
            client_factory,
        )

    if e.update:

        async def update_handler(client: QboClient, args: dict[str, Any], e: Entity = e) -> Any:
            rid = record_id(args[e.id_param], e.id_param)
            changes = _fields_body(e.update, args)
            if not changes:
                raise ValueError("Nothing to update: pass at least one field to change.")
            body = {"Id": rid, "SyncToken": str(args["SyncToken"]).strip(), "sparse": True, **changes}
            return unwrap(await client.post(e.path, body), e.entity)

        params = [_id_param(e, "update"), ("SyncToken", SyncToken, ...)] + [
            _field_param(f, f.required) for f in e.update
        ]
        add_tool(
            mcp,
            f"{e.prefix}_update",
            f"Change fields of a {e.label} (sparse update; Line replaces all lines).\n\n"
            "Needs the current SyncToken from the get tool. Only when the user explicitly asks.",
            WRITES_IDEMPOTENT,
            params,
            update_handler,
            client_factory,
        )

    if e.void:

        async def void_handler(client: QboClient, args: dict[str, Any], e: Entity = e) -> Any:
            rid = record_id(args[e.id_param], e.id_param)
            params = {"operation": "void"} if e.void == "operation" else {"operation": "update", "include": "void"}
            body = {"Id": rid, "SyncToken": str(args["SyncToken"]).strip(), "sparse": True}
            return unwrap(await client.post(e.path, body, params), e.entity)

        add_tool(
            mcp,
            f"{e.prefix}_void",
            f"Void a {e.label}: it stays in QuickBooks with all amounts set to zero.\n\n"
            "Needs the current SyncToken. Cannot be undone: only when the user explicitly asks.",
            DESTRUCTIVE,
            [_id_param(e, "void"), ("SyncToken", SyncToken, ...)],
            void_handler,
            client_factory,
        )

    if e.search:
        params = [
            (
                "term",
                Annotated[str, Field(description=f"Text the {e.search} contains (% is a wildcard).", min_length=1)],
                ...,
            ),
            ("startPosition", StartPosition, None),
            ("maxResults", MaxResults, None),
        ]

        async def search_handler(client: QboClient, args: dict[str, Any], e: Entity = e) -> dict:
            term = str(args["term"]).strip()
            if not term:
                raise ValueError("term must not be blank.")
            start, size = args.get("startPosition"), args.get("maxResults")
            sql = select(e.entity, [f"{e.search} LIKE {quote('%' + term + '%')}"], start, size)
            return await run_query(client, e.entity, sql, start, size)

        add_tool(
            mcp,
            f"{e.prefix}_search",
            f"Find {words} whose {e.search} contains the given text.",
            READ_ONLY,
            params,
            search_handler,
            client_factory,
        )


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    for entity in ENTITIES:
        _register_entity(mcp, entity, client_factory)
