import os
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from app.service import mcp_http
from tests.conftest import make_image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 40


# --- конфигурация ------------------------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.104", "172.26.192.1", "example.com"])
def test_only_loopback_hosts_allowed(monkeypatch, host):
    monkeypatch.setenv("STOCKER_MCP_HOST", host)

    with pytest.raises(SystemExit):
        mcp_http.resolve_host()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_loopback_hosts(monkeypatch, host):
    monkeypatch.setenv("STOCKER_MCP_HOST", host)

    assert mcp_http.resolve_host() == host


@pytest.mark.parametrize("token", ["", "short"])
def test_token_is_required(monkeypatch, token):
    monkeypatch.setenv("STOCKER_MCP_TOKEN", token)

    with pytest.raises(SystemExit):
        mcp_http.resolve_token()


# --- настоящий HTTP: отдельный процесс, изолированная БД -------------------------------

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
    qc.MIN_WIDTH = qc.MIN_HEIGHT = 10
    qc.MIN_FILE_SIZE = 0
    worker.LocalAnalyzer = FakeAnalyzer
    metadata_service.LMStudioMetadataAnalyzer = OfflineMetadataAnalyzer

    from app.service import mcp_http
    mcp_http.main()
    """
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def http_server(stocker_root, tmp_path):
    make_image(stocker_root)
    launcher = tmp_path / "launch_http.py"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    port = _free_port()
    env = {
        **os.environ,
        "STOCKER_MCP_TOKEN": TOKEN,
        "STOCKER_MCP_PORT": str(port),
        "STOCKER_MCP_ACTOR": "agent:openclaw",
    }
    process = subprocess.Popen(
        [sys.executable, str(launcher), str(stocker_root), str(PROJECT_ROOT)],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}/mcp"

    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:
        process.kill()
        pytest.fail(f"server did not start: {process.stderr.read().decode(errors='replace')}")

    yield url
    process.terminate()
    process.wait(timeout=10)


@pytest.mark.parametrize("header", [None, "Bearer wrong-token", f"Basic {TOKEN}"])
def test_requests_without_valid_token_are_rejected(http_server, header):
    request = urllib.request.Request(http_server, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    if header:
        request.add_header("Authorization", header)

    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(request, timeout=5)

    assert info.value.code == 401


def test_real_http_session(http_server):
    async def session_flow():
        client = create_mcp_http_client(headers={"Authorization": f"Bearer {TOKEN}"})
        async with client:
            async with streamable_http_client(http_server, http_client=client) as (read, write, *_):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    processed = await session.call_tool("asset_process_file", {"path": "data/incoming/photo.jpg"})
                    asset_id = processed.structured_content["asset_id"]
                    got = await session.call_tool("asset_get", {"asset_id": asset_id})
                    escalated = await session.call_tool("metadata_escalate", {"asset_id": asset_id, "reason": "check"})
                    approve = await session.call_tool("metadata_approve", {"asset_id": asset_id})
                    return listed, processed, got, escalated, approve

    listed, processed, got, escalated, approve = anyio.run(session_flow)

    names = {tool.name for tool in listed.tools}
    assert "asset_get" in names and "metadata_approve" not in names and "metadata_reject" not in names
    assert processed.structured_content["data"]["pipeline"]["metadata"] == "auto_approved"
    assert got.structured_content["data"]["pipeline"]["vision"] == "done"
    assert escalated.structured_content["data"]["pipeline"]["metadata"] == "human_review"
    assert approve.is_error is True
