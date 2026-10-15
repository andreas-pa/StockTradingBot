"""Backtest the strategy on past daily prices.

The backtest uses the same rules as the live bot (strategy.compute_indicators
and strategy.decide):
- A signal is decided on a day's close and executed at the next day's open,
  like the live bot, which trades at 9:30 ET on the previous close.
- Long only. Prices are split- and dividend-adjusted.
- Commissions and slippage are not modeled.

Usage:
    python backtest.py                 # config.yaml settings, every symbol, last 10 years
    python backtest.py --years 5       # a different period
    python backtest.py AAPL MSFT       # only these symbols
    python backtest.py --compare       # compare strategy variants over three periods
"""

import argparse
import math

import pandas as pd
import yfinance as yf

from strategy import BASE_DIR, compute_indicators, decide, load_config, validate

RESULTS_FILE = BASE_DIR / "backtest_results.csv"
TRADING_DAYS_PER_YEAR = 252

# Variants for --compare. Each one changes a single setting of config.yaml.
VARIANTS = {
    "Current config": {},
    "State mode": {"entry_mode": "state"},
    "50/200 averages": {"short_window": 50, "long_window": 200},
    "Confirm 2 days": {"confirm_days": 2},
    "Min gap 1%": {"min_gap_pct": 1},
    "Trailing stop 15%": {"trailing_stop_pct": 15},
}
SPLIT_YEAR = 2022  # --compare tests before and from this year separately.


def max_drawdown(values):
    peak, worst = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        worst = max(worst, 1 - v / peak)
    return worst


def simulate(ind, opens, config, start, end=None):
    """Run the strategy from index `start` to `end` (inclusive). Return a dict of results."""
    closes = ind["close"].to_numpy()
    dates = ind.index
    rows = list(ind.itertuples())
    end = len(closes) - 1 if end is None else end
    if end <= start:
        raise ValueError("Not enough price history.")

    equity, curve = 1.0, []
    holding, stopped = False, False
    entry_price = entry_equity = high = entry_date = None
    shares = 0
    trades, days_in_market = [], 0

    for i in range(start, end + 1):
        decision_day = rows[i - 1]  # Decided on yesterday's close, executed at today's open.
        if decision_day.bearish:
            stopped = False
        action, reason = decide(decision_day, {"holding": holding, "high": high, "stopped": stopped}, config)

        if action == "buy":
            shares = buy_quantity(config, decision_day.close)
            if shares > 0:
                holding, stopped = True, False
                entry_price, entry_equity, high, entry_date = opens[i], equity, opens[i], dates[i]
        elif action == "sell":
            trade_return = opens[i] / entry_price - 1
            trades.append({
                "entry": entry_date, "exit": dates[i], "return": trade_return,
                "pnl": (opens[i] - entry_price) * shares, "reason": reason,
            })
            equity = entry_equity * (1 + trade_return)
            holding = False
            stopped = reason.startswith("trailing stop")

        if holding:
            days_in_market += 1
            high = max(high, closes[i])
            equity = entry_equity * closes[i] / entry_price
        curve.append(equity)

    # Value a position that is still open at the last close.
    if holding:
        trades.append({
            "entry": entry_date, "exit": None, "return": closes[end] / entry_price - 1,
            "pnl": (closes[end] - entry_price) * shares, "reason": "still open",
        })

    days = end - start + 1
    years = days / TRADING_DAYS_PER_YEAR
    buy_hold_curve = closes[start:end + 1] / opens[start]
    returns = [t["return"] for t in trades]

    def annualized(total):
        return (1 + total) ** (1 / years) - 1 if total > -1 else math.nan

    return {
        "years": years,
        "trades": len(trades),
        "win_rate": sum(r > 0 for r in returns) / len(returns) if returns else math.nan,
        "avg_trade": sum(returns) / len(returns) if returns else math.nan,
        "strategy_return": equity - 1,
        "strategy_cagr": annualized(equity - 1),
        "buy_hold_return": buy_hold_curve[-1] - 1,
        "buy_hold_cagr": annualized(buy_hold_curve[-1] - 1),
        "max_drawdown": max_drawdown(curve),
        "buy_hold_drawdown": max_drawdown(buy_hold_curve),
        "time_in_market": days_in_market / days,
        "open_now": holding,
        "pnl_dollars": sum(t["pnl"] for t in trades),
        "position_cost_now": closes[end] * buy_quantity(config, closes[end]),
    }


def buy_quantity(config, price):
    if config["sizing"] == "dollars":
        return int(config["trade_dollars"] // price)
    return config["trade_size"]


def warmup(config):
    """First index where a signal can be executed."""
    return config["long_window"] + config["confirm_days"]


def fetch_history(symbol, years):
    data = yf.Ticker(symbol).history(period=f"{years}y", interval="1d", auto_adjust=True)
    if data.empty:
        raise ValueError("No price data returned.")
    return data[["Open", "Close"]].dropna()


def pct(value, digits=0):
    return "-" if pd.isna(value) else f"{value * 100:.{digits}f}%"


# ---------- Single run: one config, per-symbol table ----------

def print_table(results):
    header = (
        f"{'Symbol':6} {'Years':>5} {'Trades':>6} {'Win':>5} {'Avg':>6} "
        f"{'Strategy':>9} {'CAGR':>6} {'Buy&Hold':>9} {'CAGR':>6} {'MaxDD':>6} {'InMkt':>6} {'Now':>4} {'P/L $':>9}"
    )
    print(header)
    print("-" * len(header))
    for symbol, r in results.iterrows():
        print(
            f"{symbol:6} {r.years:>5.1f} {r.trades:>6.0f} {pct(r.win_rate):>5} {pct(r.avg_trade, 1):>6} "
            f"{pct(r.strategy_return):>9} {pct(r.strategy_cagr, 1):>6} {pct(r.buy_hold_return):>9} "
            f"{pct(r.buy_hold_cagr, 1):>6} {pct(r.max_drawdown):>6} {pct(r.time_in_market):>6} "
            f"{'long' if r.open_now else '':>4} {r.pnl_dollars:>9,.0f}"
        )


def run_single(symbols, config, years):
    size = f"${config['trade_dollars']:,} per trade" if config["sizing"] == "dollars" else f"{config['trade_size']} shares per trade"
    print(f"Backtesting {len(symbols)} symbols, last {years} years")
    print(f"{config['short_window']}/{config['long_window']}-day, {config['entry_mode']} mode, "
          f"confirm {config['confirm_days']}, gap {config['min_gap_pct']:g}%, "
          f"trailing stop {config['trailing_stop_pct']:g}%, {size}\n")

    rows, failed = {}, {}
    for symbol in symbols:
        try:
            prices = fetch_history(symbol, years)
            ind = compute_indicators(prices["Close"], config)
            rows[symbol] = simulate(ind, prices["Open"].to_numpy(), config, warmup(config))
        except Exception as e:
            failed[symbol] = str(e)

    results = pd.DataFrame.from_dict(rows, orient="index").sort_values("strategy_cagr", ascending=False)
    print_table(results)

    beat = (results.strategy_cagr > results.buy_hold_cagr).sum()
    print(f"\nStrategy beat buy and hold on {beat} of {len(results)} symbols.")
    print(f"Median CAGR: strategy {pct(results.strategy_cagr.median(), 1)}, buy and hold {pct(results.buy_hold_cagr.median(), 1)}")
    print(f"Median max drawdown: strategy {pct(results.max_drawdown.median())}, "
          f"buy and hold {pct(results.buy_hold_drawdown.median())}")
    print(f"Total trades: {results.trades.sum():.0f}, overall win rate "
          f"{pct((results.win_rate * results.trades).sum() / results.trades.sum())}")
    print(f"Total P/L ({size}): ${results.pnl_dollars.sum():,.0f}")
    print(f"Cost of one position in every symbol at today's prices: ${results.position_cost_now.sum():,.0f}")
    if (results.years < years - 0.5).any():
        short = results[results.years < years - 0.5]
        print("Shorter history: " + ", ".join(f"{s} ({y:.1f}y)" for s, y in short.years.items()))
    for symbol, error in failed.items():
        print(f"Failed: {symbol}: {error}")

    results.to_csv(RESULTS_FILE, index_label="symbol", float_format="%.4f")
    print(f"\nFull results saved to {RESULTS_FILE.name}")
    print("Columns: Win = winning trades, Avg = average trade return, CAGR = yearly growth rate,")
    print("MaxDD = largest drop from a peak, InMkt = time holding the stock, Now = position open today.")
    print("P/L $ = dollar profit. With share sizing, adjusted prices make older trades look cheaper")
    print("after stock splits, so P/L $ is mostly driven by recent years.")


# ---------- Compare: several variants, three periods ----------

def run_compare(symbols, base_config, years):
    configs = {name: validate({**base_config, **changes}) for name, changes in VARIANTS.items()}
    # Start every variant on the same day, so all of them cover the same period.
    start = max(warmup(c) for c in configs.values())

    print(f"Comparing {len(configs)} variants on {len(symbols)} symbols, last {years} years.")
    print(f"Each variant changes one setting of config.yaml. Periods: full, before {SPLIT_YEAR}, from {SPLIT_YEAR}.\n")

    stats = []  # One row per (variant, period, symbol).
    for symbol in symbols:
        try:
            prices = fetch_history(symbol, years)
        except Exception as e:
            print(f"Failed: {symbol}: {e}")
            continue
        opens = prices["Open"].to_numpy()
        split = int((prices.index.year < SPLIT_YEAR).sum())
        periods = {"Full": (start, None), f"Before {SPLIT_YEAR}": (start, split - 1), f"From {SPLIT_YEAR}": (max(split, start), None)}
        for name, config in configs.items():
            ind = compute_indicators(prices["Close"], config)
            for period, (first, last) in periods.items():
                try:
                    stats.append({"variant": name, "period": period, "symbol": symbol,
                                  **simulate(ind, opens, config, first, last)})
                except ValueError:
                    pass  # Symbol has no data in this period, for example PLTR before 2020.

    df = pd.DataFrame(stats)
    for period in df.period.unique():
        part = df[df.period == period]
        first_variant = part[part.variant == next(iter(configs))]
        print(f"===== {period}: {first_variant.symbol.nunique()} symbols, "
              f"about {first_variant.years.median():.1f} years =====")
        header = f"{'Variant':20} {'CAGR':>6} {'Beat B&H':>9} {'MaxDD':>6} {'Trades':>7} {'Win':>5} {'InMkt':>6}"
        print(header)
        print("-" * len(header))
        for name in configs:
            v = part[part.variant == name]
            win = (v.win_rate * v.trades).sum() / v.trades.sum() if v.trades.sum() else math.nan
            print(f"{name:20} {pct(v.strategy_cagr.median(), 1):>6} "
                  f"{f'{(v.strategy_cagr > v.buy_hold_cagr).sum()}/{len(v)}':>9} "
                  f"{pct(v.max_drawdown.median()):>6} {v.trades.sum():>7.0f} {pct(win):>5} "
                  f"{pct(v.time_in_market.median()):>6}")
        print(f"{'Buy and hold':20} {pct(first_variant.buy_hold_cagr.median(), 1):>6} {'':>9} "
              f"{pct(first_variant.buy_hold_drawdown.median()):>6} {'':>7} {'':>5} {'100%':>6}\n")

    print("All figures are medians across symbols, except Beat B&H (symbols where the variant's yearly")
    print("return beat buy and hold) and Trades (total). A variant that only does well in one period")
    print("is likely fit to that period. Past results do not predict future results.")


def main():
    parser = argparse.ArgumentParser(description="Backtest the trading strategy")
    parser.add_argument("symbols", nargs="*", help="symbols to test (default: all in config.yaml)")
    parser.add_argument("--years", type=int, default=10, help="years of history (default: 10)")
    parser.add_argument("--compare", action="store_true", help="compare strategy variants over three periods")
    args = parser.parse_args()

    config = load_config()
    symbols = args.symbols or config["symbols"]
    if args.compare:
        run_compare(symbols, config, args.years)
    else:
        run_single(symbols, config, args.years)


if __name__ == "__main__":
    main()
