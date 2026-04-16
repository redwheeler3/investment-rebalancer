"""Step-oriented rebalancer logic.

These helpers contain the sell / buy / sweep phases of the iterative
rebalancing algorithm.
"""

import math

from src.rebalancer_core import (
    TOLERANCE_PCT,
    RebalanceState,
    deduct_buy,
    effective_cash,
    record_trade,
    shares_for_drift,
)
from src.rules import (
    TradeRecommendation,
    allocate_sell,
    effective_qty,
    find_accounts_for_symbol,
)


def sweep_candidates(state: RebalanceState, acct, currency: str, max_price: float = None) -> list:
    """Find buyable positions in an account for the given currency."""
    candidates = []
    for pos in acct.positions:
        if pos.currency != currency or pos.quantity <= 0:
            continue
        if pos.symbol in state.transient_symbols:
            continue
        if state.targets.get(pos.symbol, 0) <= 0 and pos.symbol not in ("CAD", "USD"):
            continue

        holding = state.holdings_view.get(pos.symbol)
        if not holding:
            continue

        ask_price_native = holding.ask_price or pos.current_price
        if ask_price_native <= 0:
            continue
        if max_price is not None and ask_price_native > max_price:
            continue

        drift_pct = state.effective_drift.get(pos.symbol, 0)
        candidates.append((pos.symbol, drift_pct, ask_price_native))

    candidates.sort(key=lambda item: item[1])
    return candidates


def is_effectively_single_currency_account(state: RebalanceState, acct) -> bool:
    """Whether an account effectively holds only one position currency."""
    currencies = {
        pos.currency
        for pos in acct.positions
        if pos.quantity > 0 and pos.symbol not in state.transient_symbols
    }
    return len(currencies) == 1


def step_sell_overweight(state: RebalanceState) -> int:
    """Sell overweight positions to bring them toward target."""
    trade_count = 0
    overweight_symbols = [
        (symbol, drift_pct)
        for symbol, drift_pct in state.effective_drift.items()
        if symbol not in ("CAD", "USD") and drift_pct > TOLERANCE_PCT
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
    underweight_symbols = [
        (symbol, drift_pct)
        for symbol, drift_pct in state.effective_drift.items()
        if symbol not in ("CAD", "USD") and drift_pct < -TOLERANCE_PCT
    ]
    underweight_symbols.sort(key=lambda item: item[1])

    for symbol, _initial_drift in underweight_symbols:
        drift_pct = state.effective_drift.get(symbol, 0)
        if drift_pct >= -TOLERANCE_PCT:
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
    """Sell another position in this account to raise cash for a buy."""
    trade_count = 0
    displacement_candidates = []

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
        displacement_candidates.append((pos.symbol, drift_pct, bid_price_native))

    if not displacement_candidates:
        return 0

    displacement_candidates.sort(key=lambda item: item[1], reverse=True)
    for candidate_symbol, candidate_drift_pct, candidate_bid_native in displacement_candidates:
        if remaining_shares <= 0:
            break

        is_overweight = candidate_drift_pct > TOLERANCE_PCT
        if not is_overweight:
            holders = find_accounts_for_symbol(candidate_symbol, state.portfolio.accounts)
            if len(holders) <= 1:
                continue

        acct_cash_native = max(0, state.available_cash.get(acct.number, {}).get(currency, 0))
        shortfall_native = remaining_shares * buy_ask_native - acct_cash_native
        if shortfall_native <= 0:
            break

        sell_quantity = int(math.ceil(shortfall_native / candidate_bid_native))
        held_quantity = effective_qty(acct, candidate_symbol, state.position_deltas)
        sell_quantity = min(sell_quantity, held_quantity)
        if sell_quantity <= 0:
            continue

        note = "" if is_overweight else "Displacement sell"
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
            note=note,
        )
        state.available_cash[acct.number][currency] += sell_trade.estimated_value
        record_trade(state, sell_trade)
        trade_count += 1

    return trade_count


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
            acct_cash_native = state.available_cash.get(acct.number, {}).get(currency, 0)
            if acct_cash_native <= 0:
                break

            candidates = sweep_candidates(state, acct, currency, max_price=acct_cash_native)
            if not candidates:
                break

            best_symbol, best_drift_pct, best_ask_native = candidates[0]
            affordable_shares = int(math.floor(acct_cash_native / best_ask_native))
            if affordable_shares <= 0:
                break

            if best_drift_pct < -TOLERANCE_PCT:
                gap_cad = abs(best_drift_pct / 100.0) * state.total_value
                gap_native = gap_cad / state.usd_to_cad_rate if currency == "USD" else gap_cad
                shares = min(int(math.ceil(gap_native / best_ask_native)), affordable_shares)
            else:
                shares = affordable_shares

            if shares <= 0:
                break

            cost_native = best_ask_native * shares
            state.available_cash[acct.number][currency] -= cost_native
            record_trade(state, TradeRecommendation(
                symbol=best_symbol,
                action="BUY",
                quantity=shares,
                account_number=acct.number,
                account_type=acct.account_type,
                owner=acct.owner,
                price=best_ask_native,
                currency=currency,
                estimated_value=cost_native,
            ))
            trade_count += shares

    return trade_count


def sweep_cross_currency(state: RebalanceState, acct) -> int:
    """Convert stranded cash in the wrong currency and buy the best position."""
    trade_count = 0
    fee_cad = state.norberts_gambit_fee_cad
    single_currency_account = is_effectively_single_currency_account(state, acct)

    for source_currency in ["CAD", "USD"]:
        source_cash_native = state.available_cash.get(acct.number, {}).get(source_currency, 0)
        if source_cash_native <= 0:
            continue

        target_currency = "USD" if source_currency == "CAD" else "CAD"
        candidates = sweep_candidates(state, acct, target_currency)
        if not candidates:
            continue

        best_symbol, best_drift_pct, best_ask_native = candidates[0]
        if not single_currency_account and best_drift_pct >= -TOLERANCE_PCT:
            continue

        if source_currency == "CAD":
            buying_power_native = max(0, source_cash_native - fee_cad) / state.usd_to_cad_rate
        else:
            buying_power_native = max(0, source_cash_native * state.usd_to_cad_rate - fee_cad)

        shares = int(math.floor(buying_power_native / best_ask_native))
        if shares <= 0:
            continue

        cost_native = shares * best_ask_native
        if source_currency == "CAD":
            state.available_cash[acct.number]["CAD"] -= (
                cost_native * state.usd_to_cad_rate + fee_cad
            )
        else:
            state.available_cash[acct.number]["USD"] -= (
                cost_native + fee_cad
            ) / state.usd_to_cad_rate

        record_trade(state, TradeRecommendation(
            symbol=best_symbol,
            action="BUY",
            quantity=shares,
            account_number=acct.number,
            account_type=acct.account_type,
            owner=acct.owner,
            price=best_ask_native,
            currency=target_currency,
            estimated_value=cost_native,
            note="Requires currency conversion",
        ))
        trade_count += shares

    return trade_count