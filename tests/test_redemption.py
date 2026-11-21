from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from src.redemption import account_redemption_limits, parse_redemption_rules


def buy(day, quantity, settled=None):
    return dict(
        symbol="FUND",
        type="Trades",
        action="Buy",
        tradeDate=day,
        settlementDate=settled or day,
        quantity=quantity,
    )


def check(rows, held, today="2028-07-20", cache=None, client=None):
    client = client or Mock()
    # One page contains these acquisitions. All other windows return no activity.
    client.get_activities.side_effect = lambda account, start, end: [
        row for row in rows if start[:10] <= row["tradeDate"] <= end[:10]
    ]
    pos = SimpleNamespace(
        symbol="FUND", symbol_id=33, quantity=held, account_number="123"
    )
    rules = parse_redemption_rules({"FUND": {"minimum_years": 2}})
    now = datetime.fromisoformat(today + "T16:00:00+00:00")
    return account_redemption_limits(client, [pos], rules, now, cache)["FUND"]


def test_maturity_survives_broker_history_expiry_and_prior_redemptions():
    rows = [
        buy("2026-01-07", 9, "2026-01-09"),
        buy("2026-02-05", 4),
    ]
    cache = {}
    check(rows, 13, "2026-03-20", cache)
    check([], 13, "2027-03-01", cache)
    assert check([], 13, "2028-01-08", cache).units == 0
    limit = check([], 13, "2028-01-09", cache)
    assert (limit.units, limit.next_eligible) == (9, date(2028, 2, 5))
    sell = {**buy("2028-01-10", -6), "action": "Sell"}
    assert check([sell], 7, "2028-01-10", cache).units == 3
    assert check([sell], 7, "2028-02-05", cache).units == 7


def test_lookup_continues_across_empty_months_and_stops_on_reconciliation():
    client = Mock()
    cache = {}
    limit = check([buy("2026-04-01", 3)], 3, "2026-06-10", cache, client)
    assert client.get_activities.call_count == 3
    assert limit.next_eligible == date(2028, 4, 1)
    assert cache["123:FUND"]["lots"] == {"2026-04-01": 3}


def test_cache_gap_requires_new_evidence_even_if_quantity_is_unchanged():
    cache = {}
    check([buy("2026-04-01", 3)], 3, "2026-06-10", cache)
    limit = check([], 3, "2028-06-10", cache)
    assert limit.units == 0 and "verified" in limit.reason


def test_missing_or_unsupported_unit_history_blocks_sale():
    assert check([buy("2026-07-01", 5)], 6).units == 0
    other = {**buy("2026-07-02", 1), "action": "Transfer"}
    assert "verified" in check([buy("2026-07-01", 5), other], 6).reason


def test_permanent_rule_needs_no_history_and_lookup_failure_remains_restricted():
    client = Mock()
    client.get_activities.side_effect = requests.ConnectionError()
    p = SimpleNamespace(symbol="FUND", account_number="123")
    assert (
        account_redemption_limits(
            client, [p], parse_redemption_rules({"FUND": "never"})
        )["FUND"].units
        == 0
    )
    client.get_activities.assert_not_called()
    rule = parse_redemption_rules({"FUND": {"minimum_years": 2}})
    assert (
        account_redemption_limits(client, [p], rule, datetime(2026, 2, 1, tzinfo=UTC))[
            "FUND"
        ].units
        == 0
    )


@pytest.mark.parametrize(
    "bad",
    [
        False,
        None,
        {"minimum_years": 0},
        {"minimum_years": 2, "history_from": "invalid"},
    ],
)
def test_invalid_rule_rejected(bad):
    with pytest.raises(ValueError):
        parse_redemption_rules({"FUND": bad})
