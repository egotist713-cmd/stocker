# STOCKER — КОНТРАКТ METADATA PIPELINE

> **Статус:** 🟢 СОГЛАСОВАН 25 сентября 2026 (с корректировками пользователя). Реализация — поэтапно.
>
> **Версия контракта:** `metadata-v1`, редакция 2: нормализация текста (§4.2) и
> различение концептов и конкретных утверждений (§4.7). Код до этой редакции не
> существовал, поэтому версия не увеличена.
>
> Связанные разделы паспорта: §3A (принципы), §14 (контракт событий), §35H–35I (журнал).
>
> Любое изменение структуры `metadata_json`, событий или правил валидации — это
> изменение контракта: обновить этот файл, увеличить версию и записать в журнал паспорта.

---

## 1. Назначение и принципы

Metadata pipeline превращает сохранённый Vision-анализ (`assets.ai_result`) в
редактируемые, проверенные и одобренные человеком stock metadata.

Три слоя с разными ролями:

| Слой | Отвечает на вопрос | Детерминирован | Где живёт |
|---|---|---|---|
| **Vision AI** | Что изображено? | нет | `AIAnalysis` → `assets.ai_result` (существует, **не меняется**) |
| **Metadata AI** | Как это подготовить для продажи? | нет | `MetadataSuggestion` |
| **Python metadata** | Что допустимо, что проверено, что утверждено? | **да** | `app/metadata.py` → `assets.metadata_json` |

Принятые ограничения:

- внутренний формат **не зависит от площадок**. Первые площадки — Adobe Stock
  и Shutterstock, их адаптеры и категории появятся на этапе экспорта;
- `AIAnalysis` и Vision-промпт не меняются ради metadata;
- `assets.status` не меняется: состояние review хранится в `metadata_json` и в событиях;
- UI нет, только CLI;
- все metadata на английском языке;
- **ничего не обрезается молча**: любое нарушение лимита попадает в `validation`;
- недоступность Metadata AI **не блокирует** pipeline: создаётся partial draft (§4.6).

```text
assets.ai_result (AIAnalysis, Vision)
        │
        ▼
Metadata AI ── v1: вход = JSON AIAnalysis; интерфейс допускает изображение
        │
        ├── успех ──► MetadataSuggestion ──┐
        │                                  ▼
        └── сбой ───► METADATA_AI/FAILED ─► Python: merge → normalize → flags → validate
                                           │
                                           ▼
                                assets.metadata_json
                          state = draft, completeness = full | partial
                                           │
                                           ▼
                            CLI: edit* → approve | reject
```

---

## 2. Поля, которые приходят от Vision (`AIAnalysis`, только чтение)

Metadata-слой читает `assets.ai_result` и никогда его не изменяет.

| Поле `AIAnalysis` | Как используется |
|---|---|
| `description`, `title`, `subject`, `commercial_context`, `technical_subjects` | контекст для Metadata AI; `title`/`description` — источник partial draft |
| `keywords` | контекст для Metadata AI; **обязательные кандидаты** в итоговые keywords (заземлены на изображение) |
| `people.present`, `people.count` | флаг: нужен model release |
| `brands`, `logos` | флаг (риск товарных знаков) и **фильтр** keywords / проверка текста |
| `text_visible` | флаг |
| `editorial_risk` | флаг |
| `ai_generated` | флаг |
| `confidence` | сохраняется в `sources.vision`, на логику не влияет (не откалиброван) |
| `categories` | не используется (категории — этап экспорта) |

---

## 3. Metadata AI

### 3.1. Роль и провайдер

Metadata AI — **отдельная роль и отдельный провайдер**, не Vision:

- свой интерфейс `MetadataAnalyzer` (не наследует `AIAnalyzer`);
- своя конфигурация в `.env`, независимая от `LMSTUDIO_*`:

| Переменная | По умолчанию |
|---|---|
| `METADATA_BASE_URL` | `http://192.168.1.104:1234/v1` |
| `METADATA_MODEL` | `qwen3-vl-8b-instruct` |
| `METADATA_API_KEY` | `lm-studio` |
| `METADATA_TIMEOUT` | `180` |

Значения по умолчанию совпадают с Vision **только из-за ограничений
окружения**: на 12 GB VRAM две 8–9B модели означают перезагрузки в LM Studio.
Архитектурно это разные роли. Модель для metadata (brain `qwen3.8-9b-distill`,
cloud) меняется через `.env` без изменения кода.

### 3.2. Входы

```python
class MetadataAnalyzer:
    def suggest(self, analysis: AIAnalysis, image_path: Path | None = None) -> MetadataSuggestion: ...
```

- **v1 передаёт только JSON `AIAnalysis`** (`image_path=None`): Metadata AI
  переформулирует и расширяет концептами то, что установил Vision, и не может
  добавить новые визуальные факты;
- интерфейс сохраняет `image_path`, чтобы будущие версии могли передавать
  изображение. Фактически использованные входы пишутся в provenance
  (`inputs`), поэтому результаты разных режимов различимы.

### 3.3. Выход — `MetadataSuggestion`

Новая Pydantic-модель в `app/ai/schema.py`. `AIAnalysis` не меняется.

```python
class MetadataSuggestion(BaseModel):
    suggestion_version: str = "1.0"
    title: str = ""            # короткий продающий заголовок
    description: str = ""      # 1–2 предложения, фактологично
    keywords: list[str] = []   # по убыванию релевантности, цель 30–49
```

Правила вызова:

- промпт требует: English, без брендов и логотипов, без выдуманных мест,
  людей и характеристик, keywords отсортированы по важности и включают все
  релевантные Vision-keywords;
- strict `json_schema`: все поля обязательны, лишние запрещены,
  `keywords.minItems = 25`, `keywords.maxItems = 49`. Ограничения задаются
  только в схеме запроса, сама модель их не содержит: сохранённый результат
  проверяет Python (§4.5), а не Pydantic;
- provenance: `provider`, `model`, `prompt_version` (`metadata-v1`),
  `inputs`, время, длительность;
- сырой ответ сохраняется в `metadata_json.generated` без изменений.

---

## 4. Python-слой (детерминированно)

### 4.1. Сборка `fields`

| Поле | full draft | partial draft |
|---|---|---|
| `title` | `MetadataSuggestion.title` | `AIAnalysis.title` |
| `description` | `MetadataSuggestion.description` | `AIAnalysis.description` |
| `keywords` | порядок Metadata AI, затем отсутствующие Vision-keywords | Vision-keywords |

Затем нормализация 4.2–4.3, флаги 4.4, валидация 4.5.

### 4.2. Нормализация текста (`title`, `description`)

- `app.textnorm.normalize_text`: Unicode NFKC; типографские кавычки, апострофы и
  тире заменяются на ASCII (`'`, `"`, `-`); невидимые символы (soft hyphen,
  zero-width, BOM) удаляются, управляющие заменяются пробелом; пробелы и
  переводы строк схлопываются, trim. Регистр и смысл не меняются;
- у `title` убирается завершающая точка;
- **без обрезки**: превышение лимита — ошибка валидации.

Та же `normalize_text` применяется к каждому keyword перед правилами §4.3, а
также к значениям, введённым человеком через `edit`.

### 4.3. Нормализация keywords

Нормализация меняет только форму. **Количество и длина не ограничиваются**:
это проверяет валидация.

1. разбить элементы, содержащие `,` или `;`;
2. `normalize_text`, lower case, снять окружающую пунктуацию;
3. удалить пустые → `EMPTY`;
4. удалить совпадающие с `brands`/`logos` из Vision или содержащие их
   (регистронезависимо) → `BRAND`;
5. удалить слова из стоп-листа (`photo`, `image`, `picture`, `stock`,
   `stock photo`; список в коде, расширяемый) → `STOPWORD`;
6. удалить дубли, сохранив первое вхождение (порядок = релевантность) → `DUPLICATE`.

Каждое удаление записывается в `validation.dropped_keywords` с причиной.

### 4.4. Флаги (`flags`)

| Флаг | Правило |
|---|---|
| `model_release_required` | `people.present` или `people.count > 0` |
| `trademark_risk` | `brands` или `logos` не пусты |
| `visible_text` | `text_visible` не пуст |
| `editorial_risk` | `editorial_risk` не пуст **или** `trademark_risk` |
| `ai_generated` | `ai_generated` |

Флаги — факты для человека и экспорта. Они не блокируют approve.

### 4.5. Валидация

Внутренние лимиты не зависят от площадок и выбраны так, чтобы подходить обеим
первым площадкам. Адаптеры экспорта заново проверяют точные правила каждой
площадки. Числа — рабочие значения, их нужно сверить с актуальными правилами
Adobe Stock и Shutterstock на этапе экспорта. При изменении увеличивается
`builder_version`.

**Errors** — блокируют approve:

| Код | Поле | Условие |
|---|---|---|
| `TITLE_EMPTY` | title | пусто после нормализации |
| `TITLE_TOO_LONG` | title | > 200 символов |
| `DESCRIPTION_EMPTY` | description | пусто |
| `DESCRIPTION_TOO_LONG` | description | > 200 символов |
| `TOO_FEW_KEYWORDS` | keywords | < 7 |
| `TOO_MANY_KEYWORDS` | keywords | > 49 (список **не обрезается**, человек убирает лишнее через `edit`) |
| `KEYWORD_TOO_LONG` | keywords | ключевое слово > 50 символов (одна запись на слово) |
| `BRAND_IN_TEXT` | title / description | бренд или логотип из Vision в тексте |
| `UNCONFIRMED_CLAIM` | title / description / keywords | конкретное утверждение без опоры на Vision и без подтверждения человеком (§4.7), одна запись на утверждение |

**Warnings** — не блокируют:

| Код | Условие |
|---|---|
| `PARTIAL_DRAFT` | `completeness = partial` (Metadata AI не отработал) |
| `FEW_KEYWORDS` | 7–24 keywords |
| `TITLE_LONG` | title > 70 символов |
| `MODEL_RELEASE_REQUIRED`, `TRADEMARK_RISK`, `VISIBLE_TEXT`, `EDITORIAL_RISK`, `AI_GENERATED` | соответствующий флаг |

Валидация пересчитывается после каждой сборки и каждого `edit`.

### 4.6. Partial draft

Если Metadata AI недоступен или вернул невалидный ответ:

1. событие `METADATA_AI/FAILED`, как у Vision (provenance, ошибка, `raw_output`);
2. строится draft из Vision (§4.1), `completeness = "partial"`,
   `generated = null`, `sources.metadata_ai = null`,
   `sources.metadata_ai_failure_event_id` = ID события сбоя;
3. предупреждение `PARTIAL_DRAFT`;
4. `approve` partial draft требует явного флага `--allow-partial`;
5. `build` без `--force` для partial draft **без правок человека** повторяет
   Metadata AI и при успехе заменяет его на full. Если правки есть, нужен `--force`.

Pipeline не блокируется: у asset всегда есть draft, а неполнота видна и
в `metadata_json`, и в событиях.

### 4.7. Опора на Vision: концепты и конкретные утверждения

Расширение концептами — цель Metadata AI, и оно **не запрещается**. Python
детерминированно разделяет содержимое `fields` на три вида:

| Вид | Что это | Следствие |
|---|---|---|
| `vision` | keyword, все слова которого есть в тексте Vision | ничего |
| `concept` | общее понятие, которого нет в Vision (`construction site`, `urban development`) | разрешено, видно в `grounding.concept_keywords` |
| **конкретное утверждение** | проверяемый факт, которого нет в Vision | ошибка `UNCONFIRMED_CLAIM` до подтверждения |

**Текст Vision** — нормализованные и приведённые к нижнему регистру `title`,
`description`, `subject`, `commercial_context`, `technical_subjects`,
`keywords`, `text_visible`, `brands`, `logos`, `editorial_risk`. Слова
сравниваются после упрощённого приведения к единственному числу (отбрасывается
конечное `s`).

**Конкретное утверждение** (правила детерминированы и расширяемы):

| reason | Правило | Примеры |
|---|---|---|
| `NUMBER` | слово или keyword с цифрой, которой нет в тексте Vision | `1998`, `5mm`, `model x200`, `3 floors` |
| `PROPER_NOUN` | в `description` или `title` слово с заглавной буквы (не в начале предложения) или аббревиатура, которых нет в тексте Vision; keyword, совпадающий с таким словом | `Berlin`, `Cyrillic`, `Siemens`, `USA` |

Заголовок в Title Case (не меньше 80% слов длиной от 4 букв, не считая первого, с заглавной) не
проверяется правилом `PROPER_NOUN`: там заглавные не несут информации. Имена
собственные из такого заголовка обычно есть и в `description`. Это известное
ограничение.

Бренды и логотипы, которые **назвал Vision**, обрабатываются отдельно:
удаление из keywords (§4.3) и `BRAND_IN_TEXT`.

**Подтверждение.** Человек либо убирает утверждение через `edit`, либо
подтверждает все текущие утверждения командой `approve --confirm-claims`.
Подтверждённые утверждения сохраняют `confirmed: true` при последующих правках,
пока текст утверждения не изменился.

---

## 5. Структура `assets.metadata_json` (зафиксирована)

### 5.1. Пример (full draft)

```json
{
  "metadata_version": "1",
  "state": "draft",
  "completeness": "full",

  "fields": {
    "title": "Elevator shaft interior with steel guide rails",
    "description": "Interior view of an elevator shaft with concrete walls, steel guide rails and cables.",
    "keywords": ["elevator shaft", "guide rail", "..."]
  },

  "generated": {
    "suggestion_version": "1.0",
    "title": "...",
    "description": "...",
    "keywords": ["..."]
  },

  "edited_fields": [],

  "flags": {
    "model_release_required": false,
    "trademark_risk": false,
    "visible_text": false,
    "editorial_risk": false,
    "ai_generated": false
  },

  "grounding": {
    "vision_keywords": ["elevator shaft", "concrete wall"],
    "concept_keywords": ["construction site", "urban development"],
    "specific_claims": [
      {"term": "Cyrillic", "field": "description", "reason": "PROPER_NOUN", "confirmed": false}
    ]
  },

  "validation": {
    "errors": [
      {"code": "UNCONFIRMED_CLAIM", "field": "description", "message": "Unconfirmed specific claim: 'Cyrillic' (PROPER_NOUN)"}
    ],
    "warnings": [
      {"code": "TITLE_LONG", "field": "title", "message": "Title is 74 characters (recommended <= 70)"}
    ],
    "dropped_keywords": [
      {"keyword": "stock photo", "reason": "STOPWORD"}
    ]
  },

  "review": {
    "decided_at": null,
    "reason": null,
    "allow_partial": false
  },

  "sources": {
    "vision": {
      "event_id": 17,
      "provider": "lmstudio",
      "model": "qwen3-vl-8b-instruct",
      "prompt_version": "local-v2",
      "confidence": 0.98
    },
    "metadata_ai": {
      "event_id": 18,
      "provider": "lmstudio",
      "model": "qwen3-vl-8b-instruct",
      "prompt_version": "metadata-v1",
      "inputs": ["vision_json"],
      "generated_at": "2026-09-25T15:00:00+00:00"
    },
    "metadata_ai_failure_event_id": null,
    "builder_version": "metadata-v1",
    "built_at": "2026-09-25T15:00:01+00:00"
  }
}
```

### 5.2. Поля верхнего уровня

| Ключ | Тип | Значения / смысл | Кто пишет |
|---|---|---|---|
| `metadata_version` | str | `"1"` — версия структуры | Python |
| `state` | str | `draft` \| `approved` \| `rejected` | Python (переходы §6) |
| `completeness` | str | `full` \| `partial` | Python |
| `fields` | obj | `title`, `description`, `keywords[]` — текущие значения для экспорта | Python, затем человек |
| `generated` | obj \| null | сырой `MetadataSuggestion`; `null` у partial | Metadata AI |
| `edited_fields` | list[str] | подмножество `title`/`description`/`keywords` | Python при `edit` |
| `flags` | obj | 5 bool-флагов §4.4 | Python |
| `grounding.vision_keywords` / `concept_keywords` | list[str] | классификация `fields.keywords` (§4.7), без конкретных утверждений | Python |
| `grounding.specific_claims` | list[obj] | `{term, field, reason, confirmed}`, reason ∈ `NUMBER`, `PROPER_NOUN` | Python; `confirmed` — человек |
| `validation.errors` / `warnings` | list[obj] | `{code, field, message}`; `field` может быть `null` | Python |
| `validation.dropped_keywords` | list[obj] | `{keyword, reason}`, reason ∈ `EMPTY`, `BRAND`, `STOPWORD`, `DUPLICATE` | Python |
| `review` | obj | `decided_at` (ISO UTC \| null), `reason` (str \| null), `allow_partial` (bool) | Python при approve/reject |
| `sources.vision` | obj | `event_id` события `AI/PASSED`, `provider`, `model`, `prompt_version`, `confidence` | Python |
| `sources.metadata_ai` | obj \| null | `event_id` события `METADATA_AI/PASSED`, `provider`, `model`, `prompt_version`, `inputs[]`, `generated_at` | Python |
| `sources.metadata_ai_failure_event_id` | int \| null | ID `METADATA_AI/FAILED`, если draft partial | Python |
| `sources.builder_version` | str | версия Python-правил, `metadata-v1` | Python |
| `sources.built_at` | str | ISO UTC последней сборки | Python |

Для исторических asset'ов без provenance в событии `AI/PASSED` (asset 4:
`AI analysis completed`) поля `sources.vision.provider/model/prompt_version`
равны `null`.

Хранение `generated` отдельно от `fields` позволяет видеть разницу между
машиной и человеком и перестраивать `fields` новыми правилами без AI-вызова.

---

## 6. Состояния review и переходы

Состояние хранится в `metadata_json.state`. **`assets.status` не меняется.**

| Команда | Допустимо из | Результат |
|---|---|---|
| `build` | нет metadata | Metadata AI → `draft` (full), при сбое — `draft` (partial) |
| `build` | `partial` без `edited_fields` | повтор Metadata AI; при успехе — full `draft` |
| `build --force` | любое | новый `generated` (или partial при сбое), `fields` пересобраны, `edited_fields` очищен, `draft` |
| `rebuild` | любое | только Python-правила поверх существующего `generated` (или Vision у partial); **правки человека сохраняются**; `approved`/`rejected` → `draft` |
| `edit` | любое | меняет `fields`, добавляет поле в `edited_fields`, пересчитывает validation; `approved`/`rejected` → `draft` |
| `approve` | `draft` без errors; partial — только с `--allow-partial`; с `--confirm-claims` сначала подтверждаются все текущие конкретные утверждения | `approved` |
| `reject --reason` | `draft`, `approved` | `rejected` |

Прочие переходы — ошибка CLI (код 1), без изменений и без событий.
Одобрение сбрасывается любым изменением содержимого.

---

## 7. События `processing_events` (зафиксированы)

Все `message` — JSON. Ключи ниже обязательны. `null` допустим там, где указано.

| stage | status | message |
|---|---|---|
| `METADATA_AI` | `PASSED` | `provider`, `model`, `prompt_version`, `inputs`, `generated_at`, `duration_s` |
| `METADATA_AI` | `FAILED` | `provider`, `model`, `prompt_version`, `inputs`, `failed_at`, `duration_s`, `error_type`, `error`, `raw_output` (только если был ответ, до 4000 символов) |
| `METADATA` | `DRAFTED` | `builder_version`, `completeness`, `trigger` (`build` \| `build_force` \| `rebuild`), `errors` (int), `warnings` (int), `keywords` (int) |
| `METADATA` | `EDITED` | `field`, `old`, `new`, `state_before` |
| `METADATA` | `APPROVED` | `builder_version`, `completeness`, `allow_partial`, `confirmed_claims` (list[str], подтверждённые этой командой) |
| `METADATA` | `REJECTED` | `reason`, `state_before` |

`metadata_json` хранит текущее состояние, события — полную историю,
включая каждое изменение (принцип §3A). Одна команда `edit` с несколькими
полями пишет по одному `METADATA/EDITED` на поле.

---

## 8. CLI

```powershell
python -m app.metadata build   N [--force]
python -m app.metadata rebuild N
python -m app.metadata show    N
python -m app.metadata edit    N --title "..."
python -m app.metadata edit    N --description "..."
python -m app.metadata edit    N --keywords "a, b, c"
python -m app.metadata edit    N --add-keyword "x" --remove-keyword "y"
python -m app.metadata approve N [--allow-partial] [--confirm-claims]
python -m app.metadata reject  N --reason "..."
```

Коды возврата: `1` — asset или `ai_result` не найден, недопустимый переход,
approve с errors или partial без `--allow-partial`. Partial draft после сбоя
Metadata AI — `0`: draft создан, сбой записан событием.

---

## 9. Связь с worker

1. Сначала `app.metadata` реализуется и проверяется как отдельная команда.
2. Затем `process_asset` после `AI/PASSED` вызывает `build`. Повторный
   `--asset-id N` для asset с AI, но без metadata, строит только metadata.

---

## 10. Файлы реализации

| Файл | Что |
|---|---|
| `app/ai/schema.py` | + `MetadataSuggestion` (`AIAnalysis` без изменений) |
| `app/ai/structured.py` | общий построитель strict `json_schema` (перенос `response_schema` из `local_analyzer`) |
| `app/ai/metadata_analyzer.py` | `MetadataAnalyzer` + LM Studio-провайдер (отдельная конфигурация `METADATA_*`) |
| `app/textnorm.py` | `normalize_text` (§4.2) |
| `app/metadata_builder.py` | чистый Python-слой без БД: сборка, нормализация, флаги, grounding, валидация, переходы |
| `app/metadata.py` | загрузка/сохранение `metadata_json`, события, вызов Metadata AI, CLI |
| `app/database/db.py` | + `save_metadata(asset_id, metadata_json)` |
| `tests/test_metadata*.py` | правила, переходы, события, partial (без LM Studio) |

Схема БД не меняется: `assets.metadata_json` уже существует.

Порядок реализации: `MetadataSuggestion` → `MetadataAnalyzer` → Python-слой → CLI →
production-проверка → интеграция в worker.

---

## 11. Критерий готовности

- pytest: нормализация, флаги, валидация, все переходы, события, partial, защита правок;
- production: для asset 3, 4 и 5 выполнены `build` → `edit` → `approve` через CLI,
  фактический `metadata_json` и события проверены в SQLite; проверен partial
  (Metadata AI на недоступном endpoint);
- сравнение keywords: Vision (8–9) против итоговых `fields.keywords`.
