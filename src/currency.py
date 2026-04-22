"""
Currency handling module.

Fetches USD/CAD exchange rate and handles Norbert's Gambit logic.
Calculates per-account currency conversion needs with DLR.TO/DLR.U.TO share counts.
The trading fee is loaded from config/targets.yaml and passed in by the caller.
"""

import math
import requests
from dataclasses import dataclass


@dataclass
class CurrencyConversion:
    """A specific currency conversion action for a specific account."""

    account_number: str
    account_type: str
    owner: str
    direction: str  # "CAD_TO_USD" or "USD_TO_CAD"
    source_amount: float  # Amount in source currency to convert
    target_amount: float  # Approximate amount in target currency after conversion
    dlr_symbol: str  # "DLR.TO" (CAD->USD) or "DLR.U.TO" (USD->CAD)
    dlr_shares: int  # Number of DLR shares to buy
    dlr_price: float  # Buy-side quote used for the DLR leg (ask, when available)
    fee: float  # Trading fee in CAD


def get_usd_to_cad_rate(client=None) -> float:
    """
    Get the current USD to CAD exchange rate.

    Tries multiple sources in order of accuracy:
    1. Questrade market data (DLR.TO / DLR.U.TO ratio) — real-time market rate
    2. exchangerate-api.com — free, updated daily
    3. Bank of Canada Valet API — official daily rate
    4. Hardcoded 1.36 fallback

    Args:
        client: Optional QuestradeClient instance for real-time market rate.

    Returns:
        USD to CAD exchange rate (e.g., 1.37 means 1 USD = 1.37 CAD).
    """
    # Primary: derive rate from DLR.TO (CAD) / DLR.U.TO (USD) via Questrade
    if client is not None:
        try:
            rate = _get_rate_from_dlr(client)
            if rate:
                return rate
        except Exception:
            pass

    # Fallback 1: free exchange rate API (daily rates)
    try:
        resp = requests.get(
            "https://api.exchangerate-api.com/v4/latest/USD",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        rate = data["rates"].get("CAD")
        if rate:
            return float(rate)
    except Exception:
        pass

    # Fallback 2: Bank of Canada Valet API (official daily rate)
    try:
        resp = requests.get(
            "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?recent=1",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        observations = data.get("observations", [])
        if observations:
            rate = observations[-1].get("FXUSDCAD", {}).get("v")
            if rate:
                return float(rate)
    except Exception:
        pass

    # Last resort fallback
    print("WARNING: Could not fetch live exchange rate. Using fallback rate of 1.36")
    return 1.36


@dataclass
class DlrQuotes:
    """Cached DLR quote data — used for both exchange rate and Norbert's Gambit."""

    cad_bid_price: float  # DLR.TO sell price (CAD-denominated)
    cad_ask_price: float  # DLR.TO buy price (CAD-denominated)
    usd_bid_price: float  # DLR.U.TO sell price (USD-denominated)
    usd_ask_price: float  # DLR.U.TO buy price (USD-denominated)
    exchange_rate: float | None  # Derived USD/CAD rate, or None if unavailable

    @property
    def cad_price(self) -> float:
        """Backward-compatible alias for the DLR.TO buy-side quote."""
        return self.cad_buy_price

    @property
    def usd_price(self) -> float:
        """Backward-compatible alias for the DLR.U.TO buy-side quote."""
        return self.usd_buy_price

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


def calculate_currency_needs(
    trades: list,
    accounts: list,
    usd_to_cad_rate: float,
    dlr_quotes: DlrQuotes | None = None,
    norberts_gambit_fee_cad: float = 10.49,
) -> list:
    """
    Analyze trades to determine per-account currency conversion needs.

    For each account, calculates the NET currency position after all trades.
    Only generates a conversion if an account has a shortfall in one currency
    AND a surplus in the other (or needs to convert from external cash).

    For CAD->USD (buying DLR.TO): reserves the trading fee from CAD,
    reducing the DLR shares by enough to cover it.

    Args:
        trades: List of TradeRecommendation objects.
        accounts: List of AccountInfo objects.
        usd_to_cad_rate: Current exchange rate.
        dlr_quotes: DLR quote bundle used for conservative Norbert's Gambit math.
        norberts_gambit_fee_cad: Trading fee in CAD for Norbert's Gambit.

    Returns:
        List of CurrencyConversion objects with per-account conversion details.
    """
    # Build a map of account info
    if dlr_quotes is None:
        dlr_quotes = DlrQuotes(
            cad_bid_price=0.0,
            cad_ask_price=0.0,
            usd_bid_price=0.0,
            usd_ask_price=0.0,
            exchange_rate=None,
        )

    cad_buy_price = dlr_quotes.cad_buy_price
    cad_sell_price = dlr_quotes.cad_sell_price
    usd_buy_price = dlr_quotes.usd_buy_price
    usd_sell_price = dlr_quotes.usd_sell_price

    acct_map = {}
    for acct in accounts:
        acct_map[acct.number] = acct

    # Track net currency impact per account
    # Positive = spending cash, negative = receiving cash (from sells)
    account_impact = {}  # account_number -> {"CAD": net_spend, "USD": net_spend}

    for trade in trades:
        acct_num = trade.account_number
        if acct_num not in account_impact:
            account_impact[acct_num] = {"CAD": 0.0, "USD": 0.0}

        if trade.action == "BUY":
            account_impact[acct_num][trade.currency] += trade.estimated_value
        elif trade.action == "SELL":
            account_impact[acct_num][trade.currency] -= trade.estimated_value

    conversions = []

    for acct_num, impact in account_impact.items():
        acct = acct_map.get(acct_num)
        if not acct:
            continue

        # Net cash position after trades
        # Positive = have cash left, negative = short on cash
        net_cad = acct.cash_cad - impact["CAD"]
        net_usd = acct.cash_usd - impact["USD"]

        # Only generate conversion if one currency is short and there's
        # a logical need for conversion. Don't generate both directions.

        if net_usd < -0.01 and net_cad > 0.01:
            # Need USD but have CAD surplus -> Convert CAD to USD
            usd_shortfall = abs(net_usd)
            # Size the gambit from the USD proceeds side, not the CAD spend side.
            # Otherwise the bid/ask spread between DLR.TO and DLR.U.TO can leave
            # us a little short of the USD needed for the buy.
            if cad_buy_price > 0 and usd_sell_price > 0:
                dlr_shares_needed = int(math.ceil(usd_shortfall / usd_sell_price))
                dlr_shares_affordable = int(
                    math.floor(max(0.0, net_cad - norberts_gambit_fee_cad) / cad_buy_price)
                )
                dlr_shares = min(dlr_shares_needed, dlr_shares_affordable)
            else:
                dlr_shares = 0

            # Skip conversion if we can't buy even 1 DLR share (fee exceeds amount)
            if dlr_shares <= 0:
                continue

            # Recalculate actual USD we'd get
            actual_usd = (
                dlr_shares * usd_sell_price
                if dlr_shares > 0 and usd_sell_price > 0
                else (dlr_shares * cad_buy_price) / usd_to_cad_rate if dlr_shares > 0 and usd_to_cad_rate > 0 else 0.0
            )

            if usd_shortfall > 0.01:
                conversions.append(CurrencyConversion(
                    account_number=acct_num,
                    account_type=acct.account_type,
                    owner=acct.owner,
                    direction="CAD_TO_USD",
                    source_amount=(
                        dlr_shares * cad_buy_price
                        if dlr_shares > 0
                        else usd_shortfall * usd_to_cad_rate
                    ),
                    target_amount=actual_usd,
                    dlr_symbol="DLR.TO",
                    dlr_shares=dlr_shares,
                    dlr_price=cad_buy_price,
                    fee=norberts_gambit_fee_cad,
                ))

        elif net_cad < -0.01 and net_usd > 0.01:
            # Need CAD but have USD surplus -> Convert USD to CAD
            cad_shortfall = abs(net_cad)

            if usd_buy_price > 0 and cad_sell_price > 0:
                # Need enough CAD proceeds to cover the shortfall plus fee,
                # while respecting the USD cost to buy DLR.U.TO at the ask.
                dlr_shares_needed = int(math.ceil((cad_shortfall + norberts_gambit_fee_cad) / cad_sell_price))
                dlr_shares_affordable = int(math.floor(net_usd / usd_buy_price))
                dlr_shares = min(dlr_shares_needed, dlr_shares_affordable)
            else:
                dlr_shares = 0

            actual_cad = (
                dlr_shares * cad_sell_price
                if dlr_shares > 0 and cad_sell_price > 0
                else (dlr_shares * usd_buy_price * usd_to_cad_rate) if dlr_shares > 0 else cad_shortfall
            )

            # Skip conversion if we can't buy even 1 DLR.U share (fee exceeds amount)
            if dlr_shares <= 0:
                continue

            usd_needed = (dlr_shares * usd_buy_price) if dlr_shares > 0 else 0.0
            if usd_needed > 0.01:
                conversions.append(CurrencyConversion(
                    account_number=acct_num,
                    account_type=acct.account_type,
                    owner=acct.owner,
                    direction="USD_TO_CAD",
                    source_amount=usd_needed,
                    target_amount=actual_cad,
                    dlr_symbol="DLR.U.TO",
                    dlr_shares=dlr_shares,
                    dlr_price=usd_buy_price,
                    fee=norberts_gambit_fee_cad,
                ))

    # ── Sweep: convert stranded foreign cash in single-currency accounts ──
    # If all positions in an account are one currency, convert leftover cash
    # in the other currency.  Keep one fee in the account for the journal.
    for acct in accounts:
        pos_currencies = {p.currency for p in acct.positions if p.quantity > 0}
        if not pos_currencies or len(pos_currencies) > 1:
            continue  # Skip multi-currency or empty accounts

        position_currency = next(iter(pos_currencies))

        # Remaining cash after trades
        impact = account_impact.get(acct.number, {"CAD": 0.0, "USD": 0.0})
        remaining_cad = acct.cash_cad - impact["CAD"]
        remaining_usd = acct.cash_usd - impact["USD"]

        # Subtract cash already allocated to first-pass conversions
        existing_conv = None
        for conv in conversions:
            if conv.account_number != acct.number:
                continue
            if conv.direction == "CAD_TO_USD":
                remaining_cad -= (conv.source_amount + conv.fee)
                if position_currency == "USD":
                    existing_conv = conv
            elif conv.direction == "USD_TO_CAD":
                remaining_usd -= conv.source_amount
                if position_currency == "CAD":
                    existing_conv = conv

        if position_currency == "USD" and remaining_cad > norberts_gambit_fee_cad and cad_buy_price > 0:
            # Sweep remaining CAD → USD, keep fee in account for journal
            cad_for_shares = remaining_cad - norberts_gambit_fee_cad
            sweep_shares = int(math.floor(cad_for_shares / cad_buy_price))
            if sweep_shares > 0:
                if existing_conv:
                    # Augment existing conversion (same journal, no extra fee)
                    existing_conv.dlr_shares += sweep_shares
                    existing_conv.source_amount = existing_conv.dlr_shares * existing_conv.dlr_price
                    if existing_conv.direction == "CAD_TO_USD":
                        existing_conv.target_amount = (
                            existing_conv.dlr_shares * usd_sell_price
                            if usd_sell_price > 0
                            else existing_conv.source_amount / usd_to_cad_rate if usd_to_cad_rate > 0 else 0.0
                        )
                else:
                    conversions.append(CurrencyConversion(
                        account_number=acct.number,
                        account_type=acct.account_type,
                        owner=acct.owner,
                        direction="CAD_TO_USD",
                        source_amount=sweep_shares * cad_buy_price,
                        target_amount=(
                            sweep_shares * usd_sell_price
                            if usd_sell_price > 0
                            else (sweep_shares * cad_buy_price) / usd_to_cad_rate if usd_to_cad_rate > 0 else 0.0
                        ),
                        dlr_symbol="DLR.TO",
                        dlr_shares=sweep_shares,
                        dlr_price=cad_buy_price,
                        fee=norberts_gambit_fee_cad,
                    ))

        elif position_currency == "CAD" and usd_buy_price > 0 and cad_sell_price > 0:
            fee_in_usd = norberts_gambit_fee_cad / usd_to_cad_rate
            if remaining_usd > fee_in_usd:
                # Sweep remaining USD → CAD, keep fee-equivalent in account
                usd_for_shares = remaining_usd - fee_in_usd
                sweep_shares = int(math.floor(usd_for_shares / usd_buy_price))
                if sweep_shares > 0:
                    if existing_conv:
                        existing_conv.dlr_shares += sweep_shares
                        existing_conv.source_amount = existing_conv.dlr_shares * existing_conv.dlr_price
                        if existing_conv.direction == "USD_TO_CAD":
                            existing_conv.target_amount = (
                                existing_conv.dlr_shares * cad_sell_price
                                if cad_sell_price > 0
                                else existing_conv.source_amount * usd_to_cad_rate
                            )
                    else:
                        conversions.append(CurrencyConversion(
                            account_number=acct.number,
                            account_type=acct.account_type,
                            owner=acct.owner,
                            direction="USD_TO_CAD",
                            source_amount=sweep_shares * usd_buy_price,
                            target_amount=(
                                sweep_shares * cad_sell_price
                                if cad_sell_price > 0
                                else sweep_shares * usd_buy_price * usd_to_cad_rate
                            ),
                            dlr_symbol="DLR.U.TO",
                            dlr_shares=sweep_shares,
                            dlr_price=usd_buy_price,
                            fee=norberts_gambit_fee_cad,
                        ))

    return conversions
