"""
Portfolio history tracking module.

Records daily portfolio values to a JSON file and calculates the all-time high.
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


def _load_history() -> list:
    """Load history from the JSON file. Returns empty list if file doesn't exist."""
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def _save_history(history: list) -> None:
    """Save history to the JSON file. Creates the data/ directory if needed."""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=None, separators=(",", ":"))
        f.write("\n")
