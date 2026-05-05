"""
Exchange rate fetching and DLR quote data.

Fetches the USD/CAD exchange rate from Questrade DLR quotes and provides
DLR.TO/DLR.U.TO quote data used for Norbert's Gambit calculations.
"""

from dataclasses import dataclass


@dataclass
class DlrQuotes:
    """Cached DLR quote data — used for both exchange rate and Norbert's Gambit."""

    cad_bid_price: float  # DLR.TO sell price (CAD-denominated)
    cad_ask_price: float  # DLR.TO buy price (CAD-denominated)
    usd_bid_price: float  # DLR.U.TO sell price (USD-denominated)
    usd_ask_price: float  # DLR.U.TO buy price (USD-denominated)
    exchange_rate: float | None  # Derived USD/CAD rate, or None if unavailable

    @property
    def cad_buy_price(self) -> float:
        """Price to buy DLR.TO (ask side)."""
        return self.cad_ask_price

    @property
    def cad_sell_price(self) -> float:
        """Price to sell DLR.TO (bid side)."""
        return self.cad_bid_price

    @property
    def usd_buy_price(self) -> float:
        """Price to buy DLR.U.TO (ask side)."""
        return self.usd_ask_price

    @property
    def usd_sell_price(self) -> float:
        """Price to sell DLR.U.TO (bid side)."""
        return self.usd_bid_price


def _get_dlr_quote(client, symbol: str) -> tuple[float, float]:
    """Look up a DLR symbol and return (bid, ask)."""
    results = client.search_symbol(symbol)
    for r in results:
        if r.get("symbol") == symbol:
            quotes = client.get_quote([r["symbolId"]])
            if quotes:
                q = quotes[0]
                return float(q.get("bidPrice") or 0), float(q.get("askPrice") or 0)
            break
    return 0.0, 0.0


def fetch_dlr_quotes(client) -> DlrQuotes:
    """
    Fetch DLR.TO and DLR.U.TO quotes and derive the USD/CAD exchange rate.

    Args:
        client: A QuestradeClient instance.

    Returns:
        DlrQuotes with prices and derived exchange rate.
    """
    cad_bid_price, cad_ask_price = _get_dlr_quote(client, "DLR.TO")
    usd_bid_price, usd_ask_price = _get_dlr_quote(client, "DLR.U.TO")

    # Derive exchange rate from the midpoints of the DLR pair
    exchange_rate = None
    if cad_bid_price > 0 and cad_ask_price > 0 and usd_bid_price > 0 and usd_ask_price > 0:
        cad_mid = (cad_bid_price + cad_ask_price) / 2
        usd_mid = (usd_bid_price + usd_ask_price) / 2
        rate = cad_mid / usd_mid
        # Sanity check: rate should be between 1.0 and 2.0
        if 1.0 < rate < 2.0:
            exchange_rate = round(rate, 4)

    return DlrQuotes(
        cad_bid_price=cad_bid_price,
        cad_ask_price=cad_ask_price,
        usd_bid_price=usd_bid_price,
        usd_ask_price=usd_ask_price,
        exchange_rate=exchange_rate,
    )


def get_usd_to_cad_rate(client) -> float:
    """
    Get the current USD to CAD exchange rate from Questrade DLR quotes.

    Args:
        client: QuestradeClient instance for real-time market rate.

    Returns:
        USD to CAD exchange rate (e.g., 1.37 means 1 USD = 1.37 CAD).
    """
    rate = fetch_dlr_quotes(client).exchange_rate
    if rate is None:
        raise RuntimeError(
            "Could not derive a live USD/CAD exchange rate from Questrade DLR quotes."
        )
    return rate
