"""Trade netting helpers.

Multiple rebalance rounds can generate offsetting trades. This module is
responsible for collapsing them into a clean final trade list.
"""

from src.rules import TradeRecommendation


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