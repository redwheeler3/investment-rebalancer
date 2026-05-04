"""Step-oriented rebalancer logic.

These helpers contain the sell / buy / sweep phases of the iterative
rebalancing algorithm.
"""

import math

from src.rebalancer_deployment import build_cross_currency_buy, build_same_currency_buy
from src.rebalancer_core import (
    RebalanceState,
    deduct_buy,
    effective_cash,
    record_trade,
    shares_for_drift,
    to_cad,
)
from src.rules import (
    TradeRecommendation,
    allocate_sell,
    effective_qty,
    find_accounts_for_symbol,
)


def step_sell_overweight(state: RebalanceState) -> int:
    """Sell overweight positions to bring them toward target."""
    trade_count = 0
    tolerance_pct = state.drift_trade_threshold_pct
    overweight_symbols = [
        (symbol, drift_pct)
        for symbol, drift_pct in state.effective_drift.items()
        if symbol not in ("CAD", "USD") and drift_pct > tolerance_pct
    ]
    overweight_symbols.sort(key=lambda item: item[1], reverse=True)

    for symbol, drift_pct in overweight_symbols:
        holding = state.holdings_view.get(symbol)
        if not holding:
            continue

        bid_price_native = holding.bid_price or holding.current_price
        currency = holding.currency
        if bid_price_native <= 0:
            continue

        shares = shares_for_drift(state, drift_pct, bid_price_native, currency)
        if shares <= 0:
            continue

        sell_trades = allocate_sell(
            symbol,
            shares,
            bid_price_native,
            currency,
            state.portfolio.accounts,
            effective_drift=state.effective_drift,
            transient_symbols=state.transient_symbols,
            drift_trade_threshold_pct=tolerance_pct,
            position_deltas=state.position_deltas,
        )
        for trade in sell_trades:
            state.available_cash[trade.account_number][currency] += trade.estimated_value
            record_trade(state, trade)
            trade_count += 1

    return trade_count


def step_buy_underweight(state: RebalanceState, existing_only: bool) -> int:
    """Buy underweight positions using available cash or displacement sells."""
    trade_count = 0
    tolerance_pct = state.drift_trade_threshold_pct
    underweight_symbols = [
        (symbol, drift_pct)
        for symbol, drift_pct in state.effective_drift.items()
        if symbol not in ("CAD", "USD") and drift_pct < -tolerance_pct
    ]
    underweight_symbols.sort(key=lambda item: item[1])

    for symbol, _initial_drift in underweight_symbols:
        drift_pct = state.effective_drift.get(symbol, 0)
        if drift_pct >= -tolerance_pct:
            continue

        holding = state.holdings_view.get(symbol)
        if not holding:
            continue

        ask_price_native = holding.ask_price or holding.current_price
        currency = holding.currency
        if ask_price_native <= 0:
            continue

        shares_needed = shares_for_drift(state, drift_pct, ask_price_native, currency)
        if shares_needed <= 0:
            continue

        if existing_only:
            eligible_accounts = find_accounts_for_symbol(symbol, state.portfolio.accounts)
        else:
            eligible_accounts = list(state.portfolio.accounts)
        if not eligible_accounts:
            continue

        eligible_accounts.sort(
            key=lambda account: effective_cash(state, account.number, currency),
            reverse=True,
        )

        remaining_shares = shares_needed
        for acct in eligible_accounts:
            if remaining_shares <= 0:
                break

            effective_cash_native = effective_cash(state, acct.number, currency)
            if effective_cash_native >= ask_price_native:
                affordable_shares = int(math.floor(effective_cash_native / ask_price_native))
                quantity = min(remaining_shares, affordable_shares)
                if quantity > 0:
                    cost_native = quantity * ask_price_native
                    converted = deduct_buy(state, acct.number, cost_native, currency)
                    record_trade(state, TradeRecommendation(
                        symbol=symbol,
                        action="BUY",
                        quantity=quantity,
                        account_number=acct.number,
                        account_type=acct.account_type,
                        owner=acct.owner,
                        price=ask_price_native,
                        currency=currency,
                        estimated_value=cost_native,
                        note="Requires currency conversion" if converted else "",
                    ))
                    remaining_shares -= quantity
                    trade_count += quantity
                continue

            trade_count += try_displacement_sell(
                state,
                symbol,
                ask_price_native,
                currency,
                acct,
                remaining_shares,
            )

            acct_cash_native = max(0, state.available_cash.get(acct.number, {}).get(currency, 0))
            if acct_cash_native >= ask_price_native and remaining_shares > 0:
                affordable_shares = int(math.floor(acct_cash_native / ask_price_native))
                quantity = min(remaining_shares, affordable_shares)
                if quantity > 0:
                    cost_native = quantity * ask_price_native
                    state.available_cash[acct.number][currency] -= cost_native
                    record_trade(state, TradeRecommendation(
                        symbol=symbol,
                        action="BUY",
                        quantity=quantity,
                        account_number=acct.number,
                        account_type=acct.account_type,
                        owner=acct.owner,
                        price=ask_price_native,
                        currency=currency,
                        estimated_value=cost_native,
                    ))
                    remaining_shares -= quantity
                    trade_count += quantity

    return trade_count


def try_displacement_sell(
    state: RebalanceState,
    buy_symbol: str,
    buy_ask_native: float,
    currency: str,
    acct,
    remaining_shares: int,
) -> int:
    """Sell another position in this account to raise cash for a buy.

    Displacement sells are intentionally conservative: they are only allowed
    from positions that are still globally overweight, and only up to the point
    where the sold symbol remains at or above the configured drift tolerance.
    This prevents cross-account churn where a symbol is sold in one account
    only to become underweight and get repurchased elsewhere.
    """
    trade_count = 0
    displacement_candidates = []
    tolerance_pct = state.drift_trade_threshold_pct

    for pos in acct.positions:
        if pos.symbol == buy_symbol or pos.symbol in state.transient_symbols:
            continue
        if pos.currency != currency or pos.quantity <= 0:
            continue

        holding = state.holdings_view.get(pos.symbol)
        if not holding:
            continue

        bid_price_native = holding.bid_price or pos.current_price
        if bid_price_native <= 0:
            continue

        drift_pct = state.effective_drift.get(pos.symbol, 0)
        max_sell_quantity = max_displacement_sell_quantity(
            state,
            drift_pct,
            bid_price_native,
            currency,
        )
        if max_sell_quantity <= 0:
            continue

        displacement_candidates.append(
            (pos.symbol, drift_pct, bid_price_native, max_sell_quantity)
        )

    if not displacement_candidates:
        return 0

    displacement_candidates.sort(key=lambda item: item[1], reverse=True)
    for (
        candidate_symbol,
        _candidate_drift_pct,
        candidate_bid_native,
        max_sell_quantity,
    ) in displacement_candidates:
        if remaining_shares <= 0:
            break

        acct_cash_native = max(0, state.available_cash.get(acct.number, {}).get(currency, 0))
        shortfall_native = remaining_shares * buy_ask_native - acct_cash_native
        if shortfall_native <= 0:
            break

        sell_quantity = int(math.ceil(shortfall_native / candidate_bid_native))
        held_quantity = effective_qty(acct, candidate_symbol, state.position_deltas)
        sell_quantity = min(sell_quantity, held_quantity, max_sell_quantity)
        if sell_quantity <= 0:
            continue

        sell_trade = TradeRecommendation(
            symbol=candidate_symbol,
            action="SELL",
            quantity=sell_quantity,
            account_number=acct.number,
            account_type=acct.account_type,
            owner=acct.owner,
            price=candidate_bid_native,
            currency=currency,
            estimated_value=candidate_bid_native * sell_quantity,
            note="Displacement sell",
        )
        state.available_cash[acct.number][currency] += sell_trade.estimated_value
        record_trade(state, sell_trade)
        trade_count += 1

    return trade_count


def max_displacement_sell_quantity(
    state: RebalanceState,
    drift_pct: float,
    price_native: float,
    currency: str,
) -> int:
    """Maximum whole shares sellable without pushing a symbol below tolerance."""
    tolerance_pct = state.drift_trade_threshold_pct
    excess_drift_pct = drift_pct - tolerance_pct
    if excess_drift_pct <= 0 or state.total_value <= 0 or price_native <= 0:
        return 0

    per_share_drift_pct = (
        to_cad(price_native, currency, state.usd_to_cad_rate) / state.total_value
    ) * 100.0
    if per_share_drift_pct <= 0:
        return 0

    return int(math.floor((excess_drift_pct + 1e-9) / per_share_drift_pct))


def step_sweep_cash(state: RebalanceState) -> int:
    """Deploy remaining cash via same-currency and cross-currency sweeps."""
    trade_count = 0
    for acct in state.portfolio.accounts:
        trade_count += sweep_same_currency(state, acct)
        trade_count += sweep_cross_currency(state, acct)
    return trade_count


def sweep_same_currency(state: RebalanceState, acct) -> int:
    """Iteratively buy the most underweight position using same-currency cash."""
    trade_count = 0

    for currency in ["CAD", "USD"]:
        for _ in range(50):
            trade = build_same_currency_buy(
                acct,
                state.available_cash,
                state.holdings_view,
                state.effective_drift,
                state.transient_symbols,
                state.total_value,
                state.usd_to_cad_rate,
                currency,
                state.drift_trade_threshold_pct,
            )
            if trade is None:
                break

            record_trade(state, trade)
            trade_count += trade.quantity

    return trade_count


def sweep_cross_currency(state: RebalanceState, acct) -> int:
    """Convert stranded cash in the wrong currency and buy the best position."""
    trade_count = 0

    for source_currency in ["CAD", "USD"]:
        for _ in range(50):
            trade = build_cross_currency_buy(
                acct,
                state.available_cash,
                state.holdings_view,
                state.effective_drift,
                state.transient_symbols,
                state.total_value,
                state.usd_to_cad_rate,
                source_currency,
                state.norberts_gambit_fee_cad,
                state.drift_trade_threshold_pct,
                note="Requires currency conversion",
            )
            if trade is None:
                break

            record_trade(state, trade)
            trade_count += trade.quantity

    return trade_count