"""Report assembly helpers.

Builds the high-level report data structure from an already-priced portfolio,
without mixing that logic into the CLI entrypoint.
"""

from dataclasses import dataclass, field
from typing import Any

from src.currency import calculate_currency_needs
from src.history import get_all_time_high, record_value
from src.portfolio import AllocationSnapshot
from src.rebalancer import calculate_trades
from src.rebalancer_simulation import simulate_rebalance
from src.rules import get_transient_status


@dataclass
class RebalanceReportData:
    """All calculated data needed to render the rebalancing report."""

    current: AllocationSnapshot
    transient_alerts: list
    trades: list
    currency_conversions: list
    projected: AllocationSnapshot | None = None
    all_time_high: Any | None = None


def build_report_data(
    portfolio,
    targets: dict,
    transient_symbols: list,
    norberts_gambit_fee_cad: float,
    drift_trade_threshold_pct: float,
    usd_to_cad_rate: float,
    dlr_quotes,
) -> RebalanceReportData:
    """Calculate all report inputs from the current portfolio state."""
    from src.portfolio import build_allocation_snapshot

    transient_status = get_transient_status(portfolio, transient_symbols)
    hidden_symbols = transient_status["symbols"]

    current_snapshot = build_allocation_snapshot(
        portfolio,
        targets,
        usd_to_cad_rate,
        excluded_symbols=hidden_symbols,
    )

    trades = calculate_trades(
        portfolio,
        targets,
        usd_to_cad_rate,
        norberts_gambit_fee_cad,
        drift_trade_threshold_pct,
        existing_only=True,
        transient_symbols=hidden_symbols,
        dlr_quotes=dlr_quotes,
    )

    currency_conversions = calculate_currency_needs(
        trades,
        portfolio.accounts,
        usd_to_cad_rate,
        dlr_quotes,
        norberts_gambit_fee_cad,
    )

    projected_snapshot = None
    if trades:
        projected_snapshot = simulate_rebalance(
            portfolio,
            trades,
            targets,
            usd_to_cad_rate,
            hidden_symbols=hidden_symbols,
        )

    record_value(portfolio.total_value_cad)
    all_time_high = get_all_time_high(current_value=portfolio.total_value_cad)

    return RebalanceReportData(
        current=current_snapshot,
        transient_alerts=transient_status["alerts"],
        trades=trades,
        currency_conversions=currency_conversions,
        projected=projected_snapshot,
        all_time_high=all_time_high,
    )