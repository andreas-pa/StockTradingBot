"""Config loading, price data, and the trading rules shared by the live bot and the backtest."""

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

# Settings that older config files may not have. These match the original strategy.
DEFAULTS = {
    "entry_mode": "crossover",
    "confirm_days": 1,
    "min_gap_pct": 0.0,
    "trailing_stop_pct": 0.0,
    "sizing": "shares",
    "trade_dollars": 1000,
    "market_filter_average": 0,
    "market_filter_symbol": "SPY",
    "market_filter_exit": False,
}


def validate(config):
    """Fill in defaults and check the strategy settings. Return the config."""
    config = {**DEFAULTS, **config}
    if config["short_window"] >= config["long_window"]:
        raise ValueError("Config error: short_window must be smaller than long_window.")
    if config["entry_mode"] not in ("crossover", "state"):
        raise ValueError("Config error: entry_mode must be 'crossover' or 'state'.")
    if config["confirm_days"] < 1:
        raise ValueError("Config error: confirm_days must be 1 or more.")
    if config["min_gap_pct"] < 0 or config["trailing_stop_pct"] < 0:
        raise ValueError("Config error: min_gap_pct and trailing_stop_pct cannot be negative.")
    if config["sizing"] not in ("shares", "dollars"):
        raise ValueError("Config error: sizing must be 'shares' or 'dollars'.")
    if config["market_filter_average"] == 1 or config["market_filter_average"] < 0:
        raise ValueError("Config error: market_filter_average must be 0 (off) or 2 or more.")
    return config


def market_filter_on(config):
    return config["market_filter_average"] > 0


def market_trend(market_closes, config, index):
    """Return a boolean Series over `index`: True where the market fund closed above its average.

    Days before the average can be computed count as False, so no buys happen then.
    """
    above = market_closes > market_closes.rolling(config["market_filter_average"]).mean()
    by_date = pd.Series(above.to_numpy(), index=[d.date() for d in market_closes.index])
    dates = [d.date() for d in index]
    aligned = by_date.reindex(sorted(set(by_date.index) | set(dates))).ffill().reindex(dates)
    return pd.Series(aligned.fillna(False).astype(bool).to_numpy(), index=index)


def load_config(path=BASE_DIR / "config.yaml"):
    with open(path) as f:
        config = validate(yaml.safe_load(f))
    if config["history_days"] <= config["long_window"] + config["confirm_days"]:
        raise ValueError("Config error: history_days must be greater than long_window + confirm_days.")
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


def compute_indicators(closes, config, market_closes=None):
    """Return a DataFrame with the moving averages and trend signals for each day.

    bullish / bearish: the trend condition has held for confirm_days days in a row.
    buy_event / sell_event: the first day of a new bullish or bearish period.
    market_ok: the market fund closed above its average, or the market filter is off.
    `market_closes` is needed only when the market filter is on.
    """
    short_ma = closes.rolling(config["short_window"]).mean()
    long_ma = closes.rolling(config["long_window"]).mean()
    gap = config["min_gap_pct"] / 100
    days = config["confirm_days"]

    up = short_ma > long_ma * (1 + gap)
    down = short_ma < long_ma * (1 - gap)
    bullish = up.astype(int).rolling(days).sum() == days
    bearish = down.astype(int).rolling(days).sum() == days

    # An event needs the day before it to be fully computable, so the first
    # usable day never counts as a crossover.
    known_before = long_ma.shift(days).notna()
    if market_filter_on(config):
        if market_closes is None:
            raise ValueError("The market filter is on, but no market prices were given.")
        market_ok = market_trend(market_closes, config, closes.index)
    else:
        market_ok = pd.Series(True, index=closes.index)
    return pd.DataFrame({
        "market_ok": market_ok,
        "close": closes,
        "short_ma": short_ma,
        "long_ma": long_ma,
        "bullish": bullish,
        "bearish": bearish,
        "buy_event": bullish & ~bullish.shift(1, fill_value=False) & known_before,
        "sell_event": bearish & ~bearish.shift(1, fill_value=False) & known_before,
    })


def decide(day, position, config):
    """Decide what to do at the next open, based on one day's indicators.

    `day` is a row from compute_indicators. `position` is a dict with:
        holding:  True if shares are held
        high:     highest close since the buy, including the buy price
        stopped:  True if the last exit was a trailing stop and no bearish
                  period has followed yet. Blocks state-mode re-entry until a
                  fresh crossover, so the bot does not buy straight back.
    Returns (action, reason) where action is "buy", "sell", or None.
    """
    market = f"{config['market_filter_symbol']} below its {config['market_filter_average']}-day average"
    if position["holding"]:
        stop = config["trailing_stop_pct"] / 100
        if stop and day.close <= position["high"] * (1 - stop):
            return "sell", f"trailing stop, {config['trailing_stop_pct']:g}% below the high of {position['high']:.2f}"
        if config["market_filter_exit"] and not day.market_ok:
            return "sell", market
        if config["entry_mode"] == "state" and day.bearish:
            return "sell", "short average below long average"
        if config["entry_mode"] == "crossover" and day.sell_event:
            return "sell", "crossover down"
        return None, "holding"

    if config["entry_mode"] == "crossover":
        wants_buy, reason = day.buy_event, "crossover up"
    else:
        wants_buy = day.bullish and (not position["stopped"] or day.buy_event)
        reason = "crossover up" if day.buy_event else "short average above long average"
    if wants_buy and not day.market_ok:
        return None, f"buy blocked, {market}"
    if wants_buy:
        return "buy", reason
    return None, "no signal"


def log_indicators(ind, config):
    """Log the last two days of prices and averages."""
    prev, curr = ind.iloc[-2], ind.iloc[-1]
    log.info(f"  Date          {ind.index[-2].date()}   {ind.index[-1].date()}")
    log.info(f"  Close         {prev.close:10.2f}   {curr.close:10.2f}")
    log.info(f"  {config['short_window']}-day MA    {prev.short_ma:10.2f}   {curr.short_ma:10.2f}")
    log.info(f"  {config['long_window']}-day MA    {prev.long_ma:10.2f}   {curr.long_ma:10.2f}")
