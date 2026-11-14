# Retail Intelligence — расчёты и ограничения

## 1. Сравнение KPI

GET /api/v1/insights/contributions

Параметры: start, end (включительно), metric=revenue|units|margin, group_by=store|region|category|sku, необязательные store, region, category, sku, limit=1–200. Диапазон не больше 366 дней; предыдущий период — такой же длины непосредственно перед текущим.

Все данные берутся из опубликованного daily_sales_mart, соединённого с products и stores. До агрегации действуют ограничения Bearer-роли: допускаемые магазины и категории. Количество измерений ограничено 10 000, при превышении сервер возвращает 413 с предложением сузить фильтры.

Формулы:
- revenue = SUM(revenue).
- units = SUM(units).
- margin = SUM(revenue - cost).
- delta = current - previous.
- shareOfChangePct = 100 * dimensionDelta / totalDelta (только если totalDelta != 0).
- percentChange = 100 * totalDelta / previousTotal (только если previousTotal != 0).
- otherChange = сумма дельт не вошедших в первые limit строк.

Массив contributions сортируется по абсолютному значению дельты. Все дельты (включая otherChange) сходятся с общей дельтой — поле reconciled. Раскладка отвечает на вопрос «где изменился KPI», а не «почему». Для причинного анализа нужно отдельно проверять промо, цены, ассортимент, корректность периода и сторонние события. Метаданные coverage показывают охват магазин-дней за оба периода, в том числе когда нет подтверждённых продаж. Отсутствие чеков без coverage нельзя интерпретировать как нулевой факт.

## 2. Запасы

GET /api/v1/replenishment/risks

Параметры: as_of (включительно), lookback_days=7–90, review_days=0–28, max_stock_days=30–365, min_coverage_pct=50–100, store/region/category/sku, status, offset/limit (по 1–200 записей).

Для каждой позиции из inventory берутся агрегированные продажи за lookback_days и подтверждённые магазин-дни. На основе полной длины периода вычисляется avgDailyUnits = unitsSold / lookback_days. Если охват магазина меньше min_coverage_pct, статус insufficient_data: нехватка или излишек не утверждаются. Если спрос равен нулю и охват достаточен, доступный остаток > 0 помечается overstock, иначе balanced.

При среднем спросе выше нуля:
- available = onHand + confirmedInbound.
- targetDays = leadDays + reviewDays.
- shortage, если available < avgDailyUnits * targetDays; shortageUnits = ceil(avgDailyUnits * targetDays - available).
- overstock, если available > avgDailyUnits * maxStockDays; excessUnits = available - ceil(avgDailyUnits * maxStockDays).
- иначе balanced.
- estimatedCoverDays = available / avgDailyUnits.
- exposureAtUnitCost = (shortageUnits или excessUnits) * product.unit_cost.

Дат прихода inbound в исходной модели нет. Поэтому оценка рисков **приблизительная**, не учитывает календарь поставок, скорость потребления по дням недели, промо и stockout-censored demand. Это не оценка вероятности убытков и не ордер на покупку. Для реального риск-скоринга нужно расширить модель поставок.

## 3. XLSX

GET /api/v1/reports/sales.xlsx

Требуются start и end, опционально store, region, category, sku. CSV принимает те же фильтры. Ограничения: 1–731 день, максимум 10 000 SKU; при превышении 413 (нельзя молча обрезать отчёт). Роли analyst/category_manager/director. Наблюдатель и инженер не могут выгрузить данные. Категории/магазины проверяются на сервере.

Лист «Информация»: версия набора, период, фильтры, роль, полнота покрытий, формулы и происхождение показателя. Лист «Продажи»: SKU, units, revenue, cost, margin и итоговая строка. Денежные поля числовые, поэтому пригодны для дальнейших вычислений. Любое текстовое значение, похожее на формулу, принудительно сохраняется текстом.

## 4. Проверка

Открыть «Анализ KPI», выбрать две недели и проверить сумму вклада. Нажать строку магазина — группировка переключится на SKU с сохранением фильтров. Нажать SKU — появится дневная таблица. Выгрузить CSV/XLSX. Открыть «Риски запасов», выбрать магазин, длительность истории и пороги; отдельно проверить позиции с insufficient_data.

Тесты: tests/test_insights.py и PostgreSQL smoke в backend/scripts/smoke_insights.py. Требования по 3 млн строк и p95 пока не подтверждены и сохраняются в docs/ACCEPTANCE.md.
