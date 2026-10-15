"""Config loading, price data, and moving average crossover detection."""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml
import yfinance as yf

BASE_DIR = Path(__file__).resolve().parent
MARKET_TZ = ZoneInfo("America/New_York")

log = logging.getLogger("bot")


def load_config(path=BASE_DIR / "config.yaml"):
    with open(path) as f:
        config = yaml.safe_load(f)

    if config["short_window"] >= config["long_window"]:
        raise ValueError("Config error: short_window must be smaller than long_window.")
    if config["history_days"] <= config["long_window"]:
        raise ValueError("Config error: history_days must be greater than long_window.")
    return config


def fetch_prices(symbol, history_days):
    """Return daily closing prices for the last `history_days` completed trading days."""
    # Fetch extra calendar days to cover weekends and holidays.
    start = datetime.now(MARKET_TZ).date() - timedelta(days=history_days * 2)
    data = yf.Ticker(symbol).history(start=start.isoformat(), interval="1d")
    if data.empty:
        raise ValueError(f"No price data returned for {symbol}.")

    closes = data["Close"]

    # During market hours yfinance includes today's unfinished bar. Drop it so
    # signals use completed days only.
    now = datetime.now(MARKET_TZ)
    if closes.index[-1].date() == now.date() and now.hour < 16:
        closes = closes.iloc[:-1]

    closes = closes.tail(history_days)
    if len(closes) < 2:
        raise ValueError(f"Too few completed trading days for {symbol}.")
    return closes


def find_crossover(closes, short_window, long_window):
    """Compare the last two days and return 'buy', 'sell', or None."""
    short_ma = closes.rolling(short_window).mean()
    long_ma = closes.rolling(long_window).mean()

    prev_short, curr_short = short_ma.iloc[-2], short_ma.iloc[-1]
    prev_long, curr_long = long_ma.iloc[-2], long_ma.iloc[-1]

    if pd.isna(prev_long):
        raise ValueError("Not enough price history to compute the long moving average.")

    log.info(f"  Date          {closes.index[-2].date()}   {closes.index[-1].date()}")
    log.info(f"  Close         {closes.iloc[-2]:10.2f}   {closes.iloc[-1]:10.2f}")
    log.info(f"  {short_window}-day MA    {prev_short:10.2f}   {curr_short:10.2f}")
    log.info(f"  {long_window}-day MA    {prev_long:10.2f}   {curr_long:10.2f}")

    if prev_short <= prev_long and curr_short > curr_long:
        return "buy"
    if prev_short >= prev_long and curr_short < curr_long:
        return "sell"
    return None
