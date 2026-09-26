"""
HTTP-сервер Stocker: MCP для OpenClaw и JSON API для n8n
(docs/SERVICE_CONTRACT.md §7, docs/N8N_CONTRACT.md §3).

    python -m app.service.mcp_http

Долгоживущий Windows-процесс: в SQLite пишет одна среда. OpenClaw в WSL
(NAT, interop выключен) подключается к http://<адрес vEthernet (WSL)>:<port>/mcp.

- слушает loopback или адрес хоста в сети WSL (STOCKER_MCP_HOST=wsl);
  LAN-адреса и 0.0.0.0 запрещены;
- каналы и токены (actor задаёт сервер, токен одного канала не подходит к другому):
    /mcp        Bearer STOCKER_MCP_TOKEN → STOCKER_MCP_ACTOR (agent:openclaw)
    /api/v1/*   Bearer STOCKER_N8N_TOKEN → STOCKER_API_ACTOR (workflow:n8n);
                без STOCKER_N8N_TOKEN канал выключен (401);
- без STOCKER_MCP_TOKEN сервер не запускается;
- инструменты MCP, allowlist workflow и ограничения — в Service Layer.
"""

import hmac
import ipaddress
import os
import subprocess
import sys
import time

import anyio
import uvicorn
from dotenv import load_dotenv
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.service.mcp_server import create_server, execute, log_call, resolve_actor

load_dotenv()

DEFAULT_HOST = "127.0.0.1"
WSL_HOST = "wsl"
DEFAULT_PORT = 8765
MCP_PATH = "/mcp"
API_PREFIX = "/api/v1/"
DEFAULT_API_ACTOR = "workflow:n8n"
MIN_TOKEN_LENGTH = 32


def wsl_host_address() -> str | None:
    """IPv4 адаптера Windows "vEthernet (WSL...)": адрес хоста в NAT-сети WSL."""
    command = (
        "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
        "Where-Object { $_.InterfaceAlias -like 'vEthernet (WSL*' } | "
        "Select-Object -First 1 -ExpandProperty IPAddress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    address = result.stdout.strip()
    return address or None


def resolve_host() -> str:
    """
    Loopback (127.0.0.1, ::1) или адрес хоста в сети WSL (STOCKER_MCP_HOST=wsl).

    OpenClaw в WSL (NAT, interop выключен) видит Windows только по адресу
    адаптера vEthernet (WSL). LAN-адреса и 0.0.0.0 запрещены.
    """
    host = os.getenv("STOCKER_MCP_HOST", DEFAULT_HOST).strip()
    wsl_address = wsl_host_address() if host == WSL_HOST or not _is_loopback(host) else None

    # При автозапуске на входе в Windows адаптер WSL появляется только после старта WSL.
    if host == WSL_HOST and wsl_address is None:
        deadline = time.monotonic() + float(os.getenv("STOCKER_MCP_WAIT_SECONDS", "300"))
        print("Waiting for the vEthernet (WSL) adapter...", file=sys.stderr, flush=True)
        while wsl_address is None and time.monotonic() < deadline:
            time.sleep(5)
            wsl_address = wsl_host_address()

    if host == WSL_HOST:
        if wsl_address is None:
            raise SystemExit("STOCKER_MCP_HOST=wsl, but no 'vEthernet (WSL...)' adapter address was found")
        return wsl_address

    if _is_loopback(host) or (wsl_address is not None and host == wsl_address):
        return host

    raise SystemExit(
        "STOCKER_MCP_HOST must be loopback (127.0.0.1, ::1) or 'wsl' "
        f"(the vEthernet (WSL) address, now {wsl_address}); got {host!r}"
    )


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_port() -> int:
    return int(os.getenv("STOCKER_MCP_PORT", str(DEFAULT_PORT)))


def resolve_token() -> str:
    token = os.getenv("STOCKER_MCP_TOKEN", "").strip()
    if len(token) < MIN_TOKEN_LENGTH:
        raise SystemExit(
            f"STOCKER_MCP_TOKEN is required (at least {MIN_TOKEN_LENGTH} characters). "
            'Generate one: python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    return token


def resolve_api_token(mcp_token: str) -> str | None:
    """Токен n8n. Нет или короткий — канал /api/v1 выключен. Совпадает с MCP — ошибка."""
    token = os.getenv("STOCKER_N8N_TOKEN", "").strip()
    if not token:
        return None
    if len(token) < MIN_TOKEN_LENGTH:
        raise SystemExit(f"STOCKER_N8N_TOKEN must be at least {MIN_TOKEN_LENGTH} characters")
    if hmac.compare_digest(token, mcp_token):
        raise SystemExit("STOCKER_N8N_TOKEN must differ from STOCKER_MCP_TOKEN (separate channels)")
    return token


def resolve_api_actor() -> str:
    actor = os.getenv("STOCKER_API_ACTOR", DEFAULT_API_ACTOR).strip()
    if not actor.startswith("workflow:") or len(actor) <= len("workflow:"):
        raise SystemExit(f"STOCKER_API_ACTOR must be workflow:<name>, got {actor!r}")
    return actor


class ChannelAuth:
    """
    ASGI middleware: у каждого канала свой токен. Путь вне каналов → 404,
    неверный или чужой токен → 401.
    """

    def __init__(self, app, channels: dict[str, str | None]):
        self.app = app
        self.channels = {
            prefix: (f"Bearer {token}".encode() if token else None) for prefix, token in channels.items()
        }

    def _expected(self, path: str) -> tuple[bool, bytes | None]:
        for prefix, expected in self.channels.items():
            if path == prefix or path.startswith(prefix):
                return True, expected
        return False, None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            known, expected = self._expected(scope.get("path", ""))
            if not known:
                await _plain(send, 404, b'{"error": "not found"}')
                return
            provided = dict(scope.get("headers") or []).get(b"authorization", b"")
            if expected is None or not hmac.compare_digest(provided, expected):
                await _plain(send, 401, b'{"error": "unauthorized"}', [(b"www-authenticate", b"Bearer")])
                return

        await self.app(scope, receive, send)


async def _plain(send, status: int, body: bytes, headers: list | None = None) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), *(headers or [])],
        }
    )
    await send({"type": "http.response.body", "body": body})


def api_route(api_actor: str) -> Route:
    """POST /api/v1/{operation}: тело — параметры, ответ — envelope (HTTP 200)."""

    async def endpoint(request: Request) -> JSONResponse:
        operation = request.path_params["operation"]
        try:
            params = await request.json()
        except ValueError:
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        if not isinstance(params, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

        envelope = await anyio.to_thread.run_sync(execute, operation, params, api_actor)
        log_call(f"API op={operation}", api_actor, envelope)
        return JSONResponse(envelope)

    return Route(API_PREFIX + "{operation}", endpoint, methods=["POST"])


def create_app(actor: str, token: str, host: str = DEFAULT_HOST, api_token: str | None = None,
               api_actor: str = DEFAULT_API_ACTOR):
    server = create_server(actor)
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        host=host,
        custom_starlette_routes=[api_route(api_actor)],
    )
    return ChannelAuth(app, {MCP_PATH: token, API_PREFIX: api_token})


def main() -> None:
    actor = resolve_actor()
    host = resolve_host()
    port = resolve_port()
    token = resolve_token()
    api_token = resolve_api_token(token)
    api_actor = resolve_api_actor()

    print(f"Stocker MCP HTTP: http://{host}:{port}{MCP_PATH} actor={actor}", file=sys.stderr)
    if api_token:
        print(f"Stocker API: http://{host}:{port}{API_PREFIX}<operation> actor={api_actor}", file=sys.stderr)
    else:
        print("Stocker API: disabled (STOCKER_N8N_TOKEN is not set)", file=sys.stderr)
    # print() из worker не должен смешиваться с логами uvicorn в stdout.
    sys.stdout = sys.stderr
    uvicorn.run(create_app(actor, token, host, api_token, api_actor), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
