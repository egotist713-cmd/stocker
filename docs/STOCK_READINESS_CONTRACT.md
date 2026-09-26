# STOCKER — КОНТРАКТ STOCK READINESS И AI ADVISORS

> **Статус:** 🟡 ПРОЕКТ на согласование (26 сентября 2026). Кода нет.
>
> **Версии:** правила готовности `readiness-v1`; профили площадок
> `adobe-2026-09`, `shutterstock-2026-09`.
>
> Связанные документы: паспорт §3A (принципы), §35Z (решение о названии этапа);
> `METADATA_CONTRACT.md` (metadata, review gate); `SERVICE_CONTRACT.md` (операции,
> права); `N8N_CONTRACT.md` (workflow).
>
> Любое изменение правил, статусов, событий или профилей площадок — это изменение
> контракта: обновить этот файл, увеличить версию и записать в журнал паспорта.

---

## 1. Назначение и границы

Цель этапа — **подготовить объект к автоматическому экспорту**, а не только
ответить «технически можно загрузить». После Stock Readiness для каждой площадки
известно: готов ли объект, что именно будет отправлено (поля, категория, файл) и
что мешает, если не готов. Экспорт затем только исполняет подготовленный план.

Оценка пригодности фотографии разделена на **два разных понятия**. Они не
смешиваются ни в коде, ни в статусах:

| | **Deterministic Stock Readiness** | **AI Advisors** (Enhancement, Creative Review) |
|---|---|---|
| Вопрос | Соответствует ли объект требованиям площадки? | Стоит ли вкладываться в объект? Что улучшить? |
| Примеры | мегапиксели, размер, формат, sRGB, metadata, keywords, бренды, релизы, категории | коммерческий потенциал, композиция, нужен ли Topaz, стоит ли расширенная проверка |
| Реализация | правила Python, константы с источником | модель через интерфейс провайдера |
| Детерминирован | **да**: тот же вход → тот же результат | нет |
| Обязателен | да, экспорт без него невозможен | **нет**; сбой или отсутствие не блокирует pipeline |
| Может запретить экспорт | да (нарушение правила площадки) | нет, только **поднять внимание** (§4.1) |
| Провайдер | нет | v1 — локальная `qwen3-vl-8b-instruct`; позже заменяемо (§4.4) |

Принципы §3A паспорта действуют полностью: AI только советует и инициирует;
истина — Stocker Core и события SQLite; approve/reject — только человек.

---

## 2. Место в pipeline

```text
ingest → QC
          │
          ▼
   Enhancement decision (AI Advisor, необязательно, §4.2)
          │   v1: только рекомендация; Topaz не запускается
          ▼
       Vision → Metadata AI → Python metadata → review gate
                                                    │
                                                    ▼
                               Stock Readiness (правила, §3)  ← обязательный этап
                                                    │
                                                    ▼
                   Creative Review Advisor (AI Advisor, необязательно, §4.3)
                                                    │
                                                    ▼
                                    Export (будущий этап, вне контракта)
```

- Stock Readiness выполняется для объектов с metadata в `auto_approved` или
  `approved` (решения gate и человека уже приняты). Для остальных статус —
  `not_evaluated` с причиной.
- Creative Review стоит **после** Readiness: модель тратит время только на
  объекты, которые технически можно продавать.
- Будущий Topaz (§2 паспорта): QC → Topaz → QC → Vision; оригинал не изменяется,
  Topaz создаёт derivative-файл. Enhancement decision только решает, нужен ли он.

---

## 3. Deterministic Stock Readiness

### 3.1. Входы (только чтение)

| Вход | Источник |
|---|---|
| файл | `source_path`; проверка hash (как `SOURCE/INVALID`) |
| QC | `assets.qc_result`: `passed`, `width`, `height`, `format`, `file_size` |
| цветовой профиль | читается из файла (ICC-профиль, Pillow) |
| metadata | `assets.metadata_json`: `state`, `completeness`, `fields`, `flags`, `validation`, `review_gate` |
| Vision | `assets.ai_result` (`AIAnalysis`): `categories`, `people`, `brands`, `logos`, `editorial_risk`, `ai_generated` |
| релизы | v1 — нет хранилища релизов (§7, вопрос 1) |

Stock Readiness **ничего не меняет** в metadata, файле, `assets.status`, решениях
человека. Результат пишется только событием (§3.8).

### 3.2. Профили площадок (проверены 26.09.2026)

Профиль — константа в коде с версией, ссылкой на источник и датой проверки.
Изменение правил площадки → новая версия профиля.

| Требование | Adobe Stock (`adobe-2026-09`) | Shutterstock (`shutterstock-2026-09`) |
|---|---|---|
| Разрешение | 4–100 MP | ≥ 4 MP |
| Размер файла | ≤ 45 MB | ≤ 50 MB (JPEG, загрузка через браузер) |
| Формат | JPEG | JPEG, TIFF (без слоёв) |
| Цвет | sRGB | sRGB (рекомендовано) |
| Текстовое поле | `title`: коротко, желательно ≤ 70 символов | `description`: фактическое описание, ≤ 2048 символов; законченное предложение (≥ 5 слов) |
| Keywords | ≤ 49, каждое один раз, первые 10 самые важные | 7–50 |
| Категории | категория Adobe (список площадки) | 1 обязательная, 2-я необязательная |
| Запрещено в metadata | торговые марки, бренды, личные данные, имена реальных людей, технические сведения о камере или файле | торговые марки в коммерческом контенте, спам, ошибки, эмодзи, личные данные |
| Релизы | model release для узнаваемых людей; property release для узнаваемых мест, объектов и произведений | model / property release; editorial-отметка |
| Editorial | не входит в v1 | подпись с местом и датой; не входит в v1 |

Источники:
- Adobe: [technical and legal requirements](https://helpx.adobe.com/stock/contributor/submit-your-content/submit-photos/technical-legal-requirements-photo-submission.html) (обновлено 11.06.2026), [titles and keywords](https://helpx.adobe.com/stock/contributor/content-policies-guidelines/metadata/tips-effective-titles-keywords.html) (18.08.2026).
- Shutterstock: [technical requirements](https://submit.shutterstock.com/help/en/articles/10617390-what-are-the-technical-requirements-for-images), [contextual metadata](https://submit.shutterstock.com/help/en/articles/10617427-content-publishing-standards-contextual-metadata), [submit photos](https://submit.shutterstock.com/help/en/articles/10594645-how-do-i-submit-photos-for-review).

**Проверить при реализации:** список категорий Adobe и Shutterstock; политику
Shutterstock в отношении AI-контента; правила обеих площадок для изображений,
улучшенных Topaz.

Совместимость с текущими правилами Stocker: builder держит 7–49 keywords
(подходит обеим площадкам), `title` и `description` ≤ 200, QC — ≥ 4000×3000 и
≤ 45 MB. Stock Readiness не дублирует эти проверки молча: нарушение профиля
площадки сообщается отдельным кодом.

### 3.3. Проверки и уровни

Каждая проверка даёт код с одним из уровней:

| Уровень | Смысл | Влияние на статус |
|---|---|---|
| `blocker` | нарушение правила площадки; нужен человек или новые данные | `blocked` |
| `derivative` | нарушение исправляется механически при экспорте (конвертация), оригинал не меняется | не блокирует; попадает в план файла (§3.5) |
| `warning` | рекомендация площадки не выполнена | `ready` с примечанием |

**Файл**

| code | Условие | Уровень |
|---|---|---|
| `SOURCE_NOT_OK` | `pipeline.source` ≠ `ok` (файл изменён или отсутствует) | blocker |
| `QC_NOT_PASSED` | QC не пройден | blocker |
| `RESOLUTION_TOO_LOW` | мегапикселей меньше минимума площадки | blocker |
| `RESOLUTION_TOO_HIGH` | больше максимума (Adobe 100 MP) | derivative (уменьшение) |
| `FILE_TOO_LARGE` | больше лимита площадки | derivative (JPEG с меньшим размером), blocker, если размер не достигается |
| `FORMAT_CONVERSION` | формат не принимается площадкой (TIFF → Adobe) | derivative (JPEG) |
| `COLOR_PROFILE_CONVERSION` | профиль не sRGB | derivative (sRGB) |
| `COLOR_PROFILE_MISSING` | ICC нет (считается sRGB) | warning |

**Проверено на данных (26.09.2026):** все 8 файлов (телефон) имеют ICC
**Display P3** с описанием `sRGB EOTF with DCI-P3 Color Gamut`. Это **не** sRGB:
у профиля sRGB-кривая, но охват P3. Поэтому sRGB определяется по
основным цветам (primaries) профиля или по точному списку известных
sRGB-профилей, **а не по подстроке `sRGB` в описании**. Для текущих файлов
ожидается `COLOR_PROFILE_CONVERSION` (derivative P3 → sRGB при экспорте) на обеих
площадках. Разрешение: 12.6–50.3 MP, JPEG, 4.9–14.8 MB — в пределах обоих
профилей.

**Metadata**

| code | Условие | Уровень |
|---|---|---|
| `METADATA_NOT_READY` | `state` ∉ {`auto_approved`, `approved`} | не оценивается (`not_evaluated`) |
| `METADATA_PARTIAL` | `completeness` = `partial` | blocker |
| `KEYWORDS_TOO_FEW` / `KEYWORDS_TOO_MANY` | вне диапазона площадки | blocker |
| `TEXT_TOO_LONG` | текстовое поле длиннее лимита площадки | blocker (молча не обрезается) |
| `TITLE_LONG` | Adobe `title` > 70 | warning |
| `DESCRIPTION_NOT_SENTENCE` | Shutterstock: меньше 5 слов | warning |
| `TRADEMARK_IN_METADATA` | бренд из Vision или словаря gate в полях | blocker |
| `CATEGORY_UNMAPPED` | категория площадки не определена детерминированно (§3.5) | blocker для Shutterstock, warning для Adobe (уточнить, §3.2) |

**Права и риски**

| code | Условие | Уровень |
|---|---|---|
| `MODEL_RELEASE_REQUIRED` | `people_risk` = `recognizable`, релиза нет | blocker |
| `PROPERTY_RELEASE_REQUIRED` | Vision `brands`/`logos` не пусты или `editorial_risk` указывает на узнаваемое место или объект, релиза нет | blocker |
| `EDITORIAL_ONLY` | `editorial_risk` не пуст (editorial не поддерживается в v1) | blocker |
| `AI_GENERATED` | Vision `ai_generated = true` | blocker (правила площадок для AI-контента — отдельное решение) |
| `PEOPLE_NOT_RECOGNIZABLE` | `people_risk` ∈ {`partial`, `unclear`} | warning (релиз не требуется для неузнаваемых людей) |

Human approve metadata **не** снимает blocker-ы прав: approve подтверждает
текст, релиз — это отдельный юридический факт.

### 3.4. Статусы (по каждой площадке)

| status | Смысл |
|---|---|
| `not_evaluated` | оценки нет или metadata не в `auto_approved`/`approved` |
| `ready` | blocker-ов нет; план экспорта готов (могут быть warnings и derivative) |
| `blocked` | есть blocker-ы; причины в `checks` |
| `stale` | после оценки изменился вход (fingerprint, §3.6) — требуется повторная оценка |

Общее поле `ready_for` — список площадок в статусе `ready`.
`pipeline.ready` из `SERVICE_CONTRACT.md` §4.1 **не меняется** (это готовность
metadata); готовность к площадкам — отдельный ключ `pipeline.stock_readiness`.

### 3.5. План экспорта (что готовит Readiness)

Для площадки в `ready` результат содержит **точные значения**, которые будет
отправлять экспорт:

| Площадка | Поле | Источник |
|---|---|---|
| Adobe | `title` | `fields.title` |
| Adobe | `keywords` | `fields.keywords` (порядок сохраняется: важные — первые) |
| Adobe | `category` | таблица соответствий (ниже) |
| Shutterstock | `description` | `fields.description`; если пусто — `fields.title` |
| Shutterstock | `keywords` | `fields.keywords` |
| Shutterstock | `categories` | 1–2 по таблице соответствий |
| обе | `file` | `source` (как есть) или `derivative` со списком операций: `to_jpeg`, `to_srgb`, `downscale_to_mp`, `jpeg_quality_for_size` |
| обе | `releases`, `editorial`, `ai_generated` | v1: `[]`, `false`, `false` (иначе объект был бы `blocked`) |

Категории — детерминированная таблица соответствий `Vision categories` +
keywords → категории площадки, константа профиля. Без совпадения —
`CATEGORY_UNMAPPED`, человек выбирает категорию (или позже — Metadata AI из
закрытого списка; это отдельное решение).

Derivative-файлы для экспорта создаются **на этапе Export**, а не Readiness:
Readiness только записывает, какие операции понадобятся.

### 3.6. Устаревание (fingerprint)

Результат хранит `fingerprint` входов: `file_hash`, hash `metadata_json.fields`
и `state`, `event_id` последнего Vision, версии профилей и правил. Если
fingerprint текущих данных отличается, представление показывает `stale`.
Экспорт (будущий) принимает только `ready` с совпадающим fingerprint.

### 3.7. Структура результата

```json
{
  "readiness_version": "readiness-v1",
  "evaluated_at": "2026-09-27T09:00:00+00:00",
  "fingerprint": "sha256:…",
  "platforms": {
    "adobe": {
      "profile": "adobe-2026-09",
      "status": "ready",
      "checks": [
        {"code": "COLOR_PROFILE_MISSING", "level": "warning", "message": "No ICC profile; assumed sRGB"}
      ],
      "export_plan": {
        "title": "Elevator shaft interior with steel guide rails",
        "keywords": ["elevator shaft", "guide rail", "…"],
        "category": "Industry",
        "file": {"source": "derivative", "operations": ["downscale_to_mp:100"]}
      }
    },
    "shutterstock": {
      "profile": "shutterstock-2026-09",
      "status": "blocked",
      "checks": [
        {"code": "CATEGORY_UNMAPPED", "level": "blocker", "message": "No Shutterstock category for: elevator, construction"}
      ],
      "export_plan": null
    }
  },
  "ready_for": ["adobe"]
}
```

### 3.8. События

| stage | status | message |
|---|---|---|
| `READINESS` | `EVALUATED` | JSON результата §3.7 + `actor` |
| `READINESS` | `FAILED` | `error_type`, `error` (например, файл не читается) |

Хранение — **только события**, без новой колонки и без миграции:
представление берёт последнее `READINESS/EVALUATED` и сравнивает fingerprint.
Повторная оценка с тем же fingerprint не пишет новое событие (исход
`UNCHANGED`).

### 3.9. Операции и права

| Операция | Уровень | human | agent | workflow | Описание |
|---|---|---|---|---|---|
| `readiness.evaluate` | pipeline | ✅ | ✅ | ✅ | оценить объект (`asset_id`, `platforms?`); детерминирована, решений не принимает |
| `readiness.get` | read | ✅ | ✅ | ✅ | последний результат + `stale` |
| `asset.list` | read | ✅ | ✅ | ✅ | новый фильтр `ready_for=<platform>` |

`review.queue.summary` дополняется счётчиками `ready_for` по площадкам и
причинами `blocked`. Worker запускает `readiness.evaluate` после gate, если
metadata в `auto_approved`/`approved`; `metadata.approve` — тоже (объект, который
одобрил человек, сразу получает оценку).

---

## 4. AI Advisors (место в архитектуре)

### 4.1. Общие правила

- **Советуют, а не решают.** Результат — рекомендация с причинами. Advisor не
  меняет metadata, файл, статус Readiness и решения человека.
- **Может только поднять внимание.** Как `metadata.escalate`: Advisor может
  отправить объект человеку (`attention`), но не может снять blocker Readiness,
  одобрить объект или снизить риск.
- **Необязательны.** Выключаются конфигурацией; сбой пишет `…/FAILED` и не
  останавливает pipeline (как partial draft у Metadata AI).
- **Провайдер заменяем.** Общий интерфейс `Advisor` (как `MetadataAnalyzer`):
  `provider`, `model`, `prompt_version`, strict JSON schema, raw output при
  ошибке, provenance в событии.
- **История — события.** Каждый вызов → событие с provenance; результат
  разных моделей сравним по событиям.

### 4.2. Enhancement Advisor

Решает, нужно ли улучшение (Topaz) **до** Vision. Topaz не считается
обязательным.

| Вход | QC-метрики (`sharpness`, `dark_ratio`, `bright_ratio`, разрешение, размер) + изображение |
|---|---|
| Выход | `decision` ∈ `enhancement_not_needed` \| `enhancement_recommended` \| `enhancement_risky`; `operations[]` (например `denoise`, `sharpen`, `upscale`) — только для `recommended`; `reasons[]`; `confidence` |
| События | `ENHANCEMENT/ADVISED`, `ENHANCEMENT/FAILED` |
| v1 | только записывается рекомендация; Topaz не запускается, Vision работает с оригиналом |
| Будущее | `enhancement_recommended` → Topaz → derivative → QC → Vision (по отдельному решению) |

`enhancement_risky` — улучшение может исказить изображение (артефакты,
«придуманные» детали, текст и мелкие надписи, лица). Такие объекты не
улучшаются автоматически.

Детерминированный предфильтр (без модели): если QC-метрики в норме и
разрешение с запасом выше минимума, advisor не вызывается —
`enhancement_not_needed`, `provider = rules`. Модель тратит время только на
спорные объекты.

### 4.3. Creative Review Advisor

Оценивает коммерческий потенциал объекта, который **уже прошёл** Readiness.

| Вход | изображение + `AIAnalysis` + `fields` metadata |
|---|---|
| Выход | `commercial_potential` ∈ `high` \| `medium` \| `low`; `composition` ∈ `good` \| `acceptable` \| `weak`; `recommendation` ∈ `proceed` \| `attention` \| `skip_suggested`; `reasons[]`; `confidence` |
| События | `CREATIVE_REVIEW/ADVISED`, `CREATIVE_REVIEW/FAILED` |
| Влияние v1 | **только информация**: видно в `asset.get`, сводке и уведомлениях; экспорт не блокирует |

`attention` и `skip_suggested` не отклоняют объект: они только добавляют его в
отдельный список человека (`review.queue`, раздел `advice`). Как их учитывать при
экспорте (например, «не экспортировать `skip_suggested` без человека»), решает
пользователь, когда накопится статистика; до этого — только информация.

### 4.4. Модель и замена провайдера

- v1: оба Advisor-а используют **ту же** локальную `qwen3-vl-8b-instruct`
  (принцип §3A.6 «одна локальная модель»). Облачная модель **не** является
  зависимостью.
- Провайдер выбирается конфигурацией для каждого Advisor-а отдельно
  (`STOCKER_ENHANCEMENT_PROVIDER`, `STOCKER_CREATIVE_PROVIDER`), без изменения
  кода pipeline.
- Переход на более сильную (в том числе облачную) модель — отдельное решение
  пользователя с записью в паспорт: оно меняет принцип §3A.6 для этой роли и
  означает передачу изображений наружу.
- Сравнение моделей — по событиям `…/ADVISED` на одних и тех же объектах.

---

## 5. Вне рамок

- Загрузка на площадки, FTP/API, учётные записи площадок — этап Export.
- Создание derivative-файлов, запуск Topaz.
- Editorial-контент, AI-generated контент.
- Хранилище релизов (кроме решения §7, вопрос 1).
- Изменение `AIAnalysis`, Vision-промпта, review gate.

---

## 6. Порядок реализации

1. **Профили и чистые функции** (`app/readiness.py`): профили площадок, проверки
   §3.3, план §3.5, fingerprint — unit-тесты на реальных данных assets 3–9.
2. **События и представление**: `READINESS/*`, `pipeline.stock_readiness`,
   `stale`.
3. **Service layer**: `readiness.evaluate`, `readiness.get`, фильтр `ready_for`,
   сводка; allowlist `workflow:n8n`; MCP.
4. **Worker**: оценка после gate и после approve.
5. **n8n**: digest показывает готовность по площадкам.
6. **Enhancement Advisor**: интерфейс, предфильтр, локальный провайдер, события
   (без Topaz).
7. **Creative Review Advisor**: интерфейс, локальный провайдер, события, раздел
   `advice` в очереди.

Шаги 6–7 — после проверки 1–5 на реальных данных. Этап Export — только после
этого контракта и по отдельному решению.

---

## 7. Решения, нужные от пользователя

1. **Релизы.** Stocker не хранит релизы. Предложение v1: `MODEL_RELEASE_REQUIRED`
   и `PROPERTY_RELEASE_REQUIRED` — blocker; позже операция человека
   `release.attach` (файл релиза + связь с asset). Для industrial stock
   узнаваемые люди редки, а `partial`/`unclear` блокером не являются.
2. **Категории.** Предложение: детерминированная таблица соответствий, при
   отсутствии совпадения — человек. Выбор категории моделью из закрытого
   списка — позже.
3. **Creative Review по умолчанию.** Предложение: выключен до реализации шагов
   1–5; после включения — только информация, без влияния на экспорт.
4. **Enhancement Advisor.** Предложение: предфильтр правилами + модель только для
   спорных объектов; v1 — только рекомендация.
