# STOCKER — КОНТРАКТ SERVICE LAYER

> **Статус:** 🟢 СОГЛАСОВАН 26 сентября 2026. Реализованы шаги 0–4 §9 (review
> gate, service layer, JSON CLI, production-проверка). Шаг 5 (MCP) — следующий.
>
> **Версия API:** `1`, от 25 сентября 2026.
>
> Связанные документы: паспорт §3A (принципы), §14 (события),
> `docs/METADATA_CONTRACT.md` (включая review gate `metadata-v2`, §6A).

---

## 1. Цель

Внешний агент (OpenClaw), workflow (n8n) или человек через CLI должен уметь:

1. получить состояние asset;
2. выполнить **разрешённую** операцию;
3. получить структурированный, машиночитаемый результат.

Service layer — единственная точка входа для внешних вызовов. Он **не содержит
бизнес-логики**: он вызывает существующие операции (`worker`,
`app.metadata`), валидирует параметры, проверяет права и упаковывает результат.

Не меняется:

- review-логика metadata и `metadata_builder`;
- существующие CLI `python -m app.worker` и `python -m app.metadata`;
- `assets.status`, схема БД, `AIAnalysis`;
- человеческий approve/reject (`approved`/`rejected`) — только человек.
  Автоматический проход низкорискованных объектов — это **отдельное** состояние
  `auto_approved`, которое устанавливает только детерминированный review gate
  (`METADATA_CONTRACT.md` §6A). Никакой вызов API не может установить его напрямую.

```text
OpenClaw (MCP)   n8n (Execute Command → позже HTTP)   человек (CLI)
       │                    │                             │
       └──────────┬─────────┴─────────────┬───────────────┘
                  ▼                       ▼
          адаптер MCP (этап 4)     python -m app.api   (JSON CLI)
                  │                       │
                  └───────────┬───────────┘
                              ▼
                 app.service: registry + dispatch
                 (параметры, права, envelope, actor)
                              │
            ┌─────────────────┼──────────────────┐
            ▼                 ▼                  ▼
       app.worker        app.metadata        read-модели
                              │             (asset view, events)
                              ▼
                           SQLite
```

---

## 2. Envelope ответа

Каждая операция возвращает один и тот же конверт:

```json
{
  "api_version": "1",
  "operation": "metadata.edit",
  "ok": true,
  "asset_id": 5,
  "outcome": "EDITED",
  "data": { },
  "error": null
}
```

| Поле | Тип | Смысл |
|---|---|---|
| `api_version` | str | версия контракта |
| `operation` | str | имя операции |
| `ok` | bool | операция выполнила свою работу |
| `asset_id` | int \| null | |
| `outcome` | str \| null | исход существующей операции (`AI_PASSED`, `DRAFTED`, `METADATA_EXISTS`, ...) без переименований |
| `data` | obj \| null | результат (§4) |
| `error` | obj \| null | `{code, message}` |

Коды ошибок:

| code | Когда |
|---|---|
| `INVALID_PARAMS` | параметры не прошли валидацию |
| `UNKNOWN_OPERATION` | нет такой операции |
| `FORBIDDEN` | операция недоступна этому actor (§5) |
| `ASSET_NOT_FOUND`, `VISION_MISSING`, `METADATA_MISSING`, `INVALID_TRANSITION`, `INVALID_EDIT` | из `app.metadata` без изменений |
| `FILE_NOT_ADDED` | ingest не добавил файл (дубликат, формат, вне проекта) |
| `INTERNAL` | непредвиденное исключение; текст — в `message`, stack trace — только в логах |

Исходы, которые не выполнили работу (`AI_FAILED`, `SOURCE_INVALID`,
`METADATA_AI_FAILED`), возвращаются с `ok: false`, `error: null` и своим
`outcome`: операция отработала, но результата нет. Partial draft — `ok: true`.

---

## 3. Операции v1

Каждая операция описана в реестре: имя, описание, Pydantic-модель параметров
(→ JSON Schema), уровень доступа, признак изменения состояния. Из реестра
генерируются CLI, манифест и будущие MCP- и HTTP-адаптеры: **одно описание — много
транспортов**.

### Чтение (`read`)

| Операция | Параметры | Возвращает |
|---|---|---|
| `asset.get` | `asset_id` | asset view (§4.1) |
| `asset.list` | `qc?`, `vision?`, `metadata_state?` (`none`/`draft`/`auto_approved`/`human_review`/`approved`/`rejected`), `ready?`, `limit=50`, `offset=0` | список кратких asset view |
| `review.queue` | `limit=50`, `offset=0` | `summary` по всему каталогу (`total_assets`, `ready`, `by_metadata_state`, `problem_assets` с причинами) + `items` — assets в `human_review` с причинами gate (очередь человека). Один вызов отвечает «что происходит» |
| `asset.history` | `asset_id`, `stage?` | события, `message` распарсен из JSON (старые текстовые — как строка) |
| `metadata.get` | `asset_id` | `metadata_json` |
| `operations.list` | — | манифест операций с JSON Schema параметров |

### Pipeline (`pipeline`) — изменяют состояние, без review-решений

| Операция | Параметры | Вызывает |
|---|---|---|
| `asset.process_file` | `path` (внутри проекта) | `worker`: ingest → QC → Vision → metadata draft |
| `asset.process` | `asset_id`, `force=false` | `worker.process_asset` |
| `metadata.build` | `asset_id`, `force=false` | `app.metadata.build` |
| `metadata.rebuild` | `asset_id` | `app.metadata.rebuild` |
| `metadata.edit` | `asset_id`, `title?`, `description?`, `keywords?`, `add_keywords?`, `remove_keywords?` | `app.metadata.edit` (как CLI), затем gate |
| `metadata.gate` | `asset_id` | повторная оценка review gate (детерминированно; `approved`/`rejected` не трогает) |
| `metadata.escalate` | `asset_id`, `reason` | → `human_review` (`MANUAL_ESCALATION`). Агент может только **поднять** риск, но не снять его |

### Review (`review`) — решения человека

| Операция | Параметры | Вызывает |
|---|---|---|
| `metadata.approve` | `asset_id`, `allow_partial=false`, `confirm_claims=false` | `app.metadata.approve` |
| `metadata.reject` | `asset_id`, `reason` | `app.metadata.reject` |

---

## 4. Данные

### 4.1. Asset view (`asset.get`)

```json
{
  "id": 5,
  "filename": "IMG_20260911_130437.jpg",
  "source_path": "data/incoming/IMG_20260911_130437.jpg",
  "file_hash": "b1816473...",
  "width": 8192, "height": 6144, "file_size": 14638264,
  "status": "PASSED",
  "created_at": "...", "updated_at": "...",

  "pipeline": {
    "source": "ok",
    "qc": "passed",
    "vision": "done",
    "metadata": "auto_approved",
    "metadata_completeness": "full",
    "ready": true,
    "review_reasons": []
  },

  "qc": { "passed": true, "errors": [], "warnings": [], "metrics": { } },
  "vision": { "analysis": { }, "provenance": { "event_id": 17, "model": "...", "prompt_version": "local-v2" } },
  "metadata": { "state": "draft", "fields": { }, "validation": { }, "grounding": { } },

  "allowed_actions": [
    {"operation": "metadata.edit", "access": "pipeline"},
    {"operation": "metadata.approve", "access": "review"}
  ]
}
```

**`pipeline`** — производное представление из `assets` и событий.
`assets.status` не меняется и отдаётся как есть. Это прообраз будущей явной
модели статусов, но без миграции:

| Ключ | Значения | Источник |
|---|---|---|
| `source` | `ok` \| `changed` \| `missing` \| `unknown` | последнее `SOURCE/INVALID` новее последнего `QC/*` → `changed`/`missing`; иначе `ok`, если есть QC, или `unknown` |
| `qc` | `pending` \| `passed` \| `failed` | `qc_result` |
| `vision` | `pending` \| `done` \| `failed` | `ai_result`; последнее `AI/FAILED` без последующего `AI/PASSED` → `failed` |
| `metadata` | `none` \| `draft` \| `auto_approved` \| `human_review` \| `approved` \| `rejected` | `metadata_json.state` |
| `metadata_completeness` | `full` \| `partial` \| null | `metadata_json.completeness` |
| `ready` | bool | `metadata ∈ {auto_approved, approved}`: объект может двигаться дальше (экспорт) без человека |
| `review_reasons` | list[code] | `review_gate.reasons` для `human_review`, иначе `[]` |

**`allowed_actions`** — операции, которые сейчас имеют смысл для asset, с
уровнем доступа. Правила детерминированы. Агент может планировать по ним, не
зная внутренней логики. Список — подсказка: окончательная проверка всегда в
самой операции.

### 4.2. Результаты изменяющих операций

`data` содержит обновлённый asset view (§4.1). Агенту не нужен второй
вызов, чтобы увидеть новое состояние.

---

## 5. Actor и права

Каждый вызов выполняется от имени **actor**: `human`, `agent:<name>`
(например, `agent:openclaw`) или `workflow:<name>` (например, `workflow:n8n`).

| Уровень | human | agent:* | workflow:* |
|---|---|---|---|
| `read` | ✅ | ✅ | ✅ |
| `pipeline` | ✅ | ✅ | ✅ |
| `review` (approve/reject) | ✅ | ❌ `FORBIDDEN` | ❌ `FORBIDDEN` |

- approve и reject (`approved`/`rejected`) — **только человек**.
- `auto_approved` устанавливает **только review gate** по детерминированным
  правилам `gate-v1`. Агент или workflow могут запустить pipeline или gate, но
  не могут задать решение. Результат определяют правила, а не вызывающий.
- Агент может править metadata (`metadata.edit`). Правка проходит gate на
  общих основаниях, actor пишется в события.
- Агент может **поднять** риск (`metadata.escalate` → `human_review`), но не
  может его снять: `MANUAL_ESCALATION` снимает только решение человека.
- Политика — константа в коде v1, без конфигурации. Её изменение — изменение
  контракта.
- Actor по умолчанию: CLI `app.api` — `human`; MCP-адаптер — `agent:openclaw`,
  задаётся адаптером, а не параметром вызова (агент не может объявить себя
  человеком).

### Actor в событиях

Изменяющие операции через service layer добавляют `"actor": "<actor>"` в
JSON-сообщение каждого события, которое они создают. Реализация:
`app.database.db.acting_as(actor)` (contextvars) — `add_event` и
`insert_event` дописывают ключ, если actor задан. Текстовые сообщения
(`INGEST/DONE`) не меняются. QC пишет событие через `insert_event` (`METADATA/EDITED`,
`METADATA/DRAFTED`, `AI/PASSED`, ...). Это **аддитивное** расширение контракта
событий. У событий из существующих CLI (`app.worker`, `app.metadata`) ключа
`actor` нет, что означает «человек через прямой CLI».

---

## 6. JSON CLI (`python -m app.api`)

Новая команда. Существующие CLI не меняются.

```powershell
python -m app.api operations.list
python -m app.api asset.get --params '{"asset_id": 5}'
python -m app.api metadata.edit --params '{"asset_id": 5, "remove_keywords": ["construction site"]}'
echo '{"asset_id": 5}' | python -m app.api asset.history --params -     # params из stdin (n8n)
```

- stdout — **только** envelope JSON в UTF-8, одна строка (или `--pretty`);
  прогресс worker'а (`print`) уходит в stderr;
- exit code: `0` при `ok: true`, `1` при `ok: false`, `2` при ошибке
  использования CLI;
- actor — `human`, флаг `--actor workflow:n8n` для n8n. Агенты используют
  MCP-адаптер, а не этот CLI.

---

## 7. Основа для n8n и OpenClaw (этап 4)

| Клиент | Транспорт v1 | Позже |
|---|---|---|
| **OpenClaw** | MCP stdio-сервер поверх реестра (`openclaw mcp add`), actor `agent:openclaw`, операции `read` + `pipeline` | MCP over HTTP |
| **n8n** | Execute Command → `python -m app.api ... --actor workflow:n8n` | HTTP (FastAPI или MCP HTTP) поверх того же реестра |

**Ограничение окружения — одна среда записи в SQLite.** БД лежит на
`F:` (NTFS). OpenClaw работает в WSL и видит её через `/mnt/f`. Блокировки SQLite
на таком монтировании ненадёжны при одновременной записи из Windows и WSL.
Поэтому:

- MCP-сервер и CLI запускаются **в Windows** (`.venv`);
- **проверено 26.09.2026: stdio через interop невозможен и не нужен.** В
  дистрибутиве `OpenClawGateway` interop выключен намеренно
  (`/etc/wsl.conf`: `[interop] enabled=false`, `appendWindowsPath=false`):
  агент изолирован от Windows-программ. Включать interop нельзя — это дало бы
  агенту доступ ко всем Windows-программам, а не только к Stocker;
- **поэтому для OpenClaw нужен сетевой транспорт**: один долгоживущий
  Windows-процесс с MCP streamable HTTP поверх того же реестра, доступный из
  WSL. Тот же процесс позже обслуживает n8n;
- **работающая конфигурация (26.09.2026, паспорт §35P):** WSL в режиме NAT,
  `python -m app.service.mcp_http` с `STOCKER_MCP_HOST=wsl` слушает адрес
  адаптера `vEthernet (WSL)` (`172.26.192.1:8765/mcp`), обязательный Bearer-токен.
  В OpenClaw сервер зарегистрирован как `stocker` (`openclaw mcp add`).
  Правило firewall не понадобилось; из LAN сервер недоступен (слушает только
  адрес адаптера WSL). Режим mirrored на этой машине не работает
  (`0x8007054f`, откат выполнен);
- stdio-сервер (`python -m app.service.mcp_server`) остаётся для локальных
  Windows-клиентов MCP и тестов.

---

## 8. Файлы

| Файл | Что |
|---|---|
| `app/service/registry.py` | описание операций: имя, модель параметров, уровень, mutating |
| `app/service/views.py` | asset view, `pipeline`, `allowed_actions`, history, review queue |
| `app/review_gate.py` | чистый review gate `gate-v1` (`METADATA_CONTRACT.md` §6A) |
| `app/service/operations.py` | обработчики: вызов `worker` и `app.metadata` |
| `app/service/__init__.py` | `dispatch(operation, params, actor) -> envelope` |
| `app/service/errors.py` | `ServiceError(code, message)` |
| `app/api.py` | JSON CLI |
| `app/metadata.py`, `app/worker.py` | + необязательный параметр `actor` (передаётся в сообщения событий); поведение без него не меняется |
| `tests/test_service_*.py` | envelope, права, views, CLI |

| `app/service/mcp_server.py` | MCP stdio-сервер: инструменты из реестра (`read` + `pipeline`), actor задаёт сервер (`STOCKER_MCP_ACTOR`, только `agent:*`/`workflow:*`), envelope как text + structured content |

---

## 9. Порядок реализации

0. **review gate** (`metadata-v2`): `app/review_gate.py` (чистый) + тесты →
   интеграция в `app/metadata.py` (`gate`, `escalate`, gate после
   build/rebuild/edit, события `GATED`/`ESCALATED`) → `gate` для asset 3–6 на
   production с проверкой ожидаемых решений (§6A.4);
1. read-модели и `dispatch` для чтения (`asset.get/list/history`, `review.queue`, `metadata.get`, `operations.list`);
2. изменяющие операции + права actor + `actor` в событиях;
3. JSON CLI `app.api`;
4. production-проверка: сценарий «агента» только через `app.api`, от
   `asset.list` до `metadata.edit` и `escalate`; проверка `FORBIDDEN` для approve;
   проверка, что `auto_approved` нельзя получить иначе чем через gate;
5. отдельно: MCP-адаптер и n8n.
