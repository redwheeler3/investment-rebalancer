"""Core rebalancer state and shared helpers.

This module holds the immutable tuning constants plus the low-level state and
cash/drift helpers used by the rebalancing steps.
"""

import math
from dataclasses import dataclass, field


# Default minimum absolute drift before a symbol is eligible for trading
DEFAULT_DRIFT_TRADE_THRESHOLD_PCT = 0.1

# Maximum optimisation rounds before stopping
MAX_ROUNDS = 10


@dataclass
class RebalanceState:
    """Mutable state passed through all rebalance steps."""

    portfolio: object
    targets: dict
    usd_to_cad_rate: float
    norberts_gambit_fee_cad: float
    drift_trade_threshold_pct: float
    transient_symbols: set
    total_value: float
    holdings_view: dict = field(default_factory=dict)   # symbol -> holding data
    available_cash: dict = field(default_factory=dict)  # acct_number -> {"CAD": float, "USD": float}
    effective_drift: dict = field(default_factory=dict)  # symbol -> drift %
    position_deltas: dict = field(default_factory=dict)  # (acct_number, symbol) -> qty change
    all_trades: list = field(default_factory=list)


def to_cad(value: float, currency: str, usd_to_cad_rate: float) -> float:
    """Convert a value to CAD."""
    return value * usd_to_cad_rate if currency == "USD" else value


def apply_trade_to_drift(state: RebalanceState, symbol: str, value_cad: float, action: str):
    """Update effective drift after a trade."""
    pct = (value_cad / state.total_value) * 100.0
    if action == "SELL":
        state.effective_drift[symbol] = state.effective_drift.get(symbol, 0) - pct
    else:
        state.effective_drift[symbol] = state.effective_drift.get(symbol, 0) + pct


def effective_cash(state: RebalanceState, acct_number: str, buy_currency: str) -> float:
    """Total buying power: native cash + convertible from other currency."""
    fee = state.norberts_gambit_fee_cad
    native_cash = max(0, state.available_cash.get(acct_number, {}).get(buy_currency, 0))
    if buy_currency == "USD":
        cash_cad_native = max(0, state.available_cash.get(acct_number, {}).get("CAD", 0))
        convertible_cash = max(0, cash_cad_native - fee) / state.usd_to_cad_rate
    else:
        cash_usd_native = max(0, state.available_cash.get(acct_number, {}).get("USD", 0))
        convertible_cash = max(0, cash_usd_native * state.usd_to_cad_rate - fee)
    return native_cash + convertible_cash


def deduct_buy(state: RebalanceState, acct_number: str, cost_native: float, currency: str) -> bool:
    """Deduct a buy cost: native currency first, then convert remainder.

    Returns True if cross-currency conversion was needed.
    """
    fee = state.norberts_gambit_fee_cad
    native_cash = max(0, state.available_cash.get(acct_number, {}).get(currency, 0))
    if native_cash >= cost_native:
        state.available_cash[acct_number][currency] -= cost_native
        return False

    remainder_native = cost_native - native_cash
    state.available_cash[acct_number][currency] = 0
    if currency == "USD":
        state.available_cash[acct_number]["CAD"] -= (
            remainder_native * state.usd_to_cad_rate + fee
        )
    else:
        state.available_cash[acct_number]["USD"] -= (
            remainder_native + fee
        ) / state.usd_to_cad_rate
    return True


def shares_for_drift(state: RebalanceState, drift_pct: float, price_native: float, currency: str) -> int:
    """Calculate whole shares needed to close a drift gap."""
    gap_cad = abs(drift_pct / 100.0) * state.total_value
    gap_native = gap_cad / state.usd_to_cad_rate if currency == "USD" else gap_cad
    shares = int(math.floor(gap_native / price_native))
    if shares == 0:
        one_share_cad = to_cad(price_native, currency, state.usd_to_cad_rate)
        if one_share_cad < 2 * gap_cad:
            shares = 1
    return shares


def record_trade(state: RebalanceState, trade):
    """Append a trade and update drift + position deltas."""
    state.all_trades.append(trade)
    value_cad = to_cad(trade.estimated_value, trade.currency, state.usd_to_cad_rate)
    apply_trade_to_drift(state, trade.symbol, value_cad, trade.action)
    key = (trade.account_number, trade.symbol)
    delta = -trade.quantity if trade.action == "SELL" else trade.quantity
    state.position_deltas[key] = state.position_deltas.get(key, 0) + delta