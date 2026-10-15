"""Backtest the moving average crossover strategy on past daily prices.

The backtest follows the live bot's rules:
- A crossover is detected on a day's close and executed at the next day's open,
  like the live bot, which trades at 9:30 ET on the previous close's signal.
- Long only. A buy opens a position, and the next sell closes it.
- A sell signal without a position is ignored. So is a buy while already holding.

Prices are split- and dividend-adjusted. Commissions and slippage are not modeled.

Usage:
    python backtest.py                 # every symbol in config.yaml, last 10 years
    python backtest.py --years 5       # a different period
    python backtest.py AAPL MSFT       # only these symbols
"""

import argparse
import math

import pandas as pd
import yfinance as yf

from strategy import BASE_DIR, load_config

RESULTS_FILE = BASE_DIR / "backtest_results.csv"
TRADING_DAYS_PER_YEAR = 252


def crossover_signals(close, short_window, long_window):
    """Return a Series of 'buy', 'sell', or None for each day, using the live bot's rule."""
    short_ma = close.rolling(short_window).mean()
    long_ma = close.rolling(long_window).mean()
    prev_short, prev_long = short_ma.shift(1), long_ma.shift(1)

    buy = (prev_short <= prev_long) & (short_ma > long_ma)
    sell = (prev_short >= prev_long) & (short_ma < long_ma)
    signals = pd.Series(None, index=close.index, dtype=object)
    signals[buy] = "buy"
    signals[sell] = "sell"
    return signals


def backtest_symbol(prices, short_window, long_window):
    """Simulate the strategy on one symbol. Return a dict of results."""
    opens, closes = prices["Open"].to_numpy(), prices["Close"].to_numpy()
    dates = prices.index
    signals = crossover_signals(prices["Close"], short_window, long_window).to_numpy()

    # The first day a signal can be executed: the day after the long average exists.
    start = long_window
    if len(closes) <= start + 1:
        raise ValueError("Not enough price history.")

    equity, peak, max_drawdown = 1.0, 1.0, 0.0
    in_position, entry_price, entry_equity, entry_date = False, None, None, None
    trades, days_in_market = [], 0

    for i in range(start, len(closes)):
        signal = signals[i - 1]  # Detected on yesterday's close, executed at today's open.
        if signal == "buy" and not in_position:
            in_position, entry_price, entry_equity, entry_date = True, opens[i], equity, dates[i]
        elif signal == "sell" and in_position:
            trade_return = opens[i] / entry_price - 1
            trades.append({"entry": entry_date, "exit": dates[i], "return": trade_return})
            equity = entry_equity * (1 + trade_return)
            in_position = False

        if in_position:
            days_in_market += 1
            equity = entry_equity * closes[i] / entry_price
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1 - equity / peak)

    # Value a position that is still open at the last close.
    if in_position:
        trades.append({"entry": entry_date, "exit": None, "return": closes[-1] / entry_price - 1})

    days = len(closes) - start
    years = days / TRADING_DAYS_PER_YEAR
    buy_hold = closes[-1] / opens[start] - 1
    returns = [t["return"] for t in trades]

    def annualized(total):
        return (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else math.nan

    return {
        "years": years,
        "trades": len(trades),
        "win_rate": sum(r > 0 for r in returns) / len(returns) if returns else math.nan,
        "avg_trade": sum(returns) / len(returns) if returns else math.nan,
        "best_trade": max(returns) if returns else math.nan,
        "worst_trade": min(returns) if returns else math.nan,
        "strategy_return": equity - 1,
        "strategy_cagr": annualized(equity - 1),
        "buy_hold_return": buy_hold,
        "buy_hold_cagr": annualized(buy_hold),
        "max_drawdown": max_drawdown,
        "time_in_market": days_in_market / days,
        "open_now": in_position,
    }


def fetch_history(symbol, years):
    data = yf.Ticker(symbol).history(period=f"{years}y", interval="1d", auto_adjust=True)
    if data.empty:
        raise ValueError("No price data returned.")
    return data[["Open", "Close"]].dropna()


def pct(value, digits=0):
    return "-" if pd.isna(value) else f"{value * 100:.{digits}f}%"


def print_table(results):
    header = (
        f"{'Symbol':6} {'Years':>5} {'Trades':>6} {'Win':>5} {'Avg':>6} "
        f"{'Strategy':>9} {'CAGR':>6} {'Buy&Hold':>9} {'CAGR':>6} {'MaxDD':>6} {'InMkt':>6} {'Now':>4}"
    )
    print(header)
    print("-" * len(header))
    for symbol, r in results.iterrows():
        print(
            f"{symbol:6} {r.years:>5.1f} {r.trades:>6.0f} {pct(r.win_rate):>5} {pct(r.avg_trade, 1):>6} "
            f"{pct(r.strategy_return):>9} {pct(r.strategy_cagr, 1):>6} {pct(r.buy_hold_return):>9} "
            f"{pct(r.buy_hold_cagr, 1):>6} {pct(r.max_drawdown):>6} {pct(r.time_in_market):>6} "
            f"{'long' if r.open_now else '':>4}"
        )


def main():
    parser = argparse.ArgumentParser(description="Backtest the moving average crossover strategy")
    parser.add_argument("symbols", nargs="*", help="symbols to test (default: all in config.yaml)")
    parser.add_argument("--years", type=int, default=10, help="years of history (default: 10)")
    args = parser.parse_args()

    config = load_config()
    short_window, long_window = config["short_window"], config["long_window"]
    symbols = args.symbols or config["symbols"]
    print(f"Backtesting {len(symbols)} symbols, {short_window}/{long_window}-day crossover, last {args.years} years\n")

    rows, failed = {}, {}
    for symbol in symbols:
        try:
            rows[symbol] = backtest_symbol(fetch_history(symbol, args.years), short_window, long_window)
        except Exception as e:
            failed[symbol] = str(e)

    results = pd.DataFrame.from_dict(rows, orient="index").sort_values("strategy_cagr", ascending=False)
    print_table(results)

    beat = (results.strategy_cagr > results.buy_hold_cagr).sum()
    print(f"\nStrategy beat buy and hold on {beat} of {len(results)} symbols.")
    print(f"Median CAGR: strategy {pct(results.strategy_cagr.median(), 1)}, buy and hold {pct(results.buy_hold_cagr.median(), 1)}")
    print(f"Median max drawdown: strategy {pct(results.max_drawdown.median())}")
    print(f"Total trades: {results.trades.sum():.0f}, overall win rate "
          f"{pct((results.win_rate * results.trades).sum() / results.trades.sum())}")
    if (results.years < args.years - 0.5).any():
        short = results[results.years < args.years - 0.5]
        print("Shorter history: " + ", ".join(f"{s} ({y:.1f}y)" for s, y in short.years.items()))
    for symbol, error in failed.items():
        print(f"Failed: {symbol}: {error}")

    results.to_csv(RESULTS_FILE, index_label="symbol", float_format="%.4f")
    print(f"\nFull results saved to {RESULTS_FILE.name}")
    print("Columns: Win = winning trades, Avg = average trade return, CAGR = yearly growth rate,")
    print("MaxDD = largest drop from a peak, InMkt = time holding the stock, Now = position open today.")


if __name__ == "__main__":
    main()
