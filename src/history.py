"""
Portfolio history tracking module.

Records daily portfolio values to a JSON Lines file, calculates the all-time high,
and exposes filtered history views for reporting.
One entry per day — multiple runs on the same day keep the latest value and
the highest intraday value.
"""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime

from src.encrypted_state import read_encrypted_text, write_encrypted_text
from src.paths import get_history_file


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
    high: float


def record_value(total_value_cad: float) -> None:
    """
    Record today's portfolio value in the history file.

    If an entry for today already exists, it's updated with the latest value.
    The entry also keeps the highest value seen that day, so intraday ATHs are
    not lost when a later run records a lower latest value.
    Creates the data/ directory and history file if they don't exist.

    Args:
        total_value_cad: Current total portfolio value in CAD.
    """
    today = datetime.now(UTC).astimezone().date().isoformat()
    history = _load_history()

    rounded_value = round(total_value_cad, 2)

    # Update or append today's entry
    updated = False
    for entry in history:
        if entry["date"] == today:
            previous_high = float(entry["high"])
            entry["value"] = rounded_value
            entry["high"] = max(previous_high, rounded_value)
            updated = True
            break

    if not updated:
        history.append({"date": today, "value": rounded_value, "high": rounded_value})

    _save_history(history)


def get_all_time_high(current_value: float) -> AllTimeHigh:
    """
    Calculate the all-time high, considering both recorded history and
    the live portfolio value.

    The live current_value is compared directly against the historical max,
    so this function works correctly regardless of whether today's value
    has been written to the history file yet.

    Args:
        current_value: Today's live portfolio value in CAD.

    Returns:
        AllTimeHigh with the peak value, date, and drawdown from ATH.
    """
    history = _load_history()
    today = datetime.now(UTC).astimezone().date().isoformat()

    # Find the historical best (if any)
    if history:
        best = max(history, key=lambda e: e["high"])
        historical_max = float(best["high"])
        historical_date = best["date"]
    else:
        historical_max = 0.0
        historical_date = today

    # Determine ATH: is today's live value >= the historical max?
    if current_value >= historical_max:
        return AllTimeHigh(
            value=current_value,
            date=today,
            is_new_ath=True,
            drawdown_pct=0.0,
        )

    return AllTimeHigh(
        value=historical_max,
        date=historical_date,
        is_new_ath=False,
        drawdown_pct=((current_value - historical_max) / historical_max) * 100.0,
    )


def get_year_to_date_history(current_value: float) -> list[HistoryPoint]:
    """Return recorded portfolio values from Jan 1 of the current year through today.

    Today's entry always uses the live current_value so the chart reflects
    the current portfolio state regardless of history file contents.

    Missing dates are not synthesized; only recorded values are returned.
    """
    today = datetime.now(UTC).astimezone().date()
    start_of_year = date(today.year, 1, 1)

    latest_by_day: dict[date, tuple[float, float]] = {}
    for entry in _load_history():
        try:
            entry_date = date.fromisoformat(entry["date"])
            entry_value = float(entry["value"])
            entry_high = float(entry["high"])
        except (KeyError, TypeError, ValueError):
            continue

        if start_of_year <= entry_date <= today:
            latest_by_day[entry_date] = (entry_value, entry_high)

    # Always use the live value for today while preserving any recorded intraday high.
    _today_value, today_high = latest_by_day.get(today, (current_value, current_value))
    latest_by_day[today] = (current_value, max(today_high, current_value))

    return [
        HistoryPoint(
            date=entry_date,
            value=latest_by_day[entry_date][0],
            high=latest_by_day[entry_date][1],
        )
        for entry_date in sorted(latest_by_day)
    ]


def _load_history() -> list:
    """Load history from a JSONL file (one JSON object per line).

    Each line: {"date":"2026-04-14","value":156432.50,"high":156900.00}
    """
    history_file = get_history_file()
    if not history_file.exists():
        return []

    content = read_encrypted_text(history_file).strip()
    if not content:
        return []

    # JSONL format: one JSON object per line
    entries = []
    try:
        for line in content.splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Decrypted portfolio history at '{history_file}' is invalid."
        ) from exc
    return entries


def _save_history(history: list) -> None:
    """Save history as encrypted JSONL without writing plaintext to disk."""
    history_file = get_history_file()
    content = "".join(
        json.dumps(entry, separators=(",", ":")) + "\n" for entry in history
    )
    write_encrypted_text(history_file, content)
