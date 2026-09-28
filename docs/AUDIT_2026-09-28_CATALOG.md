# Аудит каталога после пересчёта core pipeline (28.09.2026, только чтение)

> Паспорт §35ZZQ. Пересчёт всех объектов, устаревших после введения AnalysisView,
> отпечатков и gate-v1.3: контролируемый `asset.reprocess from=qc` партиями по 13
> (§35ZZH–§35ZZP) + ранее — #3, #10, партия 4, диагностическая партия 7, повторный
> gate 13 объектов. Creative Review в core pipeline не запускался. Сравнение — с
> копией БД до этапа C (`stocker-before-stage-c.db`, scratchpad сессии).

## 1. Итоговые состояния (129 объектов)

| Состояние | Объектов |
|---|---|
| `publication_approved` | **103** |
| `human_review` | **23** |
| `source_invalid` | **3** |
| `stale` / `blocked` / `error` / `processing` | 0 |

- Publication approved: **Adobe 103, Shutterstock 103** (по площадкам совпадают).
- Заметки Publication: `NO_CURRENT_ADVICE` — 94 объекта (Creative не запускался),
  `ADVISOR_ATTENTION` — 2 объекта (#10, #14; совет Creative `attention`, не блокирует).
- Creative Review: current 9 (оценены по запросу 28.09), stale 94 (не запускался
  после пересчёта Vision), not_applicable 26 (human_review / source_invalid).
- Устаревших стадий нет: `reprocess_from` пуст у всех; dry-run `from=qc` по всему
  каталогу — ни одного шага `run`.

## 2. Требуют действия человека (26)

### 2.1. `human_review` (23)

| Причина | Объекты |
|---|---|
| Товарный знак / бренд в надписи (Vision `brands`) | #8 (Вектор Технологий, P220), #73 (bloody), #83 (Monarch), #85 (Monarch + `LEGAL_CLAIM` certified), #89 (P.I.T.), #96 (P.I.T.), #109 (Monarch + `BRAND_IN_TEXT`) |
| Бренд в надписи по правилам текста | #5 (`АО "ШПЗ"`, юрформа), #54 (`АО "ЩЛЗ"`, юрформа), #86 (`STMicroelectronics`, gate-v1.3 MIXED_CASE_NAME) |
| Ручная эскалация: однословный бренд ЗАГЛАВНЫМИ (ограничение gate-v1.3) | #46 (АТРИОН), #48 (РЕКАНТА), #74 (LKDS), #80 (АТРИОН) |
| `LEGAL_CLAIM` (keyword в metadata) | #45 (`compliance`), #56 (`compliance`), #112 (`certification`; в кадре также «АТРИОН») |
| Распознаваемые люди | #37 (man), #38 (people), #44 (people), #43 (child), #47 (child) |
| Ошибка валидации metadata | #61 (`DESCRIPTION_TOO_LONG`) |

### 2.2. `source_invalid` (3)

| Объект | Причина |
|---|---|
| #2 | `SOURCE_CHANGED` — файл изменён (SHA256 ≠ зарегистрированному) |
| #67, #68 | `SOURCE_MISSING` — файлы удалены |

Не пересчитываются и не публикуются, пока source не восстановлен.

## 3. Изменения решения gate при пересчёте (итог против состояния до этапа C)

| Объект | Было → стало | Почему |
|---|---|---|
| #8 | auto_approved → human_review | новый Vision по правильно интерпретированному P3-view прочитал бренд «Вектор Технологий» и «MODEL P220» |
| #86 | auto_approved → human_review | gate-v1.3: `STMicroelectronics` в надписи при пустом `brands` |
| #45 | auto_approved → human_review | новый Metadata AI добавил keyword `safety compliance` → существующее правило `LEGAL_CLAIM` |
| #46, #48, #74 | auto_approved → human_review | ручная эскалация (однословный бренд ЗАГЛАВНЫМИ) |
| #80 | human_review → (auto_approved, publication_approved в партии 5) → human_review | прежний reason `LEGAL_CLAIM: certified` исчез при новой генерации Metadata; объект вернули ручной эскалацией (в кадре «АТРИОН») |

Решение не изменилось, изменился только reason (новый Metadata результат): #56,
#61, #73, #85, #112. Автоматических переходов human_review → publication_approved в
итоге нет (единственный — #80 — возвращён человеком).

## 4. Brand watch (только отчёт, правила не менялись)

Критерий: `publication_approved` + пустой `brands` + однословная надпись ЗАГЛАВНЫМИ,
классифицированная descriptive. По всему каталогу: **#62 `HR`**, **#78 `OFF`** —
надписи на панели / переключателе; новых случаев с названием производителя после
эскалации #46 / #48 / #74 / #80 не обнаружено. Отдельно: #112 (human_review по
другой причине) — «АТРИОН» при пустом `brands`, повторяет исходную ситуацию #80.

## 5. Идемпотентность и согласованность

- Каждая партия: повтор `from=qc` и `from=publication` — `NOTHING_TO_DO` по всем
  объектам, **0 новых событий** (9 партий, 113 объектов + 13 повторного gate).
- Инвариант «`publication_approved` только при актуальном Publication с непустым
  `approved_for`» — 0 нарушений после каждой партии и по итоговому каталогу.
- `check_consistency`: все 8 проверок OK, файлов `data/internal` без события — 0.
- Найдено и исправлено в ходе пересчёта: повтор стадий по запросу для
  неприменимого Readiness отвечал `UPSTREAM_NOT_CURRENT` вместо no-op (a72f9fc).

## 6. События

| | |
|---|---|
| Всего в БД | **3795** |
| До этапа C | 2655 |
| Добавлено с этапа C | 1140 |

С этапа C по стадиям: QC/PASSED 126, AI/PASSED 126, METADATA_AI/PASSED 126,
METADATA/DRAFTED 126, METADATA/GATED 143, READINESS/EVALUATED 107,
PUBLICATION/EVALUATED 107, CREATIVE_REVIEW/ADVISED 9, METADATA/ESCALATED 4,
REPROCESS/DONE 266.

## 7. Наблюдения (без изменений архитектуры)

- Vision и Metadata AI недетерминированы при одинаковых входах: у одного объекта
  между прогонами меняются `brands` (#8), `text_visible` (#108), keywords (#45,
  #56, #61, #80, #85, #112). Gate оценивает текущий результат; отпечаток входов
  доказывает идентичность входов, не ответа (контракт §6a).
- Ограничение gate-v1.3 — однословные бренды ЗАГЛАВНЫМИ: закрыто ручной
  эскалацией 4 объектов; правило и словарь не менялись (решение пользователя).
- Creative Review у 94 одобренных объектов не запускался — Publication отмечает
  `NO_CURRENT_ADVICE` (по контракту не блокирует).
