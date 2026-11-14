from decimal import Decimal, ROUND_CEILING


METRICS = {
    "revenue": {
        "formula": "SUM(daily_sales_mart.revenue) = SUM(qty * unit_price)",
        "unit": "currency",
        "source": "staged_sales → daily_sales_mart",
    },
    "units": {
        "formula": "SUM(daily_sales_mart.units) = SUM(qty)",
        "unit": "items",
        "source": "staged_sales → daily_sales_mart",
    },
    "margin": {
        "formula": "SUM(revenue) - SUM(cost)",
        "unit": "currency",
        "source": "staged_sales → daily_sales_mart",
    },
    "coverage_days": {
        "formula": "on_hand / MAX(average daily units over observed days, epsilon); undefined when no sales",
        "unit": "days",
        "source": "inventory + daily_sales_mart",
    },
}


def replenishment(forecast_qty: float, on_hand: int, inbound: int, safety_stock: int,
                  pack_size: int, min_order: int):
    if any(v < 0 for v in (forecast_qty, on_hand, inbound, safety_stock)):
        raise ValueError("Quantities cannot be negative")
    if pack_size < 1 or min_order < 1:
        raise ValueError("Pack size and minimum order must be positive")
    demand = Decimal(str(forecast_qty))
    raw = max(Decimal(0), demand + safety_stock - on_hand - inbound)
    if raw == 0:
        rounded = 0
    else:
        preliminary = max(raw, Decimal(min_order))
        rounded = int((preliminary / pack_size).to_integral_value(rounding=ROUND_CEILING)) * pack_size
    return {
        "forecast": float(demand),
        "safety_stock": safety_stock,
        "on_hand": on_hand,
        "confirmed_inbound": inbound,
        "raw_need": float(raw),
        "min_order": min_order,
        "pack_size": pack_size,
        "recommended": rounded,
        "explanation": (
            f"max(0, {demand} + {safety_stock} - {on_hand} - {inbound}) = {raw}; "
            f"MOQ {min_order}, pack {pack_size} → {rounded}"
        ),
    }
