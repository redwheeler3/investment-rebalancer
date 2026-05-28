"""Tests for the portfolio module — allocation math, drift, accuracy, and projection."""

from unittest.mock import patch, MagicMock
from datetime import date

from src.portfolio import (
    calculate_allocations_for_values,
    calculate_accuracy,
    get_drifts,
    simulate_rebalance,
    build_allocation_snapshot_from_values,
    _get_prev_close_price,
    PortfolioSummary,
    AccountInfo,
    Position,
    HoldingSummary,
)
from src.display import _compute_portfolio_day_pnl
from src.models import TradeRecommendation


# ══════════════════════════════════════════════════════════════════
# Allocation calculation
# ══════════════════════════════════════════════════════════════════


class TestCalculateAllocations:
    def test_basic_allocations(self):
        holdings = {"VCN.TO": 300000.0, "VUN.TO": 500000.0, "XBB.TO": 200000.0}
        result = calculate_allocations_for_values(
            holdings_value_cad=holdings,
            total_value_cad=1000000.0,
            cash_cad_total=0.0,
            cash_usd_total=0.0,
            usd_to_cad_rate=1.36,
        )
        assert abs(result["VCN.TO"] - 30.0) < 0.01
        assert abs(result["VUN.TO"] - 50.0) < 0.01
        assert abs(result["XBB.TO"] - 20.0) < 0.01

    def test_includes_cash(self):
        holdings = {"VCN.TO": 900000.0}
        result = calculate_allocations_for_values(
            holdings_value_cad=holdings,
            total_value_cad=1000000.0,
            cash_cad_total=50000.0,
            cash_usd_total=0.0,
            usd_to_cad_rate=1.36,
        )
        assert abs(result["CAD"] - 5.0) < 0.01
        assert abs(result["VCN.TO"] - 90.0) < 0.01

    def test_usd_cash_converted(self):
        holdings = {"IVV": 900000.0}
        # USD cash: $10,000 × 1.36 = $13,600 CAD = 1.36% of $1M
        result = calculate_allocations_for_values(
            holdings_value_cad=holdings,
            total_value_cad=1000000.0,
            cash_cad_total=0.0,
            cash_usd_total=10000.0,
            usd_to_cad_rate=1.36,
        )
        assert abs(result["USD"] - 1.36) < 0.01

    def test_zero_total_returns_empty(self):
        result = calculate_allocations_for_values(
            holdings_value_cad={"VCN.TO": 0.0},
            total_value_cad=0.0,
            cash_cad_total=0.0,
            cash_usd_total=0.0,
            usd_to_cad_rate=1.36,
        )
        assert result == {}

    def test_excludes_transient_symbols(self):
        holdings = {"VCN.TO": 800000.0, "DLR.TO": 50000.0}
        result = calculate_allocations_for_values(
            holdings_value_cad=holdings,
            total_value_cad=1000000.0,
            cash_cad_total=150000.0,
            cash_usd_total=0.0,
            usd_to_cad_rate=1.36,
            excluded_symbols={"DLR.TO"},
        )
        assert "DLR.TO" not in result
        assert "VCN.TO" in result


# ══════════════════════════════════════════════════════════════════
# Drift calculation
# ══════════════════════════════════════════════════════════════════


class TestGetDrifts:
    def test_perfect_alignment(self):
        alloc = {"VCN.TO": 30.0, "VUN.TO": 50.0, "XBB.TO": 20.0}
        targets = {"VCN.TO": 30.0, "VUN.TO": 50.0, "XBB.TO": 20.0}
        drifts = get_drifts(alloc, targets)
        for symbol, drift in drifts.items():
            assert abs(drift) < 0.001

    def test_overweight_positive_drift(self):
        alloc = {"VCN.TO": 35.0, "VUN.TO": 45.0, "XBB.TO": 20.0}
        targets = {"VCN.TO": 30.0, "VUN.TO": 50.0, "XBB.TO": 20.0}
        drifts = get_drifts(alloc, targets)
        assert drifts["VCN.TO"] == 5.0  # Overweight
        assert drifts["VUN.TO"] == -5.0  # Underweight

    def test_unknown_symbol_implicit_zero_target(self):
        alloc = {"VCN.TO": 30.0, "UNKNOWN": 10.0}
        targets = {"VCN.TO": 30.0}
        drifts = get_drifts(alloc, targets)
        assert drifts["UNKNOWN"] == 10.0  # All overweight (implicit 0% target)

    def test_target_not_held_shows_negative_drift(self):
        alloc = {"VCN.TO": 100.0}
        targets = {"VCN.TO": 70.0, "VUN.TO": 30.0}
        drifts = get_drifts(alloc, targets)
        assert drifts["VUN.TO"] == -30.0  # Not held, target 30% → drift -30%


# ══════════════════════════════════════════════════════════════════
# Accuracy score
# ══════════════════════════════════════════════════════════════════


class TestCalculateAccuracy:
    def test_perfect_score(self):
        alloc = {"VCN.TO": 50.0, "VUN.TO": 50.0}
        targets = {"VCN.TO": 50.0, "VUN.TO": 50.0}
        assert calculate_accuracy(alloc, targets) == 100.0

    def test_total_mismatch(self):
        alloc = {"VCN.TO": 100.0}
        targets = {"VUN.TO": 100.0}
        # Drift: VCN +100, VUN -100 → sum_abs = 200 → 100 - 100 = 0%
        assert calculate_accuracy(alloc, targets) == 0.0

    def test_moderate_drift(self):
        # 5% absolute drift total / 2 = 2.5% penalty
        alloc = {"VCN.TO": 52.5, "VUN.TO": 47.5}
        targets = {"VCN.TO": 50.0, "VUN.TO": 50.0}
        accuracy = calculate_accuracy(alloc, targets)
        assert abs(accuracy - 97.5) < 0.01

    def test_never_below_zero(self):
        alloc = {"A": 100.0}
        targets = {"B": 50.0, "C": 50.0}
        accuracy = calculate_accuracy(alloc, targets)
        assert accuracy >= 0.0


# ══════════════════════════════════════════════════════════════════
# Build allocation snapshot
# ══════════════════════════════════════════════════════════════════


class TestBuildAllocationSnapshot:
    def test_snapshot_has_correct_fields(self):
        snapshot = build_allocation_snapshot_from_values(
            holdings_value_cad={"VCN.TO": 500000.0, "VUN.TO": 500000.0},
            cash_cad_total=0.0,
            cash_usd_total=0.0,
            total_value_cad=1000000.0,
            targets={"VCN.TO": 50.0, "VUN.TO": 50.0, "CAD": 0.0, "USD": 0.0},
            usd_to_cad_rate=1.36,
        )
        assert "VCN.TO" in snapshot.allocations
        assert "VCN.TO" in snapshot.drifts
        assert snapshot.accuracy == 100.0


# ══════════════════════════════════════════════════════════════════
# Trade simulation / projection
# ══════════════════════════════════════════════════════════════════


def _make_portfolio():
    """Build a simple test portfolio with two CAD positions."""
    positions_a = [
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
    ]
    account = AccountInfo(
        number="11111", account_type="TFSA",
        client_account_type="Individual", owner="Alice",
        positions=positions_a, cash_cad=1000.0, cash_usd=0.0,
    )
    portfolio = PortfolioSummary(
        accounts=[account],
        holdings={
            "VCN.TO": HoldingSummary(value_cad=50000.0, total_quantity=1000, current_price=50.0, currency="CAD"),
            "VUN.TO": HoldingSummary(value_cad=30000.0, total_quantity=500, current_price=60.0, currency="CAD"),
        },
        total_value_cad=81000.0,
        cash_cad_total=1000.0,
        cash_usd_total=0.0,
    )
    return portfolio


class TestSimulateRebalance:
    def test_buy_increases_allocation(self):
        portfolio = _make_portfolio()
        targets = {"VCN.TO": 50.0, "VUN.TO": 50.0, "CAD": 0.0, "USD": 0.0}
        trades = [
            TradeRecommendation(
                symbol="VUN.TO", action="BUY", quantity=10,
                account_number="11111", account_type="TFSA",
                owner="Alice", price=60.0, currency="CAD",
                estimated_value=600.0,
            )
        ]
        snapshot = simulate_rebalance(portfolio, trades, targets, 1.36)
        # VUN.TO should have higher allocation after buying
        assert snapshot.allocations["VUN.TO"] > (30000.0 / 81000.0) * 100

    def test_sell_decreases_allocation(self):
        portfolio = _make_portfolio()
        targets = {"VCN.TO": 50.0, "VUN.TO": 50.0, "CAD": 0.0, "USD": 0.0}
        trades = [
            TradeRecommendation(
                symbol="VCN.TO", action="SELL", quantity=50,
                account_number="11111", account_type="TFSA",
                owner="Alice", price=50.0, currency="CAD",
                estimated_value=2500.0,
            )
        ]
        snapshot = simulate_rebalance(portfolio, trades, targets, 1.36)
        assert snapshot.allocations["VCN.TO"] < (50000.0 / 81000.0) * 100

    def test_no_trades_returns_current_state(self):
        portfolio = _make_portfolio()
        targets = {"VCN.TO": 50.0, "VUN.TO": 50.0, "CAD": 0.0, "USD": 0.0}
        snapshot = simulate_rebalance(portfolio, [], targets, 1.36)
        assert abs(snapshot.allocations["VCN.TO"] - (50000.0 / 81000.0) * 100) < 0.01

    def test_fx_buy_deducts_from_other_currency(self):
        """A requires_fx buy of a USD symbol should deduct from CAD cash."""
        portfolio = _make_portfolio()
        # Add some CAD cash to fund the FX buy
        portfolio.cash_cad_total = 10000.0
        portfolio.total_value_cad = 90000.0

        targets = {"VCN.TO": 40.0, "VUN.TO": 30.0, "IVV": 30.0, "CAD": 0.0, "USD": 0.0}
        trades = [
            TradeRecommendation(
                symbol="IVV", action="BUY", quantity=5,
                account_number="11111", account_type="TFSA",
                owner="Alice", price=500.0, currency="USD",
                estimated_value=2500.0,
                requires_fx=True,
            )
        ]
        snapshot = simulate_rebalance(portfolio, trades, targets, 1.36)
        # IVV should now have an allocation
        assert "IVV" in snapshot.allocations
        assert snapshot.allocations["IVV"] > 0


# ══════════════════════════════════════════════════════════════════
# Previous close price (candle filtering)
# ══════════════════════════════════════════════════════════════════


class TestGetPrevClosePrice:
    def test_returns_last_prior_candle_close(self):
        """Should return the close of the most recent candle before today."""
        today = date.today().isoformat()
        candles = [
            {"start": "2020-01-01T00:00:00-05:00", "close": 100.0},
            {"start": "2020-01-02T00:00:00-05:00", "close": 102.0},
            {"start": "2020-01-03T00:00:00-05:00", "close": 105.0},
            {"start": f"{today}T00:00:00-05:00", "close": 107.0},
        ]
        client = MagicMock()
        client.get_candles.return_value = candles

        result = _get_prev_close_price(client, symbol_id=12345)
        # Should be 105.0 (the last candle before today's date)
        assert result == 105.0

    def test_no_candles_returns_zero(self):
        """Should return 0.0 when no candle data is available."""
        client = MagicMock()
        client.get_candles.return_value = []
        result = _get_prev_close_price(client, symbol_id=12345)
        assert result == 0.0

    def test_only_today_candle_returns_zero(self):
        """Should return 0.0 when only today's candle exists."""
        today = date.today().isoformat()
        candles = [
            {"start": f"{today}T00:00:00-05:00", "close": 107.0},
        ]
        client = MagicMock()
        client.get_candles.return_value = candles
        result = _get_prev_close_price(client, symbol_id=12345)
        assert result == 0.0

    def test_handles_missing_close_field(self):
        """Should return 0.0 gracefully if close field is None."""
        candles = [
            {"start": "2026-05-27T00:00:00-05:00", "close": None},
        ]
        client = MagicMock()
        client.get_candles.return_value = candles
        result = _get_prev_close_price(client, symbol_id=12345)
        assert result == 0.0


# ══════════════════════════════════════════════════════════════════
# Portfolio day P&L calculation
# ══════════════════════════════════════════════════════════════════


class TestComputePortfolioDayPnl:
    def test_basic_pnl_cad(self):
        """Positive price change should produce positive P&L."""
        portfolio = PortfolioSummary(
            accounts=[],
            holdings={
                "VCN.TO": HoldingSummary(
                    value_cad=51000.0, total_quantity=1000,
                    current_price=51.0, currency="CAD",
                    prev_close_price=50.0,
                ),
            },
            total_value_cad=51000.0,
        )
        pnl, pct = _compute_portfolio_day_pnl(portfolio, usd_to_cad_rate=1.36)
        assert pnl == 1000.0  # (51 - 50) × 1000
        assert abs(pct - 2.0) < 0.01  # 1/50 × 100 = 2%

    def test_usd_holding_converted(self):
        """USD holdings P&L should be converted to CAD."""
        portfolio = PortfolioSummary(
            accounts=[],
            holdings={
                "IVV": HoldingSummary(
                    value_cad=136000.0, total_quantity=100,
                    current_price=110.0, currency="USD",
                    prev_close_price=100.0,
                ),
            },
            total_value_cad=136000.0,
        )
        pnl, pct = _compute_portfolio_day_pnl(portfolio, usd_to_cad_rate=1.36)
        # (110 - 100) × 100 × 1.36 = 1360
        assert abs(pnl - 1360.0) < 0.01
        assert abs(pct - 10.0) < 0.01

    def test_no_prev_close_excluded(self):
        """Holdings without prev_close should not affect P&L."""
        portfolio = PortfolioSummary(
            accounts=[],
            holdings={
                "NEW.TO": HoldingSummary(
                    value_cad=10000.0, total_quantity=100,
                    current_price=100.0, currency="CAD",
                    prev_close_price=0.0,
                ),
            },
            total_value_cad=10000.0,
        )
        pnl, pct = _compute_portfolio_day_pnl(portfolio, usd_to_cad_rate=1.36)
        assert pnl == 0.0
        assert pct == 0.0

    def test_negative_pnl(self):
        """Price decline should produce negative P&L."""
        portfolio = PortfolioSummary(
            accounts=[],
            holdings={
                "VCN.TO": HoldingSummary(
                    value_cad=48000.0, total_quantity=1000,
                    current_price=48.0, currency="CAD",
                    prev_close_price=50.0,
                ),
            },
            total_value_cad=48000.0,
        )
        pnl, pct = _compute_portfolio_day_pnl(portfolio, usd_to_cad_rate=1.36)
        assert pnl == -2000.0  # (48 - 50) × 1000
        assert abs(pct - (-4.0)) < 0.01
