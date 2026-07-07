"""Tests for the history module — ATH tracking, daily change, YTD chart data."""

import json
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from src.history import (
    AllTimeHigh,
    HistoryPoint,
    get_all_time_high,
    get_year_to_date_history,
    record_value,
)


@pytest.fixture
def history_file(tmp_path):
    """Provide a temporary history file path."""
    hf = tmp_path / "data" / "portfolio_history.jsonl"
    with patch("src.history.get_history_file", return_value=hf):
        yield hf


class TestRecordValue:
    def test_creates_file_on_first_record(self, history_file):
        record_value(500000.0)
        assert history_file.exists()
        lines = history_file.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["value"] == 500000.0
        assert entry["high"] == 500000.0
        assert entry["date"] == date.today().isoformat()

    def test_overwrites_same_day(self, history_file):
        record_value(500000.0)
        record_value(510000.0)
        lines = history_file.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["value"] == 510000.0
        assert entry["high"] == 510000.0

    def test_preserves_intraday_high_when_latest_value_is_lower(self, history_file):
        record_value(510000.0)
        record_value(500000.0)
        lines = history_file.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["value"] == 500000.0
        assert entry["high"] == 510000.0

    def test_appends_new_day(self, history_file):
        # Seed with yesterday's entry
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        history_file.parent.mkdir(parents=True, exist_ok=True)
        with open(history_file, "w") as f:
            f.write(json.dumps({
                "date": yesterday,
                "value": 490000.0,
                "high": 490000.0,
            }) + "\n")

        record_value(500000.0)
        lines = history_file.read_text().strip().splitlines()
        assert len(lines) == 2


class TestGetAllTimeHigh:
    def test_new_ath_today(self, history_file):
        # Seed with lower historical value
        history_file.parent.mkdir(parents=True, exist_ok=True)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        with open(history_file, "w") as f:
            f.write(json.dumps({
                "date": yesterday,
                "value": 900000.0,
                "high": 900000.0,
            }) + "\n")

        ath = get_all_time_high(current_value=1000000.0)
        assert ath.is_new_ath is True
        assert ath.value == 1000000.0
        assert ath.drawdown_pct == 0.0

    def test_below_ath(self, history_file):
        # Seed with higher historical value
        history_file.parent.mkdir(parents=True, exist_ok=True)
        past_date = (date.today() - timedelta(days=30)).isoformat()
        with open(history_file, "w") as f:
            f.write(json.dumps({
                "date": past_date,
                "value": 1000000.0,
                "high": 1000000.0,
            }) + "\n")

        ath = get_all_time_high(current_value=900000.0)
        assert ath.is_new_ath is False
        assert ath.value == 1000000.0
        assert abs(ath.drawdown_pct - (-10.0)) < 0.01

    def test_uses_recorded_intraday_high_for_ath(self, history_file):
        today = date.today().isoformat()
        history_file.parent.mkdir(parents=True, exist_ok=True)
        with open(history_file, "w") as f:
            f.write(json.dumps({
                "date": today,
                "value": 900000.0,
                "high": 1000000.0,
            }) + "\n")

        ath = get_all_time_high(current_value=950000.0)
        assert ath.is_new_ath is False
        assert ath.value == 1000000.0
        assert ath.date == today

    def test_no_history(self, history_file):
        """First run — current value is the ATH."""
        ath = get_all_time_high(current_value=500000.0)
        assert ath.is_new_ath is True
        assert ath.value == 500000.0

    def test_drawdown_calculation(self, history_file):
        """Drawdown should be negative percentage."""
        history_file.parent.mkdir(parents=True, exist_ok=True)
        past_date = (date.today() - timedelta(days=10)).isoformat()
        with open(history_file, "w") as f:
            f.write(json.dumps({
                "date": past_date,
                "value": 800000.0,
                "high": 800000.0,
            }) + "\n")

        ath = get_all_time_high(current_value=720000.0)
        assert abs(ath.drawdown_pct - (-10.0)) < 0.01


class TestGetYearToDateHistory:
    def test_includes_current_value(self, history_file):
        result = get_year_to_date_history(current_value=500000.0)
        assert len(result) == 1
        assert result[0].value == 500000.0
        assert result[0].date == date.today()

    def test_excludes_last_year(self, history_file):
        history_file.parent.mkdir(parents=True, exist_ok=True)
        last_year = date(date.today().year - 1, 6, 15).isoformat()
        this_year = date(date.today().year, 2, 1).isoformat()
        with open(history_file, "w") as f:
            f.write(json.dumps({
                "date": last_year,
                "value": 400000.0,
                "high": 400000.0,
            }) + "\n")
            f.write(json.dumps({
                "date": this_year,
                "value": 450000.0,
                "high": 450000.0,
            }) + "\n")

        result = get_year_to_date_history(current_value=500000.0)
        dates = [p.date for p in result]
        assert date.fromisoformat(last_year) not in dates
        assert date.fromisoformat(this_year) in dates

    def test_overrides_today_with_live_value(self, history_file):
        history_file.parent.mkdir(parents=True, exist_ok=True)
        today = date.today().isoformat()
        with open(history_file, "w") as f:
            f.write(json.dumps({
                "date": today,
                "value": 490000.0,
                "high": 490000.0,
            }) + "\n")

        result = get_year_to_date_history(current_value=510000.0)
        today_point = [p for p in result if p.date == date.today()]
        assert len(today_point) == 1
        assert today_point[0].value == 510000.0  # Live value, not recorded
