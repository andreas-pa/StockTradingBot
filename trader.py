"""Alpaca paper trading: connection, orders, and account queries."""

import logging
import os

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest, MarketOrderRequest
from dotenv import load_dotenv

from strategy import BASE_DIR

log = logging.getLogger("bot")


def get_client():
    load_dotenv(BASE_DIR / ".env")
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret or key.startswith("your_"):
        raise RuntimeError("Missing Alpaca keys. Copy .env.example to .env and add your paper keys.")

    # paper=True sends every order to the paper trading account, never a live one.
    return TradingClient(key, secret, paper=True)


def is_trading_day(client, day):
    """Return True if the US stock market is open on `day`. False on weekends and holidays."""
    calendar = client.get_calendar(GetCalendarRequest(start=day, end=day))
    return any(c.date == day for c in calendar)


def held_quantity(client, symbol):
    for position in client.get_all_positions():
        if position.symbol == symbol:
            return float(position.qty)
    return 0.0


def place_order(client, symbol, signal, trade_size):
    """Place a market order for a signal. Return a short description of what happened."""
    held = held_quantity(client, symbol)

    # Skip a buy if we already hold shares, so a second run on the same day
    # does not buy twice.
    if signal == "buy" and held > 0:
        message = f"Already holding {held:g} {symbol}. Skipped buy."
        log.info(message)
        return message
    # Never sell shares we do not own. That would open a short position.
    if signal == "sell" and held <= 0:
        message = f"No {symbol} shares held. Skipped sell."
        log.info(message)
        return message

    qty = trade_size if signal == "buy" else min(trade_size, held)
    side = OrderSide.BUY if signal == "buy" else OrderSide.SELL
    order = client.submit_order(
        MarketOrderRequest(symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY)
    )
    message = f"Submitted {signal} order for {qty:g} {symbol}. Order id {order.id}, status {order.status.value}."
    log.info(message)
    return message


def get_open_orders(client):
    return client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))


def get_recent_orders(client, limit=10):
    return client.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=limit))


def get_filled_orders_since(client, since):
    orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=since, limit=500))
    return [o for o in orders if o.filled_at]


def positions_snapshot(client):
    """Return {symbol: {"qty": shares, "entry": average entry price}} for every open position."""
    return {
        p.symbol: {"qty": float(p.qty), "entry": float(p.avg_entry_price)}
        for p in client.get_all_positions()
    }


def last_fill_price(client, symbol, side):
    """Return the average fill price of the latest filled `side` order for `symbol`, or None."""
    request = GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[symbol], side=OrderSide(side), limit=10)
    for order in client.get_orders(request):
        if order.filled_avg_price:
            return float(order.filled_avg_price)
    return None
