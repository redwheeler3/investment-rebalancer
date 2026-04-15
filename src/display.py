"""
Terminal display module.

Pretty-prints portfolio status, allocation drift, and trade recommendations
using the Rich library.
"""

from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box


console = Console()

# Shared constants
_CASH_SYMBOLS = {"CAD", "USD"}


def _partition_symbols(allocations: dict, targets: dict, drifts: dict):
    """Split symbols into stocks (sorted by drift) and cash (sorted alphabetically)."""
    all_symbols = allocations.keys() | targets.keys()
    stocks = sorted(
        [s for s in all_symbols if s not in _CASH_SYMBOLS],
        key=lambda s: drifts.get(s, 0.0),
    )
    cash = sorted(s for s in all_symbols if s in _CASH_SYMBOLS)
    return stocks, cash


def _add_drift_row(table, symbol: str, value_pct: float, target: float, drift: float):
    """Add an allocation/drift row to a table (shared by current and projected tables)."""
    if abs(drift) < 0.1:
        drift_style = "dim"
        status = "[green]OK[/green]"
    elif drift > 0:
        drift_style = "red"
        status = "[red]OVER[/red]"
    else:
        drift_style = "yellow"
        status = "[yellow]UNDER[/yellow]"
    table.add_row(
        symbol,
        f"{target:.1f}%",
        f"{value_pct:.1f}%",
        Text(f"{drift:+.1f}%", style=drift_style),
        status,
    )


def display_header():
    """Display the application header."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = Text()
    header.append("PORTFOLIO REBALANCER", style="bold cyan")
    header.append(f"  —  {now}", style="dim")
    console.print()
    console.print(Panel(header, box=box.DOUBLE, style="cyan", expand=False))
    console.print()


def display_holdings_summary(portfolio, usd_to_cad_rate: float):
    """Display aggregated portfolio holdings — total shares and value per symbol."""
    table = Table(
        title="Portfolio Holdings",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold magenta",
    )

    table.add_column("Symbol", style="bold", min_width=10)
    table.add_column("Shares", justify="right", min_width=10)
    table.add_column("Price", justify="right", min_width=12)
    table.add_column("Value (CAD)", justify="right", min_width=14)

    # Collect holdings sorted alphabetically
    rows = []
    for symbol in sorted(portfolio.holdings.keys()):
        data = portfolio.holdings[symbol]
        qty = data["total_quantity"]
        price = data["current_price"]
        currency = data["currency"]
        value_cad = data["value_cad"]

        if currency == "USD":
            price_str = f"US${price:,.2f}"
        else:
            price_str = f"${price:,.2f}"

        qty_str = f"{int(qty):,}" if qty == int(qty) else f"{qty:,.2f}"
        rows.append((symbol, qty_str, price_str, value_cad))

    for symbol, qty_str, price_str, value_cad in rows:
        table.add_row(
            symbol,
            qty_str,
            price_str,
            f"${value_cad:,.2f}",
        )

    # Add cash rows
    table.add_section()
    if portfolio.cash_cad_total != 0:
        table.add_row(
            "Cash CAD",
            "",
            "",
            f"${portfolio.cash_cad_total:,.2f}",
        )
    if portfolio.cash_usd_total != 0:
        cash_usd_cad = portfolio.cash_usd_total * usd_to_cad_rate
        table.add_row(
            "Cash USD",
            "",
            f"US${portfolio.cash_usd_total:,.2f}",
            f"${cash_usd_cad:,.2f}",
        )

    # Total row
    table.add_section()
    table.add_row(
        "[bold]Total[/bold]",
        "",
        f"[dim]USD/CAD {usd_to_cad_rate:.4f}[/dim]",
        f"[bold green]${portfolio.total_value_cad:,.2f}[/bold green]",
    )

    console.print(table)
    console.print()


def display_all_time_high(ath):
    """Display the all-time high portfolio value with drawdown indicator."""
    if ath is None:
        return

    if ath.is_new_ath:
        console.print(
            f"  [bold]All-Time High:[/bold]           "
            f"[bold green]${ath.value:,.2f}[/bold green]  "
            f"[green]🎉 NEW ATH (today!)[/green]"
        )
    else:
        console.print(
            f"  [bold]All-Time High:[/bold]           "
            f"${ath.value:,.2f} ({ath.date})  "
            f"[yellow]▼ {ath.drawdown_pct:.1f}%[/yellow]"
        )
    console.print()


def display_accuracy(current_accuracy: float, projected_accuracy: float = None):
    """Display the portfolio accuracy score."""
    # Color based on accuracy
    if current_accuracy >= 98:
        color = "green"
    elif current_accuracy >= 95:
        color = "yellow"
    elif current_accuracy >= 90:
        color = "dark_orange"
    else:
        color = "red"

    console.print(f"  [bold]Accuracy Score:[/bold]         [{color}]{current_accuracy:.1f}%[/{color}]", end="")

    if projected_accuracy is not None:
        if projected_accuracy >= 98:
            proj_color = "green"
        elif projected_accuracy >= 95:
            proj_color = "yellow"
        else:
            proj_color = "dark_orange"
        console.print(f"  →  [{proj_color}]{projected_accuracy:.1f}%[/{proj_color}] (after trades)", end="")

    console.print()
    console.print()


def _display_allocation_table(
    title: str,
    header_style: str,
    allocations: dict,
    targets: dict,
    value_column_name: str = "Current %",
):
    """Display an allocation vs target table — shared by current and projected views."""
    # Calculate drifts inline
    all_symbols = allocations.keys() | targets.keys()
    drifts = {s: allocations.get(s, 0.0) - targets.get(s, 0.0) for s in all_symbols}

    table = Table(
        title=title,
        box=box.ROUNDED,
        show_header=True,
        header_style=header_style,
    )

    table.add_column("Symbol", style="bold", min_width=10)
    table.add_column("Target %", justify="right", min_width=10)
    table.add_column(value_column_name, justify="right", min_width=10)
    table.add_column("Drift", justify="right", min_width=10)
    table.add_column("Status", justify="center", min_width=8)

    stock_symbols, cash_list = _partition_symbols(allocations, targets, drifts)

    for symbol in stock_symbols:
        _add_drift_row(table, symbol, allocations.get(symbol, 0.0),
                       targets.get(symbol, 0.0), drifts.get(symbol, 0.0))

    if cash_list:
        table.add_section()
        for symbol in cash_list:
            _add_drift_row(table, symbol, allocations.get(symbol, 0.0),
                           targets.get(symbol, 0.0), drifts.get(symbol, 0.0))

    console.print(table)
    console.print()


def display_allocations(current_allocations: dict, targets: dict, drifts: dict):
    """Display current vs target allocation table, sorted by drift ascending."""
    _display_allocation_table(
        title="Current vs Target Allocation",
        header_style="bold magenta",
        allocations=current_allocations,
        targets=targets,
    )


def display_trades(trades: list):
    """Display recommended trades table, grouped by account with sells before buys."""
    if not trades:
        console.print("  [green]No trades needed -- portfolio is balanced![/green]")
        console.print()
        return

    # Sort trades: group by account, sells before buys within each account
    sorted_trades = sorted(
        trades,
        key=lambda t: (
            t.owner,
            t.account_type,
            t.account_number,
            0 if t.action == "SELL" else 1,
            t.symbol,
        ),
    )

    table = Table(
        title="Recommended Trades",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold magenta",
    )

    table.add_column("Symbol", style="bold", min_width=10)
    table.add_column("Action", justify="center", min_width=6)
    table.add_column("Qty", justify="right", min_width=5)
    table.add_column("Price", justify="right", min_width=10)
    table.add_column("Est. Value", justify="right", min_width=12)
    table.add_column("Account", min_width=25)
    table.add_column("Note", style="dim", min_width=15)

    prev_account = None

    for trade in sorted_trades:
        # Add a section divider between different accounts
        account_key = trade.account_number
        if prev_account is not None and prev_account != account_key:
            table.add_section()
        prev_account = account_key

        action_style = "green bold" if trade.action == "BUY" else "red bold"
        account_label = f"{trade.owner} {trade.account_type} ({trade.account_number})"

        currency_symbol = "$" if trade.currency == "CAD" else "US$"

        table.add_row(
            trade.symbol,
            Text(trade.action, style=action_style),
            str(trade.quantity),
            f"{currency_symbol}{trade.price:,.2f}",
            f"{currency_symbol}{trade.estimated_value:,.2f}",
            account_label,
            trade.note,
        )

    console.print(table)
    console.print()


def display_currency_conversions(conversions: list):
    """Display currency conversion instructions with DLR share details.
    Fee is built into the Amount column (added to CAD spend, subtracted from CAD received)."""
    if not conversions:
        return

    table = Table(
        title="Currency Conversions (Norbert's Gambit)",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold yellow",
    )

    table.add_column("Account", min_width=25)
    table.add_column("Direction", justify="center", min_width=12)
    table.add_column("Buy", style="bold", min_width=12)
    table.add_column("Shares", justify="right", min_width=8)
    table.add_column("DLR Price", justify="right", min_width=10)
    table.add_column("Amount (incl. fee)", justify="right", min_width=14)

    for conv in conversions:
        account_label = f"{conv.owner} {conv.account_type} ({conv.account_number})"

        if conv.direction == "CAD_TO_USD":
            direction = "CAD -> USD"
            # Fee adds to the CAD you spend
            total_cad = conv.source_amount + conv.fee
            amount_str = f"${total_cad:,.2f} CAD -> ${conv.target_amount:,.2f} USD"
        else:
            direction = "USD -> CAD"
            # Fee subtracts from the CAD you receive
            net_cad = conv.target_amount - conv.fee
            amount_str = f"${conv.source_amount:,.2f} USD -> ${net_cad:,.2f} CAD"

        shares_str = str(conv.dlr_shares) if conv.dlr_shares > 0 else "N/A"
        price_str = f"${conv.dlr_price:,.2f}" if conv.dlr_price > 0 else "N/A"

        table.add_row(
            account_label,
            direction,
            conv.dlr_symbol,
            shares_str,
            price_str,
            amount_str,
        )

    console.print(table)
    console.print()


def display_transient_alerts(transient_alerts: list):
    """Display alerts for transient holdings excluded from rebalancing."""
    if not transient_alerts:
        return

    for alert in transient_alerts:
        account_label = f"{alert.owner} {alert.account_type} ({alert.account_number})"
        console.print(
            f"  [yellow]⏳ {alert.symbol}:[/yellow] "
            f"{int(alert.quantity)} shares in {account_label} — {alert.note}"
        )
    console.print()


def display_account_summary(accounts: list):
    """Display a summary of all accounts."""
    table = Table(
        title="Account Summary",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold magenta",
    )

    table.add_column("Owner", style="bold", min_width=8)
    table.add_column("Type", min_width=15)
    table.add_column("Number", min_width=10)
    table.add_column("Cash CAD", justify="right", min_width=12)
    table.add_column("Cash USD", justify="right", min_width=12)
    table.add_column("Positions", justify="center", min_width=10)

    for acct in accounts:
        # Count non-zero positions
        pos_count = sum(1 for p in acct.positions if p.quantity > 0)
        pos_symbols = ", ".join(
            sorted(set(p.symbol for p in acct.positions if p.quantity > 0))
        )

        table.add_row(
            acct.owner,
            acct.account_type,
            acct.number,
            f"${acct.cash_cad:,.2f}",
            f"${acct.cash_usd:,.2f}",
            f"{pos_count} ({pos_symbols})" if pos_count <= 5 else str(pos_count),
        )

    console.print(table)
    console.print()


def display_projected_allocations(projected_allocations: dict, targets: dict):
    """Display projected allocation table after trades, sorted by drift ascending."""
    if not projected_allocations:
        return

    _display_allocation_table(
        title="Projected Allocation (After Trades)",
        header_style="bold cyan",
        allocations=projected_allocations,
        targets=targets,
        value_column_name="Projected %",
    )


def display_full_report(
    portfolio,
    current_allocations: dict,
    targets: dict,
    drifts: dict,
    accuracy: float,
    trades: list,
    currency_conversions: list,
    transient_alerts: list,
    usd_to_cad_rate: float,
    projected_accuracy: float = None,
    projected_allocations: dict = None,
    all_time_high=None,
):
    """Display the complete rebalancing report."""
    display_header()
    display_accuracy(accuracy, projected_accuracy)
    display_all_time_high(all_time_high)
    display_holdings_summary(portfolio, usd_to_cad_rate)
    display_account_summary(portfolio.accounts)
    display_allocations(current_allocations, targets, drifts)
    display_transient_alerts(transient_alerts)
    display_trades(trades)
    display_currency_conversions(currency_conversions)
    display_projected_allocations(projected_allocations, targets)
