# STOCKER — КОНТРАКТ STOCK READINESS И AI ADVISORS

> **Статус:** 🟢 СОГЛАСОВАН 26 сентября 2026 (с уточнениями пользователя:
> `commercial_score`, причины Enhancement, разделение брендов, цепочка
> original → derivative → platform export). Реализация — по §6.
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
| Текстовое поле | `title`: коротко, желательно ≤ 70 символов; максимум — лимит Stocker 200 (Adobe на странице максимум не указывает) | `description`: фактическое описание, ≤ 2048 символов; законченное предложение (≥ 5 слов) |
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
| `info` | факт для человека, не нарушение (например, маркировка производителя) | не влияет |

**Файл**

| code | Условие | Уровень |
|---|---|---|
| `SOURCE_NOT_OK` | файл отсутствует или его SHA256 не совпадает с `assets.file_hash` (проверяется при оценке) | blocker |
| `QC_NOT_PASSED` | QC не пройден | blocker |
| `RESOLUTION_TOO_LOW` | мегапикселей меньше минимума площадки | blocker |
| `RESOLUTION_TOO_HIGH` | больше максимума (Adobe 100 MP) | derivative (уменьшение) |
| `FILE_TOO_LARGE` | больше лимита площадки | derivative (JPEG с меньшим размером), blocker, если размер не достигается |
| `FORMAT_CONVERSION` | формат не принимается площадкой (TIFF → Adobe) | derivative (JPEG) |
| `COLOR_PROFILE_CONVERSION` | профиль не sRGB (сравнение xy основных цветов R, G, B профиля с встроенным sRGB Pillow, допуск 0.005) | derivative (sRGB) |
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
| `TEXT_EMPTY` | текстовое поле площадки пусто | blocker |
| `TRADEMARK_IN_METADATA` | бренд (§3.3a) упомянут в `title`, `description` или keywords | blocker |
| `CATEGORY_UNMAPPED` | категория площадки не определена детерминированно (§3.5a) | blocker для Shutterstock (категория обязательна); warning для Adobe (Adobe сам предлагает категорию при загрузке) |

**Права и риски**

| code | Условие | Уровень |
|---|---|---|
| `MODEL_RELEASE_REQUIRED` | `people_risk` = `recognizable`, релиза нет | blocker |
| `DOMINANT_BRAND` | бренд или логотип — главный объект кадра (§3.3a); нужен property release или другой кадр | blocker |
| `COMPONENT_BRAND` | бренд на оборудовании, не главный объект (Siemens на щите, §3.3a) | warning |
| `INCIDENTAL_MARKING` | маркировка, шильдик, юрлицо в надписи (§3.3a) | info |
| `EDITORIAL_ONLY` | `editorial_risk` не пуст (узнаваемое место, событие, объект; editorial не поддерживается в v1) | blocker |
| `AI_GENERATED` | Vision `ai_generated = true` | blocker (правила площадок для AI-контента — отдельное решение) |
| `PEOPLE_NOT_RECOGNIZABLE` | `people_risk` ∈ {`partial`, `unclear`} | warning (релиз не требуется для неузнаваемых людей) |

Human approve metadata **не** снимает blocker-ы прав: approve подтверждает
текст, релиз — это отдельный юридический факт.

### 3.3a. Бренды: три уровня

Промышленная фотография часто содержит маркировки производителей: они часть
объекта, и само их присутствие **не** причина блокировки.

Бренд-термины: Vision `brands`, Vision `logos` и надписи `text_visible`,
которые gate отнёс к `brand_or_legal` (`METADATA_CONTRACT.md` §6A.4). Уровень
(первое совпадение):

| prominence | Правило v1 | Код |
|---|---|---|
| `dominant` | термин встречается в Vision `subject` или `title` (Vision считает его главным в кадре), **или** в `subject` есть слово `logo`, `logotype`, `brand`, `branding`, `signage`, `trademark` | `DOMINANT_BRAND` → **blocker** |
| `component_brand` | бренд или логотип, распознанный Vision (`brands`/`logos`), но не главный — например, Siemens на промышленном оборудовании | `COMPONENT_BRAND` → **warning** |
| `incidental_marking` | только надпись `brand_or_legal` (шильдик, юрлицо, контакт) | `INCIDENTAL_MARKING` → **info** |

Независимо от уровня бренд **не должен** попадать в metadata:
`TRADEMARK_IN_METADATA` → blocker.

Проверка на данных: asset 5 (`АО "ШПЗ"` на замке, `subject` — «Mechanical door
locking system») → `incidental_marking`.

**v1 — без OCR и детекторов брендов.** Уровень выводится только из того, что
уже дал Vision. Три уровня — точка расширения: будущий детектор (OCR, brand
detection, анализ площади в кадре) может переклассифицировать термин, не меняя
кодов, уровней и статусов. Детекторы добавляются **только если реальные отказы
площадок покажут необходимость** (§8).

Review gate (`gate-v1.1`) по-прежнему отправляет любой бренд человеку
(`TRADEMARK`, `TEXT_BRAND_OR_LEGAL`); Readiness gate не меняет. Смягчение gate для
`component_brand` / `incidental_marking` — отдельное решение (возможный
`gate-v1.2`), когда накопится статистика.

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
| обе | `file` | `{"from": "original", "operations": []}` или `{"from": "original", "operations": [...]}` → derivative; операции: `to_jpeg`, `to_srgb`, `downscale_to_mp:<N>`, `fit_file_size:<bytes>` |
| обе | `releases`, `editorial`, `ai_generated` | v1: `[]`, `false`, `false` (иначе объект был бы `blocked`) |

### 3.5a. Категории

Детерминированная таблица «термины → категория площадки» (константа
`readiness-v1`). Термины ищутся по границам слов в Vision `subject`, `title`,
`technical_subjects`, `categories` (вес 3) и в keywords metadata (вес 1).
Категория с наибольшим весом — первая; при равенстве — порядок таблицы.
Shutterstock: вторая категория — следующая по весу, если её вес ≥ 2 **и** ≥ ¼
веса первой (иначе общие keywords вроде `concrete wall` делали бы
распределительную коробку «архитектурой»). На данных 26.09.2026: лифтовая шахта
(asset 3, 12 из 30) → `Industrial` + `Buildings/Landmarks`; блок питания лифта
(asset 8, 10 из 33) → `Industrial` + `Technology`; коробки (6, 7) → только
`Industrial`.

Vision почти не заполняет `categories` (из 7 объектов — только asset 9),
поэтому основной источник — `subject`, `title` и keywords.

| Группа терминов (примеры) | Adobe | Shutterstock |
|---|---|---|
| industrial, factory, machinery, electrical, wiring, cable, elevator, shaft, pipe, valve, mechanism, equipment, construction, power supply, control panel | `Industry` | `Industrial` |
| building, architecture, facade, interior, staircase, room, wall | `Buildings and architecture` | `Buildings/Landmarks` |
| computer, electronics, circuit, server, device, smartphone | `Technology` | `Technology` |
| car, truck, train, vehicle, road, highway | `Transport` | `Transportation` |
| playground, park, recreation, garden | `Hobbies and leisure` | `Parks/Outdoor` |
| tree, forest, plant, flower, nature | `Plants and flowers` | `Nature` |
| texture, background, pattern, surface | `Graphic resources` | `Backgrounds/Textures` |

- Adobe: 21 категория (проверено 26.09.2026,
  [choose the right category](https://helpx.adobe.com/stock/contributor/content-policies-guidelines/metadata/choose-right-category-content.html)).
- Shutterstock: полный официальный список без входа в портал получить не
  удалось; используются только названия, подтверждённые источниками.
  **Перед экспортом (этап Export) список сверяется с порталом / CSV-шаблоном
  площадки.**
- Без совпадения — `CATEGORY_UNMAPPED`, категорию выбирает человек. Выбор
  моделью из закрытого списка — позже, отдельным решением.

### 3.5b. Файлы: original → derivative → platform export

```text
original asset (data/incoming, только чтение, hash в assets.file_hash)
        │  операции из export_plan.file (to_srgb, to_jpeg, downscale, …)
        │  или улучшение (Topaz, будущее)
        ▼
derivative (data/derivatives/<asset_id>/<purpose>-<sha8>.jpg)
        │  событие DERIVATIVE/CREATED: path, sha256, source_sha256,
        │  purpose (export | enhancement), operations, platform?
        ▼
platform export (этап Export: поля + файл из export_plan)
```

- **Оригиналы никогда не изменяются.** Ни Readiness, ни Export, ни Topaz не
  пишут в `source_path`; hash оригинала остаётся проверкой целостности
  (`SOURCE/INVALID`).
- Derivative однозначно связан с оригиналом (`source_sha256`) и операциями;
  одинаковые операции над тем же оригиналом дают тот же derivative
  (повторно не создаётся).
- Readiness derivative-файлы **не создаёт**: только записывает операции в план.
  Создание — на этапе Export (для `purpose = export`) или Enhancement (Topaz).
- Enhancement-derivative (будущее) становится входом для QC и Vision; затем
  export-derivative строится уже от него, с цепочкой `source_sha256`.

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
        "file": {"from": "original", "operations": ["to_srgb"]}
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

Fingerprint строится **только из данных БД** — `assets.file_hash`, id последнего
`SOURCE/INVALID`, `QC passed`, `state`/`completeness`/`fields` metadata, id
Vision-события, версии правил и профилей. Поэтому `stale` в представлении
считается без чтения файла. Целостность файла (SHA256) проверяет сама оценка.

Исходы `readiness.evaluate`:

| outcome | ok | Событие | Когда |
|---|---|---|---|
| `EVALUATED` | ✅ | `READINESS/EVALUATED` | новая оценка (входы изменились или оценки не было) |
| `UNCHANGED` | ✅ | — | fingerprint совпадает с последней оценкой; возвращается сохранённый результат |
| `NOT_EVALUATED` | ✅ | — | metadata не `auto_approved`/`approved` или нет Vision; история не засоряется черновиками |
| `READINESS_FAILED` | ❌ | `READINESS/FAILED` | файл не читается |

### 3.9. Операции и права (реализовано 26.09.2026)

| Операция | Уровень | human | agent | workflow | Описание |
|---|---|---|---|---|---|
| `readiness.evaluate` | pipeline | ✅ | ✅ | ✅ | `{asset_id}` — оценка для всех площадок сразу; детерминирована, решений не принимает, metadata и файл не меняет. `data = {readiness}` (результат §3.7), не asset view |
| `readiness.get` | read | ✅ | ✅ | ✅ | `{asset_id}` → `{evaluated, stale, platforms, ready_for, event_id, result}` |

`asset.get` → `pipeline.stock_readiness` = `{evaluated, stale, platforms,
ready_for, event_id}` (без checks — они в `readiness.get`). `allowed_actions`
предлагает `readiness.evaluate`, если metadata готова, а оценки нет или она `stale`.

Следующие шаги (§6): фильтр `asset.list ready_for=<platform>`, счётчики в
`review.queue.summary`, вызов из worker после gate и после `metadata.approve`.

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
| Выход | `decision` ∈ `enhancement_not_needed` \| `enhancement_recommended` \| `enhancement_risky`; `reasons[]` — `{reason, detail}`, `reason` ∈ `noise` \| `sharpness` \| `artifacts` \| `resolution` \| `other`; `operations[]` (например `denoise`, `sharpen`, `upscale`) — только для `recommended`; `confidence` |
| Правило | для `recommended` и `risky` `reasons` **не пуст** (видно, почему Topaz рекомендован или опасен); `other` требует `detail` |
| События | `ENHANCEMENT/ADVISED`, `ENHANCEMENT/FAILED` |
| v1 | только записывается рекомендация; Topaz не запускается, Vision работает с оригиналом |
| Будущее | `enhancement_recommended` → Topaz → derivative → QC → Vision (по отдельному решению) |

`enhancement_risky` — улучшение может исказить изображение (артефакты,
«придуманные» детали, текст и мелкие надписи, лица). Такие объекты не
улучшаются автоматически.

Детерминированный предфильтр (без модели): если QC-метрики в норме и
разрешение с запасом выше минимума, advisor не вызывается —
`enhancement_not_needed`, `provider = rules`. Модель тратит время только на
спорные объекты. Предфильтр тоже пишет `reasons` для пограничных метрик
(например, `resolution`, если мегапикселей меньше запаса).

### 4.3. Creative Review Advisor

Оценивает коммерческий потенциал объекта, который **уже прошёл** Readiness.

| Вход | изображение + `AIAnalysis` + `fields` metadata |
|---|---|
| Выход модели — **первичные признаки** | `composition` ∈ `good` \| `acceptable` \| `weak` (+ `composition_notes`); `uniqueness` ∈ `high` \| `medium` \| `low` (насколько кадр отличается от типовых на стоках); `commercial_use_cases[]` — конкретные сценарии использования (например, «статья о техобслуживании лифтов», «презентация электромонтажной компании»); `demand` ∈ `high` \| `medium` \| `low` (спрос на тему); `quality_notes[]` — замечания о качестве (шум, резкость, свет); `recommendation` ∈ `proceed` \| `attention` \| `skip_suggested`; `confidence` |
| Производные — Python | `commercial_score` (0–100) рассчитывается **отдельно** из первичных признаков формулой с версией (`creative-score-v1`); `commercial_potential`: `high` ≥ 70, `medium` 40–69, `low` < 40. Модель score не выставляет |
| Хранение | событие хранит **и признаки, и** производные с версией формулы: при изменении формулы score пересчитывается по сохранённым признакам без нового вызова модели |
| События | `CREATIVE_REVIEW/ADVISED`, `CREATIVE_REVIEW/FAILED` |
| Влияние | **только рекомендация**: видно в `asset.get`, сводке и уведомлениях; признаки, `commercial_score` и `commercial_potential` **никогда не блокируют экспорт** |

Начальная формула `creative-score-v1` (калибруется по статистике продаж и
отказов, изменение — новая версия):

| Признак | Баллы |
|---|---|
| `composition` | good 35, acceptable 20, weak 5 |
| `demand` | high 30, medium 18, low 5 |
| `uniqueness` | high 25, medium 15, low 5 |
| `commercial_use_cases` | 2 балла за сценарий, не более 10 |

`commercial_score` позволяет сортировать очередь (что обрабатывать и
экспортировать первым) и сравнивать модели по одним и тем же объектам;
первичные признаки объясняют, **почему** score такой.

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

1. ✅ **Профили и чистые функции** (`app/readiness.py`): профили площадок, проверки
   §3.3, план §3.5, fingerprint — unit-тесты; прогон на реальных assets 2–9
   (26.09.2026, паспорт §35ZB).
2. ✅ **События и представление**: `READINESS/*`, `pipeline.stock_readiness`,
   `stale` (§35ZC).
3. ✅ **Service layer**: `readiness.evaluate`, `readiness.get`; allowlist
   `workflow:n8n`; MCP (§35ZC). Осталось: фильтр `ready_for`, сводка.
4. **Worker**: оценка после gate и после approve.
5. **n8n**: digest показывает готовность по площадкам.
6. **Enhancement Advisor**: интерфейс, предфильтр, локальный провайдер, события
   (без Topaz).
7. **Creative Review Advisor**: интерфейс, локальный провайдер, события, раздел
   `advice` в очереди.

Шаги 6–7 — после проверки 1–5 на реальных данных. Этап Export — только после
этого контракта и по отдельному решению.

---

## 7. Принятые решения (26.09.2026)

1. **Релизы.** Stocker не хранит релизы: `MODEL_RELEASE_REQUIRED` и
   `DOMINANT_BRAND` — blocker; позже операция человека `release.attach`.
   `partial`/`unclear` люди и второстепенные бренды блокером не являются.
2. **Категории** — детерминированная таблица (§3.5a), без совпадения — человек.
3. **Creative Review** выключен до реализации шагов 1–5; после включения —
   только рекомендация (`commercial_score`), экспорт не блокирует.
4. **Enhancement Advisor** — предфильтр правилами + модель только для спорных
   объектов; v1 — только рекомендация с причинами.
5. **Бренды** — `dominant_brand/logo` → blocker, `component_brand` →
   warning, `incidental_marking` → info (§3.3a); без OCR и детекторов в v1.
6. **Файлы** — original → derivative → platform export; оригиналы не
   изменяются (§3.5b).
7. **Creative Review** хранит первичные признаки (`composition`,
   `uniqueness`, `commercial_use_cases`, `demand`, `quality_notes`);
   `commercial_score` рассчитывается отдельно (§4.3).

---

## 8. Известные ограничения v1

| Ограничение | Почему приемлемо | Когда пересматривать |
|---|---|---|
| **Бренды и мелкий текст.** Vision может не увидеть мелкую надпись или производителя (asset 8: `Вектор Технологий` gate отнёс к `descriptive`) — тогда ни gate, ни Readiness его не видят. OCR и детектора брендов нет | в industrial stock маркировки часто — часть объекта; редкие отказы площадок дешевле обработать через обратную связь и ручную очередь, чем строить систему без статистики | когда статистика реальных отказов площадок покажет, что причина — бренды или надписи |
| **Заметность бренда** — по тексту Vision (`subject`/`title`), а не по площади в кадре | Vision описывает главное в кадре; ошибка возможна в обе стороны | то же; детектор может переклассифицировать уровень без изменения кодов |
| **Категории Shutterstock** — только подтверждённые названия | обязательная категория блокирует, а не угадывается | перед этапом Export — сверка с порталом/CSV-шаблоном |
| **Релизы** не хранятся | узнаваемые люди в industrial stock редки | когда понадобится экспорт таких объектов (`release.attach`) |
| **Правила площадок** — снимок 26.09.2026 | профиль версионирован и ссылается на источник | при изменении правил площадки или отказе по неизвестному правилу |

## 9. Приоритеты развития (26.09.2026)

1. **Качество изображения и Enhancement decision.**
2. **Metadata.**
3. **Stock Readiness.**
4. **Экспорт.**

Следствие для §6: после шагов 2–3 (оценка и операции) следующим идёт
Enhancement decision (шаг 6) — он влияет на то, какой файл вообще стоит
готовить. Небольшие шаги 4–5 Readiness (worker, digest) выполняются, когда они
не отнимают время у приоритета 1.
