"""Spendable fixed-income budgets for portfolios with locked certificates."""

import math
from dataclasses import dataclass

from src.fx_math import to_cad
from src.portfolio import is_gic_group


@dataclass
class FixedIncomeBudget:
    total_cad: float
    # Feasible fixed weights as a percentage of the household.
    targets: dict[str, float]
    locked_excess_cad: float

    def room_cad(self, snapshot):
        invested_pct = sum(snapshot.allocations.get(s, 0.0) for s in self.targets)
        return max(0.0, self.total_cad - snapshot.total_value_cad * invested_pct / 100)

    def buy_limits(self, snapshot, holdings, rate):
        """Maximum whole units per symbol that fit the same remaining budget."""
        room = self.room_cad(snapshot)
        limits = {}
        for symbol in self.targets.keys() & holdings.keys():
            holding = holdings[symbol]
            # Cash leaves at ask and holdings enter the projection at the account mark.
            marks = [
                row.market_value / row.quantity
                for row in holding.accounts
                if row.quantity
            ]
            unit = max([holding.ask_price or holding.current_price, *marks])
            if unit > 0:
                limits[symbol] = math.floor(
                    room / to_cad(unit, holding.currency, rate) + 1e-9
                )
        return limits


def fixed_income_budget(portfolio, targets, composition, rate):
    """Reserve locked principal, then split the remainder using the configured weights."""
    locked = {}
    for holding in portfolio.holdings.values():
        group = holding.locked_bond_target
        if group:
            locked[group] = locked.get(group, 0.0) + holding.value_cad
    for symbol in composition:
        if is_gic_group(symbol):
            locked.setdefault(symbol, 0.0)
    if not composition or not locked or portfolio.total_value_cad <= 0:
        return None

    total = portfolio.total_value_cad * sum(targets[s] for s in composition) / 100
    budgets = dict(locked)
    weights = {s: w for s, w in composition.items() if s not in locked}
    # Manual redemptions cannot finance other trades in this run.
    floors = {}
    for acct in portfolio.accounts:
        for pos in acct.positions:
            if pos.symbol in weights and pos.symbol in acct.redemption_limits:
                floors[pos.symbol] = floors.get(pos.symbol, 0.0) + to_cad(
                    pos.market_value, pos.currency, rate
                )
    for symbol in list(weights):
        if not weights[symbol] or symbol not in portfolio.holdings:
            budgets[symbol] = floors.get(symbol, 0.0)
            del weights[symbol]
    while weights:
        remaining = max(0.0, total - sum(budgets.values()))
        weight_sum = sum(weights.values())
        proposed = {s: remaining * w / weight_sum for s, w in weights.items()}
        pinned = [s for s in weights if floors.get(s, 0.0) > proposed[s]]
        if not pinned:
            budgets.update(proposed)
            break
        for symbol in pinned:
            budgets[symbol] = floors[symbol]
            del weights[symbol]
    return FixedIncomeBudget(
        total,
        {s: 100 * value / portfolio.total_value_cad for s, value in budgets.items()},
        max(0.0, sum(locked.values()) + sum(floors.values()) - total),
    )
