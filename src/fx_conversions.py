"""
Post-rebalance currency conversion planning.

Analyzes planned trades to determine per-account Norbert's Gambit conversions:
calculates how many DLR.TO or DLR.U.TO shares to buy/sell to fund cross-currency
trades, including sweep logic for stranded foreign cash.
"""

import math
from dataclasses import dataclass

from src.fx_math import build_account_trade_impacts, net_account_cash
from src.fx_rate import DlrQuotes


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


def _positive_target_currency_homes(acct, trades: list, targets: dict) -> set:
    """Return the currencies in which an account has a deployable home.

    A currency is a "home" when, *after the planned trades*, the account still
    holds at least one position in that currency whose target is positive. That
    is exactly the condition under which the rebalancer's same-currency
    deployment can put cash in that currency to work.

    Post-trade quantities matter because a target-0 position being wound down
    (e.g. an untargeted AMZN) may be fully sold within the plan — its currency
    should not count as a home just because the position existed pre-trade.
    Target-0 positions never count regardless: they are only ever sold, never
    bought, so cash in their currency has nowhere to go.
    """
    deltas: dict[str, int] = {}
    for trade in trades:
        if trade.action not in {"BUY", "SELL"} or trade.quantity is None:
            continue
        if trade.account_number != acct.number:
            continue
        delta = trade.quantity if trade.action == "BUY" else -trade.quantity
        deltas[trade.symbol] = deltas.get(trade.symbol, 0) + delta

    homes = set()
    for pos in acct.positions:
        post_trade_qty = pos.quantity + deltas.get(pos.symbol, 0)
        if post_trade_qty > 0 and targets.get(pos.symbol, 0.0) > 0:
            homes.add(pos.currency)
    return homes


def calculate_currency_needs(
    trades: list,
    accounts: list,
    usd_to_cad_rate: float,
    dlr_quotes: DlrQuotes,
    targets: dict,
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
        targets: Resolved target allocations, used to decide which currency an
            account can actually deploy cash in (its "home" currency).
        norberts_gambit_fee_cad: Trading fee in CAD for Norbert's Gambit.

    Returns:
        List of CurrencyConversion objects with per-account conversion details.
    """
    if dlr_quotes is None:
        raise RuntimeError("DLR quotes are required for currency conversion planning.")

    impacts = build_account_trade_impacts(trades)
    conversions = []
    for acct in accounts:
        funds = net_account_cash(acct, impacts.get(acct.number))
        homes = _positive_target_currency_homes(acct, trades, targets)
        # A deficit takes precedence. Sweep the entire available source when
        # the destination is the only currency this account can deploy.
        if funds.cad > 0 and (funds.usd < -0.005 or homes == {"USD"}):
            direction, symbol = "CAD_TO_USD", "DLR.TO"
            ask, bid = dlr_quotes.cad_buy_price, dlr_quotes.usd_sell_price
            source = max(0.0, funds.cad - norberts_gambit_fee_cad)
            needed = max(0.0, -funds.usd)
            sweep = homes == {"USD"}
        elif funds.usd > 0 and (funds.cad < -0.005 or homes == {"CAD"}):
            direction, symbol = "USD_TO_CAD", "DLR.U.TO"
            ask, bid = dlr_quotes.usd_buy_price, dlr_quotes.cad_sell_price
            source = funds.usd
            needed = max(0.0, -funds.cad) + norberts_gambit_fee_cad
            sweep = homes == {"CAD"}
        else:
            continue
        if ask <= 0 or bid <= 0:
            continue
        shares = math.floor(source / ask + 1e-10)
        if not sweep:
            shares = min(shares, math.ceil(needed / bid))
        if shares <= 0 or (
            direction == "USD_TO_CAD" and shares * bid <= norberts_gambit_fee_cad
        ):
            continue
        conversions.append(
            CurrencyConversion(
                account_number=acct.number,
                account_type=acct.account_type,
                owner=acct.owner,
                direction=direction,
                source_amount=shares * ask,
                target_amount=shares * bid,
                dlr_symbol=symbol,
                dlr_shares=shares,
                dlr_price=ask,
                fee=norberts_gambit_fee_cad,
            )
        )
    return conversions
