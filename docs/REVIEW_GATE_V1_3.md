# Review Gate `gate-v1.3` — описание

> Код: `app/review_gate.py` (`POLICY_VERSION = "gate-v1.3"`). Контракт:
> `docs/METADATA_CONTRACT.md` §6A (§6A.4 — gate-v1.3). Решения: паспорт §35ZZE
> (введение v1.3), §35ZZT–§35ZZU (поведение после `metadata.edit`).
> Описание составлено по коду на 29.09.2026.

## 1. Назначение

Gate отвечает на один вопрос: **можно ли пропустить metadata объекта без
человека.** Итог — маршрут:

| `decision` | Состояние metadata | Смысл |
|---|---|---|
| `auto_approved` | `auto_approved` | Причин для проверки нет — дальше Readiness / Publication |
| `human_review` | `human_review` | Есть хотя бы одна причина (`reasons`) — решает человек |
| `deferred` | `draft` | Metadata неполные (`completeness = partial`) — решение откладывается |

Gate — **чистые функции без БД и без AI**. Он не меняет содержимое metadata
(title / description / keywords), не исправляет ошибки и не «угадывает» бренды
по словарю. Он читает результат Vision, результат builder (валидацию и
grounding) и решает только маршрут.

## 2. Когда gate запускается

Gate пересчитывается при каждом изменении содержимого metadata:

| Триггер (`trigger` в `METADATA/GATED`) | Операция |
|---|---|
| `build` / `build_force` | `metadata.build` — новый draft (Metadata AI → builder); `build_force` — с `force` (в т. ч. `asset.reprocess`) |
| `rebuild` | `metadata.rebuild` — пересборка правилами без AI |
| `edit` | `metadata.edit` — правка человека |
| `gate` | `metadata.gate` — повторная оценка без изменения содержимого (например, при смене версии политики) |
| — | `metadata.escalate` — ручная эскалация (gate пересчитывается с `MANUAL_ESCALATION`) |

Каждое решение фиксируется событием `METADATA/GATED` (`policy_version`,
`decision`, коды `reasons` и `notes`, `state_before`, `trigger`).

**Решения человека gate не трогает.** Он меняет только состояния `draft`,
`auto_approved`, `human_review` (`GATEABLE_STATES`). Состояния `approved` и
`rejected` gate не пересматривает ни при какой смене правил.

## 3. Входы

1. **Vision** (`assets.ai_result`): `brands`, `logos`, `text_visible`,
   `people` (`present`, `count`), `subject`, `title`, `description`,
   `keywords`, `commercial_context`, `technical_subjects`, `editorial_risk`,
   `ai_generated`.
2. **Metadata** (`assets.metadata_json`): `fields` (title / description /
   keywords), `validation.errors`, `grounding.specific_claims`,
   `completeness`, `review_gate.escalation`.
3. **Аварийный выключатель** `STOCKER_AUTO_APPROVE` (по умолчанию `1`). При `0`
   автоодобрение запрещено: объект без причин получает `AUTO_APPROVE_DISABLED`
   и идёт к человеку.

Важно: gate оценивает **и Vision, и metadata**. Поэтому правка metadata
не снимает причин, которые пришли из Vision (например, `TRADEMARK`).

## 4. Проверки и причины (`reasons`)

Любая причина отправляет объект на `human_review`. Проверки выполняются все
(не до первого срабатывания), в отчёт попадают все найденные причины.

| Код | Откуда | Условие |
|---|---|---|
| `VALIDATION_ERRORS` | builder `validation.errors` | Есть ошибки валидации (кроме `UNCONFIRMED_CLAIM`, у неё свой код). Коды: `TITLE_EMPTY`, `TITLE_TOO_LONG` (> 200), `DESCRIPTION_EMPTY`, `DESCRIPTION_TOO_LONG` (> 200), `TOO_FEW_KEYWORDS` (< 7), `TOO_MANY_KEYWORDS` (> 49), `KEYWORD_TOO_LONG` (> 50 символов), `BRAND_IN_TEXT` (бренд / логотип из Vision в title или description) |
| `UNCONFIRMED_CLAIM` | builder `grounding.specific_claims` | Конкретное утверждение в metadata не подтверждено Vision |
| `TRADEMARK` | Vision `brands` / `logos` | Модель назвала бренд или логотип |
| `TEXT_BRAND_OR_LEGAL` | Vision `text_visible` | Надпись в кадре классифицирована как бренд / юридическое (§5) |
| `LEGAL_CLAIM` | metadata title / description / каждое keyword | Юридически значимое утверждение (§6) |
| `PEOPLE_RECOGNIZABLE` | Vision | Узнаваемый человек или ребёнок (§7) |
| `PERSONAL_DOCUMENT` | Vision | Документ с персональными данными (§8) |
| `EDITORIAL_RISK` | Vision `editorial_risk` | Модель указала редакционный риск |
| `AI_GENERATED` | Vision `ai_generated` | Изображение помечено как сгенерированное |
| `MANUAL_ESCALATION` | `review_gate.escalation` | Человек / агент вручную поднял риск (`metadata.escalate`) |
| `AUTO_APPROVE_DISABLED` | окружение | Причин нет, но автоодобрение выключено |

### Заметки (`notes`) — информационные, не блокируют

| Код | Смысл |
|---|---|
| `TEXT_TECHNICAL` | Надпись технического характера (цифры, коды, предупреждения, знаки соответствия) |
| `TEXT_DESCRIPTIVE` | Обычная надпись без признаков бренда |
| `PEOPLE_PARTIAL` | Человек виден частично (руки, со спины, силуэт) |
| `PEOPLE_INCIDENTAL` | Люди есть, но признаков узнаваемости нет |

## 5. Классификация надписей `text_visible`

Каждая надпись получает одну категорию — **по первому сработавшему правилу**
в таком порядке:

**brand_or_legal → причина `TEXT_BRAND_OR_LEGAL`:**

1. `BRAND` — содержит бренд / логотип, который Vision перечислил в `brands` /
   `logos`.
2. `LEGAL_FORM` — организационно-правовая форма: `Inc`, `Ltd`, `LLC`, `GmbH`,
   `AG`, `Corp`, `PLC`, `S.A.`, `SRL`, `BV`, `ООО`, `ОАО`, `ЗАО`, `ПАО`, `АО`,
   `НПО`, `ФГУП`, `ГУП`, `МУП`, `ИП` (пример: `АО "ШПЗ"` — #5).
3. `LEGAL_SYMBOL` — `®`, `™`, `©`.
4. `LEGAL_WORD` — `copyright`, `patent`, `patented`, `trademark`,
   `all rights reserved`.
5. `CONTACT` — URL / домен (`.com`, `.ru`, `.рф` …), e-mail, телефон
   (не меньше 10 цифр).
6. **Новое в v1.3 — имя собственное** (только если в надписи нет цифр и
   слов-предупреждений):
   - `PROPER_NAME` — 2 и более слов, каждое с заглавной буквы и строчными
     (`Вектор Технологий` — #8);
   - `MIXED_CASE_NAME` — слово от 5 букв со смешанным регистром внутри
     (`STMicroelectronics` — #86, `iPhone`).

**technical → заметка `TEXT_TECHNICAL`:**

7. `CONFORMITY_MARK` — надпись целиком из знаков соответствия: `CE`, `EAC`,
   `UL`, `RoHS`, `FCC`, `CCC`, `UKCA`, `TÜV`, `VDE`, `ГОСТ`, `СТ`. Это
   техническая маркировка, не бренд.
8. `DIGITS` — есть цифры (`63A`, `400B~`, `0411E.06.05.090`).
9. `CODE` — код вида `AB-12`.
10. `WARNING_WORD` — `warning`, `caution`, `danger`, `high voltage`, `notice`,
    `exit`, `emergency`, `fire`, `stop`, `no entry`, `keep out`, `внимание`,
    `осторожно`, `опасно`, `высокое напряжение`, `не включать`, `запрещено`,
    `выход`, `стоп`.

**descriptive → заметка `TEXT_DESCRIPTIVE`:**

11. `DEFAULT` — всё остальное.

## 6. Юридически значимые утверждения (`LEGAL_CLAIM`)

Ищутся в **metadata** (title, description и каждом keyword отдельно) как целые
слова, без учёта регистра:

`certified`, `certification`, `compliant`, `compliance`, `approved`,
`patented`, `patent`, `trademark` / `trademarked`, `licensed`, `official`,
`guaranteed`, `guarantee`, `warranty`, `ISO` + номер (`ISO 9001`),
`UL listed`, `CE marked`, `meets … standard(s)`.

Совпадение внутри составного keyword тоже считается: `safety compliance` →
`LEGAL_CLAIM: keywords: compliance` (#45, #56), `ce certified` → `certified`
(#85), `eac certification` → `certification` (#112).

## 7. Люди (`PEOPLE_RECOGNIZABLE`)

Проверка только если Vision сообщил о людях (`people.present` или
`people.count > 0`). Уровень риска определяется по порядку:

1. **Дети — всегда `recognizable`** (с v1.2). Слова `child`, `children`,
   `kid(s)`, `baby`, `toddler`, `infant`, `boy`, `girl`, `schoolboy`,
   `schoolgirl`, `teen(ager)` и их формы ищутся в Vision `subject`, `title`,
   `description` — **не в keywords** (концепт «children's playground» без
   ребёнка не срабатывает). Маркер `child:<слово>` (#43, #47).
2. **Узнаваемость по описанию:** `face`, `facial`, `portrait`, `headshot`,
   `smiling`, `looking at (the) camera`, `eyes` (отрицания вида
   `face not visible` / `faceless` исключаются).
3. **Частичное присутствие → `partial`** (заметка, не блокирует): `hand(s)`,
   `glove(s)`, `gloved`, `arm(s)`, `finger(s)`, `legs`, `feet`,
   `from behind`, `back view`, `silhouette`, `unrecognizable`, `anonymous` и
   т. п. Не-человеческие фразы (`hand tool`, `robotic arm`, `crane arm` …)
   предварительно вырезаются.
4. **Человек как главный объект** — слова `person`, `people`, `man`, `woman`,
   `worker`, `engineer`, `technician`, `operator`, `electrician`, `builder`,
   `welder`, `mechanic` в Vision `subject` → `recognizable`, маркер
   `subject:<слово>` (#37, #38, #44).
5. Иначе — `unclear` (заметка `PEOPLE_INCIDENTAL`, с v1.1 не блокирует).

Builder при этом ставит флаг `model_release_required`. Это **требование
контракта**, а не сведения о наличии или отсутствии release: данных о release в
системе нет.

## 8. Документы с персональными данными (`PERSONAL_DOCUMENT`, с v1.2)

Термины в Vision `subject` / `title` / `description` / `keywords`:
`passport`, `identity document`, `identity card`, `id card`, `national id`,
`driver's license / licence`, `driving license / licence`,
`residence permit`, `residential registration`, `propiska`,
`birth certificate`, `marriage certificate`, `bank card`, `credit card`,
`debit card`, `social security card`, `personal details`, `personal data`.

Такие объекты всегда идут к человеку (#67 — паспорт, #68 — прописка).
Распознанный текст документа не хранится: worker удаляет `text_visible` до
сохранения результата Vision (в событии фиксируется только число удалённых
надписей). Поэтому у #67 / #68 `text_visible` пуст.

## 9. Ручная эскалация (`MANUAL_ESCALATION`)

`metadata.escalate` записывает `review_gate.escalation` (причина и время) и
пересчитывает gate. Эскалация:

- **сохраняется** при `metadata.edit`, `metadata.rebuild`, `metadata.gate`
  (gate читает её из текущего `review_gate`);
- **снимается только решением человека** — `metadata.approve` или
  `metadata.reject`;
- блокирует `deferred`: эскалированный неполный объект идёт на
  `human_review`, а не откладывается.

Используется для случаев, которые правила не видят (§11.1): #46, #48, #74, #80.

## 10. Решение

```
если completeness == partial и нет эскалации  → deferred   (state: draft)
иначе если есть хотя бы одна причина          → human_review
иначе если STOCKER_AUTO_APPROVE = 0           → human_review (AUTO_APPROVE_DISABLED)
иначе                                         → auto_approved
```

Результат хранится в `metadata_json.review_gate`: `policy_version`,
`decision`, `reasons`, `notes`, `text_items` (категория и правило каждой
надписи), `people_risk`, `escalation`, `auto_approve_enabled`,
`evaluated_at`.

Дальше: `auto_approved` и `approved` (решение человека) считаются
одобренными metadata — для них применимы Readiness и Publication.
`human_review` / `draft` — Readiness, Creative и Publication `not_applicable`.

## 11. Известные ограничения

1. **Однословный бренд ЗАГЛАВНЫМИ буквами не распознаётся**, если Vision не
   поместил его в `brands` (`АТРИОН`, `РЕКАНТА`, `LKDS`). Без словаря такие
   надписи не отличить от `ON` / `OFF` / `ENTER` — они получают
   `TEXT_DESCRIPTIVE`. Решение пользователя: словарь брендов не вводить,
   правило не менять; такие объекты эскалируются вручную, brand watch —
   только отчёт (#62 `HR`, #78 `OFF` — служебные надписи).
2. **`metadata.edit` пересчитывает gate целиком.** Если удалить только
   сработавший термин, объект может стать `auto_approved`, даже когда в кадре
   остаётся необнаруживаемый бренд. Пример — #112: после удаления
   `eac certification` gate дал бы `auto_approved` при «АТРИОН» в
   `text_visible` и пустом `brands`. Правило работы: правка metadata не должна
   использоваться для обхода ручной проверки; такие объекты остаются на
   `human_review` до решения человека.
3. **Правка metadata не снимает причин из Vision.** `TRADEMARK` и
   `TEXT_BRAND_OR_LEGAL` берутся из Vision, поэтому, например, удаление
   `Monarch` из description снимает `BRAND_IN_TEXT`, но не `TRADEMARK: Monarch`
   (#109).
4. **Недетерминированность Vision и Metadata AI.** При одинаковых входах
   ответы модели различаются (`brands`, `text_visible`, keywords), и решение
   gate может меняться между прогонами (#8, #45, #80). Gate оценивает текущий
   результат; отпечаток входов доказывает идентичность входов, а не ответа.
5. **`LEGAL_CLAIM` срабатывает на подстроку-слово** в составных keywords
   (`safety compliance`, `eac certification`): смысл keyword не
   анализируется.

## 12. История версий

| Версия | Дата | Изменение |
|---|---|---|
| gate-v1 | — | Базовая политика: валидация, grounding, бренды / логотипы, надписи, юридические утверждения, люди, редакционный риск, AI-генерация, ручная эскалация |
| gate-v1.1 | 26.09.2026 | Неясное присутствие людей (`unclear`) больше не отправляет на review — заметка `PEOPLE_INCIDENTAL` |
| gate-v1.2 | 26.09.2026 | Дети — всегда review; причина `PERSONAL_DOCUMENT` |
| gate-v1.3 | 28.09.2026 | Надпись — brand / legal не только по `brands`: `PROPER_NAME` (2+ слов Title Case) и `MIXED_CASE_NAME`; явное правило `CONFORMITY_MARK` для знаков соответствия |

## 13. Состояние каталога (29.09.2026)

129 объектов: `publication_approved` 106, `human_review` 22,
`source_invalid` 1 (#2). Причины `human_review`:

| Причина | Объекты |
|---|---|
| `TRADEMARK` / `TEXT_BRAND_OR_LEGAL` | #5, #8, #54, #73, #83, #85, #86, #89, #96, #109 |
| `MANUAL_ESCALATION` (однословный бренд) | #46, #48, #74, #80 |
| `LEGAL_CLAIM` | #85 (`certified`), #112 (`certification`), #67 (`official`), #68 (`official`, `compliance`) |
| `VALIDATION_ERRORS: BRAND_IN_TEXT` | #109 |
| `PEOPLE_RECOGNIZABLE` | #37, #38, #44; дети — #43, #47 |
| `PERSONAL_DOCUMENT` | #67, #68 |
