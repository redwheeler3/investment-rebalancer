"""Post-processing helpers for trimming excess sell funding.

The baseline rebalancer sizes sells and buys from drift. This module performs a
final optimisation pass that:
1. trims sell quantities when those sells raise more cash than the account's
   planned buys actually require,
2. deploys residual cash into still-underweight holdings when possible, and
3. repeats until the plan stabilises.
"""

import math

from src.funding import (
    build_account_trade_impacts,
    can_fund_net_cash_requirement,
    net_account_cash,
    settle_net_cash_after_conversion,
)
from src.rebalancer_core import to_cad
from src.rebalancer_deployment import build_cross_currency_buy, build_same_currency_buy
from src.rebalancer_netting import net_trades
from src.rebalancer_simulation import simulate_rebalance
from src.rules import TradeRecommendation


POST_PROCESS_MAX_PASSES = 5


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
    """Optimise the final trade plan after the main rebalance rounds."""
    if not trades:
        return trades

    if hidden_symbols is None:
        hidden_symbols = set()

    optimised = net_trades(list(trades))
    for _ in range(POST_PROCESS_MAX_PASSES):
        before = list(optimised)
        optimised = _trim_excess_sell_funding_once(
            portfolio,
            optimised,
            targets,
            usd_to_cad_rate,
            fee_cad,
            drift_trade_threshold_pct,
            dlr_quotes=dlr_quotes,
            hidden_symbols=hidden_symbols,
        )
        optimised = net_trades(optimised)
        optimised = _deploy_residual_underweight_buys(
            portfolio,
            optimised,
            targets,
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes=dlr_quotes,
            hidden_symbols=hidden_symbols,
        )
        optimised = net_trades(optimised)
        if optimised == before:
            break

    return optimised


def _trim_excess_sell_funding_once(
    portfolio,
    trades: list,
    targets: dict,
    usd_to_cad_rate: float,
    fee_cad: float,
    drift_trade_threshold_pct: float,
    dlr_quotes=None,
    hidden_symbols: set | None = None,
) -> list:
    """Trim sell quantities when they raise more cash than account buys require."""
    if not trades:
        return trades

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


def _deploy_residual_underweight_buys(
    portfolio,
    trades: list,
    targets: dict,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
    hidden_symbols: set | None = None,
) -> list:
    """Use leftover account cash for any holdings that are still under target."""
    if not trades:
        return trades

    if hidden_symbols is None:
        hidden_symbols = set()

    updated = list(trades)
    residual_cash = _build_residual_cash_map(
        portfolio,
        updated,
        usd_to_cad_rate,
        fee_cad,
        dlr_quotes=dlr_quotes,
    )

    while True:
        projected = simulate_rebalance(
            portfolio,
            updated,
            targets,
            usd_to_cad_rate,
            hidden_symbols=hidden_symbols,
        )
        made_trade = False

        for acct in portfolio.accounts:
            for currency in ("CAD", "USD"):
                while True:
                    trade = build_same_currency_buy(
                        acct,
                        residual_cash,
                        portfolio.holdings,
                        projected.drifts,
                        hidden_symbols,
                        portfolio.total_value_cad,
                        usd_to_cad_rate,
                        currency,
                        0.0,
                        note="Residual cash deployment",
                    )
                    if trade is None:
                        break

                    updated.append(trade)
                    made_trade = True
                    projected = simulate_rebalance(
                        portfolio,
                        updated,
                        targets,
                        usd_to_cad_rate,
                        hidden_symbols=hidden_symbols,
                    )

            for source_currency in ("CAD", "USD"):
                while True:
                    trade = build_cross_currency_buy(
                        acct,
                        residual_cash,
                        portfolio.holdings,
                        projected.drifts,
                        hidden_symbols,
                        portfolio.total_value_cad,
                        usd_to_cad_rate,
                        source_currency,
                        fee_cad,
                        0.0,
                        note="Requires currency conversion; residual cash deployment",
                        dlr_quotes=dlr_quotes,
                    )
                    if trade is None:
                        break

                    updated.append(trade)
                    made_trade = True
                    projected = simulate_rebalance(
                        portfolio,
                        updated,
                        targets,
                        usd_to_cad_rate,
                        hidden_symbols=hidden_symbols,
                    )

        if not made_trade:
            break

    return updated


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


def _build_residual_cash_map(
    portfolio,
    trades: list,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
) -> dict[str, dict[str, float]]:
    """Return per-account residual cash after the current trade plan settles."""
    impacts = build_account_trade_impacts(trades)
    residual = {}

    for acct in portfolio.accounts:
        net_cash = net_account_cash(acct, impacts.get(acct.number))
        settled = settle_net_cash_after_conversion(
            net_cash,
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes=dlr_quotes,
        )
        residual[acct.number] = {
            "CAD": settled.cad,
            "USD": settled.usd,
        }

    for acct_number in impacts:
        residual.setdefault(acct_number, {"CAD": 0.0, "USD": 0.0})

    return residual


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
