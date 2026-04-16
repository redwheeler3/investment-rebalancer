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
from dataclasses import dataclass, field
from src.portfolio import (
    PortfolioSummary,
    get_current_allocations,
    get_drifts,
    calculate_accuracy,
    get_holdings_view,
)
from src.rules import (
    allocate_sell,
    effective_qty,
    find_accounts_for_symbol,
    TradeRecommendation,
)

# Drift tolerance — don't trade if drift is smaller than this
TOLERANCE_PCT = 0.1

# Maximum optimisation rounds before stopping
MAX_ROUNDS = 10


# ══════════════════════════════════════════════════════════════════
# Shared state for a single rebalance run
# ══════════════════════════════════════════════════════════════════

@dataclass
class RebalanceState:
    """Mutable state passed through all rebalance steps.

    Keeps the helpers pure functions of explicit inputs rather than
    closures over half-a-dozen locals in one giant function.
    """

    portfolio: PortfolioSummary
    targets: dict
    usd_to_cad_rate: float
    norberts_gambit_fee_cad: float
    transient_symbols: set
    total_value: float
    holdings_view: dict = field(default_factory=dict)     # symbol -> holding data (tradeable view)
    available_cash: dict = field(default_factory=dict)   # acct_number -> {"CAD": float, "USD": float}
    effective_drift: dict = field(default_factory=dict)   # symbol -> drift %
    position_deltas: dict = field(default_factory=dict)   # (acct_number, symbol) -> qty change
    all_trades: list = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════
# Helper functions (operate on explicit RebalanceState)
# ══════════════════════════════════════════════════════════════════

def _to_cad(value: float, currency: str, usd_to_cad_rate: float) -> float:
    """Convert a value to CAD."""
    return value * usd_to_cad_rate if currency == "USD" else value


def _apply_trade(state: RebalanceState, symbol: str, value_cad: float, action: str):
    """Update effective drift after a trade."""
    pct = (value_cad / state.total_value) * 100.0
    if action == "SELL":
        state.effective_drift[symbol] = state.effective_drift.get(symbol, 0) - pct
    else:
        state.effective_drift[symbol] = state.effective_drift.get(symbol, 0) + pct


def _effective_cash(state: RebalanceState, acct_number: str, buy_currency: str) -> float:
    """Total buying power: native cash + convertible from other currency.
    Accounts for the Norbert's Gambit fee in both directions."""
    fee = state.norberts_gambit_fee_cad
    native = max(0, state.available_cash.get(acct_number, {}).get(buy_currency, 0))
    if buy_currency == "USD":
        cad = max(0, state.available_cash.get(acct_number, {}).get("CAD", 0))
        convertible = max(0, cad - fee) / state.usd_to_cad_rate
    else:
        usd = max(0, state.available_cash.get(acct_number, {}).get("USD", 0))
        convertible = max(0, usd * state.usd_to_cad_rate - fee)
    return native + convertible


def _deduct_buy(state: RebalanceState, acct_number: str, cost: float, currency: str) -> bool:
    """Deduct a buy cost: native currency first, then convert remainder.
    Returns True if cross-currency conversion was needed."""
    fee = state.norberts_gambit_fee_cad
    native = max(0, state.available_cash.get(acct_number, {}).get(currency, 0))
    if native >= cost:
        state.available_cash[acct_number][currency] -= cost
        return False
    # Use all native, convert the rest
    remainder = cost - native
    state.available_cash[acct_number][currency] = 0
    if currency == "USD":
        state.available_cash[acct_number]["CAD"] -= (
            remainder * state.usd_to_cad_rate + fee
        )
    else:
        state.available_cash[acct_number]["USD"] -= (
            remainder + fee
        ) / state.usd_to_cad_rate
    return True


def _shares_for_drift(state: RebalanceState, drift_pct: float, price: float, currency: str) -> int:
    """Calculate whole shares needed to close a drift gap."""
    gap_cad = abs(drift_pct / 100.0) * state.total_value
    gap_native = gap_cad / state.usd_to_cad_rate if currency == "USD" else gap_cad
    shares = int(math.floor(gap_native / price))
    # Expensive-stock fix: buy/sell 1 if it improves accuracy
    if shares == 0:
        one_cad = _to_cad(price, currency, state.usd_to_cad_rate)
        if one_cad < 2 * gap_cad:
            shares = 1
    return shares


def _sweep_candidates(state: RebalanceState, acct, currency: str, max_price: float = None) -> list:
    """Find buyable positions in an account for the given currency.
    Returns list of (symbol, drift, ask_price) sorted most-underweight first.
    If max_price is given, only include positions with ask ≤ max_price."""
    candidates = []
    for pos in acct.positions:
        if pos.currency != currency or pos.quantity <= 0:
            continue
        if pos.symbol in state.transient_symbols:
            continue
        # Never sweep cash into 0%-target symbols (unknowns being sold off).
        if state.targets.get(pos.symbol, 0) <= 0 and pos.symbol not in ("CAD", "USD"):
            continue
        holding = state.holdings_view.get(pos.symbol)
        if not holding:
            continue
        ask_price = holding.get("ask_price", pos.current_price)
        if ask_price <= 0:
            continue
        if max_price is not None and ask_price > max_price:
            continue
        drift = state.effective_drift.get(pos.symbol, 0)
        candidates.append((pos.symbol, drift, ask_price))
    candidates.sort(key=lambda x: x[1])  # Most underweight first
    return candidates


def _is_effectively_single_currency_account(state: RebalanceState, acct) -> bool:
    """Whether an account effectively holds only one position currency.

    Transient symbols are ignored so temporary holdings like DLR do not make an
    otherwise single-currency account look multi-currency.
    """
    currencies = {
        pos.currency
        for pos in acct.positions
        if pos.quantity > 0 and pos.symbol not in state.transient_symbols
    }
    return len(currencies) == 1


def _record_trade(state: RebalanceState, trade: TradeRecommendation):
    """Append a trade and update drift + position deltas.

    Every trade generation site should call this instead of manually
    calling all_trades.append / _apply_trade / delta bookkeeping.
    Cash adjustments still happen at each call site because the
    deduction logic varies (same-currency, cross-currency, sell credit).
    """
    state.all_trades.append(trade)
    value_cad = _to_cad(trade.estimated_value, trade.currency, state.usd_to_cad_rate)
    _apply_trade(state, trade.symbol, value_cad, trade.action)
    # Update position delta
    key = (trade.account_number, trade.symbol)
    delta = -trade.quantity if trade.action == "SELL" else trade.quantity
    state.position_deltas[key] = state.position_deltas.get(key, 0) + delta


# ══════════════════════════════════════════════════════════════════
# Step A: Sell overweight positions
# ══════════════════════════════════════════════════════════════════

def _step_sell_overweight(state: RebalanceState) -> int:
    """Sell overweight positions to bring them toward target.
    Returns the number of trades generated."""
    count = 0

    overweight = [
        (sym, d)
        for sym, d in state.effective_drift.items()
        if sym not in ("CAD", "USD") and d > TOLERANCE_PCT
    ]
    overweight.sort(key=lambda x: x[1], reverse=True)  # Most overweight first

    for symbol, drift in overweight:
        holding = state.holdings_view.get(symbol)
        if not holding:
            continue
        bid = holding.get("bid_price", holding["current_price"])
        currency = holding["currency"]
        if bid <= 0:
            continue

        shares = _shares_for_drift(state, drift, bid, currency)
        if shares <= 0:
            continue

        sell_trades = allocate_sell(
            symbol, shares, bid, currency, state.portfolio.accounts,
            effective_drift=state.effective_drift,
            transient_symbols=state.transient_symbols,
            position_deltas=state.position_deltas,
        )
        for trade in sell_trades:
            state.available_cash[trade.account_number][currency] += trade.estimated_value
            _record_trade(state, trade)
            count += 1

    return count


# ══════════════════════════════════════════════════════════════════
# Step B: Buy underweight positions
# ══════════════════════════════════════════════════════════════════

def _step_buy_underweight(state: RebalanceState, existing_only: bool) -> int:
    """Buy underweight positions using available cash, cross-currency
    conversion, or displacement sells. Returns trade count."""
    count = 0

    underweight = [
        (sym, d)
        for sym, d in state.effective_drift.items()
        if sym not in ("CAD", "USD") and d < -TOLERANCE_PCT
    ]
    underweight.sort(key=lambda x: x[1])  # Most underweight first

    for symbol, _initial_drift in underweight:
        # Re-check drift (earlier buys in this step may have changed it)
        drift = state.effective_drift.get(symbol, 0)
        if drift >= -TOLERANCE_PCT:
            continue

        holding = state.holdings_view.get(symbol)
        if not holding:
            continue
        ask = holding.get("ask_price", holding["current_price"])
        currency = holding["currency"]
        if ask <= 0:
            continue

        shares_needed = _shares_for_drift(state, drift, ask, currency)
        if shares_needed <= 0:
            continue

        # Find eligible accounts
        if existing_only:
            eligible = find_accounts_for_symbol(symbol, state.portfolio.accounts)
        else:
            eligible = list(state.portfolio.accounts)
        if not eligible:
            continue

        # Sort by total effective cash (highest first)
        eligible.sort(
            key=lambda a: _effective_cash(state, a.number, currency),
            reverse=True,
        )

        remaining = shares_needed

        for acct in eligible:
            if remaining <= 0:
                break

            # ── Try buying with available cash ──
            eff = _effective_cash(state, acct.number, currency)
            if eff >= ask:
                affordable = int(math.floor(eff / ask))
                qty = min(remaining, affordable)
                if qty > 0:
                    cost = qty * ask
                    converted = _deduct_buy(state, acct.number, cost, currency)
                    _record_trade(state, TradeRecommendation(
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
                    count += qty
                continue  # Move to next account

            # ── Not enough cash — try displacement sell ──
            count += _try_displacement_sell(
                state, symbol, ask, currency, acct, remaining
            )
            # Re-check remaining after displacement
            acct_cash = max(0, state.available_cash.get(acct.number, {}).get(currency, 0))
            if acct_cash >= ask and remaining > 0:
                affordable = int(math.floor(acct_cash / ask))
                qty = min(remaining, affordable)
                if qty > 0:
                    cost = qty * ask
                    state.available_cash[acct.number][currency] -= cost
                    _record_trade(state, TradeRecommendation(
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
                    count += qty

    return count


def _try_displacement_sell(
    state: RebalanceState,
    buy_symbol: str,
    buy_ask: float,
    currency: str,
    acct,
    remaining: int,
) -> int:
    """Sell another position in this account to raise cash for a buy.
    Prefer overweight positions; accept at-target if held in multiple accounts.
    Returns number of sell trades generated."""
    count = 0

    displacement_candidates = []
    for pos in acct.positions:
        if pos.symbol == buy_symbol or pos.symbol in state.transient_symbols:
            continue
        if pos.currency != currency or pos.quantity <= 0:
            continue
        pos_holding = state.holdings_view.get(pos.symbol)
        if not pos_holding:
            continue
        pos_bid = pos_holding.get("bid_price", pos.current_price)
        if pos_bid <= 0:
            continue
        pos_drift = state.effective_drift.get(pos.symbol, 0)
        displacement_candidates.append((pos.symbol, pos_drift, pos_bid))

    if not displacement_candidates:
        return 0

    # Most overweight first (best displacement targets)
    displacement_candidates.sort(key=lambda x: x[1], reverse=True)

    for cand_sym, cand_drift, cand_bid in displacement_candidates:
        if remaining <= 0:
            break

        is_overweight = cand_drift > TOLERANCE_PCT

        # Skip at-target positions held in only 1 account
        # (can't be repaired in next round)
        if not is_overweight:
            holders = find_accounts_for_symbol(cand_sym, state.portfolio.accounts)
            if len(holders) <= 1:
                continue

        # Calculate how many to sell
        acct_cash = max(0, state.available_cash.get(acct.number, {}).get(currency, 0))
        shortfall = remaining * buy_ask - acct_cash
        if shortfall <= 0:
            break  # Enough cash already

        sell_qty = int(math.ceil(shortfall / cand_bid))
        held = effective_qty(acct, cand_sym, state.position_deltas)
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
        state.available_cash[acct.number][currency] += sell_trade.estimated_value
        _record_trade(state, sell_trade)
        count += 1

    return count


# ══════════════════════════════════════════════════════════════════
# Step C: Sweep remaining cash into best positions
# ══════════════════════════════════════════════════════════════════

def _step_sweep_cash(state: RebalanceState) -> int:
    """Deploy remaining cash — buy the most underweight existing position
    in each account. Handles same-currency and cross-currency sweeps.
    Returns trade count."""
    count = 0

    for acct in state.portfolio.accounts:
        count += _sweep_same_currency(state, acct)
        count += _sweep_cross_currency(state, acct)

    return count


def _sweep_same_currency(state: RebalanceState, acct) -> int:
    """Iteratively buy the most underweight position using same-currency cash."""
    count = 0

    for currency in ["CAD", "USD"]:
        for _ in range(50):
            acct_cash = state.available_cash.get(acct.number, {}).get(currency, 0)
            if acct_cash <= 0:
                break

            candidates = _sweep_candidates(state, acct, currency, max_price=acct_cash)
            if not candidates:
                break

            best_symbol, best_drift, best_ask = candidates[0]

            affordable = int(math.floor(acct_cash / best_ask))
            if affordable <= 0:
                break

            # Cap at drift gap if underweight, else deploy all remaining cash
            if best_drift < -TOLERANCE_PCT:
                gap_cad = abs(best_drift / 100.0) * state.total_value
                gap_native = gap_cad / state.usd_to_cad_rate if currency == "USD" else gap_cad
                shares = min(int(math.ceil(gap_native / best_ask)), affordable)
            else:
                shares = affordable

            if shares <= 0:
                break

            cost = best_ask * shares
            state.available_cash[acct.number][currency] -= cost
            _record_trade(state, TradeRecommendation(
                symbol=best_symbol,
                action="BUY",
                quantity=shares,
                account_number=acct.number,
                account_type=acct.account_type,
                owner=acct.owner,
                price=best_ask,
                currency=currency,
                estimated_value=cost,
            ))
            count += shares

    return count


def _sweep_cross_currency(state: RebalanceState, acct) -> int:
    """Convert stranded cash in the wrong currency and buy the best position.

    Cross-currency sweep buys are more conservative than same-currency sweeps:
    only allow them when the destination symbol is still meaningfully
    underweight, or when the account is effectively single-currency.
    """
    count = 0
    fee = state.norberts_gambit_fee_cad
    is_single_currency = _is_effectively_single_currency_account(state, acct)

    for source_currency in ["CAD", "USD"]:
        source_cash = state.available_cash.get(acct.number, {}).get(source_currency, 0)
        if source_cash <= 0:
            continue

        target_currency = "USD" if source_currency == "CAD" else "CAD"
        candidates = _sweep_candidates(state, acct, target_currency)
        if not candidates:
            continue

        best_symbol, best_drift, best_ask = candidates[0]

        if not is_single_currency and best_drift >= -TOLERANCE_PCT:
            continue

        # Convert source cash to target currency, less Norbert's Gambit fee
        if source_currency == "CAD":
            buying_power = max(0, source_cash - fee) / state.usd_to_cad_rate
        else:
            buying_power = max(0, source_cash * state.usd_to_cad_rate - fee)

        shares = int(math.floor(buying_power / best_ask))
        if shares <= 0:
            continue

        cost = shares * best_ask
        # Deduct from source currency (cost converted back + fee)
        if source_currency == "CAD":
            state.available_cash[acct.number]["CAD"] -= (
                cost * state.usd_to_cad_rate + fee
            )
        else:
            state.available_cash[acct.number]["USD"] -= (
                cost + fee
            ) / state.usd_to_cad_rate
        _record_trade(state, TradeRecommendation(
            symbol=best_symbol,
            action="BUY",
            quantity=shares,
            account_number=acct.number,
            account_type=acct.account_type,
            owner=acct.owner,
            price=best_ask,
            currency=target_currency,
            estimated_value=cost,
            note="Requires currency conversion",
        ))
        count += shares

    return count


# ══════════════════════════════════════════════════════════════════
# Post-processing: net and consolidate trades
# ══════════════════════════════════════════════════════════════════

def _net_trades(all_trades: list) -> list:
    """Net buys and sells for the same (symbol, account) into a single trade.

    Multiple rounds may generate offsetting trades — e.g., sell 3 then buy 1
    of the same symbol in the same account.  This collapses them.
    """
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
        round_count += _step_sell_overweight(state)
        round_count += _step_buy_underweight(state, existing_only)
        round_count += _step_sweep_cash(state)

        if round_count == 0:
            break  # No more work to do

    return _net_trades(state.all_trades)


def simulate_rebalance(
    portfolio: PortfolioSummary,
    trades: list,
    targets: dict,
    usd_to_cad_rate: float,
    hidden_symbols: set = None,
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
    # Start with current holdings values from the canonical holdings map.
    # Hidden symbols still contribute to total-value math, but can be omitted
    # from the displayed projected allocation rows.
    source_holdings = portfolio.holdings
    projected_holdings = {}
    for symbol, data in source_holdings.items():
        projected_holdings[symbol] = data["value_cad"]
    if hidden_symbols is None:
        hidden_symbols = set()

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
            if symbol in hidden_symbols:
                continue
            projected_alloc[symbol] = (val / projected_total) * 100

    projected_accuracy = calculate_accuracy(projected_alloc, targets)

    return {
        "projected_allocations": projected_alloc,
        "projected_accuracy": projected_accuracy,
    }
