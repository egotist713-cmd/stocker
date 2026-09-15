# Stocker

Локальная автоматизация обработки и подготовки контента для фотостоков.

## Архитектура

- Python — основное локальное ядро
- SQLite — состояние обработки
- Pillow / OpenCV — технический QC
- Topaz Gigapixel — обработка изображений
- Cloud multimodal LLM — коммерческая оценка и metadata
- ExifTool — metadata / EXIF
- OpenClaw — оркестрация
- n8n — дополнительные workflow при необходимости
- Adobe Stock — публикация
- Shutterstock — публикация

## Структура

- app/ — основной код
- config/ — конфигурация
- data/incoming/ — новые фотографии
- data/working/ — фотографии в обработке
- data/approved/ — прошедшие обработку
- data/rejected/ — отклонённые
- logs/ — журналы
- scripts/ — служебные скрипты
- tests/ — тесты

## Python

Используется Python 3.12 и отдельное виртуальное окружение .venv.

## Статус

MVP находится в разработке.
