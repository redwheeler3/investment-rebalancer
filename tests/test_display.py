"""Tests for terminal display helpers."""

from datetime import date
from io import StringIO

from rich.console import Console

from src.history import HistoryPoint
from src import display


def test_ytd_chart_high_uses_intraday_high(monkeypatch):
    """Chart footer High should use daily high, not latest daily value."""
    output = StringIO()
    monkeypatch.setattr(
        display,
        "console",
        Console(file=output, width=120, height=30, force_terminal=False),
    )

    today = date.today()
    history_points = [
        HistoryPoint(date=date(today.year, 1, 1), value=100.0, high=100.0),
        HistoryPoint(date=today, value=90.0, high=120.0),
    ]

    display.display_year_to_date_chart(history_points, console_height=14)

    rendered = output.getvalue()
    assert "Latest $90.00" in rendered
    assert "High $120.00" in rendered
