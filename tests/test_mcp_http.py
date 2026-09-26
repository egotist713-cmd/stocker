import json
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
N8N_TOKEN = "n" * 40


# --- конфигурация ------------------------------------------------------------------


WSL_ADDRESS = "172.26.192.1"


@pytest.fixture
def wsl_adapter(monkeypatch):
    monkeypatch.setattr(mcp_http, "wsl_host_address", lambda: WSL_ADDRESS)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.104", "172.27.112.1", "example.com", "::"])
def test_lan_and_wildcard_hosts_are_refused(monkeypatch, wsl_adapter, host):
    monkeypatch.setenv("STOCKER_MCP_HOST", host)

    with pytest.raises(SystemExit):
        mcp_http.resolve_host()


@pytest.mark.parametrize("host", ["wsl", WSL_ADDRESS])
def test_wsl_adapter_address_allowed(monkeypatch, wsl_adapter, host):
    monkeypatch.setenv("STOCKER_MCP_HOST", host)

    assert mcp_http.resolve_host() == WSL_ADDRESS


def test_wsl_host_requires_adapter(monkeypatch):
    monkeypatch.setattr(mcp_http, "wsl_host_address", lambda: None)
    monkeypatch.setenv("STOCKER_MCP_HOST", "wsl")
    monkeypatch.setenv("STOCKER_MCP_WAIT_SECONDS", "0")

    with pytest.raises(SystemExit):
        mcp_http.resolve_host()


def test_wsl_host_waits_for_adapter(monkeypatch):
    answers = iter([None, None, WSL_ADDRESS])
    monkeypatch.setattr(mcp_http, "wsl_host_address", lambda: next(answers))
    monkeypatch.setattr(mcp_http.time, "sleep", lambda _: None)
    monkeypatch.setenv("STOCKER_MCP_HOST", "wsl")

    assert mcp_http.resolve_host() == WSL_ADDRESS


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
    qc.MIN_MEGAPIXELS = 0.0
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
        # Рабочий .env задаёт STOCKER_MCP_HOST=wsl; тестовый сервер — только loopback.
        "STOCKER_MCP_HOST": "127.0.0.1",
        "STOCKER_N8N_TOKEN": N8N_TOKEN,
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



# --- HTTP API для n8n (/api/v1) ---------------------------------------------------------



def _api(base_mcp_url: str, operation: str, params=None, token: str | None = N8N_TOKEN, raw: bytes | None = None):
    url = base_mcp_url.rsplit("/mcp", 1)[0] + f"/api/v1/{operation}"
    body = raw if raw is not None else json.dumps(params or {}).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, None


def test_api_workflow_flow_and_rights(http_server):
    status, listed = _api(http_server, "incoming.list")
    assert status == 200 and listed["data"]["items"][0]["path"] == "data/incoming/photo.jpg"

    status, processed = _api(http_server, "asset.process_file", {"path": "data/incoming/photo.jpg"})
    assert status == 200 and processed["ok"]
    asset_id = processed["asset_id"]
    assert processed["data"]["pipeline"]["metadata"] == "auto_approved"

    for operation, params in [
        ("metadata.edit", {"asset_id": asset_id, "title": "n8n"}),
        ("metadata.approve", {"asset_id": asset_id}),
        ("metadata.reject", {"asset_id": asset_id, "reason": "x"}),
        ("asset.process", {"asset_id": asset_id, "force": True}),
    ]:
        status, envelope = _api(http_server, operation, params)
        assert status == 200 and envelope["error"]["code"] == "FORBIDDEN", operation

    status, history = _api(http_server, "asset.history", {"asset_id": asset_id, "stage": "METADATA"})
    assert {event["message"]["actor"] for event in history["data"]} == {"workflow:n8n"}


def test_api_tokens_are_per_channel(http_server):
    assert _api(http_server, "review.queue", token=None)[0] == 401
    assert _api(http_server, "review.queue", token=TOKEN)[0] == 401  # токен OpenClaw не подходит к API

    request = urllib.request.Request(http_server, data=b"{}", method="POST", headers={"Authorization": f"Bearer {N8N_TOKEN}"})
    with pytest.raises(urllib.error.HTTPError) as info:  # токен n8n не подходит к MCP
        urllib.request.urlopen(request, timeout=5)
    assert info.value.code == 401


def test_api_bad_requests(http_server):
    assert _api(http_server, "review.queue", raw=b"not json")[0] == 400
    assert _api(http_server, "review.queue", raw=b"[1, 2]")[0] == 400
    status, envelope = _api(http_server, "no.such.operation")
    assert status == 200 and envelope["error"]["code"] == "UNKNOWN_OPERATION"

    url = http_server.rsplit("/mcp", 1)[0] + "/other"
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(urllib.request.Request(url, data=b"{}", method="POST"), timeout=5)
    assert info.value.code == 404


def test_api_token_rules(monkeypatch):
    monkeypatch.delenv("STOCKER_N8N_TOKEN", raising=False)
    assert mcp_http.resolve_api_token(TOKEN) is None

    monkeypatch.setenv("STOCKER_N8N_TOKEN", "short")
    with pytest.raises(SystemExit):
        mcp_http.resolve_api_token(TOKEN)

    monkeypatch.setenv("STOCKER_N8N_TOKEN", TOKEN)
    with pytest.raises(SystemExit):  # тот же токен, что у MCP
        mcp_http.resolve_api_token(TOKEN)

    monkeypatch.setenv("STOCKER_API_ACTOR", "human")
    with pytest.raises(SystemExit):
        mcp_http.resolve_api_actor()
