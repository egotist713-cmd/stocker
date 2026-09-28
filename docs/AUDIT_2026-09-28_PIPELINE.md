# Аудит рабочего pipeline после Vision (28.09.2026, read-only)

> Паспорт §35ZT. Перед этапом «Metadata → Readiness → Creative Review → Gate /
> human review → состояние объекта → n8n orchestration». Код не менялся; данные —
> рабочая БД (129 объектов) на момент коммита 3a78df5.

## 1. Что реально работает end-to-end

Автоматически: n8n `stocker-ingest` (каждые 5 мин) → `incoming.list` →
`asset.process_file` → `worker.process_asset`:

```
ingest (INGEST/DONE)
→ проверка source (SOURCE/INVALID при расхождении — стоп)
→ NORMALIZE: факты (EVALUATED) + representation (PASSED) — FAILED = стоп
→ AnalysisView (VIEW/FAILED = стоп)
→ QC (QC/PASSED|FAILED; FAILED = стоп)
→ Enhancement: правила (ASSESSED), советник Qwen только для disputed (ADVISED)
→ Vision (AI/PASSED|FAILED)
→ Metadata build: Metadata AI (METADATA_AI/PASSED|FAILED) → Python-правила
  → validation → review gate — одной транзакцией (METADATA/DRAFTED + METADATA/GATED)
→ КОНЕЦ
```

Затем n8n `stocker-digest` (`review.queue`) → `stocker-notify` →
`notification.record` (NOTIFY/SENT). `stocker-retry` — выключен.

**Pipeline заканчивается на `METADATA/GATED`.** Состояние metadata:
`auto_approved` / `human_review` / `draft` (gate `deferred` — partial).

## 2. Что подключено к `asset.process`, а что — только операции сервиса

| Стадия | В `asset.process` | В n8n | Только операция сервиса |
|---|---|---|---|
| Normalization, View, QC | ✅ | через `asset.process_file` | `normalize.run/evaluate/get` |
| Enhancement (правила + советник) | ✅ | через `asset.process_file` | `enhancement.assess/advise/get` |
| Vision | ✅ | через `asset.process_file` | — |
| Metadata build + gate | ✅ | через `asset.process_file` | `metadata.build/rebuild/edit/gate/escalate` |
| **Readiness** | ❌ | ❌ | `readiness.evaluate/get` |
| **Creative Review** | ❌ | ❌ | `creative.review/get` |
| Human approve / reject | ❌ | ❌ (запрещено) | `metadata.approve/reject` — только human |

`asset.process` для объекта с готовым Vision (без `force`) выполняет **только**
`run_metadata` — Normalization / QC / Enhancement не обновляются.

## 3. Какие события и статусы реально записываются

| stage / status | Событий |
|---|---|
| INGEST/DONE | 129 |
| NORMALIZE EVALUATED / PASSED / FAILED | 504 / 252 / 21 |
| QC/PASSED | 132 |
| ENHANCEMENT ASSESSED / ADVISED / FAILED | 362 / 41 / 9 |
| AI/PASSED | 128 (с `view` — **0**: все Vision-результаты — до AnalysisView) |
| METADATA_AI PASSED / FAILED | 128 / 1 |
| METADATA DRAFTED / GATED | 130 / 201 |
| METADATA APPROVED / REJECTED | **0** — решения человека ни разу не принимались |
| READINESS/EVALUATED | 219 (v1 110, v2 109) |
| CREATIVE_REVIEW ADVISED / FAILED | 373 / 6 |
| PRIVACY/REDACTED, SOURCE/INVALID, SOURCE/NORMALIZED | 2 / 1 / 2 |
| NOTIFY/SENT | 14 |

Actor: `agent:claude-code` 1458, `workflow:n8n` 388, `agent:openclaw` 7.
**Readiness и Creative Review запускались только вручную** (скрипты), не n8n и
не агентом.

Статусы объекта:

- `assets.status` — **наследие**: пишет только QC (`PASSED`/`FAILED`); сейчас
  129 × `PASSED`; решений по нему никто не принимает, view отдаёт как есть.
- Фактический жизненный цикл — `metadata_json.state`: `auto_approved` 109,
  `human_review` 19, нет metadata 1 (#2).
- Остальное — производные сводки в `asset.get → pipeline` (normalize,
  enhancement, stock_readiness, creative_review), вычисляемые из событий.
- Единого состояния объекта («готов / ждёт человека / заблокирован / ошибка»)
  **нет**.

## 4. Контракты между стадиями (что уже существует)

| Связь | Контракт в коде | Документ |
|---|---|---|
| Vision → Metadata | `assets.ai_result` (AIAnalysis) + `sources.vision.event_id`; Metadata **не** пересобирается при новом Vision без `force` | METADATA_CONTRACT |
| Metadata → Gate | gate-v1.2 внутри build/rebuild/edit, та же транзакция; `auto_approved` / `human_review` / `draft` | METADATA_CONTRACT, паспорт §6A |
| Gate → human | `review.queue` (human_review), `metadata.approve/reject` — human | SERVICE_CONTRACT |
| Metadata → Readiness | Readiness оценивает только `auto_approved` / `approved`; fingerprint — metadata (state, fields), vision event, facts event, QC | STOCK_READINESS_CONTRACT §3 |
| Readiness → Creative | в документе: Creative **после** Readiness (§4.3); в коде зависимости **нет** — нужен только Vision | STOCK_READINESS_CONTRACT §4.3 |
| Enhancement / Creative → дальше | **никто не потребляет**: ни Readiness, ни gate, ни metadata их результаты не читают | — |

## 5. Расхождения, которые нужно решить до следующего этапа

1. **Два разных «ready».** `pipeline.ready` = metadata `auto_approved|approved`
   (его считает `review.queue` → digest); `stock_readiness.ready_for` — правила
   площадок. Сейчас совпадают (109 = 109), но это разные понятия под одним словом.
2. **Потерянный source не виден в сводке.** `pipeline.source` считается только
   по `SOURCE/INVALID` и QC: #67, #68 (файлы удалены, `NORMALIZE/FAILED
   SOURCE_MISSING`) показаны как `source: ok`, а `allowed_actions` предлагает
   `metadata.approve`. `review.queue` их как проблемные не показывает.
3. **Сводка не знает новых стадий:** `NORMALIZE_FAILED`, `VIEW/FAILED`,
   Enhancement не попадают в `problems` / `allowed_actions`; `allowed_actions`
   никогда не предлагает `normalize.run` и `creative.review`.
4. **Место gate.** Целевая цепочка ставит Gate после Readiness / Creative, а
   сейчас review gate metadata — **внутри** Metadata и Readiness от него зависит.
   Нужно решить: это один gate или два (риск содержимого metadata ≠ итоговое
   решение о публикации).
5. **Нет единого состояния объекта**; `assets.status` устарел.
6. **Staleness не распространяется вниз.** У Vision нет fingerprint: все 128
   Vision-результатов сделаны до AnalysisView (18 Display P3 — на
   неинтерпретированных P3-пикселях). Metadata не отслеживает смену Vision.
   Отпечаток Creative Review теперь включает view — все 373 оценки формально
   устарели, но `creative_review` в сводке **не имеет** признака `stale`.
7. **Readiness для объектов, ушедших в human_review**, остаётся старым событием
   (#47: `readiness-v1`, stale) — `NOT_EVALUATED` событие не пишет.
8. **Human review не проверен в работе:** 19 объектов ждут, решений 0.
9. **n8n не оркестрирует ничего после Metadata:** нет вызовов Readiness /
   Creative; digest считает «ready» по metadata.
10. **Устаревшие места документов:** SERVICE_CONTRACT — `creative-review-v2`
    (в коде v3); N8N_CONTRACT §8 — «ingest → gate → сводка» без Readiness.
