"""Residual cash deployment helpers.

Builds buy trades from available cash after the planner's main sell/buy passes:

- **underweight_candidates** — find account-local underweight holdings in a currency
- **build_deploy_cash_underweight** — build one buy trade funded by available cash
  (handles both same-currency and cross-currency depending on parameters)
"""

import math

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

    candidates.sort(key=lambda item: (item[1], item[0]))
    return candidates


def build_deploy_cash_underweight(
    acct,
    ledger,
    holdings: dict,
    drifts: dict,
    hidden_symbols: set,
    total_value_cad: float,
    usd_to_cad_rate: float,
    source_currency: str,
    buy_currency: str,
    underweight_threshold_pct: float,
    dlr_quotes=None,
    buy_limits=None,
) -> TradeRecommendation | None:
    """Build one buy trade funded by available cash.

    Handles both same-currency (source_currency == buy_currency) and
    cross-currency (source_currency != buy_currency) cases. Iterates
    through underweight candidates so that if the most underweight symbol
    is too expensive, a cheaper alternative is used.
    """
    requires_fx = source_currency != buy_currency
    if ledger.cash(acct.number, source_currency) <= 0:
        return None
    buying_power = (
        ledger.total_buying_power(acct.number, buy_currency, dlr_quotes)
        if requires_fx
        else ledger.cash(acct.number, buy_currency)
    )

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
        affordable_shares = math.floor(buying_power / ask_price_native)
        if buy_limits and symbol in buy_limits:
            affordable_shares = min(affordable_shares, buy_limits[symbol])
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

        requires_fx = ledger.fund_buy(
            acct.number, buy_currency, cost_native, dlr_quotes
        )

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
            note="Deploy cash (underweight) (FX)"
            if requires_fx
            else "Deploy cash (underweight)",
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
    return math.ceil(gap_native / price_native)
