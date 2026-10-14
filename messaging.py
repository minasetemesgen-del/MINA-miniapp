"""
Everything that SENDS Telegram messages on behalf of the app — so the bot
commands, the website API and the Admin dashboard all send them the same way.

The key piece is send_rich(): a message with a picture is sent as a PHOTO with
the text as its CAPTION; without a picture it's a normal text message.
"""

import asyncio
import io
import logging
from datetime import datetime, timedelta, timezone

from telegram import InputFile
from telegram.error import BadRequest, Forbidden, RetryAfter

import admin_core
import core
from config import TELEBIRR_NAME, TELEBIRR_NUMBER

logger = logging.getLogger(__name__)

CAPTION_LIMIT = 1024  # Telegram's limit for a photo caption
EAT = timezone(timedelta(hours=3))  # East Africa Time (Ethiopia)
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def fmt_deadline(deadline):
    return deadline.astimezone(EAT).strftime("%a %d %b, %H:%M EAT")


async def send_rich(bot, chat_id, text, media_token="", reply_markup=None, parse_mode=None):
    """Sends `text`, as a photo with a caption if `media_token` names a stored picture.
    Never raises. Returns True if it was delivered."""
    media = await asyncio.to_thread(core.get_media, media_token) if media_token else None
    for attempt in (1, 2):  # the second try only happens after Telegram says "slow down"
        try:
            if media:
                try:
                    return await _send_photo(bot, chat_id, text, media_token, media, reply_markup, parse_mode)
                except (Forbidden, RetryAfter):
                    raise
                except Exception:
                    logger.warning("Photo could not be sent to %s — sending text only", chat_id, exc_info=True)
            await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
            return True
        except RetryAfter as e:
            if attempt == 2:
                return False
            await asyncio.sleep(min(float(getattr(e, "retry_after", 2)) + 0.5, 30))
        except Forbidden:
            return False  # the player blocked the bot / never started it
        except Exception:
            logger.warning("Could not send a message to %s", chat_id, exc_info=True)
            return False
    return False


async def _send_photo(bot, chat_id, text, token, media, reply_markup, parse_mode):
    mime, data, file_id = media
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "jpg")
    short = len(text) <= CAPTION_LIMIT
    kwargs = {"caption": text, "parse_mode": parse_mode, "reply_markup": reply_markup} if short else {}
    msg = None
    if file_id:  # Telegram already has this picture — sending by id is instant
        try:
            msg = await bot.send_photo(chat_id, file_id, **kwargs)
        except BadRequest:
            msg = None  # id no longer valid; upload the bytes again
    if msg is None:
        msg = await bot.send_photo(chat_id, InputFile(io.BytesIO(data), filename=f"picture.{ext}"), **kwargs)
        try:
            new_id = msg.photo[-1].file_id
            if new_id:
                await asyncio.to_thread(core.set_media_file_id, token, new_id)
        except Exception:
            pass  # caching is only an optimisation
    if not short:  # too long for a caption: picture first, then the full text
        await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    return True


async def send_template(bot, chat_id, key, reply_markup=None, **vars):
    t = await asyncio.to_thread(core.get_template, key)
    return await send_rich(bot, chat_id, core.render(t["text"], **vars), t["media_token"], reply_markup)


# --- the values each template can use ---------------------------------------
def split_text():
    return " / ".join(f"{p:g}%" for p in core.get_setting("prize_percents"))


def vars_welcome():
    return {"fee": core.entry_fee(), "winners": len(core.get_setting("prize_percents")), "split": split_text()}


def vars_pay(window):
    number = TELEBIRR_NUMBER
    name = TELEBIRR_NAME
    return {
        "fee": core.entry_fee(), "gw": window["gameweek"], "deadline": fmt_deadline(window["deadline"]),
        "number": number, "name": name, "account": f"{number} ({name})" if name else number,
    }


def sample_vars(key):
    """Example values so the admin can preview / test-send a message."""
    number = TELEBIRR_NUMBER or "0912345678"
    name = TELEBIRR_NAME or "Account name"
    v = {
        "fee": core.entry_fee(), "winners": len(core.get_setting("prize_percents")), "split": split_text(),
        "gw": 8, "deadline": "Sat 18 Oct, 14:30 EAT", "number": number, "name": name, "account": f"{number} ({name})",
        "credit": 50, "credit_line": "\n💼 50 birr extra was added to your wallet.",
        "left": "less than 3 hours", "rank": 1, "points": 91, "prize": 350,
    }
    if key == "winners_all":
        v["winners"] = "🥇 Selam — 91 pts\n🥈 Abebe — 88 pts\n🥉 Dawit — 85 pts"
    return v


# --- alerts and shared admin actions ---------------------------------------------
async def notify_admins(bot, text, reply_markup=None):
    for admin_id in core.admin_ids():
        await send_rich(bot, admin_id, text, reply_markup=reply_markup)


async def process_withdrawal(bot, wid, paid, admin_id):
    """Admin marks a withdrawal paid or rejected (from the Telegram buttons OR the dashboard).
    Returns the withdrawal, or None if it was already handled."""
    w = await asyncio.to_thread(core.resolve_withdrawal, wid, "paid" if paid else "rejected")
    if not w:
        return None
    core.audit(admin_id, "withdrawal_paid" if paid else "withdrawal_rejected", f"#{wid}: {w['amount']} birr to {w['account']}")
    if paid:
        title, body = "Withdrawal sent 💸", f"{w['amount']} birr was sent to {w['account']} on Telebirr."
    else:
        title, body = "Withdrawal not processed", f"Your {w['amount']} birr request was rejected and returned to your wallet."
    core.add_notification(w["telegram_id"], "payment", title, body)
    if bot:
        await send_rich(bot, w["telegram_id"], f"{title}\n{body}")
    return w


async def run_announcement(bot, gameweek, admin_id):
    """Announces a gameweek's winners: saves prizes, congratulates winners, adds in-app
    notifications for everyone entered (and, if switched on in Settings, messages them all).
    Returns the preview dict plus the admin summary text."""
    prev = await asyncio.to_thread(admin_core.announcement_preview, gameweek)
    results, prizes, winners = prev["results"], prev["prizes"], prev["winners"]
    if not results:
        raise core.ContestError("no_entries", f"No confirmed entries for GW{gameweek}.")
    core.set_gameweek_prizes(gameweek, prizes)  # shows up in each winner's Wallet
    core.audit(admin_id, "announce", f"GW{gameweek}: pot {prev['pot']}, " + ", ".join(
        f"#{w['rank']} {w['name']} {w['prize']}" for w in winners))

    lines = [f"🏆 GW{gameweek} winners — pot {prev['pot']} birr"]
    for w in winners:
        lines.append(f"{MEDALS.get(w['rank'], '🏅')} #{w['rank']} {w['name']} — {w['points']} pts — {w['prize']} birr — FPL ID {w['team_id']}")
    if len(winners) < prev["places"]:
        lines.append(f"(Fewer than {prev['places']} entrants — unused prize places are not paid out.)")
    summary = "\n".join(lines)

    if bot:
        for w in winners:
            await send_template(bot, w["telegram_id"], "winner_dm", gw=gameweek, rank=w["rank"], points=w["points"], prize=w["prize"])

    names = ", ".join(f"{MEDALS.get(w['rank'], '🏅')} {w['name']}" for w in winners)
    for r in results:
        core.add_notification(r["telegram_id"], "winner", f"GW{gameweek} winners",
                              f"{names}. You finished #{r['rank']} with {r['points']} points.")
    if bot and core.get_setting("announce_winners_to_all"):
        board = "\n".join(f"{MEDALS.get(w['rank'], '🏅')} {w['name']} — {w['points']} pts" for w in winners)
        winner_ids = {w["telegram_id"] for w in winners}
        for r in results:
            if r["telegram_id"] not in winner_ids:  # winners already got their own message
                await send_template(bot, r["telegram_id"], "winners_all", gw=gameweek, winners=board)
                await asyncio.sleep(0.05)
    return {**prev, "summary": summary}


async def run_broadcast(bot, broadcast_id):
    """Sends a saved broadcast to its audience, ~15 messages a second (Telegram's limit is 30)."""
    b = await asyncio.to_thread(admin_core.get_broadcast, broadcast_id)
    sent = failed = 0
    try:
        ids = await asyncio.to_thread(admin_core.audience_ids, b["audience"])
        for i, chat_id in enumerate(ids, 1):
            if await send_rich(bot, chat_id, b["text"], b["media_token"]):
                sent += 1
            else:
                failed += 1
            if i % 10 == 0:
                await asyncio.to_thread(admin_core.update_broadcast, broadcast_id, sent, failed)
            await asyncio.sleep(0.07)
        await asyncio.to_thread(admin_core.update_broadcast, broadcast_id, sent, failed, "done")
    except Exception:
        logger.exception("Broadcast %s failed part-way", broadcast_id)
        await asyncio.to_thread(admin_core.update_broadcast, broadcast_id, sent, failed, "error")
