"""
MCP stdio-сервер Stocker для агентов (docs/SERVICE_CONTRACT.md §7).

    python -m app.service.mcp_server

- инструменты генерируются из реестра операций (одно описание — много транспортов);
- actor фиксирует сервер, а не агент: STOCKER_MCP_ACTOR (по умолчанию
  agent:openclaw), только agent:* или workflow:*;
- операции уровня review (approve/reject) агенту не показываются и не выполняются;
- каждый вызов возвращает envelope service layer (JSON) как текст и как
  structured content.

Запускать в Windows (.venv): в SQLite должна писать одна среда (§7).
"""

import contextlib
import json
import os
import re
import sys

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from app.service import dispatch
from app.service.registry import PIPELINE, READ, build_registry

DEFAULT_ACTOR = "agent:openclaw"
EXPOSED_ACCESS = (READ, PIPELINE)
_NON_HUMAN_ACTOR = re.compile(r"^(agent|workflow):[a-z0-9_.-]+$")

INSTRUCTIONS = """Stocker: industrial stock photo pipeline (ingest -> QC -> Vision -> metadata -> review gate).
Start with asset_list or review_queue, inspect with asset_get (see pipeline and allowed_actions).
You may process images, build/rebuild/edit metadata, re-run the gate and escalate to a human.
Approve/reject are human decisions and are not available to agents. auto_approved is set only by
the deterministic review gate. Every result is a JSON envelope: {ok, outcome, data, error}."""


def tool_name(operation: str) -> str:
    """MCP-совместимое имя: asset.get → asset_get."""
    return operation.replace(".", "_")


def resolve_actor() -> str:
    actor = os.getenv("STOCKER_MCP_ACTOR", DEFAULT_ACTOR).strip()
    if not _NON_HUMAN_ACTOR.match(actor):
        raise SystemExit(f"STOCKER_MCP_ACTOR must be agent:<name> or workflow:<name>, got {actor!r}")
    return actor


def exposed_operations() -> dict:
    """tool name → Operation, только read и pipeline."""
    return {
        tool_name(operation.name): operation
        for operation in build_registry().values()
        if operation.access in EXPOSED_ACCESS
    }


def tools() -> list[types.Tool]:
    result = []
    for name, operation in exposed_operations().items():
        read_only = operation.access == READ
        result.append(
            types.Tool(
                name=name,
                title=operation.name,
                description=f"[{operation.access}] {operation.description}",
                input_schema=operation.params.model_json_schema(),
                annotations=types.ToolAnnotations(
                    read_only_hint=read_only,
                    destructive_hint=False,
                    idempotent_hint=read_only or operation.name == "metadata.gate",
                    open_world_hint=False,
                ),
            )
        )
    return result


def call(name: str, arguments: dict | None, actor: str) -> types.CallToolResult:
    operations = exposed_operations()

    if name not in operations:
        envelope = {
            "api_version": "1",
            "operation": name,
            "ok": False,
            "asset_id": None,
            "outcome": None,
            "data": None,
            "error": {"code": "UNKNOWN_OPERATION", "message": f"Unknown or not allowed tool: {name}"},
        }
    else:
        envelope = dispatch(operations[name].name, arguments or {}, actor=actor)

    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False))],
        structured_content=envelope,
        is_error=not envelope["ok"],
    )


def create_server(actor: str) -> Server:
    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools())

    async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        # dispatch синхронный и может ждать LM Studio минутами — в отдельном потоке.
        return await anyio.to_thread.run_sync(call, params.name, params.arguments, actor)

    return Server(
        "stocker",
        version="1",
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def _serve(actor: str) -> None:
    async with stdio_server() as (read_stream, write_stream):
        # stdout — канал протокола (уже захвачен транспортом): любой print() → stderr.
        with contextlib.redirect_stdout(sys.stderr):
            server = create_server(actor)
            await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    actor = resolve_actor()
    print(f"Stocker MCP server: actor={actor}", file=sys.stderr)
    anyio.run(_serve, actor)


if __name__ == "__main__":
    main()
