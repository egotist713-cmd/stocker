"""
MCP streamable HTTP-сервер Stocker для OpenClaw в WSL (docs/SERVICE_CONTRACT.md §7).

    python -m app.service.mcp_http

Долгоживущий Windows-процесс: в SQLite пишет одна среда. OpenClaw в WSL
(mirrored networking) подключается к http://127.0.0.1:<port>/mcp.

- слушает только loopback (STOCKER_MCP_HOST: 127.0.0.1 или ::1);
- обязателен токен: заголовок "Authorization: Bearer <STOCKER_MCP_TOKEN>".
  Без токена сервер не запускается;
- инструменты, actor и ограничения — те же, что у stdio-сервера (mcp_server).
"""

import hmac
import ipaddress
import os
import sys

import uvicorn
from dotenv import load_dotenv

from app.service.mcp_server import create_server, resolve_actor

load_dotenv()

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MCP_PATH = "/mcp"
MIN_TOKEN_LENGTH = 32


def resolve_host() -> str:
    host = os.getenv("STOCKER_MCP_HOST", DEFAULT_HOST).strip()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise SystemExit(f"STOCKER_MCP_HOST must be a loopback address (127.0.0.1 or ::1), got {host!r}")
    return host


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
