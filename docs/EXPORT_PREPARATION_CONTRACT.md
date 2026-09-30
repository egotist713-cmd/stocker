# STOCKER — КОНТРАКТ EXPORT PREPARATION (ФАЙЛ ПОД ПЛОЩАДКУ)

> **Статус:** 🟢 РЕАЛИЗОВАНО v1 (30.09.2026, паспорт §35ZZW) — `export-v1`, профиль
> `adobe-2026-09` (Adobe Stock); код `app/export_preparation.py`, тесты
> `tests/test_export_preparation.py`, `tests/test_export_metadata_contamination.py`.
> Профиль `shutterstock-2026-09` — реализован 30.09.2026 (§35ZZZG, §9a). Загрузка (FTP / API / CSV) —
> следующий этап. Проект контракта — 27.09.2026 (§35ZN); уточнения реализации — §9.
>
> Связанные документы: `FORMAT_CONTRACT.md` (normalization, факты об
> исходнике), `STOCK_READINESS_CONTRACT.md` (§3.4 `ready` / `ready_for_export`,
> §3.5 план экспорта, §3.5b original → derivative), паспорт §3A.11–3A.13.

---

## 1. Назначение и границы

Export preparation создаёт **финальный производный файл для загрузки** на
конкретную площадку. Он **не улучшает** изображение (это Enhancement) и **не
решает**, стоит ли кадр экспорта (это Readiness и человек) — он только
готовит файл по уже принятым решениям.

```text
original (только чтение)
   → processing history (события Stocker: QC, Enhancement, Vision, Metadata, Readiness, …)
   → derivative export file (отдельный файл под площадку)
```

Принципы:

1. **Оригинал никогда не изменяется.** Файл площадки строится из оригинала
   (или из утверждённого enhancement-derivative, §6) в новый файл.
2. **Связь derivative → original сохраняется** (`source_sha256`, цепочка
   derivative-ов) — во внутренней истории, не в экспортном файле.
3. **Внутренняя история Stocker никогда не смешивается с экспортным файлом**
   (§4).
4. **Площадки — через профили** (§5); ядро export preparation не знает правил
   конкретной площадки.
5. **Детерминированность:** те же входы и профиль → тот же файл (тот же hash).

---

## 2. Входы и статусы

| Вход | Источник |
|---|---|
| Оригинал | `assets.source_path`, SHA256 = `assets.file_hash` (проверяется перед экспортом) |
| Факты об исходнике | `NORMALIZE/PASSED` (`FORMAT_CONTRACT.md` §3.1): формат, профиль, HDR, встроенные данные, ориентация |
| План экспорта | последнее `READINESS/EVALUATED` площадки со статусом `ready` и **совпадающим fingerprint** (`STOCK_READINESS_CONTRACT.md` §3.5, §3.6) |
| Metadata площадки | `export_plan` Readiness: title / description, keywords, категории — **снимок** на момент подготовки |
| Профиль площадки | §5 (та же версия, что у профиля Readiness) |

Статусы (`STOCK_READINESS_CONTRACT.md` §3.4):

| Статус | Значит |
|---|---|
| `ready` | объект соответствует правилам площадки (Readiness) |
| **`ready_for_export`** | Export preparation создал и **проверил** файл площадки: sRGB, формат, размер, metadata, очистка — пакет экспорта готов |
| `stale` | после подготовки изменился вход (оригинал, metadata, план, профиль) — файл площадки нужно пересоздать |

Загрузка на площадку (будущий этап) — **только** из `ready_for_export`.

---

## 3. Шаги подготовки

| # | Шаг | Правило |
|---|---|---|
| 1 | Проверка входа | SHA256 оригинала = `assets.file_hash`; Readiness `ready` с совпадающим fingerprint; иначе отказ (`EXPORT/FAILED`, `SOURCE_CHANGED` / `NOT_READY` / `STALE`) |
| 2 | Декодирование | оригинал в полном разрешении; основной кадр; ориентация EXIF применена к пикселям |
| 3 | Цвет | в цветовое пространство профиля (Adobe / Shutterstock — **sRGB**), 8 бит; ICC профиля встраивается в файл |
| 4 | Размер | проверка MP профиля; `downscale_to_mp`, если больше максимума; **не увеличивать** (upscale — только решение Enhancement) |
| 5 | Кодирование | формат профиля (JPEG; TIFF — если профиль разрешает); качество JPEG — политика профиля (§5); `fit_file_size` снижает качество ступенями, но не ниже минимума профиля — иначе отказ |
| 6 | Встроенные данные | удаляются: видео Motion Photo, gain map Ultra HDR, карты глубины, миниатюры, вспомогательные изображения (`strip_embedded`) |
| 7 | Metadata | файл получает **только** разрешённые профилем записи (§4.2), собранные заново; ничего не копируется из оригинала «как есть» |
| 8 | Проверка результата | файл повторно разбирается: формат, размер в MP и байтах, цветовой профиль, ориентация, **список всех записей metadata** против белого списка профиля — любое лишнее поле → отказ |
| 9 | Учёт | `DERIVATIVE/CREATED` (§6) и статус `ready_for_export` |

Хранение: `data/export/<platform>/<asset_id>/` (runtime-данные, не в git); имя
файла — по §3.1.

### 3.1. Имя экспортного файла (решение пользователя 27.09.2026)

Имя файла — тоже часть подготовки экспорта. **Исходное имя —
только внутренний атрибут** (`assets.filename`, например
`PXL_20260927_143522847.jpg`): оно остаётся связанным с оригиналом внутри
Stocker и **не становится именем экспортного файла**.

```text
<slug утверждённого title>_<asset_id>.<расширение формата площадки>
industrial_electrical_control_panel_1234.jpg
```

| Правило | |
|---|---|
| Основа | slug из **утверждённого** title площадки (снимок `export_plan`): латиница в нижнем регистре, цифры, `_`; транслитерация и нормализация Unicode; без стоп-слов вида `photo`, `image`; ограничение длины (например, 60 символов основы) |
| Уникальность | стабильный идентификатор — `asset_id` |
| Расширение | формат площадки (`.jpg`, `.tif`) |
| Детерминированность | тот же title и asset → то же имя |

**Не допускается в имени экспортного файла** (проверка шага 8 — как для metadata):

- названия и модели камер, служебные шаблоны телефонов (`IMG_`, `PXL_`,
  `DSC`, `MVIMG`, `.MV`), даты съёмки (если площадка их не требует);
- названия программ обработки: `Topaz`, `Photoshop`, `PS`, `Lightroom`, `LR`,
  `Gigapixel`, `AI` как метка обработки;
- рабочие пометки: `edited`, `edit`, `final`, `final2`, `copy`, `v2`,
  `standard v2-1x`, номера версий;
- внутренние обозначения Stocker (кроме `asset_id` как суффикса уникальности).

| Плохо | Хорошо |
|---|---|
| `IMG_001_TopazAI.jpg`, `factory_photo_final_final2.jpg`, `DSC1234_edit_PS.jpg`, `222MVIMG_…-standard v2-1x.jpg` | `industrial_control_panel_1234.jpg` |

**AI отвечает за содержание** (title), **правила Export preparation — за
корректность выходного файла** (имя, формат, metadata).

Разные площадки — разные derivative-файлы (могут совпадать по имени, лежат в
разных каталогах); внутри Stocker это **один объект с одной историей**:

```text
asset 1234 — original: PXL_20260927_143522847.jpg
    adobe:        data/export/adobe/1234/industrial_electrical_control_panel_1234.jpg
    shutterstock: data/export/shutterstock/1234/industrial_electrical_control_panel_1234.jpg
```

---

## 4. Политика metadata

### 4.1. Два мира

| | Внутренняя история Stocker | Экспортный файл |
|---|---|---|
| Где | БД и события Stocker, файлы в `data/` | файл, который уходит на площадку |
| Что | оригинал; все этапы обработки; применённые инструменты (в т.ч. Topaz); версии моделей и промптов; результаты QC, Enhancement, Vision, Readiness, Creative Review; решения человека; actor | только технические параметры изображения, подготовленные metadata площадки (title / description, keywords, категории — если площадка читает их из файла), данные, требуемые площадкой |
| Кто видит | пользователь, агент, n8n (по правам) | площадка и покупатели |

**Внутренняя история никогда не попадает в экспортный файл.** Экспортный
файл не содержит ни одного идентификатора Stocker.

### 4.2. Белый список, а не чёрный

```text
SOURCE METADATA
     ↓  НЕ переносится автоматически (ни одно поле, ни один сегмент)
Stocker создаёт metadata заново
     ↓  platform-specific whitelist
NEW EXPORT METADATA
```

Metadata экспортного файла **собираются заново** по белому списку профиля
площадки — из оригинала **ничего** не копируется, включая поля, которые
«безобидны» или совпадают по смыслу. Правило **не** «удалить `Software`,
`CreatorTool` и ещё 20 известных полей»: завтра след другого инструмента
появится в новом namespace или сегменте и будет пропущен. Решение пользователя
28.09.2026 (паспорт §35ZQ).

| Разрешено (по профилю) | Источник |
|---|---|
| ICC-профиль **целевого** цветового пространства площадки (например, sRGB) | **назначается профилем площадки** (шаг 3), **не копируется** из source |
| IPTC / XMP: title (headline / object name), description (caption), keywords | снимок `export_plan` Readiness |
| Автор / авторские права | только если пользователь задал значение для профиля (§8) |
| Раскрытие происхождения контента (например, IPTC `digitalSourceType`) | **только если** профиль конкретной площадки требует его в файле — значение формируется Stocker по правилам площадки, а **не** переносится из source |

**ICC — не просто «мусор metadata».** Исходный ICC — внутренний факт
(`NORMALIZE`); он нужен для корректной конвертации цвета (шаг 3), но **в файл
площадки не переносится**: профиль площадки определяет целевое пространство и
встраивает соответствующий профиль. Если цвет source не объявлен
(`undeclared` / `uncalibrated`), конвертация невозможна без догадки →
`EXPORT/FAILED` `COLOR_SPACE_UNDECLARED` (`INTERNAL_IMAGE_REPRESENTATION_CONTRACT.md` §7).

**Provenance source** (`digitalSourceType`, `Software`, `CreatorTool`, `History`,
авторы, кредиты, чужие идентификаторы и URL) — **сохраняется внутри Stocker** и
может использоваться правилами Readiness / площадок; **наружу не выходит**, если
не входит в белый список профиля. Это не сокрытие: AI-контент публикуется на
соответствующих площадках; задача техническая — не переносить в новый файл чужие
идентификаторы, следы редакторов и историю обработки.

### 4.3. Никогда не попадает в экспортный файл

| Категория | Примеры полей |
|---|---|
| Местоположение | GPS (широта, долгота, высота, направление), адресные теги |
| Устройство | производитель, модель, серийный номер камеры и объектива, `MakerNotes`, внутренние ID устройства |
| Программы и обработка | `Software`, `ProcessingSoftware`, `xmp:CreatorTool`, `photoshop:History`, `xmpMM:History`, `DocumentID` / `InstanceID` / `OriginalDocumentID`, `DerivedFrom`, Photoshop IRB, названия и следы редакторов и AI-инструментов (Adobe Photoshop, Lightroom, Topaz Photo AI, Topaz Gigapixel и др.), строки вида «Image processed with …», «Software: …», «Editing history: …» |
| Служебные данные телефона | Google Camera / Motion Photo XMP (`GCamera:*`, `Container:*`), Ultra HDR (`hdrgm:*`), карты глубины, встроенные миниатюры |
| Идентификаторы Stocker | asset_id, пути, hash, версии, события, actor |
| Комментарии | `UserComment`, `ImageDescription` не из плана экспорта, `XPComment` |

Дата съёмки и параметры экспозиции (выдержка, диафрагма, ISO) **по умолчанию не
записываются**; включить их может только профиль площадки, если она их требует
(§8).

### 4.4. Проверка очистки

Шаг 8 выводит **полный перечень** записей metadata готового файла и сверяет его
с белым списком профиля. Результат — часть `DERIVATIVE/CREATED`
(`metadata_audit`: список полей, без значений GPS и т.п.). Любое поле вне
белого списка → `EXPORT/FAILED` (`METADATA_NOT_CLEAN`), файл не считается
готовым.

### 4.5. Regression fixture `export_metadata_contamination_case`

Сохранён заранее (`tests/fixtures/export_metadata_contamination.py`,
`tests/test_export_metadata_contamination.py`): синтетический JPEG с
**классами** загрязнения реального файла новой сотни — EXIF (описание,
программа, камера, серийный номер, автор, права, GPS), IPTC в APP13, XMP из
многих пространств имён (`xmp:CreatorTool`, `xmpMM:History` / `DocumentID` /
`OriginalDocumentID`, `stEvt:softwareAgent`, `photoshop:History`,
`dc:creator`, `xmpRights:WebStatement`, `plus:Licensor` / `LicensorURL` /
`DataMining`, `Iptc4xmpExt:DigitalSourceType`, чужой `AssetID`), ICC,
комментарий, C2PA / JUMBF, встроенное видео.

- `inventory(path)` перечисляет **всё** в файле, кроме пикселей;
- тест экспорта (включится с `export.prepare`): `inventory(export) ⊆ whitelist`
  профиля — проверяется **отсутствие всей неразрешённой исходной metadata**, а не
  нескольких известных тегов; ICC экспорта — профиль площадки, а не байты source.

---

## 5. Профили площадок (Export profile)

Профиль площадки — **один на Readiness и Export** (одна версия, например
`adobe-2026-09`): Readiness проверяет соответствие, Export готовит файл. Ядро
export preparation правил площадок не содержит.

| Параметр профиля | Adobe Stock | Shutterstock |
|---|---|---|
| Формат | JPEG | JPEG (TIFF допускается, не используется по умолчанию) |
| Цвет | sRGB, ICC встроен | sRGB, ICC встроен |
| Разрешение | 4–100 MP | ≥ 4 MP |
| Размер файла | ≤ 45 MB | ≤ 50 MB |
| Качество JPEG | по умолчанию 95; нижняя граница для `fit_file_size` — 90 (проверить при реализации) | то же |
| Metadata в файле | title, keywords (≤ 49) | description (≤ 150 символов — страницы 30.09.2026; было 2048), keywords (7–50) |
| Категории | не в файле — при загрузке / API / CSV | не в файле — при загрузке / API / CSV |
| Белый список metadata | ICC, IPTC / XMP title, keywords (+ автор / права — если задано) | ICC, IPTC / XMP description, keywords (+ автор / права — если задано) |
| Правила очистки | §4.3 полностью | §4.3 полностью |

**Проверить при реализации:** какие поля IPTC / XMP площадки читают при
загрузке; допустимы ли Ultra HDR JPEG; требования к автору / правам.

Новая площадка — новый профиль, без изменения ядра.

---

## 6. Processing history (внутренняя)

`DERIVATIVE/CREATED` — во внутренней истории, никогда в файле:

| Поле | Значение |
|---|---|
| `purpose` | `export` (или `enhancement` — будущий Topaz) |
| `platform`, `profile` | `adobe`, `adobe-2026-09` |
| `path`, `sha256`, `size_bytes`, `width`, `height` | готовый файл |
| `source_sha256`, `source_derivative_id` | оригинал; если файл строился из enhancement-derivative — его id (цепочка до оригинала) |
| `operations` | список с параметрами: `to_srgb`, `downscale_to_mp:N`, `encode_jpeg:q95`, `strip_embedded`, `strip_metadata`, `write_iptc` |
| `inputs_fingerprint` | fingerprint Readiness + версия профиля + версия нормализатора + snapshot metadata |
| `tools` | версии кодировщика / Pillow, нормализатора — для воспроизводимости |
| `metadata_audit` | перечень полей готового файла (§4.4) |

**Enhancement-derivative** (Topaz, будущее) может быть источником экспорта
только после повторных QC и Vision; в файл площадки о нём не попадает ничего —
он виден только во внутренней истории (`source_derivative_id`).

Идемпотентность: тот же `inputs_fingerprint` → существующий файл
(`UNCHANGED`), новый не создаётся.

---

## 7. Операции (будущие)

| Операция | Уровень | Описание |
|---|---|---|
| `export.prepare` | pipeline | `{asset_id, platform}` — один объект, одна площадка; без массовых вызовов (как Readiness) |
| `export.get` | read | статус `ready_for_export` / `stale`, файл, `metadata_audit` |

Загрузка (FTP / API площадок, CSV) — следующий этап, **не** часть Export
preparation. Решения человека (approve / reject) Export preparation не
принимает и не обходит.

---

## 8. Открытые вопросы

- Автор / авторские права в файле: значения и нужны ли площадкам — **открыт** (v1 не
  пишет; значение задаёт пользователь).
- ~~Качество JPEG по умолчанию и нижняя граница `fit_file_size`~~ — **закрыт** (v1): 95,
  нижняя граница 90; Adobe значения качества не задаёт (страницы 30.09.2026).
- Дата съёмки: нужна ли какой-либо площадке — **открыт** (по умолчанию не пишется).
- ~~Ultra HDR JPEG на площадках~~ — **закрыт для v1**: экспортируется обычный SDR JPEG
  (gain map / MPF не переносятся — пиксели основного кадра из AnalysisView).
- Какие поля IPTC / XMP читают площадки — **частично**: Adobe сохраняет встроенные title
  и keywords из Lightroom / Bridge / Photoshop (пишут XMP `dc:title` / `dc:subject`);
  v1 пишет только XMP IPTC Core, без IIM (APP13 = Photoshop IRB, §4.3). Подтвердить
  первой реальной загрузкой. Shutterstock: поддерживает встроенные titles / keywords (Bridge, Lightroom,
  Photo Mechanic), какие поля читает — не указано; v1 пишет XMP `dc:description` / `dc:subject` —
  подтвердить первой загрузкой.
- Provenance (`digitalSourceType`) в файл — **открыт**: Adobe отмечает генеративный AI
  при загрузке в портале, а не полем файла; v1 не пишет (§4.2).

---

## 9. Реализация v1 (30.09.2026, паспорт §35ZZW)

| Решение | Почему |
|---|---|
| Пиксели — `AnalysisView.full` (source → ориентация → цвет в sRGB, 8 бит) | единственный вход пикселей; основной кадр без Motion Photo / gain map; undeclared → `COLOR_SPACE_UNDECLARED` |
| Вход — актуальные Readiness `ready` **и** Publication `approved` площадки | `ASSET_STATE_CONTRACT.md` §2.2a: файл площадки — только из актуального approved; иначе `NOT_READY` |
| Title / keywords — снимок `export_plan` последнего `READINESS/EVALUATED` | §2; не из `ai_result` и не из `metadata_json` |
| ICC — фиксированный файл `app/profiles/srgb.icc` (sRGB, sha256 в коде) | профиль LittleCMS, сгенерированный заново, несёт дату — hash экспорта был бы разным |
| Metadata — XMP (`dc:title`, `dc:subject`) + ICC; белый список `segment:APP2:ICC`, `segment:APP1:XMP`, `xmp:dc:title`, `xmp:dc:subject` | IIM потребовал бы Photoshop IRB (APP13) |
| Имя — `<slug>_<asset_id>.jpg`, **не длиннее 30 символов с расширением** | CSV Adobe: Filename ≤ 30; пример «60 символов основы» из §3.1 заменён более строгим |
| Title: > 200 — отказ `TITLE_TOO_LONG_FOR_PROFILE`; > 70 и запятая — предупреждения `TITLE_LONG` / `TITLE_HAS_COMMA` | 70 и запятые — рекомендация / правило CSV; 200 — лимит портала (вторичные источники) и Gate |
| Keywords 7–49 → иначе `KEYWORDS_OUT_OF_RANGE` | 49 — Adobe (CSV допускает 50 — строже); 7 — как Gate |
| Файл: временный → fsync → проверка шага 8 → `os.replace` | непроверенный файл не остаётся в `data/export` |
| JPEG без `optimize` (4:4:4, baseline) | при `optimize` Pillow ограничивает буфер w×h байт — детальный кадр его превышает |
| `export.prepare` / `export.get` — только человек (`AGENT_FORBIDDEN`, не в allowlist n8n) | инструменты OpenClaw и права n8n не расширяются |
| Отказы — `EXPORT/FAILED` с кодом; дополнительно `RESOLUTION_TOO_LOW`, `FILE_TOO_LARGE`, `TITLE_EMPTY`, `VIEW_UNAVAILABLE`, `VERIFY_FAILED` | коды §3 + ограничения профиля |
| `check_consistency`: последний `DERIVATIVE/CREATED` без файла / с другим файлом — FAIL; файл без события — INFO | как у internal derivative |

### 9a. Профиль shutterstock-2026-09 (30.09.2026, паспорт §35ZZZG)

| Параметр | Значение | Источник / решение |
|---|---|---|
| Формат, цвет | JPEG (TIFF допускается), sRGB | «What are the technical requirements for images?» (19.02.2026) |
| Разрешение, размер | ≥ 4 MP, без верхнего предела для фото; ≤ 50 MB | там же; «How do I submit photos» |
| Текстовое поле | `description` → XMP `dc:description`, ≤ 150 символов, без обрезки → `DESCRIPTION_TOO_LONG_FOR_PROFILE` | «Preparing Your Uploaded Content» (10.02.2026): 150; Readiness / контракт — 2048 → **строже 150** |
| Короткое описание | < 5 слов → предупреждение `DESCRIPTION_SHORT` | как Readiness (`text_min_words`) |
| Keywords | 7–50 | «7-50 keywords» |
| Белый список | `segment:APP2:ICC`, `segment:APP1:XMP`, `xmp:dc:description`, `xmp:dc:subject` | dc:title не пишется |
| Имя файла | `<slug description>_<asset_id>.jpg` ≤ 30 символов, `data/export/shutterstock/<id>/` | лимит Shutterstock не найден — как Adobe; slug — из описания (в плане Shutterstock нет title) |

Ядро обобщено по текстовому полю профиля (`text_field`, `xmp_text`); новые параметры входят в
`spec()` только если отличаются от умолчаний Adobe — отпечатки существующих экспортов Adobe не
изменились (проверено: 10 production-объектов остались `ready_for_export`).
