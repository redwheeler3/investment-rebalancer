"""Public rebalancer API.

The step-by-step implementation now lives in focused modules:
- rebalancer_core.py: state and low-level helpers
- rebalancer_steps.py: sell / buy / sweep phases
- rebalancer_netting.py: final trade cleanup
- rebalancer_simulation.py: projected portfolio math
"""

from src.portfolio import (
    PortfolioSummary,
    get_current_allocations,
    get_drifts,
    get_holdings_view,
)
from src.rebalancer_core import (
    MAX_ROUNDS,
    RebalanceState,
)
from src.rebalancer_netting import net_trades
from src.rebalancer_steps import (
    step_buy_underweight,
    step_sell_overweight,
    step_sweep_cash,
)


# ══════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════

def calculate_trades(
    portfolio: PortfolioSummary,
    targets: dict,
    usd_to_cad_rate: float,
    norberts_gambit_fee_cad: float = 10.49,
    existing_only: bool = True,
    transient_symbols: set = None,
) -> list:
    """
    Calculate trades to rebalance the portfolio using an iterative algorithm.

    Repeats Sell → Buy → Sweep rounds until all positions are within
    tolerance or no further improvement is possible.

    Args:
        portfolio: Aggregated portfolio summary.
        targets: Target allocation percentages by symbol.
        usd_to_cad_rate: Current USD/CAD exchange rate.
        norberts_gambit_fee_cad: Trading fee in CAD for Norbert's Gambit.
        existing_only: If True, only trade in accounts that already hold the position.
        transient_symbols: Symbols to skip (listed in config as transient).

    Returns:
        List of TradeRecommendation objects.
    """
    if transient_symbols is None:
        transient_symbols = set()

    total_value = portfolio.total_value_cad
    if total_value == 0:
        return []

    # Build shared state
    current_alloc = get_current_allocations(
        portfolio,
        usd_to_cad_rate,
        excluded_symbols=transient_symbols,
    )
    drifts = get_drifts(current_alloc, targets)

    state = RebalanceState(
        portfolio=portfolio,
        targets=targets,
        usd_to_cad_rate=usd_to_cad_rate,
        norberts_gambit_fee_cad=norberts_gambit_fee_cad,
        transient_symbols=transient_symbols,
        total_value=total_value,
        holdings_view=get_holdings_view(portfolio, transient_symbols),
        effective_drift=dict(drifts),
    )

    # Initialise per-account cash tracker
    for acct in portfolio.accounts:
        state.available_cash[acct.number] = {
            "CAD": acct.cash_cad,
            "USD": acct.cash_usd,
        }

    # ── Iterative rounds: Sell → Buy → Sweep ──
    for _round in range(MAX_ROUNDS):
        round_count = 0
        round_count += step_sell_overweight(state)
        round_count += step_buy_underweight(state, existing_only)
        round_count += step_sweep_cash(state)

        if round_count == 0:
            break  # No more work to do

    return net_trades(state.all_trades)


def simulate_rebalance(
    portfolio: PortfolioSummary,
    trades: list,
    targets: dict,
    usd_to_cad_rate: float,
    hidden_symbols: set = None,
) -> dict:
    """Compatibility wrapper for the dedicated simulation module."""
    from src.rebalancer_simulation import simulate_rebalance as _simulate_rebalance

    snapshot = _simulate_rebalance(
        portfolio,
        trades,
        targets,
        usd_to_cad_rate,
        hidden_symbols=hidden_symbols,
    )
    return {
        "projected_allocations": snapshot.allocations,
        "projected_accuracy": snapshot.accuracy,
    }
