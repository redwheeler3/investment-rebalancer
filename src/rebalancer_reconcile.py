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
)
from src.rebalancer_netting import net_trades
from src.rebalancer_core import to_cad
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
    """Optimise the final trade plan after the main rebalance rounds.

    The optimisation alternates between:
    1. trimming excess sells down to the minimum still-fundable size, and
    2. redeploying any leftover account cash into still-underweight holdings,

    repeating until no further improvement is possible.
    """
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
            while _deploy_same_currency_buy_once(
                portfolio,
                acct,
                updated,
                residual_cash,
                projected.drifts,
                usd_to_cad_rate,
                hidden_symbols,
            ):
                made_trade = True
                projected = simulate_rebalance(
                    portfolio,
                    updated,
                    targets,
                    usd_to_cad_rate,
                    hidden_symbols=hidden_symbols,
                )

            while _deploy_cross_currency_buy_once(
                portfolio,
                acct,
                updated,
                residual_cash,
                projected.drifts,
                usd_to_cad_rate,
                fee_cad,
                hidden_symbols,
                dlr_quotes=dlr_quotes,
            ):
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
    acct_map = {acct.number: acct for acct in portfolio.accounts}
    impacts = build_account_trade_impacts(trades)
    residual = {}

    for acct in portfolio.accounts:
        net_cash = net_account_cash(acct, impacts.get(acct.number))
        settled = _settle_net_cash_after_conversion(
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


def _settle_net_cash_after_conversion(net_cash, usd_to_cad_rate: float, fee_cad: float, dlr_quotes=None):
    """Normalise net CAD/USD cash after satisfying at most one currency deficit."""
    cad = net_cash.cad
    usd = net_cash.usd

    if cad >= 0 and usd >= 0:
        return type(net_cash)(cad=max(0.0, cad), usd=max(0.0, usd))

    if usd < 0 and cad > 0:
        spent_cad, received_usd = _cad_to_usd_conversion_for_target(
            cad,
            abs(usd),
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes=dlr_quotes,
        )
        return type(net_cash)(
            cad=max(0.0, cad - spent_cad),
            usd=max(0.0, usd + received_usd),
        )

    if cad < 0 and usd > 0:
        spent_usd, received_cad = _usd_to_cad_conversion_for_target(
            usd,
            abs(cad),
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes=dlr_quotes,
        )
        return type(net_cash)(
            cad=max(0.0, cad + received_cad),
            usd=max(0.0, usd - spent_usd),
        )

    return type(net_cash)(cad=max(0.0, cad), usd=max(0.0, usd))


def _cad_to_usd_conversion_for_target(
    cad_available: float,
    usd_target: float,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
) -> tuple[float, float]:
    """Return (CAD spent incl. fee, USD received) for a CAD->USD conversion."""
    cad_buy_price = getattr(dlr_quotes, "cad_buy_price", 0.0) if dlr_quotes else 0.0
    usd_sell_price = getattr(dlr_quotes, "usd_sell_price", 0.0) if dlr_quotes else 0.0

    if cad_buy_price > 0 and usd_sell_price > 0:
        shares_needed = int(math.ceil(usd_target / usd_sell_price))
        shares_affordable = int(math.floor(max(0.0, cad_available - fee_cad) / cad_buy_price))
        shares = min(shares_needed, shares_affordable)
        if shares <= 0:
            return 0.0, 0.0
        return shares * cad_buy_price + fee_cad, shares * usd_sell_price

    if usd_to_cad_rate <= 0 or cad_available <= fee_cad:
        return 0.0, 0.0

    spent_cad = min(cad_available, usd_target * usd_to_cad_rate + fee_cad)
    received_usd = max(0.0, (spent_cad - fee_cad) / usd_to_cad_rate)
    return spent_cad, received_usd


def _usd_to_cad_conversion_for_target(
    usd_available: float,
    cad_target: float,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
) -> tuple[float, float]:
    """Return (USD spent, CAD received net of fee) for a USD->CAD conversion."""
    usd_buy_price = getattr(dlr_quotes, "usd_buy_price", 0.0) if dlr_quotes else 0.0
    cad_sell_price = getattr(dlr_quotes, "cad_sell_price", 0.0) if dlr_quotes else 0.0

    if usd_buy_price > 0 and cad_sell_price > 0:
        shares_needed = int(math.ceil((cad_target + fee_cad) / cad_sell_price))
        shares_affordable = int(math.floor(usd_available / usd_buy_price))
        shares = min(shares_needed, shares_affordable)
        if shares <= 0:
            return 0.0, 0.0
        return shares * usd_buy_price, max(0.0, shares * cad_sell_price - fee_cad)

    if usd_to_cad_rate <= 0 or usd_available <= 0:
        return 0.0, 0.0

    usd_needed = min(usd_available, (cad_target + fee_cad) / usd_to_cad_rate)
    received_cad = max(0.0, usd_needed * usd_to_cad_rate - fee_cad)
    return usd_needed, received_cad


def _account_underweight_candidates(
    portfolio,
    acct,
    drifts: dict,
    hidden_symbols: set,
    currency: str,
) -> list[tuple[str, float, float]]:
    """Return account-local underweight holdings in the requested currency."""
    candidates = []
    seen = set()

    for pos in acct.positions:
        if pos.quantity <= 0 or pos.currency != currency:
            continue
        if pos.symbol in hidden_symbols or pos.symbol in seen:
            continue

        drift_pct = drifts.get(pos.symbol, 0.0)
        if drift_pct >= 0:
            continue

        holding = portfolio.holdings.get(pos.symbol)
        if not holding:
            continue

        ask_price_native = holding.ask_price or pos.current_price
        if ask_price_native <= 0:
            continue

        seen.add(pos.symbol)
        candidates.append((pos.symbol, drift_pct, ask_price_native))

    candidates.sort(key=lambda item: item[1])
    return candidates


def _deploy_same_currency_buy_once(
    portfolio,
    acct,
    trades: list,
    residual_cash: dict[str, dict[str, float]],
    drifts: dict,
    usd_to_cad_rate: float,
    hidden_symbols: set,
) -> bool:
    """Use same-currency residual cash for the most underweight account holding."""
    acct_cash = residual_cash.setdefault(acct.number, {"CAD": 0.0, "USD": 0.0})

    best_choice = None
    for currency in ("CAD", "USD"):
        cash_native = acct_cash.get(currency, 0.0)
        if cash_native <= 0:
            continue

        candidates = _account_underweight_candidates(
            portfolio,
            acct,
            drifts,
            hidden_symbols,
            currency,
        )
        if not candidates:
            continue

        symbol, drift_pct, ask_price_native = candidates[0]
        affordable_shares = int(math.floor(cash_native / ask_price_native))
        if affordable_shares <= 0:
            continue

        gap_cad = abs(drift_pct / 100.0) * portfolio.total_value_cad
        gap_native = gap_cad / usd_to_cad_rate if currency == "USD" else gap_cad
        shares = min(int(math.ceil(gap_native / ask_price_native)), affordable_shares)
        if shares <= 0:
            continue

        choice = (drift_pct, symbol, shares, ask_price_native, currency)
        if best_choice is None or choice[0] < best_choice[0]:
            best_choice = choice

    if best_choice is None:
        return False

    drift_pct, symbol, shares, ask_price_native, currency = best_choice
    cost_native = shares * ask_price_native
    acct_cash[currency] -= cost_native
    trades.append(TradeRecommendation(
        symbol=symbol,
        action="BUY",
        quantity=shares,
        account_number=acct.number,
        account_type=acct.account_type,
        owner=acct.owner,
        price=ask_price_native,
        currency=currency,
        estimated_value=cost_native,
        note="Residual cash deployment",
    ))
    return True


def _deploy_cross_currency_buy_once(
    portfolio,
    acct,
    trades: list,
    residual_cash: dict[str, dict[str, float]],
    drifts: dict,
    usd_to_cad_rate: float,
    fee_cad: float,
    hidden_symbols: set,
    dlr_quotes=None,
) -> bool:
    """Use cross-currency residual cash for the most underweight account holding."""
    acct_cash = residual_cash.setdefault(acct.number, {"CAD": 0.0, "USD": 0.0})
    best_choice = None

    for source_currency, target_currency in (("CAD", "USD"), ("USD", "CAD")):
        source_cash = acct_cash.get(source_currency, 0.0)
        if source_cash <= 0:
            continue

        candidates = _account_underweight_candidates(
            portfolio,
            acct,
            drifts,
            hidden_symbols,
            target_currency,
        )
        if not candidates:
            continue

        symbol, drift_pct, ask_price_native = candidates[0]
        buying_power_native = _cross_currency_buying_power(
            source_cash,
            source_currency,
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes=dlr_quotes,
        )
        affordable_shares = int(math.floor(buying_power_native / ask_price_native))
        if affordable_shares <= 0:
            continue

        gap_cad = abs(drift_pct / 100.0) * portfolio.total_value_cad
        gap_native = gap_cad / usd_to_cad_rate if target_currency == "USD" else gap_cad
        shares = min(int(math.ceil(gap_native / ask_price_native)), affordable_shares)
        if shares <= 0:
            continue

        choice = (
            drift_pct,
            symbol,
            shares,
            ask_price_native,
            source_currency,
            target_currency,
        )
        if best_choice is None or choice[0] < best_choice[0]:
            best_choice = choice

    if best_choice is None:
        return False

    (
        _drift_pct,
        symbol,
        shares,
        ask_price_native,
        source_currency,
        target_currency,
    ) = best_choice
    cost_native = shares * ask_price_native
    _consume_cross_currency_cash(
        acct_cash,
        source_currency,
        target_currency,
        cost_native,
        usd_to_cad_rate,
        fee_cad,
        dlr_quotes=dlr_quotes,
    )
    trades.append(TradeRecommendation(
        symbol=symbol,
        action="BUY",
        quantity=shares,
        account_number=acct.number,
        account_type=acct.account_type,
        owner=acct.owner,
        price=ask_price_native,
        currency=target_currency,
        estimated_value=cost_native,
        note="Requires currency conversion; residual cash deployment",
    ))
    return True


def _cross_currency_buying_power(
    source_cash: float,
    source_currency: str,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
) -> float:
    """Return conservative target-currency buying power from one source balance."""
    if source_currency == "CAD":
        cad_buy_price = getattr(dlr_quotes, "cad_buy_price", 0.0) if dlr_quotes else 0.0
        usd_sell_price = getattr(dlr_quotes, "usd_sell_price", 0.0) if dlr_quotes else 0.0
        if cad_buy_price > 0 and usd_sell_price > 0:
            shares = int(math.floor(max(0.0, source_cash - fee_cad) / cad_buy_price))
            return shares * usd_sell_price
        return max(0.0, source_cash - fee_cad) / usd_to_cad_rate if usd_to_cad_rate > 0 else 0.0

    usd_buy_price = getattr(dlr_quotes, "usd_buy_price", 0.0) if dlr_quotes else 0.0
    cad_sell_price = getattr(dlr_quotes, "cad_sell_price", 0.0) if dlr_quotes else 0.0
    if usd_buy_price > 0 and cad_sell_price > 0:
        shares = int(math.floor(source_cash / usd_buy_price))
        return max(0.0, shares * cad_sell_price - fee_cad)
    return max(0.0, source_cash * usd_to_cad_rate - fee_cad)


def _consume_cross_currency_cash(
    acct_cash: dict[str, float],
    source_currency: str,
    target_currency: str,
    cost_native: float,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
) -> None:
    """Apply a cross-currency buy to the residual cash map conservatively."""
    if source_currency == "CAD" and target_currency == "USD":
        spent_cad, received_usd = _cad_to_usd_conversion_for_target(
            acct_cash.get("CAD", 0.0),
            cost_native,
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes=dlr_quotes,
        )
        acct_cash["CAD"] = max(0.0, acct_cash.get("CAD", 0.0) - spent_cad)
        acct_cash["USD"] = max(0.0, acct_cash.get("USD", 0.0) + received_usd - cost_native)
        return

    if source_currency == "USD" and target_currency == "CAD":
        spent_usd, received_cad = _usd_to_cad_conversion_for_target(
            acct_cash.get("USD", 0.0),
            cost_native,
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes=dlr_quotes,
        )
        acct_cash["USD"] = max(0.0, acct_cash.get("USD", 0.0) - spent_usd)
        acct_cash["CAD"] = max(0.0, acct_cash.get("CAD", 0.0) + received_cad - cost_native)



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
