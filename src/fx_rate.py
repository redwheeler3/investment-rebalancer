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
        """Least advantageous buy price for DLR.TO (ask preferred)."""
        return self.cad_ask_price or self.cad_bid_price

    @property
    def cad_sell_price(self) -> float:
        """Least advantageous sell price for DLR.TO (bid preferred)."""
        return self.cad_bid_price or self.cad_ask_price

    @property
    def usd_buy_price(self) -> float:
        """Least advantageous buy price for DLR.U.TO (ask preferred)."""
        return self.usd_ask_price or self.usd_bid_price

    @property
    def usd_sell_price(self) -> float:
        """Least advantageous sell price for DLR.U.TO (bid preferred)."""
        return self.usd_bid_price or self.usd_ask_price


def _extract_quote_sides(quote: dict) -> tuple[float, float, float]:
    """Return (bid, ask, last) with sensible fallbacks when one side is missing."""
    bid = float(quote.get("bidPrice") or 0)
    ask = float(quote.get("askPrice") or 0)
    last = float(quote.get("lastTradePrice") or 0)

    if bid <= 0:
        bid = last if last > 0 else ask
    if ask <= 0:
        ask = last if last > 0 else bid

    return bid, ask, last


def _midpoint(bid: float, ask: float) -> float:
    """Return a midpoint quote for non-directional exchange-rate estimation."""
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return ask if ask > 0 else bid


def fetch_dlr_quotes(client) -> DlrQuotes:
    """
    Fetch DLR.TO and DLR.U.TO quotes in one pass.

    Returns both prices and the derived exchange rate. This avoids
    redundant API calls — previously DLR.TO was looked up three times.

    Args:
        client: A QuestradeClient instance.

    Returns:
        DlrQuotes with prices and derived exchange rate.
    """
    cad_bid_price = 0.0
    cad_ask_price = 0.0
    usd_bid_price = 0.0
    usd_ask_price = 0.0

    try:
        # Look up DLR.TO (CAD-denominated)
        results = client.search_symbol("DLR.TO")
        for r in results:
            if r.get("symbol") == "DLR.TO":
                quotes = client.get_quote([r["symbolId"]])
                if quotes:
                    cad_bid_price, cad_ask_price, _ = _extract_quote_sides(quotes[0])
                break

        # Look up DLR.U.TO (USD-denominated)
        results = client.search_symbol("DLR.U.TO")
        for r in results:
            if r.get("symbol") == "DLR.U.TO":
                quotes = client.get_quote([r["symbolId"]])
                if quotes:
                    usd_bid_price, usd_ask_price, _ = _extract_quote_sides(quotes[0])
                break
    except Exception:
        pass

    # Derive exchange rate from the DLR pair
    exchange_rate = None
    cad_mid = _midpoint(cad_bid_price, cad_ask_price)
    usd_mid = _midpoint(usd_bid_price, usd_ask_price)
    if cad_mid > 0 and usd_mid > 0:
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


def _get_rate_from_dlr(client) -> float | None:
    """Derive USD/CAD exchange rate from DLR quotes. Delegates to fetch_dlr_quotes()."""
    return fetch_dlr_quotes(client).exchange_rate


def get_usd_to_cad_rate(client=None) -> float:
    """
    Get the current USD to CAD exchange rate from Questrade DLR quotes.

    Args:
        client: QuestradeClient instance for real-time market rate.

    Returns:
        USD to CAD exchange rate (e.g., 1.37 means 1 USD = 1.37 CAD).
    """
    if client is None:
        raise RuntimeError(
            "A Questrade client is required to derive USD/CAD from DLR quotes."
        )

    rate = _get_rate_from_dlr(client)
    if rate is None:
        raise RuntimeError(
            "Could not derive a live USD/CAD exchange rate from Questrade DLR quotes."
        )
    return rate
