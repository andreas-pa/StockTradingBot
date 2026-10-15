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


def open_order_symbols(client):
    """Return the symbols that have an unfilled order."""
    return {o.symbol for o in client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))}


def place_order(client, symbol, side, qty):
    """Place a market order. Return a short description of what happened."""
    order = client.submit_order(
        MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide(side), time_in_force=TimeInForce.DAY)
    )
    message = f"Submitted {side} order for {qty:g} {symbol}. Order id {order.id}, status {order.status.value}."
    log.info(message)
    return message


def last_buy_fill_time(client, symbol):
    """Return when the latest filled buy order for `symbol` filled, or None."""
    request = GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[symbol], side=OrderSide.BUY, limit=10)
    for order in client.get_orders(request):
        if order.filled_at:
            return order.filled_at
    return None


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
