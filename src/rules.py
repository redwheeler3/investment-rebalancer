"""
Placement rules engine.

Determines which account to place each trade in, respecting configurable rules
like "only trade existing positions" and currency conversion handling.
"""

from dataclasses import dataclass


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


def find_accounts_for_symbol(symbol: str, accounts: list) -> list:
    """
    Find all accounts that currently hold a given symbol.

    Args:
        symbol: The ticker symbol to look for.
        accounts: List of AccountInfo objects.

    Returns:
        List of AccountInfo objects that hold at least one share of the symbol.
    """
    matching = []
    for acct in accounts:
        for pos in acct.positions:
            if pos.symbol == symbol and pos.quantity > 0:
                matching.append(acct)
                break
    return matching


def get_position_quantity(account, symbol: str) -> float:
    """Get the quantity of a symbol held in an account."""
    for pos in account.positions:
        if pos.symbol == symbol:
            return pos.quantity
    return 0.0


# Drift tolerance for sell-allocation decisions (mirrors rebalancer.TOLERANCE_PCT)
_SELL_ALLOC_TOLERANCE = 0.1


def _has_underweight_alternatives(
    acct,
    sell_symbol: str,
    effective_drift: dict,
    transient_symbols: set,
) -> bool:
    """Can the proceeds from selling be productively redeployed in this account?

    Returns True if the account holds at least one other non-transient
    position that is currently underweight (drift < -tolerance).  When True,
    the cash freed by selling *sell_symbol* can flow into that underweight
    position rather than boomeranging back into *sell_symbol* via the sweep.
    """
    for pos in acct.positions:
        if pos.symbol == sell_symbol or pos.quantity <= 0:
            continue
        if pos.symbol in transient_symbols:
            continue
        if effective_drift.get(pos.symbol, 0) < -_SELL_ALLOC_TOLERANCE:
            return True
    return False


def allocate_sell(
    symbol: str,
    total_shares: int,
    price: float,
    currency: str,
    accounts: list,
    effective_drift: dict = None,
    transient_symbols: set = None,
) -> list:
    """
    Allocate a SELL order across accounts.

    Strategy:
    - Only sell from accounts that hold the symbol
    - Prefer accounts with underweight alternatives (cash can be redeployed)
    - Among equally ranked accounts, sell from the largest position first

    Args:
        symbol: Ticker to sell.
        total_shares: Total shares to sell.
        price: Current price per share.
        currency: "CAD" or "USD".
        accounts: All AccountInfo objects.
        effective_drift: Current drift per symbol (used to check for underweight
            alternatives).  When None, falls back to quantity-only sorting.
        transient_symbols: Symbols excluded from rebalancing (e.g. DLR.TO).

    Returns:
        List of TradeRecommendation objects.
    """
    if total_shares <= 0:
        return []

    if effective_drift is None:
        effective_drift = {}
    if transient_symbols is None:
        transient_symbols = set()

    # Find accounts holding this symbol
    holders = find_accounts_for_symbol(symbol, accounts)
    if not holders:
        return []

    # Sort by: (1) accounts with underweight alternatives first (cash can be
    # redeployed productively), (2) largest position first within each tier.
    holders.sort(
        key=lambda a: (
            1 if _has_underweight_alternatives(a, symbol, effective_drift, transient_symbols) else 0,
            get_position_quantity(a, symbol),
        ),
        reverse=True,
    )

    trades = []
    remaining = total_shares

    for acct in holders:
        if remaining <= 0:
            break

        held = int(get_position_quantity(acct, symbol))
        shares_to_sell = min(remaining, held)

        if shares_to_sell > 0:
            trades.append(TradeRecommendation(
                symbol=symbol,
                action="SELL",
                quantity=shares_to_sell,
                account_number=acct.number,
                account_type=acct.account_type,
                owner=acct.owner,
                price=price,
                currency=currency,
                estimated_value=price * shares_to_sell,
            ))
            remaining -= shares_to_sell

    return trades


# ── Transient symbol handling ─────────────────────────────────────


def get_transient_status(portfolio, transient_symbols: list = None) -> dict:
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
