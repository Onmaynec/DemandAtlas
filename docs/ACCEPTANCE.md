# Протокол приёмки PV-DATA-05

Статус: промежуточная реализация. Тестовый код не является подтверждением выполнения всех Must и эксплуатационных критериев.

| Код | Проверка | Наличие проверок / статус |
|---|---|---|
| AT-01 | Импорт 100 000, контрольная сумма | Нужен воспроизводимый замер на эталонном сервере; скрипт benchmark |
| AT-02 | Повтор batch | test_retry_idempotence_and_atomic_quality_gate |
| AT-03 | 100 ошибок SKU/qty, mart неизменен | Автотест 100 ошибок: tests/test_acceptance_coverage.py |
| AT-04 | Нет данных магазина 3 дня | Автотест 3 пропущенных дней: tests/test_acceptance_coverage.py |
| AT-05 | KPI маржи, формула и источники | /api/v1/metrics/catalog |
| AT-06 | Backtest трёх baseline | test_forecast_determinism_and_common_origins |
| AT-07 | WAPE при нулевом факте | test_wape_zero_actual_at07 |
| AT-08 | Одинаковый seed, одинаковый прогноз | test_forecast_determinism_and_common_origins |
| AT-09 | Заказ 25 → 30 | test_replenishment_acceptance_at09 |
| AT-10 | What-if не меняет прогноз и остатки | test_forecast_plan_and_approval_roles |
| AT-11 | Без права нельзя утвердить | test_forecast_plan_and_approval_roles |
| AT-12 | Lineage до batch | test_lineage_and_csv_role_protection |

Нужно отдельно измерить BI p95 ≤ 2 сек, throughput 100 000/5 мин, 3 млн фактов, восстановление БД, CI с PostgreSQL под конкуренцией и полное e2e браузера. До этих испытаний акт приёмки подписывать нельзя.

## Остаточные задачи

DAT-F-07: явная замена source/store/date реализована в v0.2.0; нужно подтвердить восстановление после сбоя и нагрузку.
DAT-F-12: XLSX-экспорт реализован в v0.3.0, scope/формулы проверяются интеграционными тестами.
DAT-F-24: детерминированный обзор рисков излишка/дефицита реализован в v0.3.0; не является вероятностной оценкой.
DAT-F-25: декомпозиция вклада KPI реализована в v0.3.0; арифметические вклады не обозначают причинность.
Производственное IAM, долговечная очередь/повтор и графические e2e входят в hardening.
