"""Trade plan reconciliation: netting, cash deployment, and sell-trimming.

After the main planner generates raw trades, this module handles all the
finalization logic:

- **Netting** — collapse offsetting buys/sells for the same (symbol, account)
- **Deployment** — build buy trades from available cash (same- or cross-currency)
- **Sell trimming** — reduce sell quantities when they raise more cash than needed
- **Residual deployment** — use leftover cash for still-underweight holdings
"""

import math

from src.funding import (
    build_account_trade_impacts,
    can_fund_net_cash_requirement,
    consume_cross_currency_cash,
    cross_currency_buying_power,
    net_account_cash,
    settle_net_cash_after_conversion,
    to_cad,
)
from src.portfolio import simulate_rebalance
from src.models import TradeRecommendation


POST_PROCESS_MAX_PASSES = 5


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

        for trade in trades_list:
            if trade.action == "BUY":
                total_buy_qty += trade.quantity
                buy_price = trade.price
                if trade.note and not buy_note:
                    buy_note = trade.note
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
# Cash deployment helpers
# ══════════════════════════════════════════════════════════════════


def underweight_candidates(
    acct,
    holdings: dict,
    drifts: dict,
    hidden_symbols: set,
    currency: str,
    underweight_threshold_pct: float,
) -> list[tuple[str, float, float]]:
    """Return account-local underweight holdings in the requested currency."""
    candidates = []
    seen = set()

    for pos in acct.positions:
        if pos.quantity <= 0 or pos.currency != currency:
            continue
        if pos.symbol in hidden_symbols or pos.symbol in seen:
            continue

        drift_pct = drifts.get(pos.symbol, 0.0)
        if drift_pct >= -underweight_threshold_pct:
            continue

        holding = holdings.get(pos.symbol)
        if not holding:
            continue

        ask_price_native = holding.ask_price or pos.current_price
        if ask_price_native <= 0:
            continue

        seen.add(pos.symbol)
        candidates.append((pos.symbol, drift_pct, ask_price_native))

    candidates.sort(key=lambda item: item[1])
    return candidates


def build_same_currency_buy(
    acct,
    cash_by_account: dict[str, dict[str, float]],
    holdings: dict,
    drifts: dict,
    hidden_symbols: set,
    total_value_cad: float,
    usd_to_cad_rate: float,
    currency: str,
    underweight_threshold_pct: float,
    note: str = "",
) -> TradeRecommendation | None:
    """Build one same-currency buy and deduct its cash cost if possible."""
    acct_cash = cash_by_account.setdefault(acct.number, {"CAD": 0.0, "USD": 0.0})
    cash_native = acct_cash.get(currency, 0.0)
    if cash_native <= 0:
        return None

    candidates = underweight_candidates(
        acct,
        holdings,
        drifts,
        hidden_symbols,
        currency,
        underweight_threshold_pct,
    )
    if not candidates:
        return None

    symbol, drift_pct, ask_price_native = candidates[0]
    affordable_shares = int(math.floor(cash_native / ask_price_native))
    if affordable_shares <= 0:
        return None

    shares = min(
        _shares_to_close_underweight(
            total_value_cad,
            drift_pct,
            ask_price_native,
            currency,
            usd_to_cad_rate,
        ),
        affordable_shares,
    )
    if shares <= 0:
        return None

    cost_native = shares * ask_price_native
    acct_cash[currency] -= cost_native
    return TradeRecommendation(
        symbol=symbol,
        action="BUY",
        quantity=shares,
        account_number=acct.number,
        account_type=acct.account_type,
        owner=acct.owner,
        price=ask_price_native,
        currency=currency,
        estimated_value=cost_native,
        note=note,
    )


def build_cross_currency_buy(
    acct,
    cash_by_account: dict[str, dict[str, float]],
    holdings: dict,
    drifts: dict,
    hidden_symbols: set,
    total_value_cad: float,
    usd_to_cad_rate: float,
    source_currency: str,
    fee_cad: float,
    underweight_threshold_pct: float,
    note: str,
    dlr_quotes=None,
) -> TradeRecommendation | None:
    """Build one cross-currency buy and deduct its conservative cash cost."""
    acct_cash = cash_by_account.setdefault(acct.number, {"CAD": 0.0, "USD": 0.0})
    source_cash_native = acct_cash.get(source_currency, 0.0)
    if source_cash_native <= 0:
        return None

    target_currency = "USD" if source_currency == "CAD" else "CAD"
    candidates = underweight_candidates(
        acct,
        holdings,
        drifts,
        hidden_symbols,
        target_currency,
        underweight_threshold_pct,
    )
    if not candidates:
        return None

    symbol, drift_pct, ask_price_native = candidates[0]
    buying_power_native = cross_currency_buying_power(
        source_cash_native,
        source_currency,
        usd_to_cad_rate,
        fee_cad,
        dlr_quotes=dlr_quotes,
    )
    affordable_shares = int(math.floor(buying_power_native / ask_price_native))
    if affordable_shares <= 0:
        return None

    shares = min(
        _shares_to_close_underweight(
            total_value_cad,
            drift_pct,
            ask_price_native,
            target_currency,
            usd_to_cad_rate,
        ),
        affordable_shares,
    )
    if shares <= 0:
        return None

    cost_native = shares * ask_price_native
    consume_cross_currency_cash(
        acct_cash,
        source_currency,
        target_currency,
        cost_native,
        usd_to_cad_rate,
        fee_cad,
        dlr_quotes=dlr_quotes,
    )
    return TradeRecommendation(
        symbol=symbol,
        action="BUY",
        quantity=shares,
        account_number=acct.number,
        account_type=acct.account_type,
        owner=acct.owner,
        price=ask_price_native,
        currency=target_currency,
        estimated_value=cost_native,
        note=note,
    )


def _shares_to_close_underweight(
    total_value_cad: float,
    drift_pct: float,
    price_native: float,
    currency: str,
    usd_to_cad_rate: float,
) -> int:
    """Return whole shares needed to close an underweight gap."""
    if total_value_cad <= 0 or price_native <= 0:
        return 0

    gap_cad = abs(drift_pct / 100.0) * total_value_cad
    gap_native = gap_cad / usd_to_cad_rate if currency == "USD" else gap_cad
    return int(math.ceil(gap_native / price_native))


# ══════════════════════════════════════════════════════════════════
# Post-processing: trim excess sells and deploy residual cash
# ══════════════════════════════════════════════════════════════════


def trim_excess_sell_funding(
    portfolio,
    trades: list,
    targets: dict,
    usd_to_cad_rate: float,
    fee_cad: float,
    drift_trade_threshold_pct: float,
    dlr_quotes=None,
    hidden_symbols: set | None = None,
) -> list:
    """Optimise the final trade plan after the main rebalance rounds."""
    if not trades:
        return trades

    if hidden_symbols is None:
        hidden_symbols = set()

    optimised = net_trades(list(trades))
    for _ in range(POST_PROCESS_MAX_PASSES):
        before = list(optimised)
        optimised = _trim_excess_sell_funding_once(
            portfolio,
            optimised,
            targets,
            usd_to_cad_rate,
            fee_cad,
            drift_trade_threshold_pct,
            dlr_quotes=dlr_quotes,
            hidden_symbols=hidden_symbols,
        )
        optimised = net_trades(optimised)
        optimised = _deploy_residual_underweight_buys(
            portfolio,
            optimised,
            targets,
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes=dlr_quotes,
            hidden_symbols=hidden_symbols,
        )
        optimised = net_trades(optimised)
        if optimised == before:
            break

    return optimised


def _trim_excess_sell_funding_once(
    portfolio,
    trades: list,
    targets: dict,
    usd_to_cad_rate: float,
    fee_cad: float,
    drift_trade_threshold_pct: float,
    dlr_quotes=None,
    hidden_symbols: set | None = None,
) -> list:
    """Trim sell quantities when they raise more cash than account buys require."""
    if not trades:
        return trades

    trimmed = list(trades)
    projected = simulate_rebalance(
        portfolio,
        trimmed,
        targets,
        usd_to_cad_rate,
        hidden_symbols=hidden_symbols,
    )

    index = 0
    while index < len(trimmed):
        trade = trimmed[index]
        if trade.action != "SELL":
            index += 1
            continue

        max_reduction = _max_sell_reduction_allowed_by_tolerance(
            portfolio,
            trade,
            projected.drifts,
            drift_trade_threshold_pct,
            usd_to_cad_rate,
        )
        if max_reduction <= 0:
            index += 1
            continue

        low = 0
        high = max_reduction
        while low < high:
            reduction = (low + high + 1) // 2
            candidate_qty = trade.quantity - reduction
            candidate_trades = _updated_trades_with_resized_trade(trimmed, index, candidate_qty)
            if _trade_plan_is_fundable(
                portfolio,
                candidate_trades,
                usd_to_cad_rate,
                fee_cad,
                dlr_quotes=dlr_quotes,
            ):
                low = reduction
            else:
                high = reduction - 1

        if low > 0:
            trimmed = _updated_trades_with_resized_trade(trimmed, index, trade.quantity - low)
            projected = simulate_rebalance(
                portfolio,
                trimmed,
                targets,
                usd_to_cad_rate,
                hidden_symbols=hidden_symbols,
            )

        index += 1

    return trimmed


def _deploy_residual_underweight_buys(
    portfolio,
    trades: list,
    targets: dict,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
    hidden_symbols: set | None = None,
) -> list:
    """Use leftover account cash for any holdings that are still under target."""
    if not trades:
        return trades

    if hidden_symbols is None:
        hidden_symbols = set()

    updated = list(trades)
    residual_cash = _build_residual_cash_map(
        portfolio,
        updated,
        usd_to_cad_rate,
        fee_cad,
        dlr_quotes=dlr_quotes,
    )

    while True:
        projected = simulate_rebalance(
            portfolio,
            updated,
            targets,
            usd_to_cad_rate,
            hidden_symbols=hidden_symbols,
        )
        made_trade = False

        for acct in portfolio.accounts:
            for currency in ("CAD", "USD"):
                while True:
                    trade = build_same_currency_buy(
                        acct,
                        residual_cash,
                        portfolio.holdings,
                        projected.drifts,
                        hidden_symbols,
                        portfolio.total_value_cad,
                        usd_to_cad_rate,
                        currency,
                        0.0,
                        note="Residual cash deployment",
                    )
                    if trade is None:
                        break

                    updated.append(trade)
                    made_trade = True
                    projected = simulate_rebalance(
                        portfolio,
                        updated,
                        targets,
                        usd_to_cad_rate,
                        hidden_symbols=hidden_symbols,
                    )

            for source_currency in ("CAD", "USD"):
                while True:
                    trade = build_cross_currency_buy(
                        acct,
                        residual_cash,
                        portfolio.holdings,
                        projected.drifts,
                        hidden_symbols,
                        portfolio.total_value_cad,
                        usd_to_cad_rate,
                        source_currency,
                        fee_cad,
                        0.0,
                        note="Requires currency conversion; residual cash deployment",
                        dlr_quotes=dlr_quotes,
                    )
                    if trade is None:
                        break

                    updated.append(trade)
                    made_trade = True
                    projected = simulate_rebalance(
                        portfolio,
                        updated,
                        targets,
                        usd_to_cad_rate,
                        hidden_symbols=hidden_symbols,
                    )

        if not made_trade:
            break

    return updated


def _updated_trades_with_resized_trade(trades: list, index: int, quantity: int) -> list:
    """Return a new trade list with one trade resized or removed."""
    updated = list(trades)
    if quantity <= 0:
        del updated[index]
        return updated

    trade = updated[index]
    updated[index] = TradeRecommendation(
        symbol=trade.symbol,
        action=trade.action,
        quantity=quantity,
        account_number=trade.account_number,
        account_type=trade.account_type,
        owner=trade.owner,
        price=trade.price,
        currency=trade.currency,
        estimated_value=trade.price * quantity,
        note=trade.note,
    )
    return updated


def _build_residual_cash_map(
    portfolio,
    trades: list,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
) -> dict[str, dict[str, float]]:
    """Return per-account residual cash after the current trade plan settles."""
    impacts = build_account_trade_impacts(trades)
    residual = {}

    for acct in portfolio.accounts:
        net_cash = net_account_cash(acct, impacts.get(acct.number))
        settled = settle_net_cash_after_conversion(
            net_cash,
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes=dlr_quotes,
        )
        residual[acct.number] = {
            "CAD": settled.cad,
            "USD": settled.usd,
        }

    for acct_number in impacts:
        residual.setdefault(acct_number, {"CAD": 0.0, "USD": 0.0})

    return residual


def _trade_plan_is_fundable(
    portfolio,
    trades: list,
    usd_to_cad_rate: float,
    fee_cad: float,
    dlr_quotes=None,
) -> bool:
    """Check whether each account can still fund the candidate trade plan."""
    acct_map = {acct.number: acct for acct in portfolio.accounts}
    for acct_number, impact in build_account_trade_impacts(trades).items():
        acct = acct_map.get(acct_number)
        if acct is None:
            continue

        net_cash = net_account_cash(acct, impact)
        if not can_fund_net_cash_requirement(
            net_cash,
            usd_to_cad_rate,
            fee_cad,
            dlr_quotes=dlr_quotes,
        ):
            return False

    return True


def _max_sell_reduction_allowed_by_tolerance(
    portfolio,
    trade,
    projected_drifts: dict,
    drift_trade_threshold_pct: float,
    usd_to_cad_rate: float,
) -> int:
    """Maximum shares this sell can be reduced without breaching tolerance."""
    if trade.action != "SELL" or portfolio.total_value_cad <= 0:
        return 0

    per_share_drift_pct = (
        to_cad(trade.price, trade.currency, usd_to_cad_rate) / portfolio.total_value_cad
    ) * 100.0
    if per_share_drift_pct <= 0:
        return 0

    current_projected_drift = projected_drifts.get(trade.symbol, 0.0)
    drift_room = drift_trade_threshold_pct - current_projected_drift
    if drift_room <= 0:
        return 0

    return min(trade.quantity, int(math.floor((drift_room + 1e-9) / per_share_drift_pct)))
