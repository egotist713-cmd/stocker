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

### 2.2. Publication / Export Gate (будущий, только определение)

Отдельное понятие, **не** второй metadata gate. Отвечает на вопрос «можно ли
выпускать объект в конкретный export profile». Входы: актуальный Readiness для
профиля (`ready`), актуальная metadata в состоянии одобрения, актуальный
Creative Review (если политика gate его требует — решение при реализации),
решения человека. Отпечаток = отпечатки этих входов + версия политики gate +
профиль. Результат — на профиль площадки.

### 2.3. Распространение устаревания

- **Собственное устаревание:** `stored_fp != current_fp`, либо отпечатка нет
  (наследие) → `stale` (`FINGERPRINT_CHANGED` / `NO_FINGERPRINT`).
- **Унаследованное:** если обязательная стадия выше по графу `stale`, `failed`
  или `missing`, результат ниже по графу тоже не актуален →
  `stale` (`UPSTREAM_STALE:<стадия>`), даже если его собственный отпечаток
  совпадает.
- Metadata: другой `sources.vision.event_id`, чем последнее `AI/PASSED`, →
  `VISION_CHANGED`; другой `builder_version` / `policy_version` для
  **не-человеческих** состояний (`draft`, `auto_approved`, `human_review`) →
  `RULES_CHANGED`. Решение человека (`approved`) сменой правил не отменяется, но
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
| 10 | `publication_approved` | (будущее) Publication gate разрешил выпуск для профиля | — |
| 11 | `ready_for_export` | (будущее) Export preparation создал и проверил файл площадки | нет — «terminal до изменения входов» |

Состояния 10–11 пока недостижимы: Publication gate и Export не реализованы.

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

- `reprocess_from` — первая обязательная устаревшая стадия; пересчитывается она
  и всё ниже по графу, **через те же операции**, что и обычная обработка.
- Отдельная контролируемая операция пересчёта (`asset.reprocess {from}`,
  ограничение объёма за запуск, human / agent) — следующий шаг, не n8n.
- Решения человека пересчётом не отменяются: approved metadata с `stale`
  требует повторного подтверждения, а не автоматической перезаписи.

---

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
