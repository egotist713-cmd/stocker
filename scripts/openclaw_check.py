"""
Проверка OpenClaw для check_environment.ps1. Запускается внутри WSL
(python3 без зависимостей), только чтение. Секреты не выводятся.

    python3 openclaw_check.py <адрес хоста WSL> <путь к SKILL.md в репозитории>

Печатает строки "PASS|WARN|FAIL <проверка>: <детали>".
"""

import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOST = sys.argv[1]
REPO_SKILL = Path(sys.argv[2])
HOME = Path.home() / ".openclaw"
EXPECTED_MODEL = "lmstudio/qwen3-vl-8b-instruct"


def report(status: str, name: str, detail: str = "") -> None:
    print(f"{status} {name}: {detail}")


def run(*command: str) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def http_status(url: str, method: str = "GET") -> int | None:
    request = urllib.request.Request(url, method=method, data=b"{}" if method == "POST" else None)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError):
        return None


# --- WSL ------------------------------------------------------------------------------
mode = run("wslinfo", "--networking-mode")
report("PASS" if mode == "nat" else "FAIL", "wsl networking mode", mode or "unknown")

wsl_conf = Path("/etc/wsl.conf").read_text(encoding="utf-8") if Path("/etc/wsl.conf").exists() else ""
interop_off = "[interop]" in wsl_conf and "enabled=false" in wsl_conf.split("[interop]", 1)[1].split("[", 1)[0]
report("PASS" if interop_off else "WARN", "wsl interop disabled (agent isolation)", str(interop_off))

gateway = run("systemctl", "--user", "is-active", "openclaw-gateway")
report("PASS" if gateway == "active" else "FAIL", "openclaw gateway service", gateway or "unknown")

listening = ":18789 " in run("ss", "-ltn")
report("PASS" if listening else "FAIL", "openclaw gateway port 18789", str(listening))

# --- конфиг OpenClaw -----------------------------------------------------------------------
config = json.loads((HOME / "openclaw.json").read_text(encoding="utf-8"))

model = config.get("agents", {}).get("defaults", {}).get("model", {}).get("primary")
report("PASS" if model == EXPECTED_MODEL else "FAIL", "agent model", str(model))

lmstudio = config.get("models", {}).get("providers", {}).get("lmstudio", {})
base_url = lmstudio.get("baseUrl", "")
base_host = urllib.parse.urlparse(base_url).hostname
report("PASS" if base_host == HOST else "FAIL", "LM Studio baseUrl host", f"{base_host} (WSL host now {HOST})")

entry = next((m for m in lmstudio.get("models", []) if m.get("id") == "qwen3-vl-8b-instruct"), {})
report("PASS" if entry.get("contextWindow") == 32768 else "FAIL", "qwen3-vl contextWindow", str(entry.get("contextWindow")))

stocker = config.get("mcp", {}).get("servers", {}).get("stocker", {})
mcp_url = stocker.get("url", "")
mcp_host = urllib.parse.urlparse(mcp_url).hostname
report("PASS" if mcp_host == HOST else "FAIL", "stocker MCP url host", f"{mcp_host} (WSL host now {HOST})")
has_token = str(stocker.get("headers", {}).get("Authorization", "")).startswith("Bearer ")
report("PASS" if has_token else "FAIL", "stocker MCP bearer header", "present" if has_token else "missing")

# --- skill ---------------------------------------------------------------------------------
installed = HOME / "workspace" / "skills" / "stocker" / "SKILL.md"
if installed.exists() and REPO_SKILL.exists():
    same = hashlib.sha256(installed.read_bytes().replace(b"\r\n", b"\n")).digest() == hashlib.sha256(
        REPO_SKILL.read_bytes().replace(b"\r\n", b"\n")
    ).digest()
    report("PASS" if same else "WARN", "skill stocker matches repository", str(same))
else:
    report("FAIL", "skill stocker installed", str(installed.exists()))

leftovers = [p.name for p in (HOME / "workspace").iterdir() if p.suffix == ".py" or p.name == "app"]
report("PASS" if not leftovers else "WARN", "workspace has no code copies", ", ".join(leftovers) or "clean")

# --- связь из WSL ---------------------------------------------------------------------------
status = http_status(base_url.rstrip("/") + "/models")
report("PASS" if status == 200 else "FAIL", "WSL -> LM Studio", str(status))

status = http_status(mcp_url, method="POST")
report("PASS" if status == 401 else "FAIL", "WSL -> Stocker MCP (401 without token)", str(status))
