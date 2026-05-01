"""
Portfolio history tracking module.

Records daily portfolio values to a JSON file, calculates the all-time high,
and exposes filtered history views for reporting.
One entry per day — multiple runs on the same day overwrite with the latest value.
"""

import json
from datetime import date
from pathlib import Path
from dataclasses import dataclass


# History file path (relative to project root)
HISTORY_FILE = Path(__file__).parent.parent / "data" / "portfolio_history.json"


@dataclass
class AllTimeHigh:
    """All-time high portfolio value."""

    value: float  # Portfolio value in CAD
    date: str  # ISO date string (YYYY-MM-DD)
    is_new_ath: bool  # True if today's value is the new ATH
    drawdown_pct: float  # Current drawdown from ATH (0.0 if at ATH)


@dataclass
class HistoryPoint:
    """A single recorded daily portfolio value."""

    date: date
    value: float


def record_value(total_value_cad: float) -> None:
    """
    Record today's portfolio value in the history file.

    If an entry for today already exists, it's updated with the latest value.
    Creates the data/ directory and history file if they don't exist.

    Args:
        total_value_cad: Current total portfolio value in CAD.
    """
    today = date.today().isoformat()
    history = _load_history()

    # Update or append today's entry
    updated = False
    for entry in history:
        if entry["date"] == today:
            entry["value"] = round(total_value_cad, 2)
            updated = True
            break

    if not updated:
        history.append({"date": today, "value": round(total_value_cad, 2)})

    _save_history(history)


def get_all_time_high(current_value: float = None) -> AllTimeHigh | None:
    """
    Calculate the all-time high from recorded history.

    Args:
        current_value: If provided, used to calculate drawdown from ATH.

    Returns:
        AllTimeHigh with the peak value and date, or None if no history exists.
    """
    history = _load_history()
    if not history:
        return None

    # Find the entry with the highest value
    best = max(history, key=lambda e: e["value"])

    today = date.today().isoformat()
    is_new_ath = best["date"] == today
    drawdown_pct = 0.0

    if current_value is not None and best["value"] > 0:
        drawdown_pct = ((current_value - best["value"]) / best["value"]) * 100.0
        # Drawdown is negative when below ATH, 0 at ATH
        drawdown_pct = min(0.0, drawdown_pct)

    return AllTimeHigh(
        value=best["value"],
        date=best["date"],
        is_new_ath=is_new_ath,
        drawdown_pct=drawdown_pct,
    )


def get_year_to_date_history(today: date | None = None) -> list[HistoryPoint]:
    """Return recorded portfolio values from Jan 1 of the current year through today.

    Missing dates are not synthesized; only recorded values are returned.
    """
    today = today or date.today()
    start_of_year = date(today.year, 1, 1)

    latest_by_day: dict[date, float] = {}
    for entry in _load_history():
        try:
            entry_date = date.fromisoformat(entry["date"])
            entry_value = float(entry["value"])
        except (KeyError, TypeError, ValueError):
            continue

        if start_of_year <= entry_date <= today:
            latest_by_day[entry_date] = entry_value

    return [
        HistoryPoint(date=entry_date, value=latest_by_day[entry_date])
        for entry_date in sorted(latest_by_day)
    ]


def _load_history() -> list:
    """Load history from a JSONL file (one JSON object per line).

    Also handles the legacy format (a single JSON array) for backward
    compatibility — it will be converted to JSONL on the next save.

    Each line: {"date":"2026-04-14","value":156432.50}
    """
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r") as f:
            content = f.read().strip()
        if not content:
            return []

        # Legacy format: entire file is a JSON array
        if content.startswith("["):
            return json.loads(content)

        # JSONL format: one JSON object per line
        entries = []
        for line in content.splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
        return entries
    except (json.JSONDecodeError, IOError):
        return []


def _save_history(history: list) -> None:
    """Save history as JSONL (one JSON object per line). Creates data/ if needed."""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        for entry in history:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
