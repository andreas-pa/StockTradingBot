"""One run of the moving average crossover bot.

Command line use:
    python bot.py            # checks every symbol and places orders on signals
    python bot.py --dry-run  # checks every symbol, never places an order

The Docker service in main.py calls run_once() on a schedule.
"""

import argparse
import json
import logging
import os
import sys
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import strategy
import timezones
import trader
from strategy import BASE_DIR, MARKET_TZ

LOG_DIR = Path(os.getenv("LOG_DIR", BASE_DIR / "logs"))
LAST_RUN_FILE = LOG_DIR / "last_run.json"

log = logging.getLogger("bot")


class TimeZoneFormatter(logging.Formatter):
    """Show log times in New York time, plus local time when LOCAL_TIMEZONE is set."""

    def formatTime(self, record, datefmt=None):
        created = datetime.fromtimestamp(record.created, MARKET_TZ)
        return timezones.format_time(created, time_format="%H:%M:%S")


FORMATTER = TimeZoneFormatter("%(asctime)s %(levelname)-7s %(message)s")

# Stops a Telegram /check from running at the same time as the scheduled run.
_run_lock = threading.Lock()


def setup_logging():
    """Log to the console and to logs/bot.log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "runs").mkdir(exist_ok=True)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(FORMATTER)
    service_file = RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=5_000_000, backupCount=5)
    service_file.setFormatter(FORMATTER)

    log.setLevel(logging.INFO)
    log.handlers = [console, service_file]
    if timezones.local_tz_error():
        log.warning(timezones.local_tz_error())


def run_once(dry_run=False):
    """Check every symbol for a crossover and trade on signals. Return a summary dict."""
    with _run_lock:
        return _run(dry_run)


def _run(dry_run):
    started = datetime.now(MARKET_TZ)

    # Live runs also get their own log file per day, for example logs/runs/2026-10-16.log.
    run_file = None
    if not dry_run:
        run_file = logging.FileHandler(LOG_DIR / "runs" / f"{started.date()}.log")
        run_file.setFormatter(FORMATTER)
        log.addHandler(run_file)

    summary = {
        "started": started.isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "results": {},
        "orders": [],
        "errors": [],
    }

    try:
        log.info(f"========== {'DRY RUN' if dry_run else 'LIVE RUN'} started ==========")
        config = strategy.load_config()
        client = None

        for symbol in config["symbols"]:
            log.info(f"----- {symbol} -----")
            try:
                closes = strategy.fetch_prices(symbol, config["history_days"])
                signal = strategy.find_crossover(closes, config["short_window"], config["long_window"])
            except Exception as e:
                log.warning(f"Skipping {symbol}: {e}")
                summary["results"][symbol] = "error"
                summary["errors"].append(f"{symbol}: {e}")
                continue

            if signal is None:
                log.info("No crossover.")
                summary["results"][symbol] = "no signal"
                continue

            log.info(f"Crossover found: {signal.upper()} signal.")
            summary["results"][symbol] = signal.upper()
            if dry_run:
                log.info("Dry run. No order placed.")
                continue

            try:
                # Connect to Alpaca only once, and only when there is an order to place.
                if client is None:
                    client = trader.get_client()
                summary["orders"].append(trader.place_order(client, symbol, signal, config["trade_size"]))
            except Exception as e:
                log.error(f"Order for {symbol} failed: {e}")
                summary["results"][symbol] = f"{signal.upper()} (order failed)"
                summary["errors"].append(f"{symbol} order failed: {e}")
    except Exception as e:
        log.exception(f"Run failed: {e}")
        summary["errors"].append(f"Run failed: {e}")
    finally:
        signals = {s: r for s, r in summary["results"].items() if r != "no signal"}
        log.info(f"Checked {len(summary['results'])} symbols. Signals and errors: {signals or 'none'}")
        log.info("========== Run finished ==========")
        if run_file:
            log.removeHandler(run_file)
            run_file.close()

    summary["finished"] = datetime.now(MARKET_TZ).isoformat(timespec="seconds")
    if not dry_run:
        LAST_RUN_FILE.write_text(json.dumps(summary, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(description="Moving average crossover bot")
    parser.add_argument("--dry-run", action="store_true", help="show signals without placing orders")
    args = parser.parse_args()

    setup_logging()
    run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
