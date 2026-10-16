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
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import core
import messaging
from config import ADMIN_TELEGRAM_ID, BOT_TOKEN, TELEBIRR_NUMBER, WEBAPP_URL

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

fmt_deadline = messaging.fmt_deadline


def app_keyboard(label="🚀 Open ETFPL App"):
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, web_app=WebAppInfo(url=WEBAPP_URL))]])


def is_admin(update: Update) -> bool:
    return core.is_admin_id(update.effective_user.id)


# ---------------------------------------------------------------------------
# User commands
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Text (and an optional picture) come from Admin → Messages.
    await messaging.send_template(
        context.bot, update.effective_chat.id, "welcome", reply_markup=app_keyboard(), **messaging.vars_welcome()
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
        core.check_can_register(update.effective_user.id)
        core.check_can_change_team(update.effective_user.id, team_id)
    except core.ContestError as e:
        await update.message.reply_text(e.message)
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


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/logout — unlink the FPL team (e.g. a wrong ID) so another can be linked."""
    try:
        old = await asyncio.to_thread(core.unlink_team, update.effective_user.id)
    except core.ContestError as e:
        await update.message.reply_text(e.message)
        return
    await update.message.reply_text(
        f"✅ Logged out of \"{old['team_name']}\" (ID {old['team_id']}).\n"
        "Link the right team with /register <your_FPL_team_id> (or on the app's Home screen).\n"
        "Reminder: if you already paid for a gameweek, link your team again before its deadline.",
        reply_markup=app_keyboard(),
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
    if not TELEBIRR_NUMBER:
        await update.message.reply_text("Payments aren't set up yet — please contact the admin.")
        return
    await messaging.send_template(
        context.bot, update.effective_chat.id, "pay_instructions",
        reply_markup=app_keyboard("Open Wallet"), **messaging.vars_pay(window),
    )


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
        alert = core.admin_alert_for(e)
        if alert:
            await messaging.send_rich(context.bot, ADMIN_TELEGRAM_ID, alert)
        return
    except Exception:
        logger.exception("deposit failed")
        await update.message.reply_text("Sorry, something went wrong — please try again in a moment.")
        return
    extra = f"\n💼 {r['credit']} birr extra was added to your wallet." if r["credit"] else ""
    await messaging.send_template(
        context.bot, update.effective_chat.id, "deposit_success", reply_markup=app_keyboard("📊 Open app"),
        gw=r["gameweek"], credit=r["credit"], credit_line=extra,
    )


async def deadline_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Background job: reminds registered users who haven't entered yet, at the times set in
    Admin → Settings (default ~24h and ~3h before the FPL deadline). Players can switch
    this off in the app (🔔 → Deadline reminders)."""
    hours = core.get_setting("reminder_hours")
    if not hours or core.get_setting("maintenance"):
        return
    try:
        window = await asyncio.to_thread(core.get_entry_window)
    except Exception:
        logger.exception("Reminder job: could not read FPL deadline")
        return
    if not window:
        return

    gw = window["gameweek"]
    hours_left = (window["deadline"] - datetime.now(timezone.utc)).total_seconds() / 3600
    due = [h for h in hours if hours_left <= h]
    if hours_left <= 0 or not due:
        return
    kind = min(due)  # the closest reminder time we have passed

    left = f"less than {kind} hour{'s' if kind != 1 else ''}"
    title = f"GW{gw} entries close soon"
    body = f"{left.capitalize()} left to enter Gameweek {gw} ({fmt_deadline(window['deadline'])})."
    for telegram_id in core.reminder_targets(gw, kind):
        core.add_notification(telegram_id, "deadline", title, body)
        core.mark_reminded(telegram_id, gw, kind)
        await messaging.send_template(
            context.bot, telegram_id, "reminder", reply_markup=app_keyboard("Enter now"),
            gw=gw, left=left.capitalize(), deadline=fmt_deadline(window["deadline"]),
        )


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
        result = await messaging.run_announcement(context.bot, gw, update.effective_user.id)
    except core.ContestError as e:
        await update.message.reply_text(e.message)
        return
    except Exception:
        logger.exception("announcewinner failed")
        await update.message.reply_text("Couldn't fetch scores just now — please try again in a moment.")
        return
    await update.message.reply_text(result["summary"])


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


async def verify_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /verifytest <transaction number or SMS> — asks verify.et and shows the verdict.
    Nothing is stored and nobody is charged."""
    if not is_admin(update):
        await update.message.reply_text("Admins only.")
        return
    text = update.message.text.partition(" ")[2].strip()
    tx_id = core.extract_tx_id(text) if text else None
    if not tx_id:
        await update.message.reply_text("Usage: /verifytest <transaction number>")
        return
    try:
        r = await asyncio.to_thread(core.verify_et_lookup, tx_id)
    except core.ContestError as e:
        await update.message.reply_text(f"❌ verify.et problem ({e.code}): {e.alert or e.message}")
        return
    except Exception as e:
        logger.exception("verifytest failed")
        await update.message.reply_text(f"❌ Unexpected error: {e}")
        return
    if r is None:
        await update.message.reply_text(f"{tx_id}: verify.et has no such payment.")
        return
    try:
        core.validate_receipt(r)
        verdict = "✅ Would be ACCEPTED."
    except core.ContestError as e:
        verdict = f"⛔ Would be REJECTED ({e.code}): {e.message}"
    await update.message.reply_text(
        f"Transaction: {tx_id}\nAmount: {r['amount']} {r.get('currency') or ''}\n"
        f"From: {r.get('sender_name')}\nTo: {r.get('receiver_name')} {r.get('receiver_account')}\n"
        f"Paid at: {r.get('paid_at')}\n\n{verdict}"
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
    if not core.is_admin_id(q.from_user.id):
        await q.answer("Admins only.", show_alert=True)
        return
    _, action, wid = q.data.split(":")
    paid = action == "paid"
    w = await messaging.process_withdrawal(context.bot, int(wid), paid, q.from_user.id)
    if not w:
        await q.answer("Already handled.")
        await q.edit_message_reply_markup(None)
        return
    await q.answer("Done")
    await q.edit_message_text(
        f"{q.message.text}\n\n" + ("✅ Marked as paid" if paid else "❌ Rejected — the amount went back to their wallet")
    )


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
def build_application():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("app", open_app))
    app.add_handler(CommandHandler("register", register))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("deposit", deposit))
    app.add_handler(CommandHandler("mystatus", my_status))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("announcewinner", announce_winner))
    app.add_handler(CommandHandler("parsesms", parse_sms_cmd))
    app.add_handler(CommandHandler("addpayment", add_payment))
    app.add_handler(CommandHandler("verifytest", verify_test))
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
