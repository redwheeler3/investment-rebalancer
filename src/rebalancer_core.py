"""Small shared constants and math helpers for the rebalancer.

The previous mutable-state engine has been replaced by the clearer planner in
``rebalancer_planner.py``. This module now keeps only the constants and basic
value-conversion helpers that are still shared across the planner and related
modules.
"""

from __future__ import annotations

import math


# Default minimum absolute drift before a symbol is eligible for trading
DEFAULT_DRIFT_TRADE_THRESHOLD_PCT = 0.1

# Maximum optimisation rounds before stopping
MAX_ROUNDS = 10


def to_cad(value: float, currency: str, usd_to_cad_rate: float) -> float:
    """Convert a value to CAD."""
    return value * usd_to_cad_rate if currency == "USD" else value


def shares_for_drift(total_value_cad: float, drift_pct: float, price_native: float, currency: str, usd_to_cad_rate: float) -> int:
    """Calculate whole shares needed to close a drift gap."""
    gap_cad = abs(drift_pct / 100.0) * total_value_cad
    gap_native = gap_cad / usd_to_cad_rate if currency == "USD" else gap_cad
    shares = int(math.floor(gap_native / price_native))
    if shares == 0:
        one_share_cad = to_cad(price_native, currency, usd_to_cad_rate)
        if one_share_cad < 2 * gap_cad:
            shares = 1
    return shares