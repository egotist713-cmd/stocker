# STOCKER — КОНТРАКТ ВНУТРЕННЕГО ПРЕДСТАВЛЕНИЯ ИЗОБРАЖЕНИЯ (INTERNAL NORMALIZATION)

> **Статус:** 🟢 ПРИНЯТ (28.09.2026, паспорт §35ZQ) с решениями пользователя
> (§12). Контракт сформирован по реальным исходникам двух независимых наборов
> (§10), а не по предполагаемому списку форматов.
>
> Связанные документы: `FORMAT_CONTRACT.md` (факты об исходнике, шаг 1),
> `EXPORT_PREPARATION_CONTRACT.md` (файл площадки — **другая** стадия),
> паспорт §3A.11–3A.14.

---

## 1. Термины

| Термин | Что это | Где живёт | Меняется ли |
|---|---|---|---|
| **Immutable source** | исходный файл пользователя, зарегистрированный в Stocker | `assets.source_path`, SHA256 в `assets.file_hash` | **никогда** |
| **Source facts** | описание исходника (формат, цвет, битность, HDR, кадры, встроенные данные, metadata-признаки) | событие `NORMALIZE/EVALUATED` | пересчитываются при новой версии фактов |
| **Representation** | каноническое декодированное изображение объекта для всех внутренних стадий: **сам source** (pass-through) или **internal derivative** | манифест `NORMALIZE/PASSED` | пересоздаётся при новой версии normalizer |
| **Internal derivative** | внутренний производный файл без потерь — только когда source нельзя надёжно использовать напрямую (§3) | `data/internal/<asset_id>/<normalizer>/<sha256>.<ext>` | создаётся заново, никогда не редактируется |
| **Analysis view** | временное изображение для QC / Enhancement / Vision / Metadata / Creative Review (§4) | только в памяти | не хранится |
| **Platform derivative** | файл для Adobe / Shutterstock | Export preparation | **не** internal derivative (§9) |

---

## 2. Immutable source

- Stocker **никогда не изменяет, не перезаписывает и не перемещает** исходный
  файл. Все стадии открывают его только на чтение.
- Перед каждой стадией, читающей файл, SHA256 сверяется с `assets.file_hash`;
  расхождение → `SOURCE/INVALID` или `NORMALIZE/FAILED` (`SOURCE_CHANGED`).
- Встроенные данные source (видео Motion Photo, gain map, изображения MPF,
  карты глубины) **остаются в source** и описываются ссылками (смещение,
  размер) — их не нужно копировать, потому что source неизменен.

---

## 3. Internal derivative

### 3.1. Когда создаётся

| Source | Representation v1 | Почему |
|---|---|---|
| JPEG 8 бит (RGB, ICC / EXIF / без объявления цвета) | **source (pass-through)** | декодирование детерминировано, копия без потерь заняла бы 30–150 MB на объект без пользы |
| PNG 8 / 16 бит, TIFF | **source (pass-through)** | формат без потерь, читается напрямую |
| AVIF, HEIF / HEIC | **internal derivative** | нужен отдельный кодек; внешние инструменты и будущие стадии не должны зависеть от него |
| Прочее | `NORMALIZE/FAILED` (§7) | |

Правило пересматривается только через изменение контракта (например, если
понадобится кэш декодирования).

### 3.2. Что обязано сохраняться без потерь

| Свойство | Как сохраняется в representation |
|---|---|
| **Пиксели основного изображения** | pass-through — сам source; derivative — PNG без потерь (контейнер без сжатия с потерями) |
| **Orientation** | pixels derivative повёрнуты по EXIF (перестановка пикселей — без потерь); исходное значение — в манифесте (`orientation_raw`). Pass-through: source как есть, ориентация — в фактах |
| **Цветовой профиль** | ICC source встраивается в derivative **без изменений**; объявление nclx / cICP / EXIF — в манифесте. Конвертации цвета в representation **нет** |
| **HDR / Ultra HDR** | базовое изображение — как есть; gain map и HDR-метаданные — ссылкой на source (MPF-запись / смещение). PQ / HLG — битность и передаточная функция сохраняются (16-bit PNG + cICP) |
| **Bit depth** | 8 → 8; 10 / 12 / 16 → 16 бит (расширение без потерь); исходная битность — в манифесте |
| **Alpha** | сохраняется (RGBA / LA); признак `alpha_used` — в манифесте |
| **Frame count** | основной кадр — в representation; заявленные и читаемые кадры (`frames`, `mpf`) — в манифесте |
| **Motion Photo / auxiliary streams** | не копируются; ссылка на диапазон байтов source + признак целостности (например, MPF-запись внутри / за пределами файла) |
| **Связь derivative → source** | `source_sha256`, `asset_id`, `facts_fingerprint`, версия normalizer и параметры (§6) |

### 3.3. Манифест (`NORMALIZE/PASSED`)

```json
{
  "normalizer_version": "normalize-v1",
  "params_hash": "sha256:…",
  "fingerprint": "sha256:<source_sha256 + normalizer_version + params_hash>",
  "source_sha256": "…",
  "facts_fingerprint": "sha256:…",
  "representation": "source | derivative",
  "derivative": {"path": "data/internal/…/….png", "sha256": "…", "size_bytes": 0,
                 "width": 0, "height": 0, "mode": "RGB", "bit_depth": 8, "icc_embedded": true},
  "transforms": ["decode:avif", "orient:6→1"],
  "preserved": {"orientation_raw": 6, "color": {"source": "icc", "kind": "srgb"}, "bit_depth": 8,
                "alpha_used": false, "frames": {"declared": 1, "readable": 1},
                "references": {"motion_video": {"offset": 0, "size": 0}, "mpf": {"declared": 2, "within_file": 2}}},
  "decoder": {"pillow": "12.3.0"}
}
```

---

## 4. Analysis views — что допускается нормализовать для QC / AI

Views строятся **из representation в памяти**, не хранятся и не являются
файлами. Допустимые преобразования — только технические, одинаковые для всех
кадров:

| Преобразование | Правило |
|---|---|
| Ориентация | применяется |
| Цвет | **объявленный** (`color_profile.declared = true`) — в **sRGB** по ICC / объявлению (намерение и параметры — §6). **Не объявленный** (`undeclared`, `uncalibrated`) — **без цветового преобразования**: view несёт значения пикселей как есть и помечен `color_space: undeclared`; sRGB **не предполагается** (§7) |
| Битность | до 8 бит масштабированием (не обрезкой) |
| Альфа | наложение на фиксированный фон (белый) — **только во view**; `alpha_composited: true` в событии стадии |
| Размер | только **уменьшение** до размера view (`full` = исходное разрешение, `preview` = 2048, `overview` = 1536) фильтром LANCZOS; **никогда не увеличивать** |
| Кадр | основной |

### 4a. ICC: источник, representation, view — три разных утверждения (28.09.2026)

| Что | Где записано | Правило |
|---|---|---|
| ICC **источника** | факты `color_profile.icc` (`sha256`, `size`, `color_space`) — `normalize-facts-v4` | факт происхождения; не меняется |
| Цвет **representation** | манифест `NORMALIZE/PASSED` → `color` {`space`, `declared`, `icc_sha256`, `icc_is_source`, `pixels_converted`} | ICC описывает **фактические** пиксели файла. Пиксели не конвертированы → ICC источника байт в байт и пространство источника (сейчас всегда так: derivative AVIF → PNG без конвертации). Если derivative когда-нибудь конвертирует пиксели — он обязан нести профиль **своего** пространства, и ICC источника в нём быть не может |
| Цвет **view** | `AnalysisView.color_space` / `identity()` в событиях стадий | view в памяти **без ICC**; пространство — `srgb` (объявленный цвет, при необходимости конвертирован) или `undeclared`, независимо от профиля источника |

Инвариант `normalizer.check_color`: нельзя одновременно считать ICC источника
сохранённым байт в байт и пиксели — уже конвертированными (и наоборот:
неконвертированные пиксели с чужим ICC). Нарушение — ошибка кода, события
`PASSED` нет (регрессия: `tests/test_normalizer.py`).

Объявленный не-sRGB **без** ICC (DCF Adobe RGB, nclx, cICP, gAMA/cHRM) —
`NORMALIZE/FAILED` `COLOR_CONVERSION_UNSUPPORTED` уже в `plan()`: профиль не
подбирается. CMYK / Gray с ICC конвертируются в пространстве профиля.

### 4b. Граница Normalization

| Normalization делает | Normalization **не** делает |
|---|---|
| ориентацию | sharpening |
| интерпретацию и конвертацию цвета (для view) | denoise |
| representation / derivative | upscale |
| битность | Topaz |
| альфу (только во view) | баланс белого |
| кадры / потоки (выбор основного, факты) | кривые, контраст, любые творческие изменения |

---

## 5. Визуальные изменения: допустимо и запрещено

Normalization **не улучшает фото**. Визуальным изменением считается любое
преобразование, которое меняет содержимое изображения, а не его
техническое представление.

| Разрешено (только как §3–§4) | **Категорически запрещено на этапе Normalization** |
|---|---|
| декодирование, поворот по EXIF, перенос в контейнер без потерь, расширение битности, конвертация цвета **во view**, уменьшение **во view**, наложение альфы **во view** | улучшения любого рода; **sharpening**; **denoise**; **upscaling**; **Topaz** и любые AI-модели; **Creative Review**; кадрирование, ретушь, удаление объектов; кривые, уровни, автоэкспозиция, баланс белого, контраст, насыщенность; тональное отображение в representation; изменение содержимого изображения |

Enhancement, Creative Review и Topaz остаются отдельными стадиями со своими
событиями.

---

## 6. Версионирование

- `normalizer_version` (`normalize-v1`, …) — код правил; `params` — все
  параметры (размеры views, намерение цветопреобразования, фильтр, цвет фона
  для альфы, формат derivative); `params_hash` — hash `params`.
- `fingerprint` = SHA256 source + `normalizer_version` + `params_hash`. Смена
  любого → representation и зависящие стадии `stale`; то же входит в
  fingerprint Enhancement / Readiness при переходе на views.
- Версии декодеров (Pillow, libavif, libheif) пишутся в манифест для
  воспроизводимости.
- Факты (`normalize-facts-vN`) версионируются отдельно (`FORMAT_CONTRACT.md` §3.4).

---

## 7. Когда безопасная нормализация невозможна — `NORMALIZE/FAILED`

**Никаких fallback-догадок и молчаливых преобразований.** Каждый отказ — с
причиной; pipeline для объекта останавливается (с шага 2; на шаге 1 факты
только наблюдают).

| Случай | Поведение | Реальный пример |
|---|---|---|
| Источник изменён / отсутствует | `FAILED` `SOURCE_CHANGED` / `SOURCE_MISSING` | asset 2; 67, 68 (удалены пользователем) |
| Нет кодека (HEIF / HEIC сейчас) | `FAILED` `MISSING_CODEC` | — (синтетический тест) |
| Файл не декодируется | `FAILED` `DECODE_ERROR` | — |
| Неизвестный контейнер | `FAILED` `UNSUPPORTED_FORMAT` | — |
| CMYK без ICC | `FAILED` `COLOR_SPACE_UNDECLARED` — без профиля CMYK нельзя получить RGB-view, профиль не предполагается | — |
| HDR PQ / HLG | `FAILED` `UNSUPPORTED_HDR` — до отдельного правила тонального отображения для views | — (синтетический тест cICP PQ) |
| Больше одного **читаемого** кадра вне MPF (анимация, многостраничный TIFF) | `FAILED` `MULTI_FRAME_UNSUPPORTED` — какой кадр «фото», не угадывается | — |

**Явные правила — не догадки** (записываются в манифест, `FAILED` не дают):

| Случай | Правило | Реальный пример |
|---|---|---|
| Цвет **не объявлен вообще** (нет ICC, нет sRGB / cICP / nclx, нет EXIF ColorSpace) — `color_profile.kind = undeclared` | факты и representation **успешны** (файл читается без цветового преобразования); **sRGB не предполагается**: views без конвертации, `color_space: undeclared`; любая операция, которой нужно знать цветовое пространство (конвертация в sRGB для площадки, проверка цвета в Readiness), даёт `COLOR_SPACE_UNDECLARED` | **29 JPEG новой сотни** |
| EXIF ColorSpace = **Uncalibrated** без ICC и без DCF `R03` — `kind = uncalibrated` | как `undeclared`: не приравнивается к sRGB, без принудительной конвертации; цветозависимые операции — `COLOR_SPACE_UNDECLARED` | **1 файл новой сотни** |
| EXIF Orientation вне 1–8 (например, `0`) | без поворота (значение не определено стандартом); `orientation_raw` сохраняется | **18 снимков первого набора, 1 — новой** |
| MPF заявляет изображения за пределами файла (устаревший индекс) | основное изображение используется; вспомогательные записи не читаются; `mpf.within_file` в манифесте | **1 файл новой сотни (0 / 2)** |
| Альфа есть, но не используется | как непрозрачное; `alpha_used: false` | **14 PNG новой сотни** |
| Альфа используется | сохраняется в representation; во view — наложение на белый | **7 PNG новой сотни** |
| Повреждённый указатель IFD EXIF | факт `exif_damaged`, не сбой | — (в реальных наборах не найден) |

---

## 8. Atomicity

**result state + processing event = одна транзакция** (паспорт §3A.14). Для
derivative, которого нет в БД, порядок такой, чтобы событие никогда не
ссылалось на отсутствующий или недописанный файл:

1. derivative пишется во **временный файл** в том же каталоге;
2. SHA256 считается, файл сбрасывается на диск (`fsync`) и атомарно
   переименовывается в адресный путь `…/<sha256>.<ext>` (`os.replace`);
3. **одной транзакцией** пишется `NORMALIZE/PASSED` с манифестом (и любое
   связанное состояние объекта).

Сбой до шага 3 оставляет только временный или «осиротевший» файл без события —
безопасно, удаляется сборкой мусора. Событие без файла невозможно. Pass-through
не создаёт файлов — одно событие.

`scripts/check_consistency.py` дополняется: `NORMALIZE/PASSED` с отсутствующим
файлом или несовпадающим SHA256 → нарушение; файлы `data/internal` без события —
отчёт о сиротах. `tests/test_atomicity.py` включает normalization с первого
коммита engine.

---

## 9. Internal ≠ Platform

Internal derivative **никогда** не становится файлом площадки и не
загружается. Export preparation строит свой файл по своему контракту (цвет
площадки, формат, очистка metadata, имя файла); он может читать representation
как декодированный источник, но всегда создаёт отдельный platform derivative.

---

## 10. Проверка на реальных наборах (27.09.2026)

Два независимых набора, **не смешиваются** (`scripts/scan_source_facts.py`,
только чтение; исходники не изменялись — SHA256 до и после совпали у всех).

| Свойство | Существующий набор (129 объектов, телефоны Pixel / Honor) | Новый набор (`data/samples/2026-09-27`, 100 файлов: веб-изображения, выходы Topaz) | Только синтетика |
|---|---|---|---|
| Прочитано / отказ | 127 / 2 (`SOURCE_MISSING`) | 100 / 0 | |
| JPEG | 127 | 72 (1 — MPO с устаревшим индексом) | |
| PNG | — | **22** (все 8 бит, блок sRGB, RGBA 21) | 16 бит, gAMA без sRGB, cICP PQ |
| AVIF | — | **6** (8 бит, ICC + nclx sRGB, SDR) | кодирование |
| HEIF / HEIC | — | — | `MISSING_CODEC` |
| TIFF | — | — | 8 / 16 бит |
| sRGB | 109 (ICC 102, EXIF 7) | 69 (ICC 45, PNG-блок 22, EXIF 2) | |
| Display P3 | 18 | — | |
| ProPhoto RGB | — | **1** | |
| Цвет не объявлен | — | **29** | |
| Uncalibrated без ICC | — | **1** | |
| Ultra HDR (gain map) | 113 (MPF 2 / 2) | — | |
| PQ / HLG | — | — | cICP PQ |
| Битность | 8 — все | 8 — все | 16 (PNG, TIFF) |
| Альфа | — | 21 (используется 7) | |
| Orientation 1 / нет / 0 | 109 / — / **18** | 47 / 52 / **1** | 6 |
| Кадры читаемые / заявленные | 1 / 1 | 99 × 1 / 1; **1 × 1 / 2** (MPF за пределами файла) | |
| Motion Photo | 74 | — | |
| Provenance: IPTC `digitalSourceType` | 4 (`compositeWithTrainedAlgorithmicMedia`) | **57** (там же; Topaz Gigapixel) | |
| C2PA | — | — | |
| GPS | 106 | 3 | |

**Пробелы, найденные новой сотней и закрытые в фактах (`normalize-facts-v2`):**
битность из заголовков (а не из режима декодера); цвет PNG из блоков sRGB /
gAMA / cHRM / cICP и AVIF из `nclx` (у AVIF два блока `colr`); HDR по
передаточной функции; реальное использование альфы; заявленные и читаемые
кадры, проверка индекса MPF; сырая ориентация; EXIF ColorSpace без ICC;
безопасное чтение IFD (Pillow бросает `KeyError` при отсутствии Interop IFD);
метки происхождения. Ошибки, найденные и исправленные при сравнении наборов:
поиск маркера SOF / C2PA по байтам давал ложные значения — заменён разбором
сегментов JPEG.

**Вне Normalization, но важно:** 57 файлов новой сотни и 4 первого набора
**сами заявляют** IPTC `digitalSourceType = compositeWithTrainedAlgorithmicMedia`
(записано Topaz Gigapixel). Это **source provenance fact** — не решение
Normalization (§10a).

### 10a. Provenance и исходная metadata (решение пользователя 28.09.2026)

- **`digitalSourceType` ≠ `ai_generated = true`** и **≠ автоматический запрет**:
  по одному тегу неизвестно, какое изменение сделал инструмент. Normalization
  вокруг этого факта не строится; он сохраняется во внутренней истории
  (`provenance`) и доступен будущим правилам gate / Readiness / площадок.
- Исходная metadata (`digitalSourceType`, `Software`, `CreatorTool`, `History`,
  авторы, кредиты, чужие идентификаторы, URL, ICC) — **факты об исходнике внутри
  Stocker**; **в экспортный файл не переносится** (`EXPORT_PREPARATION_CONTRACT.md`
  §4: metadata создаются заново по белому списку площадки).
- Цель — техническая: не тащить в новый файл чужие идентификаторы, следы
  редакторов, исходные URL, старые авторские данные и историю обработки. AI-контент
  публикуется на соответствующих площадках, а не выдаётся за фотографию.

### 10b. Влияние на существующие стадии (при переходе на views) — ✅ выполнено 28.09.2026 (§14)

- **Readiness**: сейчас `COLOR_PROFILE_MISSING` — warning «считается sRGB»
  (читает только ICC). Нужно: брать цвет из фактов (`color_profile.declared`);
  `undeclared` / `uncalibrated` → blocker `COLOR_SPACE_UNDECLARED` (без
  предположения sRGB); объявленный через EXIF / PNG-блок / nclx — не «missing».
  Выполняется вместе с переходом стадий на views (engine, шаг 3).

---

## 11. Условия начала engine

- [x] контракт принят пользователем (28.09.2026);
- [x] новая сотня проверена фактами, существующий набор сохранён отдельно;
- [x] все реальные случаи отражены в фактах и тестах (`tests/test_normalization.py`);
- [x] каждый неоднозначный случай имеет явное поведение (`FAILED` или явное правило, §7);
- [x] решения пользователя по §12;
- [x] regression fixture экспорта (`export_metadata_contamination_case`) сохранён.

## 12. Решения пользователя (28.09.2026)

1. **Representation:** JPEG / PNG / TIFF — **source напрямую**; AVIF / HEIF /
   HEIC — внутренний lossless derivative. Source всегда immutable; связь
   source → derivative обязательна; internal derivative никогда не является
   export / platform derivative; огромные копии JPEG / PNG / TIFF не создаются.
2. **Цвет не объявлен** — `color_profile.kind = undeclared` (не sRGB); файл не
   получает `FAILED` автоматически; операции, которым нужно цветовое
   пространство, sRGB молча не принимают (`COLOR_SPACE_UNDECLARED`).
3. **Uncalibrated без ICC** — так же: `COLOR_SPACE_UNDECLARED`, не
   приравнивается к sRGB, без принудительной конвертации.
4. **HEIC / HEIF** — `pillow-heif` в runtime **не добавляется** ради
   гипотетических файлов; до реального HEIC-набора — `MISSING_CODEC` и
   синтетическое покрытие.

---

## 13. Реализация: `normalize-v1` (шаг 2, 28.09.2026, паспорт §35ZQ)

- `app/normalizer.py` — движок без БД: `plan(facts)` (чистое решение или
  `NormalizationRefused` с кодом §7), `build_derivative` (декодирование →
  поворот по EXIF → PNG без потерь, ICC source байт в байт, EXIF не
  переносится — ориентация не применится второй раз; запись `.<sha>.tmp` →
  `fsync` → `os.replace` на `<sha256>.png`), `open_view` (views §4 в памяти).
  `PARAMS` → `params_hash`; `fingerprint` = SHA256 source + версия + `params_hash`.
- `app/normalization.py::run_asset` — события: факты (`EVALUATED`), затем
  `PASSED` с манифестом (`normalizer_version`, `params_hash`, `fingerprint`,
  `source_sha256`, `facts_fingerprint`, `representation`, `derivative`
  {path, sha256, size, размеры, mode, icc_embedded}, `transforms`, `preserved`,
  `decoder`) или `FAILED` (`stage = engine`, код §7). Pass-through файлов не
  создаёт. `UNCHANGED` — тот же fingerprint **и** derivative на месте с тем же
  хешем; потерянный derivative пересоздаётся (адресное имя — тот же файл).
  `FAILED` идемпотентен: та же причина при тех же входах — без нового события.
- Worker: `NORMALIZE/FAILED` **останавливает** pipeline объекта (исход
  `NORMALIZE_FAILED`), QC и Vision не выполняются.
- Views: `undeclared` / `uncalibrated` — **без** конвертации (`color_space =
  undeclared`); объявленный не-sRGB с ICC → sRGB (perceptual); объявленный
  не-sRGB **без** ICC (DCF Adobe RGB, nclx P3, cICP, gAMA/cHRM) —
  `COLOR_CONVERSION_UNSUPPORTED` (профиль не подбирается).
- `check_consistency`: `NORMALIZE/PASSED` с отсутствующим / изменённым
  derivative — FAIL; файлы `data/internal` без события — INFO (сбой между
  файлом и событием: результата без события нет, следующий прогон
  переиспользует файл).

Шаг 3 (стадии на views, Readiness по фактам, AVIF в ingest) — §14.

---

## 14. AnalysisView — единый вход пикселей (шаг 3, 28.09.2026, паспорт §35ZR)

```
SOURCE / INTERNAL DERIVATIVE
        ↓
  open_asset_view()            app/analysis_view.py (пиксели — normalizer.open_view)
        ↓
  canonical AnalysisView       full · preview 2048 · overview 1536 (уменьшения того же full)
        ↓
  ┌─────┼──────────┬───────────────┐
  QC  Enhancement  Vision   (Creative Review — по запросу)
```

- **Контракт view:** ориентирован; 8 бит RGB; альфа на белом; `color_space`
  `srgb` (объявленный, при необходимости конвертирован) или `undeclared` (без
  конвертации); ICC и EXIF не несёт; только уменьшение. `identity()` —
  `view_version` (`analysis-view-v1`), `fingerprint`, representation, цвет,
  размер — пишется в события стадий (`AI/PASSED.view`, `ENHANCEMENT/ASSESSED.view`,
  `ENHANCEMENT/ADVISED.view`, `QC.metrics.view`, `CREATIVE_REVIEW/ADVISED.view`).
- **Только по действительному манифесту:** нет `NORMALIZE/PASSED` —
  `NORMALIZE_NOT_PASSED`; манифест устарел — `NORMALIZE_STALE`; файл
  representation отсутствует / изменён — `REPRESENTATION_INVALID`. Обратного
  пути «открыть source как есть» нет.
- **Один view на объект:** worker строит view один раз после Normalization и
  передаёт тот же объект QC, Enhancement (правила и советник) и Vision; нет
  view — `VIEW/FAILED`, исход `VIEW_UNAVAILABLE`, дальше не идём. Операции
  сервиса (`enhancement.assess`, `enhancement.advise`, `creative.review`) строят
  view так же — через `open_asset_view`.
- **Отпечатки стадий** включают `view_fingerprint` (representation + версия
  views): смена normalizer или кода views делает оценки stale.
- **Файл изображения декодируют только** `normalizer.py` (representation, views),
  `source_facts.py` (факты) и `ingest.py` (размеры при регистрации) —
  архитектурный тест `tests/test_analysis_view.py::test_no_stage_opens_image_files_itself`.
- **Качество JPEG** — факт источника (`facts.jpeg_quality`, таблицы
  квантования), а не свойство view.
- **Vision получает JPEG** варианта `preview` (раньше PNG отправлялись как PNG
  с альфой): одинаковый вход для всех форматов.
- **Readiness не использует AnalysisView:** факты источника + metadata + правила
  площадки + export plan (`STOCK_READINESS_CONTRACT.md`, readiness-v2).
- **AVIF принимается ingest** и проходит pipeline на своём lossless derivative;
  HEIC / HEIF — нет (`MISSING_CODEC`, без `pillow-heif`).
