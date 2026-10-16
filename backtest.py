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
    python backtest.py --fill close    # trade at the same day's close instead of the next open
"""

import argparse
import math
from functools import lru_cache

import pandas as pd
import yfinance as yf

from strategy import BASE_DIR, compute_indicators, decide, load_config, market_filter_on, validate

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
    "Market filter": {"market_filter_average": 200},
    "Market filter + exit": {"market_filter_average": 200, "market_filter_exit": True},
    "State + filter + exit": {"entry_mode": "state", "market_filter_average": 200, "market_filter_exit": True},
}
SPLIT_YEAR = 2022  # --compare tests before and from this year separately.
FILL_NAMES = {"open": "next open", "close": "same-day close"}


# US stock market declines, from the S&P 500's peak close to its lowest close.
DECLINES = {
    "2008": ("2007-10-09", "2009-03-09"),
    "2020": ("2020-02-19", "2020-03-23"),
    "2022": ("2022-01-03", "2022-10-12"),
}


def max_drawdown(values):
    peak, worst = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        worst = max(worst, 1 - v / peak)
    return worst


@lru_cache(maxsize=1)
def _treasury_bill_rates():
    """Daily yield of 13-week US Treasury bills, by date. Cash earns this rate in the backtest."""
    irx = yf.Ticker("^IRX").history(period="max")["Close"]
    return pd.Series(irx.to_numpy() / 100, index=[d.date() for d in irx.index])


def cash_rates(index):
    """Return the daily interest rate on cash for each date in `index`, as a numpy array."""
    yearly = _treasury_bill_rates()
    dates = [d.date() for d in index]
    aligned = yearly.reindex(sorted(set(yearly.index) | set(dates))).ffill().reindex(dates).fillna(0)
    return aligned.to_numpy() / TRADING_DAYS_PER_YEAR


def sharpe_ratio(curve, cash):
    """Yearly return above cash, divided by the yearly swings of daily returns."""
    daily = pd.Series(curve).pct_change().to_numpy()[1:] - cash[1:]
    std = daily.std()
    return daily.mean() / std * math.sqrt(TRADING_DAYS_PER_YEAR) if std > 0 else math.nan


def decline_returns(curve, dates):
    """Return {decline name: return during that decline} for declines the curve fully covers."""
    series = pd.Series(curve, index=[d.date() for d in dates])
    result = {}
    for name, (start, end) in DECLINES.items():
        start, end = pd.Timestamp(start).date(), pd.Timestamp(end).date()
        if series.index[0] <= start and series.index[-1] >= end:
            result[name] = series[:end].iloc[-1] / series[:start].iloc[-1] - 1
        else:
            result[name] = math.nan
    return result


def curve_stats(curve, dates, cash, start_value=1.0):
    """CAGR, Sharpe ratio, max drawdown, and decline returns for an equity curve.

    `start_value` is the equity before the first day in `curve`.
    """
    years = len(curve) / TRADING_DAYS_PER_YEAR
    total = curve[-1] / start_value - 1
    stats = {
        "cagr": (1 + total) ** (1 / years) - 1 if total > -1 else math.nan,
        "sharpe": sharpe_ratio([start_value] + list(curve), [0.0] + list(cash)),
        "drawdown": max_drawdown([start_value] + list(curve)),
    }
    for name, ret in decline_returns(curve, dates).items():
        stats[f"decline_{name}"] = ret
    return stats


def simulate(ind, opens, config, start, end=None, cash=None, daily=False, fill="open"):
    """Run the strategy from index `start` to `end` (inclusive). Return a dict of results.

    `cash` is the daily interest rate earned while not holding, aligned with `ind`.
    With daily=True, the result also has "daily": (equity curve, invested flags, buys per day).
    fill="open" trades at the next day's open, like the live bot's 9:30 run. fill="close"
    trades at the same day's close, like a run just before the market closes.
    """
    closes = ind["close"].to_numpy()
    dates = ind.index
    rows = list(ind.itertuples())
    end = len(closes) - 1 if end is None else end
    if end <= start:
        raise ValueError("Not enough price history.")
    if cash is None:
        cash = [0.0] * len(closes)

    equity, curve, invested, buys = 1.0, [], [], []
    holding, stopped = False, False
    entry_price = entry_equity = high = entry_date = None
    shares = 0
    trades, days_in_market = [], 0

    at_close = fill == "close"
    for i in range(start, end + 1):
        if at_close:
            # Decided on today's close and filled at that close, like a run just before 16:00.
            decision_day, price = rows[i], closes[i]
        else:
            # Decided on yesterday's close and filled at today's open, like the 9:30 run.
            decision_day, price = rows[i - 1], opens[i]
        if decision_day.bearish:
            stopped = False
        action, reason = decide(decision_day, {"holding": holding, "high": high, "stopped": stopped}, config)
        bought = 0
        sold_at_close = False

        if action == "buy":
            shares = buy_quantity(config, decision_day.close)
            if shares > 0:
                bought = 1
                if at_close:
                    equity *= 1 + cash[i]  # The cash earned interest for the day before the buy.
                holding, stopped = True, False
                entry_price, entry_equity, high, entry_date = price, equity, price, dates[i]
        elif action == "sell":
            trade_return = price / entry_price - 1
            trades.append({
                "entry": entry_date, "exit": dates[i], "return": trade_return,
                "pnl": (price - entry_price) * shares, "reason": reason,
            })
            equity = entry_equity * (1 + trade_return)
            holding = False
            stopped = reason.startswith("trailing stop")
            sold_at_close = at_close

        if holding:
            days_in_market += 1
            high = max(high, closes[i])
            equity = entry_equity * closes[i] / entry_price
        elif not sold_at_close:
            equity *= 1 + cash[i]
        curve.append(equity)
        invested.append(1 if holding else 0)
        buys.append(bought)

    # Value a position that is still open at the last close.
    if holding:
        trades.append({
            "entry": entry_date, "exit": None, "return": closes[end] / entry_price - 1,
            "pnl": (closes[end] - entry_price) * shares, "reason": "still open",
        })

    days = end - start + 1
    period_dates = dates[start:end + 1]
    period_cash = list(cash[start:end + 1])
    buy_hold_curve = list(closes[start:end + 1] / opens[start])
    returns = [t["return"] for t in trades]
    strategy = curve_stats(curve, period_dates, period_cash)
    buy_hold = curve_stats(buy_hold_curve, period_dates, period_cash)

    result = {
        "years": days / TRADING_DAYS_PER_YEAR,
        "trades": len(trades),
        "win_rate": sum(r > 0 for r in returns) / len(returns) if returns else math.nan,
        "avg_trade": sum(returns) / len(returns) if returns else math.nan,
        "strategy_return": equity - 1,
        "strategy_cagr": strategy["cagr"],
        "buy_hold_return": buy_hold_curve[-1] - 1,
        "buy_hold_cagr": buy_hold["cagr"],
        "max_drawdown": strategy["drawdown"],
        "buy_hold_drawdown": buy_hold["drawdown"],
        "sharpe": strategy["sharpe"],
        "buy_hold_sharpe": buy_hold["sharpe"],
        **{f"decline_{name}": strategy[f"decline_{name}"] for name in DECLINES},
        **{f"buy_hold_decline_{name}": buy_hold[f"decline_{name}"] for name in DECLINES},
        "time_in_market": days_in_market / days,
        "open_now": holding,
        "pnl_dollars": sum(t["pnl"] for t in trades),
        "position_cost_now": closes[end] * buy_quantity(config, closes[end]),
    }
    if daily:
        result["daily"] = (curve, invested, buys)
    return result


def buy_quantity(config, price):
    if config["sizing"] == "dollars":
        return int(config["trade_dollars"] // price)
    return config["trade_size"]


def warmup(config):
    """First index where a signal can be executed."""
    days = config["long_window"] + config["confirm_days"]
    if market_filter_on(config):
        days = max(days, config["market_filter_average"] + 1)
    return days


def market_history(configs, years):
    """Closing prices of the market filter fund, or None if no config uses the filter."""
    symbols = {c["market_filter_symbol"] for c in configs if market_filter_on(c)}
    if len(symbols) > 1:
        raise ValueError("All variants must use the same market_filter_symbol.")
    return fetch_history(symbols.pop(), years)["Close"] if symbols else None


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


def run_single(symbols, config, years, fill="open"):
    size = f"${config['trade_dollars']:,} per trade" if config["sizing"] == "dollars" else f"{config['trade_size']} shares per trade"
    print(f"Backtesting {len(symbols)} symbols, last {years} years, trading at the {FILL_NAMES[fill]}")
    print(f"{config['short_window']}/{config['long_window']}-day, {config['entry_mode']} mode, "
          f"confirm {config['confirm_days']}, gap {config['min_gap_pct']:g}%, "
          f"trailing stop {config['trailing_stop_pct']:g}%, {size}\n")

    market = market_history([config], years)
    rows, failed = {}, {}
    for symbol in symbols:
        try:
            prices = fetch_history(symbol, years)
            ind = compute_indicators(prices["Close"], config, market)
            rows[symbol] = simulate(ind, prices["Open"].to_numpy(), config, warmup(config), cash=cash_rates(prices.index), fill=fill)
        except Exception as e:
            failed[symbol] = str(e)

    results = pd.DataFrame.from_dict(rows, orient="index").sort_values("strategy_cagr", ascending=False)
    print_table(results)

    beat = (results.strategy_cagr > results.buy_hold_cagr).sum()
    print(f"\nStrategy beat buy and hold on {beat} of {len(results)} symbols.")
    print(f"Median CAGR: strategy {pct(results.strategy_cagr.median(), 1)}, buy and hold {pct(results.buy_hold_cagr.median(), 1)}")
    print(f"Median max drawdown: strategy {pct(results.max_drawdown.median())}, "
          f"buy and hold {pct(results.buy_hold_drawdown.median())}")
    print(f"Median Sharpe ratio: strategy {results.sharpe.median():.2f}, buy and hold {results.buy_hold_sharpe.median():.2f}")
    for name in DECLINES:
        if results[f"decline_{name}"].notna().any():
            print(f"Median return in the {name} decline: strategy {pct(results[f'decline_{name}'].median())}, "
                  f"buy and hold {pct(results[f'buy_hold_decline_{name}'].median())}")
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
    print("Cash earns the 13-week Treasury bill rate while the strategy is out of the market.")
    print("Sharpe ratio = yearly return above cash, divided by yearly price swings. Higher is better.")


# ---------- Compare: several variants, three periods ----------

def run_compare(symbols, base_config, years, fill="open"):
    configs = {name: validate({**base_config, **changes}) for name, changes in VARIANTS.items()}
    # Start every variant on the same day, so all of them cover the same period.
    start = max(warmup(c) for c in configs.values())

    market = market_history(configs.values(), years)

    print(f"Comparing {len(configs)} variants on {len(symbols)} symbols, last {years} years, trading at the {FILL_NAMES[fill]}.")
    print(f"Each variant changes settings of config.yaml. Periods: full, before {SPLIT_YEAR}, from {SPLIT_YEAR}.\n")

    stats = []  # One row per (variant, period, symbol).
    for symbol in symbols:
        try:
            prices = fetch_history(symbol, years)
        except Exception as e:
            print(f"Failed: {symbol}: {e}")
            continue
        opens = prices["Open"].to_numpy()
        cash = cash_rates(prices.index)
        split = int((prices.index.year < SPLIT_YEAR).sum())
        periods = {"Full": (start, None), f"Before {SPLIT_YEAR}": (start, split - 1), f"From {SPLIT_YEAR}": (max(split, start), None)}
        for name, config in configs.items():
            ind = compute_indicators(prices["Close"], config, market)
            for period, (first, last) in periods.items():
                try:
                    stats.append({"variant": name, "period": period, "symbol": symbol,
                                  **simulate(ind, opens, config, first, last, cash=cash, fill=fill)})
                except ValueError:
                    pass  # Symbol has no data in this period, for example PLTR before 2020.

    df = pd.DataFrame(stats)
    for period in df.period.unique():
        part = df[df.period == period]
        first_variant = part[part.variant == next(iter(configs))]
        print(f"===== {period}: {first_variant.symbol.nunique()} symbols, "
              f"about {first_variant.years.median():.1f} years =====")
        declines = [n for n in DECLINES if part[f"decline_{n}"].notna().any()]
        header = (f"{'Variant':20} {'CAGR':>6} {'Sharpe':>6} {'Beat B&H':>9} {'MaxDD':>6} {'Trades':>7} {'Win':>5} {'InMkt':>6}"
                  + "".join(f" {n:>6}" for n in declines))
        print(header)
        print("-" * len(header))
        for name in configs:
            v = part[part.variant == name]
            win = (v.win_rate * v.trades).sum() / v.trades.sum() if v.trades.sum() else math.nan
            print(f"{name:20} {pct(v.strategy_cagr.median(), 1):>6} {v.sharpe.median():>6.2f} "
                  f"{f'{(v.strategy_cagr > v.buy_hold_cagr).sum()}/{len(v)}':>9} "
                  f"{pct(v.max_drawdown.median()):>6} {v.trades.sum():>7.0f} {pct(win):>5} "
                  f"{pct(v.time_in_market.median()):>6}"
                  + "".join(f" {pct(v[f'decline_{n}'].median()):>6}" for n in declines))
        b = first_variant
        print(f"{'Buy and hold':20} {pct(b.buy_hold_cagr.median(), 1):>6} {b.buy_hold_sharpe.median():>6.2f} {'':>9} "
              f"{pct(b.buy_hold_drawdown.median()):>6} {'':>7} {'':>5} {'100%':>6}"
              + "".join(f" {pct(b[f'buy_hold_decline_{n}'].median()):>6}" for n in declines) + "\n")

    print("All figures are medians across symbols, except Beat B&H (symbols where the variant's yearly")
    print("return beat buy and hold) and Trades (total). Year columns show the return during that")
    print("market decline. Cash earns the 13-week Treasury bill rate while out of the market.")
    print("A variant that only does well in one period is likely fit to that period.")
    print("Past results do not predict future results.")


def main():
    parser = argparse.ArgumentParser(description="Backtest the trading strategy")
    parser.add_argument("symbols", nargs="*", help="symbols to test (default: all in config.yaml)")
    parser.add_argument("--years", type=int, default=10, help="years of history (default: 10)")
    parser.add_argument("--compare", action="store_true", help="compare strategy variants over three periods")
    parser.add_argument("--fill", choices=["open", "close"], default="open",
                        help="open: trade at the next open, like the 9:30 run (default). close: trade at the same close")
    args = parser.parse_args()

    config = load_config()
    symbols = args.symbols or config["symbols"]
    if args.compare:
        run_compare(symbols, config, args.years, args.fill)
    else:
        run_single(symbols, config, args.years, args.fill)


if __name__ == "__main__":
    main()
