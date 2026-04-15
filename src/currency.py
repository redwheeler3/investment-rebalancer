"""
Currency handling module.

Fetches USD/CAD exchange rate and handles Norbert's Gambit logic.
Calculates per-account currency conversion needs with DLR.TO/DLR.U.TO share counts.
Accounts for the $9.99 + GST trading fee on CAD-side conversions.
"""

import math
import requests
from dataclasses import dataclass


# Norbert's Gambit trading fee: $9.99 + 5% GST = $10.49 CAD
NORBERTS_GAMBIT_FEE_CAD = 10.49


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
    dlr_price: float  # Current price of DLR
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


def _get_rate_from_dlr(client) -> float | None:
    """
    Derive USD/CAD exchange rate from DLR.TO and DLR.U.TO market quotes.

    DLR.TO trades in CAD, DLR.U.TO trades in USD — both represent the same
    underlying asset (Horizons US Dollar Currency ETF). The ratio of their
    prices gives the real-time market exchange rate.

    Args:
        client: A QuestradeClient instance.

    Returns:
        USD to CAD rate, or None if quotes unavailable.
    """
    dlr_cad_price = None
    dlr_usd_price = None

    # Look up DLR.TO (CAD-denominated)
    results = client.search_symbol("DLR.TO")
    for r in results:
        if r.get("symbol") == "DLR.TO":
            quotes = client.get_quote([r["symbolId"]])
            if quotes:
                dlr_cad_price = quotes[0].get("lastTradePrice") or quotes[0].get("bidPrice")
            break

    # Look up DLR.U.TO (USD-denominated)
    results = client.search_symbol("DLR.U.TO")
    for r in results:
        if r.get("symbol") == "DLR.U.TO":
            quotes = client.get_quote([r["symbolId"]])
            if quotes:
                dlr_usd_price = quotes[0].get("lastTradePrice") or quotes[0].get("bidPrice")
            break

    if dlr_cad_price and dlr_usd_price and dlr_usd_price > 0:
        rate = dlr_cad_price / dlr_usd_price
        # Sanity check: rate should be between 1.0 and 2.0
        if 1.0 < rate < 2.0:
            return round(rate, 4)

    return None


def get_dlr_price(client) -> float:
    """
    Get the current price of DLR.TO using the Questrade API.

    Args:
        client: A QuestradeClient instance.

    Returns:
        Current price of DLR.TO, or 0.0 if unavailable.
    """
    try:
        results = client.search_symbol("DLR.TO")
        for r in results:
            if r.get("symbol") == "DLR.TO":
                symbol_id = r["symbolId"]
                quotes = client.get_quote([symbol_id])
                if quotes:
                    price = quotes[0].get("lastTradePrice") or quotes[0].get("bidPrice", 0)
                    return float(price)
    except Exception:
        pass
    return 0.0


def calculate_currency_needs(
    trades: list,
    accounts: list,
    usd_to_cad_rate: float,
    dlr_price: float = 0.0,
) -> list:
    """
    Analyze trades to determine per-account currency conversion needs.

    For each account, calculates the NET currency position after all trades.
    Only generates a conversion if an account has a shortfall in one currency
    AND a surplus in the other (or needs to convert from external cash).

    For CAD->USD (buying DLR.TO): reserves $10.49 CAD for the trading fee,
    reducing the DLR shares by enough to cover it.

    Args:
        trades: List of TradeRecommendation objects.
        accounts: List of AccountInfo objects.
        usd_to_cad_rate: Current exchange rate.
        dlr_price: Current price of DLR.TO (CAD). If 0, DLR share count won't be shown.

    Returns:
        List of CurrencyConversion objects with per-account conversion details.
    """
    # Build a map of account info
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
            cad_needed = usd_shortfall * usd_to_cad_rate

            # Reserve fee from CAD side
            cad_for_dlr = cad_needed + NORBERTS_GAMBIT_FEE_CAD

            # Make sure we don't try to convert more CAD than is available
            cad_for_dlr = min(cad_for_dlr, net_cad)
            actual_cad_for_shares = cad_for_dlr - NORBERTS_GAMBIT_FEE_CAD

            if actual_cad_for_shares > 0 and dlr_price > 0:
                dlr_shares = int(math.floor(actual_cad_for_shares / dlr_price))
            else:
                dlr_shares = 0

            # Recalculate actual USD we'd get
            actual_usd = (dlr_shares * dlr_price) / usd_to_cad_rate if dlr_shares > 0 else usd_shortfall

            if cad_needed > 0.01:
                conversions.append(CurrencyConversion(
                    account_number=acct_num,
                    account_type=acct.account_type,
                    owner=acct.owner,
                    direction="CAD_TO_USD",
                    source_amount=dlr_shares * dlr_price if dlr_shares > 0 else cad_needed,
                    target_amount=actual_usd,
                    dlr_symbol="DLR.TO",
                    dlr_shares=dlr_shares,
                    dlr_price=dlr_price,
                    fee=NORBERTS_GAMBIT_FEE_CAD,
                ))

        elif net_cad < -0.01 and net_usd > 0.01:
            # Need CAD but have USD surplus -> Convert USD to CAD
            cad_shortfall = abs(net_cad)

            # DLR.U.TO price is approximately DLR.TO price / exchange rate
            dlr_u_price = dlr_price / usd_to_cad_rate if dlr_price > 0 else 0

            # Fee is always CAD-side, so we need enough USD to cover
            # the shortfall PLUS the fee after conversion
            usd_needed = (cad_shortfall + NORBERTS_GAMBIT_FEE_CAD) / usd_to_cad_rate
            usd_for_shares = min(usd_needed, net_usd)

            if usd_for_shares > 0 and dlr_u_price > 0:
                # Ceil to ensure enough CAD after conversion; cap at what's affordable
                dlr_shares_needed = int(math.ceil(usd_needed / dlr_u_price))
                dlr_shares_affordable = int(math.floor(net_usd / dlr_u_price))
                dlr_shares = min(dlr_shares_needed, dlr_shares_affordable)
            else:
                dlr_shares = 0

            actual_cad = (dlr_shares * dlr_u_price * usd_to_cad_rate) if dlr_shares > 0 else cad_shortfall

            if usd_needed > 0.01:
                conversions.append(CurrencyConversion(
                    account_number=acct_num,
                    account_type=acct.account_type,
                    owner=acct.owner,
                    direction="USD_TO_CAD",
                    source_amount=dlr_shares * dlr_u_price if dlr_shares > 0 else usd_needed,
                    target_amount=actual_cad,
                    dlr_symbol="DLR.U.TO",
                    dlr_shares=dlr_shares,
                    dlr_price=dlr_u_price,
                    fee=NORBERTS_GAMBIT_FEE_CAD,
                ))

    return conversions