# STOCKER — КОНТРАКТ METADATA PIPELINE

> **Статус:** 🟢 СОГЛАСОВАН 25 сентября 2026 (с корректировками пользователя). Реализация — поэтапно.
>
> **Версия контракта:** `metadata-v1`, редакция 2: нормализация текста (§4.2) и
> различение концептов и конкретных утверждений (§4.7). Код до этой редакции не
> существовал, поэтому версия не увеличена.
>
> **`metadata-v2` (🟢 СОГЛАСОВАН 25.09.2026, с уточнениями по людям и правкам):** автоматический review gate —
> §6A. Расширяет v1: builder, валидация и grounding не меняются, добавляются
> состояния `auto_approved` и `human_review` и блок `review_gate`.
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

> Таблица выше — `metadata-v1`. В `metadata-v2` её дополняет §6A: после
> `build`, `rebuild` и `edit` автоматически выполняется review gate.

---

## 6A. Review gate (`metadata-v2`)

### 6A.1. Цель

Stocker автоматизирует промышленный stock-поток. Человек нужен только для
спорных случаев, а не для каждой фотографии:

```text
низкий риск:  Vision → Metadata AI → validation → gate → auto_approved ──► дальше автоматически
высокий риск: Vision → Metadata AI → validation → gate → human_review  ──► human approve / reject
```

**Разделение ответственности:**

| Слой | Отвечает на вопрос | Версия |
|---|---|---|
| validation (§4.5) | корректны ли metadata по форме и лимитам? | `builder_version` |
| **review gate** | можно ли пропустить без человека? | `policy_version = gate-v1` |
| человек | спорный случай: approve или reject | — |

Gate — отдельный детерминированный модуль (`app/review_gate.py`, чистые
функции). Builder, валидация и grounding v1 **не меняются**.

### 6A.2. Состояния

| `state` | Смысл | Кто устанавливает | Дальше автоматически (экспорт) |
|---|---|---|---|
| `draft` | gate ещё не выполнялся (записи v1) или отложен (partial) | builder | нет |
| `auto_approved` | прошёл правила риска | **только gate** | **да** |
| `human_review` | есть причина риска, нужен человек | gate или `escalate` | нет |
| `approved` | одобрено человеком | **только человек** | **да** |
| `rejected` | отклонено человеком | **только человек** | нет |

`auto_approved` **не заменяет** человеческое `approved`: это отдельное
состояние с записанной причиной (правила и версия политики). Человек может
в любой момент одобрить, отклонить или отредактировать `auto_approved`.

### 6A.3. Решение gate

Gate выполняется над текущими `fields`, `validation`, `grounding` и Vision
(`AIAnalysis`) и выдаёт одно из трёх решений:

| decision | Условие | `state` |
|---|---|---|
| `deferred` | `completeness = partial` | `draft` (worker повторит Metadata AI; очередь человека не засоряется при сбоях LM Studio) |
| `human_review` | есть хотя бы одна **причина** (таблица ниже) | `human_review` |
| `auto_approved` | причин нет и автоодобрение включено | `auto_approved` |

**Причины → `human_review`:**

| code | Условие |
|---|---|
| `VALIDATION_ERRORS` | есть `validation.errors`, кроме `UNCONFIRMED_CLAIM` |
| `UNCONFIRMED_CLAIM` | есть неподтверждённые конкретные утверждения (§4.7) |
| `TRADEMARK` | Vision `brands` или `logos` не пусты |
| `TEXT_BRAND_OR_LEGAL` | элемент `text_visible` классифицирован как `brand_or_legal` (§6A.4) |
| `LEGAL_CLAIM` | в `title`, `description` или keywords юридически значимое утверждение (§6A.5) |
| `PEOPLE_RECOGNIZABLE` | уровень риска людей `recognizable` (§6A.4a): видно лицо или человек — главный объект, возможен model release |
| `PEOPLE_UNCLEAR` | люди есть, но по Vision нельзя установить, что присутствие неидентифицируемое (§6A.4a) |
| `EDITORIAL_RISK` | Vision `editorial_risk` не пуст |
| `AI_GENERATED` | Vision `ai_generated = true` |
| `MANUAL_ESCALATION` | вызван `escalate` (действует до решения человека) |
| `AUTO_APPROVE_DISABLED` | причин нет, но `STOCKER_AUTO_APPROVE=0` (аварийный выключатель) |

**Не блокируют** (записываются в `review_gate.notes`): `TEXT_TECHNICAL`,
`TEXT_DESCRIPTIVE`, `PEOPLE_PARTIAL`, а также warnings валидации (`FEW_KEYWORDS`,
`TITLE_LONG`, ...).

### 6A.4. Классификация `text_visible`

Наличие текста само по себе **не** повод для review. Каждый элемент
`text_visible` Vision классифицируется детерминированно, правила применяются
**по порядку**, первое совпадение побеждает:

| # | category | Правило (любое из) | Следствие |
|---|---|---|---|
| 1 | `brand_or_legal` | содержит бренд или логотип из Vision; **организационно-правовая форма** отдельным словом: `Inc`, `Ltd`, `LLC`, `GmbH`, `AG`, `Corp`, `PLC`, `S.A.`, `SRL`, `BV`, `ООО`, `ОАО`, `ЗАО`, `ПАО`, `АО`, `НПО`, `ФГУП`, `ГУП`, `МУП`, `ИП`; символы `®`, `™`, `©`; слова `copyright`, `patent`, `trademark`, `all rights reserved`; URL, e-mail, телефон | `TEXT_BRAND_OR_LEGAL` → review |
| 2 | `technical` | токен с цифрой (`IP20`, `220V`, `0,6kg`, `06.2016`, `X3`, `KM6000-УХЛ4`); код из заглавных через дефис (`TN-S`); слово из словаря предупреждений: `WARNING`, `CAUTION`, `DANGER`, `HIGH VOLTAGE`, `NOTICE`, `EXIT`, `EMERGENCY`, `FIRE`, `STOP`, `NO ENTRY`, `KEEP OUT`, `ВНИМАНИЕ`, `ОСТОРОЖНО`, `ОПАСНО`, `ВЫСОКОЕ НАПРЯЖЕНИЕ`, `НЕ ВКЛЮЧАТЬ`, `ЗАПРЕЩЕНО`, `ВЫХОД`, `СТОП` | note `TEXT_TECHNICAL` |
| 3 | `descriptive` | всё остальное (`Electrical room`, `ЗАМОК ДВЕРИ ШАХТЫ`) | note `TEXT_DESCRIPTIVE` |

Словари — константы в коде, расширяемые. Их изменение увеличивает `policy_version`.

Проверка на реальных данных (Vision `local-v2`/`local-v1`):

| asset | text_visible | Классификация | Ожидаемое решение |
|---|---|---|---|
| 3, 4 | — | — | `auto_approved` |
| 5 | `АО "ШПЗ"`, `ЗАМОК ДВЕРИ ШАХТЫ`, `0411Е.06.05.090` | brand_or_legal (`АО`), descriptive, technical | `human_review` (`TEXT_BRAND_OR_LEGAL`) |
| 6 | `KM6000-УХЛ4`, `N° 0626023`, `IP20`, `ВИД ЗАЗЕМЛЕНИЯ TN-S`, `0,6kg`, `06.2016`, `X1…X7` | все technical | `auto_approved` |

Asset 5 показывает, зачем нужно правило 1: название компании `АО "ШПЗ"` Vision
**не** вынес в `brands`.

**Известное ограничение:** бренд без организационно-правовой формы, не
распознанный Vision и написанный рядом с цифрами (`SIEMENS 220V`), попадёт в
`technical`. Правило 1 проверяется раньше правила 2, поэтому бренд из Vision
или с юрформой такой ошибки не даст.

### 6A.4a. Уровень риска людей

Для industrial stock присутствие человека само по себе **не** повод для review.
`AIAnalysis` не меняется и сообщает только `people.present` и `count`, поэтому
уровень вычисляется детерминированно из текста Vision (`title`, `description`,
`subject`, `commercial_context`, `technical_subjects`, `keywords`):

| people_risk | Правило | Gate |
|---|---|---|
| `none` | `present = false` и `count = 0` | — |
| `recognizable` | есть признак узнаваемости (`face`, `faces`, `facial`, `portrait`, `headshot`, `smiling`, `looking at camera`, `eyes`) **или** в `subject` человек (`person`, `people`, `man`, `woman`, `worker`, `engineer`, `technician`, `operator`, `electrician`, `builder`, `welder`, `mechanic`) без признаков частичного присутствия | `PEOPLE_RECOGNIZABLE` → review |
| `partial` | есть признак неидентифицируемого присутствия (`hand`, `hands`, `glove`, `gloved`, `arm`, `arms`, `finger(s)`, `legs`, `feet`, `from behind`, `back view`, `rear view`, `silhouette(d)`, `faceless`, `face not visible`, `face obscured`, `face hidden`, `face covered`, `unrecognizable`, `anonymous`, `blurred figure`) и нет признаков узнаваемости | note `PEOPLE_PARTIAL` |
| `unclear` | люди есть, признаков нет | `PEOPLE_UNCLEAR` → review |

- Отрицания лица (`face not visible`, `face obscured`, …) удаляются из текста
  до поиска признаков узнаваемости.
- Технические словосочетания не считаются признаками человека: `hand tool(s)`,
  `hand rail`, `hand truck`, `hand wheel`, `robotic arm`, `robot arm`,
  `crane arm`, `mechanical arm`, `swing arm`.
- Совпадения по границам слов, словари — константы `gate-v1`.

**Ограничение:** качество зависит от того, как Vision описал людей. При
неопределённости решение всегда `human_review`. Если этого окажется мало,
следующий шаг — отдельная AI-проверка людей по изображению (новая роль, без
изменения `AIAnalysis`), результат которой gate учитывает так же.

### 6A.5. Юридически значимые утверждения в metadata

`LEGAL_CLAIM` — в `title`, `description` или keywords есть слово или
фраза: `certified`, `certification`, `compliant`, `compliance`, `approved`,
`patented`, `patent`, `trademark`, `licensed`, `official`, `guaranteed`,
`guarantee`, `warranty`, `ISO <число>`, `UL listed`, `CE marked`,
`meets … standard`. Общие концепты вроде `industrial safety` **не**
считаются утверждениями.

### 6A.6. Переходы v2

| Команда | Кто | Допустимо из | Результат |
|---|---|---|---|
| `build`, `rebuild`, `edit` | любой (как в v1) | как в v1 | как в v1 → `draft` → validation → **gate автоматически** → `auto_approved` / `human_review` / `draft` (deferred). Правка сама по себе состояние `auto_approved` не даёт — его даёт только gate |
| `gate` | любой | `draft`, `auto_approved`, `human_review` | повторная оценка (записи v1, новая версия политики). **`approved`/`rejected` gate никогда не меняет** |
| `escalate --reason` | любой, включая агента | `draft`, `auto_approved`, `human_review` | `human_review` + `MANUAL_ESCALATION`; действует до решения человека, повторный gate её не снимает |
| `approve` | **человек** | `draft`, `auto_approved`, `human_review` (правила v1: без errors, partial с `--allow-partial`, `--confirm-claims`) | `approved` |
| `reject --reason` | **человек** | `draft`, `auto_approved`, `human_review`, `approved` | `rejected` |

`auto_approved` не может установить никто, кроме gate. Правка агентом
проходит gate на общих основаниях. Если после правки причин риска нет, объект
получает `auto_approved`, а actor правки записан в событиях.

### 6A.7. Блок `review_gate` в `metadata_json` (`metadata_version = "2"`)

```json
"review_gate": {
  "policy_version": "gate-v1",
  "decision": "human_review",
  "reasons": [{"code": "TEXT_BRAND_OR_LEGAL", "detail": "АО \"ШПЗ\""}],
  "notes": [{"code": "TEXT_TECHNICAL", "detail": "0411Е.06.05.090"},
            {"code": "TEXT_DESCRIPTIVE", "detail": "ЗАМОК ДВЕРИ ШАХТЫ"}],
  "text_items": [
    {"text": "АО \"ШПЗ\"", "category": "brand_or_legal", "rule": "LEGAL_FORM"},
    {"text": "ЗАМОК ДВЕРИ ШАХТЫ", "category": "descriptive", "rule": "DEFAULT"},
    {"text": "0411Е.06.05.090", "category": "technical", "rule": "DIGITS"}
  ],
  "people_risk": {"level": "none", "markers": []},
  "escalation": null,
  "auto_approve_enabled": true,
  "evaluated_at": "2026-09-25T18:00:00+00:00"
}
```

Записи `metadata_version = "1"` (asset 3–6) остаются валидными как `draft`
без `review_gate`. Команда `gate` добавляет блок и переводит их в v2. Схема БД
не меняется.

### 6A.8. События v2 (дополняют §7)

| stage | status | message |
|---|---|---|
| `METADATA` | `GATED` | `policy_version`, `decision`, `reasons` (list[code]), `notes` (list[code]), `state_before`, `trigger` (`build` \| `rebuild` \| `edit` \| `gate`) |
| `METADATA` | `ESCALATED` | `reason`, `state_before` |

`APPROVED` и `REJECTED` не меняются. `state_before` теперь может быть
`auto_approved` или `human_review`.

### 6A.8a. CLI (дополняет §8)

```powershell
python -m app.metadata gate     N
python -m app.metadata escalate N --reason "..."
```

### 6A.9. Конфигурация

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `STOCKER_AUTO_APPROVE` | `1` | `0` — аварийный выключатель: всё без причин риска идёт в `human_review` с `AUTO_APPROVE_DISABLED` |

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
