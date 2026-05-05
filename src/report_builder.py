"""Report assembly — pure calculation.

Builds the high-level report data structure from an already-priced portfolio.
No side effects: history recording is handled by the caller (main.py).
"""

from dataclasses import dataclass, field

from src.fx_conversions import CurrencyConversion
from src.history import (
    AllTimeHigh,
    DailyChange,
    HistoryPoint,
    get_all_time_high,
    get_daily_change,
    get_year_to_date_history,
)
from src.portfolio import AllocationSnapshot, simulate_rebalance
from src.models import TradeRecommendation, TransientAlert


@dataclass
class RebalanceReportData:
    """All calculated data needed to render the rebalancing report."""

    current: AllocationSnapshot
    transient_alerts: list[TransientAlert]
    trades: list[TradeRecommendation]
    currency_conversions: list[CurrencyConversion]
    projected: AllocationSnapshot | None = None
    all_time_high: AllTimeHigh | None = None
    daily_change: DailyChange | None = None
    ytd_history: list[HistoryPoint] = field(default_factory=list)


def build_report_data(
    portfolio,
    targets: dict,
    usd_to_cad_rate: float,
    trades: list[TradeRecommendation],
    currency_conversions: list[CurrencyConversion],
    transient_alerts: list[TransientAlert],
    hidden_symbols: set[str],
) -> RebalanceReportData:
    """Calculate all report inputs from the current portfolio state."""
    from src.portfolio import build_allocation_snapshot

    current_snapshot = build_allocation_snapshot(
        portfolio,
        targets,
        usd_to_cad_rate,
        excluded_symbols=hidden_symbols,
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

    all_time_high = get_all_time_high(current_value=portfolio.total_value_cad)
    daily_change_data = get_daily_change(current_value=portfolio.total_value_cad)
    ytd_history = get_year_to_date_history()

    return RebalanceReportData(
        current=current_snapshot,
        transient_alerts=transient_alerts,
        trades=trades,
        currency_conversions=currency_conversions,
        projected=projected_snapshot,
        all_time_high=all_time_high,
        daily_change=daily_change_data,
        ytd_history=ytd_history,
    )
