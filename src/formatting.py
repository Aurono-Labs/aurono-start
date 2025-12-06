# src/formatting.py
from decimal import Decimal

def decimals_from_ticksize(tick: Decimal) -> int:
    """
    Convert a price tickSize like Decimal('0.00001') → 5 decimals.
    """
    return max(0, -tick.as_tuple().exponent)

def format_price(value: Decimal, tick_size: Decimal) -> str:
    """
    Format a price with the correct number of decimals for the market.
    """
    decimals = decimals_from_ticksize(tick_size)
    return f"{value:.{decimals}f}"

def format_amount(value: Decimal, amount_decimals: int) -> str:
    """
    Format amounts (volume) using quantityDecimals.
    """
    return f"{value:.{amount_decimals}f}"
