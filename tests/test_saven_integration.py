"""Dated external balances and planned transfers share household planning math."""

from io import StringIO
from unittest.mock import patch

import pytest
from rich.console import Console

import main
from src import display, external_accounts
from src.external_accounts import (
    ExternalAccountConfig,
    attach_external_balances,
    load_external_balances,
    parse_external_accounts,
    refresh_external_balances,
    resolve_external_transfer_accounts,
)
from src.external_providers import PROVIDERS
from src.fx_conversions import calculate_currency_needs
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
from tests.test_saven_tracking import ACCOUNT_A, cached, export

SAVEN = f"SAVEN:{ACCOUNT_A}"
CONFIGS = {SAVEN: ExternalAccountConfig(SAVEN, "Saven", "HISA", "Primary", "Margin")}
IDEAL = {"EQUITY": 80.0, "RESERVE": 10.0, "CORE": 5.0, SAVEN: 5.0}


def household(tmp_path, savings="5000.00", cash=0):
    """A $100,000 household with brokerage holdings and separately held savings."""
    path = export(tmp_path / "savings.csv", amount=savings)
    snapshot = cached(path)
    values = {
        "EQUITY": 80000.0,
        "CORE": 5000.0,
        "RESERVE": 15000.0 - float(savings) - cash,
    }
    positions = [
        Position(
            symbol, index, value / 10, value, 10, "CAD", "broker", "Margin", "Primary"
        )
        for index, (symbol, value) in enumerate(values.items(), 1)
    ]
    portfolio = PortfolioSummary(
        accounts=[
            AccountInfo(
                "broker", "Margin", "Individual", "Primary", positions, cash_cad=cash
            )
        ],
        holdings={
            symbol: HoldingSummary(
                value_cad=value,
                total_quantity=value / 10,
                current_price=10,
                bid_price=10,
                ask_price=10,
            )
            for symbol, value in values.items()
        },
        total_value_cad=sum(values.values()) + cash,
        cash_cad_total=cash,
    )
    attach_external_balances(
        portfolio, {SAVEN: snapshot["accounts"][ACCOUNT_A]}, CONFIGS
    )
    return portfolio, snapshot


def test_savings_count_once_in_valuation_allocation_and_projection_not_broker_cash(
    tmp_path,
):
    portfolio, _ = household(tmp_path, cash=1000)
    assert portfolio.total_value_cad == 100000
    assert portfolio.cash_cad_total == 1000
    assert portfolio.accounts[0].cash_cad == 1000
    assert len(portfolio.accounts) == 1
    assert build_allocation_snapshot(portfolio, IDEAL, 1.3).allocations[SAVEN] == 5
    trades = calculate_trades(
        portfolio, IDEAL, 1.3, 0, 0.1, set(), None, {SAVEN: portfolio.accounts[0]}
    )
    assert trades
    assert all(
        trade.account_number == "broker" and trade.symbol != SAVEN for trade in trades
    )
    projected = simulate_rebalance(portfolio, trades, IDEAL, 1.3)
    assert projected.allocations[SAVEN] == 5
    assert projected.allocations["CAD"] >= 0
    assert (
        projected.accuracy > build_allocation_snapshot(portfolio, IDEAL, 1.3).accuracy
    )


def test_hisa_deposit_and_funding_sell_share_one_plan_without_changing_targets(
    tmp_path,
):
    portfolio, _ = household(tmp_path, savings="0.00", cash=1000)
    trades = calculate_trades(
        portfolio, IDEAL, 1.3, 0, 0.1, set(), None, {SAVEN: portfolio.accounts[0]}
    )
    transfers = [trade for trade in trades if trade.action == "DEPOSIT"]
    assert len(transfers) == 1 and transfers[0].amount_cad == 5000
    assert any(trade.action == "SELL" and trade.symbol == "RESERVE" for trade in trades)
    projected = simulate_rebalance(portfolio, trades, IDEAL, 1.3)
    assert projected.allocations[SAVEN] == 5
    assert projected.allocations["EQUITY"] == 80
    assert projected.accuracy == 100
    assert IDEAL[SAVEN] == 5
    assert portfolio.external_balances[SAVEN].balance_cad == 0


@pytest.mark.parametrize("fixed_pct", [20, 15, 10, 5, 0])
def test_tactical_stages_respect_actual_external_liquidity(tmp_path, fixed_pct):
    portfolio, _ = household(tmp_path)
    targets = {
        "EQUITY": 100 - fixed_pct,
        "CORE": fixed_pct * 0.25,
        "RESERVE": fixed_pct * 0.5,
        SAVEN: fixed_pct * 0.25,
    }
    trades = calculate_trades(
        portfolio, targets, 1.3, 0, 0.1, set(), None, {SAVEN: portfolio.accounts[0]}
    )
    assert all(
        trade.action in {"DEPOSIT", "WITHDRAW"}
        for trade in trades
        if trade.symbol == SAVEN
    )
    projected = simulate_rebalance(portfolio, trades, targets, 1.3)
    assert projected.allocations[SAVEN] == pytest.approx(targets[SAVEN])
    assert projected.allocations["EQUITY"] == pytest.approx(100 - fixed_pct, abs=0.1)
    assert portfolio.external_balances[SAVEN].balance_cad == 5000


def test_updated_export_and_broker_balance_complete_transfer_without_double_counting(
    tmp_path,
):
    portfolio, existing = household(tmp_path, savings="1000.00")
    updated = export(
        tmp_path / "new.csv", amount="5000.00", transaction_date="08/10/2026"
    )
    refreshed, _ = refresh_external_balances(
        PROVIDERS["SAVEN"], [ACCOUNT_A], existing, source_file=updated
    )
    broker_after_transfer = PortfolioSummary(accounts=[], total_value_cad=95000)
    attach_external_balances(
        broker_after_transfer, {SAVEN: refreshed["accounts"][ACCOUNT_A]}, CONFIGS
    )
    assert portfolio.total_value_cad == broker_after_transfer.total_value_cad == 100000
    assert broker_after_transfer.holdings[SAVEN].value_cad == 5000
    with pytest.raises(ValueError, match="already included"):
        attach_external_balances(
            broker_after_transfer, {SAVEN: refreshed["accounts"][ACCOUNT_A]}, CONFIGS
        )


def test_scheduled_read_uses_encrypted_cache_without_scanning_downloads(tmp_path):
    existing = cached(export(tmp_path / "source.csv"))
    destination = tmp_path / "saven_balance.json"
    destination.write_text("ciphertext")
    with (
        patch.object(external_accounts, "get_data_dir", return_value=tmp_path),
        patch.object(
            external_accounts, "read_encrypted_json", return_value=existing
        ) as read,
        patch("src.saven_export.find_latest_saven_export") as scan,
        patch.object(external_accounts, "write_encrypted_json") as write,
    ):
        assert load_external_balances(CONFIGS) == {
            SAVEN: existing["accounts"][ACCOUNT_A]
        }
    read.assert_called_once_with(destination)
    scan.assert_not_called()
    write.assert_not_called()


def test_local_refresh_saves_encrypted_snapshot_and_keeps_original_dates(tmp_path):
    path = export(tmp_path / "download.csv")
    events = []
    with (
        patch.object(external_accounts, "get_data_dir", return_value=tmp_path),
        patch.object(external_accounts, "write_encrypted_json") as write,
        patch("src.transfer_tracking.write_encrypted_json"),
    ):
        balances = load_external_balances(
            CONFIGS,
            directory=tmp_path,
            on_status=lambda *event: events.append(event),
        )
    assert events == [(SAVEN, "found", "2026-10-07")]
    assert balances[SAVEN]["last_transaction_date"] == "2026-10-07"
    assert (
        balances[SAVEN]["source_modified_at"]
        == read_saven_export(path).source_modified_at.isoformat()
    )
    write.assert_called_once_with(
        tmp_path / "saven_balance.json", {"accounts": {ACCOUNT_A: balances[SAVEN]}}
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("balance_cad", "NaN"),
        ("balance_cad", "-10"),
        ("balance_cad", "Infinity"),
        ("currency", "USD"),
        ("last_transaction_date", "bad"),
        ("source_modified_at", "2026-10-07T00:00:00"),
        ("source_sha256", "bad"),
    ],
)
def test_corrupt_cache_cannot_reach_portfolio_math(tmp_path, key, value):
    existing = cached(export(tmp_path / "source.csv"))
    existing["accounts"][ACCOUNT_A][key] = value
    with pytest.raises(ValueError, match="Invalid cached"):
        refresh_external_balances(PROVIDERS["SAVEN"], [ACCOUNT_A], existing)


def test_missing_savings_stops_sync_before_token_rotation_and_history(tmp_path):
    config = ([], {}, [], 0, {}, 0.1, None, CONFIGS)
    with (
        patch.object(main, "load_config", return_value=config),
        patch.object(main, "validate_encrypted_state_files"),
        patch.object(external_accounts, "get_data_dir", return_value=tmp_path),
        patch.object(main, "QuestradeClient") as connect,
        patch.object(main, "record_value") as record,
        patch.object(main, "evaluate_tactical_posture") as evaluate,
        pytest.raises(ValueError, match="No export or cached balance"),
    ):
        main.run_scheduled_sync()
    connect.assert_not_called()
    record.assert_not_called()
    evaluate.assert_not_called()


def test_scheduled_history_and_accuracy_include_savings(tmp_path, capsys):
    portfolio, snapshot = household(tmp_path)
    portfolio.total_value_cad -= 5000
    portfolio.holdings.pop(SAVEN)
    portfolio.external_balances.clear()
    config = (
        [
            {
                "owner_name": "Primary",
                "token_file": "primary.json",
                "account_type_display_overrides": {},
            }
        ],
        IDEAL,
        [],
        0,
        {},
        0.1,
        None,
        CONFIGS,
    )
    with (
        patch.object(main, "load_config", return_value=config),
        patch.object(main, "validate_encrypted_state_files"),
        patch.object(
            main,
            "load_external_balances",
            return_value={SAVEN: snapshot["accounts"][ACCOUNT_A]},
        ) as load,
        patch.object(main, "get_tokens_dir", return_value=tmp_path),
        patch.object(main, "QuestradeClient", return_value=object()),
        patch.object(main, "get_usd_to_cad_rate", return_value=1.3),
        patch.object(main, "build_portfolio", return_value=portfolio),
        patch.object(main, "reconcile_transfer_activity"),
        patch.object(main, "record_value") as record,
    ):
        main.run_scheduled_sync()
    load.assert_called_once_with(CONFIGS)
    record.assert_called_once_with(100000)
    assert "Accuracy: 100.0%" in capsys.readouterr().out


def test_loading_status_dates_balance_and_report_shows_manual_transfer(
    tmp_path, monkeypatch
):
    portfolio, _ = household(tmp_path, savings="1000.00")
    output = StringIO()
    console = Console(file=output, width=160, force_terminal=False)
    monkeypatch.setattr(display, "console", console)
    monkeypatch.setattr(main, "console", console)
    actions = calculate_trades(
        portfolio, IDEAL, 1.3, 0, 0.1, set(), None, {SAVEN: portfolio.accounts[0]}
    )
    main._display_external_status(CONFIGS[SAVEN], "found", "2026-10-07")
    display.display_holdings_summary(portfolio, 1.3)
    display.display_external_transfers(portfolio, actions)
    text = output.getvalue()
    assert "Primary Margin (broker) -> Saven HISA (…2223)" in text
    assert "$4,000.00" in text
    assert "last activity 2026-10-07" in text
    assert text.count("last activity") == 1
    assert text.index("Export found") < text.index("Portfolio Holdings")


def test_target_config_loads_external_fixed_membership_without_separate_account_list(
    tmp_path,
):
    tmp_path.joinpath("settings.yaml").write_text(
        "accounts:\n  - owner_name: Primary\n    token_file: primary.json\n"
        "targets:\n  EQUITY: 80\n"
        "tactical_deployment:\n  baseline_fixed_pct: 20\n  fixed_composition:\n"
        f'    "{SAVEN}": 25\n    RESERVE: 50\n    CORE: 25\n'
        f'external_accounts:\n  "{SAVEN}":\n    display_name: Saven\n    account_type: HISA\n    questrade_account:\n      owner_name: Primary\n      account_type: Margin\n'
    )
    with patch.object(main, "get_config_dir", return_value=tmp_path):
        config = main.load_config()
    assert config[-1] == CONFIGS
    assert config[-2].fixed_composition[SAVEN] == 0.25


def test_transfer_endpoint_resolves_owner_and_type_and_rejects_ambiguity(tmp_path):
    portfolio, _ = household(tmp_path)
    portfolio.accounts[0].owner = "Secondary"
    routes = {
        SAVEN: ExternalAccountConfig(SAVEN, "Saven", "HISA", "Secondary", "Margin")
    }
    assert (
        resolve_external_transfer_accounts(portfolio, routes)[SAVEN]
        is portfolio.accounts[0]
    )
    portfolio.accounts.append(AccountInfo("other", "Margin", "Individual", "Secondary"))
    with pytest.raises(ValueError, match="exactly one"):
        resolve_external_transfer_accounts(portfolio, routes)


def test_registered_account_cannot_fund_external_transfer():
    with pytest.raises(ValueError, match="personal Questrade Margin"):
        parse_external_accounts(
            {
                "targets": {SAVEN: 5},
                "external_accounts": {
                    SAVEN: {
                        "display_name": "Saven",
                        "account_type": "HISA",
                        "questrade_account": {
                            "owner_name": "Primary",
                            "account_type": "RRSP",
                        },
                    },
                },
            }
        )


def test_deposit_from_usd_funding_sales_includes_conversion_and_cad_transfer(tmp_path):
    portfolio, _ = household(tmp_path, savings="0.00")
    reserve = portfolio.accounts[0].positions[-1]
    reserve.currency = "USD"
    reserve.quantity /= 1.3
    reserve.market_value /= 1.3
    holding = portfolio.holdings["RESERVE"]
    holding.currency = "USD"
    holding.total_quantity /= 1.3
    quotes = DlrQuotes(13, 13, 10, 10, 1.3)
    actions = calculate_trades(
        portfolio, IDEAL, 1.3, 0, 0.1, set(), quotes, {SAVEN: portfolio.accounts[0]}
    )
    transfers = [action for action in actions if action.action == "DEPOSIT"]
    assert len(transfers) == 1
    assert (
        0 <= 5000 - transfers[0].amount_cad < 13
    )  # Whole DLR shares bound CAD proceeds.
    assert transfers[0].requires_fx
    conversions = calculate_currency_needs(
        actions, portfolio.accounts, 1.3, quotes, IDEAL, 0
    )
    assert any(
        conversion.direction == "USD_TO_CAD"
        and conversion.target_amount >= transfers[0].amount_cad
        for conversion in conversions
    )
    projected = simulate_rebalance(portfolio, actions, IDEAL, 1.3)
    assert projected.allocations[SAVEN] == pytest.approx(transfers[0].amount_cad / 1000)
    assert projected.allocations["CAD"] >= 0 and projected.allocations["USD"] >= 0


def test_withdrawal_funds_usd_buys_and_is_included_in_fx_cash_needs(tmp_path):
    portfolio, _ = household(tmp_path, savings="10000.00")
    # Move the reserve deficit to equity, which is held in USD.
    portfolio.holdings["RESERVE"].value_cad += 5000
    portfolio.holdings["RESERVE"].total_quantity += 500
    portfolio.accounts[0].positions[-1].market_value += 5000
    portfolio.accounts[0].positions[-1].quantity += 500
    equity = portfolio.accounts[0].positions[0]
    equity.currency = "USD"
    equity.market_value = 75000 / 1.3
    equity.quantity = equity.market_value / 10
    portfolio.holdings["EQUITY"].value_cad = 75000
    portfolio.holdings["EQUITY"].currency = "USD"
    portfolio.holdings["EQUITY"].total_quantity = equity.quantity
    quotes = DlrQuotes(13, 13, 10, 10, 1.3)
    actions = calculate_trades(
        portfolio, IDEAL, 1.3, 0, 0.1, set(), quotes, {SAVEN: portfolio.accounts[0]}
    )
    assert any(
        action.action == "WITHDRAW" and action.amount_cad == 5000 for action in actions
    )
    assert any(
        action.action == "BUY" and action.symbol == "EQUITY" for action in actions
    )
    conversions = calculate_currency_needs(
        actions, portfolio.accounts, 1.3, quotes, IDEAL, 0
    )
    assert len(conversions) == 1 and conversions[0].direction == "CAD_TO_USD"
    assert conversions[0].source_amount <= 5000
    projected = simulate_rebalance(portfolio, actions, IDEAL, 1.3)
    assert projected.allocations[SAVEN] == 5
    assert sum(projected.allocations.values()) == pytest.approx(100)


def test_unfundable_deposit_stays_unfilled_without_inventing_cash_or_target_changes(
    tmp_path,
):
    portfolio, _ = household(tmp_path, savings="0.00")
    endpoint = AccountInfo("empty", "Margin", "Individual", "Secondary")
    portfolio.accounts.append(endpoint)
    actions = calculate_trades(
        portfolio, IDEAL, 1.3, 0, 0.1, set(), None, {SAVEN: endpoint}
    )
    assert not any(action.symbol == SAVEN for action in actions)
    assert simulate_rebalance(portfolio, actions, IDEAL, 1.3).allocations[SAVEN] == 0
    assert IDEAL[SAVEN] == 5


def test_transfer_only_plan_renders_without_fake_share_quantity(tmp_path, monkeypatch):
    portfolio, _ = household(tmp_path, savings="10000.00")
    transfer = TransferRecommendation(
        SAVEN, "WITHDRAW", 5000, "broker", "Margin", "Primary"
    )
    output = StringIO()
    monkeypatch.setattr(
        display, "console", Console(file=output, width=160, force_terminal=False)
    )
    display.display_external_transfers(portfolio, [transfer])
    display.display_trades([transfer])
    assert "Saven HISA (…2223) -> Primary Margin (broker)" in output.getvalue()
    assert "$5,000.00" in output.getvalue()
    assert "No share trades recommended" in output.getvalue()
