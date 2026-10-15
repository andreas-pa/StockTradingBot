"""Long-running service: runs the bot every weekday at 9:30 ET and answers Telegram commands.

Start it with:  python main.py   (Docker runs this for you)
"""

import asyncio
import html
import json
import logging
import os
from datetime import datetime, time

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, filters

import bot
import trader
from strategy import BASE_DIR, MARKET_TZ

RUN_TIME = time(9, 30, tzinfo=MARKET_TZ)
WEEKDAYS = (1, 2, 3, 4, 5)  # Telegram's job queue counts 0 as Sunday.
JOB_NAME = "daily_run"

log = logging.getLogger("bot")

HELP_TEXT = """<b>Trading bot commands</b>
/status - open positions with profit and loss
/balance - cash, equity, and buying power
/orders - open orders
/trades - last 10 closed orders
/last - result of the last scheduled run
/check - check for signals now, without trading
/next - time of the next scheduled run
/logs - last 25 lines of the bot log
/help - this list"""


# ---------- Formatting ----------

def money(value):
    return f"${float(value):,.2f}"


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
        f"Today         {'+' if change >= 0 else '-'}{money(abs(change))}",
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
    jobs = context.job_queue.get_jobs_by_name(JOB_NAME)
    if not jobs or not jobs[0].next_t:
        await reply(update, "No run is scheduled.")
        return
    next_run = jobs[0].next_t.astimezone(MARKET_TZ).strftime("%A %Y-%m-%d %H:%M ET")
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
        job = app.job_queue.get_jobs_by_name(JOB_NAME)[0]
        next_run = job.next_t.astimezone(MARKET_TZ).strftime("%A %Y-%m-%d %H:%M ET")
        log.info(f"Service started. Next run: {next_run}")
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
    app.run_polling()


if __name__ == "__main__":
    main()
