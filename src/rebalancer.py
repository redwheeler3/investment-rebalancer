"""Public rebalancer API.

The public entrypoint delegates to the clearer planner implementation in
``rebalancer_planner.py``. Older step-based modules remain in the codebase as
reference pieces during the refactor, but this module now exposes a simple,
single public function.
"""

from src.portfolio import PortfolioSummary
from src.rebalancer_core import DEFAULT_DRIFT_TRADE_THRESHOLD_PCT
from src.rebalancer_planner import calculate_trades_with_planner


# ══════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════

def calculate_trades(
    portfolio: PortfolioSummary,
    targets: dict,
    usd_to_cad_rate: float,
    norberts_gambit_fee_cad: float = 10.49,
    drift_trade_threshold_pct: float = DEFAULT_DRIFT_TRADE_THRESHOLD_PCT,
    existing_only: bool = True,
    transient_symbols: set = None,
    dlr_quotes=None,
) -> list:
    """Calculate rebalance trades using the clearer planner model."""
    return calculate_trades_with_planner(
        portfolio=portfolio,
        targets=targets,
        usd_to_cad_rate=usd_to_cad_rate,
        norberts_gambit_fee_cad=norberts_gambit_fee_cad,
        drift_trade_threshold_pct=drift_trade_threshold_pct,
        existing_only=existing_only,
        transient_symbols=transient_symbols,
        dlr_quotes=dlr_quotes,
    )
