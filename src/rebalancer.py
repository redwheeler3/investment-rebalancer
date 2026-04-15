"""
Core rebalancing logic — Multi-Pass Algorithm.

Phase 1:  Calculate portfolio-wide needs (drifts, sells/buys lists)
Phase 2:  Direct sells — sell overweight positions, credit cash per account
Phase 3:  Direct buys — buy underweight positions using available cash (easy wins)
Phase 4:  Cash-raising sells — for unfulfilled buys, sell something in the target
          account to raise cash (prefer overweight, accept at-target with displacement)
Phase 5:  Displacement buys — re-buy displaced at-target positions in other accounts
Phase 6:  Cross-currency buys — fulfill remaining buys using convertible cash
Phase 7a: Same-currency cash sweep — deploy excess cash into existing positions
Phase 7b: Cross-currency cash sweep — deploy stranded cash via currency conversion
Phase 8:  Compensating sells — sell positions over-bought by 7b in other accounts,
          then deploy the freed cash into underweight positions

Uses bid price for sells, ask price for buys (least advantageous pricing).
Tracks per-account cash throughout all phases.
"""

import math
from src.portfolio import PortfolioSummary, get_current_allocations, get_drifts
from src.rules import (
    allocate_sell,
    find_accounts_for_symbol,
    get_position_quantity,
    TradeRecommendation,
)

# Drift tolerance — don't bother with drifts smaller than this
TOLERANCE_PCT = 0.1

# Norbert's Gambit fee: $9.99 + 5% GST = $10.49 CAD per conversion
NORBERTS_GAMBIT_FEE_CAD = 10.49


def calculate_trades(
    portfolio: PortfolioSummary,
    targets: dict,
    usd_to_cad_rate: float,
    existing_only: bool = True,
    transient_symbols: list = None,
) -> list:
    """
    Calculate the trades needed to rebalance the portfolio using a multi-pass
    algorithm that raises cash within accounts when needed.

    Args:
        portfolio: Aggregated portfolio summary.
        targets: Target allocation percentages by symbol.
        usd_to_cad_rate: Current USD/CAD exchange rate.
        existing_only: If True, only trade in accounts that already hold the position.
        transient_symbols: Symbols to skip (e.g., DLR.TO, DLR.U.TO).

    Returns:
        List of TradeRecommendation objects.
    """
    if transient_symbols is None:
        transient_symbols = []

    current_alloc = get_current_allocations(portfolio, usd_to_cad_rate)
    drifts = get_drifts(current_alloc, targets)
    total_value = portfolio.total_value_cad

    if total_value == 0:
        return []

    # ── Build per-account available cash tracker ──
    # Updated throughout all phases as sells credit and buys debit cash
    available_cash = {}
    for acct in portfolio.accounts:
        available_cash[acct.number] = {
            "CAD": acct.cash_cad,
            "USD": acct.cash_usd,
        }

    # ── Track effective drift per symbol ──
    # Starts as the calculated drift, then adjusted as trades are made
    effective_drift = dict(drifts)

    def _apply_trade_to_drift(symbol, value_cad, action):
        """Adjust effective drift after a trade is recorded."""
        drift_change_pct = (value_cad / total_value) * 100.0
        if action == "SELL":
            effective_drift[symbol] = effective_drift.get(symbol, 0) - drift_change_pct
        else:  # BUY
            effective_drift[symbol] = effective_drift.get(symbol, 0) + drift_change_pct

    def _to_cad(value, currency):
        """Convert a native-currency value to CAD."""
        return value * usd_to_cad_rate if currency == "USD" else value

    # ══════════════════════════════════════════════════════════════════
    # PHASE 1: Compute needed sells and buys from portfolio-wide drifts
    # ══════════════════════════════════════════════════════════════════
    sells_needed = []  # (symbol, shares, bid_price, currency)
    buys_needed = []   # (symbol, shares, ask_price, currency)

    for symbol, drift_pct in drifts.items():
        if symbol in ("CAD", "USD"):
            continue
        if abs(drift_pct) < TOLERANCE_PCT:
            continue

        holding = portfolio.holdings.get(symbol)
        if not holding:
            continue

        bid_price = holding.get("bid_price", holding["current_price"])
        ask_price = holding.get("ask_price", holding["current_price"])
        currency = holding["currency"]

        if bid_price <= 0 or ask_price <= 0:
            continue

        # Dollar amount of drift in native currency
        drift_value_cad = (drift_pct / 100.0) * total_value
        if currency == "USD":
            drift_value_native = drift_value_cad / usd_to_cad_rate
        else:
            drift_value_native = drift_value_cad

        if drift_pct < -TOLERANCE_PCT:
            # Underweight → BUY (use ask price)
            shares = int(math.floor(abs(drift_value_native) / ask_price))
            if shares > 0:
                buys_needed.append((symbol, shares, ask_price, currency))
        elif drift_pct > TOLERANCE_PCT:
            # Overweight → SELL (use bid price)
            shares = int(math.floor(abs(drift_value_native) / bid_price))
            if shares > 0:
                sells_needed.append((symbol, shares, bid_price, currency))

    # Sort buys by drift magnitude (most underweight first = biggest priority)
    buys_needed.sort(key=lambda x: drifts.get(x[0], 0))
    # Sort sells by drift magnitude (most overweight first)
    sells_needed.sort(key=lambda x: drifts.get(x[0], 0), reverse=True)

    all_trades = []

    # ══════════════════════════════════════════════════════════════════
    # PHASE 2: Direct sells — sell overweight positions
    # ══════════════════════════════════════════════════════════════════
    for symbol, shares, price, currency in sells_needed:
        sell_trades = allocate_sell(
            symbol=symbol,
            total_shares=shares,
            price=price,
            currency=currency,
            accounts=portfolio.accounts,
        )
        for trade in sell_trades:
            all_trades.append(trade)
            available_cash[trade.account_number][trade.currency] += trade.estimated_value
            _apply_trade_to_drift(trade.symbol, _to_cad(trade.estimated_value, trade.currency), "SELL")

    # ══════════════════════════════════════════════════════════════════
    # PHASE 3: Direct buys — easy wins using available cash
    # ══════════════════════════════════════════════════════════════════
    unfulfilled_buys = []

    for symbol, total_shares, price, currency in buys_needed:
        if total_shares <= 0:
            continue

        if existing_only:
            eligible = find_accounts_for_symbol(symbol, portfolio.accounts)
        else:
            eligible = list(portfolio.accounts)

        if not eligible:
            continue

        # Prefer accounts with the most available cash
        eligible.sort(
            key=lambda a: available_cash.get(a.number, {}).get(currency, 0),
            reverse=True,
        )

        remaining = total_shares

        for acct in eligible:
            if remaining <= 0:
                break

            acct_cash = available_cash.get(acct.number, {}).get(currency, 0)
            if acct_cash <= 0:
                continue

            max_affordable = int(math.floor(acct_cash / price))
            shares_to_buy = min(remaining, max_affordable)

            if shares_to_buy > 0:
                trade = TradeRecommendation(
                    symbol=symbol,
                    action="BUY",
                    quantity=shares_to_buy,
                    account_number=acct.number,
                    account_type=acct.account_type,
                    owner=acct.owner,
                    price=price,
                    currency=currency,
                    estimated_value=price * shares_to_buy,
                )
                all_trades.append(trade)
                available_cash[acct.number][currency] -= trade.estimated_value
                remaining -= shares_to_buy
                _apply_trade_to_drift(symbol, _to_cad(trade.estimated_value, currency), "BUY")

        if remaining > 0:
            unfulfilled_buys.append((symbol, remaining, price, currency))

    # ══════════════════════════════════════════════════════════════════
    # PHASE 4: Cash-raising sells for unfulfilled buys
    # ══════════════════════════════════════════════════════════════════
    # For each buy we couldn't complete, look at accounts that hold the
    # symbol and sell something else in that account to raise cash.
    # Prefer selling overweight positions (two birds, one stone).
    # If nothing is overweight, sell an at-target position and track it
    # as a displacement to repair in Phase 5.

    displacements = []  # (symbol, shares_sold, bid_price, currency, source_account)
    still_unfulfilled = []  # Buys remaining after Phase 4 (may need currency conversion)

    for buy_symbol, shares_needed, buy_price, buy_currency in unfulfilled_buys:
        if shares_needed <= 0:
            continue

        # Find accounts that already hold the symbol we need to buy
        if existing_only:
            target_accounts = find_accounts_for_symbol(buy_symbol, portfolio.accounts)
        else:
            target_accounts = list(portfolio.accounts)

        if not target_accounts:
            continue

        # Try accounts with the most existing holdings of buy_symbol first
        # (they're the most natural home for this position)
        target_accounts.sort(
            key=lambda a: get_position_quantity(a, buy_symbol),
            reverse=True,
        )

        for acct in target_accounts:
            if shares_needed <= 0:
                break

            # Check if this account already has enough cash (edge case)
            acct_cash = available_cash.get(acct.number, {}).get(buy_currency, 0)
            if acct_cash >= buy_price:
                # Can buy at least one share — do it
                max_affordable = int(math.floor(acct_cash / buy_price))
                buy_qty = min(shares_needed, max_affordable)
                if buy_qty > 0:
                    trade = TradeRecommendation(
                        symbol=buy_symbol,
                        action="BUY",
                        quantity=buy_qty,
                        account_number=acct.number,
                        account_type=acct.account_type,
                        owner=acct.owner,
                        price=buy_price,
                        currency=buy_currency,
                        estimated_value=buy_price * buy_qty,
                    )
                    all_trades.append(trade)
                    available_cash[acct.number][buy_currency] -= trade.estimated_value
                    shares_needed -= buy_qty
                    _apply_trade_to_drift(buy_symbol, _to_cad(trade.estimated_value, buy_currency), "BUY")
                if shares_needed <= 0:
                    continue

            # ── Find candidates to sell in this account to raise cash ──
            # Must be: same currency, not the buy symbol, not transient, has shares
            candidates = []
            for pos in acct.positions:
                if pos.symbol == buy_symbol:
                    continue
                if pos.symbol in transient_symbols:
                    continue
                if pos.currency != buy_currency:
                    continue
                if pos.quantity <= 0:
                    continue

                holding_data = portfolio.holdings.get(pos.symbol)
                if not holding_data:
                    continue

                cand_bid = holding_data.get("bid_price", pos.current_price)
                if cand_bid <= 0:
                    continue

                cand_drift = effective_drift.get(pos.symbol, 0)
                candidates.append((pos, cand_drift, cand_bid))

            if not candidates:
                continue

            # Sort: most overweight first (prefer selling what's already overweight)
            candidates.sort(key=lambda x: x[1], reverse=True)

            # Raise cash by selling candidates
            for cand_pos, cand_drift, cand_bid in candidates:
                if shares_needed <= 0:
                    break

                # Recalculate shortfall (previous fundraiser sells may have freed cash)
                acct_cash = available_cash.get(acct.number, {}).get(buy_currency, 0)
                cost_for_remaining = shares_needed * buy_price
                cash_shortfall = cost_for_remaining - max(0, acct_cash)

                if cash_shortfall <= 0:
                    break  # Enough cash raised

                # How many shares to sell to cover the shortfall?
                shares_to_sell = math.ceil(cash_shortfall / cand_bid)
                held = int(get_position_quantity(acct, cand_pos.symbol))
                shares_to_sell = min(shares_to_sell, held)

                if shares_to_sell <= 0:
                    continue

                is_overweight = cand_drift > TOLERANCE_PCT

                # Don't displace single-account holdings — Phase 5
                # can't repair them (no other account to re-buy in)
                if not is_overweight:
                    other_holders = find_accounts_for_symbol(
                        cand_pos.symbol, portfolio.accounts
                    )
                    if len(other_holders) <= 1:
                        continue

                # Generate the fundraiser sell
                sell_note = ""
                if not is_overweight:
                    sell_note = "Displacement sell"

                sell_trade = TradeRecommendation(
                    symbol=cand_pos.symbol,
                    action="SELL",
                    quantity=shares_to_sell,
                    account_number=acct.number,
                    account_type=acct.account_type,
                    owner=acct.owner,
                    price=cand_bid,
                    currency=buy_currency,
                    estimated_value=cand_bid * shares_to_sell,
                    note=sell_note,
                )
                all_trades.append(sell_trade)
                available_cash[acct.number][buy_currency] += sell_trade.estimated_value
                _apply_trade_to_drift(
                    cand_pos.symbol,
                    _to_cad(sell_trade.estimated_value, buy_currency),
                    "SELL",
                )

                # Track displacement for at-target positions
                if not is_overweight:
                    displacements.append((
                        cand_pos.symbol,
                        shares_to_sell,
                        cand_bid,
                        buy_currency,
                        acct.number,
                    ))

            # Now buy the target symbol with freed cash
            acct_cash = available_cash.get(acct.number, {}).get(buy_currency, 0)
            if acct_cash > 0 and shares_needed > 0:
                max_affordable = int(math.floor(acct_cash / buy_price))
                buy_qty = min(shares_needed, max_affordable)

                if buy_qty > 0:
                    trade = TradeRecommendation(
                        symbol=buy_symbol,
                        action="BUY",
                        quantity=buy_qty,
                        account_number=acct.number,
                        account_type=acct.account_type,
                        owner=acct.owner,
                        price=buy_price,
                        currency=buy_currency,
                        estimated_value=buy_price * buy_qty,
                    )
                    all_trades.append(trade)
                    available_cash[acct.number][buy_currency] -= trade.estimated_value
                    shares_needed -= buy_qty
                    _apply_trade_to_drift(
                        buy_symbol,
                        _to_cad(trade.estimated_value, buy_currency),
                        "BUY",
                    )

        # Track buys still unfulfilled after Phase 4 (may need currency conversion)
        if shares_needed > 0:
            still_unfulfilled.append((buy_symbol, shares_needed, buy_price, buy_currency))

    # ══════════════════════════════════════════════════════════════════
    # PHASE 5: Displacement buys — repair at-target sells
    # ══════════════════════════════════════════════════════════════════
    # When Phase 4 sold an at-target position to raise cash, we need
    # to re-buy those shares in another account that already holds the
    # symbol and has available cash. One level deep only.

    for disp_symbol, disp_shares, _disp_bid, disp_currency, source_acct in displacements:
        if disp_shares <= 0:
            continue

        holding_data = portfolio.holdings.get(disp_symbol)
        if not holding_data:
            continue

        disp_ask = holding_data.get("ask_price", holding_data["current_price"])
        if disp_ask <= 0:
            continue

        # Find accounts that hold this symbol (excluding the source account)
        eligible = find_accounts_for_symbol(disp_symbol, portfolio.accounts)
        eligible = [a for a in eligible if a.number != source_acct]

        if not eligible:
            # No other account holds it — accept the small drift
            continue

        # Prefer accounts with the most available cash
        eligible.sort(
            key=lambda a: available_cash.get(a.number, {}).get(disp_currency, 0),
            reverse=True,
        )

        remaining = disp_shares

        for acct in eligible:
            if remaining <= 0:
                break

            acct_cash = available_cash.get(acct.number, {}).get(disp_currency, 0)
            if acct_cash <= 0:
                continue

            max_affordable = int(math.floor(acct_cash / disp_ask))
            buy_qty = min(remaining, max_affordable)

            if buy_qty > 0:
                trade = TradeRecommendation(
                    symbol=disp_symbol,
                    action="BUY",
                    quantity=buy_qty,
                    account_number=acct.number,
                    account_type=acct.account_type,
                    owner=acct.owner,
                    price=disp_ask,
                    currency=disp_currency,
                    estimated_value=disp_ask * buy_qty,
                    note="Displacement buy",
                )
                all_trades.append(trade)
                available_cash[acct.number][disp_currency] -= trade.estimated_value
                remaining -= buy_qty
                _apply_trade_to_drift(
                    disp_symbol,
                    _to_cad(trade.estimated_value, disp_currency),
                    "BUY",
                )

    # ══════════════════════════════════════════════════════════════════
    # PHASE 6: Cross-currency buys for remaining unfulfilled positions
    # ══════════════════════════════════════════════════════════════════
    # If a buy couldn't be fulfilled because the account has cash in
    # the wrong currency (e.g., need USD for IVV but only have CAD),
    # generate the buy trade anyway. The currency conversion module
    # will detect the mismatch and recommend Norbert's Gambit (DLR.TO).
    # We consider total effective cash: native + convertible from the
    # other currency.

    for buy_symbol, shares_needed, buy_price, buy_currency in still_unfulfilled:
        if shares_needed <= 0:
            continue

        if existing_only:
            eligible = find_accounts_for_symbol(buy_symbol, portfolio.accounts)
        else:
            eligible = list(portfolio.accounts)

        if not eligible:
            continue

        # Calculate effective cash per account (native + convertible)
        def _effective_cash(acct):
            native = max(0, available_cash.get(acct.number, {}).get(buy_currency, 0))
            if buy_currency == "USD":
                cad_avail = max(0, available_cash.get(acct.number, {}).get("CAD", 0))
                convertible = max(0, cad_avail - NORBERTS_GAMBIT_FEE_CAD) / usd_to_cad_rate
            else:
                usd_avail = max(0, available_cash.get(acct.number, {}).get("USD", 0))
                convertible = usd_avail * usd_to_cad_rate
            return native + convertible

        # Sort by total effective cash (most cash first)
        eligible.sort(key=_effective_cash, reverse=True)

        remaining = shares_needed

        for acct in eligible:
            if remaining <= 0:
                break

            eff_cash = _effective_cash(acct)
            if eff_cash < buy_price:
                continue

            max_affordable = int(math.floor(eff_cash / buy_price))
            buy_qty = min(remaining, max_affordable)

            if buy_qty > 0:
                cost = buy_qty * buy_price
                native_cash = max(0, available_cash.get(acct.number, {}).get(buy_currency, 0))
                needs_conversion = native_cash < cost

                trade = TradeRecommendation(
                    symbol=buy_symbol,
                    action="BUY",
                    quantity=buy_qty,
                    account_number=acct.number,
                    account_type=acct.account_type,
                    owner=acct.owner,
                    price=buy_price,
                    currency=buy_currency,
                    estimated_value=cost,
                    note="Requires currency conversion" if needs_conversion else "",
                )
                all_trades.append(trade)

                # Deduct: native currency first, then other currency for the remainder
                if native_cash >= cost:
                    available_cash[acct.number][buy_currency] -= cost
                else:
                    remainder = cost - native_cash
                    available_cash[acct.number][buy_currency] = 0
                    if buy_currency == "USD":
                        cad_cost = remainder * usd_to_cad_rate + NORBERTS_GAMBIT_FEE_CAD
                        available_cash[acct.number]["CAD"] -= cad_cost
                    else:
                        usd_cost = remainder / usd_to_cad_rate
                        available_cash[acct.number]["USD"] -= usd_cost

                remaining -= buy_qty
                _apply_trade_to_drift(
                    buy_symbol,
                    _to_cad(cost, buy_currency),
                    "BUY",
                )

    # ══════════════════════════════════════════════════════════════════
    # PHASE 7a: Deploy remaining cash — same-currency sweep
    # ══════════════════════════════════════════════════════════════════
    # After all phases, some accounts may have excess cash from sells
    # that couldn't be fully redeployed. Since cash target is 0%,
    # buy the least-overweight (or most-underweight) existing position.
    # Iterative: buy only enough to close each symbol's drift gap, then
    # re-evaluate so cash is spread across multiple underweight positions.

    for acct in portfolio.accounts:
        for currency in ["CAD", "USD"]:
            # Iterative sweep: buy the most underweight, re-evaluate, repeat
            for _sweep in range(50):  # Safety limit
                acct_cash = available_cash.get(acct.number, {}).get(currency, 0)
                if acct_cash <= 0:
                    break

                # Find buyable positions in this account (same currency)
                candidates = []
                for pos in acct.positions:
                    if pos.currency != currency or pos.quantity <= 0:
                        continue
                    if pos.symbol in transient_symbols:
                        continue
                    holding = portfolio.holdings.get(pos.symbol)
                    if not holding:
                        continue
                    ask = holding.get("ask_price", pos.current_price)
                    if ask <= 0 or ask > acct_cash:
                        continue
                    drift = effective_drift.get(pos.symbol, 0)
                    candidates.append((pos.symbol, drift, ask))

                if not candidates:
                    break

                # Buy the most underweight (or least overweight) position
                candidates.sort(key=lambda x: x[1])
                symbol, drift, ask = candidates[0]

                max_affordable = int(math.floor(acct_cash / ask))
                if max_affordable <= 0:
                    break

                # Limit buy to what's needed to close this symbol's drift gap
                if drift < -TOLERANCE_PCT:
                    gap_cad = abs(drift / 100.0) * total_value
                    gap_native = gap_cad / usd_to_cad_rate if currency == "USD" else gap_cad
                    gap_shares = int(math.ceil(gap_native / ask))
                    shares = min(gap_shares, max_affordable)
                else:
                    # At or above target — dump remaining cash here
                    shares = max_affordable

                if shares <= 0:
                    break

                trade = TradeRecommendation(
                    symbol=symbol,
                    action="BUY",
                    quantity=shares,
                    account_number=acct.number,
                    account_type=acct.account_type,
                    owner=acct.owner,
                    price=ask,
                    currency=currency,
                    estimated_value=ask * shares,
                )
                all_trades.append(trade)
                available_cash[acct.number][currency] -= trade.estimated_value
                _apply_trade_to_drift(symbol, _to_cad(trade.estimated_value, currency), "BUY")

    # ══════════════════════════════════════════════════════════════════
    # PHASE 7b: Deploy remaining cash — cross-currency sweep
    # ══════════════════════════════════════════════════════════════════
    # Accounts with cash in one currency but only positions in the other
    # (e.g., CAD cash in an RRSP that only holds IVV/USD). Convert and
    # buy the best available position. Tracks overflow for Phase 8.

    overflow_buys = set()  # (symbol, account_number) — for Phase 8

    for acct in portfolio.accounts:
        for cash_currency in ["CAD", "USD"]:
            acct_cash = available_cash.get(acct.number, {}).get(cash_currency, 0)
            if acct_cash <= 0:
                continue

            buy_currency = "USD" if cash_currency == "CAD" else "CAD"

            # Find buyable positions in the OTHER currency
            candidates = []
            for pos in acct.positions:
                if pos.currency != buy_currency or pos.quantity <= 0:
                    continue
                if pos.symbol in transient_symbols:
                    continue
                holding = portfolio.holdings.get(pos.symbol)
                if not holding:
                    continue
                ask = holding.get("ask_price", pos.current_price)
                if ask <= 0:
                    continue
                drift = effective_drift.get(pos.symbol, 0)
                candidates.append((pos.symbol, drift, ask))

            if not candidates:
                continue

            # Buy the most underweight (or least overweight)
            candidates.sort(key=lambda x: x[1])
            symbol, _drift, ask = candidates[0]

            # Calculate effective buying power after conversion
            if cash_currency == "CAD":
                effective_buy = max(0, acct_cash - NORBERTS_GAMBIT_FEE_CAD) / usd_to_cad_rate
            else:
                effective_buy = acct_cash * usd_to_cad_rate

            shares = int(math.floor(effective_buy / ask))
            if shares <= 0:
                continue

            cost = shares * ask
            trade = TradeRecommendation(
                symbol=symbol,
                action="BUY",
                quantity=shares,
                account_number=acct.number,
                account_type=acct.account_type,
                owner=acct.owner,
                price=ask,
                currency=buy_currency,
                estimated_value=cost,
                note="Requires currency conversion",
            )
            all_trades.append(trade)
            overflow_buys.add((symbol, acct.number))

            # Deduct from source currency
            if cash_currency == "CAD":
                cad_cost = cost * usd_to_cad_rate + NORBERTS_GAMBIT_FEE_CAD
                available_cash[acct.number]["CAD"] -= cad_cost
            else:
                usd_cost = cost / usd_to_cad_rate
                available_cash[acct.number]["USD"] -= usd_cost

            _apply_trade_to_drift(symbol, _to_cad(cost, buy_currency), "BUY")

    # ══════════════════════════════════════════════════════════════════
    # PHASE 8: Compensating sells for overflow buys
    # ══════════════════════════════════════════════════════════════════
    # Phase 7b may have over-bought a position to deploy stranded cash
    # (e.g., bought extra IVV in an RRSP that only holds IVV). Sell
    # the overage in OTHER accounts, then deploy the freed cash into
    # underweight positions there.

    if overflow_buys:
        overflow_symbols = set(sym for sym, _ in overflow_buys)
        overflow_accounts = set(acct_num for _, acct_num in overflow_buys)

        # ── Phase 8a: Compensating sells ──
        for symbol in overflow_symbols:
            sym_drift = effective_drift.get(symbol, 0)
            if sym_drift <= TOLERANCE_PCT:
                continue  # Not overweight — no sell needed

            holding = portfolio.holdings.get(symbol)
            if not holding:
                continue

            bid = holding.get("bid_price", holding["current_price"])
            currency = holding["currency"]
            if bid <= 0:
                continue

            # How many shares to sell to bring drift back toward target
            excess_cad = (sym_drift / 100.0) * total_value
            excess_native = excess_cad / usd_to_cad_rate if currency == "USD" else excess_cad
            shares_to_sell = int(math.floor(excess_native / bid))
            if shares_to_sell <= 0:
                continue

            # Sell from accounts that hold it (excluding overflow sources)
            holders = find_accounts_for_symbol(symbol, portfolio.accounts)
            holders = [a for a in holders if a.number not in overflow_accounts]
            holders.sort(
                key=lambda a: get_position_quantity(a, symbol), reverse=True
            )

            remaining = shares_to_sell
            for acct in holders:
                if remaining <= 0:
                    break
                held = int(get_position_quantity(acct, symbol))
                sell_qty = min(remaining, held)
                if sell_qty > 0:
                    trade = TradeRecommendation(
                        symbol=symbol,
                        action="SELL",
                        quantity=sell_qty,
                        account_number=acct.number,
                        account_type=acct.account_type,
                        owner=acct.owner,
                        price=bid,
                        currency=currency,
                        estimated_value=bid * sell_qty,
                    )
                    all_trades.append(trade)
                    available_cash[acct.number][currency] += trade.estimated_value
                    remaining -= sell_qty
                    _apply_trade_to_drift(
                        symbol, _to_cad(trade.estimated_value, currency), "SELL"
                    )

        # ── Phase 8b: Deploy freed cash from compensating sells ──
        for acct in portfolio.accounts:
            if acct.number in overflow_accounts:
                continue

            # Same-currency deployment
            for currency in ["CAD", "USD"]:
                acct_cash = available_cash.get(acct.number, {}).get(currency, 0)
                if acct_cash <= 0:
                    continue

                candidates = []
                for pos in acct.positions:
                    if pos.currency != currency or pos.quantity <= 0:
                        continue
                    if pos.symbol in transient_symbols:
                        continue
                    holding = portfolio.holdings.get(pos.symbol)
                    if not holding:
                        continue
                    ask = holding.get("ask_price", pos.current_price)
                    if ask <= 0 or ask > acct_cash:
                        continue
                    drift = effective_drift.get(pos.symbol, 0)
                    candidates.append((pos.symbol, drift, ask))

                if not candidates:
                    continue

                candidates.sort(key=lambda x: x[1])
                sym, _, ask = candidates[0]

                shares = int(math.floor(acct_cash / ask))
                if shares > 0:
                    trade = TradeRecommendation(
                        symbol=sym,
                        action="BUY",
                        quantity=shares,
                        account_number=acct.number,
                        account_type=acct.account_type,
                        owner=acct.owner,
                        price=ask,
                        currency=currency,
                        estimated_value=ask * shares,
                    )
                    all_trades.append(trade)
                    available_cash[acct.number][currency] -= trade.estimated_value
                    _apply_trade_to_drift(
                        sym, _to_cad(trade.estimated_value, currency), "BUY"
                    )

            # Cross-currency deployment (e.g., USD freed from selling IVV
            # used to buy CAD positions via USD→CAD conversion)
            for cash_currency in ["CAD", "USD"]:
                acct_cash = available_cash.get(acct.number, {}).get(cash_currency, 0)
                if acct_cash <= 0:
                    continue

                buy_currency = "USD" if cash_currency == "CAD" else "CAD"

                candidates = []
                for pos in acct.positions:
                    if pos.currency != buy_currency or pos.quantity <= 0:
                        continue
                    if pos.symbol in transient_symbols:
                        continue
                    holding = portfolio.holdings.get(pos.symbol)
                    if not holding:
                        continue
                    ask = holding.get("ask_price", pos.current_price)
                    if ask <= 0:
                        continue
                    drift = effective_drift.get(pos.symbol, 0)
                    candidates.append((pos.symbol, drift, ask))

                if not candidates:
                    continue

                candidates.sort(key=lambda x: x[1])
                sym, _, ask = candidates[0]

                if cash_currency == "CAD":
                    effective_buy = max(0, acct_cash - NORBERTS_GAMBIT_FEE_CAD) / usd_to_cad_rate
                else:
                    effective_buy = acct_cash * usd_to_cad_rate

                shares = int(math.floor(effective_buy / ask))
                if shares <= 0:
                    continue

                cost = shares * ask
                trade = TradeRecommendation(
                    symbol=sym,
                    action="BUY",
                    quantity=shares,
                    account_number=acct.number,
                    account_type=acct.account_type,
                    owner=acct.owner,
                    price=ask,
                    currency=buy_currency,
                    estimated_value=cost,
                    note="Requires currency conversion",
                )
                all_trades.append(trade)

                if cash_currency == "CAD":
                    cad_cost = cost * usd_to_cad_rate + NORBERTS_GAMBIT_FEE_CAD
                    available_cash[acct.number]["CAD"] -= cad_cost
                else:
                    usd_cost = cost / usd_to_cad_rate
                    available_cash[acct.number]["USD"] -= usd_cost

                _apply_trade_to_drift(
                    sym, _to_cad(cost, buy_currency), "BUY"
                )

    # ══════════════════════════════════════════════════════════════════
    # POST-PROCESSING: Net and consolidate trades
    # ══════════════════════════════════════════════════════════════════
    # Trades for the same symbol + account may have been generated as
    # both buys and sells across phases (e.g., sell 47 ENB + buy 60 ENB).
    # Net them into a single trade per (symbol, account).

    position_map = {}  # (symbol, account_number) -> list of trades
    for trade in all_trades:
        key = (trade.symbol, trade.account_number)
        if key not in position_map:
            position_map[key] = []
        position_map[key].append(trade)

    final_trades = []
    for (symbol, acct_num), trades_list in position_map.items():
        total_buy_qty = 0
        total_sell_qty = 0
        buy_price = 0
        sell_price = 0
        template = trades_list[0]
        buy_note = ""
        sell_note = ""

        for t in trades_list:
            if t.action == "BUY":
                total_buy_qty += t.quantity
                buy_price = t.price
                if t.note and not buy_note:
                    buy_note = t.note
            else:
                total_sell_qty += t.quantity
                sell_price = t.price
                if t.note and not sell_note:
                    sell_note = t.note

        net_qty = total_buy_qty - total_sell_qty

        if net_qty > 0:
            price = buy_price if buy_price > 0 else template.price
            final_trades.append(TradeRecommendation(
                symbol=symbol,
                action="BUY",
                quantity=net_qty,
                account_number=acct_num,
                account_type=template.account_type,
                owner=template.owner,
                price=price,
                currency=template.currency,
                estimated_value=price * net_qty,
                note=buy_note,
            ))
        elif net_qty < 0:
            price = sell_price if sell_price > 0 else template.price
            final_trades.append(TradeRecommendation(
                symbol=symbol,
                action="SELL",
                quantity=abs(net_qty),
                account_number=acct_num,
                account_type=template.account_type,
                owner=template.owner,
                price=price,
                currency=template.currency,
                estimated_value=price * abs(net_qty),
                note=sell_note,
            ))
        # If net_qty == 0: trades cancel out — omit entirely

    return final_trades


def simulate_rebalance(
    portfolio: PortfolioSummary,
    trades: list,
    targets: dict,
    usd_to_cad_rate: float,
) -> dict:
    """
    Simulate what the portfolio would look like after executing the trades.

    Useful for showing the projected accuracy improvement.

    Args:
        portfolio: Current portfolio state.
        trades: List of TradeRecommendation objects.
        targets: Target allocation percentages.
        usd_to_cad_rate: Current exchange rate.

    Returns:
        Dictionary with 'projected_allocations' and 'projected_accuracy'.
    """
    from src.portfolio import calculate_accuracy

    # Start with current holdings values
    projected_holdings = {}
    for symbol, data in portfolio.holdings.items():
        projected_holdings[symbol] = data["value_cad"]

    projected_cash_cad = portfolio.cash_cad_total
    projected_cash_usd = portfolio.cash_usd_total

    # Apply trades
    for trade in trades:
        price_cad = trade.price
        if trade.currency == "USD":
            price_cad = trade.price * usd_to_cad_rate

        value_change_cad = price_cad * trade.quantity
        needs_conversion = "currency conversion" in (trade.note or "").lower()

        if trade.action == "BUY":
            projected_holdings[trade.symbol] = projected_holdings.get(trade.symbol, 0) + value_change_cad
            if needs_conversion:
                # Currency conversion: deduct from the SOURCE currency
                if trade.currency == "USD":
                    projected_cash_cad -= trade.estimated_value * usd_to_cad_rate
                else:
                    projected_cash_usd -= trade.estimated_value / usd_to_cad_rate
            else:
                if trade.currency == "CAD":
                    projected_cash_cad -= trade.estimated_value
                else:
                    projected_cash_usd -= trade.estimated_value
        elif trade.action == "SELL":
            projected_holdings[trade.symbol] = projected_holdings.get(trade.symbol, 0) - value_change_cad
            if trade.currency == "CAD":
                projected_cash_cad += trade.estimated_value
            else:
                projected_cash_usd += trade.estimated_value

    # Adjust for conversion rounding: when a trade partially uses native
    # cash and partially needs conversion, the simulation may over-deduct
    # from one currency. In practice the conversion handles the exact split.
    if projected_cash_cad < 0:
        projected_cash_usd += projected_cash_cad / usd_to_cad_rate
        projected_cash_cad = 0
    if projected_cash_usd < 0:
        projected_cash_cad += projected_cash_usd * usd_to_cad_rate
        projected_cash_usd = 0

    # Calculate projected total value
    projected_total = projected_cash_cad + (projected_cash_usd * usd_to_cad_rate)
    for val in projected_holdings.values():
        projected_total += val

    # Calculate projected allocations
    projected_alloc = {}
    if projected_total > 0:
        projected_alloc["CAD"] = (projected_cash_cad / projected_total) * 100
        projected_alloc["USD"] = ((projected_cash_usd * usd_to_cad_rate) / projected_total) * 100
        for symbol, val in projected_holdings.items():
            projected_alloc[symbol] = (val / projected_total) * 100

    projected_accuracy = calculate_accuracy(projected_alloc, targets)

    return {
        "projected_allocations": projected_alloc,
        "projected_accuracy": projected_accuracy,
    }
