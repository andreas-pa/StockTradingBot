"""Long-running service: runs the bot every weekday at 9:30 ET and answers Telegram commands.

It also sends a daily status at 16:05 ET and an alert whenever a position opens or closes.

Start it with:  python main.py   (Docker runs this for you)
"""

import asyncio
import html
import json
import logging
import os
from datetime import datetime, time

from dotenv import load_dotenv
from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, filters

import bot
import trader
from strategy import BASE_DIR, MARKET_TZ

RUN_TIME = time(9, 30, tzinfo=MARKET_TZ)
STATUS_TIME = time(16, 5, tzinfo=MARKET_TZ)  # Daily status, 5 minutes after the close.
WEEKDAYS = (1, 2, 3, 4, 5)  # Telegram's job queue counts 0 as Sunday.
JOB_NAME = "daily_run"
POSITION_CHECK_SECONDS = 60

log = logging.getLogger("bot")

# Shown in /help and in the command menu next to Telegram's message box.
COMMAND_DESCRIPTIONS = [
    ("status", "Open positions with profit and loss"),
    ("balance", "Cash, equity, and buying power"),
    ("orders", "Open orders"),
    ("trades", "Last 10 closed orders"),
    ("last", "Result of the last scheduled run"),
    ("check", "Check for signals now, without trading"),
    ("next", "Time of the next scheduled run"),
    ("logs", "Last 25 lines of the bot log"),
    ("help", "List of commands"),
]

HELP_TEXT = "<b>Trading bot commands</b>\n" + "\n".join(
    f"/{name} - {description}" for name, description in COMMAND_DESCRIPTIONS
)


# ---------- Formatting ----------

def money(value):
    return f"${float(value):,.2f}"


def signed_money(value):
    return f"{'+' if value >= 0 else '-'}{money(abs(value))}"


def pre(text):
    return f"<pre>{html.escape(text)}</pre>"


def format_summary(summary):
    title = "Dry run" if summary["dry_run"] else "Daily run"
    lines = [f"{title} at {summary['started'][:16].replace('T', ' ')} ET"]
    for symbol, result in summary["results"].items():
        lines.append(f"{symbol:6} {result}")
    text = f"<b>{title} finished</b>\n" + pre("\n".join(lines))
    if summary["orders"]:
        text += "\n<b>Orders</b>\n" + pre("\n".join(summary["orders"]))
    if summary["errors"]:
        text += "\n<b>Errors</b>\n" + pre("\n".join(summary["errors"]))
    return text


def format_positions(positions):
    if not positions:
        return "No open positions."
    lines = [f"{'Symbol':6} {'Qty':>5} {'Entry':>9} {'Now':>9} {'P/L':>10} {'P/L %':>7}"]
    total = 0.0
    for p in positions:
        total += float(p.unrealized_pl)
        lines.append(
            f"{p.symbol:6} {float(p.qty):>5g} {float(p.avg_entry_price):>9.2f} "
            f"{float(p.current_price):>9.2f} {float(p.unrealized_pl):>10.2f} "
            f"{float(p.unrealized_plpc) * 100:>6.2f}%"
        )
    lines.append(f"Total unrealized P/L: {money(total)}")
    return "<b>Open positions</b>\n" + pre("\n".join(lines))


def format_account(account):
    change = float(account.equity) - float(account.last_equity)
    lines = [
        f"Equity        {money(account.equity)}",
        f"Cash          {money(account.cash)}",
        f"Buying power  {money(account.buying_power)}",
        f"Today         {signed_money(change)}",
    ]
    return "<b>Account</b>\n" + pre("\n".join(lines))


def format_orders(orders, title):
    if not orders:
        return f"No {title.lower()}."
    lines = []
    for o in orders:
        when = (o.filled_at or o.submitted_at).astimezone(MARKET_TZ).strftime("%m-%d %H:%M")
        price = f"@ {float(o.filled_avg_price):.2f}" if o.filled_avg_price else ""
        lines.append(f"{when} {o.side.value:4} {o.qty:>4} {o.symbol:6} {o.status.value} {price}".rstrip())
    return f"<b>{title}</b>\n" + pre("\n".join(lines))


def describe_position_changes(old, new, fill_price):
    """Compare two position snapshots and return one alert message per change.

    `fill_price(symbol, side)` returns the latest fill price for a symbol and side, or None.
    """
    messages = []
    for symbol in sorted(set(old) | set(new)):
        before = old.get(symbol, {"qty": 0.0, "entry": 0.0})
        after = new.get(symbol, {"qty": 0.0, "entry": 0.0})
        change = after["qty"] - before["qty"]
        if change == 0:
            continue

        if change > 0:
            # A new position's average entry price is its fill price.
            price = after["entry"] if before["qty"] == 0 else fill_price(symbol, "buy")
            price_text = f" at {money(price)}" if price else ""
            if before["qty"] == 0:
                messages.append(
                    f"<b>Position opened: {symbol}</b>\n"
                    f"Bought {change:g} shares{price_text}\n"
                    f"Value {money(change * after['entry'])}"
                )
            else:
                messages.append(
                    f"<b>Position increased: {symbol}</b>\n"
                    f"Bought {change:g} shares{price_text}\n"
                    f"Now holding {after['qty']:g} shares, average entry {money(after['entry'])}"
                )
            continue

        sold = -change
        price = fill_price(symbol, "sell")
        price_text = f" at {money(price)}" if price else ""
        if price:
            pnl = (price - before["entry"]) * sold
            pnl_pct = (price / before["entry"] - 1) * 100
            pnl_text = f"P/L {signed_money(pnl)} ({pnl_pct:+.2f}%)"
        else:
            pnl_text = "P/L not available yet. Check /trades."

        if after["qty"] == 0:
            messages.append(
                f"<b>Position closed: {symbol}</b>\n"
                f"Sold {sold:g} shares{price_text}\n"
                f"Entry {money(before['entry'])}\n{pnl_text}"
            )
        else:
            messages.append(
                f"<b>Position reduced: {symbol}</b>\n"
                f"Sold {sold:g} shares{price_text}\n"
                f"Entry {money(before['entry'])}\n{pnl_text}\n"
                f"Still holding {after['qty']:g} shares"
            )
    return messages


def next_run_text(job_queue):
    jobs = job_queue.get_jobs_by_name(JOB_NAME)
    if not jobs:
        return None
    # Ask the schedule rule directly. job.next_t only exists after the
    # scheduler has started, and the startup message runs before that.
    next_run = jobs[0].job.trigger.get_next_fire_time(None, datetime.now(MARKET_TZ))
    return next_run.astimezone(MARKET_TZ).strftime("%A %Y-%m-%d %H:%M ET")


# ---------- Scheduled job ----------

async def daily_run(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    try:
        client = trader.get_client()
        today = datetime.now(MARKET_TZ).date()
        if not await asyncio.to_thread(trader.is_trading_day, client, today):
            log.info(f"Market closed on {today}. Skipping run.")
            await context.bot.send_message(chat_id, f"Market closed today ({today}). No run.")
            return

        summary = await asyncio.to_thread(bot.run_once, False)
        await context.bot.send_message(chat_id, format_summary(summary), parse_mode=ParseMode.HTML)
    except Exception as e:
        log.exception(f"Scheduled run failed: {e}")
        await context.bot.send_message(chat_id, f"Scheduled run failed: {e}")


async def watch_positions(context: ContextTypes.DEFAULT_TYPE):
    """Compare positions with the last check and send an alert for each opened or closed position."""
    try:
        client = trader.get_client()
        new = await asyncio.to_thread(trader.positions_snapshot, client)
        old = context.bot_data.get("positions")
        if old is None:
            # First check after startup. Remember the positions without sending alerts.
            context.bot_data["positions"] = new
            return

        def fill_price(symbol, side):
            return trader.last_fill_price(client, symbol, side)

        messages = await asyncio.to_thread(describe_position_changes, old, new, fill_price)
    except Exception as e:
        # Keep the last snapshot, so the change is reported on the next successful check.
        log.warning(f"Position check failed: {e}")
        return

    context.bot_data["positions"] = new
    for message in messages:
        log.info(message.replace("<b>", "").replace("</b>", "").replace("\n", " | "))
        await context.bot.send_message(context.job.chat_id, message, parse_mode=ParseMode.HTML)


async def daily_status(context: ContextTypes.DEFAULT_TYPE):
    """Send account value, open positions, and today's filled orders after the market closes."""
    chat_id = context.job.chat_id
    try:
        client = trader.get_client()
        now = datetime.now(MARKET_TZ)
        if not await asyncio.to_thread(trader.is_trading_day, client, now.date()):
            return

        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        account, positions, fills = await asyncio.to_thread(
            lambda: (
                client.get_account(),
                client.get_all_positions(),
                trader.get_filled_orders_since(client, start_of_day),
            )
        )
        text = "\n".join([
            f"<b>Daily status for {now:%A %Y-%m-%d}</b>",
            format_account(account),
            format_positions(positions),
            format_orders(fills, "Orders filled today"),
        ])
        log.info(f"Daily status: equity {money(account.equity)}, {len(positions)} positions, {len(fills)} fills today")
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.exception(f"Daily status failed: {e}")
        await context.bot.send_message(chat_id, f"Daily status failed: {e}")


# ---------- Commands ----------

async def reply(update: Update, text):
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, HELP_TEXT)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    positions = await asyncio.to_thread(lambda: trader.get_client().get_all_positions())
    await reply(update, format_positions(positions))


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    account = await asyncio.to_thread(lambda: trader.get_client().get_account())
    await reply(update, format_account(account))


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = await asyncio.to_thread(lambda: trader.get_open_orders(trader.get_client()))
    await reply(update, format_orders(orders, "Open orders"))


async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = await asyncio.to_thread(lambda: trader.get_recent_orders(trader.get_client()))
    await reply(update, format_orders(orders, "Closed orders"))


async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bot.LAST_RUN_FILE.exists():
        await reply(update, "No scheduled run has finished yet.")
        return
    await reply(update, format_summary(json.loads(bot.LAST_RUN_FILE.read_text())))


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, "Checking all symbols. This takes about a minute...")
    summary = await asyncio.to_thread(bot.run_once, True)
    await reply(update, format_summary(summary))


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    next_run = next_run_text(context.job_queue)
    if not next_run:
        await reply(update, "No run is scheduled.")
        return
    await reply(update, f"Next scheduled run: {next_run}\nHolidays are skipped when the day comes.")


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_file = bot.LOG_DIR / "bot.log"
    if not log_file.exists():
        await reply(update, "No log file yet.")
        return
    tail = log_file.read_text().splitlines()[-25:]
    # Telegram messages are limited to 4096 characters.
    await reply(update, pre("\n".join(tail)[-3900:]))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error(f"Telegram command failed: {context.error}")
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(f"Command failed: {context.error}")


# ---------- Startup ----------

def main():
    load_dotenv(BASE_DIR / ".env")
    bot.setup_logging()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env.")
    chat_id = int(chat_id)

    async def on_startup(app: Application):
        next_run = next_run_text(app.job_queue)
        log.info(f"Service started. Next run: {next_run}")
        try:
            # Fill the command menu next to the message box. Only your chat sees it.
            await app.bot.set_my_commands(
                [BotCommand(name, description) for name, description in COMMAND_DESCRIPTIONS],
                scope=BotCommandScopeChat(chat_id),
            )
        except Exception as e:
            log.warning(f"Could not set the Telegram command menu: {e}")
        await app.bot.send_message(chat_id, f"Trading bot started.\nNext run: {next_run}\nSend /help for commands.")

    app = Application.builder().token(token).post_init(on_startup).build()

    # Only answer messages from your own chat, so strangers cannot query your account.
    only_me = filters.Chat(chat_id=chat_id)
    commands = {
        "start": cmd_help, "help": cmd_help, "status": cmd_status, "balance": cmd_balance,
        "orders": cmd_orders, "trades": cmd_trades, "last": cmd_last, "check": cmd_check,
        "next": cmd_next, "logs": cmd_logs,
    }
    for name, handler in commands.items():
        app.add_handler(CommandHandler(name, handler, filters=only_me))
    app.add_error_handler(on_error)

    app.job_queue.run_daily(daily_run, time=RUN_TIME, days=WEEKDAYS, name=JOB_NAME, chat_id=chat_id)
    app.job_queue.run_daily(daily_status, time=STATUS_TIME, days=WEEKDAYS, name="daily_status", chat_id=chat_id)
    app.job_queue.run_repeating(
        watch_positions, interval=POSITION_CHECK_SECONDS, first=1, name="watch_positions", chat_id=chat_id
    )
    app.run_polling()


if __name__ == "__main__":
    main()
