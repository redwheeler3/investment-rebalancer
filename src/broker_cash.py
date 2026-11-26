"""CAD cash movements by broker day for comparison with a linked account's cash.

Trades affect cash on the fill date. Activities can arrive later with a different
transaction date and can consolidate multiple executions into one posting.
"""

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo


BROKER_ZONE = ZoneInfo("America/Toronto")


def broker_day(timestamp):
    stamp = datetime.fromisoformat(timestamp)
    if stamp.utcoffset() is None:
        raise ValueError("Missing broker cash timestamp timezone.")
    return stamp.astimezone(BROKER_ZONE).date()


def cash_by_day(activities, executions, currencies):
    """Use posted trade net amounts once they cover the fills for that day/side."""
    daily = defaultdict(Decimal)
    posted = {}
    filled = {}
    symbol_ids = {fill["symbol"]: fill["symbolId"] for fill in executions}
    for row in activities:
        amount = Decimal(str(row["netAmount"]))
        if row["type"].casefold() == "trades":
            day = broker_day(row["tradeDate"])
            symbol = row.get("symbol")
            security = row.get("symbolId") or symbol_ids.get(symbol, symbol)
            key = (day, security, row["action"].lower())
            qty, net = posted.get(key, (0, 0))
            posted[key] = qty + abs(Decimal(str(row["quantity"]))), net + amount
        else:
            daily[broker_day(row["transactionDate"])] += amount

    for fill in executions:
        if currencies[fill["symbolId"]] != "CAD":
            continue
        side = fill["side"].lower()
        if side not in {"buy", "sell"}:
            raise ValueError(f"Unsupported execution side for a long position: {side}")
        gross = abs(Decimal(str(fill["totalCost"])))
        fees = sum(
            Decimal(str(fill.get(k) or 0))
            for k in (
                "commission",
                "orderPlacementCommission",
                "executionFee",
                "secFee",
                "canadianExecutionFee",
            )
        )
        key = (broker_day(fill["timestamp"]), fill["symbolId"], side)
        qty, net = filled.get(key, (0, 0))
        filled[key] = (
            qty + abs(Decimal(str(fill["quantity"]))),
            net + (gross if side == "sell" else -gross) - fees,
        )

    for key in posted.keys() | filled.keys():
        posted_qty, posted_net = posted.get(key, (0, 0))
        fill_qty, fill_net = filled.get(key, (0, 0))
        daily[key[0]] += posted_net if posted_qty >= fill_qty else fill_net
    return daily
