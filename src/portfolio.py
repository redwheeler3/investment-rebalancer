"""
Portfolio aggregation module.

Collects positions and balances from all accounts across all Questrade logins
and builds a unified portfolio view.
"""

from dataclasses import dataclass, field


@dataclass
class Position:
    """A single position in a specific account."""

    symbol: str
    symbol_id: int
    quantity: float
    market_value: float  # In the position's native currency
    current_price: float
    currency: str  # "CAD" or "USD"
    account_number: str
    account_type: str
    owner: str  # "Jeff" or "Eunee"
    average_cost: float = 0.0


@dataclass
class AccountInfo:
    """Information about a single Questrade account."""

    number: str
    account_type: str  # e.g., "Margin", "TFSA", "RRSP"
    client_account_type: str  # e.g., "Individual", "Corporation" (from Questrade API)
    owner: str  # Display name: "Jeff", "Eunee", or "Rexin" for corporate
    positions: list = field(default_factory=list)  # List of Position
    cash_cad: float = 0.0
    cash_usd: float = 0.0


@dataclass
class PortfolioSummary:
    """Aggregated portfolio across all accounts."""

    accounts: list  # List of AccountInfo
    # Keyed by symbol -> total market value in CAD
    holdings: dict = field(default_factory=dict)
    total_value_cad: float = 0.0
    cash_cad_total: float = 0.0
    cash_usd_total: float = 0.0


def build_portfolio(clients: list, usd_to_cad_rate: float) -> PortfolioSummary:
    """
    Build a unified portfolio from multiple Questrade client connections.

    All positions are included in holdings. Transient symbols are
    handled post-build by freeze_symbols().

    Args:
        clients: List of QuestradeClient instances.
        usd_to_cad_rate: Current USD/CAD exchange rate.

    Returns:
        PortfolioSummary with all accounts, positions, and aggregated values.
    """
    all_accounts = []
    holdings = {}  # symbol -> {"value_cad": float, "quantity": float, "price": float, "currency": str}
    total_value_cad = 0.0
    total_cash_cad = 0.0
    total_cash_usd = 0.0

    for client in clients:
        accounts = client.get_accounts()

        for acct in accounts:
            acct_number = acct["number"]
            acct_type = acct["type"]
            client_acct_type = acct.get("clientAccountType", "Individual")

            # Determine display owner name
            # Corporation accounts under Jeff's login are labeled "Rexin"
            if client_acct_type == "Corporation":
                display_owner = "Rexin"
            else:
                display_owner = client.owner_name

            # Get balances
            balances_data = client.get_balances(acct_number)
            cash_cad = 0.0
            cash_usd = 0.0

            # Questrade returns combinedBalances with per-currency entries
            for bal in balances_data.get("perCurrencyBalances", []):
                if bal["currency"] == "CAD":
                    cash_cad = bal.get("cash", 0.0)
                elif bal["currency"] == "USD":
                    cash_usd = bal.get("cash", 0.0)

            # Get positions
            raw_positions = client.get_positions(acct_number)
            positions = []

            for pos in raw_positions:
                symbol = pos.get("symbol", "")
                quantity = pos.get("openQuantity", 0)
                market_value = pos.get("currentMarketValue", 0.0)
                current_price = pos.get("currentPrice", 0.0)
                symbol_id = pos.get("symbolId", 0)
                avg_cost = pos.get("averageEntryPrice", 0.0)

                if quantity == 0 and market_value == 0:
                    continue

                # Determine currency from the symbol
                # .TO symbols are CAD, others (like IVV) are USD
                currency = "CAD" if symbol.endswith(".TO") else "USD"

                position = Position(
                    symbol=symbol,
                    symbol_id=symbol_id,
                    quantity=quantity,
                    market_value=market_value,
                    current_price=current_price,
                    currency=currency,
                    account_number=acct_number,
                    account_type=acct_type,
                    owner=display_owner,
                    average_cost=avg_cost,
                )
                positions.append(position)

                # Convert to CAD for aggregation
                value_cad = market_value
                if currency == "USD":
                    value_cad = market_value * usd_to_cad_rate

                if symbol not in holdings:
                    holdings[symbol] = {
                        "value_cad": 0.0,
                        "total_quantity": 0.0,
                        "current_price": current_price,
                        "currency": currency,
                        "accounts": [],  # Which accounts hold this
                    }

                holdings[symbol]["value_cad"] += value_cad
                holdings[symbol]["total_quantity"] += quantity
                holdings[symbol]["current_price"] = current_price
                holdings[symbol]["bid_price"] = current_price  # Will be updated with quote data
                holdings[symbol]["ask_price"] = current_price  # Will be updated with quote data
                holdings[symbol]["accounts"].append({
                    "account_number": acct_number,
                    "account_type": acct_type,
                    "owner": display_owner,
                    "quantity": quantity,
                    "market_value": market_value,
                })

            account_info = AccountInfo(
                number=acct_number,
                account_type=acct_type,
                client_account_type=client_acct_type,
                owner=display_owner,
                positions=positions,
                cash_cad=cash_cad,
                cash_usd=cash_usd,
            )
            all_accounts.append(account_info)

            # Add cash to total (convert USD cash to CAD)
            total_cash_cad += cash_cad
            total_cash_usd += cash_usd

    # Calculate total portfolio value in CAD
    total_value_cad = total_cash_cad + (total_cash_usd * usd_to_cad_rate)
    for symbol, data in holdings.items():
        total_value_cad += data["value_cad"]

    return PortfolioSummary(
        accounts=all_accounts,
        holdings=holdings,
        total_value_cad=total_value_cad,
        cash_cad_total=total_cash_cad,
        cash_usd_total=total_cash_usd,
    )


def freeze_symbols(portfolio: PortfolioSummary, symbols: set):
    """
    Remove symbols from holdings while keeping their value in total_value_cad.

    Used for transient symbols that shouldn't be traded. The value stays
    in total_value_cad so allocation percentages remain accurate — the
    excluded portion appears as a small "gap" where allocations sum to
    slightly less than 100%.

    Args:
        portfolio: The portfolio to modify in place.
        symbols: Set of symbol strings to freeze.
    """
    for symbol in symbols:
        if symbol in portfolio.holdings:
            del portfolio.holdings[symbol]


def fetch_quotes_for_holdings(portfolio: PortfolioSummary, clients: list):
    """
    Fetch bid/ask quotes for all holdings and update the portfolio.

    Uses the first available client to fetch quotes.
    Updates holdings with bid_price (for sells) and ask_price (for buys).

    Args:
        portfolio: The portfolio to update.
        clients: List of QuestradeClient instances.
    """
    if not clients:
        return

    # Collect all unique symbol IDs from positions
    symbol_id_map = {}  # symbol_id -> symbol
    for acct in portfolio.accounts:
        for pos in acct.positions:
            if pos.symbol_id > 0 and pos.symbol in portfolio.holdings:
                symbol_id_map[pos.symbol_id] = pos.symbol

    if not symbol_id_map:
        return

    # Fetch quotes in batches (Questrade allows up to ~100 at a time)
    symbol_ids = list(symbol_id_map.keys())
    client = clients[0]

    try:
        quotes = client.get_quote(symbol_ids)
        for quote in quotes:
            symbol = quote.get("symbol", "")
            if symbol in portfolio.holdings:
                bid = quote.get("bidPrice") or quote.get("lastTradePrice", 0)
                ask = quote.get("askPrice") or quote.get("lastTradePrice", 0)
                last = quote.get("lastTradePrice", 0)

                # Use last trade price as fallback if bid/ask is 0 or None
                portfolio.holdings[symbol]["bid_price"] = bid if bid and bid > 0 else last
                portfolio.holdings[symbol]["ask_price"] = ask if ask and ask > 0 else last
    except Exception as e:
        print(f"  Warning: Could not fetch quotes: {e}")


def get_current_allocations(portfolio: PortfolioSummary, usd_to_cad_rate: float) -> dict:
    """
    Calculate current allocation percentages for each holding.

    Args:
        portfolio: The aggregated portfolio summary.
        usd_to_cad_rate: Current USD/CAD exchange rate.

    Returns:
        Dictionary mapping symbol -> current percentage of total portfolio.
        Includes "CAD" and "USD" entries for cash positions.
    """
    if portfolio.total_value_cad == 0:
        return {}

    allocations = {}

    # Cash allocations
    cad_cash_pct = (portfolio.cash_cad_total / portfolio.total_value_cad) * 100
    usd_cash_value_cad = portfolio.cash_usd_total * usd_to_cad_rate
    usd_cash_pct = (usd_cash_value_cad / portfolio.total_value_cad) * 100

    allocations["CAD"] = cad_cash_pct
    allocations["USD"] = usd_cash_pct

    # Holdings allocations
    for symbol, data in portfolio.holdings.items():
        allocations[symbol] = (data["value_cad"] / portfolio.total_value_cad) * 100

    return allocations


def calculate_accuracy(current_allocations: dict, targets: dict) -> float:
    """
    Calculate portfolio accuracy score.

    Accuracy = 100% - (sum of absolute drifts / 2)

    A perfectly balanced portfolio scores 100%.
    The division by 2 corrects for double-counting (overweight somewhere
    must equal underweight elsewhere).

    Args:
        current_allocations: Current allocation percentages by symbol.
        targets: Target allocation percentages by symbol.

    Returns:
        Accuracy score as a percentage (0-100).
    """
    all_symbols = current_allocations.keys() | targets.keys()
    total_abs_drift = sum(
        abs(current_allocations.get(s, 0.0) - targets.get(s, 0.0))
        for s in all_symbols
    )

    return max(0.0, 100.0 - (total_abs_drift / 2.0))


def get_drifts(current_allocations: dict, targets: dict) -> dict:
    """
    Calculate drift for each symbol (current - target).

    Args:
        current_allocations: Current allocation percentages by symbol.
        targets: Target allocation percentages by symbol.

    Returns:
        Dictionary mapping symbol -> drift percentage.
    """
    all_symbols = current_allocations.keys() | targets.keys()
    return {
        s: current_allocations.get(s, 0.0) - targets.get(s, 0.0)
        for s in all_symbols
    }
