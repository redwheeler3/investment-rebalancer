"""Shared data types and constants used across the rebalancer."""

from dataclasses import dataclass


# Default minimum absolute drift before a symbol is eligible for trading
DEFAULT_DRIFT_TRADE_THRESHOLD_PCT = 0.1


@dataclass
class TradeRecommendation:
    """A recommended trade to execute."""

    symbol: str
    action: str  # "BUY" or "SELL"
    quantity: int  # Whole shares
    account_number: str
    account_type: str
    owner: str
    price: float
    currency: str
    estimated_value: float  # price * quantity in native currency
    note: str = ""  # Optional note (e.g., currency conversion needed)


@dataclass
class TransientAlert:
    """Alert about a transient holding in a specific account."""

    symbol: str
    quantity: float
    account_number: str
    account_type: str
    owner: str
    note: str


def get_transient_status(portfolio, transient_symbols: list) -> dict:
    """
    Identify which transient symbols are actually held and build alerts.

    Args:
        portfolio: PortfolioSummary with current holdings.
        transient_symbols: List of symbols from config to exclude.

    Returns:
        dict with:
            symbols:  set of transient symbols actually held
            alerts:   list of TransientAlert objects for display
    """
    if not transient_symbols:
        return {"symbols": set(), "alerts": []}

    held = set()
    alerts = []

    for symbol in transient_symbols:
        if symbol not in portfolio.holdings:
            continue  # Not held — nothing to do
        held.add(symbol)
        for acct in portfolio.accounts:
            for pos in acct.positions:
                if pos.symbol == symbol and pos.quantity > 0:
                    alerts.append(TransientAlert(
                        symbol=symbol,
                        quantity=pos.quantity,
                        account_number=acct.number,
                        account_type=acct.account_type,
                        owner=acct.owner,
                        note="Excluded from rebalancing",
                    ))

    return {"symbols": held, "alerts": alerts}
