"""
Portfolio data model and allocation math.

Collects positions and balances from all accounts across all Questrade logins,
builds a unified portfolio view, calculates allocation percentages and drift,
and projects what the portfolio would look like after a set of trades.
"""

from dataclasses import dataclass, field


def _coerce_numeric(value, default: float = 0.0) -> float:
    """Convert an API field to float, defaulting to 0.0 for None."""
    return float(value) if value is not None else default


def _normalize_currency(value) -> str | None:
    """Normalize API currency values to CAD/USD when possible."""
    if not value:
        return None

    normalized = str(value).strip().upper()
    return normalized if normalized in {"CAD", "USD"} else None


def _resolve_position_currency(pos: dict, symbol_id: int, symbol: str, symbol_currency_map: dict[int, str]) -> str:
    """Require Questrade-provided currency data for each position."""
    currency = _normalize_currency(pos.get("currency")) or symbol_currency_map.get(symbol_id)
    if currency:
        return currency

    raise RuntimeError(
        f"Could not determine currency for symbol '{symbol}' (symbolId={symbol_id}) from Questrade data. "
        "Stopping rather than guessing from the symbol name."
    )


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
    owner: str  # Friendly owner/display label from config
    average_cost: float = 0.0


@dataclass
class AccountInfo:
    """Information about a single Questrade account."""

    number: str
    account_type: str  # e.g., "Margin", "TFSA", "RRSP"
    client_account_type: str  # e.g., "Individual", "Corporation" (from Questrade API)
    owner: str  # Display owner label shown in reports
    positions: list["Position"] = field(default_factory=list)
    cash_cad: float = 0.0
    cash_usd: float = 0.0


@dataclass
class HoldingAccountDetail:
    """How a holding is distributed within a specific account."""

    account_number: str
    account_type: str
    owner: str
    quantity: float
    market_value: float


@dataclass
class HoldingSummary:
    """Aggregated holding data across all accounts for one symbol."""

    value_cad: float = 0.0
    total_quantity: float = 0.0
    current_price: float = 0.0
    currency: str = "CAD"
    bid_price: float = 0.0
    ask_price: float = 0.0
    accounts: list["HoldingAccountDetail"] = field(default_factory=list)


@dataclass
class PortfolioSummary:
    """Aggregated portfolio across all accounts."""

    accounts: list["AccountInfo"]
    holdings: dict[str, "HoldingSummary"] = field(default_factory=dict)
    total_value_cad: float = 0.0
    cash_cad_total: float = 0.0
    cash_usd_total: float = 0.0


@dataclass
class AllocationSnapshot:
    """A complete allocation view with derived drift and accuracy metrics."""

    allocations: dict
    drifts: dict
    accuracy: float


def build_portfolio(clients: list, usd_to_cad_rate: float) -> PortfolioSummary:
    """
    Build a unified portfolio from multiple Questrade client connections.

    All positions are included in the canonical holdings map. Callers that
    need a filtered view (for example, excluding transient symbols from
    rebalancing) should derive that view explicitly with get_holdings_view().

    Args:
        clients: List of QuestradeClient instances.
        usd_to_cad_rate: Current USD/CAD exchange rate.

    Returns:
        PortfolioSummary with all accounts, positions, and aggregated values.
    """
    all_accounts = []
    holdings = {}  # symbol -> HoldingSummary
    total_value_cad = 0.0
    total_cash_cad = 0.0
    total_cash_usd = 0.0

    for client in clients:
        accounts = client.get_accounts()

        raw_positions_by_account = {}
        symbol_ids = set()
        for acct in accounts:
            acct_number = acct["number"]
            raw_positions = client.get_positions(acct_number)
            raw_positions_by_account[acct_number] = raw_positions
            for pos in raw_positions:
                symbol_id = int(_coerce_numeric(pos.get("symbolId", 0), default=0.0))
                if symbol_id > 0:
                    symbol_ids.add(symbol_id)

        symbol_currency_map = {}
        if symbol_ids:
            for symbol_data in client.get_symbols(sorted(symbol_ids)):
                symbol_id = int(_coerce_numeric(symbol_data.get("symbolId", 0), default=0.0))
                currency = _normalize_currency(symbol_data.get("currency"))
                if symbol_id > 0 and currency:
                    symbol_currency_map[symbol_id] = currency

        for acct in accounts:
            acct_number = acct["number"]
            acct_type = acct["type"]
            client_acct_type = acct.get("clientAccountType", "Individual")

            # Determine display owner name. Account-type-specific labels can be
            # overridden in private config, e.g. mapping "Corporation" to a
            # holding-company display name.
            display_owner = client.account_type_display_overrides.get(
                client_acct_type,
                client.owner_name,
            )

            # Get balances
            balances_data = client.get_balances(acct_number)
            cash_cad = 0.0
            cash_usd = 0.0

            # Questrade returns combinedBalances with per-currency entries
            for bal in balances_data.get("perCurrencyBalances", []):
                if bal["currency"] == "CAD":
                    cash_cad = _coerce_numeric(bal.get("cash", 0.0))
                elif bal["currency"] == "USD":
                    cash_usd = _coerce_numeric(bal.get("cash", 0.0))

            # Get positions
            raw_positions = raw_positions_by_account.get(acct_number, [])
            positions = []

            for pos in raw_positions:
                symbol = pos.get("symbol") or ""
                quantity = _coerce_numeric(pos.get("openQuantity", 0))
                market_value = _coerce_numeric(pos.get("currentMarketValue", 0.0))
                current_price = _coerce_numeric(pos.get("currentPrice", 0.0))
                symbol_id = int(_coerce_numeric(pos.get("symbolId", 0), default=0.0))
                avg_cost = _coerce_numeric(pos.get("averageEntryPrice", 0.0))

                if quantity == 0 and market_value == 0:
                    continue

                currency = _resolve_position_currency(
                    pos,
                    symbol_id,
                    symbol,
                    symbol_currency_map,
                )

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
                    holdings[symbol] = HoldingSummary(
                        current_price=current_price,
                        currency=currency,
                        bid_price=current_price,
                        ask_price=current_price,
                    )

                holding = holdings[symbol]
                holding.value_cad += value_cad
                holding.total_quantity += quantity
                holding.current_price = current_price
                holding.bid_price = current_price  # Will be updated with quote data
                holding.ask_price = current_price  # Will be updated with quote data
                holding.accounts.append(HoldingAccountDetail(
                    account_number=acct_number,
                    account_type=acct_type,
                    owner=display_owner,
                    quantity=quantity,
                    market_value=market_value,
                ))

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
    for holding in holdings.values():
        total_value_cad += holding.value_cad

    return PortfolioSummary(
        accounts=all_accounts,
        holdings=holdings,
        total_value_cad=total_value_cad,
        cash_cad_total=total_cash_cad,
        cash_usd_total=total_cash_usd,
    )


def get_position_value_cad(position: Position, usd_to_cad_rate: float) -> float:
    """Return a position's market value converted to CAD."""
    return position.market_value * usd_to_cad_rate if position.currency == "USD" else position.market_value


def get_account_positions_value_cad(account: AccountInfo, usd_to_cad_rate: float) -> float:
    """Return the total CAD value of all non-zero positions in an account."""
    return sum(
        get_position_value_cad(pos, usd_to_cad_rate)
        for pos in account.positions
        if pos.quantity > 0
    )


def get_account_total_value_cad(account: AccountInfo, usd_to_cad_rate: float) -> float:
    """Return the full CAD value of an account including cash and positions."""
    return (
        account.cash_cad
        + (account.cash_usd * usd_to_cad_rate)
        + get_account_positions_value_cad(account, usd_to_cad_rate)
    )


def get_holdings_view(portfolio: PortfolioSummary, excluded_symbols: set) -> dict:
    """Return a holdings view, excluding specific symbols.

    The portfolio keeps one canonical holdings map. Callers that need a
    filtered view for rebalancing or allocation tables should request it
    explicitly instead of mutating portfolio state.
    """
    return {
        symbol: data
        for symbol, data in portfolio.holdings.items()
        if symbol not in excluded_symbols
    }


def calculate_allocations_for_values(
    holdings_value_cad: dict,
    total_value_cad: float,
    cash_cad_total: float,
    cash_usd_total: float,
    usd_to_cad_rate: float,
    excluded_symbols: set = None,
) -> dict:
    """Build allocation percentages from already-computed holding values."""
    if total_value_cad == 0:
        return {}

    allocations = {
        "CAD": (cash_cad_total / total_value_cad) * 100,
        "USD": ((cash_usd_total * usd_to_cad_rate) / total_value_cad) * 100,
    }

    for symbol, value_cad in holdings_value_cad.items():
        if excluded_symbols and symbol in excluded_symbols:
            continue
        allocations[symbol] = (value_cad / total_value_cad) * 100

    return allocations


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

    quotes = client.get_quote(symbol_ids)
    for quote in quotes:
        symbol = quote.get("symbol", "")
        if symbol in portfolio.holdings:
            holding = portfolio.holdings[symbol]
            holding.bid_price = float(quote.get("bidPrice") or 0)
            holding.ask_price = float(quote.get("askPrice") or 0)
            holding.current_price = float(quote.get("lastTradePrice") or 0) or holding.current_price


def get_current_allocations(portfolio: PortfolioSummary, usd_to_cad_rate: float, excluded_symbols: set = None) -> dict:
    """Calculate current allocation percentages for each holding (includes CAD/USD cash entries)."""
    holdings_value_cad = {symbol: data.value_cad for symbol, data in portfolio.holdings.items()}
    return calculate_allocations_for_values(
        holdings_value_cad=holdings_value_cad,
        total_value_cad=portfolio.total_value_cad,
        cash_cad_total=portfolio.cash_cad_total,
        cash_usd_total=portfolio.cash_usd_total,
        usd_to_cad_rate=usd_to_cad_rate,
        excluded_symbols=excluded_symbols,
    )


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
    """Calculate drift for each symbol (current % - target %)."""
    all_symbols = current_allocations.keys() | targets.keys()
    return {
        s: current_allocations.get(s, 0.0) - targets.get(s, 0.0)
        for s in all_symbols
    }


def build_allocation_snapshot_from_values(
    holdings_value_cad: dict,
    cash_cad_total: float,
    cash_usd_total: float,
    total_value_cad: float,
    targets: dict,
    usd_to_cad_rate: float,
    excluded_symbols: set = None,
) -> AllocationSnapshot:
    """Build allocations, drifts, and accuracy from raw value inputs."""
    allocations = calculate_allocations_for_values(
        holdings_value_cad=holdings_value_cad,
        total_value_cad=total_value_cad,
        cash_cad_total=cash_cad_total,
        cash_usd_total=cash_usd_total,
        usd_to_cad_rate=usd_to_cad_rate,
        excluded_symbols=excluded_symbols,
    )
    return AllocationSnapshot(
        allocations=allocations,
        drifts=get_drifts(allocations, targets),
        accuracy=calculate_accuracy(allocations, targets),
    )


def build_allocation_snapshot(
    portfolio: PortfolioSummary,
    targets: dict,
    usd_to_cad_rate: float,
    excluded_symbols: set = None,
) -> AllocationSnapshot:
    """Build allocations, drifts, and accuracy from a portfolio."""
    holdings_value_cad = {symbol: data.value_cad for symbol, data in portfolio.holdings.items()}
    return build_allocation_snapshot_from_values(
        holdings_value_cad=holdings_value_cad,
        cash_cad_total=portfolio.cash_cad_total,
        cash_usd_total=portfolio.cash_usd_total,
        total_value_cad=portfolio.total_value_cad,
        targets=targets,
        usd_to_cad_rate=usd_to_cad_rate,
        excluded_symbols=excluded_symbols,
    )


def simulate_rebalance(
    portfolio,
    trades: list,
    targets: dict,
    usd_to_cad_rate: float,
    hidden_symbols: set = None,
) -> AllocationSnapshot:
    """Project what the portfolio allocation would look like after applying trades."""
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

        if trade.action == "BUY":
            projected_holdings_value_cad[trade.symbol] = (
                projected_holdings_value_cad.get(trade.symbol, 0.0) + trade_value_cad
            )
            if trade.requires_fx:
                # Funded by converting the other currency
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
