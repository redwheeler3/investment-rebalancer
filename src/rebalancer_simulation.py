"""Projected portfolio simulation.

Separated from trade generation so the rebalancer module can stay focused on
creating trades, while this module focuses on answering: "what would the
portfolio look like after those trades?"
"""

from src.portfolio import build_allocation_snapshot_from_values


def simulate_rebalance(
    portfolio,
    trades: list,
    targets: dict,
    usd_to_cad_rate: float,
    hidden_symbols: set = None,
):
    """Return the projected allocation snapshot after applying the trades."""
    projected_holdings_value_cad = {
        symbol: holding.value_cad
        for symbol, holding in portfolio.holdings.items()
    }
    projected_cash_cad = portfolio.cash_cad_total
    projected_cash_usd = portfolio.cash_usd_total

    if hidden_symbols is None:
        hidden_symbols = set()

    for trade in trades:
        trade_price_cad = trade.price * usd_to_cad_rate if trade.currency == "USD" else trade.price
        trade_value_cad = trade_price_cad * trade.quantity
        uses_conversion = "currency conversion" in (trade.note or "").lower()

        if trade.action == "BUY":
            projected_holdings_value_cad[trade.symbol] = (
                projected_holdings_value_cad.get(trade.symbol, 0.0) + trade_value_cad
            )
            if uses_conversion:
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
            projected_holdings_value_cad[trade.symbol] = (
                projected_holdings_value_cad.get(trade.symbol, 0.0) - trade_value_cad
            )
            if trade.currency == "CAD":
                projected_cash_cad += trade.estimated_value
            else:
                projected_cash_usd += trade.estimated_value

    if projected_cash_cad < 0:
        projected_cash_usd += projected_cash_cad / usd_to_cad_rate
        projected_cash_cad = 0
    if projected_cash_usd < 0:
        projected_cash_cad += projected_cash_usd * usd_to_cad_rate
        projected_cash_usd = 0

    projected_total_value_cad = projected_cash_cad + (projected_cash_usd * usd_to_cad_rate)
    projected_total_value_cad += sum(projected_holdings_value_cad.values())

    return build_allocation_snapshot_from_values(
        holdings_value_cad=projected_holdings_value_cad,
        cash_cad_total=projected_cash_cad,
        cash_usd_total=projected_cash_usd,
        total_value_cad=projected_total_value_cad,
        targets=targets,
        usd_to_cad_rate=usd_to_cad_rate,
        excluded_symbols=hidden_symbols,
    )