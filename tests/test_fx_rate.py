import pytest

from src.fx_rate import fetch_dlr_quotes, get_usd_to_cad_rate


class FakeQuestradeClient:
    def __init__(self, quote_by_symbol: dict[str, dict]):
        self.quote_by_symbol = quote_by_symbol

    def search_symbol(self, symbol: str) -> list[dict]:
        return [{"symbol": symbol, "symbolId": symbol}]

    def get_quote(self, symbol_ids: list[str]) -> list[dict]:
        return [self.quote_by_symbol[symbol_ids[0]]]


def test_fetch_dlr_quotes_uses_bid_ask_midpoint_for_exchange_rate():
    client = FakeQuestradeClient(
        {
            "DLR.TO": {"bidPrice": 13.78, "askPrice": 13.80},
            "DLR.U.TO": {"bidPrice": 10.15, "askPrice": 10.17},
        }
    )

    quotes = fetch_dlr_quotes(client)

    assert quotes.cad_bid_price == 13.78
    assert quotes.cad_ask_price == 13.80
    assert quotes.usd_bid_price == 10.15
    assert quotes.usd_ask_price == 10.17
    assert quotes.exchange_rate == 1.3573


def test_fetch_dlr_quotes_falls_back_to_last_trade_when_bid_ask_missing():
    client = FakeQuestradeClient(
        {
            "DLR.TO": {"bidPrice": 0, "askPrice": 0, "lastTradePrice": 13.79},
            "DLR.U.TO": {"bidPrice": None, "askPrice": None, "lastTradePrice": 10.16},
        }
    )

    quotes = fetch_dlr_quotes(client)

    assert quotes.cad_bid_price == 13.79
    assert quotes.cad_ask_price == 13.79
    assert quotes.usd_bid_price == 10.16
    assert quotes.usd_ask_price == 10.16
    assert quotes.exchange_rate == 1.3573


def test_fetch_dlr_quotes_accepts_below_parity_rate():
    # CAD can trade above parity with USD (USD/CAD fell to ~0.91 in 2007), so a
    # below-1.0 rate is legitimate and must NOT be rejected by the sanity band.
    client = FakeQuestradeClient(
        {
            "DLR.TO": {"bidPrice": 9.99, "askPrice": 10.01},     # mid 10.00
            "DLR.U.TO": {"bidPrice": 10.99, "askPrice": 11.01},  # mid 11.00
        }
    )

    quotes = fetch_dlr_quotes(client)

    assert quotes.exchange_rate == 0.9091
    assert get_usd_to_cad_rate(client) == 0.9091


def test_fetch_dlr_quotes_rejects_rate_above_band():
    # A derived rate above ~2.0 indicates a garbage quote (stale/empty leg, wrong
    # symbol match) — reject it rather than valuing the portfolio at a bad rate.
    client = FakeQuestradeClient(
        {
            "DLR.TO": {"bidPrice": 19.99, "askPrice": 20.01},  # mid 20.00
            "DLR.U.TO": {"bidPrice": 4.99, "askPrice": 5.01},  # mid 5.00 → rate 4.0
        }
    )

    quotes = fetch_dlr_quotes(client)

    assert quotes.exchange_rate is None
    with pytest.raises(RuntimeError):
        get_usd_to_cad_rate(client)


def test_fetch_dlr_quotes_rejects_rate_below_floor():
    # A derived rate below the 0.5 floor is implausible for USD/CAD and signals
    # bad quote data — reject it.
    client = FakeQuestradeClient(
        {
            "DLR.TO": {"bidPrice": 1.99, "askPrice": 2.01},     # mid 2.00
            "DLR.U.TO": {"bidPrice": 9.99, "askPrice": 10.01},  # mid 10.00 → rate 0.2
        }
    )

    quotes = fetch_dlr_quotes(client)

    assert quotes.exchange_rate is None
    with pytest.raises(RuntimeError):
        get_usd_to_cad_rate(client)


def test_get_usd_to_cad_rate_error_includes_quote_values_when_unavailable():
    client = FakeQuestradeClient(
        {
            "DLR.TO": {"bidPrice": 0, "askPrice": 0, "lastTradePrice": 0},
            "DLR.U.TO": {"bidPrice": 0, "askPrice": 0, "lastTradePrice": 0},
        }
    )

    with pytest.raises(RuntimeError) as exc_info:
        get_usd_to_cad_rate(client)

    assert "Could not derive a live USD/CAD exchange rate" in str(exc_info.value)