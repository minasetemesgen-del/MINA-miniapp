"""
ETFPL — FPL Weekly Contest Telegram Bot (with Mini App launcher)
-----------------------------------------------------------------
Same commands as before, now built on core.py so the bot and the Mini App
share one database and one set of rules. New: /app opens the Mini App,
and deadline reminders are sent automatically.

Normally this file is started for you by server.py (one service runs both
the website and the bot). To run ONLY the bot: python bot.py
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import core
from config import (
    ADMIN_TELEGRAM_ID,
    BOT_TOKEN,
    ENTRY_FEE_BIRR,
    TELEBIRR_NAME,
    TELEBIRR_NUMBER,
    WEBAPP_URL,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

EAT = timezone(timedelta(hours=3))  # East Africa Time (Ethiopia)


def fmt_deadline(deadline):
    return deadline.astimezone(EAT).strftime("%a %d %b, %H:%M EAT")


def app_keyboard(label="🚀 Open ETFPL App"):
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, web_app=WebAppInfo(url=WEBAPP_URL))]])


def is_admin(update: Update) -> bool:
    return update.effective_user.id == ADMIN_TELEGRAM_ID


# ---------------------------------------------------------------------------
# User commands
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to the FPL Weekly Contest! 🏆\n\n"
        f"Entry fee: {ENTRY_FEE_BIRR} birr per gameweek. Highest scorer that "
        "gameweek wins the prize.\n\n"
        "Tap the button below for the full app — leaderboard, league table, "
        "fixtures, your team and transfers.\n\n"
        "Or use commands:\n"
        "/register <your_FPL_team_id> - link your FPL team\n"
        "/pay - how to pay the entry fee (Telebirr)\n"
        "/deposit <transaction no. or SMS> - confirm your payment\n"
        "/mystatus - check your registration & payment status\n"
        "/leaderboard - see this gameweek's top 10\n"
        "/app - open the Mini App\n\n"
        "Pay on Telebirr, then submit your transaction number — you're entered "
        "the moment it's verified.",
        reply_markup=app_keyboard(),
    )


async def open_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WEBAPP_URL:
        await update.message.reply_text("The Mini App isn't set up yet — WEBAPP_URL is missing.")
        return
    await update.message.reply_text("Here's the app 👇", reply_markup=app_keyboard())


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /register <fpl_team_id>\n\n"
            "Find your team ID in the URL when you open 'Points' on the "
            "official FPL site, e.g. .../entry/1234567/event/1 → 1234567 is your ID."
        )
        return
    try:
        team_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That doesn't look like a valid numeric team ID.")
        return

    try:
        info = await asyncio.to_thread(core.fetch_team_info, team_id)
    except Exception:
        logger.exception("FPL team lookup failed")
        await update.message.reply_text("Couldn't reach the FPL servers just now — please try again in a moment.")
        return
    if info is None:
        await update.message.reply_text("I couldn't find an FPL team with that ID. Please double-check it.")
        return

    user = update.effective_user
    core.register_user(user.id, user.username or user.first_name, team_id, info["name"])
    await update.message.reply_text(
        f"✅ Registered! \"{info['name']}\" (ID {team_id}) is linked.\nNow run /pay to enter this gameweek.",
        reply_markup=app_keyboard(),
    )


def pay_instructions(window):
    if not TELEBIRR_NUMBER:
        return "Payments aren't set up yet — please contact the admin."
    name = f" ({TELEBIRR_NAME})" if TELEBIRR_NAME else ""
    return (
        f"💰 Entry fee: {ENTRY_FEE_BIRR} birr for Gameweek {window['gameweek']}\n"
        f"⏰ Entries close {fmt_deadline(window['deadline'])}\n\n"
        f"1. Send {ENTRY_FEE_BIRR} birr on Telebirr to:\n{TELEBIRR_NUMBER}{name}\n"
        "2. Copy the transaction number from the Telebirr SMS you get.\n"
        "3. Send it here:  /deposit <transaction number>\n"
        "    (or paste it in the app: Wallet → Deposit)\n\n"
        "You're entered as soon as it's verified."
    )


async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        window = await asyncio.to_thread(core.check_can_enter, update.effective_user.id)
    except core.ContestError as e:
        await update.message.reply_text(e.message)
        return
    except Exception:
        logger.exception("pay failed")
        await update.message.reply_text("Sorry, something went wrong — please try again in a moment.")
        return
    await update.message.reply_text(pay_instructions(window), reply_markup=app_keyboard("Open Wallet"))


async def deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /deposit <transaction number>\n\nPay first with /pay, then send the transaction "
            "number from the Telebirr SMS (or paste the whole SMS after the command)."
        )
        return
    text = " ".join(context.args)
    try:
        r = await asyncio.to_thread(core.claim_deposit, update.effective_user.id, text)
    except core.ContestError as e:
        await update.message.reply_text(e.message)
        return
    except Exception:
        logger.exception("deposit failed")
        await update.message.reply_text("Sorry, something went wrong — please try again in a moment.")
        return
    extra = f"\n💼 {r['credit']} birr extra was added to your wallet." if r["credit"] else ""
    await update.message.reply_text(
        f"✅ Payment verified! You're entered in GW{r['gameweek']}. Good luck!{extra}",
        reply_markup=app_keyboard("📊 Open app"),
    )


async def deadline_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Background job: reminds registered users who haven't entered yet, once
    ~24h and once ~3h before the FPL deadline. Users can switch this off in
    the app (🔔 → Deadline reminders)."""
    try:
        window = await asyncio.to_thread(core.get_entry_window)
    except Exception:
        logger.exception("Reminder job: could not read FPL deadline")
        return
    if not window:
        return

    gw = window["gameweek"]
    hours_left = (window["deadline"] - datetime.now(timezone.utc)).total_seconds() / 3600
    kind = 3 if hours_left <= 3 else 24 if hours_left <= 24 else None
    if kind is None:
        return

    label = "less than 3 hours" if kind == 3 else "less than 24 hours"
    title = f"GW{gw} entries close soon"
    body = f"{label.capitalize()} left to enter Gameweek {gw} ({fmt_deadline(window['deadline'])})."
    for telegram_id in core.reminder_targets(gw, kind):
        core.add_notification(telegram_id, "deadline", title, body)
        core.mark_reminded(telegram_id, gw, kind)
        try:
            await context.bot.send_message(
                telegram_id,
                f"⏰ {title}\n{body}\n\nYou can switch these reminders off in the app (🔔).",
                reply_markup=app_keyboard("Enter now"),
            )
        except Exception:
            logger.warning("Could not send reminder to %s", telegram_id)


async def my_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    me = core.get_user(user.id)
    if not me:
        await update.message.reply_text("You haven't registered yet. Use /register <fpl_team_id>.")
        return
    try:
        window = await asyncio.to_thread(core.get_entry_window)
    except Exception:
        await update.message.reply_text("Couldn't reach the FPL servers just now — please try again in a moment.")
        return

    team = f"{me['team_name']} (ID {me['team_id']})" if me["team_name"] else f"ID {me['team_id']}"
    if not window:
        await update.message.reply_text(f"FPL Team: {team}\nNo gameweek is open for entries right now.")
        return

    gw = window["gameweek"]
    entry = core.get_entry(user.id, gw)
    status = "Not entered this gameweek yet — run /pay."
    if entry and entry["paid"]:
        status = "✅ Payment confirmed, you're entered!"
    await update.message.reply_text(
        f"FPL Team: {team}\nGameweek {gw} (closes {fmt_deadline(window['deadline'])}):\n{status}",
        reply_markup=app_keyboard(),
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        gw = await asyncio.to_thread(core.get_live_gameweek)
        results = await asyncio.to_thread(core.contest_leaderboard, gw)
    except Exception:
        logger.exception("leaderboard failed")
        await update.message.reply_text("Couldn't reach the FPL servers just now — please try again in a moment.")
        return
    if not results:
        await update.message.reply_text(f"No confirmed entries yet for GW{gw}.")
        return

    lines = [f"📊 Gameweek {gw} — Top 10"]
    for r in results[:10]:
        lines.append(f"{r['rank']}. {r['username']} — {r['points']} pts")
    me = next((r for r in results if r["telegram_id"] == update.effective_user.id), None)
    if me and me["rank"] > 10:
        lines.append(f"…\n{me['rank']}. You — {me['points']} pts")
    await update.message.reply_text("\n".join(lines), reply_markup=app_keyboard("See full leaderboard"))


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------
async def announce_winner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admins only.")
        return
    try:
        gw = int(context.args[0]) if context.args else await asyncio.to_thread(core.get_live_gameweek)
        results = await asyncio.to_thread(core.contest_leaderboard, gw, 0)  # fresh scores
    except Exception:
        logger.exception("announcewinner failed")
        await update.message.reply_text("Couldn't fetch scores just now — please try again in a moment.")
        return
    if not results:
        await update.message.reply_text(f"No confirmed entries for GW{gw}.")
        return

    total_pot = len(results) * ENTRY_FEE_BIRR
    prizes = core.prize_allocation(results, total_pot)
    winners = [r for r in results if r["telegram_id"] in prizes]
    core.set_gameweek_prizes(gw, prizes)  # shows up in each winner's Wallet

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"🏆 GW{gw} winners — pot {total_pot} birr"]
    for r in winners:
        lines.append(
            f"{medals.get(r['rank'], '🏅')} #{r['rank']} {r['username']} — {r['points']} pts — "
            f"{prizes[r['telegram_id']]} birr — FPL ID {r['team_id']}"
        )
    if len(winners) < len(core.PRIZE_SPLIT):
        lines.append("(Fewer than 3 entrants — unused prize places are not paid out.)")
    await update.message.reply_text("\n".join(lines))

    for r in winners:
        try:
            await context.bot.send_message(
                r["telegram_id"],
                f"🎉 Congrats! You finished #{r['rank']} in GW{gw} with {r['points']} points. "
                f"Prize: {prizes[r['telegram_id']]} birr!",
            )
        except Exception:
            logger.warning("Could not message winner %s", r["telegram_id"])

    # Everyone who entered also gets it in their in-app notifications.
    names = ", ".join(f"{medals.get(r['rank'], '🏅')} {r['username']}" for r in winners)
    for r in results:
        core.add_notification(
            r["telegram_id"], "winner", f"GW{gw} winners",
            f"{names}. You finished #{r['rank']} with {r['points']} points.",
        )


async def parse_sms_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /parsesms <paste a Telebirr SMS> — shows what the app would read from it.
    Use it to check your real SMS format is understood."""
    if not is_admin(update):
        await update.message.reply_text("Admins only.")
        return
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Usage: /parsesms <paste the Telebirr SMS>")
        return
    p = core.parse_sms(text)
    ok = p["incoming"] and p["tx_id"] and p["amount"] is not None
    await update.message.reply_text(
        f"Transaction no.: {p['tx_id']}\nAmount: {p['amount']}\nFrom: {p['sender_name']}\n"
        f"Counts as money received: {'yes' if p['incoming'] else 'NO'}\n\n"
        + ("✅ This SMS would be accepted." if ok else "⚠️ This SMS would NOT be accepted — send me the text so I can adjust the reader.")
    )


async def add_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /addpayment <txid> <amount> — records a payment by hand (e.g. the phone was offline)."""
    if not is_admin(update):
        await update.message.reply_text("Admins only.")
        return
    try:
        tx_id, amount = context.args[0].upper(), float(context.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /addpayment <transaction number> <amount in birr>")
        return
    new = core.store_incoming(tx_id, amount, "added by admin", "manual")
    await update.message.reply_text(
        f"✅ Recorded {tx_id} ({amount:g} birr). The player can submit it now."
        if new else f"{tx_id} is already recorded."
    )


async def list_withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admins only.")
        return
    pending = core.pending_withdrawals()
    if not pending:
        await update.message.reply_text("No withdrawal requests waiting.")
        return
    for w in pending:
        await update.message.reply_text(core.withdrawal_text(w), reply_markup=withdrawal_keyboard(w["id"]))


def withdrawal_keyboard(wid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Mark paid", callback_data=f"wd:paid:{wid}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"wd:reject:{wid}"),
    ]])


async def withdrawal_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_TELEGRAM_ID:
        await q.answer("Admins only.", show_alert=True)
        return
    _, action, wid = q.data.split(":")
    paid = action == "paid"
    w = core.resolve_withdrawal(int(wid), "paid" if paid else "rejected")
    if not w:
        await q.answer("Already handled.")
        await q.edit_message_reply_markup(None)
        return
    await q.answer("Done")
    await q.edit_message_text(
        f"{q.message.text}\n\n" + ("✅ Marked as paid" if paid else "❌ Rejected — the amount went back to their wallet")
    )
    if paid:
        title, body = "Withdrawal sent 💸", f"{w['amount']} birr was sent to {w['account']} on Telebirr."
    else:
        title, body = "Withdrawal not processed", f"Your {w['amount']} birr request was rejected and returned to your wallet."
    core.add_notification(w["telegram_id"], "payment", title, body)
    try:
        await context.bot.send_message(w["telegram_id"], f"{title}\n{body}")
    except Exception:
        logger.warning("Could not message %s about their withdrawal", w["telegram_id"])


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
def build_application():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("app", open_app))
    app.add_handler(CommandHandler("register", register))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("deposit", deposit))
    app.add_handler(CommandHandler("mystatus", my_status))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("announcewinner", announce_winner))
    app.add_handler(CommandHandler("parsesms", parse_sms_cmd))
    app.add_handler(CommandHandler("addpayment", add_payment))
    app.add_handler(CommandHandler("withdrawals", list_withdrawals))
    app.add_handler(CallbackQueryHandler(withdrawal_action, pattern=r"^wd:(paid|reject):\d+$"))

    app.job_queue.run_repeating(deadline_reminders, interval=600, first=60)
    return app


def main():
    core.init_db()
    app = build_application()
    logger.info("Bot starting (bot only)...")
    app.run_polling()


if __name__ == "__main__":
    main()
