"""
Terminal display module.

Pretty-prints portfolio status, allocation drift, and trade recommendations
using the Rich library.
"""

from datetime import date, datetime
from textwrap import wrap
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box
from src.portfolio import get_account_positions_value_cad, get_account_total_value_cad


console = Console()

# Shared constants
_CASH_SYMBOLS = {"CAD", "USD"}


def _normalize_amount(amount: float) -> float:
    """Avoid distracting negative zero values in terminal output."""
    return 0.0 if abs(amount) < 0.005 else amount


def _format_money(amount: float, currency: str = "CAD") -> str:
    """Format a money amount with an explicit currency prefix."""
    amount = _normalize_amount(amount)
    prefix = "US$" if currency == "USD" else "$"
    return f"{prefix}{amount:,.2f}"


def _format_price(price: float, currency: str) -> str:
    """Format a quoted price in its native currency."""
    return _format_money(price, currency)


def _format_shares(quantity: float) -> str:
    """Format a share quantity for display."""
    return f"{int(quantity):,}" if quantity == int(quantity) else f"{quantity:,.2f}"


def _format_signed_change(change: float, pct: float) -> str:
    """Format a signed change with matching arrow, color, and percentage."""
    if change >= 0:
        color = "green"
        arrow = "▲"
        sign = "+"
    else:
        color = "red"
        arrow = "▼"
        sign = "-"

    return (
        f"[{color}]{arrow} {sign}${abs(change):,.2f} ({sign}{abs(pct):.1f}%)[/{color}]"
    )


def _format_account_label(owner: str, account_type: str, account_number: str) -> str:
    """Format a standard account label used across multiple tables."""
    return f"{owner} {account_type} ({account_number})"


def _partition_symbols(allocations: dict, targets: dict, drifts: dict):
    """Split symbols into stocks (sorted by drift) and cash (sorted alphabetically)."""
    all_symbols = allocations.keys() | targets.keys()
    stocks = sorted(
        [s for s in all_symbols if s not in _CASH_SYMBOLS],
        key=lambda s: drifts.get(s, 0.0),
        reverse=True,
    )
    cash = sorted(s for s in all_symbols if s in _CASH_SYMBOLS)
    return stocks, cash


def _add_drift_row(
    table,
    symbol: str,
    value_pct: float,
    target: float,
    drift: float,
    tolerance_pct: float,
):
    """Add an allocation/drift row using the configured drift tolerance."""
    if abs(drift) < tolerance_pct:
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


def _format_price_change(price_change: float, change_pct: float, currency: str) -> str:
    """Format a price change with sign, currency prefix, and percentage."""
    if price_change == 0 and change_pct == 0:
        return "[dim]—[/dim]"

    prefix = "US$" if currency == "USD" else "$"
    sign = "+" if price_change >= 0 else "-"
    color = "green" if price_change >= 0 else "red"
    return (
        f"[{color}]{sign}{prefix}{abs(price_change):,.2f} "
        f"({sign}{abs(change_pct):.1f}%)[/{color}]"
    )


def _format_day_pnl(pnl_cad: float) -> str:
    """Format a day P&L value in CAD with sign and color."""
    if pnl_cad == 0:
        return "[dim]—[/dim]"

    sign = "+" if pnl_cad >= 0 else "-"
    color = "green" if pnl_cad >= 0 else "red"
    return f"[{color}]{sign}${abs(pnl_cad):,.2f}[/{color}]"


def display_holdings_summary(portfolio, usd_to_cad_rate: float):
    """Display aggregated portfolio holdings — total shares and value per symbol."""
    table = Table(
        title="Portfolio Holdings",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold magenta",
    )

    table.add_column("Symbol", style="bold")
    table.add_column("Shares", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Change", justify="right")
    table.add_column("Value (CAD)", justify="right")
    table.add_column("Day P&L", justify="right")

    # Collect holdings sorted alphabetically
    total_day_pnl_cad = 0.0
    rows = []
    for symbol in sorted(portfolio.holdings.keys()):
        holding = portfolio.holdings[symbol]
        qty = holding.total_quantity
        price = holding.current_price
        currency = holding.currency
        value_cad = holding.value_cad
        open_px = holding.open_price

        price_str = _format_price(price, currency)
        qty_str = _format_shares(qty)

        # Calculate price change and day P&L (from today's open)
        if open_px > 0:
            price_change = price - open_px
            change_pct = (price_change / open_px) * 100.0
            day_pnl_native = price_change * qty
            day_pnl_cad = day_pnl_native * usd_to_cad_rate if currency == "USD" else day_pnl_native
        else:
            price_change = 0.0
            change_pct = 0.0
            day_pnl_cad = 0.0

        total_day_pnl_cad += day_pnl_cad
        change_str = _format_price_change(price_change, change_pct, currency)
        pnl_str = _format_day_pnl(day_pnl_cad)

        rows.append((symbol, qty_str, price_str, change_str, value_cad, pnl_str))

    for symbol, qty_str, price_str, change_str, value_cad, pnl_str in rows:
        table.add_row(
            symbol,
            qty_str,
            price_str,
            change_str,
            _format_money(value_cad),
            pnl_str,
        )

    # Add cash rows
    table.add_section()
    if portfolio.cash_cad_total != 0:
        table.add_row(
            "Cash CAD",
            "",
            "",
            "",
            _format_money(portfolio.cash_cad_total),
            "",
        )
    if portfolio.cash_usd_total != 0:
        cash_usd_cad = portfolio.cash_usd_total * usd_to_cad_rate
        table.add_row(
            "Cash USD",
            "",
            _format_money(portfolio.cash_usd_total, "USD"),
            "",
            _format_money(cash_usd_cad),
            "",
        )

    # Total row
    table.add_section()
    table.add_row(
        "[bold]Total[/bold]",
        "",
        f"[dim]USD/CAD {usd_to_cad_rate:.4f}[/dim]",
        "",
        f"[bold green]{_format_money(portfolio.total_value_cad)}[/bold green]",
        f"[bold]{_format_day_pnl(total_day_pnl_cad)}[/bold]",
    )

    console.print(table)
    console.print()


def display_all_time_high(ath, portfolio_value: float):
    """Display the all-time high portfolio value with drawdown indicator."""
    if ath is None:
        return

    ath_change = portfolio_value - ath.value
    ath_change_pct = 0.0 if ath.value == 0 else (ath_change / ath.value) * 100.0

    if ath.is_new_ath:
        console.print(
            f"  [bold]All-Time High:[/bold]           "
            f"[default]${ath.value:,.2f}[/default]  "
            f"[green]🎉 NEW ATH (today!)[/green]  "
            f"[default]({ath.date})[/default]"
        )
        return

    console.print(
        f"  [bold]All-Time High:[/bold]           "
        f"[default]${ath.value:,.2f}[/default]  "
        f"{_format_signed_change(ath_change, ath_change_pct)}  "
        f"[default]({ath.date})[/default]"
    )


def display_daily_change(daily_change, portfolio_value: float):
    """Display the current portfolio value with day-over-day change."""
    if daily_change is None:
        console.print(
            f"  [bold]Portfolio Value:[/bold]         "
            f"[default]${portfolio_value:,.2f}[/default]"
        )
    else:
        change = daily_change.change_dollars
        pct = daily_change.change_pct

        console.print(
            f"  [bold]Portfolio Value:[/bold]         "
            f"[default]${portfolio_value:,.2f}[/default]  "
            f"{_format_signed_change(change, pct)}"
        )


def _format_compact_money(amount: float) -> str:
    """Format large money values compactly for chart labels."""
    amount = _normalize_amount(amount)
    absolute_amount = abs(amount)

    if absolute_amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.2f}B"
    if absolute_amount >= 1_000_000:
        return f"${amount / 1_000_000:.2f}M"
    if absolute_amount >= 1_000:
        return f"${amount / 1_000:.1f}K"
    return f"${amount:,.0f}"


def _append_wrapped_line(lines: list[str], text: str, width: int) -> None:
    """Append text to the output, wrapped to the target width when needed."""
    wrapped = wrap(text, width=width) or [""]
    lines.extend(wrapped)


def _iter_month_starts(start_date: date, end_date: date):
    """Yield the first day of each month from start_date's month through end_date."""
    current = date(start_date.year, start_date.month, 1)
    while current <= end_date:
        yield current
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)


def _determine_chart_height(console_height: int) -> int:
    """Choose a chart height so the full chart panel nearly fills the terminal height.

    We reserve rows for:
    - top and bottom panel borders (2)
    - x-axis baseline (1)
    - x-axis date labels (1)
    - spacer before stats (1)
    - Latest / Low / High lines (3)

    That fixed overhead totals 8 rows, so a panel that should occupy roughly
    ``console_height - 2`` rows leaves ``console_height - 10`` rows available
    for the actual plotted chart area.
    """
    reserved_rows = 8
    return max(4, console_height - reserved_rows - 2)


def _build_vertical_tick_labels(min_value: float, max_value: float, chart_height: int) -> dict[int, str]:
    """Create evenly spaced Y-axis labels based on available chart height."""
    tick_count = max(3, min(6, (chart_height // 4) + 1))
    tick_labels = {}

    for tick_index in range(tick_count):
        row = round(tick_index * (chart_height - 1) / (tick_count - 1)) if tick_count > 1 else 0
        value = (
            max_value - ((max_value - min_value) * tick_index / (tick_count - 1))
            if tick_count > 1
            else max_value
        )
        tick_labels[row] = _format_compact_money(value)

    return tick_labels


def _month_tick_candidates(start_of_year: date, today: date, chart_width: int, total_days: int) -> list[tuple[int, str]]:
    """Return month-start tick candidates mapped onto the chart width."""
    candidates = []
    used_positions = set()

    for tick_date in _iter_month_starts(start_of_year, today):
        x = 0 if total_days == 0 else round(((tick_date - start_of_year).days * (chart_width - 1)) / total_days)
        if x in used_positions:
            continue
        used_positions.add(x)
        candidates.append((x, tick_date.strftime("%b %d")))

    return candidates


def _tick_label_layout(ticks: list[tuple[int, str]], chart_width: int) -> list[tuple[int, str]] | None:
    """Compute non-overlapping single-line positions for X-axis labels."""
    layout = []
    previous_end = -2

    for x, label in ticks:
        start = max(0, min(chart_width - len(label), x - (len(label) // 2)))
        end = start + len(label) - 1
        if start <= previous_end + 1:
            return None
        layout.append((start, label))
        previous_end = end

    return layout


def _select_horizontal_ticks(start_of_year: date, today: date, chart_width: int, total_days: int) -> list[tuple[int, str]]:
    """Choose a subset of month-start labels that fits the chart width cleanly."""
    candidates = _month_tick_candidates(start_of_year, today, chart_width, total_days)
    if not candidates:
        return []

    for stride in range(1, len(candidates) + 1):
        selected = candidates[::stride]
        if candidates[-1] not in selected:
            selected.append(candidates[-1])

        deduped = []
        for tick in selected:
            if tick not in deduped:
                deduped.append(tick)

        if _tick_label_layout(deduped, chart_width) is not None:
            return deduped

    return [candidates[0]]


def display_year_to_date_chart(history_points: list, console_height: int | None = None):
    """Display a terminal chart of recorded year-to-date portfolio values."""
    if not history_points:
        return

    today = date.today()
    start_of_year = date(today.year, 1, 1)
    total_days = max(1, (today - start_of_year).days)
    values = [point.value for point in history_points]
    min_value = min(values)
    max_value = max(values)
    midpoint_value = (min_value + max_value) / 2

    top_label = _format_compact_money(max_value)
    mid_label = _format_compact_money(midpoint_value)
    bottom_label = _format_compact_money(min_value)

    chart_height = _determine_chart_height(console_height or console.size.height)
    horizontal_padding = 1
    left_axis_width = max(len(top_label), len(mid_label), len(bottom_label))
    panel_inner_width = max(20, console.size.width - 4 - (horizontal_padding * 2))
    available_width = max(10, panel_inner_width - left_axis_width - 2)
    chart_width = min(total_days + 1, available_width)

    buckets = [[] for _ in range(chart_width)]
    for point in history_points:
        day_offset = (point.date - start_of_year).days
        x = 0 if total_days == 0 else round(day_offset * (chart_width - 1) / total_days)
        buckets[x].append(point)

    series = [bucket[-1] if bucket else None for bucket in buckets]
    value_span = max_value - min_value
    row_labels = _build_vertical_tick_labels(min_value, max_value, chart_height)
    left_axis_width = max(len(label) for label in row_labels.values())
    selected_ticks = _select_horizontal_ticks(start_of_year, today, chart_width, total_days)

    def value_to_row(value: float) -> int:
        if value_span == 0:
            return chart_height // 2
        scaled = (value - min_value) / value_span
        return chart_height - 1 - round(scaled * (chart_height - 1))

    grid = [[" " for _ in range(chart_width)] for _ in range(chart_height)]
    previous_plot = None

    fill_char = "░"

    for x, point in enumerate(series):
        if point is None:
            continue

        y = value_to_row(point.value)
        if previous_plot is not None:
            prev_x, prev_y = previous_plot
            dx = x - prev_x
            if dx > 1:
                for step in range(1, dx):
                    xi = prev_x + step
                    yi = round(prev_y + ((y - prev_y) * step / dx))
                    if grid[yi][xi] == " ":
                        grid[yi][xi] = fill_char

        grid[y][x] = fill_char
        previous_plot = (x, y)

    for x in range(chart_width):
        first_drawn_row = next((row for row in range(chart_height) if grid[row][x] != " "), None)
        if first_drawn_row is None:
            continue
        for y in range(first_drawn_row + 1, chart_height):
            if grid[y][x] == " ":
                grid[y][x] = fill_char

    lines = []
    for row_index, row in enumerate(grid):
        label = row_labels.get(row_index, "")
        lines.append(f"{label:>{left_axis_width}} |{''.join(row)}")

    axis_chars = ["-" for _ in range(chart_width)]
    axis_chars[0] = "+"
    for x, _label in selected_ticks:
        axis_chars[x] = "+"
    lines.append(f"{'':>{left_axis_width}} {''.join(axis_chars)}")

    label_chars = [" " for _ in range(chart_width)]
    for start, label in _tick_label_layout(selected_ticks, chart_width) or []:
        for offset, char in enumerate(label):
            label_chars[start + offset] = char
    lines.append(f"{'':>{left_axis_width + 1}} {''.join(label_chars)}")

    lines.append("")
    _append_wrapped_line(lines, f"Latest {_format_money(history_points[-1].value)}", panel_inner_width)
    _append_wrapped_line(lines, f"Low {_format_money(min_value)}", panel_inner_width)
    _append_wrapped_line(lines, f"High {_format_money(max_value)}", panel_inner_width)

    console.print(
        Panel(
            "\n".join(lines),
            title="Year-to-Date Portfolio Value",
            box=box.ROUNDED,
            border_style="cyan",
            padding=(0, horizontal_padding),
            expand=False,
        )
    )
    console.print()


def display_accuracy(current_accuracy: float, projected_accuracy: float = None):
    """Display the portfolio accuracy score."""
    # Color based on accuracy
    if current_accuracy > 95:
        color = "green"
    elif current_accuracy >= 90:
        color = "yellow"
    else:
        color = "red"

    console.print(f"  [bold]Accuracy Score:[/bold]          [{color}]{current_accuracy:.1f}%[/{color}]", end="")

    if projected_accuracy is not None:
        if projected_accuracy > 95:
            proj_color = "green"
        elif projected_accuracy >= 90:
            proj_color = "yellow"
        else:
            proj_color = "red"
        console.print(f"  →  [{proj_color}]{projected_accuracy:.1f}%[/{proj_color}]", end="")

    console.print()


def _display_allocation_table(
    title: str,
    header_style: str,
    allocations: dict,
    targets: dict,
    value_column_name: str = "Current %",
    tolerance_pct: float = 0.1,
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

    table.add_column("Symbol", style="bold")
    table.add_column("Target %", justify="right")
    table.add_column(value_column_name, justify="right")
    table.add_column("Drift", justify="right")
    table.add_column("Status", justify="center")

    stock_symbols, cash_list = _partition_symbols(allocations, targets, drifts)

    for symbol in stock_symbols:
        _add_drift_row(
            table,
            symbol,
            allocations.get(symbol, 0.0),
            targets.get(symbol, 0.0),
            drifts.get(symbol, 0.0),
            tolerance_pct,
        )

    if cash_list:
        table.add_section()
        for symbol in cash_list:
            _add_drift_row(
                table,
                symbol,
                allocations.get(symbol, 0.0),
                targets.get(symbol, 0.0),
                drifts.get(symbol, 0.0),
                tolerance_pct,
            )

    console.print(table)
    console.print()


def display_allocations(
    current_allocations: dict,
    targets: dict,
    tolerance_pct: float,
):
    """Display current vs target allocation table, sorted by drift ascending."""
    _display_allocation_table(
        title="Current vs Target Allocation",
        header_style="bold magenta",
        allocations=current_allocations,
        targets=targets,
        tolerance_pct=tolerance_pct,
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

    table.add_column("Symbol", style="bold")
    table.add_column("Action", justify="center")
    table.add_column("Qty", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Est. Value", justify="right")
    table.add_column("Account")
    table.add_column("Note", style="dim")

    prev_account = None

    for trade in sorted_trades:
        # Add a section divider between different accounts
        account_key = trade.account_number
        if prev_account is not None and prev_account != account_key:
            table.add_section()
        prev_account = account_key

        action_style = "green bold" if trade.action == "BUY" else "red bold"
        account_label = _format_account_label(trade.owner, trade.account_type, trade.account_number)

        table.add_row(
            trade.symbol,
            Text(trade.action, style=action_style),
            str(trade.quantity),
            _format_price(trade.price, trade.currency),
            _format_money(trade.estimated_value, trade.currency),
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

    table.add_column("Account")
    table.add_column("Direction", justify="center")
    table.add_column("Buy", style="bold")
    table.add_column("Shares", justify="right")
    table.add_column("DLR Price", justify="right")
    table.add_column("Amount (incl. fee)", justify="right")

    for conv in conversions:
        account_label = _format_account_label(conv.owner, conv.account_type, conv.account_number)

        if conv.direction == "CAD_TO_USD":
            direction = "CAD -> USD"
            # Fee adds to the CAD you spend
            total_cad = conv.source_amount + conv.fee
            amount_str = f"{_format_money(total_cad)} CAD -> {_format_money(conv.target_amount, 'USD')} USD"
        else:
            direction = "USD -> CAD"
            # Fee subtracts from the CAD you receive
            net_cad = conv.target_amount - conv.fee
            amount_str = f"{_format_money(conv.source_amount, 'USD')} USD -> {_format_money(net_cad)} CAD"

        shares_str = str(conv.dlr_shares) if conv.dlr_shares > 0 else "N/A"
        price_str = _format_money(conv.dlr_price) if conv.dlr_price > 0 else "N/A"

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
        account_label = _format_account_label(alert.owner, alert.account_type, alert.account_number)
        console.print(
            f"  [yellow]⏳ {alert.symbol}:[/yellow] "
            f"{int(alert.quantity)} shares in {account_label} — {alert.note}"
        )
    console.print()


def display_account_summary(accounts: list, usd_to_cad_rate: float):
    """Display a summary of all accounts."""
    table = Table(
        title="Account Summary",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold magenta",
    )

    table.add_column("Owner", style="bold")
    table.add_column("Type")
    table.add_column("Number")
    table.add_column("Total Value (CAD)", justify="right")
    table.add_column("Cash CAD", justify="right")
    table.add_column("Cash USD", justify="right")
    table.add_column("Positions", justify="center")

    # Track totals for the summary row
    total_positions_value_sum = 0.0
    total_value_sum = 0.0
    total_cash_cad_sum = 0.0
    total_cash_usd_sum = 0.0

    for acct in accounts:
        # Count non-zero positions
        pos_count = sum(1 for p in acct.positions if p.quantity > 0)
        pos_symbols = ", ".join(
            sorted(set(p.symbol for p in acct.positions if p.quantity > 0))
        )

        # Calculate account values in CAD
        positions_value_cad = get_account_positions_value_cad(acct, usd_to_cad_rate)
        total_value_cad = get_account_total_value_cad(acct, usd_to_cad_rate)

        # Accumulate totals
        total_positions_value_sum += positions_value_cad
        total_value_sum += total_value_cad
        total_cash_cad_sum += acct.cash_cad
        total_cash_usd_sum += acct.cash_usd

        table.add_row(
            acct.owner,
            acct.account_type,
            acct.number,
            _format_money(total_value_cad),
            _format_money(acct.cash_cad),
            _format_money(acct.cash_usd, "USD"),
            f"{pos_count} ({pos_symbols})" if pos_count > 0 else "0",
        )

    # Add total row
    table.add_section()
    table.add_row(
        "[bold]Total[/bold]",
        "",
        "",
        f"[bold]{_format_money(total_positions_value_sum)}[/bold]",
        f"[bold]{_format_money(total_cash_cad_sum)}[/bold]",
        f"[bold]{_format_money(total_cash_usd_sum, 'USD')}[/bold]",
        f"[bold green]{_format_money(total_value_sum)}[/bold green]",
    )

    console.print(table)
    console.print()


def display_projected_allocations(
    projected_allocations: dict,
    targets: dict,
    tolerance_pct: float,
):
    """Display projected allocation table after trades, sorted by drift ascending."""
    if not projected_allocations:
        return

    _display_allocation_table(
        title="Projected Allocation (After Trades)",
        header_style="bold cyan",
        allocations=projected_allocations,
        targets=targets,
        value_column_name="Projected %",
        tolerance_pct=tolerance_pct,
    )


def display_tactical_posture(tactical_posture) -> None:
    """Display the tactical deployment panel when not at baseline.

    Only renders when the portfolio is in an active deployment regime.
    At baseline, nothing is displayed — the feature is invisible.
    """
    if tactical_posture is None:
        return
    if tactical_posture.regime == "baseline":
        return

    # Format regime label
    regime_labels = {
        "level_1": "Level 1",
        "level_2": "Level 2",
        "level_3": "Level 3",
    }
    regime_label = regime_labels.get(tactical_posture.regime, tactical_posture.regime)

    lines = []
    lines.append(
        f"  [bold yellow]{regime_label}:[/bold yellow] "
        f"{tactical_posture.fixed_pct:.0f}% fixed / {tactical_posture.equity_pct:.0f}% equity"
    )
    lines.append(
        f"  [dim]Reference High:[/dim] "
        f"${tactical_posture.reference_high:,.2f} "
        f"[dim]({tactical_posture.reference_high_date})[/dim]"
    )
    lines.append(
        f"  [dim]Current:[/dim] "
        f"{tactical_posture.drawdown_from_reference_pct:+.1f}% from reference"
    )

    # Recovery triggers
    if tactical_posture.next_recovery_triggers:
        lines.append("")
        lines.append("  [dim]Recovery triggers:[/dim]")
        for trigger in tactical_posture.next_recovery_triggers:
            target_label = regime_labels.get(trigger["target_regime"], trigger["target_regime"])
            if trigger["target_regime"] == "baseline":
                target_label = "Baseline"
            lines.append(
                f"    [green]→[/green] {trigger['drawdown_pct']:+.1f}% "
                f"(${trigger['dollar_value']:,.0f}) → "
                f"{target_label} ({trigger['fixed_pct']:.0f}% fixed)"
            )

    # Deploy trigger
    if tactical_posture.next_deploy_trigger:
        trigger = tactical_posture.next_deploy_trigger
        target_label = regime_labels.get(trigger["target_regime"], trigger["target_regime"])
        lines.append("  [dim]Deploy trigger:[/dim]")
        lines.append(
            f"    [red]→[/red] {trigger['drawdown_pct']:+.1f}% "
            f"(${trigger['dollar_value']:,.0f}) → "
            f"{target_label} ({trigger['fixed_pct']:.0f}% fixed)"
        )

    content = "\n".join(lines)
    panel = Panel(
        content,
        title="[bold yellow]⚡ Tactical Deployment Active[/bold yellow]",
        box=box.ROUNDED,
        style="yellow",
        expand=False,
    )
    console.print(panel)
    console.print()


def display_full_report(
    portfolio,
    current_allocations: dict,
    targets: dict,
    accuracy: float,
    trades: list,
    currency_conversions: list,
    transient_alerts: list,
    usd_to_cad_rate: float,
    projected_accuracy: float = None,
    projected_allocations: dict = None,
    all_time_high=None,
    daily_change=None,
    ytd_history: list = None,
    drift_trade_threshold_pct: float = 0.1,
    tactical_posture=None,
):
    """Display the complete rebalancing report."""
    display_header()
    display_accuracy(accuracy, projected_accuracy)
    display_daily_change(daily_change, portfolio.total_value_cad)
    display_all_time_high(all_time_high, portfolio.total_value_cad)
    console.print()
    display_tactical_posture(tactical_posture)
    display_year_to_date_chart(ytd_history or [])
    display_holdings_summary(portfolio, usd_to_cad_rate)
    display_account_summary(portfolio.accounts, usd_to_cad_rate)
    display_allocations(current_allocations, targets, drift_trade_threshold_pct)
    display_transient_alerts(transient_alerts)
    display_trades(trades)
    display_currency_conversions(currency_conversions)
    display_projected_allocations(projected_allocations, targets, drift_trade_threshold_pct)
