"""Whole-run transfer scenarios: source limits, arrival order and replay safety."""

from copy import deepcopy
from decimal import Decimal
from datetime import UTC, datetime
from io import StringIO
from unittest.mock import patch

import pytest
from rich.console import Console

import main
from src import display
from src.broker_cash import cash_by_day
from src.external_accounts import (
    ExternalAccountConfig,
    attach_external_balances,
    load_external_balances,
    save_external_balances,
)
from src.external_providers import PROVIDERS
from src.fx_conversions import calculate_currency_needs
from src.fx_math import build_account_trade_impacts, net_account_cash
from src.fx_rate import DlrQuotes
from src.models import TransferRecommendation
from src.portfolio import (
    AccountInfo,
    HoldingSummary,
    PortfolioSummary,
    Position,
    build_allocation_snapshot,
    simulate_rebalance,
)
from src.rebalancer import calculate_trades
from src.saven_export import read_saven_export
from src.tactical import TacticalState, evaluate_tactical_posture, parse_tactical_config
from src.transfer_tracking import TransferLedger, cents, reconcile_transfer_activity

SAVEN = "SAVEN:0001112223"
NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
ACCOUNT = AccountInfo("broker", "Margin", "Individual", "Example")
ROUTES = {SAVEN: ACCOUNT}
CONFIGS = {SAVEN: ExternalAccountConfig(SAVEN, "Saven", "HISA", "Example", "Margin")}


def export(tmp_path, rows=None, name="bank.csv"):
    rows = rows if rows is not None else [("01/11/2026", "Deposit", 500000, 500000)]
    path = tmp_path / name
    path.write_text(
        "Your Transactions List\n\nAccount Number: 0001112223\n\n"
        "Date,Description,Amount (CAD),Balance (CAD)\n"
        + "".join(
            f"{day},{description},{amount:.2f},+${balance:.2f}\n"
            for day, description, amount, balance in rows
        ),
        encoding="utf-8",
    )
    result = read_saven_export(path)
    return result.as_snapshot(), result


def activity(amount, day=2, currency="CAD", kind=None):
    timestamp = f"2026-11-{day:02d}T00:00:00-05:00"
    return {
        "type": kind or ("Deposits" if amount > 0 else "Withdrawals"),
        "currency": currency,
        "symbol": "",
        "netAmount": amount,
        "tradeDate": timestamp,
        "transactionDate": timestamp,
        "settlementDate": timestamp,
        "description": "EFT",
        "action": "EFT",
    }


def recommendation(action="DEPOSIT", amount=180000, in_progress=False):
    return TransferRecommendation(
        SAVEN, action, amount, "broker", "Margin", "Example", in_progress=in_progress
    )


def baseline(tmp_path, action="DEPOSIT", old_activities=None):
    snapshot, parsed = export(tmp_path)
    ledger = TransferLedger()
    ledger.observe_external_balance(SAVEN, snapshot, parsed)
    ledger.observe_broker(SAVEN, "broker", old_activities or [], NOW)
    ledger.remember_recommendations([recommendation(action)], ROUTES, NOW)
    return ledger, snapshot


def portfolio(snapshot, broker_value=500000, cash=0):
    equity = broker_value - cash
    account = AccountInfo("broker", "Margin", "Individual", "Example", cash_cad=cash)
    account.positions = [
        Position(
            "EQ", 1, equity / 100, equity, 100, "CAD", "broker", "Margin", "Example"
        )
    ]
    result = PortfolioSummary(
        accounts=[account],
        holdings={
            "EQ": HoldingSummary(
                value_cad=equity,
                total_quantity=equity / 100,
                current_price=100,
                bid_price=100,
                ask_price=100,
            )
        },
        total_value_cad=broker_value,
        cash_cad_total=cash,
    )
    attach_external_balances(result, {SAVEN: snapshot}, CONFIGS)
    return result


def bank_withdrawal(tmp_path, amount, interest=0):
    rows = [
        ("02/11/2026", "Withdrawal", amount, 500000 - amount),
        ("01/11/2026", "Deposit", 500000, 500000),
    ]
    if interest:
        rows.insert(
            0, ("03/11/2026", "Credit interest", interest, 500000 - amount + interest)
        )
    return export(tmp_path, rows)


def bank_deposit(tmp_path, amount):
    return export(
        tmp_path,
        [
            ("02/11/2026", "Deposit", amount, 500000 + amount),
            ("01/11/2026", "Deposit", 500000, 500000),
        ],
    )


@pytest.mark.parametrize(
    "amount,transit,total",
    [(165000, 165000, 1000000), (180000, 180000, 1000000), (200000, 180000, 980000)],
)
@pytest.mark.parametrize("action", ["DEPOSIT", "WITHDRAW"])
def test_source_amount_is_capped_without_fictitious_cash(
    tmp_path, amount, transit, total, action
):
    ledger, bank = baseline(tmp_path, action)
    broker_value = 500000
    if action == "DEPOSIT":
        ledger.observe_broker(SAVEN, "broker", [activity(-amount)])
        broker_value -= amount
    else:
        bank, parsed = bank_withdrawal(tmp_path, amount)
        ledger.observe_external_balance(SAVEN, bank, parsed)
    current = portfolio(bank, broker_value)
    ledger.apply(current, ROUTES)
    assert current.total_value_cad == total
    assert current.transfer_progress[0].in_transit_cad == transit
    assert current.cash_cad_total == current.accounts[0].cash_cad == 0
    assert current.external_balances[SAVEN].balance_cad == float(bank["balance_cad"])


@pytest.mark.parametrize("action", ["DEPOSIT", "WITHDRAW"])
def test_top_up_from_external_bank_is_counted_once(tmp_path, action):
    ledger, bank = baseline(tmp_path, action)
    if action == "DEPOSIT":
        ledger.observe_broker(SAVEN, "broker", [activity(-165000)])
        bank, parsed = bank_deposit(tmp_path, 180000)
        ledger.observe_external_balance(SAVEN, bank, parsed)
        broker_value, cash = 335000, 0
    else:
        bank, parsed = bank_withdrawal(tmp_path, 165000)
        ledger.observe_external_balance(SAVEN, bank, parsed)
        ledger.observe_broker(SAVEN, "broker", [activity(180000)])
        broker_value, cash = 680000, 180000
    current = portfolio(bank, broker_value, cash)
    ledger.apply(current, ROUTES)
    assert current.total_value_cad == 1015000
    if action == "DEPOSIT":
        assert current.transfer_progress == []
    else:
        assert current.transfer_progress[0].in_transit_cad == 0
        assert current.transfer_progress[0].source_offset_cad == 0
    ledger.remember_recommendations([recommendation(action, 10000)], ROUTES)
    assert ledger.account(SAVEN)["cycle"]["limit"] == cents(10000)


@pytest.mark.parametrize("receipt,total", [(180000, 980000), (200000, 1000000)])
@pytest.mark.parametrize("action", ["DEPOSIT", "WITHDRAW"])
def test_excess_withdrawal_is_an_outflow_but_actual_receipt_is_uncapped(
    tmp_path, receipt, total, action
):
    ledger, bank = baseline(tmp_path, action)
    if action == "DEPOSIT":
        ledger.observe_broker(SAVEN, "broker", [activity(-200000)])
        bank, parsed = bank_deposit(tmp_path, receipt)
        ledger.observe_external_balance(SAVEN, bank, parsed)
        broker_value, cash = 300000, 0
    else:
        bank, parsed = bank_withdrawal(tmp_path, 200000)
        ledger.observe_external_balance(SAVEN, bank, parsed)
        ledger.observe_broker(SAVEN, "broker", [activity(receipt)])
        broker_value, cash = 500000 + receipt, receipt
    current = portfolio(bank, broker_value, cash)
    ledger.apply(current, ROUTES)
    assert current.total_value_cad == total
    assert sum(p.in_transit_cad for p in current.transfer_progress) == 0


def test_recipient_first_offsets_stale_saven_until_csv_confirms_partial_source(
    tmp_path,
):
    ledger, bank = baseline(tmp_path, "WITHDRAW")
    ledger.observe_broker(SAVEN, "broker", [activity(180000)])
    current = portfolio(bank, 680000, 180000)
    ledger.apply(current, ROUTES)
    assert current.total_value_cad == 1000000
    assert current.external_balances[SAVEN].balance_cad == 500000
    assert current.holdings[SAVEN].value_cad == 320000
    assert current.cash_cad_total == 180000
    assert current.transfer_progress[0].source_offset_cad == 180000
    bank, parsed = bank_withdrawal(tmp_path, 165000)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    settled = portfolio(bank, 680000, 180000)
    ledger.apply(settled, ROUTES)
    assert settled.total_value_cad == 1015000
    assert settled.transfer_progress[0].source_offset_cad == 0


def test_destination_first_deposit_and_later_broker_record_do_not_double_count(
    tmp_path,
):
    ledger, bank = baseline(tmp_path)
    bank, parsed = bank_deposit(tmp_path, 180000)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    current = portfolio(bank, 320000)
    ledger.apply(current, ROUTES)
    assert current.total_value_cad == 1000000
    ledger.observe_broker(SAVEN, "broker", [activity(-180000)])
    repeated = portfolio(bank, 320000)
    ledger.apply(repeated, ROUTES)
    assert repeated.total_value_cad == 1000000
    assert repeated.transfer_progress == []


def test_partial_duplicate_looking_withdrawals_are_counted_once_and_share_one_cap(
    tmp_path,
):
    ledger, bank = baseline(tmp_path, old_activities=[activity(-90000, day=1)])
    first = activity(-50000)
    ledger.observe_broker(SAVEN, "broker", [activity(-90000, day=1), first])
    ledger.observe_broker(SAVEN, "broker", [first, first, activity(-90000, day=1)])
    ledger.observe_broker(SAVEN, "broker", [first, activity(-90000, day=1), first])
    assert ledger.progress(SAVEN, ACCOUNT).source_cad == 100000
    ledger.remember_recommendations([recommendation(amount=300000)], ROUTES)
    assert ledger.account(SAVEN)["cycle"]["limit"] == cents(180000)
    ledger.observe_broker(SAVEN, "broker", [first, first, activity(-100000, day=3)])
    assert ledger.progress(SAVEN, ACCOUNT).source_cad == 180000


@pytest.mark.parametrize("broker_debit", [0, 6000, 10000])
def test_statement_fulfills_deposit_without_confirmation(tmp_path, broker_debit):
    ledger, _ = baseline(tmp_path)
    ledger.remember_recommendations([], ROUTES, NOW)
    ledger.remember_recommendations([recommendation(amount=10000)], ROUTES, NOW)
    bank, parsed = bank_deposit(tmp_path, 10000)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    if broker_debit:
        ledger.observe_broker(SAVEN, ACCOUNT.number, [activity(-broker_debit)])
    for _ in range(2):
        p = portfolio(bank, 500000 - broker_debit)
        ledger.apply(p, ROUTES)
        assert p.total_value_cad == 1010000 - broker_debit
        assert p.transfer_progress == []
        ledger = TransferLedger(ledger.data)
        ledger.observe_external_balance(SAVEN, bank, parsed)


@pytest.mark.parametrize("direction", ["DEPOSIT", "WITHDRAW"])
def test_late_debit_after_completed_receipt_cannot_fund_new_cycle(tmp_path, direction):
    from dataclasses import replace

    ledger, _ = baseline(tmp_path)
    ledger.remember_recommendations([], ROUTES, NOW)
    account = replace(ACCOUNT, cash_cad=10000, start_of_day_cash_cad=10000)
    day = NOW.date()
    ledger.observe_broker_cash(SAVEN, account, {}, day)
    ledger.remember_recommendations([recommendation(amount=10000)], ROUTES, NOW)
    account.cash_cad -= 6000
    ledger.observe_broker_cash(SAVEN, account, {}, day)
    bank, parsed = bank_deposit(tmp_path, 10000)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    p = portfolio(bank, 494000, account.cash_cad)
    ledger.apply(p, ROUTES)
    assert p.transfer_progress == []
    ledger.remember_recommendations([recommendation(direction, 8000)], ROUTES, NOW)
    for amount in (2000, 6000, 6000):
        ledger = TransferLedger(ledger.data)
        rows = [activity(-2000, day=1)]
        if amount == 6000:
            rows.append(activity(-4000, day=1))
        ledger.observe_broker(SAVEN, account.number, rows, NOW)
        ledger.observe_broker_cash(SAVEN, account, {day: -amount}, day)
        progress = ledger.progress(SAVEN, account)
        assert (
            progress.source_cad
            == progress.received_cad
            == progress.unposted_broker_cad
            == 0
        )
        assert progress.in_transit_cad == progress.source_offset_cad == 0
    account.cash_cad += 1000 if direction == "WITHDRAW" else -1000
    ledger.observe_broker_cash(SAVEN, account, {day: -6000}, day)
    assert ledger.progress(SAVEN, account).unposted_broker_cad == 1000


def test_new_debit_posts_before_backdated_confirmation_from_completed_cycle(tmp_path):
    from dataclasses import replace

    ledger, _ = baseline(tmp_path)
    first = replace(ACCOUNT, start_of_day_cash_cad=200000, cash_cad=140000)
    ledger.observe_broker_cash(SAVEN, first, {}, NOW.date())
    bank, parsed = bank_deposit(tmp_path, 180000)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    ledger.apply(portfolio(bank, 440000), {SAVEN: first})
    # No recommendation for a day. Only the previously observed $60k debit
    # carries forward; the $120k supplied outside the portfolio is complete.
    ledger = TransferLedger(ledger.data)
    today = NOW.replace(day=3)
    second = replace(first, start_of_day_cash_cad=140000)
    ledger.observe_broker_cash(SAVEN, second, {}, today.date())
    ledger.remember_recommendations(
        [recommendation(amount=50000)], {SAVEN: second}, today
    )
    second.cash_cad -= 20000
    fresh = activity(-20000, day=3)
    old = activity(-60000, day=2)
    for rows, still_unposted in (
        ([fresh], 20000),
        ([old, fresh], 0),
        ([fresh, old], 0),
    ):
        ledger = TransferLedger(ledger.data)
        ledger.observe_broker(SAVEN, second.number, rows, today)
        ledger.observe_broker_cash(SAVEN, second, {today.date(): -20000}, today.date())
        progress = ledger.progress(SAVEN, second)
        assert progress.source_cad == progress.in_transit_cad == 20000
        assert progress.unposted_broker_cad == still_unposted


def test_bank_replay_interest_and_same_day_round_trip_are_safe(tmp_path):
    ledger, bank = baseline(tmp_path, "WITHDRAW")
    bank, parsed = export(
        tmp_path,
        [
            ("01/11/2026", "Deposit", 100000, 500000),
            ("01/11/2026", "Withdrawal", 100000, 400000),
            ("01/11/2026", "Deposit", 500000, 500000),
        ],
    )
    ledger.observe_external_balance(SAVEN, bank, parsed)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    assert ledger.account(SAVEN)["bank_out"] == cents(100000)
    assert ledger.account(SAVEN)["bank_in"] == cents(100000)
    bank, parsed = export(
        tmp_path,
        [
            ("02/11/2026", "Credit interest", 19.91, 500019.91),
            ("01/11/2026", "Deposit", 100000, 500000),
            ("01/11/2026", "Withdrawal", 100000, 400000),
            ("01/11/2026", "Deposit", 500000, 500000),
        ],
    )
    ledger.observe_external_balance(SAVEN, bank, parsed)
    assert ledger.account(SAVEN)["bank_in"] == cents(100000)


def test_noncontinuous_csv_fails_without_partially_advancing_state(tmp_path):
    ledger, bank = baseline(tmp_path)
    before = deepcopy(ledger.data)
    bank, parsed = export(tmp_path, [("02/11/2026", "Deposit", 100000, 700000)])
    with pytest.raises(ValueError, match="continuous activity"):
        ledger.observe_external_balance(SAVEN, bank, parsed)
    assert ledger.data == before


def test_bank_replay_uses_the_tracked_close_when_a_statement_label_changes(tmp_path):
    ledger, _ = baseline(tmp_path, "WITHDRAW")
    opening = ("01/11/2026", "Deposit", 500000, 500000)
    bank, parsed = export(
        tmp_path, [("02/11/2026", "Preauthorized Debit SVF", 50000, 450000), opening]
    )
    ledger.observe_external_balance(SAVEN, bank, parsed)
    bank, parsed = export(
        tmp_path,
        [
            ("03/11/2026", "Withdrawal", 30000, 420000),
            ("02/11/2026", "Preauthorized Debit SVF SVF", 50000, 450000),
            opening,
        ],
    )
    ledger.observe_external_balance(SAVEN, bank, parsed)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    assert ledger.progress(SAVEN, ACCOUNT).in_transit_cad == 80000
    assert ledger.account(SAVEN)["bank"]["balance"] == cents(420000)


@pytest.mark.parametrize("include_anchor", [False, True])
def test_cached_only_startup_can_reconcile_a_later_statement(tmp_path, include_anchor):
    bank, _ = export(tmp_path)
    ledger = TransferLedger()
    ledger.observe_external_balance(SAVEN, bank)
    ledger.observe_broker(SAVEN, "broker", [], NOW)
    ledger.remember_recommendations([recommendation("WITHDRAW")], ROUTES, NOW)
    rows = [("02/11/2026", "Withdrawal", 180000, 320000)]
    if include_anchor:
        rows.append(("01/11/2026", "Deposit", 500000, 500000))
    bank, parsed = export(tmp_path, rows)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    assert ledger.progress(SAVEN, ACCOUNT).in_transit_cad == 180000


def test_expanding_a_csv_date_range_does_not_replay_older_bank_history(tmp_path):
    bank, _ = export(tmp_path)
    ledger = TransferLedger()
    ledger.observe_external_balance(SAVEN, bank)
    ledger.observe_broker(SAVEN, "broker", [], NOW)
    ledger.remember_recommendations([recommendation("WITHDRAW")], ROUTES, NOW)
    rows = [("01/11/2026", "Withdrawal", 180000, 320000)]
    bank, parsed = export(tmp_path, rows)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    rows.append(("01/11/2026", "Deposit", 500000, 500000))
    bank, parsed = export(tmp_path, rows)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    assert ledger.progress(SAVEN, ACCOUNT).source_cad == 180000
    assert ledger.account(SAVEN)["bank_in"] == 0


def test_partly_settled_cycle_keeps_its_remaining_limit(tmp_path):
    ledger, bank = baseline(tmp_path)
    ledger.observe_broker(SAVEN, "broker", [activity(-50000)])
    bank, parsed = bank_deposit(tmp_path, 50000)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    ledger.remember_recommendations([recommendation(amount=300000)], ROUTES)
    assert ledger.account(SAVEN)["cycle"]["limit"] == cents(180000)
    ledger.observe_broker(SAVEN, "broker", [activity(-50000), activity(-150000, day=3)])
    assert ledger.progress(SAVEN, ACCOUNT).in_transit_cad == 130000


def test_partly_settled_cycle_is_not_lost_when_remaining_gap_is_below_tolerance(
    tmp_path,
):
    ledger, _ = baseline(tmp_path)
    ledger.observe_broker(SAVEN, "broker", [activity(-165000)])
    bank, parsed = bank_deposit(tmp_path, 165000)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    ledger.remember_recommendations([], ROUTES)
    ledger.observe_broker(SAVEN, "broker", [activity(-165000), activity(-15000, day=3)])
    assert ledger.progress(SAVEN, ACCOUNT).in_transit_cad == 15000


def test_settled_cycle_starts_new_intent_without_reusing_past_activity(tmp_path):
    ledger, bank = baseline(tmp_path)
    rows = [activity(-180000)]
    ledger.observe_broker(SAVEN, "broker", rows)
    bank, parsed = bank_deposit(tmp_path, 180000)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    ledger.remember_recommendations([recommendation(amount=10000)], ROUTES)
    ledger.observe_broker(SAVEN, "broker", rows)
    assert ledger.progress(SAVEN, ACCOUNT).in_transit_cad == 0
    assert ledger.progress(SAVEN, ACCOUNT).recommended_cad == 10000


@pytest.mark.parametrize(
    "action,targets",
    [("DEPOSIT", {"EQ": 32, SAVEN: 68}), ("WITHDRAW", {"EQ": 68, SAVEN: 32})],
)
def test_in_flight_transfer_funds_projection_once_without_changing_actual_cash(
    tmp_path, action, targets
):
    ledger, bank = baseline(tmp_path, action)
    broker_value = 500000
    if action == "DEPOSIT":
        ledger.observe_broker(SAVEN, "broker", [activity(-180000)])
        broker_value = 320000
    else:
        bank, parsed = bank_withdrawal(tmp_path, 180000)
        ledger.observe_external_balance(SAVEN, bank, parsed)
    current = portfolio(bank, broker_value)
    ledger.apply(current, ROUTES)
    actions = calculate_trades(current, targets, 1.3, 0, 0.1, set(), None, ROUTES)
    assert not any(
        isinstance(item, TransferRecommendation) and not item.in_progress
        for item in actions
    )
    in_flight = [item for item in actions if isinstance(item, TransferRecommendation)]
    assert len(in_flight) == 1 and in_flight[0].amount_cad == 180000
    projected = simulate_rebalance(current, actions, targets, 1.3)
    assert projected.accuracy == pytest.approx(100)
    assert current.cash_cad_total == current.accounts[0].cash_cad == 0
    impacts = build_account_trade_impacts(actions)
    cash = net_account_cash(current.accounts[0], impacts.get("broker"))
    assert cash.cad >= 0
    conversions = calculate_currency_needs(
        actions, current.accounts, 1.3, DlrQuotes(13, 13, 10, 10, 1.3), targets, 0
    )
    assert conversions == []


def test_bank_bound_transit_counts_toward_saven_allocation_not_a_false_drift(tmp_path):
    ledger, bank = baseline(tmp_path)
    ledger.observe_broker(SAVEN, "broker", [activity(-180000)])
    current = portfolio(bank, 320000)
    ledger.apply(current, ROUTES)
    snapshot = build_allocation_snapshot(current, {"EQ": 32, SAVEN: 68}, 1.3)
    assert snapshot.accuracy == 100
    assert snapshot.allocations[SAVEN] == 68
    assert (
        current.external_balances[SAVEN].balance_cad
        == current.holdings[SAVEN].value_cad
        == 500000
    )


def test_partial_transit_and_remaining_recommendation_are_not_netted_together(tmp_path):
    ledger, bank = baseline(tmp_path)
    ledger.observe_broker(SAVEN, "broker", [activity(-165000)])
    current = portfolio(bank, 335000)
    ledger.apply(current, ROUTES)
    actions = calculate_trades(
        current, {"EQ": 32, SAVEN: 68}, 1.3, 0, 0.1, set(), None, ROUTES
    )
    assert {
        (item.in_progress, item.amount_cad)
        for item in actions
        if isinstance(item, TransferRecommendation)
    } == {(True, 165000), (False, 15000)}


def test_reconciled_valuation_prevents_false_tactical_trigger(tmp_path):
    ledger, bank = baseline(tmp_path)
    ledger.observe_broker(SAVEN, "broker", [activity(-180000)])
    current = portfolio(bank, 320000)
    assert current.total_value_cad == 820000
    ledger.apply(current, ROUTES)
    config = parse_tactical_config(
        {
            "baseline_fixed_pct": 20,
            "fixed_composition": {SAVEN: 100},
            "deploy_thresholds": [{"drawdown_pct": -8, "fixed_pct": 15}],
        }
    )
    with (
        patch("src.tactical.load_tactical_state", return_value=TacticalState()),
        patch("src.tactical.save_tactical_state") as save,
    ):
        assert (
            evaluate_tactical_posture(
                current.total_value_cad, 1000000, "2026-11-01", config
            ).regime
            == "baseline"
        )
    save.assert_not_called()


@pytest.mark.parametrize("recipient_first", [False, True])
def test_rendered_summaries_reconcile_and_names_are_shortened(
    tmp_path, monkeypatch, recipient_first
):
    ledger, bank = baseline(tmp_path, "WITHDRAW")
    if recipient_first:
        ledger.observe_broker(SAVEN, "broker", [activity(180000)])
        current = portfolio(bank, 680000, 180000)
    else:
        bank, parsed = bank_withdrawal(tmp_path, 180000)
        ledger.observe_external_balance(SAVEN, bank, parsed)
        current = portfolio(bank)
    ledger.apply(current, ROUTES)
    actions = calculate_trades(
        current, {"EQ": 68, SAVEN: 32}, 1.3, 0, 0.1, set(), None, ROUTES
    )
    output = StringIO()
    monkeypatch.setattr(
        display, "console", Console(file=output, width=160, force_terminal=False)
    )
    display.display_holdings_summary(current, 1.3)
    display.display_account_summary(current, 1.3)
    snapshot = build_allocation_snapshot(current, {"EQ": 68, SAVEN: 32}, 1.3)
    display.display_allocations(
        snapshot.allocations, {"EQ": 68, SAVEN: 32}, 0.1, current.external_balances
    )
    display.display_external_transfers(current, actions)
    rendered = output.getvalue()
    assert (
        len(
            [
                line
                for line in rendered.splitlines()
                if "Total" in line and "$1,000,000.00" in line
            ]
        )
        == 2
    )
    assert SAVEN not in rendered and "SAVEN:...2223" in rendered
    assert ("Awaiting export" if recipient_first else "In transit") in rendered


def test_csv_refresh_then_scheduled_reconciliation_preserves_transfer_evidence(
    tmp_path, encrypted_store
):
    data, storage = encrypted_store
    bank, parsed = export(tmp_path)
    assert load_external_balances(CONFIGS, tmp_path)[SAVEN] == bank
    ledger = TransferLedger.load()
    ledger.observe_broker(SAVEN, "broker", [], NOW)
    ledger.remember_recommendations([recommendation("WITHDRAW")], ROUTES, NOW)
    ledger.save()
    new_bank, parsed = bank_withdrawal(tmp_path, 165000)
    balances = load_external_balances(CONFIGS, tmp_path)
    assert balances[SAVEN] == new_bank
    assert storage[data / "transfer_tracking.json"]["accounts"][SAVEN][
        "bank_out"
    ] == cents(165000)

    class Client:
        def get_accounts(self):
            return [{"number": "broker"}]

        def get_activities(self, number, start, end):
            return [activity(180000)]

    current = portfolio(new_bank, 680000, 180000)
    reconcile_transfer_activity(current, balances, [Client()], ROUTES)
    assert current.total_value_cad == 1015000
    assert current.transfer_progress[0].in_transit_cad == 0
    before = deepcopy(storage[data / "transfer_tracking.json"])
    load_external_balances(CONFIGS, tmp_path)
    assert storage[data / "transfer_tracking.json"] == before
    assert set(storage[data / "saven_balance.json"]["accounts"]["0001112223"]) == {
        "balance_cad",
        "last_transaction_date",
        "source_modified_at",
        "source_sha256",
    }


def test_pending_route_cannot_be_pruned_out_of_the_saved_bank_cache(
    tmp_path, encrypted_store
):
    data, storage = encrypted_store
    export(tmp_path)
    balances = load_external_balances(CONFIGS, tmp_path)
    ledger = TransferLedger.load()
    ledger.observe_broker(SAVEN, "broker", [], NOW)
    ledger.remember_recommendations([recommendation()], ROUTES, NOW)
    ledger.observe_broker(SAVEN, "broker", [activity(-50000)])
    ledger.save()
    saved = deepcopy(storage[data / "saven_balance.json"])
    with pytest.raises(ValueError, match="until its transfer settles"):
        save_external_balances(
            PROVIDERS["SAVEN"],
            data / "saven_balance.json",
            {"accounts": {}},
            {"accounts": {"0001112223": balances[SAVEN]}},
            {},
            set(),
        )
    assert storage[data / "saven_balance.json"] == saved


def test_activity_windows_preserve_duplicate_multiplicity_without_boundary_replays(
    tmp_path, encrypted_store
):
    ledger, bank = baseline(tmp_path)
    ledger.path = encrypted_store[0] / "transfer_tracking.json"
    ledger.account(SAVEN)["cycle"]["started_at"] = "2026-08-01T00:00:00+00:00"
    ledger.save()
    windows = []

    class Client:
        def get_accounts(self):
            return [{"number": "broker"}]

        def get_activities(self, number, start, end):
            windows.append((datetime.fromisoformat(start), datetime.fromisoformat(end)))
            return [activity(-50000), activity(-50000)]

    current = portfolio(bank, 400000)
    reconcile_transfer_activity(current, {SAVEN: bank}, [Client()], ROUTES)
    assert len(windows) > 1
    assert all((end - start).days <= 30 for start, end in windows)
    assert current.transfer_progress[0].in_transit_cad == 100000
    assert current.total_value_cad == 1000000


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1.001", None])
def test_invalid_money_cannot_enter_tracking(value):
    with pytest.raises(ValueError, match="finite CAD"):
        cents(value)


def test_endpoint_change_with_unsettled_transfer_is_rejected(tmp_path):
    ledger, _ = baseline(tmp_path)
    ledger.observe_broker(SAVEN, "broker", [activity(-50000)])
    with pytest.raises(ValueError, match="Cannot change"):
        ledger.observe_broker(SAVEN, "another", [])


def test_non_transfer_activity_is_not_misclassified(tmp_path):
    ledger, _ = baseline(tmp_path)
    ledger.observe_broker(
        SAVEN,
        "broker",
        [
            activity(-50000, currency="USD"),
            activity(-10, kind="Fees and rebates"),
            activity(100, kind="Interest"),
        ],
    )
    assert ledger.progress(SAVEN, ACCOUNT).source_cad == 0
    assert ledger.progress(SAVEN, ACCOUNT).received_cad == 0


def test_cash_record_description_or_number_format_changes_do_not_replay_it(tmp_path):
    ledger, _ = baseline(tmp_path)
    original = activity(-50000)
    ledger.observe_broker(SAVEN, "broker", [original])
    updated = dict(
        original,
        netAmount=-50000.0,
        description="Withdrawal confirmed",
        transactionDate="2026-11-02T05:00:00+00:00",
    )
    ledger.observe_broker(SAVEN, "broker", [updated])
    assert ledger.progress(SAVEN, ACCOUNT).source_cad == 50000


def test_late_posted_history_from_before_the_recommendation_is_not_matched(tmp_path):
    ledger, _ = baseline(tmp_path)
    old = activity(-50000)
    old["transactionDate"] = "2026-10-30T00:00:00-04:00"
    ledger.observe_broker(SAVEN, "broker", [old, activity(-10000)])
    assert ledger.progress(SAVEN, ACCOUNT).source_cad == 10000


def test_saven_service_fee_remains_an_expense_not_transfer_principal(tmp_path):
    ledger, _ = baseline(tmp_path, "WITHDRAW")
    bank, parsed = export(
        tmp_path,
        [
            ("02/11/2026", "Service charge", 5, 499995),
            ("01/11/2026", "Deposit", 500000, 500000),
        ],
    )
    ledger.observe_external_balance(SAVEN, bank, parsed)
    current = portfolio(bank)
    ledger.apply(current, ROUTES)
    assert current.total_value_cad == 999995
    assert current.transfer_progress[0].in_transit_cad == 0


def test_interest_only_statement_does_not_clear_a_recipient_first_offset(tmp_path):
    ledger, _ = baseline(tmp_path, "WITHDRAW")
    ledger.observe_broker(SAVEN, "broker", [activity(180000)])
    bank, parsed = export(
        tmp_path,
        [
            ("02/11/2026", "Credit interest", 19.91, 500019.91),
            ("01/11/2026", "Deposit", 500000, 500000),
        ],
    )
    ledger.observe_external_balance(SAVEN, bank, parsed)
    current = portfolio(bank, 680000, 180000)
    ledger.apply(current, ROUTES)
    assert current.total_value_cad == pytest.approx(1000019.91)
    assert current.transfer_progress[0].source_offset_cad == 180000


@pytest.mark.parametrize("action", ["DEPOSIT", "WITHDRAW"])
def test_partial_destination_arrivals_release_only_received_principal(tmp_path, action):
    ledger, bank = baseline(tmp_path, action)
    if action == "DEPOSIT":
        ledger.observe_broker(SAVEN, "broker", [activity(-180000)])
    else:
        bank, parsed = bank_withdrawal(tmp_path, 180000)
        ledger.observe_external_balance(SAVEN, bank, parsed)
    received = []
    bank_rows = [("01/11/2026", "Deposit", 500000, 500000)]
    total_received = 0
    for day, amount in [(3, 50000), (4, 100000), (5, 30000)]:
        total_received += amount
        if action == "DEPOSIT":
            bank_rows.insert(
                0, (f"0{day}/11/2026", "Deposit", amount, 500000 + total_received)
            )
            bank, parsed = export(tmp_path, bank_rows)
            ledger.observe_external_balance(SAVEN, bank, parsed)
            current = portfolio(bank, 320000)
        else:
            received.append(activity(amount, day))
            ledger.observe_broker(SAVEN, "broker", received)
            current = portfolio(bank, 500000 + total_received, total_received)
        ledger.apply(current, ROUTES)
        assert current.total_value_cad == 1000000
        assert (
            sum(p.in_transit_cad for p in current.transfer_progress)
            == 180000 - total_received
        )


def test_same_day_duplicate_bank_rows_keep_multiplicity_across_full_exports(tmp_path):
    ledger, bank = baseline(tmp_path, "WITHDRAW")
    rows = [
        ("01/11/2026", "Deposit", 100000, 500000),
        ("01/11/2026", "Withdrawal", 100000, 400000),
        ("01/11/2026", "Deposit", 500000, 500000),
    ]
    bank, parsed = export(tmp_path, rows)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    rows = rows[:2] + rows
    bank, parsed = export(tmp_path, rows)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    assert ledger.account(SAVEN)["bank_out"] == cents(200000)
    assert ledger.progress(SAVEN, ACCOUNT).source_cad == 180000


def test_no_intent_means_an_ordinary_withdrawal(tmp_path):
    ledger, bank = baseline(tmp_path)
    ledger.remember_recommendations([], ROUTES)
    ledger.observe_broker(SAVEN, "broker", [activity(-200000)])
    current = portfolio(bank, 300000)
    ledger.apply(current, ROUTES)
    assert current.total_value_cad == 800000
    assert current.transfer_progress == []


def test_removing_a_route_cannot_drop_real_money_in_transit(tmp_path):
    ledger, _ = baseline(tmp_path)
    ledger.observe_broker(SAVEN, "broker", [activity(-50000)])
    with pytest.raises(ValueError, match="until its transfer settles"):
        ledger.retain_routes({})


def test_idle_recommendation_can_change_broker_endpoint_safely(tmp_path):
    ledger, _ = baseline(tmp_path)
    ledger.observe_broker(SAVEN, "new-account", [])
    assert ledger.account(SAVEN)["cycle"] is None
    assert ledger.account(SAVEN)["broker_account"] == "new-account"


def test_transit_withdrawal_can_fund_usd_buys_and_conversion_in_one_projection(
    tmp_path,
):
    ledger, bank = baseline(tmp_path, "WITHDRAW")
    bank, parsed = bank_withdrawal(tmp_path, 180000)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    current = portfolio(bank)
    holding = current.holdings["EQ"]
    holding.currency = "USD"
    holding.total_quantity /= 1.3
    position = current.accounts[0].positions[0]
    position.currency = "USD"
    position.quantity /= 1.3
    position.market_value /= 1.3
    ledger.apply(current, ROUTES)
    targets = {"EQ": 68, SAVEN: 32}
    quotes = DlrQuotes(13, 13, 10, 10, 1.3)
    actions = calculate_trades(current, targets, 1.3, 10.49, 0.1, set(), quotes, ROUTES)
    assert any(action.action == "BUY" and action.requires_fx for action in actions)
    conversions = calculate_currency_needs(
        actions, current.accounts, 1.3, quotes, targets, 10.49
    )
    assert len(conversions) == 1 and conversions[0].direction == "CAD_TO_USD"
    assert conversions[0].source_amount + conversions[0].fee <= 180000
    cash = net_account_cash(
        current.accounts[0], build_account_trade_impacts(actions)["broker"]
    )
    cash.cad -= conversions[0].source_amount + conversions[0].fee
    cash.usd += conversions[0].target_amount
    assert cash.cad >= -0.01 and cash.usd >= -0.01
    assert current.accounts[0].cash_cad == 0


@pytest.mark.parametrize("activity_failure", [False, True])
def test_scheduled_sync_reconciles_before_recording_history_or_tactical_evaluation(
    tmp_path, encrypted_store, activity_failure
):
    ledger, bank = baseline(tmp_path)
    ledger.path = encrypted_store[0] / "transfer_tracking.json"
    ledger.save()
    current = portfolio(bank, 320000)
    current.total_value_cad -= 500000
    current.holdings.pop(SAVEN)
    current.external_balances.clear()
    config = parse_tactical_config(
        {
            "baseline_fixed_pct": 20,
            "fixed_composition": {SAVEN: 100},
            "deploy_thresholds": [{"drawdown_pct": -8, "fixed_pct": 15}],
        }
    )

    class Client:
        def get_accounts(self):
            return [{"number": "broker"}]

        def get_activities(self, number, start, end):
            if activity_failure:
                raise RuntimeError("activity unavailable")
            return [activity(-180000)]

    raw = (
        [
            {
                "owner_name": "Example",
                "token_file": "test.json",
                "account_type_display_overrides": {},
            }
        ],
        {"EQ": 80},
        [],
        0,
        {},
        0.1,
        {},
        (),
        5000,
        config,
        CONFIGS,
    )
    with (
        patch.object(main, "load_config", return_value=raw),
        patch.object(main, "prepare_state"),
        patch.object(main, "load_external_balances", return_value={SAVEN: bank}),
        patch.object(main, "get_tokens_dir", return_value=tmp_path),
        patch.object(main, "QuestradeClient", return_value=Client()),
        patch.object(main, "get_usd_to_cad_rate", return_value=1.3),
        patch.object(main, "build_portfolio", return_value=current),
        patch.object(main, "record_value") as record,
        patch.object(main, "get_all_time_high") as ath,
        patch.object(
            main, "evaluate_tactical_posture", wraps=evaluate_tactical_posture
        ) as evaluate,
        patch("src.tactical.load_tactical_state", return_value=TacticalState()),
        patch("src.tactical.save_tactical_state") as save,
    ):
        ath.return_value.value = 1000000
        ath.return_value.date = "2026-11-01"
        if activity_failure:
            with pytest.raises(RuntimeError, match="activity unavailable"):
                main.run_scheduled_sync()
        else:
            main.run_scheduled_sync()
    if activity_failure:
        record.assert_not_called()
        evaluate.assert_not_called()
    else:
        record.assert_called_once_with(1000000, as_of=main.household_date())
        assert evaluate.call_args.kwargs["current_value"] == 1000000
        save.assert_not_called()


@pytest.mark.parametrize("action", ["DEPOSIT", "WITHDRAW"])
def test_statement_preceding_intent_advances_balance_without_consuming_cap(
    tmp_path, action
):
    snapshot, parsed = export(tmp_path)
    ledger = TransferLedger()
    ledger.observe_external_balance(SAVEN, snapshot, parsed)
    ledger.observe_broker(SAVEN, "broker", [], NOW)
    ledger.remember_recommendations(
        [recommendation(action, 100)], ROUTES, NOW.replace(day=6)
    )
    sign = -1 if action == "WITHDRAW" else 1
    rows = [
        ("07/11/2026", action, 20, 500000 + sign * 120),
        ("02/11/2026", action, 100, 500000 + sign * 100),
        ("01/11/2026", "Deposit", 500000, 500000),
    ]
    snapshot, parsed = export(tmp_path, rows)
    for _ in range(2):
        ledger.observe_external_balance(SAVEN, snapshot, parsed)
        progress = ledger.progress(SAVEN, ACCOUNT)
        assert (progress.source_cad if sign == -1 else progress.received_cad) == 20
        assert ledger.account(SAVEN)["bank_out" if sign == -1 else "bank_in"] == cents(
            120
        )


@pytest.mark.parametrize("receipt", [60000, 180000])
def test_receipt_awaiting_export_reserves_transfer_cap(tmp_path, receipt):
    ledger, snapshot = baseline(tmp_path, "WITHDRAW")
    ledger.observe_broker(SAVEN, "broker", [activity(receipt)])
    current = portfolio(snapshot, 500000 + receipt, receipt)
    ledger.apply(current, ROUTES)
    actions = calculate_trades(
        current, {"EQ": 100, SAVEN: 0}, 1.35, 0, 0.1, set(), None, ROUTES
    )
    assert (
        sum(t.amount_cad for t in actions if isinstance(t, TransferRecommendation))
        == 180000 - receipt
    )


@pytest.mark.parametrize(
    "sent,received", [(0, 4000), (6000, 4000), (4000, 6000), (6000, 6000)]
)
def test_partial_savings_receipt_limits_recommendation_to_uncommitted_amount(
    tmp_path, sent, received
):
    ledger, _ = baseline(tmp_path)
    ledger.remember_recommendations([], ROUTES, NOW)
    ledger.remember_recommendations([recommendation(amount=10000)], ROUTES, NOW)
    bank, parsed = bank_deposit(tmp_path, received)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    ledger.observe_broker(SAVEN, ACCOUNT.number, [activity(-sent)] if sent else [])
    p = portfolio(bank, 500000 - sent, 20000 - sent)
    ledger.apply(p, ROUTES)
    trades = calculate_trades(
        p, {"EQ": 20, SAVEN: 80}, 1.35, 0, 0.1, set(), None, {SAVEN: p.accounts[0]}
    )
    transfers = [t for t in trades if isinstance(t, TransferRecommendation)]
    assert sum(t.amount_cad for t in transfers if t.in_progress) == max(
        0, sent - received
    )
    assert sum(t.amount_cad for t in transfers if not t.in_progress) == 10000 - max(
        sent, received
    )
    ledger.remember_recommendations(transfers, ROUTES, NOW)
    assert ledger.account(SAVEN)["cycle"]["limit"] == cents(10000)


def test_broker_cash_receipt_clears_transit_before_activity_without_replaying(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    ledger, _ = baseline(tmp_path, "WITHDRAW")
    bank, parsed = bank_withdrawal(tmp_path, 58182.32)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    cash = replace(ACCOUNT, cash_cad=58326.883, start_of_day_cash_cad=144.563)
    routes = {SAVEN: cash}
    ledger.observe_broker_cash(SAVEN, cash, {}, NOW.date())

    # The balance evidence survives a report and the next day's opening balance.
    ledger.remember_recommendations([], routes)
    ledger = TransferLedger(ledger.data)
    output = StringIO()
    monkeypatch.setattr(
        display, "console", Console(file=output, width=160, force_terminal=False)
    )
    for rows in ([], [activity(20000)], [activity(20000), activity(38182.32, day=3)]):
        ledger.observe_broker(SAVEN, cash.number, rows)
        p = portfolio(bank, broker_value=558182.32, cash=cash.cash_cad)
        ledger.apply(p, routes)
        progress = p.transfer_progress[0]
        assert progress.received_cad == 58182.32
        assert progress.in_transit_cad == 0
        assert p.total_value_cad == pytest.approx(1000000)
        output.seek(0)
        output.truncate()
        display.display_external_transfers(p, [])
        if len(rows) < 2:
            assert "Saven Transfers" in output.getvalue()
            expected = 58182.32 - sum(row["netAmount"] for row in rows)
            assert (
                f"${expected:,.2f}" in output.getvalue()
                and "Received" in output.getvalue()
            )
        else:
            assert not output.getvalue()
    assert not progress.unposted_broker_cad


def test_partial_cash_receipt_after_other_posted_activity_reduces_transit(tmp_path):
    from dataclasses import replace

    ledger, _ = baseline(tmp_path, "WITHDRAW")
    bank, parsed = bank_withdrawal(tmp_path, 58182.32)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    cash = replace(ACCOUNT, cash_cad=58182.32, start_of_day_cash_cad=0)
    ledger.observe_broker_cash(
        SAVEN, cash, {NOW.date(): Decimal("58182.32")}, NOW.date()
    )
    assert ledger.progress(SAVEN, cash).in_transit_cad == 58182.32
    ledger.observe_broker_cash(SAVEN, cash, {NOW.date(): 20000}, NOW.date())
    assert ledger.progress(SAVEN, cash).in_transit_cad == 20000
    assert ledger.progress(SAVEN, cash).unposted_broker_cad == 38182.32


def test_ordinary_broker_withdrawal_during_inbound_intent_stays_outside(tmp_path):
    from dataclasses import replace

    ledger, bank = baseline(tmp_path, "WITHDRAW")
    account = replace(ACCOUNT, start_of_day_cash_cad=125.437, cash_cad=75.437)
    for rows in ([], [activity(-50)], [activity(-50)]):
        ledger.observe_broker(SAVEN, account.number, rows)
        ledger.observe_broker_cash(
            SAVEN, account, {NOW.date(): -50 if rows else 0}, NOW.date()
        )
        current = portfolio(bank, broker_value=499950, cash=account.cash_cad)
        ledger.apply(current, {SAVEN: account})
        assert current.total_value_cad == 999950
        assert current.transfer_progress[0].in_transit_cad == 0
        assert current.transfer_progress[0].received_cad == 0


def test_cad_sale_proceeds_use_fees_then_posted_net_without_counting_twice():
    stamp = NOW.isoformat()
    fill = dict(
        side="Sell",
        symbol="EQ",
        symbolId=11,
        timestamp=stamp,
        totalCost=500,
        quantity=10,
        commission=1,
        canadianExecutionFee=0.03,
    )
    trade = dict(
        activity(498.90, day=1),
        type="Trades",
        symbol="EQ",
        symbolId=11,
        action="Sell",
        quantity=-10,
        tradeDate=stamp,
    )
    for rows, expected in (
        ([], "498.97"),
        ([trade], "498.90"),
        ([dict(trade, symbol="")], "498.90"),
    ):
        assert cash_by_day(rows, [fill], {11: "CAD"})[NOW.date()] == Decimal(expected)


def test_provisional_debit_funds_only_transit_and_displays_unposted_part(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    ledger, bank = baseline(tmp_path)
    account = replace(ACCOUNT, cash_cad=0, start_of_day_cash_cad=10000)
    ledger.observe_broker(SAVEN, account.number, [activity(-4000)])
    ledger.observe_broker_cash(SAVEN, account, {NOW.date(): -4000}, NOW.date())
    p = portfolio(bank, broker_value=490000, cash=0)
    ledger.apply(p, {SAVEN: account})
    assert p.total_value_cad == 1000000
    transfers = calculate_trades(
        p, {"EQ": 49, SAVEN: 51}, 1.35, 0, 0.1, set(), None, ROUTES
    )
    assert (
        len(transfers) == 1
        and transfers[0].in_progress
        and transfers[0].amount_cad == 10000
    )
    assert simulate_rebalance(p, transfers, {}, 1.35).allocations["CAD"] == 0
    output = StringIO()
    monkeypatch.setattr(display, "console", Console(file=output, width=160))
    display.display_external_transfers(p, transfers)
    assert any(
        "$4,000.00" in line and "In transit" in line
        for line in output.getvalue().splitlines()
    )
    assert any(
        "$6,000.00" in line and "Sent (awaiting activity)" in line
        for line in output.getvalue().splitlines()
    )


@pytest.mark.parametrize("action,sign", [("DEPOSIT", -1), ("WITHDRAW", 1)])
def test_partial_cash_evidence_survives_midnight_and_split_postings(
    tmp_path, action, sign
):
    from dataclasses import replace

    ledger, bank = baseline(tmp_path, action)
    if action == "WITHDRAW":
        bank, parsed = bank_withdrawal(tmp_path, 10000)
        ledger.observe_external_balance(SAVEN, bank, parsed)
    day = NOW.date()
    next_day = NOW.replace(day=2).date()
    account = replace(
        ACCOUNT, start_of_day_cash_cad=20000, cash_cad=20000 + sign * 4000
    )
    ledger.observe_broker_cash(SAVEN, account, {}, day)
    assert ledger.progress(SAVEN, account).unposted_broker_cad == 4000
    assert ledger.progress(SAVEN, account).in_transit_cad == (
        4000 if sign == -1 else 6000
    )
    ledger.remember_recommendations([], {SAVEN: account})
    ledger = TransferLedger(ledger.data)
    account = replace(
        account, start_of_day_cash_cad=account.cash_cad, cash_cad=20000 + sign * 10000
    )
    posted = [activity(sign * 2000)]
    for _ in range(2):
        ledger.observe_broker(SAVEN, account.number, posted)
        ledger.observe_broker_cash(SAVEN, account, {next_day: sign * 2000}, next_day)
        progress = ledger.progress(SAVEN, account)
        assert progress.unposted_broker_cad == 8000
        assert progress.in_transit_cad == (10000 if sign == -1 else 0)
    if sign == -1:
        bank, parsed = bank_deposit(tmp_path, 10000)
        ledger.observe_external_balance(SAVEN, bank, parsed)
        assert ledger.progress(SAVEN, account).in_transit_cad == 0
        with pytest.raises(ValueError, match="until its transfer settles"):
            ledger.retain_routes({})
    posted.append(activity(sign * 8000))
    ledger.observe_broker(SAVEN, account.number, posted)
    ledger.observe_broker_cash(SAVEN, account, {next_day: sign * 10000}, next_day)
    progress = ledger.progress(SAVEN, account)
    assert progress.received_cad == progress.source_cad == 10000
    assert progress.in_transit_cad == progress.unposted_broker_cad == 0


@pytest.mark.parametrize(
    "action,change,source,expected",
    [
        ("DEPOSIT", -200000, 0, 180000),
        ("WITHDRAW", 12000, 10000, 10000),
        ("WITHDRAW", 7000, 6000, 6000),
        ("WITHDRAW", 12000, 0, 12000),
        ("WITHDRAW", 200000, 0, 180000),
        ("DEPOSIT", 500, 0, 0),
    ],
)
def test_cash_inference_caps_principal_and_keeps_outside_money(
    tmp_path, action, change, source, expected
):
    from dataclasses import replace

    ledger, bank = baseline(tmp_path, action)
    if source:
        bank, parsed = bank_withdrawal(tmp_path, source)
        ledger.observe_external_balance(SAVEN, bank, parsed)
    account = replace(ACCOUNT, start_of_day_cash_cad=250000, cash_cad=250000 + change)
    ledger.observe_broker_cash(SAVEN, account, {}, NOW.date())
    p = portfolio(bank, 500000 + change, account.cash_cad)
    ledger.apply(p, {SAVEN: account})
    progress = p.transfer_progress[0]
    assert progress.unposted_broker_cad == expected
    assert progress.source_offset_cad == (
        expected if action == "WITHDRAW" and source == 0 else 0
    )
    assert (
        p.total_value_cad
        == 1000000
        + change
        - source
        + progress.in_transit_cad
        - progress.source_offset_cad
    )
    assert p.cash_cad_total == account.cash_cad


@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize("action", ["DEPOSIT", "WITHDRAW"])
def test_new_cycle_excludes_existing_same_day_cash_movement(tmp_path, sign, action):
    from dataclasses import replace

    ledger, bank = baseline(tmp_path)
    ledger.remember_recommendations([], ROUTES)
    account = replace(ACCOUNT, start_of_day_cash_cad=500, cash_cad=500 + sign * 50)
    day = NOW.date()
    ledger.observe_broker_cash(SAVEN, account, {}, day)
    ledger.remember_recommendations([recommendation(action)], {SAVEN: account}, NOW)
    for amounts in ([], [20], [20, 30], [20, 30]):
        ledger = TransferLedger(ledger.data)
        rows = [activity(sign * n, day=1) for n in amounts]
        ledger.observe_broker(SAVEN, account.number, rows, NOW)
        daily = {day: sign * sum(amounts)}
        ledger.observe_broker_cash(SAVEN, account, daily, day)
        p = portfolio(bank, broker_value=500000 + sign * 50, cash=account.cash_cad)
        ledger.apply(p, {SAVEN: account})
        progress = p.transfer_progress[0]
        assert progress.source_cad == progress.received_cad == 0
        assert p.total_value_cad == 1000000 + sign * 50
    change = -100 if action == "DEPOSIT" else 100
    account.cash_cad += change
    ledger.observe_broker_cash(SAVEN, account, daily, day)
    assert ledger.progress(SAVEN, account).unposted_broker_cad == 100
    ledger.observe_broker(SAVEN, account.number, rows + [activity(change, day=1)], NOW)
    ledger.observe_broker_cash(SAVEN, account, {day: sign * 50 + change}, day)
    progress = ledger.progress(SAVEN, account)
    assert max(progress.source_cad, progress.received_cad) == 100
    assert progress.unposted_broker_cad == 0


@pytest.mark.parametrize("action,sign", [("DEPOSIT", -1), ("WITHDRAW", 1)])
def test_new_payment_posts_before_preintent_payment(tmp_path, action, sign):
    from dataclasses import replace

    ledger, _ = baseline(tmp_path)
    ledger.remember_recommendations([], ROUTES, NOW)
    # Earlier posted activity and fractional broker cash must not inflate the gap.
    known = activity(120, day=1)
    account = replace(
        ACCOUNT, start_of_day_cash_cad=500.103, cash_cad=620.103 + sign * 50
    )
    ledger.observe_broker(SAVEN, account.number, [known], NOW)
    ledger.observe_broker_cash(SAVEN, account, {NOW.date(): 120}, NOW.date())
    ledger.remember_recommendations([recommendation(action)], {SAVEN: account}, NOW)
    account.cash_cad += sign * 100
    fresh = activity(sign * 100, day=1)
    late = activity(sign * 50, day=1)
    for rows in ([known, fresh], [known, fresh, late], [late, known, fresh]):
        ledger = TransferLedger(ledger.data)
        ledger.observe_broker(SAVEN, account.number, rows, NOW)
        ledger.observe_broker_cash(
            SAVEN, account, {NOW.date(): sum(r["netAmount"] for r in rows)}, NOW.date()
        )
        progress = ledger.progress(SAVEN, account)
        assert max(progress.source_cad, progress.received_cad) == 100
    assert progress.unposted_broker_cad == 0


def test_distinct_ordinary_and_completed_cycle_debits_can_post_together(tmp_path):
    from dataclasses import replace

    ledger, _ = baseline(tmp_path)
    account = replace(ACCOUNT, start_of_day_cash_cad=200000, cash_cad=140000)
    ledger.observe_broker_cash(SAVEN, account, {}, NOW.date())
    bank, parsed = bank_deposit(tmp_path, 180000)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    ledger.apply(portfolio(bank, 440000), {SAVEN: account})

    # Yesterday's $60k is already in today's opening balance. The ordinary
    # $50 debit is also visible by the time a new recommendation is shown.
    today = NOW.replace(day=3)
    account = replace(account, start_of_day_cash_cad=140000, cash_cad=139950)
    ledger.observe_broker_cash(SAVEN, account, {}, today.date())
    ledger.remember_recommendations([recommendation()], {SAVEN: account}, today)
    old = [activity(-60000, day=3), activity(-50, day=3)]
    for extra in (0, 100):
        ledger = TransferLedger(ledger.data)
        account.cash_cad = 139950 - extra
        rows = old + ([activity(-extra, day=3)] if extra else [])
        ledger.observe_broker(SAVEN, account.number, rows, today)
        ledger.observe_broker_cash(
            SAVEN, account, {today.date(): -60050 - extra}, today.date()
        )
        progress = ledger.progress(SAVEN, account)
        assert progress.in_transit_cad == extra
        assert progress.unposted_broker_cad == 0


@pytest.mark.parametrize(
    ("sign", "kind", "action"),
    [(-1, "Fees and rebates", "WITHDRAW"), (1, "Interest", "DEPOSIT")],
)
def test_late_nontransfer_explains_cash_seen_before_recommendation(
    tmp_path, sign, kind, action
):
    from dataclasses import replace

    ledger, bank = baseline(tmp_path)
    ledger.remember_recommendations([], ROUTES, NOW)
    account = replace(ACCOUNT, start_of_day_cash_cad=500, cash_cad=500 + sign * 50)
    day = NOW.date()
    ledger.observe_broker_cash(SAVEN, account, {}, day)
    ledger.remember_recommendations(
        [recommendation(action, 10000)], {SAVEN: account}, NOW
    )
    # A partial posting and its replay both leave the real household total intact.
    for parts in ([20], [20], [20, 30]):
        ledger = TransferLedger(ledger.data)
        rows = [activity(sign * n, day=1, kind=kind) for n in parts]
        ledger.observe_broker(SAVEN, account.number, rows, NOW)
        daily = cash_by_day(rows, [], {})
        ledger.observe_broker_cash(SAVEN, account, daily, day)
        p = portfolio(bank, broker_value=500000 + sign * 50, cash=account.cash_cad)
        ledger.apply(p, {SAVEN: account})
        assert p.total_value_cad == 1000000 + sign * 50
        assert p.transfer_progress[0].unposted_broker_cad == 0
    account.cash_cad -= sign * 100
    ledger.observe_broker_cash(SAVEN, account, daily, day)
    assert ledger.progress(SAVEN, account).unposted_broker_cad == 100


def test_postintent_fee_with_a_real_cash_debit_leaves_prior_eft_evidence_intact(
    tmp_path,
):
    from dataclasses import replace

    ledger, _ = baseline(tmp_path)
    ledger.remember_recommendations([], ROUTES, NOW)
    day = NOW.date()
    account = replace(ACCOUNT, start_of_day_cash_cad=500, cash_cad=450)
    ledger.observe_broker_cash(SAVEN, account, {}, day)
    ledger.remember_recommendations([recommendation("WITHDRAW")], {SAVEN: account}, NOW)
    account.cash_cad -= 10
    fee = activity(-10, day=1, kind="Fees and rebates")
    for rows in ([fee], [fee, activity(-50, day=1)]):
        ledger.observe_broker(SAVEN, account.number, rows, NOW)
        daily = cash_by_day(rows, [], {})
        ledger.observe_broker_cash(SAVEN, account, daily, day)
        assert ledger.progress(SAVEN, account).received_cad == 0
    account.cash_cad += 100
    ledger.observe_broker_cash(SAVEN, account, daily, day)
    assert ledger.progress(SAVEN, account).received_cad == 100


@pytest.mark.parametrize("posted_qty", [0, 4, 10])
def test_invested_receipt_recognized_through_normal_reconciliation(
    tmp_path, encrypted_store, posted_qty
):
    from dataclasses import replace

    ledger, _ = baseline(tmp_path, "WITHDRAW")
    bank, parsed = bank_withdrawal(tmp_path, 1001)
    ledger.observe_external_balance(SAVEN, bank, parsed)
    ledger.path = encrypted_store[0] / "transfer_tracking.json"
    ledger.save()
    stamp = "2026-11-02T10:00:00-05:00"
    trade = dict(
        activity(1),
        type="Trades",
        action="Buy",
        symbol="EQ",
        symbolId=99,
        quantity=posted_qty,
        netAmount=-posted_qty * 100 - (1 if posted_qty == 10 else 0),
    )
    fill = {
        "side": "Buy",
        "symbol": "EQ",
        "symbolId": 99,
        "timestamp": stamp,
        "totalCost": 400,
        "quantity": 4,
        "commission": 0.4,
        "id": 1,
    }

    class Client:
        def get_accounts(self):
            return [{"number": "broker"}]

        def get_activities(self, *args):
            # Yesterday's buy settled today and was already in the opening balance.
            return [
                dict(
                    trade,
                    tradeDate="2026-11-01T00:00:00-04:00",
                    quantity=5,
                    netAmount=-500,
                )
            ] + ([trade] if posted_qty else [])

        def get_executions(self, *args):
            return [
                fill,
                fill,
                dict(fill, quantity=6, totalCost=600, commission=0.6, id=2),
                dict(fill, symbol="USD_ETF", symbolId=77, side="Sell", id=3),
            ]

        def get_symbols(self, ids):
            return [
                {"symbolId": i, "currency": "USD" if i == 77 else "CAD"} for i in ids
            ]

    for _ in range(2):
        p = portfolio(bank, broker_value=501001, cash=42.10)
        account = replace(
            p.accounts[0],
            start_of_day_cash_cad=42.10,
            cash_observed_at=NOW.replace(day=2, hour=18),
        )
        ledger = reconcile_transfer_activity(
            p, {SAVEN: bank}, [Client()], {SAVEN: account}
        )
        assert p.total_value_cad == 1000000
        assert (
            p.transfer_progress[0].received_cad
            == p.transfer_progress[0].unposted_broker_cad
            == 1001
        )
        assert p.transfer_progress[0].in_transit_cad == 0
