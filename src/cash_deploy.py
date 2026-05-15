"""Residual cash deployment helpers.

Builds buy trades from available cash after the planner's main sell/buy passes:

- **underweight_candidates** — find account-local underweight holdings in a currency
- **build_underweight_buy** — build one buy trade funded by available cash
  (handles both same-currency and cross-currency depending on parameters)
"""

import math

from src.fx_math import (
    consume_cross_currency_cash,
    cross_currency_buying_power,
)
from src.models import TradeRecommendation


# ══════════════════════════════════════════════════════════════════
# Cash deployment helpers
# ══════════════════════════════════════════════════════════════════


def underweight_candidates(
    acct,
    holdings: dict,
    drifts: dict,
    hidden_symbols: set,
    currency: str,
    underweight_threshold_pct: float,
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
        if drift_pct >= -underweight_threshold_pct:
            continue

        holding = holdings.get(pos.symbol)
        if not holding:
            continue

        ask_price_native = holding.ask_price or pos.current_price
        if ask_price_native <= 0:
            continue

        seen.add(pos.symbol)
        candidates.append((pos.symbol, drift_pct, ask_price_native))

    candidates.sort(key=lambda item: item[1])
    return candidates


def build_underweight_buy(
    acct,
    cash_by_account: dict[str, dict[str, float]],
    holdings: dict,
    drifts: dict,
    hidden_symbols: set,
    total_value_cad: float,
    usd_to_cad_rate: float,
    source_currency: str,
    buy_currency: str,
    underweight_threshold_pct: float,
    fee_cad: float = 0.0,
    note: str = "",
    dlr_quotes=None,
) -> TradeRecommendation | None:
    """Build one buy trade funded by available cash.

    Handles both same-currency (source_currency == buy_currency) and
    cross-currency (source_currency != buy_currency) cases. Iterates
    through underweight candidates so that if the most underweight symbol
    is too expensive, a cheaper alternative is used.
    """
    acct_cash = cash_by_account.setdefault(acct.number, {"CAD": 0.0, "USD": 0.0})
    requires_fx = source_currency != buy_currency

    # Compute buying power
    source_cash = acct_cash.get(source_currency, 0.0)
    if source_cash <= 0:
        return None

    if requires_fx:
        buying_power = cross_currency_buying_power(
            source_cash,
            source_currency,
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes=dlr_quotes,
        )
    else:
        buying_power = source_cash

    # Find candidates in the buy currency
    candidates = underweight_candidates(
        acct,
        holdings,
        drifts,
        hidden_symbols,
        buy_currency,
        underweight_threshold_pct,
    )
    if not candidates:
        return None

    for symbol, drift_pct, ask_price_native in candidates:
        affordable_shares = int(math.floor(buying_power / ask_price_native))
        if affordable_shares <= 0:
            continue

        shares = min(
            _shares_to_close_underweight(
                total_value_cad,
                drift_pct,
                ask_price_native,
                buy_currency,
                usd_to_cad_rate,
            ),
            affordable_shares,
        )
        if shares <= 0:
            continue

        cost_native = shares * ask_price_native

        # Consume cash
        if requires_fx:
            consume_cross_currency_cash(
                acct_cash,
                source_currency,
                buy_currency,
                cost_native,
                usd_to_cad_rate,
                fee_cad,
                dlr_quotes=dlr_quotes,
            )
        else:
            acct_cash[buy_currency] -= cost_native

        return TradeRecommendation(
            symbol=symbol,
            action="BUY",
            quantity=shares,
            account_number=acct.number,
            account_type=acct.account_type,
            owner=acct.owner,
            price=ask_price_native,
            currency=buy_currency,
            estimated_value=cost_native,
            note=note,
            requires_fx=requires_fx,
        )

    return None


def _shares_to_close_underweight(
    total_value_cad: float,
    drift_pct: float,
    price_native: float,
    currency: str,
    usd_to_cad_rate: float,
) -> int:
    """Return whole shares needed to close an underweight gap (rounds up)."""
    if total_value_cad <= 0 or price_native <= 0:
        return 0

    gap_cad = abs(drift_pct / 100.0) * total_value_cad
    gap_native = gap_cad / usd_to_cad_rate if currency == "USD" else gap_cad
    return int(math.ceil(gap_native / price_native))
