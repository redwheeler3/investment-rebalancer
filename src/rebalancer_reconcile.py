"""Post-processing helpers for trimming excess sell funding.

The baseline rebalancer sizes sells and buys from drift. This module performs a
final pass that trims sell quantities when those sells raise more cash than the
account's planned buys actually require, while still preserving tolerance and
funding feasibility.
"""

import math

from src.funding import (
    build_account_trade_impacts,
    can_fund_net_cash_requirement,
    net_account_cash,
)
from src.rebalancer_core import to_cad
from src.rebalancer_simulation import simulate_rebalance
from src.rules import TradeRecommendation


def trim_excess_sell_funding(
    portfolio,
    trades: list,
    targets: dict,
    usd_to_cad_rate: float,
    fee_cad: float,
    drift_trade_threshold_pct: float,
    dlr_quotes=None,
    hidden_symbols: set | None = None,
) -> list:
    """Trim sell quantities when they raise more cash than account buys require.

    Starts from the baseline netted trades, then reduces each sell as much as
    possible while preserving two conditions:
    1. the account can still fund all planned buys, and
    2. the sold symbol remains within drift tolerance after the trim.
    """
    if not trades:
        return trades

    if hidden_symbols is None:
        hidden_symbols = set()

    trimmed = list(trades)
    projected = simulate_rebalance(
        portfolio,
        trimmed,
        targets,
        usd_to_cad_rate,
        hidden_symbols=hidden_symbols,
    )

    index = 0
    while index < len(trimmed):
        trade = trimmed[index]
        if trade.action != "SELL":
            index += 1
            continue

        max_reduction = _max_sell_reduction_allowed_by_tolerance(
            portfolio,
            trade,
            projected.drifts,
            drift_trade_threshold_pct,
            usd_to_cad_rate,
        )
        if max_reduction <= 0:
            index += 1
            continue

        low = 0
        high = max_reduction
        while low < high:
            reduction = (low + high + 1) // 2
            candidate_qty = trade.quantity - reduction
            candidate_trades = _updated_trades_with_resized_trade(trimmed, index, candidate_qty)
            if _trade_plan_is_fundable(
                portfolio,
                candidate_trades,
                usd_to_cad_rate,
                fee_cad,
                dlr_quotes=dlr_quotes,
            ):
                low = reduction
            else:
                high = reduction - 1

        if low > 0:
            trimmed = _updated_trades_with_resized_trade(trimmed, index, trade.quantity - low)
            projected = simulate_rebalance(
                portfolio,
                trimmed,
                targets,
                usd_to_cad_rate,
                hidden_symbols=hidden_symbols,
            )

        index += 1

    return trimmed


def _updated_trades_with_resized_trade(trades: list, index: int, quantity: int) -> list:
    """Return a new trade list with one trade resized or removed."""
    updated = list(trades)
    if quantity <= 0:
        del updated[index]
        return updated

    trade = updated[index]
    updated[index] = TradeRecommendation(
        symbol=trade.symbol,
        action=trade.action,
        quantity=quantity,
        account_number=trade.account_number,
        account_type=trade.account_type,
        owner=trade.owner,
        price=trade.price,
        currency=trade.currency,
        estimated_value=trade.price * quantity,
        note=trade.note,
    )
    return updated


def _trade_plan_is_fundable(
    portfolio,
    trades: list,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
) -> bool:
    """Check whether each account can still fund the candidate trade plan."""
    acct_map = {acct.number: acct for acct in portfolio.accounts}
    for acct_number, impact in build_account_trade_impacts(trades).items():
        acct = acct_map.get(acct_number)
        if acct is None:
            continue

        net_cash = net_account_cash(acct, impact)
        if not can_fund_net_cash_requirement(
            net_cash,
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes=dlr_quotes,
        ):
            return False

    return True


def _max_sell_reduction_allowed_by_tolerance(
    portfolio,
    trade,
    projected_drifts: dict,
    drift_trade_threshold_pct: float,
    usd_to_cad_rate: float,
) -> int:
    """Maximum shares this sell can be reduced without breaching tolerance."""
    if trade.action != "SELL" or portfolio.total_value_cad <= 0:
        return 0

    per_share_drift_pct = (
        to_cad(trade.price, trade.currency, usd_to_cad_rate) / portfolio.total_value_cad
    ) * 100.0
    if per_share_drift_pct <= 0:
        return 0

    current_projected_drift = projected_drifts.get(trade.symbol, 0.0)
    drift_room = drift_trade_threshold_pct - current_projected_drift
    if drift_room <= 0:
        return 0

    return min(trade.quantity, int(math.floor((drift_room + 1e-9) / per_share_drift_pct)))
