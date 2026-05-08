"""Tests for the portfolio module — allocation math, drift, accuracy, and projection."""

from src.portfolio import (
    calculate_allocations_for_values,
    calculate_accuracy,
    get_drifts,
    simulate_rebalance,
    build_allocation_snapshot_from_values,
    PortfolioSummary,
    AccountInfo,
    Position,
    HoldingSummary,
)
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
