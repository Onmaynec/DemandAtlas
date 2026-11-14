# DemandAtlas

Локальная платформа аналитики розничных продаж, прогнозирования спроса и подготовки закупок. Разработка по ТЗ PV-DATA-05 (синтетический заказчик).

В демонстрации используются только синтетические данные: 25 магазинов, 5 000 SKU и история до 24 месяцев. Реальные заказы поставщикам приложение не отправляет.

## Быстрый запуск

Необходимы Docker Engine и Docker Compose v2. Команды из корня репозитория:

    cp .env.example .env
    docker compose -f compose.yml up --build -d
    docker compose -f compose.yml exec api python -m backend.scripts.seed_demo --rows 2500 --seed 42

Откройте http://localhost:8000. OpenAPI: http://localhost:8000/docs.

### Демонстрационные роли

| Роль | Bearer-токен | Доступ |
| --- | --- | --- |
| Data engineer | demo-engineer | Источники, импорты, качество |
| Аналитик | demo-analyst | BI, модели, прогноз, черновики |
| Категорийный менеджер | demo-category | Категория Food |
| Руководитель | demo-director | BI, сценарии, утверждение |
| Наблюдатель | demo-observer | Только отчёты S001–S002 |

Вставьте токен в поле «Ключ доступа» вверху веб-интерфейса.

**Демо-токены и пароль PostgreSQL не предназначены для внешней сети и настоящих данных.** API проброшен на 127.0.0.1. Production-развёртывание требует полноценного IAM, TLS и управления секретами. См. docs/SECURITY.md.

## Демонстрационный сценарий

1. С токеном аналитика откройте «Обзор»: статистика и вклад магазинов в изменение выручки.
2. С токеном инженера в «Источниках» загрузите demo/invalid_sales.csv для demo_sales. Импорт отклоняется, причины с номерами строк доступны в журнале качества; старая витрина остаётся.
3. С токеном аналитика откройте «Прогнозирование», выберите S001 / SKU00001 и 28 дней. Backtest сравнит seasonal naive, moving average, exponential smoothing и zero baseline. Опубликуйте выбранный прогноз.
4. В «Закупках» рассчитайте сценарий, создайте план и отправьте на согласование. С токеном директора утвердите его.
5. Запросите происхождение дневной метрики через GET /api/v1/lineage с параметрами day, store, sku.

## Пример расчёта через API

    curl -X POST http://localhost:8000/api/v1/replenishment/scenario \
      -H "Authorization: Bearer demo-analyst" -H "Content-Type: application/json" \
      -d '{"storeId":"S001","sku":"SKU00001","forecastQty":35,"onHand":12,"inbound":8,"safetyStock":10,"leadDays":7,"reviewDays":0}'

Для импорта используйте POST /api/v1/pipelines/{id}/runs, передайте idempotencyKey и ровно одно поле rows либо csv. Предел: 100 000 строк на пакет и CSV до 35 МиБ.

## Тестирование

В Python 3.12:

    python -m venv .venv
    pip install -r backend/requirements.txt
    export DATABASE_URL=sqlite:///./demandatlas.db
    export DEMO_MODE=1
    export FORECAST_URL=
    pytest -q tests
    ruff check backend forecast_service tests --select E4,E7,E9,F63,F7,F82

На PostgreSQL миграции запускаются командой:

    alembic -c backend/alembic.ini upgrade head

Нагрузочная проверка (после seed):

    docker compose -f compose.yml exec api python -m backend.scripts.benchmark --rows 100000

Публиковать измерения производительности без указания железа, профиля данных и p50/p95 нельзя.

## Структура

- backend/app: FastAPI, роли, ETL raw → staging → mart, метрики, закупки;
- backend/migrations: миграция структуры БД;
- backend/scripts: синтетический seed и измеритель нагрузки;
- forecast_service: сервис четырёх baseline моделей;
- web: адаптивный интерфейс без внешних платных компонентов;
- tests: проверки расчётов, прав, качества и состояний;
- docs: архитектура, ADR, эксплуатация, приёмка.

## Статус реализации

Базовые потоки реализованы в исходниках: CSV/SQL ingestion, проверки качества, атомарный mart, idempotency, BI и CSV export, lineage, прогноз и rolling-origin, what-if, MOQ, план с версионированными переходами, серверная авторизация, Compose, CI.

**Не завершено / не подтверждено:** требуемое время обработки 100 000 строк, p95 BI, устойчивость при 3 млн строк под конкурирующей нагрузкой, полное e2e браузера и восстановление из сбоя, production IAM, XLSX, автоматическое отслеживание полного календарного охвата, обработка исправлений уже опубликованных чеков. Полный перечень в docs/ACCEPTANCE.md.

Лицензия — MIT.
