"""Tests for the post-rebalance currency conversion sweep.

Focus on `calculate_currency_needs`'s stranded-cash sweep and, in particular,
its "home currency" logic: cash is only swept into a currency the account can
actually deploy (holds a post-trade, positive-target position in), and only
when there is exactly one such currency.
"""

from src.fx_conversions import calculate_currency_needs
from src.fx_rate import DlrQuotes
from src.portfolio import AccountInfo, Position
from src.models import TradeRecommendation


def _dlr():
    """A simple DLR quote bundle (1 CAD-side share ≈ 1 USD-side share)."""
    return DlrQuotes(
        cad_bid_price=13.50,
        cad_ask_price=13.50,
        usd_bid_price=9.90,
        usd_ask_price=9.90,
        exchange_rate=1.36,
    )


def _pos(symbol, qty, price, currency, acct):
    return Position(
        symbol=symbol, symbol_id=abs(hash(symbol)) % 100000, quantity=qty,
        market_value=qty * price, current_price=price, currency=currency,
        account_number=acct, account_type="Margin", owner="Alice",
    )


def _sell(symbol, qty, price, currency, acct):
    return TradeRecommendation(
        symbol=symbol, action="SELL", quantity=qty, account_number=acct,
        account_type="Margin", owner="Alice", price=price, currency=currency,
        estimated_value=qty * price,
    )


class TestSweepHomeCurrency:
    def test_single_currency_account_still_swept(self):
        """Baseline: a CAD-only account with stranded USD cash sweeps USD→CAD."""
        acct = AccountInfo(
            number="1", account_type="Margin", client_account_type="Individual",
            owner="Alice", cash_cad=0.0, cash_usd=3000.0,
            positions=[_pos("VCN.TO", 200, 50.0, "CAD", "1")],
        )
        targets = {"VCN.TO": 100.0}

        conv = calculate_currency_needs([], [acct], 1.36, _dlr(), targets, 10.49)

        assert len(conv) == 1
        assert conv[0].direction == "USD_TO_CAD"
        assert conv[0].account_number == "1"

    def test_mixed_account_with_two_real_homes_not_swept(self):
        """Genuinely mixed account (both sides positive-target) is left alone.

        Its foreign cash is deployable same-currency by the rebalancer, so
        converting here would be an unnecessary round-trip.
        """
        acct = AccountInfo(
            number="1", account_type="Margin", client_account_type="Individual",
            owner="Alice", cash_cad=0.0, cash_usd=3000.0,
            positions=[
                _pos("VCN.TO", 200, 50.0, "CAD", "1"),
                _pos("IVV", 100, 50.0, "USD", "1"),
            ],
        )
        targets = {"VCN.TO": 50.0, "IVV": 50.0}

        conv = calculate_currency_needs([], [acct], 1.36, _dlr(), targets, 10.49)

        assert conv == []

    def test_mixed_account_target0_windown_side_is_swept(self):
        """The corner: mixed account whose USD side is a target-0 wind-down.

        AMZN has no target (→ target 0), so USD is not a deployable home even
        though the account holds a USD position. The USD proceeds of selling it
        should sweep into the account's real (CAD) home rather than strand.
        """
        acct = AccountInfo(
            number="1", account_type="Margin", client_account_type="Individual",
            owner="Alice", cash_cad=0.0, cash_usd=0.0,
            positions=[
                _pos("AMZN", 20, 100.0, "USD", "1"),   # target 0 → auto-sell
                _pos("VCN.TO", 200, 50.0, "CAD", "1"),  # real home
            ],
        )
        # Fully liquidate AMZN → $2000 USD proceeds land as USD cash.
        trades = [_sell("AMZN", 20, 100.0, "USD", "1")]
        targets = {"VCN.TO": 100.0}  # AMZN absent → 0

        conv = calculate_currency_needs(trades, [acct], 1.36, _dlr(), targets, 10.49)

        assert len(conv) == 1
        assert conv[0].direction == "USD_TO_CAD"
        assert conv[0].account_number == "1"
        assert conv[0].dlr_shares > 0

    def test_no_positive_target_home_not_swept(self):
        """Zero homes: only cash and target-0 positions → nowhere to deploy.

        Converting would strand the cash just as thoroughly in the other
        currency, so no conversion is generated.
        """
        acct = AccountInfo(
            number="1", account_type="Margin", client_account_type="Individual",
            owner="Alice", cash_cad=0.0, cash_usd=3000.0,
            positions=[_pos("AMZN", 20, 100.0, "USD", "1")],  # target 0
        )
        targets = {"VCN.TO": 100.0}  # nothing this account holds is targeted

        conv = calculate_currency_needs([], [acct], 1.36, _dlr(), targets, 10.49)

        assert conv == []

    def test_position_fully_sold_no_longer_counts_as_home(self):
        """A positive-target position fully sold in-plan stops being a home.

        Here VCN is targeted but the plan sells the entire position, so CAD is
        no longer deployable; USD (with a real IVV target) becomes the single
        home and stranded CAD sweeps into it.
        """
        acct = AccountInfo(
            number="1", account_type="Margin", client_account_type="Individual",
            owner="Alice", cash_cad=2000.0, cash_usd=0.0,
            positions=[
                _pos("VCN.TO", 100, 50.0, "CAD", "1"),
                _pos("IVV", 50, 50.0, "USD", "1"),
            ],
        )
        trades = [_sell("VCN.TO", 100, 50.0, "CAD", "1")]  # fully exit VCN
        targets = {"VCN.TO": 40.0, "IVV": 60.0}

        conv = calculate_currency_needs(trades, [acct], 1.36, _dlr(), targets, 10.49)

        # CAD cash (original 2000 + 5000 proceeds) sweeps toward the USD home.
        assert any(c.direction == "CAD_TO_USD" and c.account_number == "1" for c in conv)
