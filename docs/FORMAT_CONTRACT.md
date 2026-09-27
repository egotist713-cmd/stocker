# STOCKER — КОНТРАКТ NORMALIZATION (ФОРМАТ-СЛОЙ): SOURCE → NORMALIZE

> Подготовка файла под площадку — отдельный контракт
> `docs/EXPORT_PREPARATION_CONTRACT.md`.

> **Статус:** 🟢 КОНТРАКТ ПРИНЯТ (27.09.2026, паспорт §35ZL). **Реализация не
> начинается** — контракта достаточно до завершения калибровки Enhancement и
> Creative Review. Версия нормализатора — `normalize-v1` (будущая).
>
> Решения пользователя: GPS **не удаляется из оригинала**; удаление GPS —
> **отдельная операция export preparation** (`strip_location`) для файла под
> площадку; Motion Photo и Ultra HDR учитываются **при будущем экспорте**,
> текущий pipeline не усложняется.
>
> Связанные документы: паспорт §3A.9–3A.11, §35ZJ;
> `STOCK_READINESS_CONTRACT.md` §3.5b (original → derivative → platform export).

---

## 1. Принципы

1. **Stocker не привязан к формату исходника.** Входные форматы: JPEG,
   HEIF/HEIC, AVIF, TIFF, PNG (WebP — кандидат, §7).
2. **Оригинал (`source`) никогда не изменяется** — только читается; SHA256 в
   `assets.file_hash` остаётся проверкой целостности.
3. **Анализ работает с нормализованным представлением**, а не с файлом
   напрямую: QC, Enhancement, Vision, Metadata, Readiness, Creative Review
   получают одинаково подготовленное изображение.
4. **Формат — ответственность слоя Export.** Конвертация HEIF/HEIC, AVIF, PNG,
   TIFF в JPEG выполняется **только при подготовке экспорта**, отдельным
   производным файлом, и никогда не заменяет оригинал.
5. **Нормализованное представление — кэш, а не истина:** воспроизводимо из
   оригинала и версии нормализатора, может быть удалено в любой момент.

```text
source (оригинал, только чтение)
   → normalize            представление для анализа + факты о файле
   → QC → Enhancement → Vision → Metadata → Readiness     (универсальные слои)
   → Export preparation   производный файл под требования площадки
   → platform derivative  (Adobe JPEG sRGB, Shutterstock JPEG sRGB, …)
```

```text
IMG_0001.HEIC
   ├── original                      (read only)
   ├── normalized preview            (кэш для анализа)
   ├── Adobe export JPEG sRGB        (derivative, purpose=export, platform=adobe)
   └── Shutterstock export JPEG sRGB (derivative, purpose=export, platform=shutterstock)
```

---

## 2. Текущее состояние (проверено 27.09.2026)

| Что | Сейчас | Нужно |
|---|---|---|
| Приём файлов (ingest) | `.jpg .jpeg .png .tif .tiff` | + `.heic .heif .avif` |
| Декодирование | Pillow 12.3: JPEG, PNG, TIFF, **AVIF, WebP** — да; **HEIC — нет** (нужен `pillow-heif`, новая зависимость) | один нормализатор |
| Где открывается файл | **каждая стадия сама**: QC (`qc.check_asset`), Enhancement (`read_metrics`), Vision (`_prepare_image`), Creative Review (`overview`), Readiness (`read_file_facts`) | одна точка — `normalize` |
| Ориентация EXIF | учитывают только Vision и Creative Review | в нормализаторе, для всех |
| Цветовой профиль | анализ игнорирует ICC (P3-пиксели идут в модель как sRGB); Readiness только определяет профиль | анализ — в sRGB; профиль исходника — факт |
| TIFF → JPEG | только в памяти внутри Vision | в нормализаторе (для анализа) и в Export (для площадки) |
| Размер файла в Readiness | размер **всего файла** | размер изображения без встроенного видео; итог — по derivative |

**Факты по отобранным фотографиям пользователя** (`data/incoming`, 109 JPEG):

| Особенность | Файлов | Следствие |
|---|---|---|
| Motion Photo — встроенное видео MP4 (`*.MV.jpg`) | **69** (медиана **29 %** размера файла) | не участвует в анализе; **удаляется при экспорте**; `FILE_TOO_LARGE` сейчас завышен |
| GPS-координаты в EXIF | **95** | **удаляются при экспорте по умолчанию** (приватность) |
| Ultra HDR — карта усиления (gain map) | **106** | анализ — по базовому SDR-изображению; в export — удаляется (обычный SDR JPEG), пока площадки не подтвердят поддержку |

---

## 3. Normalize

### 3.1. Факты об исходнике (`source facts`)

Нормализатор один раз разбирает оригинал и фиксирует факты (событие §3.4):

| Факт | Пример |
|---|---|
| `container`, `codec` | `JPEG`, `HEIF/HEVC`, `AVIF/AV1`, `TIFF/LZW`, `PNG` |
| `width`, `height` | **после** применения ориентации EXIF |
| `orientation` | EXIF 1–8 |
| `bit_depth` | 8, 10, 12, 16 |
| `color_mode` | `RGB` / `L` (оттенки серого) / `CMYK` / с альфой |
| **v2 (27.09.2026)** | `bit_depth` — из заголовков (JPEG SOF, PNG IHDR, AVIF `pixi`, TIFF 258), не из декодера; `color_profile.source` — `icc` / `png_srgb_chunk` / `png_cicp` / `png_gama_chrm` / `nclx` / `exif_colorspace` / `exif_dcf`; `alpha_used`; `frames {declared, readable}`; `mpf {declared, within_file}`; `orientation_raw`; `software` (история инструментов); `provenance {digital_source_type, c2pa}`; `metadata_present.exif_damaged`. Правила представления — `INTERNAL_IMAGE_REPRESENTATION_CONTRACT.md` |
| `color_profile` | `srgb` / `display_p3` / `adobe_rgb` / `other` / `missing` + описание (определение по primaries — как в Readiness) |
| `hdr` | `none` / `gain_map` (Ultra HDR) / `pq` / `hlg` |
| `has_alpha` | bool |
| `frames` | число кадров (HEIC-последовательность, многостраничный TIFF, анимированный AVIF/PNG) |
| `embedded` | `motion_video` (размер), `depth_map`, `gain_map` |
| `metadata_present` | `exif`, `gps`, `xmp`, `iptc` — только **наличие**, без значений (GPS не сохраняется) |
| `file_size`, `image_payload_size` | размер файла и размер изображения без встроенного видео |

### 3.2. Нормализованное представление (для анализа)

| Свойство | Правило |
|---|---|
| Кадр | основной (primary) кадр; остальные кадры — только факт `frames` |
| Ориентация | применена |
| Пиксели | 8 бит на канал, RGB |
| Цвет | **объявленный** — в sRGB по ICC; **не объявленный** (`undeclared` / `uncalibrated`) — без преобразования, `color_space: undeclared`, sRGB **не предполагается** (решение 28.09.2026; `INTERNAL_IMAGE_REPRESENTATION_CONTRACT.md` §4, §14) |
| Альфа-канал | сведён на белый фон; факт `has_alpha` |
| HDR | базовое SDR-изображение (gain map не применяется); PQ/HLG — тональное отображение в SDR (будущее, §7) |
| Встроенные данные | видео, карты глубины, gain map — игнорируются |
| Варианты | `full` (исходное разрешение — метрики QC/Enhancement 100 %), `preview` (длинная сторона 2048 — Vision, Metadata), `overview` (1536 — Creative Review, советник Enhancement) |

Стадии анализа **не открывают исходный файл сами** — получают представление от
нормализатора (одна точка декодирования, одинаковые пиксели для всех).

### 3.3. Хранение

v1 — в памяти процесса (как сейчас). Кэш на диске
(`data/normalized/<asset_id>/<normalize-version>/<variant>.jpg`) — только если
понадобится по производительности; всегда удаляем и воспроизводим.

### 3.4. События

| stage | status | message |
|---|---|---|
| `NORMALIZE` | `EVALUATED` | **шаг 1 (реализован 27.09.2026)**: `facts_version` (`normalize-facts-v1`), `fingerprint` (hash файла + версия), `facts` (§3.1) |
| `NORMALIZE` | `PASSED` | **шаг 2 (реализован 28.09.2026, `normalize-v1`)**: манифест representation — `source` или internal derivative (`INTERNAL_IMAGE_REPRESENTATION_CONTRACT.md` §13) |
| `NORMALIZE` | `FAILED` | `error_type` (`UNSUPPORTED_FORMAT`, `DECODE_ERROR`, `MISSING_CODEC`, `SOURCE_MISSING`, `SOURCE_CHANGED`; engine: `UNSUPPORTED_HDR`, `UNSUPPORTED_BIT_DEPTH`, `MULTI_FRAME_UNSUPPORTED`, `COLOR_SPACE_UNDECLARED`), `error`, `fingerprint`; engine — `stage = engine`. Повтор той же причины события не пишет. **С шага 2 останавливает pipeline объекта** |

**Шаг 1 (реализован, паспорт §35ZO):** `app/source_facts.py` (только чтение) +
`app/normalization.py` (события), операции `normalize.evaluate` / `normalize.get`,
вызов в worker **перед QC**; `pipeline.normalize` в `asset.get`. На шаге 1
`NORMALIZE/FAILED` **не останавливает** pipeline (наблюдение); остановка — с шага 2,
когда анализ перейдёт на нормализованное представление. Идемпотентность — по
hash файла и версии фактов. Значения GPS, серийных номеров, модели камеры, имени
программы **не сохраняются** — только признак наличия (`metadata_present`).
Motion Photo определяется по реальному MP4-блоку, а не по пометке XMP (asset 3:
XMP заявляет Motion Photo, видео нет).

Первый сбор фактов (129 объектов): JPEG 126; sRGB 102, Display P3 17, без ICC 7;
Ultra HDR 113; Motion Photo 74; GPS 106; тег Software 112; отказы — 3 (исходник
изменён / удалён).

(`SOURCE/NORMALIZED` уже занято нормализацией **пути** `source_path` — это
другое событие.)

---

### 3.5. Особые случаи источника (27.09.2026)

| Случай | Normalize (анализ) | Факт (§3.1) | Export (см. `EXPORT_PREPARATION_CONTRACT.md`) |
|---|---|---|---|
| **Цветовые профили** (Display P3, Adobe RGB, ProPhoto) | ICC → sRGB, намерение `perceptual` (для анализа важно правдоподобие цвета, а не точность) | `color_profile` + описание | `to_srgb`, намерение и точность — профиль площадки |
| **Нет ICC** | объявлен иначе (EXIF sRGB, PNG sRGB, nclx) — по объявлению; не объявлен — без преобразования (не sRGB) | `kind` по объявлению или `undeclared` | не объявлен — Readiness blocker `COLOR_SPACE_UNDECLARED` (28.09.2026) |
| **CMYK** (TIFF, JPEG из полиграфии) | CMYK → sRGB через ICC; **без ICC — `NORMALIZE/FAILED` `COLOR_SPACE_UNDECLARED`** (профиль не предполагается; уточнено 27.09.2026) | `color_mode=CMYK` | в sRGB; CMYK площадкам не отдаётся |
| **Цвет не объявлен** (нет ICC, sRGB / cICP / nclx, EXIF ColorSpace) | **не** `FAILED`; view без цветового преобразования, `color_space: undeclared` (решение 28.09.2026) | `color_profile.kind=undeclared`, `declared=false` | Readiness blocker `COLOR_SPACE_UNDECLARED` (площадке нужен sRGB) |
| **EXIF ColorSpace = Uncalibrated без ICC** | как «не объявлен» (решение 28.09.2026); DCF `R03` → Adobe RGB объявлен, но без ICC — `NORMALIZE/FAILED` `COLOR_CONVERSION_UNSUPPORTED` | `kind=uncalibrated` / `adobe_rgb` | `uncalibrated` — Readiness blocker `COLOR_SPACE_UNDECLARED` |
| **16 бит** (TIFF, PNG), 10/12 бит (HEIC, AVIF) | в 8 бит (масштабирование, не обрезка) | `bit_depth` | 8 бит JPEG |
| **Оттенки серого** | в RGB | `color_mode=L` | JPEG RGB или grayscale — по профилю площадки |
| **Альфа-канал** (PNG) | сведение на белый фон | `has_alpha` | фото площадкам — без прозрачности |
| **Ultra HDR JPEG** (gain map) | базовое SDR-изображение; gain map не применяется | `hdr=gain_map` | gain map удаляется (`strip_embedded`) — обычный SDR JPEG, пока площадки не подтвердят поддержку |
| **HDR PQ / HLG** (HEIC, AVIF) | тональное отображение в SDR (будущее; до реализации — `NORMALIZE/FAILED` с `UNSUPPORTED_HDR`, а не неверные цвета) | `hdr=pq` / `hlg` | SDR JPEG после тонального отображения |
| **Motion Photo** (видео в JPEG / HEIC) | только кадр изображения | `embedded.motion_video` + размер; `image_payload_size` | видео удаляется (`strip_embedded`) |
| **Вспомогательные изображения HEIC** (depth, matte, thumbnail) | игнорируются | `embedded` | не экспортируются |
| **Много кадров** (HEIC-последовательность, многостраничный TIFF, анимированный AVIF / PNG) | основной (primary) кадр | `frames` | основной кадр |
| **Ориентация EXIF** | применяется к пикселям | `orientation` | пиксели уже повёрнуты; тег ориентации в файле площадки = 1 или отсутствует |

Правило: **нормализатор не угадывает** — если случай не поддержан
(неизвестный кодек, HDR без тонального отображения), он пишет
`NORMALIZE/FAILED` с понятной причиной, а не отдаёт анализу неверную картинку.

---

## 4. Export preparation и platform derivative

Вынесено в отдельный контракт **`docs/EXPORT_PREPARATION_CONTRACT.md`**
(27.09.2026): export preparation строит файл площадки **из оригинала**, а
не из нормализованного представления; normalization и export используют одни и
те же факты об исходнике (§3.1).

---

## 5. Влияние на существующие контракты (при реализации)

| Где | Изменение |
|---|---|
| ingest | принимает `.heic .heif .avif`; проверка «файл — изображение» через нормализатор |
| QC | работает по представлению `full`; `RESOLUTION_*` — по размерам после ориентации |
| Enhancement | метрики — по `full` (уже sRGB, ориентация применена); fingerprint += `normalizer_version` |
| Vision / Metadata AI / Creative Review | `preview` / `overview` из нормализатора вместо своих `_prepare_image` / `overview` |
| Readiness | факты формата из `NORMALIZE/PASSED`; операции `strip_embedded`, `strip_location`; размер по `image_payload_size` |
| fingerprints | включают `normalizer_version`: смена нормализатора → оценки `stale` |

---

## 6. Порядок реализации (когда пользователь решит)

1. Факты об исходнике (только чтение) + `NORMALIZE/PASSED|FAILED` — без
   изменения анализа.
2. Единый `normalize()` для всех стадий (замена пяти мест открытия файла) —
   регрессия на выборках 26.09 и `incoming`.
3. Анализ в sRGB (ICC → sRGB).
4. HEIC/HEIF — зависимость `pillow-heif` (установка — с подтверждения
   пользователя); AVIF — Pillow уже умеет.
5. Export preparation: derivative-файлы, `strip_embedded`, `strip_location`.

## 7. Открытые вопросы

- ~~Удаление GPS~~ — решено 27.09.2026: оригинал не меняется; `strip_location` —
  отдельная операция export preparation. Включать ли её для площадки по
  умолчанию — решается при реализации Export.
- HDR-исходники (PQ/HLG в HEIC/AVIF): тональное отображение для анализа и
  экспорта.
- WebP на входе (веб-контент): Pillow поддерживает; включать ли в список.
- Поддерживают ли площадки Ultra HDR JPEG с gain map — проверить при Export.
