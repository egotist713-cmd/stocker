"""
MCP streamable HTTP-сервер Stocker для OpenClaw в WSL (docs/SERVICE_CONTRACT.md §7).

    python -m app.service.mcp_http

Долгоживущий Windows-процесс: в SQLite пишет одна среда. OpenClaw в WSL
(NAT, interop выключен) подключается к http://<адрес vEthernet (WSL)>:<port>/mcp.

- слушает loopback или адрес хоста в сети WSL (STOCKER_MCP_HOST=wsl);
  LAN-адреса и 0.0.0.0 запрещены;
- обязателен токен: заголовок "Authorization: Bearer <STOCKER_MCP_TOKEN>".
  Без токена сервер не запускается;
- инструменты, actor и ограничения — те же, что у stdio-сервера (mcp_server).
"""

import hmac
import ipaddress
import os
import subprocess
import sys

import uvicorn
from dotenv import load_dotenv

from app.service.mcp_server import create_server, resolve_actor

load_dotenv()

DEFAULT_HOST = "127.0.0.1"
WSL_HOST = "wsl"
DEFAULT_PORT = 8765
MCP_PATH = "/mcp"
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


class BearerAuth:
    """ASGI middleware: любой HTTP-запрос без верного Bearer-токена → 401."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            provided = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(provided, self.expected):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"error": "unauthorized"}'})
                return

        await self.app(scope, receive, send)


def create_app(actor: str, token: str, host: str = DEFAULT_HOST):
    server = create_server(actor)
    app = server.streamable_http_app(streamable_http_path=MCP_PATH, host=host)
    return BearerAuth(app, token)


def main() -> None:
    actor = resolve_actor()
    host = resolve_host()
    port = resolve_port()
    token = resolve_token()

    print(f"Stocker MCP HTTP: http://{host}:{port}{MCP_PATH} actor={actor}", file=sys.stderr)
    # print() из worker не должен смешиваться с логами uvicorn в stdout.
    sys.stdout = sys.stderr
    uvicorn.run(create_app(actor, token, host), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
