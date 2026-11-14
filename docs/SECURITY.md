# Эксплуатация и безопасность

Демонстрационный режим не подходит для внешнего доступа. Файл .env не коммитится; измените все токены, пароль БД и FORECAST_SERVICE_TOKEN до любой иной установки.

Перед production требуется: аутентификация с ротацией/отзывом токенов, постоянное хранение правил доступа, HTTPS, секрет-хранилище, ограничение размера входных HTTP-запросов на reverse proxy, распределённый rate limiting, очередь заданий с lease, нагрузочные испытания PostgreSQL и аудит доступа.

Текущий in-memory rate limiter действует только в одном экземпляре API. SQL-коннектор не принимает произвольные SQL-запросы, доступен лишь инженеру. CSV не принимает пользовательский путь к серверному файлу. Bearer API не использует cookie-сессии.

## Резервное копирование

    docker compose -f compose.yml exec -T postgres pg_dump -U demand -d demandatlas -Fc > demandatlas.dump

Восстановление в остановленную систему (внимание: перезаписывает существующие объекты):

    cat demandatlas.dump | docker compose -f compose.yml exec -T postgres pg_restore -U demand -d demandatlas --clean --if-exists

Проверяйте копию в изолированной тестовой БД до настоящего восстановления. Остановите worker и API перед restore.

## Диагностика

    docker compose -f compose.yml ps
    docker compose -f compose.yml logs -f api worker forecast

API health — GET /health. Для подробного протокола качества используйте /api/v1/quality/runs/{runId}. Ответы API сопровождаются requestId. Health не считается полноценным тестом доступности базы.
