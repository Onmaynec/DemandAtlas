# Эксплуатация и безопасность DemandAtlas v0.4.0

## Режимы

Локальная демонстрация: APP_ENV=local и DEMO_MODE=1. Только в этом случае доступны demo-engineer, demo-analyst, demo-category, demo-director и demo-observer. Они предназначены только для синтетических данных. Демонстрационные пароли/токены в .env.example нельзя использовать в публичной сети.

Рабочая конфигурация: APP_ENV=production и DEMO_MODE=0. В этом режиме приложение при запуске *откажется работать* со статическими TOKEN_* или стандартным FORECAST_SERVICE_TOKEN. Но штатный compose.yml всё ещё использует учебную конфигурацию PostgreSQL, поэтому требуется самостоятельно заменить пароль БД, service secret, настроить TLS reverse proxy, изоляцию сети, запуск без привилегий и безопасное управление .env до публикации в сети.

Убрать из .env переменные TOKEN_ENGINEER/ANALYST/CATEGORY/DIRECTOR/OBSERVER. Задать APP_ENV=production, DEMO_MODE=0 и криптографически стойкий FORECAST_SERVICE_TOKEN, пароль к PostgreSQL должен совпадать с заменённым в compose.yml.

## Первый администратор

После применения Alembic миграций, если ещё нет учётных записей:

    docker compose exec api python -m backend.scripts.manage_access bootstrap --name operator

Команда возвращает Bearer-ключ только один раз. Переместите его в менеджер паролей, не добавляйте в shell history, чат, конфигурацию или CI output. **Не запускайте bootstrap повторно** — он разрешён только на пустом реестре identities. Ключ действует 90 дней по умолчанию (параметр --expires-in-days).

Через веб-форму «Управление доступом» или API:
- POST /api/v1/admin/users — создать имя/роль, список магазинов и категорий, сразу выпустить ключ;
- PATCH /api/v1/admin/users/{id} — сменить роль, разрешённые магазины/категории, заблокировать пользователя;
- GET /api/v1/admin/users и GET /api/v1/admin/users/{id}/keys — показать учётные записи и метаданные ключей, *без* секретов;
- POST /api/v1/admin/users/{id}/keys — выдать дополнительный ключ;
- POST /api/v1/admin/keys/{id}/rotate — атомарно отозвать старый ключ и выдать новый;
- POST /api/v1/admin/keys/{id}/revoke — отозвать ключ;
- GET /api/v1/admin/audit — последние изменения API.

Допустимые роли: access_admin (управление доступом, без бизнес-данных), data_engineer (источники/ETL), analyst, category_manager (обязателен список категорий), director, observer (обязателен список магазинов, просмотр). Пустая категория/магазины для analyst означает unrestricted: назначайте scope осознанно. Все ограничения проверяются сервером.

## Восстановление доступа при утрате всех ключей

Если все административные ключи **утратили срок действия или отозваны**, локальный администратор сервера с доступом к базе может восстановить доступ для уже существующего access_admin:

    docker compose exec api python -m backend.scripts.manage_access recover --name operator

Операция невозможна, пока остаётся хотя бы один действующий ключ администратора. Она записывает отдельное событие CLI в audit и печатает новый секрет только один раз. Параметр --expires-in-days задаёт срок действия (по умолчанию 30). Команда не создаёт нового администратора и не доступна через HTTP. Храните доступ к Docker/БД под отдельным контролем.

## Аудит

Хранятся актор/роль, метод, маршрут без query string, статус HTTP, requestId и время. Не хранятся Bearer-токены и тело/результат запросов. Изменения users и keys записываются в audit в *той же транзакции*; остальные бизнес-мутации пишутся после ответа. **Ограничение:** отдельный сбой post-response аудита не отменяет уже завершённую бизнес-операцию. В логах появляется ошибка Audit persistence failed; до аттестации end-to-end trail и отказоустойчивого writer нельзя считать этот журнал полным.

## Ограничения запросов и rate limiting

Атомарный RateLimitWindow на PostgreSQL обеспечивает одинаковый минутный лимит независимо от числа API реплик. Учитываются IP, полученный из транспорта, и хеш заголовка Authorization; переменная RATE_LIMIT_PER_MINUTE (по умолчанию 240). Периодические устаревшие окна удаляются worker. Перед reverse proxy настройте trusted proxy и правила исходных адресов: заголовкам X-Forwarded-For по умолчанию *не доверяем*. За NAT многие пользователи могут разделять IP-лимит.

MAX_REQUEST_BYTES (по умолчанию 40 МиБ) ограничивает тело POST/PUT/PATCH, включая chunked загрузки. Дополнительно выставьте клиентский лимит на ingress/reverse proxy и ограничения числа запросов.

## Резервное копирование

    docker compose -f compose.yml exec -T postgres pg_dump -U demand -d demandatlas -Fc > demandatlas.dump

Для восстановления в ОСТАНОВЛЕННУЮ систему:

    cat demandatlas.dump | docker compose -f compose.yml exec -T postgres pg_restore -U demand -d demandatlas --clean --if-exists

Не считайте backup проверенным, пока не восстановили копию в отдельной тестовой БД. Остановите worker и API перед restore. Нет полностью автоматизированных chaos/fault-injection тестов восстановления.

## Диагностика

    docker compose ps
    docker compose logs -f api worker forecast

GET /health — проверка процесса, но не готовности к чтению БД. При ошибках API используйте requestId. Нормальный HTTP ответ не гарантирует успешного сохранения post-response audit; проверяйте лог и /admin/audit.
