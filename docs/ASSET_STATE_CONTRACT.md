# STOCKER — UNIFIED ASSET STATE И STALENESS (КОНТРАКТ)

> **Статус:** 🟢 ПРИНЯТ (28.09.2026, паспорт §35ZU, уточнения §35ZV); регрессии —
> `tests/test_asset_state.py` (25). Реализация
> чтения: `app/asset_state.py` (read-model, **ничего не пишет**); подключение к
> `asset.get` / `review.queue` / `allowed_actions` / n8n — следующим шагом (§8).
>
> Основание: аудит `docs/AUDIT_2026-09-28_PIPELINE.md`; решения пользователя
> 28.09.2026 (§9).

---

## 1. Принципы

1. **Результаты стадий остаются отдельными.** source / normalize / view / qc /
   enhancement / vision / metadata / readiness / creative_review / publication —
   каждая стадия хранит свой результат и свои события, как сейчас.
2. **Unified Asset State — производное.** Вычисляется из результатов стадий и их
   актуальности; нигде не хранится как источник истины и не пишется в БД
   (`assets.status` — наследие, см. §7).
3. **Ничего не считается актуальным молча.** У каждого результата, зависящего от
   предыдущих стадий, есть отпечаток входов. `stored != current → STALE`.
   Результат без отпечатка (наследие) — **STALE**, а не «наверное актуален».
4. **Три разных «готово» — три разных слова** (§3.3). Слово `ready` без
   уточнения не используется.
5. **Source integrity важнее любых решений ниже по цепочке:** без исходного
   файла объект не может быть готов, каким бы ни был `metadata.state`.

---

## 2. Граф зависимостей (что от чего зависит)

```
SOURCE (файл, assets.file_hash)
  └─► NORMALIZE: facts            fp = facts_version + file_hash
  └─► NORMALIZE: representation   fp = normalizer_version + params_hash + file_hash
        └─► ANALYSIS VIEW         fp = representation fp + view_version
              ├─► QC              fp = view fp                              (обязательная)
              ├─► ENHANCEMENT     fp = rules_version + file_hash + view fp  (советующая)
              ├─► VISION          fp = view fp + provider + prompt_version  (обязательная)
              │     └─► METADATA  вход = AI/PASSED event_id + builder_version + gate policy
              │           └─► READINESS  fp = readiness_version + профили площадок + file_hash
              │                          + facts event + QC passed + metadata (state, fields)
              │                          + vision event
              │                 └─► PUBLICATION GATE (будущий)  §2.2
              └─► CREATIVE REVIEW fp = file_hash + view fp + Vision result + шаблон/профиль (советующая)
```

### 2.1. Что каждая стадия реально использует

| Стадия | Данные на входе | Отпечаток в событии | Сейчас |
|---|---|---|---|
| normalize facts | байты source | `NORMALIZE/EVALUATED.fingerprint` | ✅ |
| normalize representation | source, facts, `PARAMS` | `NORMALIZE/PASSED.fingerprint` | ✅ |
| view | representation + код views | `view_fingerprint` (вычисляется) | ✅ |
| QC | view (размеры, пиксели), размер файла source | `QC.metrics.view.fingerprint` | ✅ для новых; наследие — нет |
| Enhancement | view full, `facts.jpeg_quality` | `ENHANCEMENT/ASSESSED.fingerprint` (включает view) | ✅ |
| Vision | view preview, промпт | `AI/PASSED.view.fingerprint` + `prompt_version` | ✅ для новых; **128 наследия — нет** |
| Metadata | **весь результат Vision** (`ai_result`), Metadata AI, правила | `sources.vision.event_id`, `builder_version`, `review_gate.policy_version` | ✅ (event_id) |
| Readiness | факты источника, QC, metadata, Vision (бренды, категории), правила площадок | `READINESS/EVALUATED.fingerprint` | ✅ |
| Creative Review | view **overview**; Vision `subject`, `title`, `description` (промпт), `keywords` (выбор профиля); шаблон + профиль | `CREATIVE_REVIEW/ADVISED.fingerprint` (file_hash + `ai_result` + prompt_version + view) | ✅ (с 28.09 включает view) |
| Publication gate | см. §2.2 | — | не реализован |

Creative Review **не** использует metadata и Readiness: его место «после
Readiness» — порядок (не тратить модель на заблокированные объекты), а не
зависимость по данным. Отпечаток берёт весь `ai_result` — шире фактически
используемых полей, т.е. консервативно (лишний пересчёт, но не пропущенный).

### 2.1a. Состав отпечатков QC и Vision (этап B — реализован 28.09.2026, паспорт §35ZX)

Только входы, от которых результат **реально** зависит; ничего «для наличия поля».

**QC** (`qc_fingerprint`):

| Вход | Почему |
|---|---|
| `view_fingerprint` (= SHA256 source + normalizer version + `PARAMS` + view version) | размеры, резкость, доли тёмного / светлого считаются по view full |
| `QC_RULES_VERSION` | версия формул и порогов: `MIN_MEGAPIXELS`, `RECOMMENDED_MEGAPIXELS`, `MIN/MAX_FILE_SIZE`, пороги тёмного / светлого (0.20, ≤5 / ≥250), формулы резкости и долей (превью 1600 px) |
| пороги (значения) | входят через `QC_RULES_VERSION`: изменение порога без смены версии — ошибка (тест на соответствие) |

Размер файла source — часть идентичности source (SHA256), отдельно не входит.
Модели и промптов у QC нет.

**Vision** (`vision_fingerprint`):

| Вход | Почему |
|---|---|
| `view_fingerprint` | идентичность source / normalization / view |
| вариант view и кодирование: `preview` (2048), JPEG q=90 | ровно эти байты получает модель |
| `provider`, `model` (идентификатор, как в запросе) | другая модель — другой ответ; содержимое весов по идентификатору не проверяется (ограничение) |
| `prompt_version` и `prompt_sha256` | версия промпта и SHA256 самого текста (`local_analyzer.PROMPT`): правка текста без смены версии тоже видна |
| версия схемы ответа: SHA256 строгой JSON-схемы `AIAnalysis` | другая схема — другой результат |
| параметры запроса, влияющие на ответ (`temperature` и т.п.; сейчас — умолчание сервера, фиксируется как `null`) | явная фиксация, чтобы смена была видна |

Отпечаток пишется как `input_fingerprint` вместе с `inputs` (составом): QC — в
`qc_result` (и событие QC), Vision — в `AI/PASSED` и `AI/FAILED`. Read-model
сравнивает с текущим отпечатком (QC — `qc.fingerprint(view_fp)`; Vision —
`input_fingerprint(view_fp, VISION_IDENTITIES[provider]())`, где текущая
идентичность LM Studio берёт модель из окружения) и при расхождении указывает
`changed` — какие входы изменились. Числовые пороги QC входят в `qc.rules()`
значениями; тест проверяет, что каждый числовой порог модуля QC в них есть. Старые
результаты без отпечатка остаются `STALE`; история задним числом не
переписывается.

### 2.1b. Creative Review: данные, применимость, устаревание (28.09.2026, паспорт §35ZZD)

**Принцип:** metadata current + approved → Readiness current (готов хотя бы для
одной площадки) → Creative Review. Creative — **советник**: не меняет metadata,
одобрение, Readiness и решение о выпуске; запускается **по запросу**
(`creative.review` или `asset.reprocess reprocess_from=creative_review`), в
цепочки по умолчанию и в worker **не** входит (решение 26.09: советник, а не
стадия pipeline).

| Что | Правило |
|---|---|
| **Отпечаток (данные)** | SHA256 source · отпечаток AnalysisView (overview — что видит модель) · результат Vision `ai_result` целиком (используются subject / title / description, keywords — для выбора профиля; весь — консервативно) · шаблон + профиль (`prompt_version_for(profile)`). **Не входят:** metadata, Readiness — это не данные оценки |
| **Применимость** | metadata одобрена и актуальна; Readiness актуален и `ready_for` не пуст. Иначе `not_applicable` (`METADATA_NOT_APPROVED`, `READINESS_NOT_EVALUATED`, `READINESS_BLOCKED`); сохранённая оценка — история (`has_result`) |
| **Изменение metadata** | Readiness → `stale` → Creative `stale` (`UPSTREAM_STALE:readiness`), запись запрещена. После пересчёта Readiness: готов → Creative снова `current` **без вызова модели** (её данные не менялись); не готов / metadata не одобрена → `not_applicable` |
| **Изменение Readiness** | то же: устаревание по наследованию, применимость — по новому результату |
| **human_review** | `not_applicable` (`METADATA_NOT_APPROVED`); `creative.review` → `CREATIVE_NOT_APPLICABLE`, без события |
| **Readiness blocked** | `not_applicable` (`READINESS_BLOCKED`) — модель не тратится на то, что нельзя продать |
| **Изменение Vision / view / шаблона** | собственный отпечаток не совпал → `FINGERPRINT_CHANGED` / `PROMPT_CHANGED` → нужна новая оценка (по запросу) |
| **Запрет записи при stale upstream** | `creative.review` и reprocess проверяют normalize / view / qc / vision / metadata / readiness **перед записью**; неактуально → `UPSTREAM_NOT_CURRENT` без события; устарело между планом и записью → `STOPPED` |
| **dry-run / no-op** | план `creative_review run / skip`; повтор при тех же входах — `UNCHANGED` / `NOTHING_TO_DO`, ноль событий |
| **Профиль при пересчёте** | как у прежней оценки: `auto` → `auto`, явный → тот же, иначе по умолчанию |
| **`creative.get`** | `result` — только актуальная оценка; иначе `result: null`, `last_result` с `current: false`, `status` / `status_reason` (как `readiness.get`); `pipeline.creative_review.current` |

### 2.2. Publication / Export Gate

Отдельное понятие, **не** второй metadata gate. Отвечает на вопрос «можно ли
выпускать объект в конкретный export profile». Входы: актуальный Readiness для
профиля (`ready`), актуальная metadata в состоянии одобрения, актуальный
Creative Review (если политика gate его требует — решение при реализации),
решения человека. Отпечаток = отпечатки этих входов + версия политики gate +
профиль. Результат — на профиль площадки.

### 2.2a. publication-v1 (реализовано 28.09.2026, `app/publication.py`, паспорт §35ZZG)

Решения при реализации — **из существующих контрактов, без новых критериев**:

| Вопрос | Решение и основание |
|---|---|
| Результат | **по площадкам**: `platforms.{adobe, shutterstock}.status` = `approved` / `blocked` (+ `reasons` = blocker-ы Readiness площадки, `notes`), `approved_for` — список; общего бинарного флага нет |
| Когда площадка `approved` | metadata одобрена (`auto_approved` / `approved`) и актуальна; Readiness актуален и **эта** площадка `ready`; source подтверждён **SHA256 непосредственно перед решением** (§3.5a) |
| Creative Review | **не обязателен и не блокирует** (STOCK_READINESS §4.1: советники необязательны и «не блокируют экспорт»; §4.3: `attention` / `skip_suggested` не отклоняют, «до этого — только информация»). Актуальная рекомендация записывается как информация: `notes` `ADVISOR_ATTENTION` / `ADVISOR_SKIP_SUGGESTED`; нет актуальной оценки — `NO_CURRENT_ADVICE` |
| Решения человека | `rejected` → отказ; одобрение metadata человеком — как `auto_approved`; отдельного решения человека о выпуске в существующих контрактах нет — не вводится |
| Отпечаток | версия политики `publication-v1` + версии профилей площадок + SHA256 source + **отпечаток актуального Readiness** (он уже включает metadata, Vision, факты, QC) + **снимок совета** Creative (`current`, `recommendation`) — не отпечаток Creative: новая оценка с тем же советом Publication не обесценивает |
| Применимость | metadata не одобрена / Readiness не оценён / Readiness blocked → `not_applicable` (прежнее решение — история) |
| Устаревание | наследуется от Readiness (`UPSTREAM_STALE:readiness`); собственный отпечаток → `FINGERPRINT_CHANGED` с `changed` (`readiness_fingerprint`, `profiles`, `advice`, `source_sha256`, `policy_version`) |
| Каскады | metadata → Readiness → Creative → Publication `stale`; смена политики gate с тем же решением → после повторного gate Readiness и Publication снова `current` **без** новых оценок |
| Отказы (без событий) | `PUBLICATION_NOT_APPLICABLE`, `UPSTREAM_NOT_CURRENT`, `SOURCE_INVALID` (SHA256), `REJECTED` |
| Запуск | по запросу: `publication.evaluate` или `asset.reprocess reprocess_from=publication`; не в цепочках по умолчанию, не в worker, **не для n8n** |
| Итоговое состояние | актуальное решение с непустым `approved_for` → `publication_approved` (`approved_for` в `state`); иначе `platform_ready`; устаревшее → `stale` (`reprocess_from = publication`) |
| `publication.get` | `result` — только актуальное решение; иначе `result: null`, `last_result` с `current: false` |
| Upstream | Publication **ничего не меняет** в metadata, Readiness и Creative Review; событие `PUBLICATION/EVALUATED` — и есть результат |

`publication_approved` = **одобрено Publication Gate для публикации на платформе** (по площадкам, `approved_for`), а **не** «уже опубликовано»: фактическая загрузка на Adobe Stock / Shutterstock — будущий отдельный этап (сначала Export preparation → `ready_for_export`).

`publication_approved` ≠ `ready_for_export`: файл площадки создаёт будущий
Export preparation — только из актуального `approved` для профиля.

### 2.3. Распространение устаревания

- **Собственное устаревание:** `stored_fp != current_fp`, либо отпечатка нет
  (наследие) → `stale` (`FINGERPRINT_CHANGED` / `NO_FINGERPRINT`).
- **Унаследованное:** если обязательная стадия выше по графу `stale`, `failed`
  или `missing`, результат ниже по графу тоже не актуален →
  `stale` (`UPSTREAM_STALE:<стадия>`), даже если его собственный отпечаток
  совпадает.
- Metadata: другой `sources.vision.event_id`, чем последнее `AI/PASSED`, →
  `VISION_CHANGED`; другой `builder_version` → `BUILDER_CHANGED` (пересборка
  правилами, без AI); другой `policy_version` gate → `GATE_POLICY_CHANGED`
  (только повторный gate, без AI) — оба для **не-человеческих** состояний
  (`draft`, `auto_approved`, `human_review`). `asset.reprocess` на стадии metadata
  выбирает самую дешёвую операцию, снимающую причину: gate / rebuild / build. Решение человека (`approved`) сменой правил не отменяется, но
  смена Vision делает его `stale` — нужно повторное подтверждение.
- Creative Review: отпечаток входов (view, Vision) не совпал → `FINGERPRINT_CHANGED`;
  шаблон / профиль не текущий (`prompt_version_for(profile)`) → `PROMPT_CHANGED`;
  профиль неизвестен → `UNKNOWN_PROFILE`.
- Vision: нет `view` в `AI/PASSED` → `NO_FINGERPRINT`; другой view →
  `FINGERPRINT_CHANGED`; провайдер не в реестре текущих промптов →
  `UNKNOWN_PROVIDER`; другая `prompt_version` → `PROMPT_CHANGED`.
- Readiness для объекта, чья metadata **не** в `auto_approved` / `approved`, —
  `not_applicable`; старое событие Readiness считается вытесненным (не
  «ready»).

---

## 3. Unified Asset State

### 3.1. Состояния (в порядке приоритета — первое подходящее)

| # | Состояние | Значение | Terminal? |
|---|---|---|---|
| 1 | `rejected` | человек отклонил metadata (`metadata.state = rejected`) | **да** (только новое решение человека вне Stocker-автоматики) |
| 2 | `source_invalid` | исходный файл отсутствует или изменён | нет — восстановить файл / зарегистрировать новый объект |
| 3 | `blocked` | детерминированный отказ **актуальной** обязательной стадии: `NORMALIZE_FAILED`, `VIEW_FAILED`, `QC_FAILED`, `READINESS_BLOCKED` (все профили заблокированы правилами) | нет, но автоматический повтор бессмыслен — нужен другой вход, правила или решение |
| 4 | `error` | техническая ошибка обязательной стадии, повтор имеет смысл: `VISION_FAILED`, `READINESS_FAILED` | нет — повтор |
| 5 | `stale` | обязательный результат устарел; `reprocess_from` — первая устаревшая стадия | нет — пересчёт |
| 6 | `processing` | обязательного результата ещё нет (или metadata `draft`) | нет — следующая стадия |
| 7 | `human_review` | metadata gate требует человека (`metadata.state = human_review`), всё выше актуально | нет — решение человека |
| 8 | `metadata_approved` | metadata одобрена (`auto_approved` / `approved`), Readiness ещё не оценён | нет — `readiness.evaluate` |
| 9 | `platform_ready` | актуальный Readiness: `ready` хотя бы для одного профиля (`ready_for`) | нет — дальше Publication gate |
| 10 | `publication_approved` | одобрено Publication Gate для публикации на платформе (`approved_for`); **не** «опубликовано» | нет — дальше Export preparation |
| 11 | `ready_for_export` | (будущее) Export preparation создал и проверил файл площадки | нет — «terminal до изменения входов» |

Состояние 10 — с 28.09.2026 (publication-v1, §2.2a); 11 недостижимо: Export не реализован.

**Как применяется приоритет.** После `rejected` и `source_invalid` обязательные
стадии проверяются **в порядке обработки** (normalize → view → qc → vision →
metadata → readiness); первая неактуальная определяет состояние: `failed` →
`blocked` / `error` (§3.4), `stale` → `stale` (`reprocess_from` = эта стадия),
`missing` → `processing`. Так устаревший или несостоявшийся результат выше по
цепочке всегда важнее отказа ниже (который от него зависит). Затем — состояние
metadata (`human_review`, `draft`) и Readiness.

### 3.2. Переходы

```
            ┌──────────── source_invalid ◄── (файл пропал / изменился: из любого, кроме rejected)
            │                  │ файл восстановлен и подтверждён
            ▼                  ▼
ingest → processing → (stale ↔ пересчёт) → human_review ──approve──► metadata_approved
            │   ▲                              │  │                        │ readiness.evaluate
            │   └── error (повтор) ◄───────────┘  └──reject──► rejected     ▼
            └──► blocked (NORMALIZE/VIEW/QC)          (terminal)        platform_ready ──► [publication_approved → ready_for_export]
                                                                           │ все профили blocked
                                                                           └──► blocked (READINESS_BLOCKED)
```

- Любое изменение upstream переводит объект в `stale` (с `reprocess_from`) из
  любого нетерминального состояния.
- `auto_approved` metadata ведёт прямо в `metadata_approved` (gate решил без
  человека).
- `reject` возможен из `human_review`, `metadata_approved`, `platform_ready`
  (как `mb.REJECTABLE_STATES`).

### 3.3. Три разных «готово»

| Понятие | Где | Смысл |
|---|---|---|
| **metadata approval** | `stages.metadata.state` ∈ {`auto_approved`, `approved`} | metadata корректна и безопасна; ничего не говорит о площадках и файле |
| **platform readiness** | `stages.readiness.ready_for` (актуальный) | правила конкретной площадки соблюдены (разрешение, формат, цвет, бренды, keywords…); файл площадки ещё не создан |
| **ready for export** | (будущее) `ready_for_export` | Publication gate разрешил и Export preparation создал и проверил пакет для профиля |

`metadata_approved ≠ ready_for_export`: первое — одна стадия из многих;
второе — итог всей цепочки для конкретного профиля. `pipeline.ready`
(= metadata approval) переименовывается (§7).

### 3.4. Что означают ключевые состояния

**Пять состояний не смешиваются** (уточнение 28.09.2026):

| Состояние | Смысл | Чем не является |
|---|---|---|
| `stale` | результат **существует**, но его входы не соответствуют текущему отпечатку (или отпечатка нет) | не отказ и не ошибка: результат просто нельзя считать актуальным |
| `blocked` | есть **актуальное** блокирующее решение (отказ стадии или правила площадок) | не устаревший отказ (тот — `stale`) и не сбой |
| `error` | технический сбой, который **можно повторить** | не решение правил |
| `source_invalid` | исходный файл отсутствует или изменён | не отказ стадии: стадии здесь ни при чём |
| `rejected` | человек **окончательно** отклонил объект | не блокировка правилами |

**`platform_ready`** означает только: «актуальный Readiness прошёл хотя бы для
одной целевой площадки» (`ready_for` не пуст). Это **не** «готов для всех
площадок», **не** «готов к экспорту вообще» и **не** `ready_for_export`.
Площадки, для которых объект готов, — только в `ready_for`.

- **`blocked`** — объект не продвинется без изменения входа, правил или решения
  человека; автоматический повтор ничего не изменит. Причина — код в
  `reasons` (`NORMALIZE_FAILED:<код>`, `VIEW_FAILED:<код>`, `QC_FAILED`,
  `READINESS_BLOCKED`). Блокирующий результат учитывается, **только если он
  актуален**; устаревший отказ даёт `stale` (перепроверить).
- **`human_review`** — нужен человек **для metadata** (privacy / content risk,
  claims, дети, документы…). Это не Publication gate.
- **`approved`** как отдельное состояние объекта **не используется**: есть
  `metadata.state = approved` (решение человека по metadata) и будущее
  `publication_approved` (решение о выпуске для профиля).

### 3.5. Source integrity

- `missing`: файла нет (проверка при чтении — существование).
- `changed`: размер файла ≠ `assets.file_size` (дёшево, при чтении), или
  последнее свидетельство о целостности отрицательное: `SOURCE/INVALID`,
  `NORMALIZE/FAILED` `SOURCE_CHANGED` / `SOURCE_MISSING` новее последнего
  положительного (`NORMALIZE/EVALUATED`, `NORMALIZE/PASSED`, `QC`), или —
  при `verify=True` — SHA256 ≠ `assets.file_hash`.
- `source_invalid` перекрывает **любой** `metadata.state`, кроме `rejected`:
  объект с `auto_approved` / `approved` metadata без файла не готов.
  `metadata.approve` для него не предлагается (регрессия #67, #68).

### 3.5a. Уровни проверки source и где какой нужен (уточнение 28.09.2026)

| Уровень | Что проверяет | Что доказывает |
|---|---|---|
| **fast** (`exists + size`) | файл есть, размер = `assets.file_size`, нет более нового отрицательного свидетельства | только **отсутствие признаков** изменения; идентичность **не** доказывает |
| **sha256** | SHA256 файла = `assets.file_hash` | идентичность байтов |

- **Все отпечатки** (facts, representation, view, стадии ниже) строятся от
  `assets.file_hash` — SHA256, посчитанного при ingest. Совпадение `exists + size`
  в отпечаток не входит и идентичности не подменяет.
- **SHA256 обязателен перед созданием результата**, который потом считается
  актуальным по отпечатку: `worker.verify_source` перед обработкой; факты
  (`normalize`) — при новом вычислении; `AnalysisView` — хеш representation при
  каждом открытии; Readiness — хеш source при оценке; будущие Publication gate и
  Export — хеш непосредственно перед решением / созданием файла.
- **Read-model** (`asset.get`, `review.queue`) использует **fast** и сообщает
  уровень: `stages.source.verification = "fast" | "sha256"`. Fast может только
  **опровергнуть** source (→ `source_invalid`), но не подтвердить; `ok` при fast
  означает «признаков изменения нет», а не «идентичен». `verify_source=True`
  (отчёты, пересчёт) — sha256.
- Изменение того же размера fast не видит — это ловит sha256 на следующем шаге,
  создающем результат (регрессия `test_same_size_change_needs_verify`).

### 3.6. `NORMALIZE_FAILED` и `VIEW_FAILED`

- Актуальный `NORMALIZE/FAILED` (fingerprint = текущему) новее последнего
  `PASSED` → `blocked` (`NORMALIZE_FAILED:<код>`); коды источника
  (`SOURCE_*`) → `source_invalid`.
- `VIEW/FAILED` новее последнего `NORMALIZE/PASSED` → `blocked`
  (`VIEW_FAILED:<код>`), действие `normalize.run` (пересобрать representation).
- Устаревший отказ (другая версия фактов / normalizer) → `stale`
  (`reprocess_from = normalize`).

---

## 4. Статусы стадий (`stages.<stage>.status`)

`current` · `stale` · `missing` · `failed` · `not_applicable` ·
`not_implemented`; у source — `ok` / `missing` / `changed`.
Каждая стадия отдаёт `{status, reason, stored_fp, current_fp}` (что есть).
Обязательные: source, normalize, view, qc, vision, metadata, readiness (после
одобрения metadata). Советующие: enhancement, creative_review — их устаревание
видно в `stages` и `problems`, но **не** меняет итоговое состояние.

---

## 5. `problems` и `allowed_actions`

Вычисляются **из состояния и stage results** (подсказка агенту; права
проверяет сама операция):

| Состояние / причина | problems | allowed_actions |
|---|---|---|
| `source_invalid` | `SOURCE_MISSING` / `SOURCE_CHANGED` | — (восстановить файл вне Stocker); **никаких** metadata.approve |
| `blocked` NORMALIZE_FAILED | `NORMALIZE_FAILED:<код>` | `normalize.get` |
| `blocked` VIEW_FAILED | `VIEW_FAILED:<код>` | `normalize.run` |
| `blocked` QC_FAILED | `QC_FAILED` | — |
| `blocked` READINESS_BLOCKED | `READINESS_BLOCKED` + коды blocker | `metadata.edit` (если причина в metadata), `metadata.reject` |
| `error` VISION_FAILED | `VISION_FAILED` | `asset.process` |
| `stale` | `STALE:<стадия>:<причина>` | пересчёт с `reprocess_from` (§6) |
| `processing` | `NOT_PROCESSED:<стадия>` / `METADATA_PARTIAL` | следующая операция |
| `human_review` | коды gate | `metadata.edit/rebuild/escalate`; `metadata.approve/reject` (review) |
| `metadata_approved` | — | `readiness.evaluate`; `metadata.reject` (review) |
| `platform_ready` | — | `creative.review` (нет / устарел); `metadata.reject` (review) |
| советующие stale | `STALE:enhancement` / `STALE:creative_review` | `enhancement.assess` / `creative.review` |

---

## 6. Пересчёт (reprocess) — механизм, не ручное лечение

**Реализовано 28.09.2026 (этап C, `app/reprocess.py`, операция `asset.reprocess`, паспорт §35ZY):**

| `reprocess_from` | Цепочка (порядок pipeline) |
|---|---|
| `normalize` | normalize → **view** → qc → enhancement → vision → metadata → readiness |
| `qc` | qc → enhancement → vision → metadata → readiness |
| `vision` | vision → metadata → readiness |
| `metadata` | metadata → readiness |
| `readiness` | readiness (с 28.09.2026, §35ZZC) |
| `enhancement` | enhancement (советующая, от неё никто не зависит) |

- Порядок — **один** порядок pipeline для всех цепочек (`PIPELINE_ORDER`:
  normalize → view → qc → enhancement → vision → metadata); цепочка от стадии —
  хвост этого порядка, особой логики у `normalize` нет. `view` — производный шаг
  (AnalysisView в памяти из representation): ничего не пишет; строится заново,
  если Normalize в этом проходе реально выполнился, иначе — `SKIPPED_CURRENT`.
  Порядок pipeline, а не только зависимость по данным (QC пропускает к Vision).
  Creative Review и Publication **не запускаются** (не подключены к pipeline): их
  устаревание остаётся видно в `state`. Readiness — последняя стадия цепочки
  (правила площадок, без модели): выполняется, только если metadata одобрена
  (`auto_approved` / `approved`); иначе `SKIPPED_NOT_APPLICABLE` (не считается
  выполнением, событий не пишет). Если план предполагает пересборку metadata,
  Readiness в плане — `run` «если metadata останется одобренной». После `stop` на
  metadata план заканчивается (дальше выполнение не идёт).
- Запускается только стадия, которая сейчас **не актуальна**; актуальная —
  `SKIPPED_CURRENT`. Повтор при неизменных входах — `NOTHING_TO_DO`, **ни одного
  события** (и `REPROCESS/DONE` пишется только если что-то выполнено).
- Отказы до записи: `source_invalid` (проверка **SHA256**), `rejected`, нечего
  пересчитывать (`NOTHING_STALE`), стадия не подключена (`NOT_CONNECTED`),
  неактуальный upstream (`UPSTREAM_NOT_CURRENT`). Во время выполнения upstream
  проверяется перед каждой стадией (QC не прошёл → Vision не запускается;
  Vision упал → metadata не пересобирается).
- Metadata пересобирается `metadata.build(force)` (Metadata AI + gate) **только**
  без решений и правок человека; `approved` / `rejected` / `edited_fields` →
  остановка (`HUMAN_DECISION` / `HUMAN_EDITS`), metadata остаётся честно `stale`.
- Один AnalysisView на все пиксельные стадии пересчёта.
- `dry_run=true` по умолчанию: план (состояние, причина, стадии run / skip / derive /
  stop, что будет записано), без записи.
- **`through`** ограничивает цепочку **по порядку pipeline** и должен лежать в ней:
  `from=qc, through=qc` → QC; `from=qc, through=vision` → QC → Enhancement →
  Vision; `from=vision, through=metadata` → Vision → Metadata; `through` выше
  `from` (`from=vision, through=qc`) → отказ `INVALID_THROUGH` без записи.
- **`from=metadata`**: Vision актуален, metadata устарела → пересобирается
  только metadata; Vision не актуален → отказ `UPSTREAM_NOT_CURRENT` без записи.
- **Upstream проверяется дважды:** при построении плана и **непосредственно
  перед выполнением каждой стадии** (состояние вычисляется заново). Если между
  планом и записью upstream устарел — `STOPPED` `UPSTREAM_NOT_CURRENT`, запись
  стадии и `REPROCESS/DONE` не выполняются.
- **`REPROCESS/DONE`** пишется, **только если хотя бы одна стадия реально
  пересчитана**. Пропуски актуальных (`SKIPPED_CURRENT`) и производный view
  (`DERIVED`) не в счёт: повтор полностью актуального объекта с любым
  `reprocess_from` — ноль новых событий.
- Доступ: `pipeline` (человек, агент); **n8n — нет** (массовый пересчёт не
  подключён). Одна операция — один объект.

- `reprocess_from` — первая обязательная устаревшая стадия; пересчитывается она
  и всё ниже по графу, **через те же операции**, что и обычная обработка.
- Отдельная контролируемая операция пересчёта (`asset.reprocess {from}`,
  ограничение объёма за запуск, human / agent) — следующий шаг, не n8n.
- Решения человека пересчётом не отменяются: approved metadata с `stale`
  требует повторного подтверждения, а не автоматической перезаписи.

---

### 6a. Известные ограничения (28.09.2026)

- Модель в отпечатке Vision — по идентификатору: замена весов под тем же именем
  отпечатком не видна.
- **Vision nondeterminism.** Vision запрашивается с умолчанием сервера по
  `temperature`: при **том же** отпечатке ответ может отличаться (#10: «Concrete
  Surface Detail…» и «Concrete Structure Detail…» в двух прогонах). Отпечаток
  доказывает идентичность **входов**, а не идентичность результата модели.
- **Design requirement (следующий этап, не реализовано):** execution identity
  Vision — как минимум provider; model name; **model revision или hash весов,
  если сервер их отдаёт**; prompt version / hash; schema hash; request params;
  image encoding; view fingerprint. Массовый пересчёт из-за этого не нужен.

#### Что фактически отдаёт LM Studio (исследовано 28.09.2026, паспорт §35ZZA)

| Поле execution identity | Источник | Есть? |
|---|---|---|
| provider | конфигурация Stocker (`lmstudio`) | ✅ |
| model name / key | ответ `/v1/chat/completions` `model`; `/api/v1/models` `key` | ✅ `qwen3-vl-8b-instruct` |
| publisher, display_name, architecture, params | `/api/v1/models` | ✅ `unsloth`, `qwen3vl`, `8B` |
| quantization | `/api/v1/models` (`name`, `bits_per_weight`); `/api/v0` `quantization`; ответ `/api/v0` `model_info.quant` | ✅ `Q5_K_M`, 5 bit |
| format | `/api/v1/models` `format`; `/api/v0` `compatibility_type` | ✅ `gguf` |
| size_bytes | `/api/v1/models` | ✅ 7 010 144 672 (= основной GGUF + mmproj — см. ниже) |
| параметры загрузки экземпляра | `/api/v1/models` `loaded_instances[].config` (context_length, batch sizes, flash_attention, parallel, speculative…) | ✅ |
| **runtime (движок) и его версия** | только ответ `/api/v0/chat/completions` `runtime` | ✅ `llama.cpp-win-x86_64-nvidia-cuda12-avx2` 2.46.0 |
| model revision / version | — | ❌ **unavailable** |
| путь / имя загруженного файла | — (API не отдаёт) | ❌ **unavailable** |
| **weights hash** | — | ❌ **unavailable** |
| стабильный идентификатор экземпляра | `loaded_instances[].id` = тот же key; времени загрузки нет | ❌ (нет отдельного id) |

- **Ловушка:** `system_fingerprint` в ответе `/v1` равен имени модели — это **не**
  идентичность весов; использовать как таковую нельзя.
- Vision-модель — **два файла**: основной GGUF и проектор изображений `mmproj`.
  `size_bytes` API = сумма их размеров (проверено: 5 851 114 336 + 1 159 030 336).
  Файлы лежат на этой же машине (`F:\ai\models\unsloth\Qwen3-VL-8B-Instruct-GGUF\`),
  но **API не сообщает, какие файлы загружены**: связь key → файлы — вывод по
  папке и размеру, а не данные LM Studio. Хеш этих файлов Stocker мог бы
  посчитать сам, но это был бы хеш «вероятно загруженных» файлов — решение
  отдельное, сейчас не принимается.
- Итог: execution identity может включать только поля с ✅; отсутствие revision
  и hash весов остаётся **явным ограничением**. `size_bytes` + quantization +
  runtime version ловят многие, но не все замены весов.


## 7. Совместимость и миграция для существующих 129 объектов

| Что | Правило |
|---|---|
| `assets.status` | наследие (пишет только QC); не используется для решений; остаётся в `asset.get` как есть до удаления |
| `pipeline.ready` | = metadata approval; **переименовать** в `metadata_approved` при подключении (§8); `review.queue.summary.ready` → `metadata_approved` + отдельно `platform_ready` |
| QC без `metrics.view` (все 129) | `stale` (`NO_FINGERPRINT`) → пересчёт QC по view |
| Vision без `view` (все 128) | `stale` (`NO_FINGERPRINT`), в т.ч. 18 Display P3 → пересчёт Vision |
| Vision `local-v1` / неизвестный провайдер | `stale` (`PROMPT_CHANGED` / `UNKNOWN_PROVIDER`) |
| Metadata на старом Vision event | `stale` (`VISION_CHANGED`) |
| Creative Review (373, отпечаток без view) | `stale` (`FINGERPRINT_CHANGED`) — советующая |
| Readiness v1 при metadata не в одобрении (#47) | `not_applicable`, событие вытеснено |
| #2 (source изменён), #67, #68 (файлы удалены) | `source_invalid` |
| Существующие события | не изменяются и не удаляются; миграции данных нет — всё выводится при чтении |

Ожидаемое распределение сейчас: 3 × `source_invalid`, остальные — `stale`
(первая устаревшая стадия — QC). Это правда о данных, а не ошибка: пересчёт —
отдельным контролируемым шагом (§6).

---

## 8. Порядок подключения (следующие шаги, не в этом)

1. **Этап A (28.09.2026, §35ZV):** `asset.get` / `asset.list` / `review.queue`:
   поле `state` из `app/asset_state.py`; `problems` и `allowed_actions` — из него;
   `pipeline.ready` → `pipeline.metadata_approved`; `pipeline.source` — из
   `stages.source`; фильтр `asset.list` `ready` → `state` / `metadata_approved`;
   `review.queue.summary.ready` → `by_state`, `metadata_approved`,
   `platform_ready`, `problem_counts`; очередь человека — объекты в состоянии
   `human_review` (не устаревшие).
2. Явные отпечатки входов в событиях стадий, где они сейчас вычисляются
   косвенно (QC — версия правил QC, Vision — `input_fingerprint`).
3. `asset.reprocess` — контролируемый пересчёт.
4. Readiness → Creative Review → Publication gate в pipeline; затем n8n.

---

## 9. Решения пользователя (28.09.2026)

1. Metadata Gate не удалять: остаётся внутри Metadata (корректность, privacy /
   content risks, достаточность, human review **для metadata**).
2. Publication / Export Gate — отдельное понятие после Metadata → Readiness →
   Creative Review; решает выпуск в конкретный export profile.
3. Не использовать одно `ready` для разных смыслов (§3.3).
4. Unified Asset State — производное; результаты стадий остаются отдельными.
5. Staleness: `current fingerprint != stored fingerprint → STALE`; stale-данные
   не лечить вручную — общий механизм, затем контролируемый пересчёт.
6. Source integrity: без source объект не готов независимо от `metadata.state`;
   #67 / #68 — регрессии.
7. На этом шаге не переписывать production pipeline, не подключать n8n к
   Readiness / Creative, не расширять AVIF / HEIC, не реализовывать Export.
