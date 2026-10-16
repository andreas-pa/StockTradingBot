"""Backtests of alternative strategies, measured the same way as backtest.py.

market-trend: hold one index fund (SPY by default) while it closes above its
    long-term average, and hold cash otherwise. Signals are decided on a close
    and executed at the next open, like the live bot.

momentum: at the end of each month, rank a list of symbols by their return over
    the past months, then hold the top few in equal amounts until the next month.
    The comparison is an equal amount in every symbol, rebalanced monthly.

asset-trend: equal shares of stock, bond, gold, and commodity funds. At each month end,
    a fund's share is invested while it closes above its 10-month average, else cash.

Cash earns the 13-week Treasury bill rate. Prices are split- and dividend-adjusted.
Commissions and slippage are not modeled.

Usage:
    python research.py market-trend                       # SPY, 200-day average, 20 years
    python research.py market-trend --symbol QQQ --average 150 --years 15
    python research.py momentum                           # nine sector funds, top 3, 20 years
    python research.py momentum --universe config --top 5 --years 10
    python research.py asset-trend                        # SPY EFA IEF GLD DBC, 10-month average
"""

import argparse
import math

import pandas as pd

from backtest import DECLINES, cash_rates, curve_stats, fetch_history, pct, simulate
from strategy import compute_indicators, load_config, validate

# The nine S&P 500 sector funds that trade since 1998. Real estate (XLRE, 2015) and
# communication services (XLC, 2018) are left out so the list is the same in every year.
SECTOR_FUNDS = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"]
# US stocks, developed-market stocks outside the US, US government bonds, gold, commodities.
ASSET_FUNDS = ["SPY", "EFA", "IEF", "GLD", "DBC"]
TRADING_DAYS_PER_MONTH = 21


# ---------- Reporting ----------

def period_stats(curve, dates, cash, invested, trades, start, end):
    """Stats for curve[start:end + 1], measured from the equity before `start`."""
    start_value = curve[start - 1] if start > 0 else 1.0
    stats = curve_stats(curve[start:end + 1], dates[start:end + 1], cash[start:end + 1], start_value)
    stats["invested"] = sum(invested[start:end + 1]) / (end - start + 1)
    stats["trades"] = sum(trades[start:end + 1])
    stats["years"] = (end - start + 1) / 252
    return stats


def split_periods(dates, split_year):
    """Return {label: (first index, last index)} for the full period and both halves."""
    split = sum(d.year < split_year for d in dates)
    periods = {"Full": (0, len(dates) - 1)}
    if 0 < split < len(dates) - 1:
        periods[f"Before {split_year}"] = (0, split - 1)
        periods[f"From {split_year}"] = (split, len(dates) - 1)
    return periods


def print_report(curves, dates, cash, split_year):
    """curves: {name: (curve, invested flags, trade counts)}. Print one table per period."""
    for label, (first, last) in split_periods(dates, split_year).items():
        rows = {name: period_stats(c, dates, cash, inv, tr, first, last) for name, (c, inv, tr) in curves.items()}
        declines = [n for n in DECLINES if any(not math.isnan(r[f"decline_{n}"]) for r in rows.values())]
        print(f"===== {label}: {dates[first]:%Y-%m} to {dates[last]:%Y-%m}, {(last - first + 1) / 252:.1f} years =====")
        header = (f"{'':26} {'CAGR':>6} {'Sharpe':>6} {'MaxDD':>6} {'InMkt':>6} {'Trades':>6}"
                  + "".join(f" {n:>6}" for n in declines))
        print(header)
        print("-" * len(header))
        for name, r in rows.items():
            print(f"{name:26} {pct(r['cagr'], 1):>6} {r['sharpe']:>6.2f} {pct(r['drawdown']):>6} "
                  f"{pct(r['invested']):>6} {r['trades']:>6.0f}"
                  + "".join(f" {pct(r[f'decline_{n}']):>6}" for n in declines))
        print()
    print("CAGR = yearly growth rate. Sharpe = yearly return above cash divided by yearly price swings.")
    print("MaxDD = largest drop from a peak. InMkt = average share of money invested. Trades = buys.")
    print("Year columns = return during that market decline. Past results do not predict future results.")


def default_split_year(dates):
    return dates[0].year + (dates[-1].year - dates[0].year + 1) // 2


# ---------- Market trend ----------

def run_market_trend(symbol, average, years, split_year):
    config = validate({
        **load_config(), "short_window": 1, "long_window": average, "entry_mode": "state",
        "confirm_days": 1, "min_gap_pct": 0, "trailing_stop_pct": 0, "sizing": "shares",
        "market_filter_average": 0,
    })
    prices = fetch_history(symbol, years)
    cash = cash_rates(prices.index)
    ind = compute_indicators(prices["Close"], config)
    opens, closes = prices["Open"].to_numpy(), prices["Close"].to_numpy()
    start = average + 1

    # One run of the live bot's rules over the whole history. Each period is cut from its daily curve.
    result = simulate(ind, opens, config, start, cash=cash, daily=True)
    buy_hold = list(closes[start:] / opens[start])
    dates = prices.index[start:]

    print(f"Market trend: hold {symbol} while it closes above its {average}-day average, else cash.\n")
    print_report({
        f"{symbol} above {average}-day": result["daily"],
        f"Buy and hold {symbol}": (buy_hold, [1] * len(buy_hold), [1] + [0] * (len(buy_hold) - 1)),
    }, dates, list(cash[start:]), split_year or default_split_year(dates))
    print(f"\nCurrently {'holding' if result['open_now'] else 'in cash'}: last close {closes[-1]:.2f}, "
          f"{average}-day average {ind['long_ma'].iloc[-1]:.2f}.")


# ---------- Monthly rebalancing ----------

class MonthlyPortfolio:
    """Rebalance a portfolio of funds or stocks once a month.

    Weights are decided on the last trading day of a month and traded at the next open.
    Money not given a weight stays in cash and earns interest.
    """

    def __init__(self, opens, closes, cash, first_decision):
        self.dates = closes.index
        self.symbols = list(closes.columns)
        self.o = opens.to_numpy()
        self.c = closes.ffill().to_numpy()  # ffill: value a holding on a rare missing day
        self.raw_close = closes.to_numpy()
        self.cash = cash
        self.month_ends = [i for i in range(len(self.dates) - 1) if self.dates[i].month != self.dates[i + 1].month]
        # execution day -> decision day
        self.rebalance_days = {i + 1: i for i in self.month_ends if i >= first_decision}
        if not self.rebalance_days:
            raise ValueError("Not enough history.")
        self.start = min(self.rebalance_days)

    def available(self, d, i):
        """Symbol columns with a close on the decision day and an open on the execution day."""
        return [k for k in range(len(self.symbols))
                if not math.isnan(self.raw_close[d, k]) and not math.isnan(self.o[i, k])]

    def run(self, weights):
        """`weights(d, i)` returns {column: share of money}. Returns ((curve, invested, buys), log)."""
        o, c = self.o, self.c
        cash_balance, shares, held = 1.0, {}, set()
        curve, invested, buys, log = [], [], [], []
        for i in range(self.start, len(self.dates)):
            bought = 0
            if i in self.rebalance_days:
                value = cash_balance + sum(n * o[i, k] for k, n in shares.items())
                target = {k: w for k, w in weights(self.rebalance_days[i], i).items() if w > 0}
                bought = len(set(target) - held)
                shares = {k: value * w / o[i, k] for k, w in target.items()}
                cash_balance = value * (1 - sum(target.values()))
                held = set(target)
                log.append((self.dates[i], sorted(self.symbols[k] for k in target)))
            cash_balance *= 1 + self.cash[i]
            stock_value = sum(n * c[i, k] for k, n in shares.items())
            total = cash_balance + stock_value
            curve.append(total)
            invested.append(stock_value / total if total else 0)
            buys.append(bought)
        return (curve, invested, buys), log

    def equal_weight(self, d, i):
        chosen = self.available(d, i)
        return {k: 1 / len(chosen) for k in chosen}


def load_prices(symbols, years):
    data = {s: fetch_history(s, years) for s in symbols}
    opens = pd.DataFrame({s: d["Open"] for s, d in data.items()}).sort_index()
    closes = pd.DataFrame({s: d["Close"] for s, d in data.items()}).sort_index()
    return opens, closes


# ---------- Momentum ----------

def momentum_curves(opens, closes, cash, lookback, skip, top):
    """Simulate momentum and the equal-weight comparison.

    opens, closes: DataFrames with one column per symbol (NaN before a symbol has data).
    Returns (dates, momentum, equal weight, holdings log). Each strategy is a tuple of
    (curve, invested share, buys per day).
    """
    pf = MonthlyPortfolio(opens, closes, cash, lookback * TRADING_DAYS_PER_MONTH)

    def top_momentum(d, i):
        past = pf.raw_close[d - lookback * TRADING_DAYS_PER_MONTH]
        recent = pf.raw_close[d - skip * TRADING_DAYS_PER_MONTH]
        scores = {k: recent[k] / past[k] - 1 for k in pf.available(d, i)
                  if not math.isnan(past[k]) and not math.isnan(recent[k])}
        best = sorted(scores, key=scores.get, reverse=True)[:top]
        return {k: 1 / len(best) for k in best}

    momentum, log = pf.run(top_momentum)
    equal, _ = pf.run(pf.equal_weight)
    return pf.dates[pf.start:], momentum, equal, log


# ---------- Multi-asset trend ----------

def asset_trend_curves(opens, closes, cash, months, benchmark):
    """Simulate multi-asset trend following and its comparisons.

    Each fund gets an equal share of the money. At each month end, a fund's share is
    invested if its close is above the average of its last `months` month-end closes,
    and held in cash otherwise. Prices must start on a day when every fund has data.
    Returns (dates, {name: (curve, invested, buys)}, holdings log).
    """
    month_ends = [i for i in range(len(closes) - 1) if closes.index[i].month != closes.index[i + 1].month]
    if len(month_ends) <= months:
        raise ValueError("Not enough history for the average.")
    pf = MonthlyPortfolio(opens, closes, cash, month_ends[months - 1])
    share = 1 / len(pf.symbols)

    def trend(d, i):
        recent = [m for m in pf.month_ends if m <= d][-months:]
        weights = {}
        for k in pf.available(d, i):
            average = pf.raw_close[recent, k].mean()
            if pf.raw_close[d, k] > average:
                weights[k] = share
        return weights

    bench = pf.symbols.index(benchmark)
    trend_result, log = pf.run(trend)
    curves = {
        f"Trend, {len(pf.symbols)} funds": trend_result,
        f"Equal weight, {len(pf.symbols)} funds": pf.run(pf.equal_weight)[0],
        f"Buy and hold {benchmark}": pf.run(lambda d, i: {bench: 1.0})[0],
    }
    return pf.dates[pf.start:], curves, log


def run_asset_trend(funds, months, years, split_year):
    opens, closes = load_prices(funds, years)
    # Start when every fund has data, so the test always holds the same list.
    first_day = max(closes[f].first_valid_index() for f in funds)
    opens, closes = opens.loc[first_day:], closes.loc[first_day:]
    cash = cash_rates(closes.index)

    dates, curves, log = asset_trend_curves(opens, closes, cash, months, funds[0])
    period_cash = list(cash[len(closes) - len(dates):])

    print(f"Multi-asset trend: equal shares of {', '.join(funds)}. At each month end, a fund's share is")
    print(f"invested while it closes above its {months}-month average, and held in cash otherwise.\n")
    print_report(curves, dates, period_cash, split_year or default_split_year(dates))
    print(f"\nCurrent holdings, from {log[-1][0]:%Y-%m-%d}: {', '.join(log[-1][1]) or 'all cash'}")


def run_momentum(universe, top, lookback, skip, years, split_year):
    symbols = SECTOR_FUNDS if universe == "sectors" else load_config()["symbols"]
    opens, closes = load_prices(symbols, years)
    cash = cash_rates(closes.index)

    dates, momentum, equal, log = momentum_curves(opens, closes, cash, lookback, skip, top)
    period_cash = list(cash[len(closes) - len(dates):])

    name = "sector funds" if universe == "sectors" else "config.yaml symbols"
    print(f"Momentum: each month, hold the {top} of {len(symbols)} {name} with the best return over the "
          f"past {lookback} months, skipping the most recent {skip}.\n")
    if universe == "config":
        print("Warning: config.yaml lists today's largest companies. Picking them with hindsight")
        print("flatters this test. The sector funds universe avoids that bias.\n")
    print_report({
        f"Momentum top {top}": momentum,
        f"Equal weight all {len(symbols)}": equal,
    }, dates, period_cash, split_year or default_split_year(dates))
    print(f"\nCurrent holdings, from {log[-1][0]:%Y-%m-%d}: {', '.join(log[-1][1])}")


def main():
    parser = argparse.ArgumentParser(description="Backtest market trend and momentum strategies")
    sub = parser.add_subparsers(dest="mode", required=True)

    trend = sub.add_parser("market-trend", help="hold an index fund above its long-term average")
    trend.add_argument("--symbol", default="SPY", help="fund to trade (default: SPY)")
    trend.add_argument("--average", type=int, default=200, help="average length in days (default: 200)")
    trend.add_argument("--years", type=int, default=20, help="years of history (default: 20)")
    trend.add_argument("--split-year", type=int, help="year that starts the second period (default: middle)")

    mom = sub.add_parser("momentum", help="hold the symbols with the best recent returns")
    mom.add_argument("--universe", choices=["sectors", "config"], default="sectors",
                     help="sectors: nine S&P 500 sector funds (default). config: symbols in config.yaml")
    mom.add_argument("--top", type=int, default=3, help="number of symbols to hold (default: 3)")
    mom.add_argument("--lookback", type=int, default=12, help="months of returns to rank by (default: 12)")
    mom.add_argument("--skip", type=int, default=1, help="most recent months to skip (default: 1)")
    mom.add_argument("--years", type=int, default=20, help="years of history (default: 20)")
    mom.add_argument("--split-year", type=int, help="year that starts the second period (default: middle)")

    assets = sub.add_parser("asset-trend", help="trend following across stocks, bonds, gold, and commodities")
    assets.add_argument("--funds", nargs="+", default=ASSET_FUNDS,
                        help=f"funds to hold, the first is the buy-and-hold comparison (default: {' '.join(ASSET_FUNDS)})")
    assets.add_argument("--months", type=int, default=10, help="months in the average (default: 10)")
    assets.add_argument("--years", type=int, default=21, help="years of history (default: 21)")
    assets.add_argument("--split-year", type=int, help="year that starts the second period (default: middle)")

    args = parser.parse_args()
    if args.mode == "market-trend":
        run_market_trend(args.symbol, args.average, args.years, args.split_year)
    elif args.mode == "asset-trend":
        run_asset_trend(args.funds, args.months, args.years, args.split_year)
    else:
        run_momentum(args.universe, args.top, args.lookback, args.skip, args.years, args.split_year)


if __name__ == "__main__":
    main()
