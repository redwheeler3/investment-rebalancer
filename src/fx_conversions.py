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


def _build_account_map(accounts: list) -> dict:
    """Return accounts keyed by account number."""
    return {acct.number: acct for acct in accounts}


def _build_cad_to_usd_conversion(
    acct_num: str,
    acct,
    net_cash,
    usd_to_cad_rate: float,
    cad_buy_price: float,
    usd_sell_price: float,
    fee_cad: float,
) -> CurrencyConversion | None:
    """Build a CAD->USD conversion instruction when USD is short."""
    if net_cash.usd >= -0.01 or net_cash.cad <= 0.01:
        return None

    usd_shortfall = abs(net_cash.usd)
    if cad_buy_price > 0 and usd_sell_price > 0:
        shares_needed = int(math.ceil(usd_shortfall / usd_sell_price))
        shares_affordable = int(math.floor(max(0.0, net_cash.cad - fee_cad) / cad_buy_price))
        dlr_shares = min(shares_needed, shares_affordable)
    else:
        dlr_shares = 0

    if dlr_shares <= 0:
        return None

    actual_usd = (
        dlr_shares * usd_sell_price
        if usd_sell_price > 0
        else (dlr_shares * cad_buy_price) / usd_to_cad_rate if usd_to_cad_rate > 0 else 0.0
    )

    return CurrencyConversion(
        account_number=acct_num,
        account_type=acct.account_type,
        owner=acct.owner,
        direction="CAD_TO_USD",
        source_amount=dlr_shares * cad_buy_price,
        target_amount=actual_usd,
        dlr_symbol="DLR.TO",
        dlr_shares=dlr_shares,
        dlr_price=cad_buy_price,
        fee=fee_cad,
    )


def _build_usd_to_cad_conversion(
    acct_num: str,
    acct,
    net_cash,
    usd_to_cad_rate: float,
    usd_buy_price: float,
    cad_sell_price: float,
    fee_cad: float,
) -> CurrencyConversion | None:
    """Build a USD->CAD conversion instruction when CAD is short."""
    if net_cash.cad >= -0.01 or net_cash.usd <= 0.01:
        return None

    cad_shortfall = abs(net_cash.cad)
    if usd_buy_price > 0 and cad_sell_price > 0:
        shares_needed = int(math.ceil((cad_shortfall + fee_cad) / cad_sell_price))
        shares_affordable = int(math.floor(net_cash.usd / usd_buy_price))
        dlr_shares = min(shares_needed, shares_affordable)
    else:
        dlr_shares = 0

    if dlr_shares <= 0:
        return None

    actual_cad = (
        dlr_shares * cad_sell_price
        if cad_sell_price > 0
        else (dlr_shares * usd_buy_price * usd_to_cad_rate)
    )

    return CurrencyConversion(
        account_number=acct_num,
        account_type=acct.account_type,
        owner=acct.owner,
        direction="USD_TO_CAD",
        source_amount=dlr_shares * usd_buy_price,
        target_amount=actual_cad,
        dlr_symbol="DLR.U.TO",
        dlr_shares=dlr_shares,
        dlr_price=usd_buy_price,
        fee=fee_cad,
    )


def calculate_currency_needs(
    trades: list,
    accounts: list,
    usd_to_cad_rate: float,
    dlr_quotes: DlrQuotes,
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
    if dlr_quotes is None:
        raise RuntimeError(
            "DLR quotes are required for currency conversion planning."
        )

    cad_buy_price = dlr_quotes.cad_buy_price
    cad_sell_price = dlr_quotes.cad_sell_price
    usd_buy_price = dlr_quotes.usd_buy_price
    usd_sell_price = dlr_quotes.usd_sell_price

    acct_map = _build_account_map(accounts)
    account_impact = build_account_trade_impacts(trades)

    conversions = []

    for acct_num, impact in account_impact.items():
        acct = acct_map.get(acct_num)
        if not acct:
            continue

        net_cash = net_account_cash(acct, impact)

        required_conversion = _build_cad_to_usd_conversion(
            acct_num,
            acct,
            net_cash,
            usd_to_cad_rate,
            cad_buy_price,
            usd_sell_price,
            norberts_gambit_fee_cad,
        )
        if required_conversion is None:
            required_conversion = _build_usd_to_cad_conversion(
                acct_num,
                acct,
                net_cash,
                usd_to_cad_rate,
                usd_buy_price,
                cad_sell_price,
                norberts_gambit_fee_cad,
            )

        if required_conversion is not None:
            conversions.append(required_conversion)

    # ── Sweep: convert stranded foreign cash in single-currency accounts ──
    # Foreign cash is only "stranded" when every position in the account is
    # denominated in the other currency — making it structurally impossible
    # for the rebalancer to deploy the cash without FX conversion.
    #
    # Mixed-currency accounts (e.g. Margin accounts holding both IVV and
    # VSP.TO) are intentionally excluded: any foreign cash there is
    # deployable by the rebalancer's cascade logic (the "best available"
    # fallback will buy into an existing same-currency position when a
    # trade fires). Converting it here would cause an unnecessary round-trip.
    for acct in accounts:
        pos_currencies = {p.currency for p in acct.positions if p.quantity > 0}
        if not pos_currencies or len(pos_currencies) > 1:
            continue  # Skip empty and mixed-currency accounts (see note above)

        position_currency = next(iter(pos_currencies))

        # Remaining cash after trades
        impact = account_impact.get(acct.number)
        remaining_cash = net_account_cash(acct, impact)
        remaining_cad = remaining_cash.cad
        remaining_usd = remaining_cash.usd

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
