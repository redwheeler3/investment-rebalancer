"""Tests for terminal display helpers."""

from datetime import UTC, date, datetime
from io import StringIO

from rich.console import Console

from src import display
from src.history import HistoryPoint


def test_ytd_chart_high_uses_intraday_high(monkeypatch):
    """Chart footer High should use daily high, not latest daily value."""
    output = StringIO()
    monkeypatch.setattr(
        display,
        "console",
        Console(file=output, width=120, height=30, force_terminal=False),
    )

    today = datetime.now(UTC).astimezone().date()
    history_points = [
        HistoryPoint(date=date(today.year, 1, 1), value=100.0, high=100.0),
        HistoryPoint(date=today, value=90.0, high=120.0),
    ]

    display.display_year_to_date_chart(history_points, console_height=14)

    rendered = output.getvalue()
    assert "Latest $90.00" in rendered
    assert "High $120.00" in rendered


def test_household_value_shows_day_change_without_label(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(
        display, "console", Console(file=output, width=140, force_terminal=False)
    )
    display.display_daily_change(10100, 100, 10)
    rendered = output.getvalue()
    assert "Priced holdings:" not in rendered
    assert "$10,100.00" in rendered and "+$100.00 (+10.0%)" in rendered


def test_allocation_tables_hide_internal_transit_key(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(display, "console", Console(file=output, width=100))
    allocations = {"ETF": 90, "TRANSIT:SAVEN:1006": 10}
    display.display_allocations(allocations, {"ETF": 100}, 0.1)
    display.display_projected_allocations(allocations, {"ETF": 100}, 0.1)
    assert "Transit" not in output.getvalue() and "TRANSIT" not in output.getvalue()
    assert output.getvalue().count("ETF") == 2
