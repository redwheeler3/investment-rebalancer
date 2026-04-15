"""
Core rebalancing logic — Iterative Algorithm.

Each round applies three steps:
  Step A:  Sell overweight positions to bring them toward target
  Step B:  Buy underweight positions using available cash (same-currency,
           cross-currency via Norbert's Gambit, or displacement sells)
  Step C:  Sweep any remaining cash into the best available positions

Rounds repeat until all positions are within ±0.1% of target, no further
improvement is possible, or the safety limit is reached.

Uses bid price for sells, ask price for buys (least advantageous pricing).
Tracks per-account cash throughout all steps.
"""

import math
from src.portfolio import PortfolioSummary, get_current_allocations, get_drifts
from src.rules import (
    allocate_sell,
    find_accounts_for_symbol,
    get_position_quantity,
    TradeRecommendation,
)

# Drift tolerance — don't trade if drift is smaller than this
TOLERANCE_PCT = 0.1

# Norbert's Gambit fee: $9.99 + 5% GST = $10.49 CAD per conversion
NORBERTS_GAMBIT_FEE_CAD = 10.49

# Maximum optimisation rounds before stopping
MAX_ROUNDS = 10


def calculate_trades(
    portfolio: PortfolioSummary,
    targets: dict,
    usd_to_cad_rate: float,
    existing_only: bool = True,
    transient_symbols: list = None,
) -> list:
    """
    Calculate trades to rebalance the portfolio using an iterative algorithm.

    Repeats Sell → Buy → Sweep rounds until all positions are within
    tolerance or no further improvement is possible.

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

    # ── Per-account cash tracker (updated as trades are recorded) ──
    available_cash = {}
    for acct in portfolio.accounts:
        available_cash[acct.number] = {
            "CAD": acct.cash_cad,
            "USD": acct.cash_usd,
        }

    # ── Effective drift tracker (adjusted as trades change allocations) ──
    effective_drift = dict(drifts)

    # ── Helper functions ──

    def _apply_trade(symbol, value_cad, action):
        """Update effective drift after a trade."""
        pct = (value_cad / total_value) * 100.0
        if action == "SELL":
            effective_drift[symbol] = effective_drift.get(symbol, 0) - pct
        else:
            effective_drift[symbol] = effective_drift.get(symbol, 0) + pct

    def _to_cad(value, currency):
        return value * usd_to_cad_rate if currency == "USD" else value

    def _effective_cash(acct_number, buy_currency):
        """Total buying power: native cash + convertible from other currency.
        Accounts for the $10.49 CAD Norbert's Gambit fee in both directions."""
        native = max(0, available_cash.get(acct_number, {}).get(buy_currency, 0))
        if buy_currency == "USD":
            cad = max(0, available_cash.get(acct_number, {}).get("CAD", 0))
            convertible = max(0, cad - NORBERTS_GAMBIT_FEE_CAD) / usd_to_cad_rate
        else:
            usd = max(0, available_cash.get(acct_number, {}).get("USD", 0))
            convertible = max(0, usd * usd_to_cad_rate - NORBERTS_GAMBIT_FEE_CAD)
        return native + convertible

    def _deduct_buy(acct_number, cost, currency):
        """Deduct a buy cost: native currency first, then convert remainder.
        Returns True if cross-currency conversion was needed."""
        native = max(0, available_cash.get(acct_number, {}).get(currency, 0))
        if native >= cost:
            available_cash[acct_number][currency] -= cost
            return False
        # Use all native, convert the rest
        remainder = cost - native
        available_cash[acct_number][currency] = 0
        if currency == "USD":
            available_cash[acct_number]["CAD"] -= (
                remainder * usd_to_cad_rate + NORBERTS_GAMBIT_FEE_CAD
            )
        else:
            available_cash[acct_number]["USD"] -= (
                remainder + NORBERTS_GAMBIT_FEE_CAD
            ) / usd_to_cad_rate
        return True

    def _shares_for_drift(drift_pct, price, currency):
        """Calculate whole shares needed to close a drift gap."""
        gap_cad = abs(drift_pct / 100.0) * total_value
        gap_native = gap_cad / usd_to_cad_rate if currency == "USD" else gap_cad
        shares = int(math.floor(gap_native / price))
        # Expensive-stock fix: buy/sell 1 if it improves accuracy
        if shares == 0:
            one_cad = _to_cad(price, currency)
            if one_cad < 2 * gap_cad:
                shares = 1
        return shares

    # ══════════════════════════════════════════════════════════════════
    all_trades = []

    for _round in range(MAX_ROUNDS):
        round_count = 0

        # ──────────────────────────────────────────────────────────────
        # STEP A: Sell overweight positions
        # ──────────────────────────────────────────────────────────────
        overweight = [
            (sym, d)
            for sym, d in effective_drift.items()
            if sym not in ("CAD", "USD") and d > TOLERANCE_PCT
        ]
        overweight.sort(key=lambda x: x[1], reverse=True)  # Most overweight first

        for symbol, drift in overweight:
            holding = portfolio.holdings.get(symbol)
            if not holding:
                continue
            bid = holding.get("bid_price", holding["current_price"])
            currency = holding["currency"]
            if bid <= 0:
                continue

            shares = _shares_for_drift(drift, bid, currency)
            if shares <= 0:
                continue

            sell_trades = allocate_sell(
                symbol, shares, bid, currency, portfolio.accounts
            )
            for trade in sell_trades:
                all_trades.append(trade)
                available_cash[trade.account_number][currency] += trade.estimated_value
                _apply_trade(symbol, _to_cad(trade.estimated_value, currency), "SELL")
                round_count += 1

        # ──────────────────────────────────────────────────────────────
        # STEP B: Buy underweight positions
        # ──────────────────────────────────────────────────────────────
        # Unified: considers same-currency cash, cross-currency conversion,
        # and displacement sells — all in one pass.

        underweight = [
            (sym, d)
            for sym, d in effective_drift.items()
            if sym not in ("CAD", "USD") and d < -TOLERANCE_PCT
        ]
        underweight.sort(key=lambda x: x[1])  # Most underweight first

        for symbol, _initial_drift in underweight:
            # Re-check drift (earlier buys in this step may have changed it)
            drift = effective_drift.get(symbol, 0)
            if drift >= -TOLERANCE_PCT:
                continue

            holding = portfolio.holdings.get(symbol)
            if not holding:
                continue
            ask = holding.get("ask_price", holding["current_price"])
            currency = holding["currency"]
            if ask <= 0:
                continue

            shares_needed = _shares_for_drift(drift, ask, currency)
            if shares_needed <= 0:
                continue

            # Find eligible accounts
            if existing_only:
                eligible = find_accounts_for_symbol(symbol, portfolio.accounts)
            else:
                eligible = list(portfolio.accounts)
            if not eligible:
                continue

            # Sort by total effective cash (highest first)
            eligible.sort(
                key=lambda a: _effective_cash(a.number, currency),
                reverse=True,
            )

            remaining = shares_needed

            for acct in eligible:
                if remaining <= 0:
                    break

                # ── Try buying with available cash ──
                eff = _effective_cash(acct.number, currency)
                if eff >= ask:
                    affordable = int(math.floor(eff / ask))
                    qty = min(remaining, affordable)
                    if qty > 0:
                        cost = qty * ask
                        converted = _deduct_buy(acct.number, cost, currency)
                        all_trades.append(TradeRecommendation(
                            symbol=symbol,
                            action="BUY",
                            quantity=qty,
                            account_number=acct.number,
                            account_type=acct.account_type,
                            owner=acct.owner,
                            price=ask,
                            currency=currency,
                            estimated_value=cost,
                            note="Requires currency conversion" if converted else "",
                        ))
                        remaining -= qty
                        _apply_trade(symbol, _to_cad(cost, currency), "BUY")
                        round_count += qty
                    continue  # Move to next account

                # ── Not enough cash — try displacement sell ──
                # Sell another position in this account to raise cash.
                # Prefer overweight positions; accept at-target if held
                # in multiple accounts (so next round can repair).
                candidates = []
                for pos in acct.positions:
                    if pos.symbol == symbol or pos.symbol in transient_symbols:
                        continue
                    if pos.currency != currency or pos.quantity <= 0:
                        continue
                    h = portfolio.holdings.get(pos.symbol)
                    if not h:
                        continue
                    cbid = h.get("bid_price", pos.current_price)
                    if cbid <= 0:
                        continue
                    cdrift = effective_drift.get(pos.symbol, 0)
                    candidates.append((pos.symbol, cdrift, cbid))

                if not candidates:
                    continue

                # Most overweight first (best displacement targets)
                candidates.sort(key=lambda x: x[1], reverse=True)

                for cand_sym, cand_drift, cand_bid in candidates:
                    if remaining <= 0:
                        break

                    is_overweight = cand_drift > TOLERANCE_PCT

                    # Skip at-target positions held in only 1 account
                    # (can't be repaired in next round)
                    if not is_overweight:
                        holders = find_accounts_for_symbol(
                            cand_sym, portfolio.accounts
                        )
                        if len(holders) <= 1:
                            continue

                    # Calculate how many to sell
                    acct_cash = max(0, available_cash.get(acct.number, {}).get(currency, 0))
                    shortfall = remaining * ask - acct_cash
                    if shortfall <= 0:
                        break  # Enough cash already

                    sell_qty = int(math.ceil(shortfall / cand_bid))
                    held = int(get_position_quantity(acct, cand_sym))
                    sell_qty = min(sell_qty, held)
                    if sell_qty <= 0:
                        continue

                    note = "" if is_overweight else "Displacement sell"
                    sell_trade = TradeRecommendation(
                        symbol=cand_sym,
                        action="SELL",
                        quantity=sell_qty,
                        account_number=acct.number,
                        account_type=acct.account_type,
                        owner=acct.owner,
                        price=cand_bid,
                        currency=currency,
                        estimated_value=cand_bid * sell_qty,
                        note=note,
                    )
                    all_trades.append(sell_trade)
                    available_cash[acct.number][currency] += sell_trade.estimated_value
                    _apply_trade(cand_sym, _to_cad(sell_trade.estimated_value, currency), "SELL")
                    round_count += 1

                # Now buy with the freed cash
                acct_cash = max(0, available_cash.get(acct.number, {}).get(currency, 0))
                if acct_cash >= ask and remaining > 0:
                    affordable = int(math.floor(acct_cash / ask))
                    qty = min(remaining, affordable)
                    if qty > 0:
                        cost = qty * ask
                        available_cash[acct.number][currency] -= cost
                        all_trades.append(TradeRecommendation(
                            symbol=symbol,
                            action="BUY",
                            quantity=qty,
                            account_number=acct.number,
                            account_type=acct.account_type,
                            owner=acct.owner,
                            price=ask,
                            currency=currency,
                            estimated_value=cost,
                        ))
                        remaining -= qty
                        _apply_trade(symbol, _to_cad(cost, currency), "BUY")
                        round_count += qty

        # ──────────────────────────────────────────────────────────────
        # STEP C: Deploy remaining cash (sweep)
        # ──────────────────────────────────────────────────────────────
        # Since cash target is 0%, buy the most underweight (or least
        # overweight) existing position in each account.

        for acct in portfolio.accounts:
            # Same-currency sweep (iterative to spread across positions)
            for currency in ["CAD", "USD"]:
                for _ in range(50):
                    acct_cash = available_cash.get(acct.number, {}).get(currency, 0)
                    if acct_cash <= 0:
                        break

                    cands = []
                    for pos in acct.positions:
                        if pos.currency != currency or pos.quantity <= 0:
                            continue
                        if pos.symbol in transient_symbols:
                            continue
                        h = portfolio.holdings.get(pos.symbol)
                        if not h:
                            continue
                        a = h.get("ask_price", pos.current_price)
                        if a <= 0 or a > acct_cash:
                            continue
                        cands.append((pos.symbol, effective_drift.get(pos.symbol, 0), a))

                    if not cands:
                        break

                    cands.sort(key=lambda x: x[1])
                    sym, d, a = cands[0]

                    affordable = int(math.floor(acct_cash / a))
                    if affordable <= 0:
                        break

                    # Cap at drift gap if underweight, else dump remaining
                    if d < -TOLERANCE_PCT:
                        gap_cad = abs(d / 100.0) * total_value
                        gap_n = gap_cad / usd_to_cad_rate if currency == "USD" else gap_cad
                        shares = min(int(math.ceil(gap_n / a)), affordable)
                    else:
                        shares = affordable

                    if shares <= 0:
                        break

                    cost = a * shares
                    all_trades.append(TradeRecommendation(
                        symbol=sym,
                        action="BUY",
                        quantity=shares,
                        account_number=acct.number,
                        account_type=acct.account_type,
                        owner=acct.owner,
                        price=a,
                        currency=currency,
                        estimated_value=cost,
                    ))
                    available_cash[acct.number][currency] -= cost
                    _apply_trade(sym, _to_cad(cost, currency), "BUY")
                    round_count += shares

            # Cross-currency sweep (stranded cash in the wrong currency)
            for cash_cur in ["CAD", "USD"]:
                acct_cash = available_cash.get(acct.number, {}).get(cash_cur, 0)
                if acct_cash <= 0:
                    continue

                buy_cur = "USD" if cash_cur == "CAD" else "CAD"
                cands = []
                for pos in acct.positions:
                    if pos.currency != buy_cur or pos.quantity <= 0:
                        continue
                    if pos.symbol in transient_symbols:
                        continue
                    h = portfolio.holdings.get(pos.symbol)
                    if not h:
                        continue
                    a = h.get("ask_price", pos.current_price)
                    if a <= 0:
                        continue
                    cands.append((pos.symbol, effective_drift.get(pos.symbol, 0), a))

                if not cands:
                    continue

                cands.sort(key=lambda x: x[1])
                sym, _, a = cands[0]

                if cash_cur == "CAD":
                    eff_buy = max(0, acct_cash - NORBERTS_GAMBIT_FEE_CAD) / usd_to_cad_rate
                else:
                    eff_buy = max(0, acct_cash * usd_to_cad_rate - NORBERTS_GAMBIT_FEE_CAD)

                shares = int(math.floor(eff_buy / a))
                if shares <= 0:
                    continue

                cost = shares * a
                all_trades.append(TradeRecommendation(
                    symbol=sym,
                    action="BUY",
                    quantity=shares,
                    account_number=acct.number,
                    account_type=acct.account_type,
                    owner=acct.owner,
                    price=a,
                    currency=buy_cur,
                    estimated_value=cost,
                    note="Requires currency conversion",
                ))
                if cash_cur == "CAD":
                    available_cash[acct.number]["CAD"] -= (
                        cost * usd_to_cad_rate + NORBERTS_GAMBIT_FEE_CAD
                    )
                else:
                    available_cash[acct.number]["USD"] -= (
                        cost + NORBERTS_GAMBIT_FEE_CAD
                    ) / usd_to_cad_rate

                _apply_trade(sym, _to_cad(cost, buy_cur), "BUY")
                round_count += shares

        # ── Check: any more work to do? ──
        if round_count == 0:
            break

    # ══════════════════════════════════════════════════════════════════
    # POST-PROCESSING: Net and consolidate trades
    # ══════════════════════════════════════════════════════════════════
    # Multiple rounds may generate buys and sells for the same
    # (symbol, account). Net them into a single trade.

    position_map = {}  # (symbol, account_number) -> list of trades
    for trade in all_trades:
        key = (trade.symbol, trade.account_number)
        position_map.setdefault(key, []).append(trade)

    final_trades = []
    for (symbol, acct_num), trades_list in position_map.items():
        total_buy = 0
        total_sell = 0
        buy_price = 0
        sell_price = 0
        template = trades_list[0]
        buy_note = ""
        sell_note = ""

        for t in trades_list:
            if t.action == "BUY":
                total_buy += t.quantity
                buy_price = t.price
                if t.note and not buy_note:
                    buy_note = t.note
            else:
                total_sell += t.quantity
                sell_price = t.price
                if t.note and not sell_note:
                    sell_note = t.note

        net = total_buy - total_sell

        if net > 0:
            price = buy_price if buy_price > 0 else template.price
            final_trades.append(TradeRecommendation(
                symbol=symbol,
                action="BUY",
                quantity=net,
                account_number=acct_num,
                account_type=template.account_type,
                owner=template.owner,
                price=price,
                currency=template.currency,
                estimated_value=price * net,
                note=buy_note,
            ))
        elif net < 0:
            price = sell_price if sell_price > 0 else template.price
            final_trades.append(TradeRecommendation(
                symbol=symbol,
                action="SELL",
                quantity=abs(net),
                account_number=acct_num,
                account_type=template.account_type,
                owner=template.owner,
                price=price,
                currency=template.currency,
                estimated_value=price * abs(net),
                note=sell_note,
            ))
        # net == 0: trades cancel out — omit

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

    # Adjust for conversion rounding
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
