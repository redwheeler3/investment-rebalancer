"""Tests for the FX math module — currency conversion sizing and buying power."""

import pytest
from dataclasses import dataclass

from src.fx_math import (
    to_cad,
    CurrencyTotals,
    build_account_trade_impacts,
    net_account_cash,
    max_usd_from_cad,
    max_cad_from_usd,
    cross_currency_buying_power,
    consume_cross_currency_cash,
)
from src.models import TradeRecommendation


@dataclass
class FakeDLRQuotes:
    """Minimal DLR quote structure for testing."""
    cad_buy_price: float = 13.79   # Ask price to buy DLR.TO (CAD leg)
    cad_sell_price: float = 13.78  # Bid price to sell DLR.TO
    usd_buy_price: float = 10.16   # Ask price to buy DLR.U.TO (USD leg)
    usd_sell_price: float = 10.15  # Bid price to sell DLR.U.TO


class TestToCad:
    def test_cad_value_unchanged(self):
        assert to_cad(100.0, "CAD", 1.36) == 100.0

    def test_usd_value_converted(self):
        assert to_cad(100.0, "USD", 1.36) == 136.0

    def test_zero_value(self):
        assert to_cad(0.0, "USD", 1.36) == 0.0


class TestCurrencyTotals:
    def test_add_cad(self):
        t = CurrencyTotals()
        t.add("CAD", 100.0)
        assert t.cad == 100.0
        assert t.usd == 0.0

    def test_add_usd(self):
        t = CurrencyTotals()
        t.add("USD", 50.0)
        assert t.cad == 0.0
        assert t.usd == 50.0

    def test_add_unsupported_raises(self):
        t = CurrencyTotals()
        with pytest.raises(ValueError):
            t.add("GBP", 10.0)


class TestBuildAccountTradeImpacts:
    def test_buy_spends_cash(self):
        trades = [
            TradeRecommendation(
                symbol="VCN.TO", action="BUY", quantity=10,
                account_number="12345", account_type="TFSA",
                owner="Alice", price=50.0, currency="CAD",
                estimated_value=500.0,
            )
        ]
        impacts = build_account_trade_impacts(trades)
        assert impacts["12345"].cad == 500.0  # Positive = spent

    def test_sell_raises_cash(self):
        trades = [
            TradeRecommendation(
                symbol="VCN.TO", action="SELL", quantity=10,
                account_number="12345", account_type="TFSA",
                owner="Alice", price=50.0, currency="CAD",
                estimated_value=500.0,
            )
        ]
        impacts = build_account_trade_impacts(trades)
        assert impacts["12345"].cad == -500.0  # Negative = received


class TestNetAccountCash:
    def test_no_impact(self):
        @dataclass
        class FakeAccount:
            cash_cad: float = 1000.0
            cash_usd: float = 500.0

        result = net_account_cash(FakeAccount())
        assert result.cad == 1000.0
        assert result.usd == 500.0

    def test_with_impact(self):
        @dataclass
        class FakeAccount:
            cash_cad: float = 1000.0
            cash_usd: float = 500.0

        impact = CurrencyTotals(cad=300.0, usd=100.0)
        result = net_account_cash(FakeAccount(), impact)
        assert result.cad == 700.0
        assert result.usd == 400.0


class TestMaxUsdFromCad:
    def test_with_dlr_quotes(self):
        """DLR-based: buy DLR.TO at ask, sell DLR.U.TO at bid."""
        dlr = FakeDLRQuotes()
        # $60,000 CAD - $10.49 fee = $59,989.51 usable
        # floor($59,989.51 / $13.79) = 4,349 shares
        # 4,349 * $10.15 = $44,142.35
        result = max_usd_from_cad(60000.0, 1.36, 10.49, dlr)
        assert result > 44000.0
        assert result < 45000.0

    def test_without_dlr_quotes_uses_rate(self):
        result = max_usd_from_cad(60000.0, 1.36, 10.49, None)
        expected = (60000.0 - 10.49) / 1.36
        assert abs(result - expected) < 0.01

    def test_insufficient_cad_after_fee(self):
        result = max_usd_from_cad(5.0, 1.36, 10.49, None)
        assert result == 0.0

    def test_zero_cad(self):
        result = max_usd_from_cad(0.0, 1.36, 10.49, None)
        assert result == 0.0


class TestMaxCadFromUsd:
    def test_with_dlr_quotes(self):
        """DLR-based: buy DLR.U.TO at ask, sell DLR.TO at bid."""
        dlr = FakeDLRQuotes()
        # $10,000 USD / $10.16 ask = 984 shares
        # 984 * $13.78 bid = $13,559.52 gross CAD
        # net = $13,559.52 - $10.49 fee = $13,549.03
        result = max_cad_from_usd(10000.0, 1.36, 10.49, dlr)
        assert result > 13500.0
        assert result < 13600.0

    def test_without_dlr_quotes_uses_rate(self):
        result = max_cad_from_usd(10000.0, 1.36, 10.49, None)
        expected = 10000.0 * 1.36 - 10.49
        assert abs(result - expected) < 0.01

    def test_zero_usd(self):
        result = max_cad_from_usd(0.0, 1.36, 10.49, None)
        assert result == 0.0


class TestCrossCurrencyBuyingPower:
    def test_cad_source(self):
        result = cross_currency_buying_power(60000.0, "CAD", 1.36, 10.49, None)
        assert result > 0

    def test_usd_source(self):
        result = cross_currency_buying_power(10000.0, "USD", 1.36, 10.49, None)
        assert result > 0

    def test_unsupported_currency_raises(self):
        with pytest.raises(ValueError):
            cross_currency_buying_power(1000.0, "GBP", 1.36, 10.49, None)


class TestConsumeCrossCurrencyCash:
    def test_cad_to_usd_reduces_cad(self):
        cash = {"CAD": 60000.0, "USD": 0.0}
        consume_cross_currency_cash(cash, "CAD", "USD", 5000.0, 1.36, 10.49, None)
        assert cash["CAD"] < 60000.0
        # USD remainder after paying cost should be >= 0
        assert cash["USD"] >= 0.0

    def test_usd_to_cad_reduces_usd(self):
        cash = {"CAD": 0.0, "USD": 10000.0}
        consume_cross_currency_cash(cash, "USD", "CAD", 5000.0, 1.36, 10.49, None)
        assert cash["USD"] < 10000.0
        assert cash["CAD"] >= 0.0

    def test_invalid_conversion_path_raises(self):
        cash = {"CAD": 1000.0, "USD": 1000.0}
        with pytest.raises(ValueError):
            consume_cross_currency_cash(cash, "GBP", "CAD", 500.0, 1.36, 10.49, None)
