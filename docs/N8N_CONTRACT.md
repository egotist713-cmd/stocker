# STOCKER — КОНТРАКТ ИНТЕГРАЦИИ n8n

> **Статус:** 🟢 СОГЛАСОВАН 26 сентября 2026 (решения пользователя). Реализация — по §8.
>
> Связанные документы: паспорт §3A, §3B (права), §35M (роль n8n);
> `docs/SERVICE_CONTRACT.md`; `docs/RECOVERY.md`.

---

## 1. Роль n8n

n8n — **workflow engine**: расписания, очереди и повторы, интеграции,
уведомления. Бизнес-логики и состояния в n8n нет: истина — Stocker Core и
события SQLite (§3A.5). n8n только **инициирует** операции Stocker и читает
результаты.

| n8n может | n8n не может |
|---|---|
| читать состояние, очереди, статусы | approve / reject |
| запускать обработку новых файлов | менять решения человека (`approved`, `rejected`) |
| запускать pipeline (`asset.process`) | править metadata (`edit`), пересобирать (`rebuild`), эскалировать |
| запускать `metadata.build` и `metadata.gate` | выдавать себя за `human` или за агента |
| готовить уведомления | держать собственное «состояние» объектов |

Права n8n **уже**, чем у OpenClaw: без правки metadata и эскалации.

---

## 2. Топология

```text
n8n (Docker Desktop, контейнер, сеть bridge)
   │  POST http://172.26.192.1:8765/api/v1/<operation>
   │  Authorization: Bearer <STOCKER_N8N_TOKEN>
   ▼
Stocker HTTP-сервер (Windows, тот же процесс, что MCP для OpenClaw)
   │  токен → actor workflow:n8n (задаёт сервер)
   ▼
Service Layer (dispatch: allowlist workflow:n8n, защита решений человека)
   ▼
Stocker Core → SQLite events
```

- **Проверено 26.09.2026:** из контейнера `busybox` (сеть `bridge`)
  `http://172.26.192.1:8765/mcp` отвечает `401` без токена (доступен);
  `host.docker.internal:8765` недоступен — сервер слушает только адрес
  адаптера `vEthernet (WSL)`, и это сохраняется.
- Один долгоживущий Windows-процесс обслуживает и MCP (OpenClaw), и HTTP API
  (n8n): один writer SQLite, общая очередь изменяющих операций.
- Адрес WSL-хоста может смениться (как для OpenClaw): его проверяет
  `check_environment.ps1`, а в n8n он задаётся **одной** переменной окружения
  контейнера `STOCKER_URL`.

---

## 3. HTTP API для workflow

```http
POST /api/v1/{operation}
Authorization: Bearer <token>
Content-Type: application/json

{ ...параметры операции... }
```

- `{operation}` — имя из реестра (`review.queue`, `asset.process_file`, …).
- Тело — параметры операции (как в `SERVICE_CONTRACT.md` §3), `{}` если нет.
- Ответ — **envelope** `{api_version, operation, ok, asset_id, outcome, data, error}`
  с HTTP 200, в том числе при `ok: false` (решение принимает workflow по `ok` и `error.code`).
- HTTP 401 — нет или неверный токен; 400 — тело не JSON-объект; 404 — неизвестный путь.

### Токены и actor (задаёт сервер)

| Токен (`.env`) | Actor | Канал |
|---|---|---|
| `STOCKER_MCP_TOKEN` | `agent:openclaw` | только `/mcp` |
| `STOCKER_N8N_TOKEN` | `workflow:n8n` | только `/api/v1/*` |

Токен одного канала не работает на другом. `human` через HTTP недоступен.

---

## 4. Права `workflow:n8n` (allowlist в Service Layer)

| Операция | Разрешено | Ограничение |
|---|---|---|
| `asset.get`, `asset.list`, `asset.history`, `metadata.get`, `review.queue`, `operations.list`, `incoming.list` | ✅ | — |
| `asset.process_file` | ✅ | — |
| `asset.process` | ✅ | только `force=false` |
| `metadata.build` | ✅ | только `force=false` (создать или дозаполнить partial; существующие metadata не заменяются) |
| `metadata.gate` | ✅ | gate не меняет решения человека |
| `metadata.edit`, `metadata.rebuild`, `metadata.escalate` | ❌ `FORBIDDEN` | |
| `metadata.approve`, `metadata.reject` | ❌ `FORBIDDEN` | |

Allowlist — константа в коде; изменение — изменение контракта.

---

## 5. Новая операция чтения `incoming.list`

Файлы в `data/incoming/` поддерживаемых форматов, которых ещё нет в Stocker:

```json
{"total": 1, "items": [{"path": "data/incoming/IMG_1.jpg", "filename": "IMG_1.jpg", "file_size": 15478054, "duplicate_of": null}]}
```

- «Нет в Stocker» — нет asset с таким `source_path`.
- Для таких файлов считается SHA256: если такой hash уже зарегистрирован под
  другим именем, `duplicate_of` = id этого asset (обрабатывать не нужно).
- Только чтение; доступна всем actor.

---

## 6. Workflows v1

Хранятся в репозитории: `integrations/n8n/workflows/*.json` (экспорт n8n, **без
токенов** — токен в credential n8n `Stocker API`).

| Workflow | Триггер | Действия |
|---|---|---|
| `stocker-ingest` | каждые 5 минут | `incoming.list` → для каждого нового файла без `duplicate_of` → `asset.process_file` **последовательно** |
| `stocker-retry` | каждые 30 минут | `asset.list {"vision":"failed"}` → `asset.process`; `asset.list {"metadata_state":"draft"}` → `metadata.build` (дозаполнение partial). Не более N попыток на asset (считаются по событиям `AI/FAILED`, `METADATA_AI/FAILED` через `asset.history`) |
| `stocker-digest` | ежедневно + после ingest при изменениях | `review.queue` → сводка (`summary`) → `stocker-notify` |
| `stocker-notify` | вызывается другими | слой уведомлений: `{severity, title, text, data}` → канал |

### Слой уведомлений

Stocker **не знает** о мессенджерах. Канал — только внутри `stocker-notify`:

- v1: тестовый канал без внешней отправки (запись в журнал выполнения n8n);
- позже, по решению пользователя: Telegram, e-mail, MAX. Смена канала не
  затрагивает Stocker и остальные workflow.

---

## 7. Эксплуатация n8n

- Docker Desktop, контейнер `n8n` (`restart: unless-stopped`), UI только на
  `127.0.0.1:5678`, данные — именованный volume `n8n_data`.
- Переменная окружения контейнера `STOCKER_URL=http://172.26.192.1:8765`.
- Токен `STOCKER_N8N_TOKEN` — в credential n8n (Header Auth), не в workflow.
- Резервное копирование: volume `n8n_data` + экспорт workflows в репозиторий
  (добавить в `RECOVERY.md`).
- `check_environment.ps1` дополняется проверкой контейнера и доступа из него к Stocker.

---

## 8. Порядок реализации

1. Stocker: allowlist `workflow:n8n`, операция `incoming.list`, HTTP API
   `/api/v1/*` с токенами по каналам — тесты.
2. Production: новый токен `STOCKER_N8N_TOKEN` (пользователь), перезапуск
   задачи `Stocker MCP`, проверка из контейнера `busybox` с токеном n8n: чтение
   ✅, `metadata.edit` / `approve` → `FORBIDDEN`, токен OpenClaw на `/api` → 401.
3. n8n: загрузка образа и запуск контейнера — **с подтверждения пользователя**.
4. Workflows v1 + credential, тестовый канал уведомлений.
5. Проверка: новый файл в `data/incoming` → автоматически ingest → gate →
   сводка, всё видно в событиях Stocker с `actor=workflow:n8n`.

---

## 9. Вне рамок этого контракта

Image Enhancement (Topaz) — будущий этап (паспорт §2, §35V). Когда появится,
n8n сможет запускать соответствующие операции Stocker; решение
«улучшать или нет» принимает Stocker Core, а не n8n.
