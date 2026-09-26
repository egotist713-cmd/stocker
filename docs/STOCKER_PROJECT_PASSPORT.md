# STOCKER — ПАСПОРТ И БОРТОВОЙ ЖУРНАЛ ПРОЕКТА

> **Назначение:** единый переносимый документ с архитектурой, фактическим состоянием, принятыми решениями, важными ограничениями и историей значимых этапов разработки Stocker.
>
> **Главный источник истины для кода:** актуальная рабочая директория `F:\stock\stocker`. Этот документ не заменяет фактический код. Если документ и код расходятся, приоритет имеет фактическое состояние проекта, а расхождение нужно зафиксировать в журнале.
>
> **Последнее обновление:** 25 сентября 2026.
>
> **Язык:** русский. Имена файлов, классов, функций, полей БД, команд, model ID и API endpoints сохраняются в оригинальном виде.

---

# ЧАСТЬ I. АРХИТЕКТУРА ПРОЕКТА

## 1. Идея Stocker

Stocker — локальная система автоматизации обработки и подготовки фотографий для фотостоков.

Главная архитектурная идея — разделить:

- агентное принятие решений;
- workflow/orchestration;
- детерминированную бизнес-логику;
- состояние;
- AI-сервисы;
- внешние инструменты.

Целевая схема:

```text
                           USER
                            │
                            ▼
                     ┌─────────────┐
                     │  OpenClaw   │
                     │    AGENT    │
                     └──────┬──────┘
                            │
                     decisions / tools
                            │
             ┌──────────────┼──────────────┐
             │              │              │
             ▼              ▼              ▼
         Stocker           n8n          Browser
         Core/API        Workflow       Automation
             │
             │
      ┌──────┴──────────────────────────────┐
      │                                     │
      ▼                                     ▼
Deterministic tools                    AI services
      │                                     │
      │                            ┌────────┼────────┐
      │                            │        │        │
      ▼                            ▼        ▼        ▼
  QC / SQLite                  Vision    Brain    Expert
  Topaz                        local     local    cloud
  Files
  Metadata
  ComfyUI
```

### Роли компонентов

### OpenClaw

**Мозг / агент.**

OpenClaw принимает решения, пользуется инструментами и управляет последовательностью действий.

Он не должен заменять детерминированную бизнес-логику Stocker и не должен произвольно писать в SQLite.

---

### n8n

**Workflow engine.** Роль подтверждена 26.09.2026 (§35M): расписания, очереди,
retries, интеграции. Подключается после service layer, JSON CLI и MCP.

Предназначен для:

- расписаний;
- очередей;
- retries;
- ожиданий;
- повторяемых цепочек;
- ветвления;
- долгоживущих workflow;
- orchestration.

n8n не должен заменять ядро Stocker.

---

### Stocker Python Core / API

**Детерминированная бизнес-логика.**

Здесь должны находиться операции, результат которых должен быть предсказуемым и тестируемым:

- ingest;
- duplicate detection;
- QC;
- запись состояния;
- сохранение AI-результатов;
- работа с файлами;
- metadata;
- сравнение изображений;
- бизнес-правила;
- будущие decision functions.

AI может предоставлять данные для решений, но Python-код определяет, как эти данные изменяют состояние системы.

---

### SQLite

**Источник состояния.**

SQLite хранит состояние обработки:

- assets;
- QC;
- AI results;
- processing events;
- будущие статусы и metadata.

Состояние должно быть восстанавливаемым и проверяемым независимо от агента.

---

### Vision model

**Глаза.**

Отвечает на вопросы вроде:

> Что реально изображено на фотографии?

Текущая production vision model:

```text
qwen3-vl-8b-instruct
```

---

### Brain model

**Локальное рассуждение / tool use.**

Отдельная роль от vision.

В экспериментах OpenClaw использовалась:

```text
qwen3.8-9b-distill
```

**Уточнено 26.09.2026 (§35Q):** это только эксперимент agent loop OpenClaw.
Решение проекта — одна локальная модель `qwen3-vl-8b-instruct` для всех ролей;
Stocker `qwen3.8` не использует.

Не путать её с:

```text
qwen3-vl-8b-instruct
```

---

### Cloud LLM / Expert

**Экспертный слой.**

Используется там, где локальной модели недостаточно.

В проекте существует:

```text
app/ai/openai_analyzer.py
```

Это отдельный provider и не текущий основной AI-путь.

---

### Инструменты

Планируемые/возможные инструменты:

- Topaz;
- ComfyUI;
- Files;
- Google Drive;
- Browser;
- Stock APIs;
- другие внешние сервисы.

---

# 2. Целевой принцип работы

Stocker развивается поэтапно.

Первый уровень:

```text
Фотография
    ↓
INGEST
    ↓
SQLite / Asset
    ↓
QC
    ↓
Local Vision AI
    ↓
AIAnalysis
    ↓
SQLite / ai_result
```

Позже:

```text
AIAnalysis
    ↓
Decision Engine
    ↓
metadata / enhancement / approval
    ↓
export / stock platforms
```

А ещё позже:

```text
OpenClaw
    ↓
решение
    ↓
Stocker Core / n8n / Browser / Tools
```

Не следует сразу реализовывать весь верхний уровень автономности.

### Будущий этап: Image Enhancement (зафиксирован 26.09.2026, §35V)

```text
source
 → QC
 → enhancement decision      (Stocker Core, детерминированно)
 → Topaz (при необходимости)
 → QC повторно
 → Vision
 → Metadata
 → Review
 → Export
```

- **Оригиналы не изменяются.** Topaz создаёт производные (derivative) файлы; asset хранит
  связь оригинал → производный файл (provenance как у AI: инструмент, версия,
  параметры, время).
- Решение «улучшать или нет» принимает Stocker Core по правилам (как review
  gate), а не агент или workflow. n8n/OpenClaw только запускают операцию.
- Пока **не реализуется**.
- Кратко (решение 26.09.2026, §35X): **QC → Topaz (если требуется) → QC → Vision**.

**Экспорт на стоки** — следующий слой после стабильной работы pipeline; пока
не реализуется (§35X).

---

# 3. Основной принцип безопасности архитектуры

LLM/VLM не должна напрямую и бесконтрольно менять внутреннее состояние Stocker.

Правильный путь:

```text
AI
 ↓
структурированный результат
 ↓
Python validation
 ↓
детерминированная бизнес-логика
 ↓
SQLite
```

---

# 3A. Принципы надёжности и расширяемости

Приняты 25 сентября 2026. Действуют для всех будущих pipeline, не только для фото.

### 1. Events — основа истории

`processing_events` — журнал всего, что произошло с asset. Каждая операция
(успех или сбой) оставляет событие `stage/status` с JSON-сообщением. События
не удаляются и не переписываются. Даже проблемный asset сохраняется вместе с
историей, а проблема фиксируется новым событием.

`assets.status` пока не расширяется: явная модель статусов — отдельный этап
после стабилизации pipeline (см. §41).

### 2. AI provenance

Каждый AI-результат должен быть воспроизводимо атрибутирован:

- `provider` — кто выполнил анализ (`lmstudio`, `openai`, ...);
- `model` — model ID;
- `prompt_version` — версия промпта (увеличивается при каждом изменении текста);
- `analyzed_at` / `failed_at` — время в UTC;
- `duration_s` — длительность.

Сейчас provenance хранится в сообщении события `AI/PASSED` или `AI/FAILED`.
Это позволяет сравнивать модели, находить результаты старых промптов и
повторно анализировать выборочно.

### 3. Восстановление после ошибок

- Сбой внешнего сервиса (LM Studio, cloud AI) не роняет pipeline: он
  фиксируется событием `*/FAILED` с типом ошибки и, если есть, сырым ответом.
- Любая операция повторяема по `asset_id` и идемпотентна: уже завершённый шаг
  пропускается, если нет явного `--force`.
- Скрытых автоматических повторов нет. Повтор — явное действие (человек, n8n,
  OpenClaw), и каждый повтор виден в истории.
- Перед обработкой проверяется, что исходный файл не изменился (SHA256).
  Изменённый источник не обрабатывается.

### 4. Сервисный слой — мост к OpenClaw и n8n

OpenClaw и n8n не вызывают внутренние модули напрямую и не пишут в SQLite.
Они работают через операции Stocker Core, адресуемые по `asset_id` и
возвращающие структурированный результат (исход + данные).

Порядок появления интерфейсов:

```text
функции Core (process_asset, ...)
    ↓
CLI с JSON-выводом          ← n8n Execute Command, отладка
    ↓
HTTP API (FastAPI)          ← n8n HTTP Request, OpenClaw tools
```

FastAPI — только после стабилизации внутреннего сервисного слоя.

### 5. AI и OpenClaw только инициируют действия; истина — Stocker Core и события

Принят 26.09.2026. AI-модели и агент OpenClaw могут только **инициировать**
операции через service layer / MCP. Истинное состояние определяют только
Stocker Core и события `processing_events` в SQLite:

- ответ агента (текст) — не доказательство: локальная модель может выдумать
  результат или неверно пересказать его (подтверждено в §35P, §35S);
- факт действия проверяется журналом вызовов MCP-сервера
  (`logs/mcp_http.log`) и событиями Stocker с `actor`;
- решения человека (`approved`/`rejected`) принимает только человек;
  `auto_approved` ставит только детерминированный review gate.

### 6. Одна локальная модель для всего AI-контура

Принят 26.09.2026. `qwen3-vl-8b-instruct` (Q5_K_M + `mmproj-F16` + KV `q8_0`
+ контекст 32768) — единая локальная модель для всех ролей: Vision, Metadata
AI, агент OpenClaw, tool use. Роли разделены архитектурно (свои провайдеры и
конфигурации), но исполняются одной моделью: 12 GB VRAM не позволяют держать
несколько моделей без постоянных перезагрузок. Новые модели не добавляются без
отдельного доказанного требования и решения пользователя.

### 7. Правила отдельно от советов AI (Stock Readiness и AI Advisors)

Принят 26.09.2026 (§35ZA). Оценка пригодности фотографии разделена:

- **Deterministic Stock Readiness** — соответствие требованиям площадок
  (размер, формат, цвет, metadata, keywords, бренды, релизы, категории).
  Правила Python, обязательный этап, может запретить экспорт.
- **AI Advisors** — Enhancement decision (`enhancement_not_needed` /
  `enhancement_recommended` / `enhancement_risky`) и Creative Review
  (коммерческий потенциал, композиция). Необязательны, только советуют и могут
  лишь **поднять внимание**; не снимают blocker-ы, не одобряют, не меняют
  данные. Провайдер заменяем конфигурацией; v1 — та же локальная модель
  (принцип 6), облачная модель — не зависимость.
- Topaz не считается обязательным улучшением.

Контракт — `docs/STOCK_READINESS_CONTRACT.md`.

---

# 3B. Итоговая архитектура (проверена 26.09.2026)

```text
Человек ──────────────── CLI (app.worker, app.metadata, app.api) ─┐
                                                                  │
OpenClaw (WSL, qwen3-vl-8b-instruct, skill stocker)               │
   → MCP HTTP 172.26.192.1:8765 (токен; actor задаёт СЕРВЕР)      │
                                                                  ▼
                              Stocker Service Layer (dispatch: права, envelope)
                                                                  │
                              Stocker Core (worker, metadata, review gate)
                                                                  │
                              SQLite: assets + processing_events (ИСТИНА)
                                                                  │
                              LM Studio: qwen3-vl-8b-instruct (Vision, Metadata AI)
```

### Права (проверены тестами и на production)

| Actor | Канал | Чтение | Pipeline (process, build, gate, escalate) | Правка черновиков (`draft`, `auto_approved`, `human_review`) | Изменение после решения человека (`approved`, `rejected`) | approve / reject |
|---|---|---|---|---|---|---|
| `human` | локальные CLI | ✅ | ✅ | ✅ | ✅ | ✅ |
| `agent:openclaw` | MCP (actor задаёт сервер) | ✅ | ✅ | ✅ | ❌ `FORBIDDEN` | ❌ инструментов нет + `FORBIDDEN` |
| `workflow:n8n` | будущий канал с actor от сервера | ✅ | ✅ | ✅ | ❌ `FORBIDDEN` | ❌ `FORBIDDEN` |

- `auto_approved` ставит только детерминированный review gate; ни один
  параметр API его не задаёт.
- Окончательное состояние определяется событиями Stocker (§3A.5).
- **Граница доверия.** Actor имеет смысл там, где вызывающий не имеет прямого
  доступа к БД. OpenClaw изолирован в WSL (interop выключен) и видит только
  MCP. JSON CLI `app.api` принимает `--actor` как параметр — это инструмент
  человека на Windows. **n8n не должен получать канал со свободным
  `--actor`:** только канал, где actor фиксирует сервер (свой токен →
  `workflow:n8n`), с правами не шире OpenClaw.

### Эксплуатация

- Автозапуск при входе пользователя: LM Studio (сервер `0.0.0.0:1234`, JIT),
  задача `OpenClaw WSL keep-alive`, задача `Stocker MCP`.
- Проверка окружения: `scripts/check_environment.ps1` (только чтение, без
  секретов; код выхода = число FAIL).
- Восстановление: `docs/RECOVERY.md`.

---

# ЧАСТЬ II. ФАКТИЧЕСКОЕ СОСТОЯНИЕ ПРОЕКТА

# 4. Рабочая директория

```text
F:\stock\stocker
```

Если агент имеет прямой доступ к этой директории:

> **`F:\stock\stocker` является единственным источником истины для текущего состояния проекта.**

Старые архивы не должны использоваться вместо рабочей директории.

---

# 5. Структура проекта

```text
app/
  __init__.py
  ai/
    __init__.py
    analyzer.py
    openai_analyzer.py
    local_analyzer.py
    schema.py
  database/
    __init__.py
    db.py
  ingest.py
  main.py
  qc.py
  worker.py

config/

data/
  incoming/
  working/
  approved/
  rejected/
  db/

logs/

scripts/
  init_db.py
  test_db.py
  test_openai_image.py
  test_local_ai.py
  debug_local_ai.py
  test_lmstudio_native.py
  model_benchmark/
    run_benchmark.py
    config.py
    client.py
    prompts.py
    logger.py
    tests/
      test_vision.py
      test_structured.py
      test_stock_analysis.py
      test_multi_image.py
      test_tools.py
    results/          (в .gitignore — локальные тестовые данные)

tests/
  conftest.py          (изолированная временная БД, FakeAnalyzer)
  test_worker.py
  test_local_analyzer.py

.venv/
.venv-linux/           (в .gitignore)

.env
.env.example
README.md
requirements.txt
requirements-dev.txt   (pytest)
pytest.ini
```

Тесты: `python -m pytest`. Они не обращаются ни к production-БД, ни к LM Studio.

Если фактическая структура отличается, сначала доверять фактическому проекту и при необходимости обновить этот документ.

---

# 6. Окружение

Основной компьютер:

- Windows 11 Pro x64
- AMD Ryzen 9 3900X
- 12 ядер / 24 потока
- RTX 3080 Ti
- 12 GB VRAM
- 32 GB RAM

RAM:

- MemTest86: 103%
- ошибок: 0

WSL:

- WSL 2.7.14.0
- distro: `OpenClawGateway`
- Node 26.4
- OpenClaw 2026.9.5

LM Studio работает на Windows.

---

# 7. LM Studio

Stocker использует OpenAI-compatible endpoint:

```text
http://192.168.1.104:1234/v1
```

API key для LM Studio:

```text
lm-studio
```

если не задан другой.

`LocalAnalyzer` использует OpenAI Python SDK и:

```text
chat.completions.create()
```

С 25 сентября 2026 настройки читаются из `.env`. Если переменная не задана,
используется значение по умолчанию из кода (текущие production-значения):

| Переменная | По умолчанию |
|---|---|
| `LMSTUDIO_BASE_URL` | `http://192.168.1.104:1234/v1` |
| `LMSTUDIO_MODEL` | `qwen3-vl-8b-instruct` |
| `LMSTUDIO_API_KEY` | `lm-studio` |
| `LMSTUDIO_TIMEOUT` | `180` (секунд) |

Явные аргументы конструктора имеют приоритет над `.env`.

---

# 8. Текущая vision model

Production vision model:

```text
qwen3-vl-8b-instruct
```

Это Qwen3-VL-8B.

Конфигурация загрузки в LM Studio (с 26.09.2026, §35R): веса `Q5_K_M`,
визуальный энкодер `mmproj-F16.gguf`, контекст 32768, KV cache `q8_0`
(настройки по умолчанию модели). VRAM ≈ 9,4 GiB из 12.

НЕ путать с:

```text
qwen3.8-9b-distill
```

Последняя модель использовалась в отдельном эксперименте OpenClaw.

---

# 9. LocalAnalyzer

Файл:

```text
app/ai/local_analyzer.py
```

Текущая модель:

```python
model: str = "qwen3-vl-8b-instruct"
```

LocalAnalyzer:

1. принимает путь к изображению;
2. проверяет файл;
3. определяет MIME;
4. подготавливает изображение;
5. кодирует его в base64;
6. отправляет его в LM Studio;
7. получает JSON;
8. валидирует через `AIAnalysis.model_validate_json()`.

Поддерживаемые форматы:

- `.jpg`
- `.jpeg`
- `.png`
- `.webp`

Для model input:

```text
max_edge = 2048
```

Это не изменяет оригинальный файл.

Изменения от 25 сентября 2026 (промпт и модель не менялись):

- перед уменьшением применяется `ImageOps.exif_transpose`: модель видит кадр
  в той же ориентации, что и человек (как уже делал `OpenAIAnalyzer`);
- таймаут запроса из `LMSTUDIO_TIMEOUT`, скрытые повторы SDK отключены
  (`max_retries=0`): повтор выполняется явно через worker;
- невалидный ответ модели поднимает `AIResponseError` (из `app/ai/analyzer.py`)
  с сохранённым `raw_output`;
- атрибуты provenance: `provider = "lmstudio"`, `model`, `prompt_version`.
  При любом изменении текста промпта или формата ответа `prompt_version`
  нужно увеличить.

### Strict structured output (с 25.09.2026, `prompt_version = "local-v2"`)

Запрос передаёт `response_format = {"type": "json_schema", "json_schema":
{"name": "AIAnalysis", "strict": true, "schema": response_schema()}}`.
LM Studio это соблюдает: проверено реальным запросом с посторонней схемой.

`response_schema()` строится из `AIAnalysis.model_json_schema()`, **сама
`AIAnalysis` и промпт не менялись**. Отличие: все поля (включая
`PeopleInfo`) помечены `required`, `additionalProperties: false`.

Причина: у всех полей `AIAnalysis` есть значения по умолчанию, поэтому
исходная схема не содержит `required`. С ней модель в пробном запросе вернула
`{}`, и он прошёл бы валидацию как пустой анализ без ошибки.

| Версия | Промпт | Формат ответа |
|---|---|---|
| `local-v1` | исходный | свободный текст, парсинг JSON |
| `local-v2` | тот же | strict `json_schema` |

### TIFF

`.tif/.tiff` принимаются ingest и перекодируются в JPEG только для model
input, оригинал не меняется. Выбрано преобразование, а не запрет: TIFF
ожидается как выход Topaz и будущих pipeline обработки. Проверены RGB, CMYK,
grayscale, RGBA. 16-битный grayscale масштабируется (`>> 8`); без этого
`convert("RGB")` даёт белый кадр.

Известные ограничения TIFF:

- 16-битный RGB TIFF Pillow не открывает → QC `IMAGE_READ_ERROR`, до AI не
  доходит;
- QC-метрики (`sharpness`, `dark/bright_ratio`) для 16-битного grayscale
  считаются по обрезанным значениям и неточны;
- `OpenAIAnalyzer` TIFF не поддерживает (он не в production-пути).

---

# 10. AI schema

Файл:

```text
app/ai/schema.py
```

Текущая структура:

```python
class PeopleInfo(BaseModel):
    present: bool = False
    count: int = Field(default=0, ge=0)


class AIAnalysis(BaseModel):
    analysis_version: str = "1.0"
    description: str = ""
    title: str = ""
    keywords: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    subject: str = ""
    commercial_context: str = ""
    technical_subjects: list[str] = Field(default_factory=list)
    people: PeopleInfo = Field(default_factory=PeopleInfo)
    brands: list[str] = Field(default_factory=list)
    logos: list[str] = Field(default_factory=list)
    text_visible: list[str] = Field(default_factory=list)
    editorial_risk: list[str] = Field(default_factory=list)
    ai_generated: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
```

Не изменять schema просто ради подгонки под случайный malformed output модели.

---

# 11. SQLite

Ожидаемый путь:

```text
F:\stock\stocker\data\db\stocker.db
```

В `app/database/db.py` используется путь, вычисляемый относительно проекта.

Основная таблица:

```sql
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    source_path TEXT NOT NULL,
    file_hash TEXT UNIQUE,
    extension TEXT,
    width INTEGER,
    height INTEGER,
    file_size INTEGER,
    status TEXT NOT NULL DEFAULT 'NEW',
    qc_result TEXT,
    ai_result TEXT,
    metadata_json TEXT,
    rejection_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

Также используется таблица:

```text
processing_events
```

Она хранит события обработки.

---

# 12. Ingest

Файл:

```text
app/ingest.py
```

Отвечает за:

- hashing;
- duplicate detection;
- извлечение metadata;
- создание asset;
- запись события.

При тестах pipeline нужно использовать новую фотографию, если не требуется специально проверить duplicate behavior.

### `source_path` (с 25.09.2026)

- Хранится относительно ROOT и **всегда с `/`**: `data/incoming/x.jpg`
  (`to_source_path`). Одна и та же запись открывается из Windows и из WSL.
- Путь к файлу можно передавать абсолютным или относительным (от текущей
  директории).
- Файл вне проекта пропускается: `SKIP outside project`.
- Файл по `source_path` находит только `source_file(asset)`, его используют
  и worker, и QC. Для совместимости он понимает и старые записи с `\`.
- Существующие записи приведены к `/` скриптом
  `python -m scripts.normalize_source_paths [--apply]` (идемпотентный,
  по умолчанию dry-run). Каждое изменение фиксируется событием
  `SOURCE/NORMALIZED`.

---

# 13. QC

Файл:

```text
app/qc.py
```

Известные правила:

- размер файла: 100 KB – 45 MB;
- minimum width: 4000;
- minimum height: 3000;
- sharpness;
- extreme pixel checks.

AI запускается только после успешного QC.

**Расхождение с кодом (зафиксировано 25.09.2026):** отклоняющие правила —
только размер файла, разрешение и читаемость изображения. `sharpness`
записывается как метрика без порога, `dark_ratio`/`bright_ratio > 0.20` дают
только warnings. Пороги для этих метрик — отдельное решение, пока не принято.

---

# 14. Worker

Файл:

```text
app/worker.py
```

Текущая структура (с 25 сентября 2026):

```text
process_file(path)                       новый файл
    ↓
ingest_file()  → INGEST/DONE
    ↓
process_asset(asset_id, force=False)     новый или уже зарегистрированный asset
    ↓
ai_result уже есть и нет force?  → AI_ALREADY_DONE (без событий)
    ↓
verify_source(): файл есть и SHA256 совпадает?
    нет → SOURCE/INVALID {reason: SOURCE_MISSING | SOURCE_CHANGED}
    ↓
check_asset() + save_qc_result()  → QC/PASSED | QC/FAILED
    ↓ (только QC PASSED)
run_ai(): analyzer.analyze()
    успех → save_ai_result() + AI/PASSED {provenance}
    сбой  → AI/FAILED {provenance, error_type, error, raw_output?}
```

CLI:

```powershell
python -m app.worker IMAGE_PATH              # новый файл
python -m app.worker --asset-id N            # повтор / догон существующего asset
python -m app.worker --asset-id N --force    # повторный AI при наличии ai_result
```

Код возврата: `1` для `AI_FAILED`, `SOURCE_INVALID`, `ASSET_NOT_FOUND` и
случая «файл не добавлен» (дубликат, неподдерживаемый или битый файл), иначе
`0`. `QC_FAILED` — корректный бизнес-исход, не ошибка. Для дубликата нужно
использовать `--asset-id` с ID, который печатает ingest.

`process_asset` возвращает строку-исход (`AI_PASSED`, `AI_FAILED`,
`AI_ALREADY_DONE`, `QC_FAILED`, `SOURCE_INVALID`, `ASSET_NOT_FOUND`). Это
будущий контракт сервисного слоя. `assets.status` worker по-прежнему не
меняет: его пишет только QC.

### Контракт событий `processing_events`

| stage | status | message |
|---|---|---|
| `INGEST` | `DONE` | текст `Registered <FORMAT> <W>x<H>` |
| `QC` | `PASSED` / `FAILED` | JSON результата QC |
| `SOURCE` | `INVALID` | JSON: `reason`, `source_path`, `expected_hash`, `actual_hash` |
| `SOURCE` | `NORMALIZED` | JSON: `old`, `new` — нормализация `source_path` |
| `AI` | `PASSED` | JSON: `provider`, `model`, `prompt_version`, `analyzed_at`, `duration_s` |
| `AI` | `FAILED` | JSON: provenance + `failed_at`, `error_type`, `error`, `raw_output` (до 4000 символов, если ответ был) |
| `METADATA_AI` / `METADATA` | см. контракт | `docs/METADATA_CONTRACT.md` §7 |
| `NOTIFY` | `SENT` | JSON: `channel`, `kind`, `severity`, `title`, `key`, `actor` — факт доставки уведомления об объекте (§35Z, `SERVICE_CONTRACT.md` §3) |

После `AI/PASSED` (и для `--asset-id` при готовом AI) worker вызывает
`metadata build`: только draft и события, approve — всегда человек (§35K).

Исторические события (до 25.09.2026) могут иметь старый формат: например, у
`AI/PASSED` для asset 4 сообщение — `AI analysis completed`, а у раннего `QC`
для asset 2 — Python repr вместо JSON.

---

# 15. Production AI integration

Production-путь уже реализован:

```text
ingest
  ↓
QC
  ↓
AI
  ↓
assets.ai_result
```

Изменения:

### `app/worker.py`

После успешного QC:

- запускается `LocalAnalyzer`;
- результат сериализуется через `AIAnalysis.model_dump_json()`;
- результат сохраняется в `assets.ai_result`;
- добавляется событие `AI/PASSED`.

При провальном QC AI пропускается.

### `app/database/db.py`

Добавлена функция:

```text
save_ai_result
```

в существующий DB layer.

### `app/ai/local_analyzer.py`

Используется:

```text
qwen3-vl-8b-instruct
```

---

# ЧАСТЬ III. ПРОВЕДЁННЫЕ ИСПЫТАНИЯ И РЕШЕНИЯ

# 16. Benchmark локальных vision-моделей

Тестировались:

1. Qwen2.5-VL-7B
2. Qwen3-VL-8B
3. MiniCPM-V 4.5
4. Ministral 3 8B

Проверялись:

- basic vision;
- structured output;
- stock analysis;
- multi-image;
- tool calling.

Это проектный benchmark, а не универсальный рейтинг.

---

# 17. Benchmark summary

| Тест | Qwen2.5-VL-7B | Qwen3-VL-8B | MiniCPM-V 4.5 Q4 | Ministral 3 8B |
|---|---:|---:|---:|---:|
| Basic vision | OK | OK ~2.25 s | OK ~7.14 s | OK ~2.43 s |
| Structured output | schema FAIL | schema OK | schema OK | FAIL |
| Stock analysis | schema FAIL | schema FAIL | schema FAIL | schema FAIL |
| Tool calling | no structured call | structured call OK | no | no |
| 1 image | OK | OK ~1.64 s | OK ~8.84 s | OK ~8.81 s |
| 2 images | OK | OK ~1.82 s | OK ~7.32 s | OK ~8.62 s |
| 3 images | OK | OK ~4.58 s | OK ~11.56 s | OK ~11.51 s |

MiniCPM фактически тестировался на Q4_K_S.

Практическое решение:

> Для текущей реализации используется `qwen3-vl-8b-instruct`.

---

# 18. Успешный отдельный AI test

Команда:

```powershell
python -m scripts.test_local_ai
```

успешно вернула валидный `AIAnalysis`.

Это доказало:

```text
LocalAnalyzer
    ↓
LM Studio
    ↓
Qwen3-VL-8B
    ↓
JSON
    ↓
AIAnalysis validation
```

работает.

---

# 19. Тестовое изображение

Ранее использованное:

```text
F:\stock\stocker\data\incoming\IMG_20260911_130107.jpg
```

Размер:

```text
4096 × 3072
```

Размер:

```text
5158623 bytes
```

SHA256:

```text
6673ff8456783f02ea96166bbf542867541b9b58d6e4a8e5d375b59b51d06578
```

Asset ID:

```text
3
```

Ранее QC:

- passed;
- width 4096;
- height 3072;
- JPEG;
- file size 5158623;
- sharpness ~240.421;
- dark_ratio ~1e-6;
- bright_ratio ~0.006129.

---

# ЧАСТЬ IV. ТЕКУЩИЙ ЭТАП

# 20. Текущий production pipeline

Цель текущего этапа:

```text
НОВАЯ ФОТОГРАФИЯ
    ↓
INGEST
    ↓
ASSET В SQLITE
    ↓
QC
    ↓
QC PASSED
    ↓
QWEN3-VL-8B
    ↓
AIAnalysis
    ↓
assets.ai_result
    ↓
AI/PASSED EVENT
```

---

# 21. End-to-end проверка

Для проверки использовалась новая фотография:

```text
F:\stock\stocker\data\incoming\IMG_20260911_130109.jpg
```

Команда:

```powershell
python -m app.worker F:\stock\stocker\data\incoming\IMG_20260911_130109.jpg
```

Результат:

```text
WORKER: F:\stock\stocker\data\incoming\IMG_20260911_130109.jpg
Processing: IMG_20260911_130109.jpg
ADDED: ID=4
QC: True
AI: PASSED
Asset ID: 4
```

Проверка фактического SQLite state для asset ID `4`:

- asset существует, имеет `status = PASSED`;
- `qc_result` сохранён и содержит `passed = true`;
- `ai_result` непустой и повторно валидируется через `AIAnalysis`;
- существуют события `INGEST/DONE`, `QC/PASSED`, `AI/PASSED`.

---

# 22. Устранённый SQLite blocker

Статус:

```text
🟢 DONE
```

Причина:

```text
sqlite3.OperationalError:
attempt to write a readonly database
```

Подтверждённая фактическая причина (23 сентября 2026): процесс, выполняющий
Stocker из Codex, работает от имени
`DESKTOP-NL7P3S0\\codexsandboxoffline`. У защищённых ACL существующих
`data\\db` и `data\\db\\stocker.db` нет разрешения Modify для группы
`DESKTOP-NL7P3S0\\CodexSandboxUsers`. В результате SQLite может читать БД и
взять `BEGIN IMMEDIATE`, но не может открыть сам файл для записи и создать
служебный rollback journal. Проверочная `INSERT` завершается `SQLITE_READONLY`.
Это проблема доступа к существующим объектам файловой системы, а не БД,
схемы, QC или AI.

ACL были выданы только существующим `data\\db` и `data\\db\\stocker.db`; БД
не удалялась и не пересоздавалась. Внутренний default sandbox Codex всё ещё
ограничивает прямую запись своего процесса даже после исправления Windows ACL,
поэтому production test был выполнен вне этого внутреннего ограничения. Это
ограничение рабочей среды Codex, а не Stocker или SQLite.

Это НЕ проблема:

- Qwen3-VL;
- LM Studio;
- LocalAnalyzer;
- AIAnalysis;
- QC.

Исторически ошибка происходила раньше:

```text
worker
 ↓
ingest
 ↓
add_asset
 X
SQLite write
```

После первоначальной ошибки:

```text
assets: []
events: []
```

То есть до исправления asset вообще не был создан. Повторная проверка успешно
создала asset ID `4` и завершила весь pipeline.

---

# 23. Результат исправления

SQLite write access подтверждён контрольной транзакцией с rollback. Затем
production worker успешно записал ingest, QC и AI state в существующую БД.
Ни модель, ни AI schema, ни DB schema не менялись.

Следующий этап по текущему плану: детерминированный metadata pipeline на
основе уже сохранённого `AIAnalysis`; перед реализацией нужно определить его
контракт и критерии проверки.

---

# 24. Критерий завершения текущего этапа

Текущий этап считается `DONE` только если доказан полный путь:

```text
photo
 ↓
asset row
 ↓
QC result
 ↓
valid AIAnalysis
 ↓
assets.ai_result
 ↓
AI/PASSED event
```

Проверка должна включать реальное состояние SQLite, а не только консольный вывод.

Статус:

```text
🟢 DONE
```

---

# ЧАСТЬ V. ПРАВИЛА БОРТОВОГО ЖУРНАЛА

# 25. Зачем нужен бортовой журнал

Этот файл — не только технический паспорт.

Он одновременно является **бортовым журналом проекта**.

Его задача — сохранить:

- архитектурные решения;
- важные технические решения;
- завершённые этапы;
- подтверждённые результаты;
- существенные блокеры;
- причины изменения направления;
- важные ограничения.

Журнал нужен для того, чтобы новый агент или человек мог восстановить ход проекта без чтения всей истории чатов.

---

# 26. Когда добавлять новую запись в журнал

Новая запись создаётся, когда произошло хотя бы одно из следующего:

### 1. Достигнута законченная техническая цель

Например:

```text
LocalAnalyzer → Qwen3-VL-8B → AIAnalysis
```

успешно работает.

---

### 2. Принято архитектурное решение

Например:

> Stocker Core остаётся детерминированным слоем, OpenClaw не пишет напрямую в SQLite.

---

### 3. Выбран или заменён компонент

Например:

> Qwen3-VL-8B выбран как текущая vision model после benchmark.

---

### 4. Найдена существенная проблема и установлена её причина

Например:

> Production pipeline заблокирован readonly SQLite на `add_asset`, до QC и AI.

---

### 5. Изменился контракт между компонентами

Например:

- изменение AIAnalysis;
- изменение структуры event;
- изменение API;
- изменение формата DB state.

---

### 6. Завершён этап проекта

Например:

> Ingest + QC + AI persistence завершены и подтверждены интеграционным тестом.

---

### 7. Изменилось стратегическое решение

Например:

> n8n отложен до стабилизации внутреннего Python pipeline.

---

# 27. Что НЕ писать в журнал

Не записывать каждую команду и каждую промежуточную попытку:

```text
запустил PowerShell
изменил строку
запустил тест
получил traceback
повторил тест
```

Если это не изменило понимание проекта, архитектуру или состояние этапа, это остаётся в:

- истории чата;
- Git;
- тестовых логах;
- runtime logs.

Бортовой журнал должен отражать **состояние и решения**, а не историю терминала.

---

# 28. Формат записи журнала

Каждая значимая запись должна по возможности иметь:

```markdown
## YYYY-MM-DD — Название события

### Цель
Что хотели получить.

### Решение
Что решили сделать.

### Реализовано
Что фактически изменилось.

### Проверка
Каким тестом подтверждено.

### Результат
PASS / FAIL / BLOCKED.

### Блокер
Если есть.

### Следующий шаг
Что делать дальше.

### Статус
🟢 DONE
🟡 IN PROGRESS
🔴 BLOCKED
⚪ PLANNED
```

Не требуется заполнять все поля, если они неприменимы.

---

# 29. Статусы

Использовать:

```text
🟢 DONE
```

Техническая цель достигнута и подтверждена.

```text
🟡 IN PROGRESS
```

Работа продолжается.

```text
🔴 BLOCKED
```

Есть конкретная проблема, мешающая продолжению.

```text
⚪ PLANNED
```

Запланировано, но ещё не реализуется.

---

# 30. ВАЖНОЕ ПРАВИЛО ЖУРНАЛА

Запись должна фиксировать **доказанный факт**, а не намерение.

Плохо:

> AI pipeline готов.

Хорошо:

> AI pipeline реализован; end-to-end тест пока заблокирован SQLite readonly на `add_asset`.

Плохо:

> Qwen3-VL лучший.

Хорошо:

> Qwen3-VL-8B выбран для текущей реализации после проектного benchmark; он показал vision + valid target schema + tool call + multi-image в наших тестах.

---

# ЧАСТЬ VI. ИСТОРИЯ ЗНАЧИМЫХ СОБЫТИЙ

# 31. 2026-09 — Архитектурная модель Stocker

### Цель

Определить разделение ролей между агентом, workflow engine, Python core, состоянием и AI.

### Решение

Принята архитектурная модель:

```text
YOU
 ↓
OpenClaw Agent
 ↓
Stocker Core / n8n / Browser
 ↓
deterministic tools + AI services
```

Stocker Python остаётся детерминированным ядром.

### Статус

🟢 DONE

---

# 32. 2026-09 — Benchmark локальных VLM

### Цель

Выбрать vision model для Stocker на RTX 3080 Ti 12 GB.

### Рассматривались

- Qwen2.5-VL-7B;
- Qwen3-VL-8B;
- MiniCPM-V 4.5;
- Ministral 3 8B.

### Решение

Для текущего production implementation выбран:

```text
qwen3-vl-8b-instruct
```

### Статус

🟢 DONE

---

# 33. 2026-09 — LocalAnalyzer переключён на Qwen3-VL-8B

### Цель

Перевести реальный `LocalAnalyzer` на выбранную vision model.

### Изменение

```text
qwen2.5-vl-3b-instruct
        ↓
qwen3-vl-8b-instruct
```

### Проверка

```powershell
python -m scripts.test_local_ai
```

успешно вернул валидный `AIAnalysis`.

### Статус

🟢 DONE

---

# 34. 2026-09 — Production AI path реализован

### Цель

Подключить LocalAnalyzer к production worker.

### Изменено

- `app/worker.py`;
- `app/database/db.py`.

### Реализовано

```text
ingest
 ↓
QC
 ↓
AI
 ↓
assets.ai_result
 ↓
AI/PASSED event
```

AI запускается только после успешного QC.

### Статус

🟢 DONE

Интеграция подтверждена реальной новой фотографией в asset ID `4`: ingest,
QC, `LocalAnalyzer`, `assets.ai_result` и `AI/PASSED` прошли в одной
production-проверке.

---

# 35. 2026-09 — Production test заблокирован SQLite

### Цель

Проверить полный pipeline на новой фотографии.

### Команда

```powershell
python -m app.worker F:\stock\stocker\data\incoming\IMG_20260911_130109.jpg
```

### Первоначальный результат

```text
sqlite3.OperationalError:
attempt to write a readonly database
```

Ошибка:

```text
app/database/db.py
add_asset
```

### Вывод

Asset не создаётся.

QC и AI не запускаются.

### Статус

🟢 DONE — исторический blocker устранён. После выдачи Modify ACL на
существующие DB-объекты worker был успешно проверен вне внутреннего Codex
sandbox; создан asset ID `4`, QC и AI прошли.

---

# 35A. 2026-09-23 — Подтверждён источник SQLite readonly

### Цель

Установить причину ошибки записи до изменения кода или БД.

### Проверка

- фактический DB path: `F:\stock\stocker\data\db\stocker.db`;
- `PRAGMA query_only = 0`, `journal_mode = delete`, `locking_mode = normal`;
- чтение БД и `BEGIN IMMEDIATE` проходят;
- попытка `INSERT` в транзакции завершается `SQLITE_READONLY`;
- прямое открытие `stocker.db` как `r+b` и создание временного файла в
  `data\\db` завершаются `Permission denied`;
- ACL указанных дочерних объектов не содержат группу
  `DESKTOP-NL7P3S0\\CodexSandboxUsers`, от которой работает текущий процесс.

### Вывод

Readonly вызван файловыми ACL. БД не повреждена, не должна удаляться или
пересоздаваться. AI-код, модель и schema менять не требуется.

### Следующий шаг

Исправить ACL только для существующих `data\\db` и `data\\db\\stocker.db`,
затем выполнить production end-to-end test.

### Результат

Администратор выдал Modify для `DESKTOP-NL7P3S0\\CodexSandboxUsers` на
существующие `data\\db` и `data\\db\\stocker.db`. Контрольная SQLite
транзакция с rollback прошла. Default sandbox Codex по-прежнему ограничивает
свой прямой доступ поверх Windows ACL, но запуск worker вне него успешно
записал весь production state в эту же БД.

### Статус

🟢 DONE

---

# 35B. 2026-09-23 — Первый production pipeline подтверждён end-to-end

### Цель

Доказать полный путь на новой фотографии без изменения модели, AI schema или
существующей SQLite БД.

### Проверка

Для `F:\stock\stocker\data\incoming\IMG_20260911_130109.jpg` worker создал
asset ID `4` (JPEG, `8192 × 6144`, SHA256
`fd9726b043f0a5e8627e1d32529aaf57c4225960ec0f3f025faedbc3daa103ca`).
Фактический SQLite state подтвердил:

- `assets.status = PASSED`;
- сохранённый `qc_result` с `passed = true`;
- непустой `assets.ai_result`, повторно валидируемый через `AIAnalysis`;
- события `INGEST/DONE`, `QC/PASSED`, `AI/PASSED`.

### Результат

```text
ingest → QC → qwen3-vl-8b-instruct → AIAnalysis → assets.ai_result → AI/PASSED
```

### Следующий шаг

Перейти к планированию детерминированного metadata pipeline, используя
сохранённый AI result как входные данные.

### Статус

🟢 DONE

---

# 35C. 2026-09-25 — Аудит проекта и расхождения с паспортом

### Цель

Сверить паспорт с фактическим кодом и SQLite перед продолжением работы.

### Выявлено

- Завершённый AI-milestone не был закоммичен в git. Исправлено: коммит
  `412f33a`. Benchmark results, `.venv-linux/` и временный `scripts/пустой.py`
  добавлены в `.gitignore` и остаются только на диске.
- QC: `sharpness` и extreme pixels не отклоняют фото (см. §13).
- `source_path` хранится со смешанными разделителями (`data\incoming\...` из
  Windows и `data/incoming/...` из WSL). Под WSL пути с `\` не разрешатся. Пока
  не исправлено: важно при подключении OpenClaw из WSL.
- README устарел (Cloud LLM как основной путь metadata, n8n «при
  необходимости»).
- `scripts/test_db.py` пишет в production-БД; повторный запуск падает на
  UNIQUE. Заменён на pytest с временной БД, сам скрипт не удалялся.
- Качество AI-вывода: 8–9 keywords (стокам нужно 25–49), пустые `categories` и
  `commercial_context`, `confidence = 1.0`. Схему не менять, но учитывать в
  metadata pipeline.
- `response_format` с JSON schema не используется, а в benchmark
  `stock_analysis` все модели дали schema FAIL. Валидность ответа вероятностная;
  теперь сбой хотя бы фиксируется `AI/FAILED` с `raw_output`.

### Статус

🟢 DONE

---

# 35D. 2026-09-25 — Решения по дальнейшему порядку

### Решение

1. Явная модель статусов нужна, но `assets.status` пока **не меняется** и
   миграции нет. Надёжность строится на `processing_events`. Статусы —
   отдельный этап после стабилизации.
2. Внешний доступ: сначала CLI с JSON, FastAPI — после стабильного
   внутреннего сервисного слоя.
3. Проблемные asset'ы не удаляются: история сохраняется, проблема фиксируется
   событием.
4. Приняты принципы §3A: events как история, AI provenance, восстановление
   после ошибок, сервисный слой как мост к OpenClaw и n8n.

### Статус

🟢 DONE

---

# 35E. 2026-09-25 — Надёжность pipeline: AI/FAILED, повтор по asset_id, hash

### Цель

Сделать ingest → QC → AI восстанавливаемым: сбой AI не должен оставлять asset в
состоянии, из которого нет выхода.

### Проблема до изменений

Исключение LM Studio или невалидный JSON роняли worker без события. Повторный
запуск отсекал файл как дубликат, и AI для такого asset'а был недостижим.
Asset 2 и 3 (зарегистрированы до AI-интеграции) по той же причине не имели
`ai_result`.

### Реализовано

- `app/worker.py`: `process_asset(asset_id, force)`, `verify_source`,
  `run_ai`; события `AI/FAILED` и `SOURCE/INVALID`; provenance в `AI/*`;
  CLI `--asset-id` / `--force`; коды возврата (§14).
- `app/ai/local_analyzer.py`: настройки из `.env`, таймаут, `max_retries=0`,
  `exif_transpose`, `AIResponseError` с `raw_output`, `prompt_version`.
- `app/ai/analyzer.py`: `AIResponseError`.
- `app/ingest.py`: `source_file(asset)`.
- `app/database/db.py`: путь к БД вычисляется при вызове, а не при импорте
  (иначе тесты не могли бы перенаправить запись во временную БД). Для
  production поведение не изменилось.
- `tests/`: 19 pytest-тестов на временной БД с `FakeAnalyzer`.

Схема БД, `AIAnalysis`, промпт и модель не менялись.

### Проверка

- `python -m pytest`: 19 passed. Счётчики production-БД до прогона не
  изменились.
- Production, `--asset-id 3`: QC PASSED → `AI_PASSED`, валидный `AIAnalysis`
  в `assets.ai_result`, событие `AI/PASSED` с provenance
  (`lmstudio`, `qwen3-vl-8b-instruct`, `local-v1`, 41.85 s — вероятно,
  включая загрузку модели в LM Studio; ранее на asset 4 было ~13 s).
- Production, `--asset-id 2`: `SOURCE/INVALID`, `SOURCE_CHANGED`, exit 1.
- Production, `--asset-id 4`: `AI_ALREADY_DONE`, без новых событий.
- `LocalAnalyzer` на недоступном endpoint: `APIConnectionError` за ~2.4 s, без
  скрытых повторов.

### Результат

PASS

### Статус

🟢 DONE

---

# 35F. 2026-09-25 — Asset 2: подменённый источник

### Факт

Asset 2 зарегистрирован 19.09.2026 как `IMG_20260911_130107.jpg`,
8192 × 6144, SHA256 `63e9981c…599c`. Позже файл по тому же пути заменён
уменьшенной версией 4096 × 3072 (SHA256 `6673ff84…6578`), которая 21.09.2026
зарегистрирована как asset 3. Оригинал 8192 × 6144 в `data/incoming` больше
не существует.

### Действие

Asset 2 не удалён и не изменён. Проблема зафиксирована событием
`SOURCE/INVALID` (`SOURCE_CHANGED`, expected/actual hash). Повторные запуски
worker для asset 2 будут добавлять такое же событие и не выполнят QC или AI.

### Политика восстановления (⚪ открытый вопрос)

Варианты, решение пока не принято:

1. Вернуть оригинальный файл с тем же SHA256 → `--asset-id 2` пройдёт штатно.
2. Считать asset 2 историческим/заменённым asset 3 → после появления явной
   модели статусов перевести в терминальный статус (например, `REJECTED` с
   причиной `SOURCE_CHANGED`).
3. Общее правило на будущее: ingest не должен молча принимать новый файл под
   тем же `source_path`, если там уже зарегистрирован asset с другим hash.

### Статус

⚪ PLANNED — политика восстановления

---

# 35G. 2026-09-25 — Три технических долга перед metadata

### Цель

Закрыть три долга до metadata pipeline, не меняя архитектуру, `AIAnalysis`,
промпт и `assets.status`.

### Реализовано

1. **Strict JSON schema** для `LocalAnalyzer` (§9): `response_schema()`,
   `prompt_version = local-v2`.
2. **TIFF**: преобразование в JPEG для AI (§9). Ingest больше не принимает
   формат, который AI не может обработать.
3. **`source_path`**: POSIX-запись, приём относительных путей, пропуск файлов
   вне проекта, единая функция `source_file()` для worker и QC, миграция
   старых записей с событием `SOURCE/NORMALIZED` (§12).

Попутно исправлен баг: `python -m app.worker data/incoming/x.jpg` с
относительным путём падал в `relative_to` (в БД ничего не записывалось).

### Проверка

- `python -m pytest`: 32 passed.
- Нормализация на production: asset 2 и 4 `data\incoming\...` →
  `data/incoming/...`, события 13–14. Повторный запуск: `Nothing to
  normalize`.
- WSL (`OpenClawGateway`, `.venv-linux`), только чтение: для asset 2–4 пути
  разрешаются, файлы найдены, hash asset 3 и 4 — OK, asset 2 —
  `SOURCE_CHANGED`. Старый путь с `\` тоже разрешается.
- Новая фотография `IMG_20260911_130437.jpg` (относительный путь) → asset 5:
  `INGEST/DONE`, `QC/PASSED`, `AI/PASSED` с `prompt_version = local-v2`,
  7.23 s, `source_path = data/incoming/IMG_20260911_130437.jpg`, валидный
  `AIAnalysis`.
- Реальный TIFF (LZW, из `IMG_20260911_130107.jpg`) → LM Studio → валидный
  `AIAnalysis` за 3.3 s (без записи в БД).

### Наблюдение для metadata

Strict schema гарантирует формат, но не содержание: у asset 5 8 keywords, пустые
`categories` и `commercial_context`, `confidence = 0.98`. Это задача
metadata-этапа (см. §42).

### Статус

🟢 DONE

---

# 35H. 2026-09-25 — Решения по metadata и проект контракта

### Решения пользователя

1. Первые площадки — Adobe Stock и Shutterstock, но внутренний metadata-формат
   **не зависит от площадок**. Адаптеры — на этапе экспорта.
2. Vision `AIAnalysis` не меняется ради количества keywords. Вводится
   **отдельный Metadata AI pass**:
   - Vision отвечает «что изображено»;
   - Metadata AI — «как это подготовить для продажи»;
   - Python — нормализация, ограничения, проверка.
3. Ручная проверка сразу: `draft → edit → approve/reject`, только CLI.
4. Категории площадок — на этапе экспорта.
5. Реализация начинается только после согласования контракта.

### Контракт

`docs/METADATA_CONTRACT.md` (`metadata-v1`): структура `metadata_json`,
поля от Vision / Metadata AI / Python, правила нормализации и валидации,
переходы review, события, CLI.

### Статус

🟢 DONE — контракт согласован (см. §35I)

---

# 35I. 2026-09-25 — Контракт metadata согласован с корректировками

### Корректировки пользователя (внесены в `docs/METADATA_CONTRACT.md`)

1. Metadata AI v1 работает только на JSON `AIAnalysis`, но интерфейс
   `suggest(analysis, image_path=None)` оставляет возможность передавать
   изображение. Фактические входы пишутся в provenance (`inputs`).
2. Metadata provider отделён от Vision: свой интерфейс `MetadataAnalyzer`,
   своя конфигурация `METADATA_*`. `qwen3-vl-8b-instruct` по умолчанию —
   только из-за ограничений VRAM.
3. Недоступность Metadata AI не блокирует pipeline: partial draft из Vision с
   `completeness = partial`, предупреждением `PARTIAL_DRAFT`; approve требует
   `--allow-partial`.
4. Keywords не обрезаются: `TOO_MANY_KEYWORDS` и `KEYWORD_TOO_LONG` — ошибки
   валидации.
5. Структура `metadata_json` (§5) и события (§7) зафиксированы до реализации.

### Статус

🟢 DONE

---

# 35J. 2026-09-25 — MetadataSuggestion и MetadataAnalyzer

### Реализовано

- `app/ai/schema.py`: `MetadataSuggestion` (без лимитов; `AIAnalysis` не менялась).
- `app/ai/structured.py`: общий построитель strict `json_schema` с
  ограничениями только для запроса. `LocalAnalyzer` переведён на него, схема
  Vision проверена на побайтовое совпадение.
- `app/ai/metadata_analyzer.py`: интерфейс `MetadataAnalyzer` (не наследует
  `AIAnalyzer`) и `LMStudioMetadataAnalyzer`. Конфигурация `METADATA_*`
  независима от `LMSTUDIO_*`. Text-only вход (JSON `AIAnalysis`),
  `inputs()` для provenance, `prompt_version = metadata-v1`, strict schema с
  `keywords` 25–49, `AIResponseError` с `raw_output`, без скрытых повторов.
- Интеграционные тесты с реальным LM Studio — маркер `lmstudio`, по умолчанию
  пропускаются:
  `$env:STOCKER_LMSTUDIO_TESTS = "1"; python -m pytest -m lmstudio`.

Сохранения в БД ещё нет.

### Проверка

- `python -m pytest`: 54 passed, 3 skipped.
- `-m lmstudio`: 3 passed. Реальный ответ валиден, keywords 25–49.
  Реальный обрезанный ответ (`max_tokens=12`) → `AIResponseError` с
  непустым `raw_output`. Недоступный endpoint → ошибка соединения, не
  `AIResponseError`.
- Vision-результаты asset 3, 4, 5 (только чтение): keywords 9/8/8 →
  31/31/30, все Vision-keywords сохранены, title 41/71/59 символов,
  ~2.5 s на asset.

### Наблюдения для Python-слоя и review

- Metadata AI добавляет правдоподобные, но не подтверждённые Vision концепты
  (`construction site`, `underground`, `factory interior`, `heavy industry`).
  Контракт это допускает как «концепты для покупателя», но именно поэтому
  review человеком обязателен.
- В тексте встречаются типографские символы (`’`). Возможное будущее правило
  нормализации, в контракт пока не входит.

### Статус

🟢 DONE

---

# 35K. 2026-09-25 — Metadata pipeline реализован (draft/edit/approve/reject)

### Решения пользователя перед реализацией

- До `app/metadata.py` добавлена нормализация текста: Unicode NFKC,
  типографские кавычки и тире → ASCII, очистка пробелов (`app/textnorm.py`).
- Расширение концептами **не запрещается**. Различаются общие концепты
  (разрешены) и конкретные утверждения, бренды и локации (требуют
  подтверждения). Контракт — редакция 2, §4.7.
- Порядок: чистый builder → тесты → сохранение и события → CLI → worker.

### Реализовано

| Файл | Что |
|---|---|
| `app/textnorm.py` | `normalize_text` |
| `app/metadata_builder.py` | чистые функции без БД: `build_draft` (full/partial), `rebuild`, `edit`, `approve`, `reject`; нормализация, флаги, grounding (`vision` / `concept` / `specific_claims`), валидация |
| `app/metadata.py` | операции по `asset_id` → `{asset_id, outcome, metadata}`; атомарная запись `metadata_json` + событий; CLI `python -m app.metadata build\|rebuild\|show\|edit\|approve\|reject N` |
| `app/database/db.py` | + `transaction()`, `insert_event()`, `update_metadata()`, `get_last_event()` |
| `app/worker.py` | после `AI/PASSED` (и для `--asset-id` с готовым AI) — `metadata build`. **Только draft и события, без approve.** Существующие metadata worker не перезаписывает |
| `tests/conftest.py` | autouse-фикстура: ни один unit-тест не вызывает настоящий Metadata AI |

`AIAnalysis`, Vision-промпт, `assets.status` и схема БД не менялись.

### Проверка

- `python -m pytest`: 138 passed, 3 skipped (LM Studio).
- Production, asset 3: Metadata AI на недоступном endpoint →
  `METADATA_AI/FAILED` + partial draft (9 keywords, `PARTIAL_DRAFT`,
  `FEW_KEYWORDS`); затем обычный `build` → `METADATA_AI/PASSED` + full draft
  (31 keyword). События 18–21.
- Production, asset 4 и 5: full drafts (32 и 30 keywords). У asset 4 старое
  событие Vision без provenance → `sources.vision.prompt_version = null`, как
  предусмотрено контрактом.
- Production, asset 2: `VISION_MISSING`, exit 1.
- Production, worker на новой фотографии `IMG_20260911_130507.jpg` → asset 6:
  `INGEST/DONE`, `QC/PASSED`, `AI/PASSED`, `METADATA_AI/PASSED`,
  `METADATA/DRAFTED`, `state = draft`. `worker --asset-id 5` →
  `METADATA_EXISTS`, без новых событий.
- Полный цикл review — на **копии** production-БД (production не
  изменялась): правка с «Moscow» → `UNCONFIRMED_CLAIM` → обычный approve
  отклонён → `approve --confirm-claims` → правка после approve возвращает в
  `draft`, подтверждение сохраняется → approve → reject с причиной. События
  `EDITED` / `APPROVED` (`confirmed_claims`) / `REJECTED` соответствуют
  контракту.
- В production нет ни одного `METADATA/APPROVED`: одобрение — решение
  человека, агент его не выполнял.

### Найдено и исправлено на реальных данных

Asset 6: keyword `ip20 rating` ошибочно считался утверждением `NUMBER`, хотя
`IP20` есть в `text_visible` Vision. Код требовал опоры для всех слов
keyword'а, а контракт — только для слов с цифрами. Исправлено по контракту,
добавлен тест, `rebuild 6` пересобрал draft без AI-вызова (claims → []).

### Известные ограничения

- После `worker --asset-id N --force` Vision пересчитывается, но существующие
  metadata остаются построенными по прежнему Vision (защита правок человека).
  Их устаревание видно по несовпадению `sources.vision.event_id` с последним
  `AI/PASSED`. Автоматическая пометка «stale» — кандидат на будущее.
- `PROPER_NOUN` не проверяет заголовки в Title Case (§4.7 контракта).
- Metadata AI может сделать вывод, который не является ни концептом, ни
  утверждением с цифрой или именем собственным (asset 5: «labels indicate
  industrial security use»). Такое детерминированно не ловится — для этого
  нужен review.

### Статус

🟢 DONE — metadata pipeline; drafts asset 3–6 ждут review человеком
(обновлено в §35L: review gate, человек нужен только для спорных случаев)

---

# 35L. 2026-09-26 — Review gate: автоматический проход безопасных объектов

### Решение пользователя

Stocker автоматизирует промышленный stock-поток, а не превращает обработку в
ручную модерацию. Человек нужен только для спорных случаев:

```text
низкий риск:  AI → metadata → validation → gate → auto_approved
высокий риск: AI → metadata → validation → gate → human_review → human approve/reject
```

- `auto_approved` не заменяет человеческий `approved`: это отдельное
  состояние, которое устанавливает **только** детерминированный gate.
- Текст на изображении сам по себе не повод для review. Технические маркировки
  (`IP20`, `220V`, `WARNING`, номера) и описательные надписи review не требуют;
  бренды, логотипы, названия компаний и юридические утверждения — требуют.
- Люди: узнаваемый человек (лицо, главный объект) → review; частичное или
  неидентифицируемое присутствие (рука, вид со спины, силуэт) → не блокирует.
- После любой правки (агентом или человеком): edit → validation → gate. Сама
  правка `auto_approved` не даёт.

### Реализовано (`metadata-v2`, `gate-v1`; контракт — `docs/METADATA_CONTRACT.md` §6A)

- `app/review_gate.py` — чистый gate: решения `auto_approved` /
  `human_review` / `deferred` (partial остаётся `draft`); классификация
  `text_visible`; уровень риска людей (`none` / `partial` / `recognizable` /
  `unclear`) из текста Vision без изменения `AIAnalysis`; юридические
  утверждения в metadata; эскалация до решения человека; аварийный
  выключатель `STOCKER_AUTO_APPROVE=0`. Решения человека gate не меняет никогда.
- `app/metadata.py` — gate автоматически после `build` / `rebuild` / `edit`
  в той же транзакции; новые операции `gate`, `escalate`; события
  `METADATA/GATED`, `METADATA/ESCALATED`; approve/reject снимают эскалацию.
  CLI: `python -m app.metadata gate|escalate N`. Синтаксис существующих команд
  не менялся.
- `app/metadata_builder.py` — только расширение допустимых состояний для
  approve/reject. Остальная логика builder не менялась.

### Проверка

- `python -m pytest`: 207 passed, 3 skipped.
- Production, `gate` для записей v1 — решения совпали с прогнозом контракта:
  asset 3, 4 → `auto_approved`; asset 6 (`KM6000-УХЛ4`, `IP20`, `TN-S`, серийный
  номер, дата, клеммы — всё `technical`) → `auto_approved`; asset 5 →
  `human_review` (`TEXT_BRAND_OR_LEGAL`: `АО "ШПЗ"` — название компании,
  которое Vision **не** вынес в `brands`).
- Уточнено в §35M: `gate-v1.1`, неясное присутствие людей не блокирует.
- Production, worker на новой фотографии `IMG_20260911_130530.jpg` → asset 7:
  `INGEST → QC → AI → METADATA_AI → DRAFTED → GATED(auto_approved)` без
  участия человека.
- Итог: `auto_approved` — 4, `human_review` — 1, человеческих `APPROVED` — 0.

### Известные ограничения

- Риск людей вычисляется по тексту Vision. При неопределённости решение —
  `human_review`. Следующий шаг при необходимости — отдельная AI-проверка людей
  по изображению.
- Бренд без юрформы, не распознанный Vision, рядом с цифрами (`SIEMENS 220V`)
  классифицируется как `technical`.

### Статус

🟢 DONE

---

# 35M. 2026-09-26 — Приоритет автоматизации; роль n8n

### Решения пользователя

1. **Цель — массовая автоматическая обработка industrial stock**, а не
   универсальная модерация с максимальной осторожностью. Человек подключается
   только там, где действительно нужно решение.
2. **Люди:** `human_review` — только узнаваемый человек (лицо видно, главный
   объект, нужен model release). Рука с инструментом, часть тела, вид со
   спины, силуэт, рабочий, случайно попавший в кадр, автопроход не блокируют.
   Определение узнаваемости по изображению — возможное будущее улучшение.
3. **Текст:** техническая маркировка и описательные надписи не блокируют;
   бренды, логотипы, названия компаний, юрлица, юридические утверждения →
   `human_review` (без изменений относительно §35L).
4. **Роли в целевой архитектуре** (зафиксировано):

| Компонент | Роль |
|---|---|
| **OpenClaw** | агент: принятие решений, работа с инструментами |
| **n8n** | workflow engine: расписания, очереди, retries, интеграции |
| **Stocker Core** | бизнес-логика, состояние, обработка изображений |

5. **n8n сейчас не устанавливается и не подключается.** Порядок: service layer
   → JSON CLI → интерфейс для внешних инструментов (MCP) → затем n8n.

### Реализовано

- `gate-v1.1`: `unclear` (люди есть, признаков узнаваемости нет) → note
  `PEOPLE_INCIDENTAL` вместо review. `recognizable` → `PEOPLE_RECOGNIZABLE`
  без изменений. Версия политики поднята, чтобы по событиям `GATED` было видно,
  по каким правилам принято решение.
- Контракт (`METADATA_CONTRACT.md` §6A.3, §6A.4a) обновлён.

### Проверка

`python -m pytest`: 209 passed, 3 skipped. Production-решения asset 3–7 не
меняются: людей на этих кадрах нет.

### Статус

🟢 DONE

---

# 35N. 2026-09-26 — Service layer и JSON CLI

### Реализовано (контракт — `docs/SERVICE_CONTRACT.md`)

| Файл | Что |
|---|---|
| `app/service/__init__.py` | `dispatch(operation, params, actor) -> envelope`; права actor; ошибки → envelope (никогда не бросает) |
| `app/service/registry.py` | реестр 15 операций: Pydantic-модели параметров (`extra="forbid"`) → JSON Schema, уровни `read` / `pipeline` / `review` |
| `app/service/operations.py` | обработчики поверх `worker` и `app.metadata`, без бизнес-логики |
| `app/service/views.py` | asset view, производный `pipeline` (source/qc/vision/metadata/ready/review_reasons), `allowed_actions`, list, history, review queue |
| `app/api.py` | JSON CLI `python -m app.api <operation> --params '{..}' \| - [--actor] [--pretty]`; stdout — только envelope, вывод worker'а → stderr |
| `app/database/db.py` | `acting_as(actor)`: actor в JSON-сообщениях событий |
| `app/qc.py` | событие QC через `insert_event` (чтобы получить actor); поведение прежнее |
| `app/worker.py` | `_ingest_and_process` → публичный `ingest_and_process` |

Существующие CLI (`app.worker`, `app.metadata`), review-логика, схема БД и
`assets.status` не менялись.

### Права

- `read`, `pipeline` — любой actor (`human`, `agent:*`, `workflow:*`).
- `review` (approve/reject) — только `human`; агент и workflow получают `FORBIDDEN`.
- `auto_approved` не задаётся ни одним параметром — только review gate.
  Параметр `state` отклоняется как `INVALID_PARAMS`.
- Агент может поднять риск (`metadata.escalate`), но не снять его.

### Проверка

- `python -m pytest`: 248 passed, 3 skipped.
- Production, только через `python -m app.api --actor agent:openclaw`:
  `review.queue` → [5 (`TEXT_BRAND_OR_LEGAL`)]; `asset.process_file` для
  `IMG_20260911_130601.jpg` → asset 8, `AI_PASSED`, `auto_approved`;
  `metadata.approve` → `FORBIDDEN`, exit 1, без изменений. У всех событий
  asset 8, кроме текстового `INGEST`, `actor = agent:openclaw`.
  Человеческих `APPROVED` — 0.
- Копия production-БД: правка агентом с «ISO 9001 certified» → `human_review`
  (`LEGAL_CLAIM`, `UNCONFIRMED_CLAIM`); удаление утверждения → gate сам вернул
  `auto_approved`; эскалация агентом держится после `gate`; approve агентом →
  `FORBIDDEN`, человеком → `approved`; `gate` после решения человека →
  `INVALID_TRANSITION`.

### Наблюдения

- `metadata.review_gate` хранит последнюю оценку gate. После решения
  человека там видны причины, по которым объект был в review. Текущие
  причины для очереди — `pipeline.review_reasons` (только в `human_review`).
- `asset.list` фильтрует по производному состоянию в Python — достаточно для
  текущего объёма. При росте каталога понадобится индексируемое хранение
  состояния (кандидат для явной модели статусов).

### Следующий шаг

MCP-адаптер поверх реестра для OpenClaw (`SERVICE_CONTRACT.md` §7, шаг 5 §9).

### Статус

🟢 DONE

---

# 35O. 2026-09-26 — MCP-сервер; изоляция OpenClaw в WSL

### Реализовано

`app/service/mcp_server.py` (`python -m app.service.mcp_server`, SDK `mcp` 2.2):

- 13 инструментов генерируются из реестра (`asset_get`, `review_queue`,
  `asset_process_file`, `metadata_edit`, `metadata_escalate`, ...) с JSON Schema
  параметров и подсказками read-only/idempotent;
- операции `review` (approve/reject) агенту **не показываются и не
  выполняются**;
- actor задаёт сервер (`STOCKER_MCP_ACTOR`, по умолчанию `agent:openclaw`).
  `human` и некорректные значения — отказ при запуске;
- stdout — канал протокола: весь вывод worker'а перенаправлен в stderr;
  `dispatch` выполняется в отдельном потоке (LM Studio может отвечать минутами).

### Проверка

- `python -m pytest`: 257 passed, 3 skipped. Среди них настоящая stdio-сессия:
  MCP-клиент запускает сервер отдельным процессом на изолированной БД и
  выполняет `asset_process_file` (с выводом worker'а) → `metadata_edit` →
  `asset_history` → `metadata_approve`. Протокол не нарушен, правка ушла в
  `human_review`, actor записан, approve недоступен.

### Находка: OpenClaw изолирован от Windows

Запуск Windows-сервера из WSL (`OpenClawGateway`) через interop завершился
`Exec format error`. Причина — намеренная настройка `/etc/wsl.conf`:
`[interop] enabled=false`, `appendWindowsPath=false`. Это правильная изоляция
агента, её **не меняем**. Вариант «stdio через interop» из
`SERVICE_CONTRACT.md` §7 отпадает. Нужен сетевой транспорт — запасной вариант,
заложенный в контракт.

Сеть WSL — NAT: хост Windows из WSL виден как `172.26.192.1` (адрес может
меняться после перезагрузки); LM Studio — `192.168.1.104`.

### Решение пользователя (ожидается)

Способ подключения OpenClaw: MCP streamable HTTP из Windows — адрес привязки,
токен авторизации, правило firewall.

### Статус

🟡 IN PROGRESS — сервер готов; транспорт для OpenClaw ждёт решения
(завершено в §35P)

---

# 35P. 2026-09-26 — OpenClaw подключён к Stocker через MCP

### Цель

Рабочая связка `OpenClaw → MCP → Stocker Service Layer → Stocker Core → SQLite`.

### Ход

1. Проверка до изменений: WSL 2.7.14 (NAT), Windows build 26200; LM Studio
   слушает `0.0.0.0:1234`; OpenClaw ходит в LM Studio по
   `http://172.26.192.1:1234/v1` (адрес NAT-адаптера WSL); Docker Desktop на
   WSL-движке, контейнеры остановлены; Hyper-V firewall для WSL: входящие `Block`.
2. **Mirrored networking** (выбор пользователя) — пользователь включил вручную.
   WSL не смог его настроить: `CreateInstance/CreateVm/ConfigureNetworking/0x8007054f`,
   откат в `networkingMode None` (без сети), `Wsl/Service/E_UNEXPECTED`.
   Выполнен **откат** (пользователем, по командам): `.wslconfig` и
   `openclaw.json` восстановлены из резервных копий. Проверено: NAT, прежний
   адрес `172.26.192.1`, LM Studio 200, gateway здоров, Docker отвечает.
3. **Путь 1 (NAT):** `mcp_http` может слушать адрес адаптера
   `vEthernet (WSL)` (`STOCKER_MCP_HOST=wsl`, определяется при запуске, с
   ожиданием появления адаптера); LAN и `0.0.0.0` запрещены; токен обязателен.
   Токен создан пользователем в `.env`, сервер зарегистрирован в OpenClaw
   пользователем (`openclaw mcp add stocker ... --transport streamable-http`).
   `openclaw mcp probe`: `stocker: 13 tools`.
4. Серверный журнал вызовов (`logs/mcp_http.log`): время, инструмент, actor,
   asset_id, ok, outcome, error (без аргументов и токена).

### Проверка (агент OpenClaw `tardis`, модель `lmstudio/qwen3.8-9b-distill`)

| Тест | Результат | Доказательство |
|---|---|---|
| чтение asset | ✅ | журнал: `tool=asset_get actor=agent:openclaw asset_id=5 ok=True`; данные в ответе агента совпадают с БД |
| pipeline-операция | ✅ | журнал: `tool=metadata_gate ... asset_id=3 ok=True outcome=GATED`; SQLite: событие 48 `METADATA/GATED`, `actor=agent:openclaw`, `decision=auto_approved` |
| approve/reject через агента | ✅ отказ | агент сообщил, что таких инструментов нет; вызовов в журнале нет |
| approve/reject напрямую по MCP (в обход LLM, с валидным токеном) | ✅ отказ | `UNKNOWN_OPERATION`; `metadata_edit` с `state: approved` → `INVALID_PARAMS`; `APPROVED`/`REJECTED` в БД — 0 |

### Находки

- **Локальная модель может выдумывать результат инструмента.** В первой
  попытке теста pipeline агент вернул правдоподобный, но полностью выдуманный
  ответ («HydroFlow Dynamics Corp», серийный номер, стоимость), не вызвав
  инструмент: в журнале и в БД вызова не было. С явной инструкцией вызвать
  инструмент тест прошёл. **Вывод:** истина — журнал сервера и события Stocker,
  а не текст ответа агента. Деструктивных последствий нет: состояние меняет
  только Stocker.
- **Gateway OpenClaw живёт, только пока к WSL подключена сессия `wsl.exe`.**
  Службы systemd дистрибутив в живых не держат: без сессии WSL гасит его
  примерно через минуту. Так было и до этой работы (gateway жил, пока была
  открыта консоль). Для постоянной работы нужна keep-alive сессия.
- `restart-loop breaker` OpenClaw после серии `wsl --shutdown` подавил
  автозапуск каналов. Каналы не настроены, поэтому последствий нет.
- Предупреждение плагина `codex` (state migration pending) было и до работы,
  к Stocker отношения не имеет.

### Статус

🟢 DONE — связка работает, автозапуск настроен.

### Автозапуск (зарегистрирован пользователем, проверен)

| Задача Планировщика | Что делает |
|---|---|
| `OpenClaw WSL keep-alive` | при входе: `conhost --headless wsl.exe -d OpenClawGateway --exec sleep infinity` — держит дистрибутив и gateway OpenClaw |
| `Stocker MCP` | при входе: `scripts/start_mcp_http.cmd` → `python -m app.service.mcp_http`, журнал `logs/mcp_http.log`; ждёт адаптер WSL до 5 мин |

Обе задачи: без лимита времени, перезапуск раз в минуту при сбое.
`.env`: `STOCKER_MCP_HOST=wsl`, `STOCKER_MCP_TOKEN`.

Проверка после запуска задач (временные процессы агента остановлены):
сервер задачи слушает `172.26.192.1:8765`; `openclaw mcp probe`: 13 tools;
агент вызвал `review_queue` — в журнале
`tool=review_queue actor=agent:openclaw ok=True`, ответ совпал с реальной
очередью (asset 5); gateway работает непрерывно с момента старта задачи,
без `SIGTERM`.

Удаление: `Unregister-ScheduledTask -TaskName "<имя>" -Confirm:$false`.

---

# 35Q. 2026-09-26 — Модели: Stocker на qwen3-vl-8b-instruct, qwen3.8 — только эксперимент агента OpenClaw

### Архитектурная договорённость (напоминание пользователя)

`qwen3-vl-8b-instruct` выбрана как **единая универсальная локальная модель**
(vision + reasoning + tools), чтобы не вводить несколько локальных моделей без
необходимости. Роли Vision / Metadata AI / агента остаются разными
архитектурно (отдельные провайдеры и конфигурации), но исполняются одной
локальной моделью.

### Проверенные факты

- **Stocker `qwen3.8-9b-distill` не использует нигде.** В `app/`, `scripts/`,
  `.env.example` ссылок нет; `.env` не переопределяет `LMSTUDIO_MODEL` и
  `METADATA_MODEL`; значения по умолчанию в `local_analyzer.py` и
  `metadata_analyzer.py` — `qwen3-vl-8b-instruct`. Provenance в SQLite: все
  `AI/PASSED` (5) и `METADATA_AI/*` (7) — `qwen3-vl-8b-instruct`, плюс одно
  старое текстовое событие asset 4 (тоже Qwen3-VL, §35B).
- **`qwen3.8-9b-distill` — модель агента OpenClaw**:
  `agents.defaults.model.primary = lmstudio/qwen3.8-9b-distill`. Настроена в
  OpenClaw до этой работы (есть в резервных копиях конфига от 20–21.09; в
  паспорте §5 — «использовалась в эксперименте OpenClaw»). В §35P агентские
  тесты шли на ней, потому что это модель OpenClaw по умолчанию. Её не выбирали
  для Stocker.
- В списке моделей провайдера `lmstudio` в OpenClaw есть `qwen3.8-9b-distill`
  и `qwen2.5-vl-7b-instruct`, а `qwen3-vl-8b-instruct` **нет**.

### Почему это важно

- **12 GB VRAM:** сейчас в LM Studio загружена только `qwen3.8-9b-distill`
  (после тестов агента). Следующий вызов Stocker заставит LM Studio выгрузить
  её и загрузить `qwen3-vl-8b-instruct`. При чередовании «агент ↔ Stocker»
  модели будут постоянно перезагружаться — именно этого избегает решение про
  одну модель.
- Выдумывание результата инструмента (§35P) наблюдалось на `qwen3.8`. В
  проектном benchmark (§17) `qwen3-vl-8b-instruct` показала корректный
  structured tool call (один тест).

### Решение

`qwen3.8-9b-distill` считается **только экспериментом agent loop OpenClaw**.
Целевое состояние — агент OpenClaw тоже на `qwen3-vl-8b-instruct`. Переключение
меняет конфиг OpenClaw и выполняется после подтверждения пользователя.

### Переключение (выполнено с подтверждением пользователя)

- LM Studio (пользователь): контекст загрузки `qwen3-vl-8b-instruct` = 16384.
- OpenClaw: резервная копия `~/.openclaw/openclaw.json.bak-before-qwen3vl`;
  в `models.providers.lmstudio.models` добавлена `qwen3-vl-8b-instruct`
  (`contextWindow` 16384, `maxTokens` 8192, `supportsTools`);
  `agents.defaults.model.primary = lmstudio/qwen3-vl-8b-instruct`. Применено
  без перезапуска gateway; MCP-сервер `stocker` на месте.
- В LM Studio после вызова агента загружена одна модель —
  `qwen3-vl-8b-instruct` (ctx 16384). Цель «одна модель в VRAM» достигнута.

### Блокер: промпт агента OpenClaw не помещается в 16k

Журнал OpenClaw (`/tmp/openclaw/openclaw-2026-09-26.log`):

- `asset_get` вызван реально (журнал Stocker `11:49:43 tool=asset_get ... ok=True`),
  но ответ оборван: `insufficient_output_budget ... effectiveContext=16384
  estimatedInput=18201`;
- даже с маленьким результатом (`review_queue`): `context-pressure-diagnostic
  route=compact_only estimatedPromptTokens=16174 promptBudgetBeforeReserve=12288`.
  В этом режиме `qwen3-vl` вызвала несуществующий инструмент `stocker` вместо
  `stocker__review_queue` и честно сообщила об ошибке, ничего не выдумав.

Вклад Stocker небольшой: 13 определений инструментов ≈ 1,9k токенов,
`asset_get` ≈ 1,6k, `review_queue` ≈ 150. Основной объём (~14k) — собственный
промпт агента OpenClaw (13 плагинов, файлы workspace, их инструменты).

VRAM RTX 3080 Ti: занято 11,57 из 12,29 GB при `qwen3-vl` + 16k. Контекст 32k
в VRAM не помещается без квантования KV-кеша.

`qwen3.8` работала в том же бюджете (16384 в LM Studio, в OpenClaw указано
`contextWindow` 262144 / `contextTokens` 16384). Выдуманный результат в §35P,
вероятно, связан с той же нехваткой контекста.

**Вывод:** надёжный агентский цикл на локальной модели 12 GB требует
компактного промпта агента. Это задача конфигурации OpenClaw, а не Stocker и не
выбора модели. Серверные гарантии Stocker (approve/reject недоступны, actor,
журнал) от модели не зависят.

### Статус

🟢 DONE — факты и переключение; 🔴 BLOCKED — надёжный агентский цикл
(промпт агента OpenClaw > контекста 16k), решение пользователя
(блокер контекста снят в §35R: mmproj F16 + KV q8_0 + 32k)

---

# 35R. 2026-09-26 — qwen3-vl: mmproj F16 + KV q8_0 + контекст 32k (без смены весов)

### Цель

Снять нехватку контекста агента OpenClaw (§35Q), сохранив ту же модель и
архитектуру «одна локальная модель».

### Замеры до изменений

- Фон GPU без модели: 1 203 MiB. С `qwen3-vl` (Q5_K_M, 16k) — 11 569 MiB,
  т.е. модель ≈ 10,1 GiB: веса 5,45 + **`mmproj-F32` 2,15** + KV fp16 16k 2,25
  (36 слоёв × 8 KV-голов × 128 → 144 KiB/токен) + буферы ~0,3.
- Расчёт показал: Q4_K_M + 24k при F32-энкодере **не помещается** (~10,6 GiB);
  главный резерв — визуальный энкодер (F32 → F16 ≈ −1,07 GiB) и KV q8_0.
- Локальные файлы сверены по SHA256 с `unsloth/Qwen3-VL-8B-Instruct-GGUF` —
  та же сборка.

### Решение пользователя и изменения (выполнены пользователем)

- Веса без изменений: `Qwen3-VL-8B-Instruct-Q5_K_M.gguf`.
- `mmproj-F16.gguf` из того же репозитория (SHA256 `d406d03e…fc4` проверен);
  `mmproj-F32.gguf` перенесён в `F:\ai\model-backup\Qwen3-VL-8B-Instruct-GGUF\`
  (не удалён).
- Настройки загрузки **по умолчанию** (важно: JIT-загрузки Stocker и OpenClaw
  используют именно их): контекст 32768, K/V cache `q8_0`. Первая попытка
  (квантование только в диалоге загрузки) не применилась — KV был fp16,
  `llama-server` 11 542 MiB + 270 MiB в общей памяти.
- OpenClaw (агент): `models.providers.lmstudio.models[2].contextWindow = 32768`
  (резервная копия `openclaw.json.bak-before-ctx32k`).

### Проверка

| Что | Результат |
|---|---|
| mmproj | лог LM Studio: `loaded multimodal model, '.../mmproj-F16.gguf'`, `n_ctx_slot = 32768`, cache `Q8_0` |
| VRAM | `llama-server` 9 585 MiB (расчёт ≈ 9,4 GiB), свободно ~2 GiB, общая память ~0 |
| Benchmark (`scripts/model_benchmark`) против 22.09 | профиль идентичен: 6/7 (как и было, `stock_analysis` без strict schema не проходит; production использует `json_schema`); `tool_calling` — структурированный вызов; скорость та же |
| `pytest -m lmstudio` | 3/3 |
| Vision на asset 5 и 6 (без записи в БД) | `text_visible` совпал **символ в символ** (`АО "ШПЗ"`, `KM6000-УХЛ4`, `ВИД ЗАЗЕМЛЕНИЯ TN-S`, ...); решения gate прежние: 5 → `human_review`, 6 → `auto_approved` |
| Контекст агента OpenClaw | в прогоне с 32k нет ни одного `insufficient_output_budget` / `context-pressure` |
| Агент: чтение | ✅ `13:10:22 tool=asset_get ... ok=True`, ответ точный |
| Агент: pipeline | ✅ `13:10:27 tool=metadata_gate ... outcome=GATED`, ответ точный |
| Агент: approve/reject | ✅ OpenClaw: инструментов не существует; вызовов в Stocker нет; `APPROVED`/`REJECTED` в БД — 0 |

### Оставшаяся проблема: протокол вызова инструментов OpenClaw

OpenClaw отдаёт MCP-инструменты через мета-инструменты `tool_search` →
`tool_describe` → `tool_call` с именами `stocker__<tool>`. С формулировкой
«вызови инструмент asset_get» `qwen3-vl` обращалась к несуществующим именам
(`stocker`, `metadata_gate`) — в Stocker вызовов не было. С явным
«`tool_call` → `stocker__asset_get`» — все тесты прошли. Нужна инструкция для
агента (skill / workspace) с точными именами инструментов Stocker. Это
значительно меньше, чем отдельный агент.

### Статус

🟢 DONE — контекст и VRAM; инструкция для агента — §35S

---

# 35S. 2026-09-26 — Skill Stocker для OpenClaw; агент сам выбирает инструменты

### Решение пользователя

Архитектура закреплена: `qwen3-vl-8b-instruct` Q5_K_M + `mmproj-F16` + KV
`q8_0` + контекст 32768; отдельный Brain-агент не вводится. Следующий шаг —
нормальная интеграция OpenClaw: skill с правилами работы с MCP.

### Реализовано

- **Skill** `integrations/openclaw/skills/stocker/SKILL.md` (исходник в
  репозитории; копия в `~/.openclaw/workspace/skills/stocker/SKILL.md`,
  OpenClaw: `stocker ✓ Ready`, `Visible to model: yes`). Содержит: протокол
  `tool_search` / `tool_describe` / `tool_call`, точную форму вызова
  `{"id": "stocker__<tool>", "args": {...}}` (взята из реальной записи OpenClaw),
  таблицу 13 инструментов с аргументами, правила: не выдумывать, отчитываться
  только данными из результата, approve/reject — только человек (с командой),
  изменяющие инструменты — только по просьбе, не добавлять лишние фильтры.
- **Описания инструментов в реестре Stocker** (`registry.DESCRIPTIONS`) — с
  точными аргументами и примером; для `asset_list` — фильтры отдельными полями,
  отсылка к `review_queue`. Контракт API не менялся.
- **`INVALID_PARAMS`** теперь содержит `Allowed params: ... (required) ...`
  для самоисправления агента.
- Тест HTTP-сервера явно задаёт `STOCKER_MCP_HOST=127.0.0.1`: рабочий `.env`
  теперь содержит `wsl` и не должен влиять на тесты.

### Ключевая находка

Модель OpenClaw видит **название и описание** skill, а тело читает только
тогда, когда сама решит. В первом прогоне она тело не прочитала и угадывала
аргументы по описаниям инструментов: `{"filter": "..."}`, `{"assetId": 6}`,
лишний фильтр `qc` → пустой результат и неверный ответ «ничего не ждёт
проверки»; на просьбу одобрить написала «Фото 5 одобрено» (сервер ничего не
одобрил). Поэтому основные рычаги — **описания инструментов Stocker, тексты
ошибок и описание skill**, а не только его тело.

### Проверка (обычные фразы на русском, без имён инструментов)

| Тест | Прогон 1 | Итог (после исправлений) |
|---|---|---|
| Общий вопрос «что происходит, сколько готово, что ждёт проверки» | `asset_list` ×2 → `INVALID_PARAMS` | `review_queue` ✅ верно про проверку; **часть «сколько готово» пропущена** (готовы 5: 3, 4, 6, 7, 8) |
| Состояние asset 6 | `asset_get` c `assetId` → `INVALID_PARAMS` | `metadata_get` ✅ заголовок, 30 keywords, `auto_approved` — сверено с БД |
| Что ждёт проверки и почему | `asset_list` с лишним `qc` → неверно «ничего» | `review_queue` ✅ asset 5, `TEXT_BRAND_OR_LEGAL`, `АО "ШПЗ"` |
| Просьба одобрить | «Фото 5 одобрено» (ложь; сервер не одобрил) | ✅ без вызовов; «решение человека» + `python -m app.metadata approve 5` |

Во всех прогонах `APPROVED`/`REJECTED` в БД — 0.

### Ограничения

- Составной вопрос 8B-модель закрывает одним вызовом — часть вопроса может
  остаться без ответа. Возможное улучшение без смены архитектуры: сводка
  (`ready` / `human_review` / всего) в ответе `review_queue`.
- Ответ агента по-прежнему не является доказательством: проверка — журнал
  Stocker и события.
- В workspace OpenClaw лежат старые копии кода Stocker (`app/`,
  `local_analyzer.py` от 21.09) — остатки ранних экспериментов, к текущей
  интеграции не относятся.
- Обновление skill: скопировать файл из репозитория в
  `~/.openclaw/workspace/skills/stocker/`; после изменения описаний
  инструментов — перезапустить задачу `Stocker MCP` и `openclaw mcp reload`.

### Статус

🟢 DONE — агент сам выбирает инструменты для чтения, очереди и отказа в
approve; составные вопросы закрыты сводкой `review_queue` (§35T)

---

# 35T. 2026-09-26 — Интеграция OpenClaw завершена

### Реализовано

- **Сводка в `review_queue`** (один вызов закрывает вопрос «что происходит»):
  `data.summary` = `total_assets`, `ready`, `by_metadata_state` (none / draft /
  auto_approved / human_review / approved / rejected), `problem_assets` (id,
  filename, причины: `SOURCE_CHANGED`, `SOURCE_MISSING`, `QC_FAILED`,
  `VISION_FAILED`, причины review gate, `METADATA_PARTIAL`,
  `METADATA_NOT_GATED`). `items` — очередь `human_review`, как раньше
  (совместимо). Описание инструмента и skill обновлены.
- **Workspace OpenClaw очищен.** Устаревшие копии кода Stocker (`app/`,
  `local_analyzer.py` от 21.09; все отличались от текущего кода) перенесены
  из `~/.openclaw/workspace` в `~/.openclaw/archive/workspace-stocker-copies-20260926/`
  (с README; безвозвратное удаление — на усмотрение пользователя). В workspace
  остались инструкции (`AGENTS.md`, `IDENTITY.md`, `SOUL.md`, `USER.md`),
  `skills/` и собственная память OpenClaw (`memory/`, `DREAMS.md`).
  Источник истины кода — только `F:\stock\stocker`.
- **Принципы §3A.5 и §3A.6** зафиксированы: AI/OpenClaw только инициирует,
  истина — Stocker Core и события; одна локальная модель `qwen3-vl-8b-instruct`
  для всех ролей.

### Проверка

- Production (только чтение): `total_assets` 7, `ready` 5, `auto_approved` 5,
  `human_review` 1, `none` 1; проблемные — asset 2 (`SOURCE_CHANGED`) и asset 5
  (`TEXT_BRAND_OR_LEGAL`). Ответ ≈ 230 токенов.
- Агент, составной вопрос «что происходит, сколько готово, что ждёт
  проверки» (без имён инструментов): вызовы `operations_list` + `review_queue`;
  ответ полный и совпадает с БД (7 / 5 / 1, причины, оба проблемных объекта).
- `python -m pytest`: 277 passed, 3 skipped.

### Итог интеграции OpenClaw

```text
OpenClaw (qwen3-vl-8b-instruct, skill stocker)
   → MCP HTTP (172.26.192.1:8765, токен, actor agent:openclaw)
   → Stocker Service Layer (dispatch, права: без approve/reject)
   → Stocker Core → SQLite events (истина)
```

### Статус

🟢 DONE — интеграция OpenClaw завершена. Следующий этап — n8n (роль — §35M).

---

# 35U. 2026-09-26 — Стабилизация перед n8n

### Решение пользователя

Этап OpenClaw признан правильным: OpenClaw только управляет через MCP;
Stocker Core + события SQLite — единственный источник состояния;
`qwen3-vl-8b-instruct` — единая локальная модель; approve/reject — только
человек. Перед n8n — стабилизация: архитектура, восстановление, отсутствие
ручных зависимостей. n8n — только scheduler, интеграции, уведомления,
очереди; прав не больше, чем у OpenClaw.

### Найдено и исправлено

- **Брешь в правах:** агент мог вызвать `metadata.edit` / `rebuild` /
  `build --force` над объектом, который человек одобрил или отклонил; правка
  возвращала его в `draft`, и gate мог сделать `rejected` → `auto_approved` в
  обход человека. Теперь для не-человеческих actor это `FORBIDDEN`
  (`app/service/__init__.py`, `_human_decision_guard`); 14 новых тестов.
- **Условие для n8n:** JSON CLI принимает `--actor` от вызывающего. n8n
  подключать только через канал с actor от сервера (§3B).

### Эксплуатация

- `docs/RECOVERY.md`: что где хранится (код, БД, фото, `.env`, модель,
  LM Studio, WSL/OpenClaw, задачи), резервное копирование, порядок
  восстановления, SHA256 модели, параметры загрузки, настройки OpenClaw без
  секретов, риски.
- `ops/lmstudio/`: копии настроек сервера LM Studio и параметров загрузки
  `qwen3-vl` (без секретов) — восстановимы из git.
- `requirements.lock.txt` — точные версии (41 пакет).
- `scripts/check_environment.ps1` (+ `scripts/openclaw_check.py` в WSL) —
  проверка без изменений и без вывода секретов: git, `.env`, SQLite
  `quick_check`, LM Studio и модель (mmproj F16, ctx 32768, KV q8_0), MCP на
  адресе WSL, задачи Планировщика, `.wslconfig`, WSL (NAT, interop выключен),
  gateway, модель агента, адреса LM Studio/MCP в OpenClaw совпадают с текущим
  адресом WSL-хоста, токен, skill совпадает с репозиторием, workspace без кода,
  связь WSL → LM Studio и WSL → MCP (401 без токена).

### Проверка

`check_environment.ps1`: все проверки `PASS`, кроме двух ожидаемых `WARN`
(незакоммиченные файлы этого этапа; **28 коммитов не отправлены на GitHub**).
Ручных зависимостей нет: MCP, keep-alive и LM Studio стартуют при входе;
токен в `.env` и в OpenClaw; адреса совпадают.
`python -m pytest`: 291 passed, 3 skipped.

### Риски (в RECOVERY.md §7)

Смена адреса NAT-подсети WSL; автозапуск только при входе пользователя;
обновление LM Studio может сбросить параметры загрузки; неотправленные коммиты.

### Статус

🟢 DONE — стабилизация. Следующий этап — n8n.

---

# 35V. 2026-09-26 — Решения по n8n; контракт n8n; Image Enhancement в архитектуре

### Решения пользователя

1. **n8n в Docker Desktop** — изоляция, переносимость и восстановление,
   разделение компонентов, перенос workflow.
2. **Права n8n (урезанные):** читать состояние, очереди, статусы; запускать
   pipeline и обработку новых файлов; `metadata.build` / `metadata.gate`.
   **Не может:** approve/reject, менять решения человека, выдавать себя за
   `human`.
3. **Уведомления** — слой в n8n; Stocker не привязывается к мессенджеру.
   Начать с тестового канала; позже Telegram, e-mail, MAX.
4. **Image Enhancement** (Topaz) — будущий этап в архитектуре (§2); оригиналы
   не изменяются, Topaz создаёт производные файлы. Пока не реализуется.

### Проверка до реализации

Контейнер `busybox` (локальный образ, без загрузки; сеть `bridge`):
`http://172.26.192.1:8765/mcp` → `401` без токена (Stocker доступен);
`host.docker.internal:8765` → нет ответа (сервер слушает только адрес
WSL-адаптера — так и остаётся); LM Studio через `host.docker.internal:1234` → 200.
n8n обращается к Stocker по тому же адресу, что и OpenClaw.

### Контракт

`docs/N8N_CONTRACT.md`: роль, топология, HTTP API `POST /api/v1/{operation}`
(envelope, HTTP 200), токены по каналам (`STOCKER_MCP_TOKEN` → `agent:openclaw`
только `/mcp`; `STOCKER_N8N_TOKEN` → `workflow:n8n` только `/api/v1`),
allowlist `workflow:n8n`, операция `incoming.list`, workflows v1
(ingest, retry, digest, notify), эксплуатация, порядок реализации.

### Статус

🟢 DONE — решения и контракт; 🟡 IN PROGRESS — реализация (§8 контракта)

---

# 35W. 2026-09-26 — n8n: канал Stocker, контейнер, workflows v1 (до включения)

### Реализовано и проверено

- **Stocker:** HTTP API `/api/v1/{operation}` (тот же процесс, что MCP), токены
  по каналам (`STOCKER_MCP_TOKEN` → только `/mcp`, `agent:openclaw`;
  `STOCKER_N8N_TOKEN` → только `/api/v1`, `workflow:n8n`; одинаковые токены
  отвергаются), allowlist `workflow:*`, операция `incoming.list`. 305 тестов.
- **Production-проверка канала** из контейнера `busybox` с настоящими токенами:
  `review.queue` / `incoming.list` — ok; `metadata.edit`, `metadata.approve`,
  `asset.process` с `force` → `FORBIDDEN`; токен OpenClaw на `/api/v1`, токен
  n8n на `/mcp`, запрос без токена → 401. Журнал сервера: `API op=...
  actor=workflow:n8n`.
- **n8n 2.40.7** в Docker Desktop (с подтверждения пользователя; образ ~1,04 GB,
  digest `sha256:ffeb5248…6c34`, запуск по digest): контейнер `n8n`,
  `restart unless-stopped`, UI только `127.0.0.1:5678`, volume `n8n_data`,
  `TZ=Asia/Barnaul`, `STOCKER_URL=http://172.26.192.1:8765`; секретов в окружении
  нет. Из контейнера n8n Stocker API → 401 без токена.
- **Workflows v1** в `integrations/n8n/workflows/` (без токенов):
  `stocker-ingest` (5 мин), `stocker-retry` (30 мин, ≤3 неудачи по событиям
  Stocker), `stocker-digest` (09:00), `stocker-notify` (тестовый канал).
  Развёртывание — `scripts/deploy_n8n_workflows.ps1` (id credential
  подставляется; зашифрованные данные не выводятся; импорт выключенными).
- **Эксплуатация:** n8n в `RECOVERY.md` (§1, §2, §3, §7, §8) и в
  `check_environment.ps1` (контейнер, UI только loopback, доступ к Stocker,
  `STOCKER_URL`, импорт workflows).

### Ожидает пользователя

Создание владельца n8n и credential «Stocker API» (Header Auth) — учётные
данные вводит пользователь. **Выполнено пользователем.**

### Развёртывание и сквозная проверка (26.09.2026)

- `deploy_n8n_workflows.ps1`: credential найден, 4 workflow импортированы.
  Исправлена очистка временных файлов в контейнере (от root).
- **Особенности n8n 2.40:** `n8n execute` стартует только от Execute
  Workflow Trigger — в `ingest`, `retry`, `digest` добавлен вход «Run on
  demand» (ручной запуск / вызов из другого workflow; основной триггер —
  расписание). Вызываемый подпроцесс должен быть **опубликован**:
  `stocker-notify` опубликован (`n8n publish:workflow` + перезапуск
  контейнера); расписаний у него нет. CLI-запуск рядом с сервером требует
  отдельного порта брокера (`N8N_RUNNERS_BROKER_PORT=5690`).
- **`stocker-ingest` на новой фотографии** пользователя
  `MVIMG_20260926_135350.jpg`: `incoming.list` → `asset.process_file` →
  asset 9: QC → Vision → Metadata AI → draft → gate → **`auto_approved`**
  («Wooden Playground with Rope Netting in Urban Park»). Все события, кроме
  текстового `INGEST`, — `actor=workflow:n8n`. Человек не участвовал.
- **`stocker-digest` → `stocker-notify`**: тестовый канал, `severity:
  attention`, текст по `review_queue.summary`: всего 8, готово 6,
  `human_review` 1, требуют внимания #2 (`SOURCE_CHANGED`) и #5
  (`TEXT_BRAND_OR_LEGAL`).
- **`stocker-retry`**: без ошибок, повторять нечего (2 вызова `asset.list`).
- `check_environment.ps1`: все `PASS` (включая n8n и импорт workflows).

### Статус

🟡 IN PROGRESS — всё проверено вручную; расписания (`ingest`, `retry`,
`digest`) не включены — ждут подтверждения пользователя

---

# 35X. 2026-09-26 — Расписания n8n включены поэтапно; ограничители retry

### Решения пользователя

1. Включить `stocker-ingest` и `stocker-digest`.
2. `stocker-retry` — выключен до накопления статистики; перед включением —
   ограничители: максимум объектов за запуск, уведомление при повторных
   ошибках, защита от бесконечных повторов.
3. Image Enhancement — будущий этап: **QC → Topaz (если требуется) → QC →
   Vision** (подробно — §2).
4. **Экспорт на стоки не реализуется** — следующий слой после стабильной
   работы pipeline.

### Реализовано и проверено

- `stocker-ingest`, `stocker-digest` опубликованы (`n8n publish:workflow` +
  перезапуск контейнера); `stocker-notify` опубликован ранее; `stocker-retry`
  не опубликован. Плановые вызовы `incoming.list` от `workflow:n8n` видны в
  журнале Stocker.
- `stocker-retry` перестроен в одну линейную цепочку (без Merge):
  кандидаты (Vision failed + partial) → история → план (лимит 3 неудачи по
  событиям Stocker) → не более 5 объектов за запуск → вызов с изоляцией сбоев
  → отчёт; исчерпавшие лимит → уведомление только о новых (статические данные
  workflow). Контракт n8n §6.1.
- `integrations/n8n/tests/retry_logic.test.js` — проверка кода узлов из JSON
  workflow в Node.js контейнера n8n: все сценарии проходят.
- Реальный запуск нового `stocker-retry` в n8n — без ошибок (повторять нечего).
- `deploy_n8n_workflows.ps1 -Only <имя>` — выборочное развёртывание; скрипт
  печатает опубликованные workflow. Выборочный импорт `stocker-retry` не
  снял публикацию с `ingest` / `digest` / `notify`.

### Статус

🟢 DONE — `ingest` и `digest` работают по расписанию; ⚪ `retry` готов, ждёт
решения о включении

---

# 35Y. 2026-09-26 — Baseline перед включением retry

### Решение пользователя

Перед включением `stocker-retry` зафиксировать текущую архитектуру как
baseline. `retry` — выключен до накопления статистики ошибок. Следующий этап
после стабилизации — **оценка готовности к автоматическому экспорту на стоки**.

### Зафиксировано

- **`docs/BASELINE_2026-09-26.md`** — архитектура, версии всех компонентов
  (Stocker, контракты, модель, LM Studio 0.4.25, OpenClaw 2026.9.5, n8n 2.40.7
  по digest, Docker, хост), права, автоматизация, состояние данных,
  доказательства, открытые вопросы. Git-метка `baseline-2026-09-26`.
- **Image Enhancement** (§2): QC → Topaz (при необходимости) → QC → Vision;
  оригиналы не изменяются, Topaz создаёт **derivative**-файлы.
- **Экспорт на стоки** — отдельный будущий этап после стабильности pipeline и
  оценки готовности.
- **Состояние уведомлений — в будущем в событиях Stocker** (принцип §3A.5):
  сейчас «что уже сообщено» хранится в статических данных n8n
  (`stocker-retry`). Запланирована операция Stocker, фиксирующая факт
  уведомления событием (`NOTIFY/SENT`: канал, тип, объекты), — тогда история
  уведомлений переживёт пересоздание n8n и будет видна агенту.

### Найдено

- Git: `main` опережает `origin/main` на 2 коммита (+ коммит этого этапа);
  отправка — действие пользователя.
- **Задача `LMStudioAutoServer` сломана:** путь `…\Programs\lm-studio\LM`
  не существует (LM Studio установлен в `…\Programs\LM Studio\`), при каждом
  входе — `0x80070002`. LM Studio фактически стартует через ключ `Run`
  (`LM Studio.exe --run-as-service`). `check_environment.ps1` раньше давал
  ложный `PASS` (проверял только наличие задачи) — теперь проверяет путь в
  `Run` (`PASS`) и цель задачи (`WARN`). Удаление задачи — действие
  пользователя.

### Проверка

`check_environment.ps1`: все `PASS`; `WARN` — незакоммиченное/неотправленное
состояние и устаревшая задача `LMStudioAutoServer`. `pytest`: 305 passed.

### Статус

🟢 DONE — baseline зафиксирован

---

# 35Z. 2026-09-26 — Состояние уведомлений в событиях Stocker (NOTIFY/SENT)

### Решение пользователя

1. Состояние уведомлений хранить как события Stocker, а не только в n8n
   (принцип §3A.5).
2. `stocker-retry` не включать до накопления статистики.
3. Следующий этап называется **Stock Readiness**: проверка metadata и
   соответствия требованиям Adobe Stock / Shutterstock **без фактической
   загрузки**. Экспорт через API — только после прохождения этого этапа.
4. Устаревшую задачу `LMStudioAutoServer` удаляет пользователь (автозапуск LM
   Studio — ключ `Run`, §35Y).
5. После стабилизации пользователь отправляет baseline и коммиты в GitHub.

### Реализовано

- Операция **`notification.record`** (`pipeline`): события `NOTIFY/SENT`
  `{channel, kind, severity, title, key, actor}` на каждый объект, одна
  транзакция, идемпотентность по (`asset`, `kind`, `key`), исходы `RECORDED` /
  `ALREADY_SENT`. Контракт — `SERVICE_CONTRACT.md` §3.
- **Права:** разрешена `workflow:n8n` (allowlist); **запрещена агентам**
  (`AGENT_FORBIDDEN`, скрыта из списка MCP-инструментов) — агент не может
  объявить уведомление доставленным.
- **n8n:** `stocker-notify` после канала записывает факт доставки в Stocker;
  `ingest` / `digest` / `retry` передают `kind` и `items`. `stocker-retry`
  определяет «уже сообщено» по `NOTIFY/SENT` из `asset.history`; статические
  данные n8n больше не используются (тест это проверяет).
  `N8N_CONTRACT.md` §6.2.

### Проверка

- `pytest`: 312 passed, 3 skipped (`tests/test_notifications.py` — 7 тестов).
- `retry_logic.test.js` в контейнере n8n: все проверки проходят.
- Реальный запуск `stocker-digest`: события 56, 57 `NOTIFY/SENT` для assets 2
  и 5, `kind=daily_digest`, `key=2026-09-26`, `actor=workflow:n8n`; журнал
  `API op=notification.record ... outcome=RECORDED`.
- Повторный запуск: `outcome=ALREADY_SENT`, новых событий нет.
- Опубликованы `ingest`, `digest`, `notify`; `retry` — не опубликован.

### Статус

🟢 DONE — состояние уведомлений в событиях Stocker

---

# 35ZA. 2026-09-26 — Границы Stock Readiness и AI Advisors (до реализации)

### Решение пользователя

Изначальная цель проекта — оценка пригодности фотографии для стокового
рынка, а не только техническая валидация. Разделить:

1. **Deterministic Stock Readiness** — технические требования площадок и
   metadata, локально, правилами.
2. **Commercial / Creative Review** — коммерческий потенциал, композиция,
   стоит ли тратить время на обработку, нужен ли Topaz, нужна ли расширенная
   проверка. Отдельный **необязательный** этап — место для будущего AI Advisor;
   облачная модель не становится обязательной зависимостью.

Порядок: QC → Enhancement decision → Vision → Metadata → Stock Readiness →
(optional) Creative Review Advisor → Export.

Topaz не обязателен; результат анализа — `enhancement_not_needed` /
`enhancement_recommended` / `enhancement_risky`. Решение пока принимает
локальная модель, с возможностью передать этап более сильной модели.

Главная цель Stock Readiness — подготовить объект к автоматическому экспорту.

### Зафиксировано

- Принцип §3A.7 (правила отдельно от советов AI).
- **`docs/STOCK_READINESS_CONTRACT.md`** (проект, `readiness-v1`): профили
  Adobe Stock и Shutterstock с источниками (проверены 26.09.2026), проверки с
  уровнями `blocker` / `derivative` / `warning`, статусы по площадкам
  (`not_evaluated` / `ready` / `blocked` / `stale`), **план экспорта** (точные
  поля, категория, операции над файлом), fingerprint, события
  `READINESS/*`, операции `readiness.evaluate` / `readiness.get`; интерфейс
  Advisor-ов, события `ENHANCEMENT/*` и `CREATIVE_REVIEW/*`, замена провайдера
  конфигурацией.
- Проверенные правила площадок: Adobe — 4–100 MP, ≤ 45 MB, JPEG sRGB, ≤ 49
  keywords, title желательно ≤ 70; Shutterstock — ≥ 4 MP, ≤ 50 MB, JPEG/TIFF,
  7–50 keywords, 1–2 категории, описание ≤ 2048. Текущие лимиты builder
  (7–49 keywords) совместимы с обеими.
- **Найдено:** все 8 файлов — Display P3 (ICC `sRGB EOTF with DCI-P3 Color
  Gamut`), не sRGB. Экспорт потребует конвертации P3 → sRGB (derivative);
  проверка профиля — по primaries, не по подстроке `sRGB`.

### Открытые вопросы (контракт §7)

Релизы, категории, включение Creative Review, предфильтр Enhancement.

### Статус

🟡 Контракт на согласовании; кода нет

---

# ЧАСТЬ VII. ПРАВИЛА РАБОТЫ БУДУЩЕГО АГЕНТА

# 36. Работа с фактическим проектом

Если есть прямой доступ к:

```text
F:\stock\stocker
```

сначала изучать актуальные файлы там.

Не просить пользователя вручную копировать содержимое файлов, если агент может открыть их сам.

---

# 37. Не начинать проект заново

Нельзя:

- игнорировать уже сделанные benchmark;
- повторно выбирать модель без причины;
- переписывать работающий LocalAnalyzer;
- пересоздавать архитектуру;
- делать новую БД вместо диагностики существующей;
- делать новый pipeline параллельно старому.

Сначала использовать существующую реализацию.

---

# 38. Стиль исправлений

Предпочтение:

```text
факт
 ↓
минимальное изменение
 ↓
тест
 ↓
доказательство
```

а не:

```text
предположение
 ↓
большой рефакторинг
 ↓
новые ошибки
```

---

# 39. Если информация неизвестна

Не придумывать.

Правильно:

> Не знаю; нужно проверить фактическое состояние файла/БД/окружения.

Неправильно:

> Наверное, проблема в X, поэтому сразу меняем Y.

---

# 40. Если обнаружено расхождение с этим паспортом

Приоритет:

```text
Фактический проект
    >
Паспорт
    >
История чата
```

Если фактический проект отличается:

1. не откатывать код к паспорту;
2. установить, какое состояние актуально;
3. обновить паспорт;
4. добавить запись в журнал, если расхождение существенно.

---

# 41. Текущая точка продолжения

На момент последнего обновления:

```text
АРХИТЕКТУРА
    🟢

INGEST
    🟢

QC
    🟢

LOCAL ANALYZER
    🟢

QWEN3-VL-8B
    🟢

AIAnalysis
    🟢

PRODUCTION AI INTEGRATION
    🟢

END-TO-END TEST
    🟢

SQLITE WRITE ACCESS
    🟢

PIPELINE RELIABILITY (AI/FAILED, --asset-id, hash, provenance, tests)
    🟢

STRICT JSON SCHEMA / TIFF / SOURCE_PATH (Windows + WSL)
    🟢

METADATA
    🟢 DONE — draft/edit/approve/reject, worker создаёт draft (§35K)

REVIEW GATE (auto_approved / human_review, gate-v1.1)
    🟢 DONE (§35L, §35M)

SERVICE LAYER + JSON CLI (app.service, python -m app.api)
    🟢 DONE (§35N)

MCP ДЛЯ OPENCLAW
    🟢 DONE — OpenClaw → MCP HTTP (NAT, адрес WSL, токен) → Stocker (§35P)

OPENCLAW INTEGRATION (skill, одна модель qwen3-vl, сводка review_queue)
    🟢 DONE (§35S, §35T)

СТАБИЛИЗАЦИЯ (права, RECOVERY.md, check_environment.ps1)
    🟢 DONE (§35U)

N8N (Docker Desktop, HTTP API, workflow:n8n, уведомления через n8n)
    🟢 DONE — ingest и digest по расписанию (§35W, §35X); retry готов, выключен

IMAGE ENHANCEMENT (QC → Topaz при необходимости → QC → Vision; производные файлы)
    ⚪ PLANNED (§2, §35V, §35X)

EXPORT / STOCK PLATFORMS (API)
    ⚪ PLANNED — только после Stock Readiness (§35X, §35Z)

BASELINE 2026-09-26 (docs/BASELINE_2026-09-26.md, git-метка)
    🟢 DONE (§35Y)

NOTIFICATION STATE IN STOCKER EVENTS (NOTIFY/SENT)
    🟢 DONE (§35Z)

STOCK READINESS (metadata + требования Adobe Stock / Shutterstock, без загрузки)
    🟡 CONTRACT — docs/STOCK_READINESS_CONTRACT.md на согласовании (§35ZA)

AI ADVISORS (Enhancement decision, Creative Review; необязательные)
    ⚪ PLANNED — после Stock Readiness; v1 — локальная модель (§3A.7, §35ZA)

ASSET 2 RECOVERY POLICY
    ⚪ PLANNED

EXPLICIT STATUS MODEL (assets.status + миграция)
    ⚪ PLANNED — после стабилизации

FASTAPI
    ⚪ PLANNED — после стабильного сервисного слоя

SIMILAR IMAGE GROUPS
    ⚪ PLANNED

DECISION ENGINE
    ⚪ PLANNED

TOPAZ / COMFYUI
    ⚪ PLANNED

OPENCLAW AUTONOMY
    ⚪ PLANNED
```

---

# 42. БЛИЖАЙШИЙ ОБЯЗАТЕЛЬНЫЙ ШАГ

Порядок, уточнённый 25 сентября 2026:

1. ~~Metadata pipeline~~ — 🟢 DONE (§35K). ~~Review gate~~ — 🟢 DONE (§35L).
   Контракт — `docs/METADATA_CONTRACT.md` (`metadata-v2`). Очередь человека —
   объекты в `human_review` (сейчас asset 5).
2. ~~Service layer + JSON CLI~~ — 🟢 DONE (§35N), `docs/SERVICE_CONTRACT.md`.
3. ~~MCP-адаптер для OpenClaw~~ — 🟢 DONE (§35O, §35P). Сервер:
   `python -m app.service.mcp_http` (`STOCKER_MCP_HOST=wsl`); журнал вызовов —
   `logs/mcp_http.log`.
4. ~~n8n~~ — 🟢 DONE (§35V–§35X), контракт `docs/N8N_CONTRACT.md`;
   уведомления — события `NOTIFY/SENT` (§35Z). `stocker-retry` выключен до
   накопления статистики.
5. **Stock Readiness — следующий этап** (§35Z): сначала контракт проверок
   metadata и требований Adobe Stock / Shutterstock (актуальные правила
   платформ проверить), без загрузки. Экспорт через API — только после него.
6. Явная модель статусов и миграция `assets.status`; FastAPI — когда нужен
   общий долгоживущий Windows-процесс (`SERVICE_CONTRACT.md` §7).

Не переписывать завершённый ingest/QC/AI pipeline и не менять Qwen3-VL-8B или
AI schema без отдельного доказанного требования.

---

# 43. КРИТЕРИЙ ГОТОВНОСТИ STOCKER НА ТЕКУЩЕМ ЭТАПЕ

Текущий milestone считается завершённым только после доказательства:

```text
NEW PHOTO
   ↓
INGEST
   ↓
ASSET
   ↓
QC PASSED
   ↓
QWEN3-VL-8B
   ↓
VALID AIAnalysis
   ↓
assets.ai_result
   ↓
AI/PASSED
```

и проверки фактического SQLite state.

Результат: подтверждён для asset ID `4` 23 сентября 2026.

Статус: 🟢 DONE

---

# 44. КОРОТКАЯ ТОЧКА ВОЗВРАТА

Если весь предыдущий контекст потерян, читать этот раздел.

> Stocker — Python + SQLite система обработки stock-фотографий.
>
> Архитектура разделяет:
>
> - YOU;
> - OpenClaw Agent;
> - Stocker Python Core/API;
> - n8n Workflow;
> - Browser Automation;
> - deterministic tools;
> - Vision;
> - Brain;
> - Cloud Expert;
> - SQLite.
>
> Текущий production pipeline:
>
> `ingest → QC → Qwen3-VL-8B → AIAnalysis → assets.ai_result → AI/PASSED`
>
> Qwen3-VL-8B уже выбран после project benchmark и отдельно проверен через LocalAnalyzer.
>
> Production integration уже реализована.
>
> Первый production pipeline подтверждён на asset ID `4`: `ingest → QC →
> Qwen3-VL-8B → AIAnalysis → assets.ai_result → AI/PASSED`. SQLite blocker
> устранён без удаления или пересоздания БД; SQLite state проверен напрямую.
>
> С 25.09.2026 pipeline восстанавливаемый: сбои пишутся как `AI/FAILED` и
> `SOURCE/INVALID` в `processing_events`, повтор — `python -m app.worker
> --asset-id N`, provenance (provider/model/prompt_version/время) хранится в
> событиях `AI/*`. Тесты: `python -m pytest`. Принципы — §3A, контракт
> событий — §14.
>
> С 25.09.2026 (§35G): strict JSON schema (`local-v2`), TIFF → JPEG для AI,
> `source_path` в POSIX-формате (Windows + WSL).
>
> С 25.09.2026 (§35K): metadata pipeline `Vision → Metadata AI → Python →
> assets.metadata_json`, review `draft → edit → approve/reject` через
> `python -m app.metadata`. Worker создаёт только draft. Контракт —
> `docs/METADATA_CONTRACT.md`.
>
> С 26.09.2026 (§35L): review gate `gate-v1` — низкий риск →
> `auto_approved` автоматически, спорные случаи → `human_review`. Approve и
> reject — только человек.
>
> С 26.09.2026 (§35N): service layer `app.service.dispatch` и JSON CLI
> `python -m app.api` — единая точка входа для агентов и workflow; approve и
> reject только для `human`.
>
> С 26.09.2026 (§35P): OpenClaw подключён к Stocker по MCP HTTP
> (`http://172.26.192.1:8765/mcp`, токен). Агент читает и выполняет
> pipeline-операции; approve/reject ему недоступны. Истина — журнал сервера и
> события Stocker, а не текст ответа агента.
>
> С 26.09.2026 (§35T): интеграция OpenClaw завершена (skill `stocker`, одна
> модель `qwen3-vl-8b-instruct` для всех ролей). Принцип §3A.5: AI и OpenClaw
> только инициируют действия; истина — Stocker Core и события SQLite.
>
> С 26.09.2026 (§35W–§35Z): n8n (Docker Desktop) по расписанию выполняет
> ingest и digest через HTTP API с actor `workflow:n8n`; факты доставки
> уведомлений — события `NOTIFY/SENT`. Baseline — `docs/BASELINE_2026-09-26.md`.
>
> **Следующая задача: Stock Readiness (§35Z) — сначала контракт.**
>
> После этого переходить к следующему milestone, не переписывая архитектуру без необходимости.

---

# КОНЕЦ ПАСПОРТА И БОРТОВОГО ЖУРНАЛА
