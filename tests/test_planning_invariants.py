"""Executable examples for funding, valuation, and input validation at both entry points."""

from unittest.mock import Mock, patch

import pytest

import main
from src.fx_conversions import calculate_currency_needs
from src.fx_rate import DlrQuotes
from src.fx_targets import resolve_targets
from src.models import TradeRecommendation
from src.portfolio import build_portfolio, simulate_rebalance
from src.questrade_client import QuestradeClient
from src.rebalancer import calculate_trades
from src.report_builder import build_report_data
from tests.test_rebalancer import _build_test_portfolio


RATE = 1.35
QUOTES = DlrQuotes(13.5, 13.5, 10, 10, RATE)


def portfolio(cad, usd, price=30):
    return _build_test_portfolio(
        [
            dict(
                number="a",
                type="Margin",
                owner="Example",
                cash_cad=cad,
                cash_usd=usd,
                positions=[dict(symbol="EQ", qty=1, price=price, currency="USD")],
            )
        ],
        RATE,
    )


@pytest.mark.parametrize(
    "usd,cad,expected_orders,expected_journal,usd_left",
    [
        (20, 37.49, 1, 2, 10),
        (20, 37.48, 1, 1, 0),
        (0, 37.49, 0, 2, 20),
    ],
)
def test_single_fee_and_conversion_only_projection(
    usd, cad, expected_orders, expected_journal, usd_left
):
    p = portfolio(cad, usd)
    target = {"EQ": 100}
    orders = calculate_trades(p, target, RATE, 10.49, 0.1, set(), QUOTES)
    conversions = calculate_currency_needs(
        orders, p.accounts, RATE, QUOTES, target, 10.49
    )
    assert sum(t.quantity for t in orders) == expected_orders
    assert len(conversions) == 1 and conversions[0].dlr_shares == expected_journal
    with (
        patch("src.report_builder.get_all_time_high"),
        patch("src.report_builder.get_year_to_date_history"),
    ):
        report = build_report_data(p, target, RATE, orders, conversions, [], set())
    assert report.projected.allocations["USD"] == pytest.approx(
        usd_left * RATE / (p.total_value_cad - 10.49) * 100
    )


def test_existing_debit_cannot_fund_a_whole_share():
    p = portfolio(135, -50, price=100)
    assert calculate_trades(p, {"EQ": 100}, RATE, 0, 0.1, set(), QUOTES) == []


def test_projection_uses_native_cash_before_fx_and_preserves_other_account():
    p = portfolio(135, 100, price=150)
    from src.portfolio import AccountInfo

    p.accounts.append(AccountInfo("b", "Margin", "Individual", "Other", cash_usd=1000))
    p.cash_usd_total += 1000
    p.total_value_cad += 1000 * RATE
    order = TradeRecommendation(
        "EQ", "BUY", 1, "a", "Margin", "Example", 150, "USD", 150, requires_fx=True
    )
    projected = simulate_rebalance(p, [order], {"EQ": 100}, RATE, dlr_quotes=QUOTES)
    assert projected.allocations["CAD"] == pytest.approx(67.5 / p.total_value_cad * 100)
    assert projected.allocations["USD"] == pytest.approx(
        1000 * RATE / p.total_value_cad * 100
    )


@pytest.mark.parametrize("units", [1, 2])
def test_sale_changes_units_at_mark_and_cash_at_bid(units):
    p = _build_test_portfolio(
        [
            dict(
                number="a",
                type="Margin",
                owner="A",
                positions=[dict(symbol="X", qty=2, price=100, currency="CAD")],
            )
        ]
    )
    sale = TradeRecommendation(
        "X", "SELL", units, "a", "Margin", "A", 99, "CAD", units * 99
    )
    result = simulate_rebalance(p, [sale], {}, 1)
    assert result.allocations["X"] == pytest.approx(
        (2 - units) * 100 / (200 - units) * 100
    )
    assert result.allocations["CAD"] == pytest.approx(units * 99 / (200 - units) * 100)


def test_duplicate_accounts_and_missing_cash_cannot_enter_valuation():
    c = Mock(account_type_display_overrides={}, owner_name="Example")
    c.get_accounts.return_value = [{"number": "a", "type": "Margin"}]
    c.get_positions.return_value = []
    c.get_balances.return_value = {
        "perCurrencyBalances": [{"currency": "CAD", "cash": 0}]
    }
    assert build_portfolio([c], RATE).total_value_cad == 0
    with pytest.raises(ValueError, match="more than once"):
        build_portfolio([c, c], RATE)
    for malformed in (
        {},
        {"perCurrencyBalances": []},
        {"perCurrencyBalances": [{"currency": "CAD", "cash": float("nan")}]},
    ):
        c.get_balances.return_value = malformed
        with pytest.raises((KeyError, ValueError)):
            build_portfolio([c], RATE)
    broker = QuestradeClient.__new__(QuestradeClient)
    with patch.object(broker, "_get", return_value={}):
        with pytest.raises(KeyError):
            broker.get_positions("a")
        with pytest.raises(KeyError):
            broker.get_accounts()


@pytest.mark.parametrize("rate", [1, 1.25, 1.5])
@pytest.mark.parametrize("prior", [None, 30])
def test_fx_budget_is_conserved(rate, prior):
    rule = dict(
        cad_symbol="C",
        usd_symbol="U",
        total_target_pct=20,
        target_rounding_step=3,
        min_usd_to_cad_rate=1,
        max_usd_to_cad_rate=1.5,
    )
    state = {} if prior is None else {"pair": {"cad_target_pct": prior}}
    with (
        patch("src.fx_targets._load_fx_state", return_value=state),
        patch("src.fx_targets._save_fx_state"),
    ):
        weights = resolve_targets({}, {"pair": rule}, rate)
    assert weights["C"] + weights["U"] == 20
    assert min(weights.values()) >= 0


@pytest.mark.parametrize("flag,code", [("--syn", 2), ("--help", 0)])
def test_cli_stops_before_side_effects(flag, code):
    with (
        patch("sys.argv", ["main.py", flag]),
        patch.object(main, "_pull_latest") as pull,
        patch.object(main, "run_scheduled_sync") as sync,
        pytest.raises(SystemExit) as stop,
    ):
        main.main()
    assert stop.value.code == code
    pull.assert_not_called()
    sync.assert_not_called()


@pytest.mark.parametrize(
    "update,match",
    [
        ({"targets": {"ETF": 110, "OTHER": -10}}, "Invalid target"),
        ({"targets": {"ETF": float("nan")}}, "Invalid target"),
        ({"targets": {"ETF": 100, "CAD": 0}}, "Remove CAD"),
        ({"targets": {"ETF": 95, "USD": 5}}, "Remove USD"),
        (
            {
                "accounts": [
                    {"owner_name": "A", "token_file": "a.json"},
                    {"owner_name": "B", "token_file": "a.json"},
                ]
            },
            "distinct",
        ),
    ],
)
def test_invalid_config_stops_both_runs_before_auth(tmp_path, update, match):
    import yaml

    config = {
        "targets": {"ETF": 100},
        "accounts": [{"owner_name": "A", "token_file": "a.json"}],
    }
    config.update(update)
    (tmp_path / "settings.yaml").write_text(yaml.safe_dump(config))
    with (
        patch.object(main, "get_config_dir", return_value=tmp_path),
        patch.object(main, "QuestradeClient") as auth,
    ):
        for run in (main.run_rebalancer, main.run_scheduled_sync):
            with pytest.raises(ValueError, match=match):
                run()
        auth.assert_not_called()
