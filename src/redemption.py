"""Fee-free redemption eligibility from posted broker activity and verified lots."""

from calendar import monthrange
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests


@dataclass(frozen=True)
class RedemptionRule:
    minimum_years: int | None  # None means never recommend a redemption.


def parse_redemption_rules(raw: dict) -> dict[str, RedemptionRule]:
    rules = {}
    if not isinstance(raw, dict):
        raise ValueError("redemption_rules must map symbols to redemption policies")
    for symbol, policy in raw.items():
        if policy == "never":
            rules[symbol] = RedemptionRule(None)
        elif (
            isinstance(policy, dict)
            and set(policy) == {"minimum_years"}
            and type(policy["minimum_years"]) is int
            and policy["minimum_years"] > 0
        ):
            rules[symbol] = RedemptionRule(policy["minimum_years"])
        else:
            raise ValueError(f"Invalid redemption rule for {symbol}")
    return rules


@dataclass(frozen=True)
class RedemptionLimit:
    units: int
    reason: str
    next_eligible: date | None = None


def _remaining_lots(buys, sold):
    """Charge prior sales against the oldest lots to give a conservative mature balance."""
    remaining = {}
    for acquired, units in sorted(buys.items()):
        consumed = min(units, sold)
        sold -= consumed
        if units > consumed:
            remaining[acquired] = units - consumed
    if sold:
        raise ValueError("redemptions exceed verified purchases")
    return remaining


def _movements(rows, position):
    buys = Counter()
    sold = 0
    for row in rows:
        if row.get("symbol") != position.symbol and not (
            position.symbol_id and row.get("symbolId") == position.symbol_id
        ):
            continue
        qty = float(row["quantity"])
        if row["type"] == "Dividends" and qty == 0:
            continue
        if row["type"] != "Trades" or not qty.is_integer() or qty == 0:
            raise ValueError("unrecognized unit movement")
        if row["action"] == "Buy" and qty > 0:
            acquired = max(
                date.fromisoformat(row[key][:10])
                for key in ("tradeDate", "settlementDate")
            )
            buys[acquired.isoformat()] += int(qty)
        elif row["action"] == "Sell":
            sold += abs(int(qty))
        else:
            raise ValueError("unrecognized trade action")
    return buys, sold


def _verified_lots(client, position, now, cache):
    """Walk backwards until posted units explain the holding or meet a verified checkpoint.

    Save lots through the day before the newest window. Re-reading that window
    picks up posting changes and avoids caching an incomplete trading day.
    """
    today = now.date()
    month_index = today.year * 12 + today.month - 1 - 16
    year, month = divmod(month_index, 12)
    floor = date(year, month + 1, min(today.day, monthrange(year, month + 1)[1]))
    key = f"{position.account_number}:{position.symbol}"
    previous = cache.get(key)
    checkpoint = today - timedelta(days=30)
    history = Counter()
    if previous:
        through = date.fromisoformat(previous["through"])
        if floor <= through <= checkpoint:
            floor = through + timedelta(days=1)
            for day, units in previous["lots"].items():
                date.fromisoformat(day)
                if type(units) is not int or units <= 0:
                    raise ValueError("invalid stored units")
                history[day] = units
        else:
            previous = None

    recent = Counter()
    recent_sold = older_sold = 0
    last = today
    while last >= floor:
        first = max(floor, last - timedelta(days=29))
        rows = client.get_activities(
            position.account_number,
            datetime.combine(first, time.min, now.tzinfo).isoformat(),
            min(now, datetime.combine(last, time.max, now.tzinfo)).isoformat(),
        )
        buys, sold = _movements(rows, position)
        if last == today:
            recent, recent_sold = buys, sold
        else:
            history.update(buys)
            older_sold += sold

        # A saved balance is usable only after replaying the entire interval
        # between its date and today, even when an intermediate net happens to match.
        if not previous or first == floor:
            quantity = history.total() + recent.total() - older_sold - recent_sold
            if quantity == position.quantity:
                older_lots = _remaining_lots(history, older_sold)
                combined = Counter(older_lots) + recent
                lots = _remaining_lots(combined, recent_sold)
                cache[key] = {"through": checkpoint.isoformat(), "lots": older_lots}
                return lots
        last = first - timedelta(days=1)
    raise ValueError("unit history could not be verified")


def account_redemption_limits(client, positions, rules, now=None, cache=None):
    """Resolve account-level limits. Unverifiable units never fund a redemption."""
    limits = {}
    if cache is None:
        cache = {}
    for position in positions:
        rule = rules.get(position.symbol)
        if rule is None:
            continue
        if rule.minimum_years is None:
            limits[position.symbol] = RedemptionLimit(0, "redemption disabled")
            continue
        eastern = ZoneInfo("America/Toronto")
        as_of = (now or datetime.now(eastern)).astimezone(eastern)
        try:
            lots = _verified_lots(client, position, as_of, cache)
            mature = 0
            next_date = None
            for day, units in sorted(lots.items()):
                acquired = date.fromisoformat(day)
                year = acquired.year + rule.minimum_years
                # Feb 29 reaches its anniversary on March 1 in a non-leap year.
                try:
                    anniversary = acquired.replace(year=year)
                except ValueError:
                    anniversary = date(year, 3, 1)
                if anniversary <= as_of.date():
                    mature += units
                elif next_date is None:
                    next_date = anniversary
            limits[position.symbol] = RedemptionLimit(
                mature, f"{rule.minimum_years}-year hold", next_date
            )
        except requests.RequestException:
            limits[position.symbol] = RedemptionLimit(0, "activity lookup unavailable")
        except (ValueError, KeyError, TypeError, OverflowError):
            limits[position.symbol] = RedemptionLimit(
                0, "unit history could not be verified"
            )
    return limits
