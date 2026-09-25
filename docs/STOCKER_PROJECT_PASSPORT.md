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

**Workflow engine.**

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

SERVICE LAYER + CLI JSON
    ⚪ PLANNED — после metadata

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

EXPORT / STOCK APIs
    ⚪ PLANNED

N8N AUTOMATION
    ⚪ PLANNED

OPENCLAW AUTONOMY
    ⚪ PLANNED
```

---

# 42. БЛИЖАЙШИЙ ОБЯЗАТЕЛЬНЫЙ ШАГ

Порядок, уточнённый 25 сентября 2026:

1. ~~Metadata pipeline~~ — 🟢 DONE (§35K). Контракт — `docs/METADATA_CONTRACT.md`.
   Drafts asset 3–6 ждут review человеком через `python -m app.metadata`.
2. **Сервисный слой + CLI с JSON-выводом.** Операции `app/metadata.py` уже
   возвращают `{asset_id, outcome, metadata}` — образец для остальных. Операции Core по `asset_id`
   (`process_asset` уже есть, плюс `get`/`list`/`history`/metadata)
   возвращают структурированный результат. CLI печатает JSON для n8n и
   OpenClaw. Без FastAPI.
3. Явная модель статусов и миграция `assets.status`.
4. FastAPI → n8n → OpenClaw tools.

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
> **Следующая задача: сервисный слой + CLI с JSON-выводом (§42), не
> переписывая завершённый pipeline.**
>
> После этого переходить к следующему milestone, не переписывая архитектуру без необходимости.

---

# КОНЕЦ ПАСПОРТА И БОРТОВОГО ЖУРНАЛА
