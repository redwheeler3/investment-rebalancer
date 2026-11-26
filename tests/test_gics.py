"""Posted broker units, household allocation and funding for locked certificates."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from src.external_accounts import parse_external_accounts
from src.fixed_income import fixed_income_budget
from src.fx_conversions import calculate_currency_needs
from src.fx_rate import DlrQuotes
from src.portfolio import (
    HoldingSummary,
    build_portfolio,
    get_current_allocations,
    simulate_rebalance,
)
from src.rebalancer import calculate_trades
from src.redemption import RedemptionLimit
from src.tactical import parse_tactical_config
from tests.test_rebalancer import _build_test_portfolio


def posted_portfolio():
    client = Mock(owner_name="Example", account_type_display_overrides={})
    client.get_accounts.return_value = [
        {"number": number, "type": "RRSP"} for number in ("first", "second")
    ]
    client.get_balances.return_value = {
        "perCurrencyBalances": [{"currency": "CAD", "cash": 0}]
    }
    client.get_positions.return_value = [
        {
            "symbol": symbol,
            "symbolId": i,
            "openQuantity": 40000,
            "currentPrice": 100,
            "currentMarketValue": 40000,
            "averageEntryPrice": 100,
        }
        for i, symbol in [(1, "CERTA"), (2, "CERTB")]
    ] + [
        {
            "symbol": "EQ",
            "symbolId": 3,
            "openQuantity": 1,
            "currentPrice": 100,
            "currentMarketValue": 100,
        }
    ]
    client.get_symbols.return_value = [
        {
            "symbolId": i,
            "currency": "CAD",
            "securityType": "Bond",
            "isTradable": False,
            "isQuotable": False,
            "description": f"EXAMPLE MAT 11/25/{year} 4.10% {term}Y CPD",
        }
        for i, year, term in [(1, 2028, 2), (2, 2031, 5)]
    ] + [{"symbolId": 3, "currency": "USD"}]
    return client


def test_certificates_cannot_fund_sales_buys_or_fx():
    portfolio = build_portfolio([posted_portfolio()], 1.35)
    targets = {"EQ": 100}
    quotes = DlrQuotes(13.5, 13.5, 10, 10, 1.35)
    orders = calculate_trades(portfolio, targets, 1.35, 10, 0.1, set(), quotes)
    assert orders == []
    assert (
        calculate_currency_needs(orders, portfolio.accounts, 1.35, quotes, targets)
        == []
    )
    assert portfolio.total_value_cad == 160270
    allocations = get_current_allocations(portfolio, 1.35)
    assert "CERTA" not in allocations
    for term in ["GIC:2Y", "GIC:5Y"]:
        assert allocations[term] == pytest.approx(80000 / 160270 * 100)
    assert simulate_rebalance(portfolio, [], targets, 1.35).allocations == allocations
    for account in portfolio.accounts:
        assert [p.symbol for p in account.positions[:2]] == ["CERTA", "CERTB"]
        assert (
            account.redemption_limits["CERTA"].units
            == account.redemption_limits["CERTB"].units
            == 0
        )


@pytest.mark.parametrize(
    "description",
    [
        "EXAMPLE MAT 11/25/2028 4.10% CPD",
        "EXAMPLE MAT 02/30/2028 4.10% 2Y CPD",
        "EXAMPLE MAT 11/25/2028 4.10% 3Y 2Y CPD",
    ],
)
def test_ambiguous_bond_retains_value_and_cannot_trade(description, caplog):
    client = posted_portfolio()
    client.get_symbols.return_value[0]["description"] = description
    portfolio = build_portfolio([client], 1)
    assert get_current_allocations(portfolio, 1)["CERTA"] == pytest.approx(
        100 * 80000 / 160200
    )
    assert "Review CERTA" in caplog.text
    assert calculate_trades(portfolio, {"EQ": 100}, 1, 0, 0.1, set(), None) == []


def test_missing_bond_details_cannot_turn_par_units_into_cash(caplog):
    client = posted_portfolio()
    client.get_symbols.return_value.pop(0)
    client.get_positions.return_value[0]["currency"] = "CAD"
    portfolio = build_portfolio([client], 1)
    assert "Review CERTA" in caplog.text
    assert portfolio.holdings["CERTA"].value_cad == 80000
    assert calculate_trades(portfolio, {"EQ": 100}, 1, 0, 0.1, set(), None) == []


def test_level_one_budget_keeps_ideal_targets_and_respects_restricted_floor():
    portfolio = build_portfolio([posted_portfolio()], 1)
    portfolio.total_value_cad = 2500000
    composition = {"GIC:2Y": 20, "GIC:5Y": 20, "PRIVATE": 25, "SAVINGS": 25, "MM": 10}
    targets = {s: weight * 0.15 for s, weight in composition.items()}
    for symbol in ("PRIVATE", "SAVINGS", "MM"):
        portfolio.holdings[symbol] = HoldingSummary()

    def dollar_budgets():
        budget = fixed_income_budget(portfolio, targets, composition, 1)
        return {
            s: pct * portfolio.total_value_cad / 100
            for s, pct in budget.targets.items()
        }

    amounts = dollar_budgets()
    assert amounts["GIC:2Y"] == amounts["GIC:5Y"] == pytest.approx(80000)
    remaining = 2500000 * 0.15 - 160000
    assert (
        amounts["PRIVATE"] == amounts["SAVINGS"] == pytest.approx(remaining * 25 / 60)
    )
    assert amounts["MM"] == pytest.approx(remaining * 10 / 60)
    assert targets["GIC:2Y"] == targets["GIC:5Y"] == 3

    acct = portfolio.accounts[0]
    acct.positions.append(
        replace(
            acct.positions[-1], symbol="PRIVATE", market_value=120000, currency="CAD"
        )
    )
    acct.redemption_limits["PRIVATE"] = RedemptionLimit(0, "hold")
    amounts = dollar_budgets()
    assert amounts["PRIVATE"] == pytest.approx(120000)
    assert amounts["MM"] == pytest.approx((remaining - 120000) * 10 / 35)


@pytest.mark.parametrize("room, expected_units", [(-200, 0), (0, 0), (99, 0), (250, 2)])
def test_cash_cannot_buy_fixed_units_beyond_tactical_budget(room, expected_units):
    client = posted_portfolio()
    # Only the money-market security can accept the large cash balance in either RRSP.
    client.get_symbols.return_value[-1]["currency"] = "CAD"
    client.get_positions.return_value[-1]["symbol"] = "MM"
    client.get_balances.return_value["perCurrencyBalances"][0]["cash"] = 25000
    portfolio = build_portfolio([client], 1)
    fixed_pct = (160200 + room) / portfolio.total_value_cad * 100
    composition = {"MM": 60, "GIC:2Y": 20, "GIC:5Y": 20}
    targets = {s: fixed_pct * w / 100 for s, w in composition.items()}
    targets["EQ"] = 100 - fixed_pct
    budget = fixed_income_budget(portfolio, targets, composition, 1)
    orders = calculate_trades(
        portfolio, targets, 1, 0, 0.1, set(), None, fixed_budget=budget
    )
    assert all((t.symbol, t.action) == ("MM", "BUY") for t in orders)
    assert sum(t.quantity for t in orders) == expected_units

    zero_targets = {s: 0 for s in composition}
    budget = fixed_income_budget(portfolio, zero_targets, composition, 1)
    assert budget.locked_excess_cad == 160000
    assert (
        calculate_trades(
            portfolio, zero_targets, 1, 0, 0.1, set(), None, fixed_budget=budget
        )
        == []
    )


def test_empty_term_is_a_fixed_target_without_external_account_routing():
    mix = {"MM": 60, "GIC:2Y": 20, "GIC:5Y": 20}
    assert (
        parse_external_accounts({"tactical_deployment": {"fixed_composition": mix}})
        == {}
    )
    with pytest.raises(ValueError, match="only in fixed_composition"):
        parse_external_accounts({"targets": {"GIC:2Y": 20}})
    client = posted_portfolio()
    client.get_positions.return_value.pop(0)
    portfolio = build_portfolio([client], 1)
    budget = fixed_income_budget(portfolio, mix, mix, 1)
    assert budget.targets["GIC:2Y"] == 0
    assert mix["GIC:2Y"] == 20


def purchase_plan(accounts, targets):
    portfolio = _build_test_portfolio(accounts, 1)
    composition = {s: w for s, w in targets.items() if s != "EQ"}
    config = parse_tactical_config(
        {
            "fixed_composition": composition,
        }
    )
    budget = fixed_income_budget(portfolio, targets, config.fixed_composition, 1)
    quotes = DlrQuotes(10, 10, 10, 10, 1)
    trades = calculate_trades(
        portfolio,
        targets,
        1,
        10,
        0.1,
        set(),
        quotes,
        fixed_budget=budget,
        tactical_config=config,
        gic_account_types=("RRSP", "SRRSP"),
        gic_min_buy_cad=5000,
    )
    return portfolio, trades, quotes


@pytest.mark.parametrize(
    ("accounts", "expected"),
    [
        ([("RRSP", 3000), ("SRRSP", 3000), ("Margin", 12000)], []),
        ([("Margin", 20200), ("SRRSP", 4999.99)], []),
        ([("SRRSP", 5000), ("TFSA", 9000)], [5000]),
        ([("RRSP", 8250.75), ("SRRSP", 7020.25)], [8250.75, 7020.25]),
    ],
)
def test_empty_term_uses_only_funded_account_types(accounts, expected):
    portfolio, trades, quotes = purchase_plan(
        [
            {
                "number": str(i),
                "type": kind,
                "owner": "A",
                "cash_cad": cash,
                "positions": [],
            }
            for i, (kind, cash) in enumerate(accounts)
        ],
        {"GIC:2Y": 100},
    )
    assert [t.estimated_value for t in trades] == expected
    assert all(
        t.quantity is None and t.price is None and t.symbol == "GIC:2Y" for t in trades
    )
    projected = simulate_rebalance(
        portfolio, trades, {"GIC:2Y": 100}, 1, dlr_quotes=quotes
    )
    assert projected.allocations.get(
        "GIC:2Y", 0
    ) * portfolio.total_value_cad / 100 == pytest.approx(sum(expected))
    assert sum(projected.allocations.values()) == pytest.approx(100)


def test_amount_based_purchase_releases_parked_fixed_cash_once():
    account = {
        "number": "r",
        "type": "RRSP",
        "owner": "A",
        "cash_cad": 0,
        "positions": [{"symbol": "MM", "qty": 200, "price": 100, "currency": "CAD"}],
    }
    portfolio, trades, quotes = purchase_plan([account], {"GIC:2Y": 40, "MM": 60})
    assert [(t.symbol, t.action, t.estimated_value) for t in trades] == [
        ("MM", "SELL", 8000),
        ("GIC:2Y", "BUY", 8000),
    ]
    assert (
        simulate_rebalance(portfolio, trades, {}, 1, dlr_quotes=quotes).allocations[
            "CAD"
        ]
        == 0
    )
    account["type"] = "Margin"
    assert purchase_plan([account], {"GIC:2Y": 40, "MM": 60})[1] == []


def test_gic_fx_funding_reserves_fee_and_shows_manual_trade():
    from io import StringIO
    from unittest.mock import patch
    from rich.console import Console
    from src.display import display_trades

    accounts = [
        {
            "number": "r",
            "type": "SRRSP",
            "owner": "Example",
            "cash_usd": 8010,
            "positions": [],
        }
    ]
    portfolio, trades, quotes = purchase_plan(accounts, {"GIC:5Y": 100})
    [trade] = trades
    assert trade.requires_fx and trade.estimated_value == 8000
    [fx] = calculate_currency_needs(trades, portfolio.accounts, 1, quotes, {}, 10)
    projected = simulate_rebalance(portfolio, trades, {}, 1, conversions=[fx])
    assert projected.allocations["GIC:5Y"] == pytest.approx(100)
    output = StringIO()
    with patch("src.display.console", Console(file=output, width=140)):
        display_trades(trades)
    for value in ("Recommended Trades", "GIC:5Y", "BUY", "8,000.00", "SRRSP", "—"):
        assert value in output.getvalue()


def test_two_minimum_purchases_can_fill_a_term_deficit():
    accounts = [
        {"number": kind, "owner": "A", "type": kind, "cash_cad": 6250, "positions": []}
        for kind in ("RRSP", "SRRSP")
    ]
    _, trades, _ = purchase_plan(accounts, {"GIC:2Y": 80, "EQ": 20})
    assert [t.estimated_value for t in trades] == [5000, 5000]


def test_daily_prices_use_face_dollars_for_quote_and_previous_close():
    from src.portfolio import fetch_quotes_for_holdings
    from src.display import _compute_portfolio_day_pnl

    client = posted_portfolio()
    portfolio = build_portfolio([client], 1)
    assert portfolio.holdings["CERTA"].current_price == 1
    assert portfolio.accounts[0].positions[0].average_cost == 1
    client.get_quote.return_value = [
        {
            "symbol": "CERTA",
            "lastTradePrice": 101,
            "lastTradeTime": "2026-11-25T15:00:00-05:00",
        }
    ]
    client.get_candles.side_effect = lambda sid, *args, **kwargs: (
        [{"start": "2026-11-24", "close": 100}] if sid == 1 else []
    )
    fetch_quotes_for_holdings(portfolio, [client])
    assert portfolio.holdings["CERTA"].prev_close_price == 1
    assert _compute_portfolio_day_pnl(portfolio, 1) == pytest.approx((800, 1))


def test_cashable_certificate_with_unfamiliar_terms_stays_locked():
    client = posted_portfolio()
    client.get_symbols.return_value[0].update(
        description="EXAMPLE CASHABLE GIC", isTradable=True
    )
    portfolio = build_portfolio([client], 1)
    assert portfolio.holdings["CERTA"].locked_bond_target == "CERTA"
    assert calculate_trades(portfolio, {"EQ": 100}, 1, 0, 0.1, set(), None) == []


def test_partial_gic_funding_keeps_inaccessible_parked_money_in_its_account():
    accounts = [
        {
            "number": kind,
            "type": kind,
            "owner": "A",
            "positions": [
                {"symbol": symbol, "qty": 100, "price": 100, "currency": "CAD"}
            ],
        }
        for kind, symbol in [("RRSP", "MM"), ("Margin", "MF")]
    ]

    _, trades, _ = purchase_plan(accounts, {"GIC:2Y": 50, "MM": 25, "MF": 25})
    assert [(t.symbol, t.action, t.estimated_value) for t in trades] == [
        ("MM", "SELL", 5000),
        ("GIC:2Y", "BUY", 5000),
    ]


def test_example_config_enables_saven_and_gic_purchases(tmp_path, monkeypatch):
    from pathlib import Path
    from shutil import copyfile
    import main

    copyfile(
        Path(main.__file__).parent / "config/settings.example.yaml",
        tmp_path / "settings.yaml",
    )
    monkeypatch.setattr(main, "get_config_dir", lambda: tmp_path)
    *_, account_types, minimum, tactical, external = main.load_config()
    assert account_types == ("RRSP", "SRRSP")
    assert minimum == 5000
    assert (
        tactical.fixed_composition["GIC:2Y"]
        == tactical.fixed_composition["GIC:5Y"]
        == 0.2
    )
    assert tactical.fixed_composition[next(iter(external))] == 0.25
