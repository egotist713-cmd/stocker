import json
import os
import sys
import textwrap
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from app.service import mcp_server
from tests.conftest import make_image
from tests.test_service import _vision_asset

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --- unit ---------------------------------------------------------------------------


def test_review_operations_are_not_exposed():
    names = {tool.name for tool in mcp_server.tools()}

    assert "metadata_approve" not in names and "metadata_reject" not in names
    assert {"asset_get", "review_queue", "asset_process_file", "metadata_edit", "metadata_escalate"} <= names


def test_tools_carry_schemas_and_hints():
    tools = {tool.name: tool for tool in mcp_server.tools()}

    assert tools["asset_get"].input_schema["required"] == ["asset_id"]
    assert tools["asset_get"].annotations.read_only_hint is True
    assert tools["metadata_edit"].annotations.read_only_hint is False
    assert tools["metadata_edit"].title == "metadata.edit"


@pytest.mark.parametrize("actor", ["human", "root", "agent:", "HUMAN"])
def test_server_refuses_human_or_invalid_actor(monkeypatch, actor):
    monkeypatch.setenv("STOCKER_MCP_ACTOR", actor)

    with pytest.raises(SystemExit):
        mcp_server.resolve_actor()


def test_default_actor(monkeypatch):
    monkeypatch.delenv("STOCKER_MCP_ACTOR", raising=False)

    assert mcp_server.resolve_actor() == "agent:openclaw"


def test_call_uses_server_actor_and_blocks_review(stocker_root):
    asset_id = _vision_asset(stocker_root)

    built = mcp_server.call("metadata_build", {"asset_id": asset_id}, "agent:openclaw")
    approve = mcp_server.call("metadata_approve", {"asset_id": asset_id}, "agent:openclaw")

    assert built.is_error is False
    assert built.structured_content["data"]["pipeline"]["metadata"] == "auto_approved"
    assert json.loads(built.content[0].text) == built.structured_content
    assert approve.is_error is True
    assert approve.structured_content["error"]["code"] == "UNKNOWN_OPERATION"


# --- настоящий stdio: отдельный процесс, изолированная БД ------------------------------

LAUNCHER = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    root = Path(sys.argv[1])
    sys.path.insert(0, sys.argv[2])

    from app import ingest, qc, worker
    from app import metadata as metadata_service
    from app.database import db
    from tests.conftest import FakeAnalyzer, OfflineMetadataAnalyzer

    ingest.ROOT = qc.ROOT = root
    qc.DB_PATH = db.DEFAULT_DB_PATH = root / "data" / "db" / "stocker.db"
    qc.MIN_MEGAPIXELS = 0.0
    qc.MIN_FILE_SIZE = 0
    worker.LocalAnalyzer = FakeAnalyzer
    metadata_service.LMStudioMetadataAnalyzer = OfflineMetadataAnalyzer

    from app.service import mcp_server
    mcp_server.main()
    """
)


def test_real_stdio_session(stocker_root, tmp_path):
    make_image(stocker_root)
    launcher = tmp_path / "launch_mcp.py"
    launcher.write_text(LAUNCHER, encoding="utf-8")

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(launcher), str(stocker_root), str(PROJECT_ROOT)],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "STOCKER_MCP_ACTOR": "agent:openclaw"},
    )

    async def session_flow():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()

                # process_file печатает прогресс worker'а: канал протокола не должен сломаться.
                processed = await session.call_tool("asset_process_file", {"path": "data/incoming/photo.jpg"})
                asset_id = processed.structured_content["asset_id"]
                edited = await session.call_tool(
                    "metadata_edit", {"asset_id": asset_id, "description": "ISO 9001 certified junction box."}
                )
                history = await session.call_tool("asset_history", {"asset_id": asset_id, "stage": "METADATA"})
                approve = await session.call_tool("metadata_approve", {"asset_id": asset_id})
                return listed, processed, edited, history, approve

    listed, processed, edited, history, approve = anyio.run(session_flow)

    assert "metadata_approve" not in {tool.name for tool in listed.tools}
    assert processed.is_error is False
    assert processed.structured_content["data"]["pipeline"]["metadata"] == "auto_approved"
    assert edited.structured_content["data"]["pipeline"]["metadata"] == "human_review"
    assert "LEGAL_CLAIM" in edited.structured_content["data"]["pipeline"]["review_reasons"]
    assert {event["message"]["actor"] for event in history.structured_content["data"]} == {"agent:openclaw"}
    assert approve.is_error is True
