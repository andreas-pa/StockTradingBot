# Stock Trading Bot

A trading bot that buys and sells US stocks on a moving average crossover signal. It trades on an [Alpaca](https://alpaca.markets) **paper trading** account, so all money is simulated.

The bot runs in Docker every weekday at 9:30 AM Eastern time. It logs every run to files on the host. Telegram sends you a daily status and an alert for every opened or closed position, and you can check the bot's status there at any time.

## How it works

1. The bot downloads the last 100 trading days of daily closing prices for each symbol from Yahoo Finance, using `yfinance`.
2. It calculates a 20-day and a 50-day simple moving average.
3. It compares the last two completed trading days:
   - **BUY** when the 20-day average was at or below the 50-day average on the first day and above it on the second day.
   - **SELL** when the 20-day average was at or above the 50-day average on the first day and below it on the second day.
   - **No signal** when the averages did not cross.
4. On a signal, it places a market order on Alpaca.

The bot ignores today's unfinished price bar while the market is open. At the 9:30 run, signals therefore come from the previous day's close, and orders fill near the opening price.

### Safety checks

- Orders always go to the paper trading account. The code sets `paper=True` and has no live trading option.
- The bot skips a buy if it already holds the stock, so a second run on the same day does not buy twice.
- The bot never sells shares it does not hold, so it never opens a short position.
- It skips weekends and US market holidays, using Alpaca's market calendar.
- An error with one symbol, such as missing data or a rejected order, does not stop the run. The bot logs the error and moves to the next symbol.

## Project files

| File | Purpose |
|---|---|
| `config.yaml` | Symbols, moving average periods, trade size, history length |
| `strategy.py` | Loads the config, downloads prices, detects crossovers |
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
trade_size: 5      # shares per order, for every symbol
history_days: 100  # trading days of price history to download
```

The bot reads this file again at every run. In Docker, the file is mounted from the host, so changes take effect without a rebuild.

## Requirements

- An Alpaca account with paper trading API keys. Find the keys at [app.alpaca.markets](https://app.alpaca.markets) under Paper Trading > API Keys.
- A Telegram bot token. Message [@BotFather](https://t.me/BotFather) in Telegram and send `/newbot`.
- Your Telegram chat ID. Message [@userinfobot](https://t.me/userinfobot) in Telegram to get it.
- Docker with Docker Compose, on a machine that is on at 9:30 AM Eastern time.

## Setup with Docker

Clone the repo and create your `.env` file:

```bash
git clone git@github.com:kmgalanakis/StockTradingBot.git
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

Check for signals and place orders:

```bash
.venv/bin/python bot.py
```

Start the scheduled service with Telegram:

```bash
.venv/bin/python main.py
```

## Backtesting

`backtest.py` replays the strategy on past daily prices, using the same crossover rule as the live bot. A signal on one day's close is executed at the next day's open. The backtest is long only, uses split- and dividend-adjusted prices, and ignores commissions and slippage.

```bash
.venv/bin/python backtest.py              # every symbol in config.yaml, last 10 years
.venv/bin/python backtest.py --years 5    # a different period
.venv/bin/python backtest.py AAPL MSFT    # only these symbols
```

For each symbol it reports the number of trades, win rate, average trade return, the strategy's total and yearly return, buy-and-hold return for comparison, maximum drawdown, and time in the market. It also saves the full results to `backtest_results.csv`.

Past results do not predict future results. A list of today's largest companies also flatters any backtest, because it leaves out companies that shrank or failed over the same years.

## Disclaimer

This project is for learning and paper trading. It is not financial advice. A moving average crossover is a simple strategy, and past signals do not predict future results.
