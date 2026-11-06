"""Whole-run transfer scenarios: source limits, arrival order and replay safety."""

from copy import deepcopy
from datetime import UTC, datetime
from io import StringIO
from unittest.mock import patch

import pytest
from rich.console import Console

import main
from src import display, saven_tracking, transfer_tracking
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
from src.saven_tracking import (
    attach_saven_balances,
    load_saven_balances,
    save_saven_balances,
)
from src.tactical import TacticalState, evaluate_tactical_posture, parse_tactical_config
from src.transfer_tracking import TransferLedger, cents, reconcile_transfer_activity

SAVEN = "SAVEN:0001112223"
NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
ACCOUNT = AccountInfo("broker", "Margin", "Individual", "Example")
ROUTES = {SAVEN: ACCOUNT}


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
    ledger.observe_saven(SAVEN, snapshot, parsed)
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
    attach_saven_balances(result, {"0001112223": snapshot})
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
        ledger.observe_saven(SAVEN, bank, parsed)
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
        ledger.observe_saven(SAVEN, bank, parsed)
        broker_value, cash = 335000, 0
    else:
        bank, parsed = bank_withdrawal(tmp_path, 165000)
        ledger.observe_saven(SAVEN, bank, parsed)
        ledger.observe_broker(SAVEN, "broker", [activity(180000)])
        broker_value, cash = 680000, 180000
    current = portfolio(bank, broker_value, cash)
    ledger.apply(current, ROUTES)
    assert current.total_value_cad == 1015000
    assert current.transfer_progress[0].in_transit_cad == 0
    assert current.transfer_progress[0].awaiting_csv_cad == 0
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
        ledger.observe_saven(SAVEN, bank, parsed)
        broker_value, cash = 300000, 0
    else:
        bank, parsed = bank_withdrawal(tmp_path, 200000)
        ledger.observe_saven(SAVEN, bank, parsed)
        ledger.observe_broker(SAVEN, "broker", [activity(receipt)])
        broker_value, cash = 500000 + receipt, receipt
    current = portfolio(bank, broker_value, cash)
    ledger.apply(current, ROUTES)
    assert current.total_value_cad == total
    assert current.transfer_progress[0].in_transit_cad == 0


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
    assert current.transfer_progress[0].awaiting_csv_cad == 180000
    bank, parsed = bank_withdrawal(tmp_path, 165000)
    ledger.observe_saven(SAVEN, bank, parsed)
    settled = portfolio(bank, 680000, 180000)
    ledger.apply(settled, ROUTES)
    assert settled.total_value_cad == 1015000
    assert settled.transfer_progress[0].awaiting_csv_cad == 0


def test_destination_first_deposit_and_later_broker_record_do_not_double_count(
    tmp_path,
):
    ledger, bank = baseline(tmp_path)
    bank, parsed = bank_deposit(tmp_path, 180000)
    ledger.observe_saven(SAVEN, bank, parsed)
    current = portfolio(bank, 320000)
    ledger.apply(current, ROUTES)
    assert current.total_value_cad == 1000000
    ledger.observe_broker(SAVEN, "broker", [activity(-180000)])
    repeated = portfolio(bank, 320000)
    ledger.apply(repeated, ROUTES)
    assert repeated.total_value_cad == 1000000
    assert repeated.transfer_progress[0].in_transit_cad == 0


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
    ledger.observe_saven(SAVEN, bank, parsed)
    ledger.observe_saven(SAVEN, bank, parsed)
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
    ledger.observe_saven(SAVEN, bank, parsed)
    assert ledger.account(SAVEN)["bank_in"] == cents(100000)


def test_noncontinuous_csv_fails_without_partially_advancing_state(tmp_path):
    ledger, bank = baseline(tmp_path)
    before = deepcopy(ledger.data)
    bank, parsed = export(tmp_path, [("02/11/2026", "Deposit", 100000, 700000)])
    with pytest.raises(ValueError, match="continuous activity"):
        ledger.observe_saven(SAVEN, bank, parsed)
    assert ledger.data == before


@pytest.mark.parametrize("include_anchor", [False, True])
def test_cached_only_startup_can_reconcile_a_later_statement(tmp_path, include_anchor):
    bank, _ = export(tmp_path)
    ledger = TransferLedger()
    ledger.observe_saven(SAVEN, bank)
    ledger.observe_broker(SAVEN, "broker", [], NOW)
    ledger.remember_recommendations([recommendation("WITHDRAW")], ROUTES, NOW)
    rows = [("02/11/2026", "Withdrawal", 180000, 320000)]
    if include_anchor:
        rows.append(("01/11/2026", "Deposit", 500000, 500000))
    bank, parsed = export(tmp_path, rows)
    ledger.observe_saven(SAVEN, bank, parsed)
    assert ledger.progress(SAVEN, ACCOUNT).in_transit_cad == 180000


def test_expanding_a_csv_date_range_does_not_replay_older_bank_history(tmp_path):
    bank, _ = export(tmp_path)
    ledger = TransferLedger()
    ledger.observe_saven(SAVEN, bank)
    ledger.observe_broker(SAVEN, "broker", [], NOW)
    ledger.remember_recommendations([recommendation("WITHDRAW")], ROUTES, NOW)
    rows = [("01/11/2026", "Withdrawal", 180000, 320000)]
    bank, parsed = export(tmp_path, rows)
    ledger.observe_saven(SAVEN, bank, parsed)
    rows.append(("01/11/2026", "Deposit", 500000, 500000))
    bank, parsed = export(tmp_path, rows)
    ledger.observe_saven(SAVEN, bank, parsed)
    assert ledger.progress(SAVEN, ACCOUNT).source_cad == 180000
    assert ledger.account(SAVEN)["bank_in"] == 0


def test_partly_settled_cycle_keeps_its_remaining_limit(tmp_path):
    ledger, bank = baseline(tmp_path)
    ledger.observe_broker(SAVEN, "broker", [activity(-50000)])
    bank, parsed = bank_deposit(tmp_path, 50000)
    ledger.observe_saven(SAVEN, bank, parsed)
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
    ledger.observe_saven(SAVEN, bank, parsed)
    ledger.remember_recommendations([], ROUTES)
    ledger.observe_broker(SAVEN, "broker", [activity(-165000), activity(-15000, day=3)])
    assert ledger.progress(SAVEN, ACCOUNT).in_transit_cad == 15000


def test_settled_cycle_starts_new_intent_without_reusing_past_activity(tmp_path):
    ledger, bank = baseline(tmp_path)
    rows = [activity(-180000)]
    ledger.observe_broker(SAVEN, "broker", rows)
    bank, parsed = bank_deposit(tmp_path, 180000)
    ledger.observe_saven(SAVEN, bank, parsed)
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
        ledger.observe_saven(SAVEN, bank, parsed)
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
        ledger.observe_saven(SAVEN, bank, parsed)
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
    display.display_allocations(snapshot.allocations, {"EQ": 68, SAVEN: 32}, 0.1)
    display.display_external_transfers(actions, current.transfer_progress)
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
    assert ("Awaiting CSV" if recipient_first else "In transit") in rendered


@pytest.fixture
def encrypted_store(tmp_path, monkeypatch):
    """Keep test plaintext in memory while paths contain only ciphertext markers."""
    data = tmp_path / "data"
    data.mkdir()
    storage = {}

    def read(path):
        return deepcopy(storage[path])

    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("encrypted-test-placeholder", encoding="utf-8")
        storage[path] = deepcopy(value)

    for module in (transfer_tracking, saven_tracking):
        monkeypatch.setattr(module, "read_encrypted_json", read)
        monkeypatch.setattr(module, "write_encrypted_json", write)
        monkeypatch.setattr(module, "get_data_dir", lambda: data)
    return data, storage


def test_csv_refresh_then_scheduled_reconciliation_preserves_transfer_evidence(
    tmp_path, encrypted_store
):
    data, storage = encrypted_store
    bank, parsed = export(tmp_path)
    assert load_saven_balances(["0001112223"], tmp_path)["0001112223"] == bank
    ledger = TransferLedger.load()
    ledger.observe_broker(SAVEN, "broker", [], NOW)
    ledger.remember_recommendations([recommendation("WITHDRAW")], ROUTES, NOW)
    ledger.save()
    new_bank, parsed = bank_withdrawal(tmp_path, 165000)
    balances = load_saven_balances(["0001112223"], tmp_path)
    assert balances["0001112223"] == new_bank
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
    load_saven_balances(["0001112223"], tmp_path)
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
    balances = load_saven_balances(["0001112223"], tmp_path)
    ledger = TransferLedger.load()
    ledger.observe_broker(SAVEN, "broker", [], NOW)
    ledger.remember_recommendations([recommendation()], ROUTES, NOW)
    ledger.observe_broker(SAVEN, "broker", [activity(-50000)])
    ledger.save()
    saved = deepcopy(storage[data / "saven_balance.json"])
    with pytest.raises(ValueError, match="until its transfer settles"):
        save_saven_balances(
            data / "saven_balance.json", {"accounts": {}}, {"accounts": balances}, {}
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
    reconcile_transfer_activity(current, {"0001112223": bank}, [Client()], ROUTES)
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
    ledger.observe_saven(SAVEN, bank, parsed)
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
    ledger.observe_saven(SAVEN, bank, parsed)
    current = portfolio(bank, 680000, 180000)
    ledger.apply(current, ROUTES)
    assert current.total_value_cad == pytest.approx(1000019.91)
    assert current.transfer_progress[0].awaiting_csv_cad == 180000


@pytest.mark.parametrize("action", ["DEPOSIT", "WITHDRAW"])
def test_partial_destination_arrivals_release_only_received_principal(tmp_path, action):
    ledger, bank = baseline(tmp_path, action)
    if action == "DEPOSIT":
        ledger.observe_broker(SAVEN, "broker", [activity(-180000)])
    else:
        bank, parsed = bank_withdrawal(tmp_path, 180000)
        ledger.observe_saven(SAVEN, bank, parsed)
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
            ledger.observe_saven(SAVEN, bank, parsed)
            current = portfolio(bank, 320000)
        else:
            received.append(activity(amount, day))
            ledger.observe_broker(SAVEN, "broker", received)
            current = portfolio(bank, 500000 + total_received, total_received)
        ledger.apply(current, ROUTES)
        assert current.total_value_cad == 1000000
        assert current.transfer_progress[0].in_transit_cad == 180000 - total_received


def test_same_day_duplicate_bank_rows_keep_multiplicity_across_full_exports(tmp_path):
    ledger, bank = baseline(tmp_path, "WITHDRAW")
    rows = [
        ("01/11/2026", "Deposit", 100000, 500000),
        ("01/11/2026", "Withdrawal", 100000, 400000),
        ("01/11/2026", "Deposit", 500000, 500000),
    ]
    bank, parsed = export(tmp_path, rows)
    ledger.observe_saven(SAVEN, bank, parsed)
    rows = rows[:2] + rows
    bank, parsed = export(tmp_path, rows)
    ledger.observe_saven(SAVEN, bank, parsed)
    ledger.observe_saven(SAVEN, bank, parsed)
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
    ledger.observe_saven(SAVEN, bank, parsed)
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
        config,
        ["0001112223"],
        {SAVEN: {"owner_name": "Example", "account_type": "Margin"}},
    )
    with (
        patch.object(main, "load_config", return_value=raw),
        patch.object(main, "validate_encrypted_state_files"),
        patch.object(main, "load_saven_balances", return_value={"0001112223": bank}),
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
        main.run_scheduled_sync()
    if activity_failure:
        record.assert_not_called()
        evaluate.assert_not_called()
    else:
        record.assert_called_once_with(1000000)
        assert evaluate.call_args.kwargs["current_value"] == 1000000
        save.assert_not_called()
