# STOCKER — ВОССТАНОВЛЕНИЕ ОКРУЖЕНИЯ

> Что нужно сохранить и в каком порядке восстанавливать Stocker + LM Studio +
> OpenClaw после переустановки Windows или замены диска.
>
> Проверка после восстановления (и после любой перезагрузки):
> `F:\stock\stocker\scripts\check_environment.ps1` — все пункты должны быть `PASS`.
>
> Состояние на 26 сентября 2026 (включая n8n).

---

## 1. Что где хранится

| Что | Где | В git | Секрет | Как сохранить |
|---|---|---|---|---|
| Код Stocker, документы, skill, `ops/` | `F:\stock\stocker` | ✅ `origin` = `github.com/egotist713-cmd/stocker` | нет | `git push` (**регулярно**: на 26.09 не отправлено 28 коммитов) |
| Зависимости Python | `requirements.txt`, `requirements.lock.txt` (точные версии), `requirements-dev.txt` | ✅ | нет | в git; Python **3.12.7** (`C:\Python312`) |
| База состояния | `data\db\stocker.db` | ❌ | нет | копия SQLite (§2) |
| Исходные фотографии | `data\incoming\` (и далее `working`, `approved`, `rejected`) | ❌ | нет | копия каталога. **Без файлов asset'ы станут `SOURCE_MISSING`** (проверка SHA256) |
| Журналы | `logs\` | ❌ | нет | по желанию |
| Секреты Stocker | `.env`: `OPENAI_API_KEY`, `STOCKER_MCP_TOKEN`, `STOCKER_MCP_HOST=wsl` | ❌ | **да** | менеджер паролей / зашифрованная копия |
| Модель | `F:\ai\models\unsloth\Qwen3-VL-8B-Instruct-GGUF\` (§4) | ❌ | нет | копия или повторная загрузка с проверкой SHA256 |
| Настройки LM Studio | `%USERPROFILE%\.lmstudio\settings.json`, `.internal\http-server-config.json`, настройки загрузки модели | копии в `ops\lmstudio\` ✅ | нет | в git (`ops/lmstudio`) |
| WSL-настройки Windows | `%USERPROFILE%\.wslconfig` | ❌ (содержимое в §5) | нет | по документу |
| Задачи Планировщика | `Stocker MCP`, `OpenClaw WSL keep-alive`, `LMStudioAutoServer` | ❌ (команды в §5) | нет | по документу |
| Дистрибутив OpenClaw | WSL `OpenClawGateway`: OpenClaw 2026.9.5, `/etc/wsl.conf`, служба `openclaw-gateway` (systemd user) | ❌ | **да** (внутри конфиг с токенами) | `wsl --export` (§2) |
| Конфиг и память OpenClaw | `~/.openclaw/` (`openclaw.json`, `agents/`, `workspace/`, `memory/`) | ❌ | **да**: `openclaw.json` содержит Bearer-токен Stocker MCP и учётные данные провайдеров | `openclaw backup create` (§2) |
| Skill Stocker | `~/.openclaw/workspace/skills/stocker/SKILL.md` | исходник ✅ `integrations/openclaw/skills/stocker/SKILL.md` | нет | из git |
| n8n: workflows | `integrations/n8n/workflows/*.json` (без токенов) | ✅ | нет | из git; развёртывание `scripts/deploy_n8n_workflows.ps1` |
| n8n: данные | Docker volume `n8n_data` (владелец, credential «Stocker API», история выполнений, **ключ шифрования n8n**) | ❌ | **да** | копия volume (§2) |
| Секрет n8n-канала | `.env`: `STOCKER_N8N_TOKEN` | ❌ | **да** | вместе с `.env` |

---

## 2. Резервное копирование

Команды PowerShell. Каталог копий — например, `D:\backup\stocker\<дата>` (не на диске `F:`).

```powershell
$B = "D:\backup\stocker\$(Get-Date -Format yyyy-MM-dd)"; New-Item -ItemType Directory -Force $B | Out-Null
```

1. **Код** — отправить коммиты на GitHub:

   ```powershell
   git -C F:\stock\stocker push origin main
   ```

2. **База** — согласованная копия средствами SQLite (безопасно при работающем MCP):

   ```powershell
   F:\stock\stocker\.venv\Scripts\python.exe -c "import sqlite3,sys; s=sqlite3.connect(r'F:\stock\stocker\data\db\stocker.db'); d=sqlite3.connect(sys.argv[1]); s.backup(d); d.close(); print('ok')" "$B\stocker.db"
   ```

3. **Фотографии и журналы:**

   ```powershell
   robocopy F:\stock\stocker\data "$B\data" /E /XD db
   robocopy F:\stock\stocker\logs "$B\logs" /E
   ```

4. **Секреты Stocker** — `F:\stock\stocker\.env` в менеджер паролей или зашифрованное хранилище.

5. **OpenClaw** — архив конфигурации, учётных данных, сессий и workspace (содержит секреты):

   ```powershell
   wsl -d OpenClawGateway --exec bash -lc "openclaw backup create --verify --output /mnt/d/backup/stocker"
   ```

   Раз в месяц / перед крупными изменениями — дистрибутив целиком:

   ```powershell
   wsl --export OpenClawGateway "$B\OpenClawGateway.tar"
   ```

6. **Модель** (≈ 6,5 GB) — копия каталога или повторная загрузка (§4).

7. **n8n** — volume с данными (содержит ключ шифрования credential):

   ```powershell
   docker run --rm -v n8n_data:/data -v "${B}:/backup" busybox:latest tar czf /backup/n8n_data.tgz -C /data .
   ```

---

## 3. Порядок восстановления

1. **Windows, драйвер NVIDIA, WSL 2.** Проверено на WSL 2.7.14, Windows 11 build 26200.
2. **Python 3.12.7** в `C:\Python312`.
3. **Код:** `git clone https://github.com/egotist713-cmd/stocker.git F:\stock\stocker`.
4. **Окружение Python:**

   ```powershell
   cd F:\stock\stocker; C:\Python312\python.exe -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.lock.txt
   ```

5. **Данные:** вернуть `data\db\stocker.db` и `data\incoming\` (и прочие каталоги `data\`) из копии.
6. **`.env`** — из хранилища секретов (`OPENAI_API_KEY`, `STOCKER_MCP_TOKEN`, `STOCKER_MCP_HOST=wsl`).
7. **LM Studio** — установить, затем:
   - папка моделей `F:\ai\models` (`settings.json` → `downloadsFolder`);
   - файлы модели (§4), проверить SHA256;
   - сервер: `ops\lmstudio\http-server-config.json` (порт 1234, `0.0.0.0`, JIT, автозапуск) — выставить в UI или скопировать в `%USERPROFILE%\.lmstudio\.internal\`;
   - настройки загрузки модели по умолчанию (§4) — в UI (My Models → шестерёнка → сохранить) или скопировать `ops\lmstudio\Qwen3-VL-8B-Instruct-Q5_K_M.load-config.json` в `%USERPROFILE%\.lmstudio\.internal\user-concrete-model-default-config\unsloth\Qwen3-VL-8B-Instruct-GGUF\Qwen3-VL-8B-Instruct-Q5_K_M.gguf.json`;
   - в настройках LM Studio: запуск при входе и локальный сервис (`enableLocalService`); разрешить LM Studio в firewall при первом запуске. Автозапуск работает через ключ `HKCU\...\Run` → `LM Studio.exe --run-as-service` (задача `LMStudioAutoServer` на этой машине устаревшая и не используется).
8. **WSL и OpenClaw:**

   ```powershell
   wsl --import OpenClawGateway D:\wsl\OpenClawGateway "<копия>\OpenClawGateway.tar"
   ```

   либо новая установка OpenClaw 2026.9.5 и `openclaw backup restore`. В `/etc/wsl.conf` должно быть `[interop] enabled=false`, `appendWindowsPath=false`, `[boot] systemd=true`, `[user] default=openclaw`.
9. **`.wslconfig`** (§5) — **без** `networkingMode=mirrored`.
10. **Адрес WSL-хоста.** После новой установки подсеть NAT может отличаться от `172.26.192.1`. Узнать адрес:

    ```powershell
    (Get-NetIPAddress -AddressFamily IPv4 | Where-Object InterfaceAlias -like 'vEthernet (WSL*').IPAddress
    ```

    и обновить в OpenClaw оба адреса (LM Studio и Stocker MCP), §6.
11. **Skill:** скопировать `integrations/openclaw/skills/stocker/SKILL.md` в `~/.openclaw/workspace/skills/stocker/`.
12. **Задачи Планировщика** (§5); перезайти в Windows или запустить задачи вручную.
13. **n8n** (§7): Docker Desktop → образ по digest → контейнер → восстановить
    volume `n8n_data` из копии (или создать владельца и credential заново) →
    `scripts\deploy_n8n_workflows.ps1` → включить workflows в UI.
14. **Проверка:** `scripts\check_environment.ps1` → все `PASS`.

---

## 4. Модель: файлы и параметры загрузки

Репозиторий: `unsloth/Qwen3-VL-8B-Instruct-GGUF` (Hugging Face).

| Файл | SHA256 |
|---|---|
| `Qwen3-VL-8B-Instruct-Q5_K_M.gguf` (5,45 GiB) | `bb6d45711239c508c18b6a67f00dd094a41add64e8639f8554738bfeccf5a3bc` |
| `mmproj-F16.gguf` (1,08 GiB) | `d406d03ebabefdef86a2c86bf0c1b65f9e046f7a81c218f25de4931b46a07fc4` |

В папке модели должен лежать **только** `mmproj-F16` (не F32: как LM Studio выбирает между двумя mmproj, не определено).
`mmproj-F32.gguf` (`1b9824e7…4ba0`) хранится в `F:\ai\model-backup\` для отката.

Параметры загрузки **по умолчанию** (их используют JIT-загрузки Stocker и OpenClaw):

| Параметр | Значение |
|---|---|
| Context Length | 32768 |
| K / V Cache Quantization | `q8_0` / `q8_0` (требует flash attention) |
| GPU Offload | максимум |
| Parallel sessions | 1 |

Ожидаемо: `llama-server` ≈ 9,4–9,6 GiB VRAM из 12 (RTX 3080 Ti). В логе загрузки
LM Studio (`%USERPROFILE%\.lmstudio\server-logs\`): `loaded multimodal model, '.../mmproj-F16.gguf'`, `n_ctx_slot = 32768`.

---

## 5. Windows: `.wslconfig` и задачи Планировщика

`%USERPROFILE%\.wslconfig`:

```ini
[wsl2]
memory=16GB
swap=4GB
```

Режим сети — NAT (по умолчанию). `networkingMode=mirrored` на этой машине не работает (`0x8007054f`, паспорт §35P).

Задачи (PowerShell, от имени пользователя):

```powershell
Register-ScheduledTask -TaskName "OpenClaw WSL keep-alive" -Action (New-ScheduledTaskAction -Execute "conhost.exe" -Argument "--headless wsl.exe -d OpenClawGateway --exec sleep infinity") -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries)
```

```powershell
Register-ScheduledTask -TaskName "Stocker MCP" -Action (New-ScheduledTaskAction -Execute "conhost.exe" -Argument "--headless F:\stock\stocker\scripts\start_mcp_http.cmd" -WorkingDirectory "F:\stock\stocker") -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries)
```

LM Studio запускается ключом `Run` (`--run-as-service`), а не задачей: задача
`LMStudioAutoServer` на этой машине ссылается на несуществующий путь и
падает (`0x80070002`) — её можно удалить:

```powershell
Unregister-ScheduledTask -TaskName "LMStudioAutoServer" -Confirm:$false
```

Все задачи запускаются **при входе пользователя** в Windows.

---

## 6. OpenClaw: значимые настройки (без секретов)

| Путь в `openclaw.json` | Значение |
|---|---|
| `agents.defaults.model.primary` | `lmstudio/qwen3-vl-8b-instruct` |
| `models.providers.lmstudio.baseUrl` | `http://<адрес WSL-хоста>:1234/v1` |
| `models.providers.lmstudio.models[]` (`qwen3-vl-8b-instruct`) | `{"id":"qwen3-vl-8b-instruct","name":"Qwen3 VL 8B Instruct","reasoning":false,"input":["text","image"],"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0},"contextWindow":32768,"maxTokens":8192,"compat":{"supportsTools":true},"api":"openai-completions"}` |
| `mcp.servers.stocker` | `url` = `http://<адрес WSL-хоста>:8765/mcp`, `transport` = `streamable-http`, `headers.Authorization` = `Bearer <STOCKER_MCP_TOKEN из .env>`, таймаут 600 s |

Регистрация сервера Stocker (PowerShell; токен берётся из `.env` и не выводится):

```powershell
$token = (Select-String -Path F:\stock\stocker\.env -Pattern '^STOCKER_MCP_TOKEN=(.+)$').Matches[0].Groups[1].Value.Trim(); wsl -d OpenClawGateway --exec openclaw mcp add stocker --url http://172.26.192.1:8765/mcp --transport streamable-http --header "Authorization=Bearer $token" --timeout 600; Remove-Variable token
```

Смена адреса WSL-хоста (если `check_environment.ps1` показывает несовпадение):

```powershell
wsl -d OpenClawGateway --exec openclaw config set models.providers.lmstudio.baseUrl http://<новый адрес>:1234/v1
```

и повторная регистрация `stocker` с новым адресом (`openclaw mcp unset stocker`, затем команда выше).

**Смена токена:** новый `STOCKER_MCP_TOKEN` в `.env` → перезапуск задачи `Stocker MCP` → повторная регистрация `stocker` в OpenClaw.

---

## 7. n8n (Docker Desktop)

Образ **n8n 2.40.7**, запуск строго по digest:

```powershell
docker volume create n8n_data
```

```powershell
docker run -d --name n8n --restart unless-stopped -p 127.0.0.1:5678:5678 -v n8n_data:/home/node/.n8n -e GENERIC_TIMEZONE=Asia/Barnaul -e TZ=Asia/Barnaul -e STOCKER_URL=http://172.26.192.1:8765 -e N8N_BLOCK_ENV_ACCESS_IN_NODE=false -e N8N_DIAGNOSTICS_ENABLED=false -e N8N_VERSION_NOTIFICATIONS_ENABLED=false n8nio/n8n@sha256:ffeb52485f78b1b06c9a832205853cf75da72a07a514c9a27724df85979d6c34
```

- UI только на `http://127.0.0.1:5678` (не виден из LAN).
- В окружении контейнера секретов нет. `STOCKER_URL` — адрес WSL-хоста (как у
  OpenClaw); при смене адреса контейнер пересоздаётся с новым значением
  (данные в volume сохраняются).
- Восстановление volume из копии:

  ```powershell
  docker run --rm -v n8n_data:/data -v "<копия>:/backup" busybox:latest sh -c "cd /data && tar xzf /backup/n8n_data.tgz"
  ```

- Credential «Stocker API» (Header Auth: `Authorization` = `Bearer <STOCKER_N8N_TOKEN>`)
  создаёт пользователь в UI; workflows ссылаются на него по id (подставляет
  `deploy_n8n_workflows.ps1`).
- Docker Desktop запускается при входе в Windows (`AutoStart`), контейнер —
  политикой `unless-stopped`.
- **Публикация (active) в n8n 2.x.** Подпроцесс `stocker-notify` должен быть
  опубликован, иначе вызовы через Execute Workflow падают («Workflow is not
  active»). Расписания `stocker-ingest` / `-retry` / `-digest` работают только
  опубликованными. CLI-публикация требует перезапуска n8n:

  ```powershell
  docker exec n8n n8n publish:workflow --id=StockerNotify001; docker restart n8n
  ```

- **Разовый запуск** (у workflow есть вход «Run on demand»; отдельный порт
  брокера, чтобы не конфликтовать с работающим сервером):

  ```powershell
  docker exec -e N8N_RUNNERS_BROKER_PORT=5690 n8n n8n execute --id StockerIngest001
  ```

---

## 8. Известные эксплуатационные риски

- **Адрес WSL-хоста (NAT) может смениться** — OpenClaw потеряет LM Studio и Stocker MCP. Обнаруживается `check_environment.ps1` (FAIL «url host»), исправляется по §6.
- **Всё стартует при входе пользователя**, не при загрузке Windows.
- **Обновление LM Studio** может сбросить настройки загрузки модели — проверять `check_environment.ps1` после обновления.
- **Неотправленные коммиты** — единственная копия кода на `F:` до `git push`.
- **n8n:** без копии volume `n8n_data` credential придётся создать заново (он
  зашифрован ключом из этого volume).
- **Смена адреса WSL-хоста** затрагивает и n8n (`STOCKER_URL`).
