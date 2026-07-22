"""Portfolio rebalancer — trade planning decisions.

This is the single home for rebalancing logic. It decides what trades to make
based on the household rules documented in the README:

- measure drift at the unified household-portfolio level,
- use the drift threshold to suppress small *starter* trades,
- sell overweight symbols and buy underweight symbols,
- only buy symbols that already exist in the destination account,
- prefer same-currency deployment before cross-currency deployment,
- minimize stranded account-level cash whenever practical,
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
from src.cash_deploy import build_deploy_cash_underweight
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
        return effective_qty(account, symbol, self.position_deltas())

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

            cash_changes = self._deploy_residual_cash()

            if starter_changes == 0 and cash_changes == 0:
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

            current_drifts = self.plan.drifts()
            productive = self._productive_accounts_for_sell(symbol, current_drifts)
            sell_trades = allocate_sell(
                symbol,
                shares,
                bid_price_native,
                currency,
                self.portfolio.accounts,
                effective_drift=current_drifts,
                transient_symbols=self.hidden_symbols,
                position_deltas=self.plan.position_deltas(),
                productive_accounts=productive,
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
            self._raise_cash_in_account(
                acct, symbol, currency,
                target_native=remaining_shares * ask_price_native,
                min_useful_native=ask_price_native,
            )
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
            note="Underweight buy (FX)" if converted else "Underweight buy",
            requires_fx=converted,
        ))
        return quantity

    def _raise_cash_in_account(
        self,
        acct,
        buy_symbol: str,
        currency: str,
        target_native: float,
        min_useful_native: float | None = None,
    ) -> None:
        """Raise same-currency cash by selling other overweight holdings in the account.

        Tries to raise up to target_native by selling overweight positions.
        Only commits funding sells if the total proceeds will be at least
        min_useful_native (defaults to target_native). This prevents orphaned
        sells that generate cash but not enough to buy a single share of the target.
        """
        if min_useful_native is None:
            min_useful_native = target_native
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

        # Dry-run: compute planned sells and check if they raise enough cash
        current_cash = self.ledger.same_currency_buying_power(acct.number, currency)
        simulated_cash = current_cash
        planned_sells: list[tuple[str, int, float, float]] = []

        for _drift_pct, symbol, bid_price_native, max_sellable in candidates:
            shortfall = target_native - simulated_cash
            if shortfall <= 0:
                break

            sell_qty = min(max_sellable, int(math.ceil(shortfall / bid_price_native)))
            if sell_qty <= 0:
                continue

            proceeds = bid_price_native * sell_qty
            simulated_cash += proceeds
            planned_sells.append((symbol, sell_qty, bid_price_native, proceeds))

        # Only commit if we can raise at least enough for one share
        if simulated_cash < min_useful_native:
            return

        for symbol, sell_qty, bid_price_native, proceeds in planned_sells:
            trade = TradeRecommendation(
                symbol=symbol,
                action="SELL",
                quantity=sell_qty,
                account_number=acct.number,
                account_type=acct.account_type,
                owner=acct.owner,
                price=bid_price_native,
                currency=currency,
                estimated_value=proceeds,
                note="Funding sell",
            )
            self.ledger.credit_sale(acct.number, currency, trade.estimated_value)
            self.plan.add_trade(trade)

    def _cascade_score(self, symbol: str, current_account_number: str, drifts: dict[str, float]) -> float:
        """Score a symbol by the total underweight drift it can unlock in other accounts.

        When the current account buys this symbol with leftover cash, it may
        overshoot the household allocation, triggering a sell in a *different*
        account that holds the same symbol. If that other account has its own
        underweight positions, the sell proceeds get redeployed there — a
        cascade. The score is the total absolute underweight drift across all
        positions in all other accounts that hold this symbol. A higher score
        means buying this symbol now is more likely to unlock downstream
        rebalancing.
        """
        score = 0.0
        for acct in self.portfolio.accounts:
            if acct.number == current_account_number:
                continue
            if not any(p.symbol == symbol and p.quantity > 0 for p in acct.positions):
                continue
            for pos in acct.positions:
                if pos.symbol in self.hidden_symbols or pos.quantity <= 0:
                    continue
                drift = drifts.get(pos.symbol, 0.0)
                if drift < 0:
                    score += abs(drift)
        return score

    def _productive_accounts_for_sell(self, sell_symbol: str, drifts: dict[str, float]) -> set[str]:
        """Determine which accounts can productively use proceeds from selling a symbol.

        An account can productively use proceeds if it has:
        1. An underweight alternative (direct rebalancing), OR
        2. An alternative with greater cascade potential than the sell symbol
           (routes value out through a better conduit)
        """
        sell_cascade = {}
        productive = set()

        for acct in self.portfolio.accounts:
            if not any(p.symbol == sell_symbol and p.quantity > 0 for p in acct.positions):
                continue

            if _has_underweight_alternatives(
                acct, sell_symbol, drifts, self.hidden_symbols,
            ):
                productive.add(acct.number)
                continue

            if acct.number not in sell_cascade:
                sell_cascade[acct.number] = self._cascade_score(
                    sell_symbol, acct.number, drifts,
                )

            for pos in acct.positions:
                if pos.symbol == sell_symbol or pos.quantity <= 0:
                    continue
                if pos.symbol in self.hidden_symbols:
                    continue
                alt_score = self._cascade_score(pos.symbol, acct.number, drifts)
                if alt_score > sell_cascade[acct.number]:
                    productive.add(acct.number)
                    break

        return productive

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
        fallback that prefers the symbol most likely to trigger useful downstream
        rebalancing (highest cascade score), breaking ties by lowest drift.
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

        if underweight_only:
            candidates.sort(key=lambda item: item[0])
        else:
            candidates.sort(
                key=lambda item: (-self._cascade_score(item[1], acct.number, drifts), item[0])
            )
        return candidates

    def _build_deploy_cash_any(
        self,
        acct,
        source_currency: str,
        drifts: dict[str, float],
    ):
        """Deploy an account's residual ``source_currency`` cash to the highest-cascade holding.

        This is the "best available" deployment used when no symbol is still
        underweight but cash would otherwise sit idle (earning nothing). The
        cash flows to wherever it has the most potential to be useful, which
        may mean crossing the currency boundary.

        Both currencies are considered: the best same-currency candidate and
        the best cross-currency candidate (each already the top of its
        cascade-sorted list). We deploy into whichever has the higher cascade
        score — the total underweight drift it can unlock in other accounts
        that hold it — since buying a high-cascade symbol overshoots the
        household allocation and a later round corrects it by selling from an
        account with underweight alternatives, routing the cash to where it's
        most needed. Ties favour the same-currency buy, which avoids a
        Norbert's Gambit conversion. A cross-currency win pays that conversion
        cost; the next round then deploys the converted cash same-currency.
        """
        source_cash = self.ledger.same_currency_buying_power(acct.number, source_currency)
        if source_cash <= 0:
            return None

        other_currency = "USD" if source_currency == "CAD" else "CAD"

        same = self._top_buyable_candidate(acct, source_currency, drifts)
        cross = self._top_buyable_candidate(acct, other_currency, drifts)

        # Choose the destination with the higher cascade score; prefer the
        # same-currency buy on ties so we don't convert without a reason.
        # A same-currency buy is free to fire even at zero cascade (no FX cost),
        # but crossing the currency boundary is only worth a Norbert's Gambit
        # conversion when the destination has real cascade potential — otherwise
        # we'd pay a fee to park cash in an at-target holding with no downstream
        # benefit. Genuinely stranded foreign cash is instead handled by the
        # post-rebalance conversion sweep in ``fx_conversions``.
        if same is not None and (cross is None or same[2] >= cross[2]):
            symbol, ask_price_native, _cascade = same
            buy_currency = source_currency
            requires_fx = False
            available_cash = source_cash
        elif cross is not None and cross[2] > 0:
            symbol, ask_price_native, _cascade = cross
            buy_currency = other_currency
            requires_fx = True
            available_cash = cross_currency_buying_power(
                source_cash,
                source_currency,
                self.usd_to_cad_rate,
                self.fee_cad,
                dlr_quotes=self.dlr_quotes,
            )
        else:
            return None

        quantity = int(math.floor(available_cash / ask_price_native))
        if quantity <= 0:
            return None

        cost_native = quantity * ask_price_native
        if requires_fx:
            self.ledger.fund_buy(
                acct.number,
                buy_currency,
                cost_native,
                dlr_quotes=self.dlr_quotes,
            )
        else:
            self.ledger.balances[acct.number][buy_currency] -= cost_native

        return TradeRecommendation(
            symbol=symbol,
            action="BUY",
            quantity=quantity,
            account_number=acct.number,
            account_type=acct.account_type,
            owner=acct.owner,
            price=ask_price_native,
            currency=buy_currency,
            estimated_value=cost_native,
            note="Deploy cash (any) (FX)" if requires_fx else "Deploy cash (any)",
            requires_fx=requires_fx,
        )

    def _top_buyable_candidate(self, acct, currency: str, drifts: dict[str, float]):
        """Return the highest-cascade buyable ``(symbol, ask_price, cascade)`` in a currency, or None."""
        candidates = self._account_buyable_candidates(acct, currency, drifts, underweight_only=False)
        if not candidates:
            return None
        _drift_pct, symbol, ask_price_native = candidates[0]
        return symbol, ask_price_native, self._cascade_score(symbol, acct.number, drifts)

    def _deploy_residual_cash(self) -> int:
        """Minimize stranded cash, deploying it where it can be most useful.

        Two layers run per account, each preferring same-currency before
        cross-currency (an FX buy pays a Norbert's Gambit conversion, so a
        same-currency option of equal value wins):

        1. Underweight buys (``build_deploy_cash_underweight``) — close a real drift
           gap, same-currency first, then cross-currency.
        2. Best-available deployment (``_build_deploy_cash_any``) — once
           nothing is underweight, route leftover cash to the highest-cascade
           holding, comparing same- and cross-currency destinations so idle
           cash flows to where it has the most potential to be useful.
        """
        trades_added = 0
        while True:
            made_trade = False
            projected_drifts = self.plan.drifts()

            for acct in self.portfolio.accounts:
                # Underweight buys — same-currency first, then cross-currency.
                for source_currency in ("CAD", "USD"):
                    same_currency = source_currency
                    cross_currency = "USD" if source_currency == "CAD" else "CAD"
                    for buy_currency in (same_currency, cross_currency):
                        requires_fx = buy_currency != source_currency
                        while True:
                            trade = build_deploy_cash_underweight(
                                acct,
                                self.ledger.balances,
                                self.holdings_view,
                                projected_drifts,
                                self.hidden_symbols,
                                self.portfolio.total_value_cad,
                                self.usd_to_cad_rate,
                                source_currency=source_currency,
                                buy_currency=buy_currency,
                                underweight_threshold_pct=0.0,
                                fee_cad=self.fee_cad if requires_fx else 0.0,
                                note="Deploy cash (underweight) (FX)" if requires_fx else "Deploy cash (underweight)",
                                dlr_quotes=self.dlr_quotes if requires_fx else None,
                            )
                            if trade is None:
                                break
                            self.plan.add_trade(trade)
                            made_trade = True
                            trades_added += 1
                            projected_drifts = self.plan.drifts()

                # Best-available deployment for any leftover cash. This routes
                # to the highest-cascade holding across both currencies, so it
                # subsumes same- and cross-currency deployment in one call.
                for source_currency in ("CAD", "USD"):
                    while True:
                        trade = self._build_deploy_cash_any(
                            acct, source_currency, projected_drifts,
                        )
                        if trade is None:
                            break
                        self.plan.add_trade(trade)
                        made_trade = True
                        trades_added += 1
                        projected_drifts = self.plan.drifts()

            if not made_trade:
                break

        return trades_added


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
    # Nudge before flooring: the drift→shares round-trip (dollars → drift % →
    # dollars → ÷ FX rate) accumulates binary floating-point error that can land
    # a whole-share result a hair below an integer (e.g. 19.9999999998), which
    # would otherwise floor down and under-sell by one share — notably leaving a
    # 1-share sliver when fully liquidating a target-0 position.
    shares = int(math.floor(gap_native / price_native + 1e-9))
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


def effective_qty(account, symbol: str, position_deltas: dict) -> int:
    """Get the effective quantity accounting for trades already planned."""
    original = int(get_position_quantity(account, symbol))
    delta = position_deltas.get((account.number, symbol), 0)
    return max(0, original + delta)


def _has_underweight_alternatives(
    acct,
    sell_symbol: str,
    effective_drift: dict,
    transient_symbols: set,
) -> bool:
    """Check if proceeds from selling can be redeployed into underweight positions.

    A position counts as a redeployment target if it is below target at all
    (drift < 0), matching the residual-cash deployment layer which buys any
    underweight symbol. This is deliberately looser than
    ``drift_trade_threshold_pct``: that threshold suppresses tiny *starter*
    trades, but once an overweight sell has been triggered its proceeds should
    be usable against any underweight holding rather than stranded.
    """
    for pos in acct.positions:
        if pos.symbol == sell_symbol or pos.quantity <= 0:
            continue
        if pos.symbol in transient_symbols:
            continue
        if effective_drift.get(pos.symbol, 0) < 0:
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
    position_deltas: dict,
    productive_accounts: set | None = None,
) -> list:
    """Allocate a SELL order across accounts.

    Strategy:
    - Only sell from accounts that hold the symbol
    - Skip accounts that can't productively use the proceeds (when
      productive_accounts is provided, only sell from those accounts)
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

    if productive_accounts is not None:
        holders = [a for a in holders if a.number in productive_accounts]
        if not holders:
            return []

    holders.sort(
        key=lambda a: (
            1 if _has_underweight_alternatives(
                a,
                symbol,
                effective_drift,
                transient_symbols,
            ) else 0,
            effective_qty(a, symbol, position_deltas),
        ),
        reverse=True,
    )

    trades = []
    remaining = total_shares

    for acct in holders:
        if remaining <= 0:
            break

        held = effective_qty(acct, symbol, position_deltas)
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
