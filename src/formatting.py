from decimal import Decimal

def format_price_dynamic(value):
    """
    Dynamic visual formatting for dashboard / reports.

    - >= 1 euro     → 2 decimals (1.23)
    - >= 0.01       → 4 decimals (0.1234)
    - >= 0.0001     → 6 decimals (0.000123)
    - <  0.0001     → 8 decimals (0.00001234)
    """
    if value is None:
        return "-"

    try:
        v = float(value)
    except:
        return str(value)

    if v >= 1:
        return f"{v:.2f}"
    if v >= 0.01:
        return f"{v:.4f}"
    if v >= 0.0001:
        return f"{v:.6f}"
    return f"{v:.8f}"

