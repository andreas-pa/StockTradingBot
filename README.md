# Stock Trading Bot

A trading bot that buys and sells US stocks on a moving average crossover signal. It trades on an [Alpaca](https://alpaca.markets) **paper trading** account, so all money is simulated.

The bot runs in Docker every weekday at 9:30 AM Eastern time. It logs every run to files on the host. Telegram sends you a daily status and an alert for every opened or closed position, and you can check the bot's status there at any time.

## How it works

1. The bot downloads the last 100 trading days of daily closing prices for each symbol from Yahoo Finance, using `yfinance`.
2. It calculates a 20-day and a 50-day simple moving average.
3. With the default settings, it compares the last two completed trading days:
   - **BUY** when the 20-day average was at or below the 50-day average on the first day and above it on the second day.
   - **SELL** when the 20-day average was at or above the 50-day average on the first day and below it on the second day.
   - **No signal** when the averages did not cross.
4. On a signal, it places a market order on Alpaca. A sell closes the whole position.

Optional settings change these rules: hold whenever the trend is up instead of only on crossover days, wait for confirmation, require a minimum gap, add a trailing stop, pause buying while the whole market is in a downtrend, or size orders in dollars. See [Strategy options](#strategy-options).

The bot ignores today's unfinished price bar while the market is open. At the 9:30 run, signals therefore come from the previous day's close, and orders fill near the opening price.

### Safety checks

- Orders always go to the paper trading account. The code sets `paper=True` and has no live trading option.
- The bot skips a buy if it already holds the stock, so a second run on the same day does not buy twice.
- It skips a symbol that still has an unfilled order.
- The bot never sells shares it does not hold, so it never opens a short position.
- It skips weekends and US market holidays, using Alpaca's market calendar.
- An error with one symbol, such as missing data or a rejected order, does not stop the run. The bot logs the error and moves to the next symbol.

## Project files

| File | Purpose |
|---|---|
| `config.yaml` | Symbols, strategy settings, order size, history length |
| `strategy.py` | Loads the config, downloads prices, and holds the trading rules shared by the bot and the backtest |
| `backtest.py` | Replays the strategy on past prices and compares variants |
| `research.py` | Backtests two alternative strategies: a market trend rule and momentum |
| `trader.py` | Connects to Alpaca, places orders, reads the account |
| `bot.py` | Runs one check of all symbols. Also works from the command line. |
| `main.py` | Docker service: daily schedule, Telegram commands, alerts, and daily status |
| `timezones.py` | Formats times in Eastern time plus your optional local time zone |
| `.env.example` | Template for API keys and Telegram settings |
| `Dockerfile`, `docker-compose.yml` | Container setup |

## Configuration

Edit `config.yaml` to change what the bot trades:

```yaml
symbols:
  - AAPL
  - MSFT
  # ...
short_window: 20   # short moving average, in trading days
long_window: 50    # long moving average, in trading days
sizing: shares     # shares or dollars
trade_size: 10     # shares per buy, when sizing is shares
trade_dollars: 1000  # dollars per buy, when sizing is dollars
history_days: 100  # trading days of price history to download
```

The bot reads this file again at every run. In Docker, the file is mounted from the host, so changes take effect without a rebuild.

### Strategy options

All options default to the original strategy. Change one at a time, and test it with the backtest first.

| Setting | Values | Effect |
|---|---|---|
| `entry_mode` | `crossover` (default), `state` | `crossover` trades only on crossover days. `state` holds whenever the short average is above the long one, so the bot catches up after missed days. Switching to `state` buys every symbol in an uptrend at the next run. |
| `confirm_days` | `1` (default) or more | A signal counts only after it has held this many days in a row |
| `min_gap_pct` | `0` (default) or more | A signal counts only when the averages differ by at least this percentage |
| `trailing_stop_pct` | `0` (default, off) or more | Sells when a close falls this percentage below the highest close since the buy. After a stop, the bot waits for a fresh crossover before buying that stock again. |
| `sizing` | `shares` (default), `dollars` | Buy `trade_size` shares, or as many whole shares as `trade_dollars` allows |
| `market_filter_average` | `0` (default, off), or a number of days such as `200` | Blocks new buys while `market_filter_symbol` closes at or below its average over this many days |
| `market_filter_symbol` | `SPY` (default) | The fund that measures the market's trend |
| `market_filter_exit` | `false` (default), `true` | With the market filter on, also sells every position while the market fund is below its average |

These options are not in the default `config.yaml`. Add a line, for example `market_filter_average: 200`, to use one. The trailing stop memory is kept in `logs/strategy_state.json`. Longer averages need more history: set `history_days` above `long_window + confirm_days`, for example 250 for a 200-day average.

## Requirements

- An Alpaca account with paper trading API keys. Find the keys at [app.alpaca.markets](https://app.alpaca.markets) under Paper Trading > API Keys.
- A Telegram bot token. Message [@BotFather](https://t.me/BotFather) in Telegram and send `/newbot`.
- Your Telegram chat ID. Message [@userinfobot](https://t.me/userinfobot) in Telegram to get it.
- Docker with Docker Compose, on a machine that is on at 9:30 AM Eastern time.

## Setup with Docker

Clone the repo and create your `.env` file:

```bash
git clone https://github.com/kmgalanakis/StockTradingBot.git
cd StockTradingBot
cp .env.example .env
chmod 600 .env
```

Open `.env` and fill in the four required values:

```
APCA_API_KEY_ID=your_paper_key_id
APCA_API_SECRET_KEY=your_paper_secret_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
```

### Local time zone (optional)

All times are in US Eastern time (ET), the market's time zone. To also see your own time, set `LOCAL_TIMEZONE` in `.env` to a [time zone name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones):

```
LOCAL_TIMEZONE=Europe/Athens
```

Logs and Telegram messages then show both times, for example `2026-10-16 09:30 ET (16:30 EEST)`. Without this setting, they show Eastern time only. An unknown name logs a warning, and the bot shows Eastern time only.

Start the bot:

```bash
docker compose up -d --build
```

Telegram sends you a "Trading bot started" message with the next run time. Send `/balance` to confirm that your Alpaca keys work.

Docker restarts the container after a crash or a server reboot, because the compose file sets `restart: unless-stopped`.

### Updating

```bash
git pull
docker compose up -d --build
```

### Stopping

```bash
docker compose down
```

## Telegram commands

The bot only answers messages from the chat ID in `.env`.

| Command | Shows |
|---|---|
| `/status` | Open positions with profit and loss |
| `/balance` | Equity, cash, buying power, and today's change |
| `/orders` | Open orders |
| `/trades` | Last 10 closed orders |
| `/last` | Result of the last scheduled run |
| `/check` | Signals right now, without placing orders |
| `/next` | Time of the next scheduled run |
| `/logs` | Last 25 lines of the bot log |
| `/help` | List of commands |

The commands also appear in the menu next to the message box in Telegram. The bot sets this menu at startup, and only your chat sees it.

### Automatic messages

| Message | When |
|---|---|
| Daily run result | After the 9:30 ET run: signals, orders placed, and errors |
| Position alert | Within about a minute of a position opening, closing, growing, or shrinking. Closing and shrinking alerts include the fill price and the profit or loss. |
| Daily status | 16:05 ET on trading days: account value, open positions, and orders filled that day |

Position alerts come from a check that compares your Alpaca positions every 60 seconds. They also cover trades you place by hand in the Alpaca dashboard. Changes that happen while the bot is stopped are not reported after a restart.

## Logs

Docker writes logs to the `logs/` folder on the host. Log times are in Eastern time, followed by your local time when `LOCAL_TIMEZONE` is set.

| File | Contents |
|---|---|
| `logs/runs/YYYY-MM-DD.log` | Everything one scheduled run did |
| `logs/bot.log` | All activity, including dry runs and errors. Rotates at 5 MB. |
| `logs/last_run.json` | Result of the last scheduled run, used by `/last` |
| `logs/strategy_state.json` | Symbols sold by a trailing stop, so the bot does not buy them straight back |

To follow the container output live:

```bash
docker compose logs -f
```

## Running without Docker

You need Python 3.9 or later.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Check for signals without placing orders:

```bash
.venv/bin/python bot.py --dry-run
```

A dry run reads your Alpaca positions, to decide between buying, holding, and selling. Without Alpaca keys in `.env`, it logs a warning and assumes you hold nothing.

Check for signals and place orders:

```bash
.venv/bin/python bot.py
```

Start the scheduled service with Telegram:

```bash
.venv/bin/python main.py
```

## Backtesting

`backtest.py` replays the strategy on past daily prices. It uses the same rule code as the live bot, in `strategy.py`, so the two cannot drift apart. A signal on one day's close is executed at the next day's open. The backtest is long only, uses split- and dividend-adjusted prices, and ignores commissions and slippage.

```bash
.venv/bin/python backtest.py              # config.yaml settings, every symbol, last 10 years
.venv/bin/python backtest.py --years 5    # a different period
.venv/bin/python backtest.py AAPL MSFT    # only these symbols
.venv/bin/python backtest.py --compare    # compare strategy variants
.venv/bin/python backtest.py --fill close # trade at the same day's close instead of the next open
```

By default, trades fill at the next day's open, like the live bot's 9:30 run. `--fill close` decides on a day's close and fills at that same close, which estimates a run shortly before 16:00 ET. It works with `--compare` too.

For each symbol it reports the number of trades, win rate, average trade return, the strategy's total and yearly return, buy-and-hold return for comparison, maximum drawdown, time in the market, and dollar profit and loss. It also saves the full results to `backtest_results.csv`.

While the strategy is out of the market, its cash earns the 13-week US Treasury bill rate, from Yahoo Finance symbol `^IRX`. The summary also shows the Sharpe ratio, which is the yearly return above cash divided by the yearly price swings, and the return during the 2008, 2020, and 2022 market declines that the period covers.

`--compare` tests variants that change settings of `config.yaml`: state mode, 50/200-day averages, 2-day confirmation, a 1% minimum gap, a 15% trailing stop, and a 200-day SPY market filter, with and without exits and combined with state mode. It runs each one over the full period, before 2022, and from 2022, and compares them with buy and hold. A variant that only does well in one period is probably fit to that period.

Past results do not predict future results. A list of today's largest companies also flatters any backtest, because it leaves out companies that shrank or failed over the same years.

### Alternative strategies

`research.py` tests two other kinds of strategy, measured the same way: cash interest, Sharpe ratio, drawdown, declines, and an early and a late period. The live bot does not use them.

```bash
.venv/bin/python research.py market-trend             # SPY, 200-day average, 20 years
.venv/bin/python research.py market-trend --symbol QQQ --average 150
.venv/bin/python research.py momentum                 # nine sector funds, top 3, 20 years
.venv/bin/python research.py momentum --universe config --top 5 --years 10
```

- **Market trend** holds one index fund while it closes above its long-term average, and holds cash otherwise. It uses the live bot's rule code, with a 1-day short average in state mode.
- **Momentum** ranks a list of symbols at the end of each month by their return over the past 12 months, skipping the most recent month. It holds the top few in equal amounts until the next month. The comparison is an equal amount in every symbol, rebalanced monthly.

The default momentum universe is nine S&P 500 sector funds that have traded since 1998. Their list is the same in every year, so the test avoids the hindsight bias of using today's largest companies. Add `--split-year` to choose where the second period starts.

## Disclaimer

This project is for learning and paper trading. It is not financial advice. A moving average crossover is a simple strategy, and past signals do not predict future results.
