"""Trade plan helpers: netting and cash deployment.

Provides utilities used by the planner for finalizing trade lists:

- **Netting** — collapse offsetting buys/sells for the same (symbol, account)
- **Deployment** — build buy trades from available cash (same- or cross-currency)
"""

import math

from src.funding import (
    consume_cross_currency_cash,
    cross_currency_buying_power,
)
from src.models import TradeRecommendation


# ══════════════════════════════════════════════════════════════════
# Trade netting
# ══════════════════════════════════════════════════════════════════


def net_trades(all_trades: list) -> list:
    """Net buys and sells for the same (symbol, account) into a single trade."""
    position_map = {}  # (symbol, account_number) -> list of trades
    for trade in all_trades:
        key = (trade.symbol, trade.account_number)
        position_map.setdefault(key, []).append(trade)

    final_trades = []
    for (symbol, account_number), trades_list in position_map.items():
        total_buy_qty = 0
        total_sell_qty = 0
        buy_price = 0
        sell_price = 0
        template = trades_list[0]
        buy_note = ""
        sell_note = ""

        for trade in trades_list:
            if trade.action == "BUY":
                total_buy_qty += trade.quantity
                buy_price = trade.price
                if trade.note and not buy_note:
                    buy_note = trade.note
            else:
                total_sell_qty += trade.quantity
                sell_price = trade.price
                if trade.note and not sell_note:
                    sell_note = trade.note

        net_quantity = total_buy_qty - total_sell_qty
        if net_quantity > 0:
            price = buy_price if buy_price > 0 else template.price
            final_trades.append(TradeRecommendation(
                symbol=symbol,
                action="BUY",
                quantity=net_quantity,
                account_number=account_number,
                account_type=template.account_type,
                owner=template.owner,
                price=price,
                currency=template.currency,
                estimated_value=price * net_quantity,
                note=buy_note,
            ))
        elif net_quantity < 0:
            price = sell_price if sell_price > 0 else template.price
            final_trades.append(TradeRecommendation(
                symbol=symbol,
                action="SELL",
                quantity=abs(net_quantity),
                account_number=account_number,
                account_type=template.account_type,
                owner=template.owner,
                price=price,
                currency=template.currency,
                estimated_value=price * abs(net_quantity),
                note=sell_note,
            ))

    return final_trades


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


def build_same_currency_buy(
    acct,
    cash_by_account: dict[str, dict[str, float]],
    holdings: dict,
    drifts: dict,
    hidden_symbols: set,
    total_value_cad: float,
    usd_to_cad_rate: float,
    currency: str,
    underweight_threshold_pct: float,
    note: str = "",
) -> TradeRecommendation | None:
    """Build one same-currency buy and deduct its cash cost if possible."""
    acct_cash = cash_by_account.setdefault(acct.number, {"CAD": 0.0, "USD": 0.0})
    cash_native = acct_cash.get(currency, 0.0)
    if cash_native <= 0:
        return None

    candidates = underweight_candidates(
        acct,
        holdings,
        drifts,
        hidden_symbols,
        currency,
        underweight_threshold_pct,
    )
    if not candidates:
        return None

    symbol, drift_pct, ask_price_native = candidates[0]
    affordable_shares = int(math.floor(cash_native / ask_price_native))
    if affordable_shares <= 0:
        return None

    shares = min(
        _shares_to_close_underweight(
            total_value_cad,
            drift_pct,
            ask_price_native,
            currency,
            usd_to_cad_rate,
        ),
        affordable_shares,
    )
    if shares <= 0:
        return None

    cost_native = shares * ask_price_native
    acct_cash[currency] -= cost_native
    return TradeRecommendation(
        symbol=symbol,
        action="BUY",
        quantity=shares,
        account_number=acct.number,
        account_type=acct.account_type,
        owner=acct.owner,
        price=ask_price_native,
        currency=currency,
        estimated_value=cost_native,
        note=note,
    )


def build_cross_currency_buy(
    acct,
    cash_by_account: dict[str, dict[str, float]],
    holdings: dict,
    drifts: dict,
    hidden_symbols: set,
    total_value_cad: float,
    usd_to_cad_rate: float,
    source_currency: str,
    fee_cad: float,
    underweight_threshold_pct: float,
    note: str,
    dlr_quotes=None,
) -> TradeRecommendation | None:
    """Build one cross-currency buy and deduct its conservative cash cost."""
    acct_cash = cash_by_account.setdefault(acct.number, {"CAD": 0.0, "USD": 0.0})
    source_cash_native = acct_cash.get(source_currency, 0.0)
    if source_cash_native <= 0:
        return None

    target_currency = "USD" if source_currency == "CAD" else "CAD"
    candidates = underweight_candidates(
        acct,
        holdings,
        drifts,
        hidden_symbols,
        target_currency,
        underweight_threshold_pct,
    )
    if not candidates:
        return None

    symbol, drift_pct, ask_price_native = candidates[0]
    buying_power_native = cross_currency_buying_power(
        source_cash_native,
        source_currency,
        usd_to_cad_rate,
        fee_cad,
        dlr_quotes=dlr_quotes,
    )
    affordable_shares = int(math.floor(buying_power_native / ask_price_native))
    if affordable_shares <= 0:
        return None

    shares = min(
        _shares_to_close_underweight(
            total_value_cad,
            drift_pct,
            ask_price_native,
            target_currency,
            usd_to_cad_rate,
        ),
        affordable_shares,
    )
    if shares <= 0:
        return None

    cost_native = shares * ask_price_native
    consume_cross_currency_cash(
        acct_cash,
        source_currency,
        target_currency,
        cost_native,
        usd_to_cad_rate,
        fee_cad,
        dlr_quotes=dlr_quotes,
    )
    return TradeRecommendation(
        symbol=symbol,
        action="BUY",
        quantity=shares,
        account_number=acct.number,
        account_type=acct.account_type,
        owner=acct.owner,
        price=ask_price_native,
        currency=target_currency,
        estimated_value=cost_native,
        note=note,
    )


def _shares_to_close_underweight(
    total_value_cad: float,
    drift_pct: float,
    price_native: float,
    currency: str,
    usd_to_cad_rate: float,
) -> int:
    """Return whole shares needed to close an underweight gap."""
    if total_value_cad <= 0 or price_native <= 0:
        return 0

    gap_cad = abs(drift_pct / 100.0) * total_value_cad
    gap_native = gap_cad / usd_to_cad_rate if currency == "USD" else gap_cad
    return int(math.ceil(gap_native / price_native))
