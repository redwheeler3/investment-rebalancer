"""Portfolio rebalancer — trade planning decisions.

This is the single home for rebalancing logic. It decides what trades to make
based on the household rules documented in the README:

- measure drift at the unified household-portfolio level,
- use the drift threshold to suppress small *starter* trades,
- sell overweight symbols and buy underweight symbols,
- only buy symbols that already exist in the destination account,
- prefer same-currency deployment before cross-currency deployment,
- minimize stranded cash after meaningful trades have started,
- and allow account-constrained cash deployment (buy the best available symbol
  in an account to avoid leaving cash idle, even if that symbol is not globally
  underweight).

The planner runs in iterative rounds with two layers per round:

1. starter trades that react to materially overweight / underweight symbols, and
2. residual cash deployment that spends remaining cash on the best available
   holdings in each account.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from src.fx_math import consume_cross_currency_cash, cross_currency_buying_power, to_cad
from src.portfolio import get_current_allocations, get_drifts, get_holdings_view, simulate_rebalance
from src.cash_deploy import (
    build_cross_currency_buy,
    build_same_currency_buy,
)
from src.models import TradeRecommendation

# Maximum optimisation rounds before stopping
MAX_ROUNDS = 10


# ══════════════════════════════════════════════════════════════════
# Trade netting
# ══════════════════════════════════════════════════════════════════


def net_trades(all_trades: list) -> list:
    """Net buys and sells for the same (symbol, account) into a single trade."""
    position_map = {}  # (symbol, account_number) -> list of trades
    for trade in all_trades:
        key = (trade.symbol, trade.account_number)
        position_map.setdefault(key, []).append(trade)

    final_trades = []
    for (symbol, account_number), trades_list in position_map.items():
        total_buy_qty = 0
        total_sell_qty = 0
        buy_price = 0
        sell_price = 0
        template = trades_list[0]
        buy_note = ""
        sell_note = ""
        buy_requires_fx = False

        for trade in trades_list:
            if trade.action == "BUY":
                total_buy_qty += trade.quantity
                buy_price = trade.price
                if trade.note and not buy_note:
                    buy_note = trade.note
                if trade.requires_fx:
                    buy_requires_fx = True
            else:
                total_sell_qty += trade.quantity
                sell_price = trade.price
                if trade.note and not sell_note:
                    sell_note = trade.note

        net_quantity = total_buy_qty - total_sell_qty
        if net_quantity > 0:
            price = buy_price if buy_price > 0 else template.price
            final_trades.append(TradeRecommendation(
                symbol=symbol,
                action="BUY",
                quantity=net_quantity,
                account_number=account_number,
                account_type=template.account_type,
                owner=template.owner,
                price=price,
                currency=template.currency,
                estimated_value=price * net_quantity,
                note=buy_note,
                requires_fx=buy_requires_fx,
            ))
        elif net_quantity < 0:
            price = sell_price if sell_price > 0 else template.price
            final_trades.append(TradeRecommendation(
                symbol=symbol,
                action="SELL",
                quantity=abs(net_quantity),
                account_number=account_number,
                account_type=template.account_type,
                owner=template.owner,
                price=price,
                currency=template.currency,
                estimated_value=price * abs(net_quantity),
                note=sell_note,
            ))

    return final_trades


# ══════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════


def calculate_trades(
    portfolio,
    targets: dict,
    usd_to_cad_rate: float,
    norberts_gambit_fee_cad: float,
    drift_trade_threshold_pct: float,
    transient_symbols: set,
    dlr_quotes,
) -> list:
    """Calculate rebalancing trades for the portfolio."""
    if portfolio.total_value_cad == 0:
        return []

    planner = RebalancePlanner(
        portfolio=portfolio,
        targets=targets,
        usd_to_cad_rate=usd_to_cad_rate,
        fee_cad=norberts_gambit_fee_cad,
        drift_trade_threshold_pct=drift_trade_threshold_pct,
        hidden_symbols=transient_symbols,
        dlr_quotes=dlr_quotes,
    )
    return planner.build()


# ══════════════════════════════════════════════════════════════════
# Planning state
# ══════════════════════════════════════════════════════════════════


@dataclass
class TradePlan:
    """Mutable trade plan with cached netting and projected drift information."""

    portfolio: object
    targets: dict[str, float]
    usd_to_cad_rate: float
    hidden_symbols: set[str]
    trades: list[TradeRecommendation] = field(default_factory=list)
    _netted_cache: list[TradeRecommendation] | None = field(default=None, init=False, repr=False)
    _snapshot_cache: object | None = field(default=None, init=False, repr=False)

    def add_trade(self, trade: TradeRecommendation) -> None:
        self.trades.append(trade)
        self._invalidate()

    def netted_trades(self) -> list[TradeRecommendation]:
        if self._netted_cache is None:
            self._netted_cache = net_trades(self.trades)
        return list(self._netted_cache)

    def projected_snapshot(self):
        if self._snapshot_cache is None:
            self._snapshot_cache = simulate_rebalance(
                self.portfolio,
                self.netted_trades(),
                self.targets,
                self.usd_to_cad_rate,
                hidden_symbols=self.hidden_symbols,
            )
        return self._snapshot_cache

    def drifts(self) -> dict[str, float]:
        return dict(self.projected_snapshot().drifts)

    def position_deltas(self) -> dict[tuple[str, str], int]:
        deltas: dict[tuple[str, str], int] = {}
        for trade in self.trades:
            key = (trade.account_number, trade.symbol)
            delta = trade.quantity if trade.action == "BUY" else -trade.quantity
            deltas[key] = deltas.get(key, 0) + delta
        return deltas

    def effective_qty(self, account, symbol: str) -> int:
        original = int(get_position_quantity(account, symbol))
        delta = self.position_deltas().get((account.number, symbol), 0)
        return max(0, original + delta)

    def _invalidate(self) -> None:
        self._netted_cache = None
        self._snapshot_cache = None


@dataclass
class CashLedger:
    """Per-account cash state used while planning trades."""

    usd_to_cad_rate: float
    fee_cad: float
    balances: dict[str, dict[str, float]]

    @classmethod
    def from_portfolio(cls, portfolio, usd_to_cad_rate: float, fee_cad: float):
        balances = {
            acct.number: {"CAD": acct.cash_cad, "USD": acct.cash_usd}
            for acct in portfolio.accounts
        }
        return cls(usd_to_cad_rate=usd_to_cad_rate, fee_cad=fee_cad, balances=balances)

    def cash(self, account_number: str, currency: str) -> float:
        return max(0.0, self.balances.get(account_number, {}).get(currency, 0.0))

    def same_currency_buying_power(self, account_number: str, currency: str) -> float:
        return self.cash(account_number, currency)

    def total_buying_power(self, account_number: str, buy_currency: str, dlr_quotes=None) -> float:
        native = self.cash(account_number, buy_currency)
        source_currency = "CAD" if buy_currency == "USD" else "USD"
        convertible = cross_currency_buying_power(
            self.cash(account_number, source_currency),
            source_currency,
            self.usd_to_cad_rate,
            self.fee_cad,
            dlr_quotes=dlr_quotes,
        )
        return native + convertible

    def credit_sale(self, account_number: str, currency: str, proceeds_native: float) -> None:
        self.balances.setdefault(account_number, {"CAD": 0.0, "USD": 0.0})
        self.balances[account_number][currency] += proceeds_native

    def fund_buy(self, account_number: str, currency: str, cost_native: float, dlr_quotes=None) -> bool:
        """Spend native cash first, then convert the remainder if needed."""
        self.balances.setdefault(account_number, {"CAD": 0.0, "USD": 0.0})
        native_cash = self.cash(account_number, currency)
        if native_cash >= cost_native:
            self.balances[account_number][currency] -= cost_native
            return False

        remainder_native = cost_native - native_cash
        self.balances[account_number][currency] = 0.0
        source_currency = "CAD" if currency == "USD" else "USD"
        consume_cross_currency_cash(
            self.balances[account_number],
            source_currency,
            currency,
            remainder_native,
            self.usd_to_cad_rate,
            self.fee_cad,
            dlr_quotes=dlr_quotes,
        )
        return True


# ══════════════════════════════════════════════════════════════════
# Planner engine
# ══════════════════════════════════════════════════════════════════


@dataclass
class RebalancePlanner:
    """Readable rebalance planner built around explicit objectives and phases."""

    portfolio: object
    targets: dict[str, float]
    usd_to_cad_rate: float
    fee_cad: float
    drift_trade_threshold_pct: float
    hidden_symbols: set[str] = field(default_factory=set)
    dlr_quotes: object | None = None
    holdings_view: dict = field(init=False)
    initial_drifts: dict[str, float] = field(init=False)
    plan: TradePlan = field(init=False)
    ledger: CashLedger = field(init=False)

    def __post_init__(self) -> None:
        current_alloc = get_current_allocations(
            self.portfolio,
            self.usd_to_cad_rate,
            excluded_symbols=self.hidden_symbols,
        )
        self.initial_drifts = get_drifts(current_alloc, self.targets)
        self.holdings_view = get_holdings_view(self.portfolio, self.hidden_symbols)
        self.plan = TradePlan(
            portfolio=self.portfolio,
            targets=self.targets,
            usd_to_cad_rate=self.usd_to_cad_rate,
            hidden_symbols=self.hidden_symbols,
        )
        self.ledger = CashLedger.from_portfolio(
            self.portfolio,
            self.usd_to_cad_rate,
            self.fee_cad,
        )

    def build(self) -> list:
        """Run the planner phases and return the final recommended trades."""
        for _ in range(MAX_ROUNDS):
            starter_changes = 0
            starter_changes += self._sell_overweight_starters()
            starter_changes += self._buy_underweight_starters()

            if starter_changes > 0:
                self._deploy_residual_cash()

            if starter_changes == 0:
                break

        return self.plan.netted_trades()

    def _sell_overweight_starters(self) -> int:
        count = 0
        drifts = self.plan.drifts() if self.plan.trades else dict(self.initial_drifts)
        overweight_symbols = [
            (symbol, drift_pct)
            for symbol, drift_pct in drifts.items()
            if symbol not in ("CAD", "USD") and drift_pct > self.drift_trade_threshold_pct
        ]
        overweight_symbols.sort(key=lambda item: item[1], reverse=True)

        for symbol, _ in overweight_symbols:
            current_drift = self.plan.drifts().get(symbol, drifts.get(symbol, 0.0))
            if current_drift <= self.drift_trade_threshold_pct:
                continue

            holding = self.holdings_view.get(symbol)
            if not holding:
                continue

            bid_price_native = holding.bid_price or holding.current_price
            currency = holding.currency
            if bid_price_native <= 0:
                continue

            shares = shares_for_drift_gap(
                self.portfolio.total_value_cad,
                current_drift,
                bid_price_native,
                currency,
                self.usd_to_cad_rate,
            )
            if shares <= 0:
                continue

            sell_trades = allocate_sell(
                symbol,
                shares,
                bid_price_native,
                currency,
                self.portfolio.accounts,
                effective_drift=self.plan.drifts(),
                transient_symbols=self.hidden_symbols,
                drift_trade_threshold_pct=self.drift_trade_threshold_pct,
                position_deltas=self.plan.position_deltas(),
            )
            for trade in sell_trades:
                self.ledger.credit_sale(trade.account_number, currency, trade.estimated_value)
                self.plan.add_trade(trade)
                count += 1

        return count

    def _buy_underweight_starters(self) -> int:
        count = 0
        drifts = self.plan.drifts() if self.plan.trades else dict(self.initial_drifts)
        underweight_symbols = [
            (symbol, drift_pct)
            for symbol, drift_pct in drifts.items()
            if symbol not in ("CAD", "USD") and drift_pct < -self.drift_trade_threshold_pct
        ]
        underweight_symbols.sort(key=lambda item: item[1])

        for symbol, _ in underweight_symbols:
            count += self._buy_symbol_toward_target(symbol)

        return count

    def _buy_symbol_toward_target(self, symbol: str) -> int:
        drift_pct = self.plan.drifts().get(symbol, self.initial_drifts.get(symbol, 0.0))
        if drift_pct >= -self.drift_trade_threshold_pct:
            return 0

        holding = self.holdings_view.get(symbol)
        if not holding:
            return 0

        ask_price_native = holding.ask_price or holding.current_price
        currency = holding.currency
        if ask_price_native <= 0:
            return 0

        shares_needed = shares_for_drift_gap(
            self.portfolio.total_value_cad,
            drift_pct,
            ask_price_native,
            currency,
            self.usd_to_cad_rate,
        )
        if shares_needed <= 0:
            return 0

        eligible_accounts = self._eligible_buy_accounts(symbol, currency)
        if not eligible_accounts:
            return 0

        remaining = shares_needed
        trades_added = 0
        for acct in eligible_accounts:
            if remaining <= 0:
                break

            quantity = self._buy_in_account(acct, symbol, ask_price_native, currency, remaining)
            if quantity > 0:
                remaining -= quantity
                trades_added += 1

        return trades_added

    def _eligible_buy_accounts(self, symbol: str, currency: str) -> list:
        accounts = find_accounts_for_symbol(symbol, self.portfolio.accounts)

        def sort_key(account):
            same_currency = self.ledger.same_currency_buying_power(account.number, currency)
            total = self.ledger.total_buying_power(account.number, currency, dlr_quotes=self.dlr_quotes)
            return (
                1 if same_currency > 0 else 0,
                same_currency,
                total,
            )

        accounts.sort(key=sort_key, reverse=True)
        return accounts

    def _buy_in_account(
        self,
        acct,
        symbol: str,
        ask_price_native: float,
        currency: str,
        remaining_shares: int,
    ) -> int:
        buying_power = self.ledger.total_buying_power(
            acct.number,
            currency,
            dlr_quotes=self.dlr_quotes,
        )
        if buying_power < ask_price_native:
            self._raise_cash_in_account(acct, symbol, currency, ask_price_native)
            buying_power = self.ledger.total_buying_power(
                acct.number,
                currency,
                dlr_quotes=self.dlr_quotes,
            )

        affordable = int(math.floor(buying_power / ask_price_native))
        quantity = min(remaining_shares, affordable)
        if quantity <= 0:
            return 0

        cost_native = quantity * ask_price_native
        converted = self.ledger.fund_buy(
            acct.number,
            currency,
            cost_native,
            dlr_quotes=self.dlr_quotes,
        )
        self.plan.add_trade(TradeRecommendation(
            symbol=symbol,
            action="BUY",
            quantity=quantity,
            account_number=acct.number,
            account_type=acct.account_type,
            owner=acct.owner,
            price=ask_price_native,
            currency=currency,
            estimated_value=cost_native,
            note="Underweight buy (requires FX)" if converted else "Underweight buy",
            requires_fx=converted,
        ))
        return quantity

    def _raise_cash_in_account(self, acct, buy_symbol: str, currency: str, minimum_needed_native: float) -> None:
        """Raise same-currency cash by selling other overweight holdings in the account."""
        drifts = self.plan.drifts()
        candidates = []

        for pos in acct.positions:
            if pos.symbol == buy_symbol or pos.symbol in self.hidden_symbols:
                continue
            if pos.quantity <= 0 or pos.currency != currency:
                continue

            drift_pct = drifts.get(pos.symbol, 0.0)
            if drift_pct <= 0:
                continue

            holding = self.holdings_view.get(pos.symbol)
            if not holding:
                continue

            bid_price_native = holding.bid_price or pos.current_price
            if bid_price_native <= 0:
                continue

            max_sellable = min(
                self.plan.effective_qty(acct, pos.symbol),
                max_sellable_without_crossing_target(
                    self.portfolio.total_value_cad,
                    drift_pct,
                    bid_price_native,
                    currency,
                    self.usd_to_cad_rate,
                ),
            )
            if max_sellable <= 0:
                continue

            candidates.append((drift_pct, pos.symbol, bid_price_native, max_sellable))

        candidates.sort(key=lambda item: item[0], reverse=True)
        for _drift_pct, symbol, bid_price_native, max_sellable in candidates:
            current_cash = self.ledger.same_currency_buying_power(acct.number, currency)
            shortfall = minimum_needed_native - current_cash
            if shortfall <= 0:
                break

            sell_qty = min(max_sellable, int(math.ceil(shortfall / bid_price_native)))
            if sell_qty <= 0:
                continue

            trade = TradeRecommendation(
                symbol=symbol,
                action="SELL",
                quantity=sell_qty,
                account_number=acct.number,
                account_type=acct.account_type,
                owner=acct.owner,
                price=bid_price_native,
                currency=currency,
                estimated_value=bid_price_native * sell_qty,
                note="Funding sell",
            )
            self.ledger.credit_sale(acct.number, currency, trade.estimated_value)
            self.plan.add_trade(trade)

    def _account_buyable_candidates(
        self,
        acct,
        currency: str,
        drifts: dict[str, float],
        *,
        underweight_only: bool,
    ) -> list[tuple[float, str, float]]:
        """Return account-local buyable symbols sorted by lowest drift first.

        When ``underweight_only`` is false, this becomes the cash-minimizing
        fallback that prefers the least overweight / most underweight symbol the
        account is actually allowed to hold.
        """
        candidates = []
        seen = set()

        for pos in acct.positions:
            if pos.quantity <= 0 or pos.currency != currency:
                continue
            if pos.symbol in self.hidden_symbols or pos.symbol in seen:
                continue
            if self.targets.get(pos.symbol, 0.0) <= 0:
                continue

            drift_pct = drifts.get(pos.symbol, 0.0)
            if underweight_only and drift_pct >= 0:
                continue

            holding = self.holdings_view.get(pos.symbol)
            if not holding:
                continue

            ask_price_native = holding.ask_price or pos.current_price
            if ask_price_native <= 0:
                continue

            seen.add(pos.symbol)
            candidates.append((drift_pct, pos.symbol, ask_price_native))

        candidates.sort(key=lambda item: item[0])
        return candidates

    def _build_cash_minimizing_same_currency_buy(self, acct, currency: str, drifts: dict[str, float]):
        """Spend same-currency cash on the best buyable symbol in the account."""
        available_cash = self.ledger.same_currency_buying_power(acct.number, currency)
        if available_cash <= 0:
            return None

        candidates = self._account_buyable_candidates(
            acct,
            currency,
            drifts,
            underweight_only=False,
        )
        if not candidates:
            return None

        _drift_pct, symbol, ask_price_native = candidates[0]
        quantity = int(math.floor(available_cash / ask_price_native))
        if quantity <= 0:
            return None

        cost_native = quantity * ask_price_native
        self.ledger.balances[acct.number][currency] -= cost_native
        return TradeRecommendation(
            symbol=symbol,
            action="BUY",
            quantity=quantity,
            account_number=acct.number,
            account_type=acct.account_type,
            owner=acct.owner,
            price=ask_price_native,
            currency=currency,
            estimated_value=cost_native,
            note="Best available buy",
        )

    def _build_cash_minimizing_cross_currency_buy(self, acct, source_currency: str, drifts: dict[str, float]):
        """Convert remaining cash and spend it on the best buyable symbol."""
        target_currency = "USD" if source_currency == "CAD" else "CAD"
        buying_power = cross_currency_buying_power(
            self.ledger.cash(acct.number, source_currency),
            source_currency,
            self.usd_to_cad_rate,
            self.fee_cad,
            dlr_quotes=self.dlr_quotes,
        )
        if buying_power <= 0:
            return None

        candidates = self._account_buyable_candidates(
            acct,
            target_currency,
            drifts,
            underweight_only=False,
        )
        if not candidates:
            return None

        _drift_pct, symbol, ask_price_native = candidates[0]
        quantity = int(math.floor(buying_power / ask_price_native))
        if quantity <= 0:
            return None

        cost_native = quantity * ask_price_native
        converted = self.ledger.fund_buy(
            acct.number,
            target_currency,
            cost_native,
            dlr_quotes=self.dlr_quotes,
        )
        return TradeRecommendation(
            symbol=symbol,
            action="BUY",
            quantity=quantity,
            account_number=acct.number,
            account_type=acct.account_type,
            owner=acct.owner,
            price=ask_price_native,
            currency=target_currency,
            estimated_value=cost_native,
            note="Best available buy (requires FX)" if converted else "Best available buy",
            requires_fx=converted,
        )

    def _deploy_residual_cash(self) -> None:
        """Minimize stranded cash after starter trades, same-currency first."""
        while True:
            made_trade = False
            projected_drifts = self.plan.drifts()

            for acct in self.portfolio.accounts:
                for currency in ("CAD", "USD"):
                    while True:
                        trade = build_same_currency_buy(
                            acct,
                            self.ledger.balances,
                            self.holdings_view,
                            projected_drifts,
                            self.hidden_symbols,
                            self.portfolio.total_value_cad,
                            self.usd_to_cad_rate,
                            currency,
                            0.0,
                            note="Leftover cash buy",
                        )
                        if trade is None:
                            trade = self._build_cash_minimizing_same_currency_buy(
                                acct,
                                currency,
                                projected_drifts,
                            )
                        if trade is None:
                            break

                        self.plan.add_trade(trade)
                        made_trade = True
                        projected_drifts = self.plan.drifts()

                for source_currency in ("CAD", "USD"):
                    while True:
                        trade = build_cross_currency_buy(
                            acct,
                            self.ledger.balances,
                            self.holdings_view,
                            projected_drifts,
                            self.hidden_symbols,
                            self.portfolio.total_value_cad,
                            self.usd_to_cad_rate,
                            source_currency,
                            self.fee_cad,
                            0.0,
                            note="Leftover cash buy (requires FX)",
                            dlr_quotes=self.dlr_quotes,
                        )
                        if trade is None:
                            trade = self._build_cash_minimizing_cross_currency_buy(
                                acct,
                                source_currency,
                                projected_drifts,
                            )
                        if trade is None:
                            break

                        self.plan.add_trade(trade)
                        made_trade = True
                        projected_drifts = self.plan.drifts()

            if not made_trade:
                break


# ══════════════════════════════════════════════════════════════════
# Sizing math
# ══════════════════════════════════════════════════════════════════


def shares_for_drift_gap(
    total_value_cad: float,
    drift_pct: float,
    price_native: float,
    currency: str,
    usd_to_cad_rate: float,
) -> int:
    """Return whole shares needed to reduce a drift gap, using whole-share math."""
    if total_value_cad <= 0 or price_native <= 0:
        return 0

    gap_cad = abs(drift_pct / 100.0) * total_value_cad
    gap_native = gap_cad / usd_to_cad_rate if currency == "USD" else gap_cad
    shares = int(math.floor(gap_native / price_native))
    if shares == 0:
        one_share_cad = to_cad(price_native, currency, usd_to_cad_rate)
        if one_share_cad < 2 * gap_cad:
            shares = 1
    return max(0, shares)


def max_sellable_without_crossing_target(
    total_value_cad: float,
    drift_pct: float,
    price_native: float,
    currency: str,
    usd_to_cad_rate: float,
) -> int:
    """Maximum whole shares sellable before a positive drift turns negative."""
    if drift_pct <= 0 or total_value_cad <= 0 or price_native <= 0:
        return 0

    per_share_drift_pct = (
        to_cad(price_native, currency, usd_to_cad_rate) / total_value_cad
    ) * 100.0
    if per_share_drift_pct <= 0:
        return 0

    return int(math.floor((drift_pct + 1e-9) / per_share_drift_pct))


# ══════════════════════════════════════════════════════════════════
# Sell allocation — decides which accounts to sell from
# ══════════════════════════════════════════════════════════════════


def find_accounts_for_symbol(symbol: str, accounts: list) -> list:
    """Find all accounts that currently hold a given symbol."""
    matching = []
    for acct in accounts:
        for pos in acct.positions:
            if pos.symbol == symbol and pos.quantity > 0:
                matching.append(acct)
                break
    return matching


def get_position_quantity(account, symbol: str) -> float:
    """Get the quantity of a symbol held in an account."""
    for pos in account.positions:
        if pos.symbol == symbol:
            return pos.quantity
    return 0.0


def _effective_qty(account, symbol: str, position_deltas: dict) -> int:
    """Get the effective quantity accounting for trades already planned."""
    original = int(get_position_quantity(account, symbol))
    delta = position_deltas.get((account.number, symbol), 0)
    return max(0, original + delta)


def _has_underweight_alternatives(
    acct,
    sell_symbol: str,
    effective_drift: dict,
    transient_symbols: set,
    drift_trade_threshold_pct: float,
) -> bool:
    """Check if proceeds from selling can be redeployed into underweight positions."""
    for pos in acct.positions:
        if pos.symbol == sell_symbol or pos.quantity <= 0:
            continue
        if pos.symbol in transient_symbols:
            continue
        if effective_drift.get(pos.symbol, 0) < -drift_trade_threshold_pct:
            return True
    return False


def allocate_sell(
    symbol: str,
    total_shares: int,
    price: float,
    currency: str,
    accounts: list,
    effective_drift: dict,
    transient_symbols: set,
    drift_trade_threshold_pct: float,
    position_deltas: dict,
) -> list:
    """Allocate a SELL order across accounts.

    Strategy:
    - Only sell from accounts that hold the symbol
    - Prefer accounts with underweight alternatives (cash can be redeployed)
    - Among equally ranked accounts, sell from the largest position first
    - Uses effective quantities (adjusted for prior-round trades) to prevent
      over-selling beyond what an account actually has available.
    """
    if total_shares <= 0:
        return []

    holders = find_accounts_for_symbol(symbol, accounts)
    if not holders:
        return []

    holders.sort(
        key=lambda a: (
            1 if _has_underweight_alternatives(
                a,
                symbol,
                effective_drift,
                transient_symbols,
                drift_trade_threshold_pct,
            ) else 0,
            _effective_qty(a, symbol, position_deltas),
        ),
        reverse=True,
    )

    trades = []
    remaining = total_shares

    for acct in holders:
        if remaining <= 0:
            break

        held = _effective_qty(acct, symbol, position_deltas)
        shares_to_sell = min(remaining, held)

        if shares_to_sell > 0:
            trades.append(TradeRecommendation(
                symbol=symbol,
                action="SELL",
                quantity=shares_to_sell,
                account_number=acct.number,
                account_type=acct.account_type,
                owner=acct.owner,
                price=price,
                currency=currency,
                estimated_value=price * shares_to_sell,
                note="Overweight sell",
            ))
            remaining -= shares_to_sell

    return trades
