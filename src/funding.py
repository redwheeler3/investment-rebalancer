"""Shared funding and currency-capacity helpers.

This module centralizes the conservative cash/conversion math used by both the
trade reconciler and the currency-conversion planner.
"""

import math
from dataclasses import dataclass


FUNDING_TOLERANCE = 0.01


@dataclass
class CurrencyTotals:
    """Simple CAD/USD totals container used for account cash math."""

    cad: float = 0.0
    usd: float = 0.0

    def add(self, currency: str, amount: float) -> None:
        if currency == "CAD":
            self.cad += amount
        elif currency == "USD":
            self.usd += amount
        else:
            raise ValueError(f"Unsupported currency: {currency}")


def build_account_trade_impacts(trades: list) -> dict:
    """Return per-account cash impacts where buys spend and sells raise cash."""
    impacts = {}
    for trade in trades:
        totals = impacts.setdefault(trade.account_number, CurrencyTotals())
        amount = trade.estimated_value if trade.action == "BUY" else -trade.estimated_value
        totals.add(trade.currency, amount)
    return impacts


def net_account_cash(account, impact: CurrencyTotals | None = None) -> CurrencyTotals:
    """Return remaining account cash after applying a trade impact."""
    impact = impact or CurrencyTotals()
    return CurrencyTotals(
        cad=account.cash_cad - impact.cad,
        usd=account.cash_usd - impact.usd,
    )


def max_usd_from_cad(
    cad_available: float,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
) -> float:
    """Maximum USD obtainable from available CAD under conservative assumptions."""
    usable_cad = max(0.0, cad_available - fee_cad)
    if usable_cad <= 0:
        return 0.0

    cad_buy_price = getattr(dlr_quotes, "cad_buy_price", 0.0) if dlr_quotes else 0.0
    usd_sell_price = getattr(dlr_quotes, "usd_sell_price", 0.0) if dlr_quotes else 0.0
    if cad_buy_price > 0 and usd_sell_price > 0:
        shares = int(math.floor(usable_cad / cad_buy_price))
        return shares * usd_sell_price

    return usable_cad / usd_to_cad_rate if usd_to_cad_rate > 0 else 0.0


def max_cad_from_usd(
    usd_available: float,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
) -> float:
    """Maximum net CAD obtainable from available USD under conservative assumptions."""
    if usd_available <= 0:
        return 0.0

    usd_buy_price = getattr(dlr_quotes, "usd_buy_price", 0.0) if dlr_quotes else 0.0
    cad_sell_price = getattr(dlr_quotes, "cad_sell_price", 0.0) if dlr_quotes else 0.0
    if usd_buy_price > 0 and cad_sell_price > 0:
        shares = int(math.floor(usd_available / usd_buy_price))
        gross_cad = shares * cad_sell_price
        return max(0.0, gross_cad - fee_cad)

    return max(0.0, usd_available * usd_to_cad_rate - fee_cad)


def cad_to_usd_conversion_for_target(
    cad_available: float,
    usd_target: float,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
) -> tuple[float, float]:
    """Return (CAD spent incl. fee, USD received) for a CAD->USD conversion."""
    cad_buy_price = getattr(dlr_quotes, "cad_buy_price", 0.0) if dlr_quotes else 0.0
    usd_sell_price = getattr(dlr_quotes, "usd_sell_price", 0.0) if dlr_quotes else 0.0

    if cad_buy_price > 0 and usd_sell_price > 0:
        shares_needed = int(math.ceil(usd_target / usd_sell_price))
        shares_affordable = int(math.floor(max(0.0, cad_available - fee_cad) / cad_buy_price))
        shares = min(shares_needed, shares_affordable)
        if shares <= 0:
            return 0.0, 0.0
        return shares * cad_buy_price + fee_cad, shares * usd_sell_price

    if usd_to_cad_rate <= 0 or cad_available <= fee_cad:
        return 0.0, 0.0

    spent_cad = min(cad_available, usd_target * usd_to_cad_rate + fee_cad)
    received_usd = max(0.0, (spent_cad - fee_cad) / usd_to_cad_rate)
    return spent_cad, received_usd


def usd_to_cad_conversion_for_target(
    usd_available: float,
    cad_target: float,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
) -> tuple[float, float]:
    """Return (USD spent, CAD received net of fee) for a USD->CAD conversion."""
    usd_buy_price = getattr(dlr_quotes, "usd_buy_price", 0.0) if dlr_quotes else 0.0
    cad_sell_price = getattr(dlr_quotes, "cad_sell_price", 0.0) if dlr_quotes else 0.0

    if usd_buy_price > 0 and cad_sell_price > 0:
        shares_needed = int(math.ceil((cad_target + fee_cad) / cad_sell_price))
        shares_affordable = int(math.floor(usd_available / usd_buy_price))
        shares = min(shares_needed, shares_affordable)
        if shares <= 0:
            return 0.0, 0.0
        return shares * usd_buy_price, max(0.0, shares * cad_sell_price - fee_cad)

    if usd_to_cad_rate <= 0 or usd_available <= 0:
        return 0.0, 0.0

    usd_needed = min(usd_available, (cad_target + fee_cad) / usd_to_cad_rate)
    received_cad = max(0.0, usd_needed * usd_to_cad_rate - fee_cad)
    return usd_needed, received_cad


def settle_net_cash_after_conversion(
    net_cash: CurrencyTotals,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
    tolerance: float = FUNDING_TOLERANCE,
) -> CurrencyTotals:
    """Normalise CAD/USD cash after satisfying at most one currency deficit."""
    cad = net_cash.cad
    usd = net_cash.usd

    if cad >= -tolerance and usd >= -tolerance:
        return CurrencyTotals(cad=max(0.0, cad), usd=max(0.0, usd))

    if usd < -tolerance and cad > tolerance:
        spent_cad, received_usd = cad_to_usd_conversion_for_target(
            cad,
            abs(usd),
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes=dlr_quotes,
        )
        return CurrencyTotals(
            cad=max(0.0, cad - spent_cad),
            usd=max(0.0, usd + received_usd),
        )

    if cad < -tolerance and usd > tolerance:
        spent_usd, received_cad = usd_to_cad_conversion_for_target(
            usd,
            abs(cad),
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes=dlr_quotes,
        )
        return CurrencyTotals(
            cad=max(0.0, cad + received_cad),
            usd=max(0.0, usd - spent_usd),
        )

    return CurrencyTotals(cad=max(0.0, cad), usd=max(0.0, usd))


def cross_currency_buying_power(
    source_cash: float,
    source_currency: str,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
) -> float:
    """Return conservative target-currency buying power from one source balance."""
    if source_currency == "CAD":
        return max_usd_from_cad(source_cash, usd_to_cad_rate, fee_cad, dlr_quotes)
    if source_currency == "USD":
        return max_cad_from_usd(source_cash, usd_to_cad_rate, fee_cad, dlr_quotes)
    raise ValueError(f"Unsupported source currency: {source_currency}")


def consume_cross_currency_cash(
    cash_by_currency: dict[str, float],
    source_currency: str,
    target_currency: str,
    cost_native: float,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
) -> None:
    """Apply a cross-currency buy to a cash map conservatively."""
    if source_currency == "CAD" and target_currency == "USD":
        spent_cad, received_usd = cad_to_usd_conversion_for_target(
            cash_by_currency.get("CAD", 0.0),
            cost_native,
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes=dlr_quotes,
        )
        cash_by_currency["CAD"] = max(0.0, cash_by_currency.get("CAD", 0.0) - spent_cad)
        cash_by_currency["USD"] = max(0.0, cash_by_currency.get("USD", 0.0) + received_usd - cost_native)
        return

    if source_currency == "USD" and target_currency == "CAD":
        spent_usd, received_cad = usd_to_cad_conversion_for_target(
            cash_by_currency.get("USD", 0.0),
            cost_native,
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes=dlr_quotes,
        )
        cash_by_currency["USD"] = max(0.0, cash_by_currency.get("USD", 0.0) - spent_usd)
        cash_by_currency["CAD"] = max(0.0, cash_by_currency.get("CAD", 0.0) + received_cad - cost_native)
        return

    raise ValueError(f"Unsupported conversion path: {source_currency} -> {target_currency}")


def can_fund_net_cash_requirement(
    net_cash: CurrencyTotals,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
    tolerance: float = FUNDING_TOLERANCE,
) -> bool:
    """Whether an account can fund its net needs with at most one conversion."""
    if net_cash.cad >= -tolerance and net_cash.usd >= -tolerance:
        return True

    if net_cash.usd < -tolerance and net_cash.cad > tolerance:
        return cross_currency_buying_power(
            net_cash.cad,
            "CAD",
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes,
        ) + tolerance >= abs(net_cash.usd)

    if net_cash.cad < -tolerance and net_cash.usd > tolerance:
        return cross_currency_buying_power(
            net_cash.usd,
            "USD",
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes,
        ) + tolerance >= abs(net_cash.cad)

    return False
