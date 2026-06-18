"""Tests for the rebalancer module — sizing math, trade netting, and sell allocation."""

from src.rebalancer import (
    shares_for_drift_gap,
    max_sellable_without_crossing_target,
    net_trades,
    find_accounts_for_symbol,
    get_position_quantity,
    allocate_sell,
)
from src.portfolio import AccountInfo, Position
from src.models import TradeRecommendation


# ══════════════════════════════════════════════════════════════════
# Sizing math
# ══════════════════════════════════════════════════════════════════


class TestSharesForDriftGap:
    def test_basic_cad_sizing(self):
        """2.3% drift on $1M portfolio with $114.14 CAD price → 201 shares."""
        shares = shares_for_drift_gap(1000000.0, 2.3, 114.14, "CAD", 1.36)
        assert shares == 201

    def test_basic_usd_sizing(self):
        """1.9% drift on $1M portfolio with $512 USD price → floor(gap_usd / price)."""
        # gap_cad = 1.9% × $1M = $19,000
        # gap_usd = $19,000 / 1.36 = $13,970.59
        # shares = floor($13,970.59 / $512) = 27
        shares = shares_for_drift_gap(1000000.0, 1.9, 512.0, "USD", 1.36)
        assert shares == 27

    def test_zero_drift_returns_zero(self):
        shares = shares_for_drift_gap(1000000.0, 0.0, 100.0, "CAD", 1.36)
        assert shares == 0

    def test_zero_price_returns_zero(self):
        shares = shares_for_drift_gap(1000000.0, 2.0, 0.0, "CAD", 1.36)
        assert shares == 0

    def test_zero_portfolio_returns_zero(self):
        shares = shares_for_drift_gap(0.0, 2.0, 100.0, "CAD", 1.36)
        assert shares == 0

    def test_one_share_rule_kicks_in(self):
        """When gap is small but one share roughly closes it, return 1."""
        # gap_cad = 0.01% × $1M = $100
        # floor($100 / $80) = 1
        # But let's try even smaller: 0.005% × $1M = $50
        # floor($50 / $80) = 0
        # one_share_cad = $80 < 2 × $50 = $100 → return 1
        shares = shares_for_drift_gap(1000000.0, 0.005, 80.0, "CAD", 1.36)
        assert shares == 1

    def test_one_share_rule_doesnt_fire_when_too_expensive(self):
        """If one share costs more than 2× the gap, don't trade."""
        # gap_cad = 0.001% × $1M = $10
        # floor($10 / $500) = 0
        # one_share_cad = $500 >= 2 × $10 = $20 → NO one-share rule
        shares = shares_for_drift_gap(1000000.0, 0.001, 500.0, "CAD", 1.36)
        assert shares == 0

    def test_uses_floor_not_round(self):
        """Sizing always rounds down to prevent overshoot."""
        # gap_cad = 1.0% × $1M = $10,000
        # floor($10,000 / $33.33) = 300 (not 300.03 rounded)
        shares = shares_for_drift_gap(1000000.0, 1.0, 33.33, "CAD", 1.36)
        assert shares == 300  # floor(10000/33.33) = 300


class TestMaxSellableWithoutCrossingTarget:
    def test_basic_calculation(self):
        """2.3% overweight, each share = 0.0114% drift → floor(2.3/0.0114) shares."""
        # per_share_drift = ($114.14 / $1M) × 100 = 0.011414%
        # max = floor(2.3 / 0.011414) = 201
        result = max_sellable_without_crossing_target(1000000.0, 2.3, 114.14, "CAD", 1.36)
        assert result == 201

    def test_zero_drift_returns_zero(self):
        result = max_sellable_without_crossing_target(1000000.0, 0.0, 100.0, "CAD", 1.36)
        assert result == 0

    def test_negative_drift_returns_zero(self):
        """Can't sell an underweight position."""
        result = max_sellable_without_crossing_target(1000000.0, -1.0, 100.0, "CAD", 1.36)
        assert result == 0

    def test_usd_position(self):
        # per_share_drift = ($512 × 1.36 / $1M) × 100 = 0.069632%
        # max = floor(5.0 / 0.069632) = 71
        result = max_sellable_without_crossing_target(1000000.0, 5.0, 512.0, "USD", 1.36)
        assert result == 71


# ══════════════════════════════════════════════════════════════════
# Trade netting
# ══════════════════════════════════════════════════════════════════


class TestNetTrades:
    def test_single_buy_passes_through(self):
        trades = [
            TradeRecommendation(
                symbol="VCN.TO", action="BUY", quantity=100,
                account_number="11111", account_type="TFSA",
                owner="Alice", price=50.0, currency="CAD",
                estimated_value=5000.0, note="Underweight buy",
            )
        ]
        result = net_trades(trades)
        assert len(result) == 1
        assert result[0].action == "BUY"
        assert result[0].quantity == 100

    def test_single_sell_passes_through(self):
        trades = [
            TradeRecommendation(
                symbol="VCN.TO", action="SELL", quantity=50,
                account_number="11111", account_type="TFSA",
                owner="Alice", price=50.0, currency="CAD",
                estimated_value=2500.0, note="Overweight sell",
            )
        ]
        result = net_trades(trades)
        assert len(result) == 1
        assert result[0].action == "SELL"
        assert result[0].quantity == 50

    def test_buy_and_sell_cancel_out(self):
        """Equal buy and sell of same symbol/account net to nothing."""
        trades = [
            TradeRecommendation(
                symbol="VCN.TO", action="BUY", quantity=100,
                account_number="11111", account_type="TFSA",
                owner="Alice", price=50.0, currency="CAD",
                estimated_value=5000.0,
            ),
            TradeRecommendation(
                symbol="VCN.TO", action="SELL", quantity=100,
                account_number="11111", account_type="TFSA",
                owner="Alice", price=50.0, currency="CAD",
                estimated_value=5000.0,
            ),
        ]
        result = net_trades(trades)
        assert len(result) == 0

    def test_net_buy_from_mixed(self):
        """Sell 50 + Buy 150 = Net Buy 100."""
        trades = [
            TradeRecommendation(
                symbol="VCN.TO", action="SELL", quantity=50,
                account_number="11111", account_type="TFSA",
                owner="Alice", price=50.0, currency="CAD",
                estimated_value=2500.0, note="Funding sell",
            ),
            TradeRecommendation(
                symbol="VCN.TO", action="BUY", quantity=150,
                account_number="11111", account_type="TFSA",
                owner="Alice", price=51.0, currency="CAD",
                estimated_value=7650.0, note="Underweight buy",
            ),
        ]
        result = net_trades(trades)
        assert len(result) == 1
        assert result[0].action == "BUY"
        assert result[0].quantity == 100
        assert result[0].note == "Underweight buy"

    def test_net_sell_from_mixed(self):
        """Sell 200 + Buy 50 = Net Sell 150."""
        trades = [
            TradeRecommendation(
                symbol="VCN.TO", action="SELL", quantity=200,
                account_number="11111", account_type="TFSA",
                owner="Alice", price=50.0, currency="CAD",
                estimated_value=10000.0, note="Overweight sell",
            ),
            TradeRecommendation(
                symbol="VCN.TO", action="BUY", quantity=50,
                account_number="11111", account_type="TFSA",
                owner="Alice", price=51.0, currency="CAD",
                estimated_value=2550.0, note="Leftover cash buy",
            ),
        ]
        result = net_trades(trades)
        assert len(result) == 1
        assert result[0].action == "SELL"
        assert result[0].quantity == 150
        assert result[0].note == "Overweight sell"

    def test_different_accounts_not_netted(self):
        """Same symbol but different accounts remain separate."""
        trades = [
            TradeRecommendation(
                symbol="VCN.TO", action="BUY", quantity=100,
                account_number="11111", account_type="TFSA",
                owner="Alice", price=50.0, currency="CAD",
                estimated_value=5000.0,
            ),
            TradeRecommendation(
                symbol="VCN.TO", action="SELL", quantity=50,
                account_number="22222", account_type="RRSP",
                owner="Alice", price=50.0, currency="CAD",
                estimated_value=2500.0,
            ),
        ]
        result = net_trades(trades)
        assert len(result) == 2

    def test_multiple_buys_accumulate(self):
        """Multiple buys of same symbol/account combine."""
        trades = [
            TradeRecommendation(
                symbol="VCN.TO", action="BUY", quantity=100,
                account_number="11111", account_type="TFSA",
                owner="Alice", price=50.0, currency="CAD",
                estimated_value=5000.0, note="Underweight buy",
            ),
            TradeRecommendation(
                symbol="VCN.TO", action="BUY", quantity=25,
                account_number="11111", account_type="TFSA",
                owner="Alice", price=50.0, currency="CAD",
                estimated_value=1250.0, note="Leftover cash buy",
            ),
        ]
        result = net_trades(trades)
        assert len(result) == 1
        assert result[0].quantity == 125
        assert result[0].note == "Underweight buy"  # First buy note wins


# ══════════════════════════════════════════════════════════════════
# Account helpers
# ══════════════════════════════════════════════════════════════════


def _make_accounts():
    """Create test accounts with different holdings."""
    acct_a = AccountInfo(
        number="11111", account_type="TFSA",
        client_account_type="Individual", owner="Alice",
        positions=[
            Position(
                symbol="VCN.TO", symbol_id=1, quantity=1000,
                market_value=50000.0, current_price=50.0, currency="CAD",
                account_number="11111", account_type="TFSA", owner="Alice",
            ),
            Position(
                symbol="VUN.TO", symbol_id=2, quantity=500,
                market_value=30000.0, current_price=60.0, currency="CAD",
                account_number="11111", account_type="TFSA", owner="Alice",
            ),
        ],
    )
    acct_b = AccountInfo(
        number="22222", account_type="RRSP",
        client_account_type="Individual", owner="Bob",
        positions=[
            Position(
                symbol="VCN.TO", symbol_id=1, quantity=2000,
                market_value=100000.0, current_price=50.0, currency="CAD",
                account_number="22222", account_type="RRSP", owner="Bob",
            ),
            Position(
                symbol="XBB.TO", symbol_id=3, quantity=800,
                market_value=24000.0, current_price=30.0, currency="CAD",
                account_number="22222", account_type="RRSP", owner="Bob",
            ),
        ],
    )
    return [acct_a, acct_b]


class TestFindAccountsForSymbol:
    def test_finds_both_holders(self):
        accounts = _make_accounts()
        result = find_accounts_for_symbol("VCN.TO", accounts)
        assert len(result) == 2

    def test_finds_single_holder(self):
        accounts = _make_accounts()
        result = find_accounts_for_symbol("VUN.TO", accounts)
        assert len(result) == 1
        assert result[0].number == "11111"

    def test_no_holders(self):
        accounts = _make_accounts()
        result = find_accounts_for_symbol("UNKNOWN", accounts)
        assert len(result) == 0


class TestGetPositionQuantity:
    def test_held_symbol(self):
        accounts = _make_accounts()
        qty = get_position_quantity(accounts[0], "VCN.TO")
        assert qty == 1000

    def test_not_held_symbol(self):
        accounts = _make_accounts()
        qty = get_position_quantity(accounts[0], "XBB.TO")
        assert qty == 0.0


class TestAllocateSell:
    def test_sells_from_largest_holder(self):
        accounts = _make_accounts()
        drifts = {"VCN.TO": 2.0, "VUN.TO": -1.0, "XBB.TO": -0.5}

        trades = allocate_sell(
            symbol="VCN.TO",
            total_shares=100,
            price=50.0,
            currency="CAD",
            accounts=accounts,
            effective_drift=drifts,
            transient_symbols=set(),
            drift_trade_threshold_pct=0.5,
            position_deltas={},
        )
        assert len(trades) >= 1
        total_sold = sum(t.quantity for t in trades)
        assert total_sold == 100

    def test_prefers_account_with_underweight_alternatives(self):
        """Account with underweight holdings ranks higher for selling."""
        accounts = _make_accounts()
        # Account A has VUN.TO which is underweight — selling VCN.TO there
        # frees cash that can be redeployed
        drifts = {"VCN.TO": 3.0, "VUN.TO": -2.0, "XBB.TO": 0.1}

        trades = allocate_sell(
            symbol="VCN.TO",
            total_shares=50,
            price=50.0,
            currency="CAD",
            accounts=accounts,
            effective_drift=drifts,
            transient_symbols=set(),
            drift_trade_threshold_pct=0.5,
            position_deltas={},
        )
        # Account A should be preferred (has VUN.TO underweight)
        assert trades[0].account_number == "11111"

    def test_does_not_oversell(self):
        """Cannot sell more than the account holds."""
        accounts = _make_accounts()
        drifts = {"VCN.TO": 10.0}

        # Try to sell 5000 shares but accounts only hold 3000 total
        trades = allocate_sell(
            symbol="VCN.TO",
            total_shares=5000,
            price=50.0,
            currency="CAD",
            accounts=accounts,
            effective_drift=drifts,
            transient_symbols=set(),
            drift_trade_threshold_pct=0.5,
            position_deltas={},
        )
        total_sold = sum(t.quantity for t in trades)
        assert total_sold == 3000  # All available shares

    def test_zero_shares_returns_empty(self):
        accounts = _make_accounts()
        trades = allocate_sell(
            symbol="VCN.TO",
            total_shares=0,
            price=50.0,
            currency="CAD",
            accounts=accounts,
            effective_drift={"VCN.TO": 2.0},
            transient_symbols=set(),
            drift_trade_threshold_pct=0.5,
            position_deltas={},
        )
        assert trades == []


# ══════════════════════════════════════════════════════════════════
# Full planner integration tests
# ══════════════════════════════════════════════════════════════════

from src.rebalancer import calculate_trades, CashLedger
from src.portfolio import PortfolioSummary, HoldingSummary


def _build_test_portfolio(
    positions_by_account: list[dict],
    usd_to_cad_rate: float = 1.36,
) -> PortfolioSummary:
    """Build a portfolio from a simplified spec.

    Each dict in positions_by_account:
    {
        "number": "11111", "type": "TFSA", "owner": "Alice",
        "cash_cad": 100.0, "cash_usd": 0.0,
        "positions": [
            {"symbol": "VCN.TO", "qty": 1000, "price": 50.0, "currency": "CAD"},
        ]
    }
    """
    accounts = []
    holdings = {}
    total_value_cad = 0.0
    total_cash_cad = 0.0
    total_cash_usd = 0.0

    for acct_spec in positions_by_account:
        positions = []
        for i, pos_spec in enumerate(acct_spec["positions"]):
            symbol = pos_spec["symbol"]
            qty = pos_spec["qty"]
            price = pos_spec["price"]
            currency = pos_spec["currency"]
            market_value = qty * price
            value_cad = market_value * usd_to_cad_rate if currency == "USD" else market_value

            positions.append(Position(
                symbol=symbol, symbol_id=i + 1, quantity=qty,
                market_value=market_value, current_price=price,
                currency=currency, account_number=acct_spec["number"],
                account_type=acct_spec["type"], owner=acct_spec["owner"],
            ))

            if symbol not in holdings:
                holdings[symbol] = HoldingSummary(
                    value_cad=0.0, total_quantity=0, current_price=price,
                    currency=currency, bid_price=price, ask_price=price,
                )
            holdings[symbol].value_cad += value_cad
            holdings[symbol].total_quantity += qty
            total_value_cad += value_cad

        cash_cad = acct_spec.get("cash_cad", 0.0)
        cash_usd = acct_spec.get("cash_usd", 0.0)
        total_cash_cad += cash_cad
        total_cash_usd += cash_usd
        total_value_cad += cash_cad + cash_usd * usd_to_cad_rate

        accounts.append(AccountInfo(
            number=acct_spec["number"],
            account_type=acct_spec["type"],
            client_account_type="Individual",
            owner=acct_spec["owner"],
            positions=positions,
            cash_cad=cash_cad,
            cash_usd=cash_usd,
        ))

    return PortfolioSummary(
        accounts=accounts,
        holdings=holdings,
        total_value_cad=total_value_cad,
        cash_cad_total=total_cash_cad,
        cash_usd_total=total_cash_usd,
    )


class TestPlannerOverweightSells:
    """Test that overweight positions get sold."""

    def test_sells_overweight_symbol(self):
        """A symbol significantly over target should generate a sell."""
        portfolio = _build_test_portfolio([{
            "number": "11111", "type": "TFSA", "owner": "Alice",
            "cash_cad": 200.0, "cash_usd": 0.0,
            "positions": [
                {"symbol": "VCN.TO", "qty": 600, "price": 50.0, "currency": "CAD"},
                {"symbol": "VUN.TO", "qty": 200, "price": 60.0, "currency": "CAD"},
            ],
        }])
        # VCN.TO = 30000, VUN.TO = 12000, total ≈ 42200
        # Target: VCN.TO 50%, VUN.TO 50%
        # Actual: VCN.TO ≈ 71%, VUN.TO ≈ 28.4% → VCN.TO is way overweight
        targets = {"VCN.TO": 50.0, "VUN.TO": 50.0, "CAD": 0.0, "USD": 0.0}

        trades = calculate_trades(portfolio, targets, 1.36, 10.49, 0.5, set(), None)

        sell_trades = [t for t in trades if t.action == "SELL"]
        buy_trades = [t for t in trades if t.action == "BUY"]
        assert len(sell_trades) > 0
        assert sell_trades[0].symbol == "VCN.TO"
        assert len(buy_trades) > 0
        assert buy_trades[0].symbol == "VUN.TO"

    def test_no_trades_when_balanced(self):
        """A perfectly balanced portfolio should produce no trades."""
        portfolio = _build_test_portfolio([{
            "number": "11111", "type": "TFSA", "owner": "Alice",
            "cash_cad": 0.0, "cash_usd": 0.0,
            "positions": [
                {"symbol": "VCN.TO", "qty": 1000, "price": 50.0, "currency": "CAD"},
                {"symbol": "VUN.TO", "qty": 1000, "price": 50.0, "currency": "CAD"},
            ],
        }])
        targets = {"VCN.TO": 50.0, "VUN.TO": 50.0, "CAD": 0.0, "USD": 0.0}

        trades = calculate_trades(portfolio, targets, 1.36, 10.49, 0.5, set(), None)
        assert trades == []

    def test_drift_threshold_suppresses_small_trades(self):
        """Symbols within the drift threshold should not trigger trades."""
        portfolio = _build_test_portfolio([{
            "number": "11111", "type": "TFSA", "owner": "Alice",
            "cash_cad": 0.0, "cash_usd": 0.0,
            "positions": [
                {"symbol": "VCN.TO", "qty": 505, "price": 100.0, "currency": "CAD"},
                {"symbol": "VUN.TO", "qty": 495, "price": 100.0, "currency": "CAD"},
            ],
        }])
        # VCN.TO ≈ 50.5%, VUN.TO ≈ 49.5% → drift is ±0.5%
        targets = {"VCN.TO": 50.0, "VUN.TO": 50.0, "CAD": 0.0, "USD": 0.0}

        # With threshold 1.0%, the 0.5% drift should be suppressed
        trades = calculate_trades(portfolio, targets, 1.36, 10.49, 1.0, set(), None)
        assert trades == []


class TestPlannerCashDeployment:
    """Test that idle cash gets deployed into underweight positions."""

    def test_deploys_cash_into_underweight(self):
        """Pre-existing cash in an account should be deployed."""
        portfolio = _build_test_portfolio([{
            "number": "11111", "type": "TFSA", "owner": "Alice",
            "cash_cad": 5000.0, "cash_usd": 0.0,
            "positions": [
                {"symbol": "VCN.TO", "qty": 1000, "price": 50.0, "currency": "CAD"},
                {"symbol": "VUN.TO", "qty": 800, "price": 50.0, "currency": "CAD"},
            ],
        }])
        # VCN.TO = 50000, VUN.TO = 40000, cash = 5000, total = 95000
        # VCN.TO ≈ 52.6%, VUN.TO ≈ 42.1%
        # With targets 50/50: VCN.TO is over, VUN.TO is under
        targets = {"VCN.TO": 50.0, "VUN.TO": 50.0, "CAD": 0.0, "USD": 0.0}

        trades = calculate_trades(portfolio, targets, 1.36, 10.49, 0.5, set(), None)

        # Should buy VUN.TO (underweight) with available cash
        buy_trades = [t for t in trades if t.action == "BUY"]
        assert any(t.symbol == "VUN.TO" for t in buy_trades)

    def test_deploys_idle_cash_even_when_no_starter_trade_needed(self):
        """Cash deployment should run even when asset drift is within threshold."""
        portfolio = _build_test_portfolio([{
            "number": "11111", "type": "Margin", "owner": "Alice",
            "cash_cad": 6000.0, "cash_usd": 0.0,
            "positions": [
                {"symbol": "VSP.TO", "qty": 10000, "price": 100.0, "currency": "CAD"},
                {"symbol": "ZMMK.TO", "qty": 10000, "price": 100.0, "currency": "CAD"},
            ],
        }])
        # Total = 2,006,000. Each holding starts at about 49.85%.
        # VSP.TO is slightly underweight, but not enough to trigger a starter buy.
        targets = {"VSP.TO": 49.9, "ZMMK.TO": 49.8, "CAD": 0.0, "USD": 0.0}

        trades = calculate_trades(portfolio, targets, 1.36, 10.49, 0.5, set(), None)

        buy_trades = [t for t in trades if t.action == "BUY"]
        assert len(buy_trades) > 0
        assert all(t.account_number == "11111" for t in buy_trades)
        assert all(t.symbol in {"VSP.TO", "ZMMK.TO"} for t in buy_trades)
        assert sum(t.estimated_value for t in buy_trades) >= 5900.0

    def test_zero_value_portfolio_returns_no_trades(self):
        """Empty portfolio should produce no trades."""
        portfolio = PortfolioSummary(
            accounts=[], holdings={}, total_value_cad=0.0,
            cash_cad_total=0.0, cash_usd_total=0.0,
        )
        targets = {"VCN.TO": 100.0}
        trades = calculate_trades(portfolio, targets, 1.36, 10.49, 0.5, set(), None)
        assert trades == []


class TestPlannerMultiAccount:
    """Test multi-account rebalancing behavior."""

    def test_sells_from_account_with_alternatives(self):
        """Account with underweight alternatives should be preferred for sells."""
        portfolio = _build_test_portfolio([
            {
                "number": "11111", "type": "TFSA", "owner": "Alice",
                "cash_cad": 0.0, "cash_usd": 0.0,
                "positions": [
                    {"symbol": "VCN.TO", "qty": 2000, "price": 50.0, "currency": "CAD"},
                    {"symbol": "VUN.TO", "qty": 100, "price": 60.0, "currency": "CAD"},
                ],
            },
            {
                "number": "22222", "type": "RRSP", "owner": "Alice",
                "cash_cad": 0.0, "cash_usd": 0.0,
                "positions": [
                    {"symbol": "VCN.TO", "qty": 1000, "price": 50.0, "currency": "CAD"},
                ],
            },
        ])
        # VCN.TO total = 150000, VUN.TO = 6000, total = 156000
        # Target: VCN.TO 50%, VUN.TO 50%
        # VCN.TO is massively overweight, VUN.TO massively underweight
        targets = {"VCN.TO": 50.0, "VUN.TO": 50.0, "CAD": 0.0, "USD": 0.0}

        trades = calculate_trades(portfolio, targets, 1.36, 10.49, 0.5, set(), None)

        sell_trades = [t for t in trades if t.action == "SELL" and t.symbol == "VCN.TO"]
        assert len(sell_trades) > 0
        # Account 11111 should be preferred (has VUN.TO underweight alternative)
        first_sell = sell_trades[0]
        assert first_sell.account_number == "11111"

    def test_only_buys_in_accounts_that_hold_symbol(self):
        """Rule 5: can only buy symbols already held in the account."""
        portfolio = _build_test_portfolio([
            {
                "number": "11111", "type": "TFSA", "owner": "Alice",
                "cash_cad": 50000.0, "cash_usd": 0.0,
                "positions": [
                    {"symbol": "VCN.TO", "qty": 1000, "price": 50.0, "currency": "CAD"},
                ],
            },
            {
                "number": "22222", "type": "RRSP", "owner": "Alice",
                "cash_cad": 0.0, "cash_usd": 0.0,
                "positions": [
                    {"symbol": "VUN.TO", "qty": 500, "price": 60.0, "currency": "CAD"},
                ],
            },
        ])
        targets = {"VCN.TO": 50.0, "VUN.TO": 50.0, "CAD": 0.0, "USD": 0.0}

        trades = calculate_trades(portfolio, targets, 1.36, 10.49, 0.5, set(), None)

        # VUN.TO buys should only be in account 22222 (only holder)
        vun_buys = [t for t in trades if t.action == "BUY" and t.symbol == "VUN.TO"]
        for trade in vun_buys:
            assert trade.account_number == "22222"


class TestPlannerCurrencyPreference:
    """Test Rule 6: Prefer same-currency deployment before cross-currency."""

    def test_same_currency_buy_before_cross_currency(self):
        """Given CAD cash and both CAD/USD underweight symbols, buys CAD first."""
        portfolio = _build_test_portfolio([{
            "number": "11111", "type": "TFSA", "owner": "Alice",
            "cash_cad": 20000.0, "cash_usd": 0.0,
            "positions": [
                {"symbol": "VCN.TO", "qty": 500, "price": 50.0, "currency": "CAD"},
                {"symbol": "IVV", "qty": 10, "price": 500.0, "currency": "USD"},
            ],
        }])
        # Total ≈ 25000 + 6800 + 20000 = 51800
        # VCN.TO ≈ 48.3%, IVV ≈ 13.1%, cash ≈ 38.6%
        # Both underweight vs 50/50 target
        targets = {"VCN.TO": 50.0, "IVV": 50.0, "CAD": 0.0, "USD": 0.0}

        trades = calculate_trades(portfolio, targets, 1.36, 10.49, 0.5, set(), None)

        # Should have CAD buy (VCN.TO) without requires_fx
        vcn_buys = [t for t in trades if t.symbol == "VCN.TO" and t.action == "BUY"]
        assert len(vcn_buys) > 0
        assert vcn_buys[0].requires_fx is False

    def test_cross_currency_used_when_needed(self):
        """When only USD symbols are underweight but cash is CAD, FX is used."""
        portfolio = _build_test_portfolio([{
            "number": "11111", "type": "TFSA", "owner": "Alice",
            "cash_cad": 20000.0, "cash_usd": 0.0,
            "positions": [
                {"symbol": "VCN.TO", "qty": 1000, "price": 50.0, "currency": "CAD"},
                {"symbol": "IVV", "qty": 5, "price": 500.0, "currency": "USD"},
            ],
        }])
        # VCN.TO = 50000, IVV ≈ 3400, cash = 20000, total ≈ 73400
        # VCN.TO massively overweight, IVV underweight
        targets = {"VCN.TO": 40.0, "IVV": 60.0, "CAD": 0.0, "USD": 0.0}

        trades = calculate_trades(portfolio, targets, 1.36, 10.49, 0.5, set(), None)

        # IVV should be bought (funded via cross-currency conversion)
        ivv_buys = [t for t in trades if t.symbol == "IVV" and t.action == "BUY"]
        assert len(ivv_buys) > 0
        assert ivv_buys[0].requires_fx is True


class TestPlannerTransientSymbols:
    """Test that transient symbols are excluded from rebalancing."""

    def test_transient_symbol_excluded(self):
        """Transient symbols should not be bought or sold."""
        portfolio = _build_test_portfolio([{
            "number": "11111", "type": "TFSA", "owner": "Alice",
            "cash_cad": 100.0, "cash_usd": 0.0,
            "positions": [
                {"symbol": "VCN.TO", "qty": 1000, "price": 50.0, "currency": "CAD"},
                {"symbol": "DLR.TO", "qty": 500, "price": 13.79, "currency": "CAD"},
            ],
        }])
        targets = {"VCN.TO": 100.0, "CAD": 0.0, "USD": 0.0}

        trades = calculate_trades(
            portfolio, targets, 1.36, 10.49, 0.5,
            transient_symbols={"DLR.TO"},
            dlr_quotes=None,
        )

        # DLR.TO should not appear in any trades
        dlr_trades = [t for t in trades if t.symbol == "DLR.TO"]
        assert len(dlr_trades) == 0


class TestPlannerUnknownSymbols:
    """Test that unknown symbols (not in targets) get sold."""

    def test_unknown_symbol_sold(self):
        """A holding with no target (implicit 0%) should be recommended for sale."""
        portfolio = _build_test_portfolio([{
            "number": "11111", "type": "TFSA", "owner": "Alice",
            "cash_cad": 0.0, "cash_usd": 0.0,
            "positions": [
                {"symbol": "VCN.TO", "qty": 800, "price": 50.0, "currency": "CAD"},
                {"symbol": "RANDOM.TO", "qty": 200, "price": 50.0, "currency": "CAD"},
            ],
        }])
        # RANDOM.TO has no target → implicit 0% → should be sold
        targets = {"VCN.TO": 100.0, "CAD": 0.0, "USD": 0.0}

        trades = calculate_trades(portfolio, targets, 1.36, 10.49, 0.5, set(), None)

        sell_trades = [t for t in trades if t.symbol == "RANDOM.TO" and t.action == "SELL"]
        assert len(sell_trades) > 0


class TestPlannerWholeSharePricing:
    """Test that trades use correct pricing and whole shares."""

    def test_trades_are_whole_shares(self):
        """All trade quantities must be integers."""
        portfolio = _build_test_portfolio([{
            "number": "11111", "type": "TFSA", "owner": "Alice",
            "cash_cad": 200.0, "cash_usd": 0.0,
            "positions": [
                {"symbol": "VCN.TO", "qty": 700, "price": 50.0, "currency": "CAD"},
                {"symbol": "VUN.TO", "qty": 300, "price": 60.0, "currency": "CAD"},
            ],
        }])
        targets = {"VCN.TO": 50.0, "VUN.TO": 50.0, "CAD": 0.0, "USD": 0.0}

        trades = calculate_trades(portfolio, targets, 1.36, 10.49, 0.5, set(), None)

        for trade in trades:
            assert trade.quantity == int(trade.quantity)
            assert trade.quantity > 0


class TestCashLedger:
    """Test the CashLedger helper used during planning."""

    def test_from_portfolio(self):
        portfolio = _build_test_portfolio([{
            "number": "11111", "type": "TFSA", "owner": "Alice",
            "cash_cad": 5000.0, "cash_usd": 1000.0,
            "positions": [
                {"symbol": "VCN.TO", "qty": 100, "price": 50.0, "currency": "CAD"},
            ],
        }])
        ledger = CashLedger.from_portfolio(portfolio, 1.36, 10.49)
        assert ledger.cash("11111", "CAD") == 5000.0
        assert ledger.cash("11111", "USD") == 1000.0

    def test_credit_sale_adds_cash(self):
        portfolio = _build_test_portfolio([{
            "number": "11111", "type": "TFSA", "owner": "Alice",
            "cash_cad": 100.0, "cash_usd": 0.0,
            "positions": [
                {"symbol": "VCN.TO", "qty": 100, "price": 50.0, "currency": "CAD"},
            ],
        }])
        ledger = CashLedger.from_portfolio(portfolio, 1.36, 10.49)
        ledger.credit_sale("11111", "CAD", 5000.0)
        assert ledger.cash("11111", "CAD") == 5100.0

    def test_fund_buy_same_currency(self):
        portfolio = _build_test_portfolio([{
            "number": "11111", "type": "TFSA", "owner": "Alice",
            "cash_cad": 10000.0, "cash_usd": 0.0,
            "positions": [
                {"symbol": "VCN.TO", "qty": 100, "price": 50.0, "currency": "CAD"},
            ],
        }])
        ledger = CashLedger.from_portfolio(portfolio, 1.36, 10.49)
        converted = ledger.fund_buy("11111", "CAD", 3000.0)
        assert converted is False  # Same currency, no FX needed
        assert ledger.cash("11111", "CAD") == 7000.0

    def test_fund_buy_cross_currency(self):
        portfolio = _build_test_portfolio([{
            "number": "11111", "type": "TFSA", "owner": "Alice",
            "cash_cad": 10000.0, "cash_usd": 0.0,
            "positions": [
                {"symbol": "IVV", "qty": 10, "price": 500.0, "currency": "USD"},
            ],
        }])
        ledger = CashLedger.from_portfolio(portfolio, 1.36, 10.49)
        # Buy USD symbol with no USD cash → needs FX from CAD
        converted = ledger.fund_buy("11111", "USD", 1000.0)
        assert converted is True
        assert ledger.cash("11111", "CAD") < 10000.0

    def test_total_buying_power_includes_cross_currency(self):
        portfolio = _build_test_portfolio([{
            "number": "11111", "type": "TFSA", "owner": "Alice",
            "cash_cad": 50000.0, "cash_usd": 0.0,
            "positions": [
                {"symbol": "IVV", "qty": 10, "price": 500.0, "currency": "USD"},
            ],
        }])
        ledger = CashLedger.from_portfolio(portfolio, 1.36, 10.49)
        # USD buying power should include convertible CAD
        usd_power = ledger.total_buying_power("11111", "USD")
        assert usd_power > 0
        # Should be roughly $50,000 / 1.36 ≈ $36,764
        assert usd_power > 30000
