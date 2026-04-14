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
    """Alert about transient holdings like DLR.TO / DLR.U.TO."""

    symbol: str
    quantity: float
    account_number: str
    account_type: str
    owner: str
    action: str  # "SELL" or "WAIT"
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


def get_account_cash(account, currency: str) -> float:
    """
    Get available cash in an account for a specific currency.

    Args:
        account: AccountInfo object.
        currency: "CAD" or "USD".

    Returns:
        Available cash amount.
    """
    if currency == "CAD":
        return account.cash_cad
    elif currency == "USD":
        return account.cash_usd
    return 0.0


def get_position_quantity(account, symbol: str) -> float:
    """Get the quantity of a symbol held in an account."""
    for pos in account.positions:
        if pos.symbol == symbol:
            return pos.quantity
    return 0.0


def allocate_sell(
    symbol: str,
    total_shares: int,
    price: float,
    currency: str,
    accounts: list,
) -> list:
    """
    Allocate a SELL order across accounts.

    Strategy:
    - Only sell from accounts that hold the symbol
    - Distribute proportionally based on current holdings

    Args:
        symbol: Ticker to sell.
        total_shares: Total shares to sell.
        price: Current price per share.
        currency: "CAD" or "USD".
        accounts: All AccountInfo objects.

    Returns:
        List of TradeRecommendation objects.
    """
    if total_shares <= 0:
        return []

    # Find accounts holding this symbol
    holders = find_accounts_for_symbol(symbol, accounts)
    if not holders:
        return []

    # Sort by quantity held (descending) — sell from largest positions first
    holders.sort(key=lambda a: get_position_quantity(a, symbol), reverse=True)

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


def check_transient_holdings(accounts: list, transient_symbols: list) -> list:
    """
    Check for transient holdings (DLR.TO, DLR.U.TO) and generate alerts.

    Args:
        accounts: List of AccountInfo objects.
        transient_symbols: List of transient symbol strings.

    Returns:
        List of TransientAlert objects.
    """
    alerts = []

    for acct in accounts:
        for pos in acct.positions:
            if pos.symbol in transient_symbols and pos.quantity > 0:
                if pos.symbol == "DLR.U.TO":
                    action = "SELL"
                    note = "DLR.U.TO detected (post-journal) — sell to complete Norbert's Gambit"
                elif pos.symbol == "DLR.TO":
                    action = "WAIT"
                    note = "DLR.TO detected (pre-journal) — wait for journal to complete"
                else:
                    action = "REVIEW"
                    note = f"Transient symbol {pos.symbol} detected"

                alerts.append(TransientAlert(
                    symbol=pos.symbol,
                    quantity=pos.quantity,
                    account_number=acct.number,
                    account_type=acct.account_type,
                    owner=acct.owner,
                    action=action,
                    note=note,
                ))

    return alerts