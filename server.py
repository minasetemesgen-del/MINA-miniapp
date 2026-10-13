"""
ETFPL Mini App server.

One service does everything:
  • serves the website (static/) that Telegram opens as a Mini App
  • exposes a small JSON API (/api/...) that the website calls
  • runs the Telegram bot in the background (same process, same database)

Start it with:  uvicorn server:app --host 0.0.0.0 --port $PORT
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import core
import fpl_views
from config import (
    ADMIN_TELEGRAM_ID, BOT_TOKEN, DEV_MODE, ENTRY_FEE_BIRR, RUN_BOT,
    SMS_WEBHOOK_SECRET, TELEBIRR_NAME, TELEBIRR_NUMBER, VERIFY_ET_API_KEY, WEBAPP_URL,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger("server")

STATIC_DIR = Path(__file__).parent / "static"
INIT_DATA_MAX_AGE = 24 * 3600  # seconds


# ---------------------------------------------------------------------------
# Startup / shutdown — runs the Telegram bot alongside the website
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    core.init_db()
    if not VERIFY_ET_API_KEY and not SMS_WEBHOOK_SECRET:
        logger.warning(
            "No payment verification is set up: add VERIFY_ET_API_KEY on Railway (see README), "
            "otherwise players' deposits can't be confirmed automatically."
        )
    elif VERIFY_ET_API_KEY and not TELEBIRR_NUMBER:
        logger.warning("VERIFY_ET_API_KEY is set but TELEBIRR_NUMBER is not — deposits will be refused until it is.")
    tg_app = None
    if RUN_BOT:
        from telegram import MenuButtonWebApp, WebAppInfo

        import bot

        tg_app = bot.build_application()
        app.state.tg_app = tg_app  # lets API handlers message the admin
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling()
        if WEBAPP_URL:
            try:
                await tg_app.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(text="ETFPL", web_app=WebAppInfo(url=WEBAPP_URL))
                )
                logger.info("Menu button set to %s", WEBAPP_URL)
            except Exception:
                logger.exception("Could not set the menu button")
        else:
            logger.warning("WEBAPP_URL is not set — the app button will not appear in Telegram yet.")
        logger.info("Bot started")
    yield
    if tg_app:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()


app = FastAPI(title="ETFPL Mini App", lifespan=lifespan)


@app.exception_handler(core.ContestError)
async def contest_error_handler(request: Request, exc: core.ContestError):
    return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=400)


@app.exception_handler(requests.RequestException)
async def upstream_error_handler(request: Request, exc: requests.RequestException):
    logger.warning("Upstream request failed: %s", exc)
    return JSONResponse(
        {"detail": "Couldn't reach the FPL servers. Please try again in a moment."}, status_code=502
    )


# ---------------------------------------------------------------------------
# Telegram authentication
# ---------------------------------------------------------------------------
def validate_init_data(init_data: str) -> dict:
    """Checks the signed `initData` Telegram gives the Mini App, exactly as
    described in Telegram's docs. This is what proves a request really comes
    from that Telegram user — without it, anyone could claim to be anyone."""
    if not init_data:
        raise HTTPException(401, "Please open this app from inside Telegram.")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Invalid Telegram session.")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(401, "Invalid Telegram session.")

    try:
        age = time.time() - int(pairs.get("auth_date", "0"))
    except ValueError:
        raise HTTPException(401, "Invalid Telegram session.")
    if age > INIT_DATA_MAX_AGE:
        raise HTTPException(401, "Session expired — please close and reopen the app.")

    try:
        user = json.loads(pairs["user"])
        user["id"] = int(user["id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(401, "Invalid Telegram session.")
    return user


def current_user(
    x_telegram_init_data: str = Header(default=""),
    x_dev_user: str = Header(default=""),
) -> dict:
    # Browser testing only — enabled with DEV_MODE=1, never on the live server.
    if DEV_MODE and x_dev_user.isdigit():
        return {"id": int(x_dev_user), "first_name": "Dev", "last_name": "User", "username": f"dev{x_dev_user}"}
    return validate_init_data(x_telegram_init_data)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_admin(user):
    return user["id"] == ADMIN_TELEGRAM_ID


def _display_name(user):
    return user.get("username") or user.get("first_name") or "Player"


# ---------------------------------------------------------------------------
# API — home / contest
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/home")
def home(user: dict = Depends(current_user)):
    tid = user["id"]
    admin = _is_admin(user)
    me = core.get_user(tid)
    window = core.get_entry_window()
    live_gw = core.get_live_gameweek()

    out = {
        "user": {"id": tid, "name": _display_name(user), "first_name": user.get("first_name")},
        "fee": ENTRY_FEE_BIRR,
        "is_admin": admin,
        "registered": bool(me),
        "team": {"id": me["team_id"], "name": me["team_name"]} if me else None,
        "entry": None,
        "live": None,
        "unread": core.unread_count(tid),
    }

    if window:
        gw = window["gameweek"]
        entry = core.get_entry(tid, gw)
        n = core.count_paid_entries(gw)
        out["entry"] = {
            "gameweek": gw,
            "deadline": _iso(window["deadline"]),
            "paid": bool(entry and entry["paid"]),
            "pending": bool(entry and not entry["paid"]),
            "entrants": n,
        }
        # Money figures are for the admin only.
        if admin:
            out["entry"]["pot"] = n * ENTRY_FEE_BIRR
            out["entry"]["prize"] = round(n * ENTRY_FEE_BIRR * core.PRIZE_SHARE)

    if live_gw:
        board = core.contest_leaderboard(live_gw)
        mine = next((r for r in board if r["telegram_id"] == tid), None)
        out["live"] = {
            "gameweek": live_gw,
            "entrants": len(board),
            "entered": bool(mine),
            "rank": mine["rank"] if mine else None,
            "points": mine["points"] if mine else None,
        }
    return out


class RegisterBody(BaseModel):
    team_id: int


@app.post("/api/register")
def register(body: RegisterBody, user: dict = Depends(current_user)):
    if body.team_id <= 0:
        raise core.ContestError("bad_id", "That doesn't look like a valid FPL team ID.")
    info = core.fetch_team_info(body.team_id)
    if info is None:
        raise core.ContestError("not_found", "I couldn't find an FPL team with that ID. Please double-check it.")
    core.register_user(user["id"], _display_name(user), body.team_id, info["name"])
    return {"ok": True, "team": {"id": body.team_id, "name": info["name"]}}


class DepositBody(BaseModel):
    text: str


async def alert_admin(err):
    """Tells the admin on Telegram about a problem on THEIR side (e.g. verify.et
    out of credits). Players never see this; it's sent at most once an hour."""
    text = core.admin_alert_for(err)
    tg_app = getattr(app.state, "tg_app", None)
    if text and tg_app:
        try:
            await tg_app.bot.send_message(ADMIN_TELEGRAM_ID, text)
        except Exception:
            logger.exception("Could not send the admin alert")


@app.post("/api/deposit")
async def deposit(body: DepositBody, user: dict = Depends(current_user)):
    """The player pays on Telebirr, then submits the transaction number (or the
    whole SMS). verify.et confirms the payment reached our account; then they're in."""
    if len(body.text) > 2000:
        raise core.ContestError("too_long", "That's too long — paste just the Telebirr SMS or the transaction number.")
    try:
        result = await asyncio.to_thread(core.claim_deposit, user["id"], body.text)
    except core.ContestError as e:
        await alert_admin(e)
        raise
    return {"ok": True, **result}


class WithdrawBody(BaseModel):
    amount: int
    account: str


@app.post("/api/withdraw")
async def withdraw(body: WithdrawBody, user: dict = Depends(current_user)):
    """Only people with money in their wallet (winners) can get past this."""
    w = core.request_withdrawal(user["id"], body.amount, body.account)
    tg_app = getattr(app.state, "tg_app", None)
    if tg_app:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        try:
            await tg_app.bot.send_message(
                ADMIN_TELEGRAM_ID,
                core.withdrawal_text(core.get_withdrawal(w["id"])),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Mark paid", callback_data=f"wd:paid:{w['id']}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"wd:reject:{w['id']}"),
                ]]),
            )
        except Exception:
            logger.exception("Could not notify the admin about withdrawal %s", w["id"])
    return {"ok": True, "id": w["id"], "balance": w["balance"]}


@app.post("/api/sms-hook")
async def sms_hook(request: Request):
    """Called by the SMS-forwarder app on your receiving phone for every SMS.
    Protected by SMS_WEBHOOK_SECRET so nobody can fake a payment."""
    if not SMS_WEBHOOK_SECRET:
        raise HTTPException(503, "SMS webhook is not configured.")
    given = request.headers.get("x-webhook-secret") or request.query_params.get("secret") or ""
    if not hmac.compare_digest(given.encode(), SMS_WEBHOOK_SECRET.encode()):
        raise HTTPException(401, "Bad secret.")
    raw = await request.body()
    try:
        data = json.loads(raw)
    except ValueError:
        data = dict(parse_qsl(raw.decode("utf-8", "ignore")))
    if not isinstance(data, dict):
        raise HTTPException(400, "Expected a JSON object.")

    def pick(*keys):
        for k in keys:
            if data.get(k):
                return str(data[k])
        return ""

    sender = pick("sender", "from", "address", "number", "phone")
    message = pick("message", "text", "body", "content", "sms")
    result = core.handle_sms(sender, message)  # always 200, so the phone app doesn't keep retrying
    return {"ok": True, **result}


@app.get("/api/leaderboard")
def leaderboard(user: dict = Depends(current_user)):
    gw = core.get_live_gameweek()
    board = core.contest_leaderboard(gw) if gw else []
    tid = user["id"]
    admin = _is_admin(user)
    pot = len(board) * ENTRY_FEE_BIRR
    prizes = core.prize_allocation(board, pot)

    def row(r):
        item = {
            "rank": r["rank"],
            "name": r["username"],
            "team_name": r["team_name"],
            "points": r["points"],
            "is_me": r["telegram_id"] == tid,
            "prize": prizes.get(r["telegram_id"], 0),
        }
        if admin and r["telegram_id"] in prizes:
            item["fpl_id"] = r["team_id"]  # winners' FPL IDs are for the admin only
        return item

    top = [row(r) for r in board[:10]]
    mine = next((r for r in board if r["telegram_id"] == tid), None)
    me = row(mine) if mine and mine["rank"] > 10 else None
    out = {
        "gameweek": gw,
        "entrants": len(board),
        "top": top,
        "me": me,
        "is_admin": admin,
        # 1st / 2nd / 3rd prize amounts — everyone sees these
        "prizes": [round(pot * s) for s in core.PRIZE_SPLIT],
    }
    if admin:
        out["pot"] = pot  # pot is admin-only
    return out


# ---------------------------------------------------------------------------
# API — Premier League data
# ---------------------------------------------------------------------------
@app.get("/api/table")
def league_table(user: dict = Depends(current_user)):
    return {"rows": fpl_views.build_table()}


@app.get("/api/fixtures")
def fixtures(gw: Optional[int] = None, user: dict = Depends(current_user)):
    return fpl_views.build_fixtures(gw)


@app.get("/api/my-team")
def my_team(user: dict = Depends(current_user)):
    me = core.get_user(user["id"])
    if not me:
        return {"registered": False}
    data = fpl_views.build_my_team(me["team_id"])
    data["registered"] = True
    data["team"] = {"id": me["team_id"], "name": me["team_name"]}
    return data


@app.get("/api/transfers")
def transfers(user: dict = Depends(current_user)):
    me = core.get_user(user["id"])
    data = fpl_views.build_transfers(me["team_id"] if me else None)
    data["registered"] = bool(me)
    return data


@app.get("/api/stats")
def stats(user: dict = Depends(current_user)):
    return fpl_views.build_stats()


@app.get("/api/news")
def news(user: dict = Depends(current_user)):
    return fpl_views.build_news()


@app.get("/api/wallet")
def wallet(user: dict = Depends(current_user)):
    data = core.wallet_data(user["id"])
    data["fee"] = ENTRY_FEE_BIRR
    data["account"] = {"name": TELEBIRR_NAME, "number": TELEBIRR_NUMBER}
    try:
        w = core.check_can_enter(user["id"])
        data["deposit"] = {"ok": True, "gameweek": w["gameweek"], "deadline": _iso(w["deadline"])}
    except core.ContestError as e:
        data["deposit"] = {"ok": False, "code": e.code, "message": e.message}
    return data


# ---------------------------------------------------------------------------
# API — notifications & settings
# ---------------------------------------------------------------------------
@app.get("/api/notifications")
def notifications(user: dict = Depends(current_user)):
    tid = user["id"]
    return {
        "items": core.list_notifications(tid),
        "unread": core.unread_count(tid),
        "settings": {"notify_deadline": core.get_notify_deadline(tid)},
    }


@app.post("/api/notifications/read")
def notifications_read(user: dict = Depends(current_user)):
    core.mark_all_read(user["id"])
    return {"ok": True}


class SettingsBody(BaseModel):
    notify_deadline: bool


@app.post("/api/settings")
def settings(body: SettingsBody, user: dict = Depends(current_user)):
    core.set_notify_deadline(user["id"], body.notify_deadline)
    return {"ok": True, "notify_deadline": body.notify_deadline}


# ---------------------------------------------------------------------------
# The website itself (must be mounted last)
# ---------------------------------------------------------------------------
class FreshStaticFiles(StaticFiles):
    """Tells Telegram/the browser to re-check files on every open, so an
    update is picked up immediately instead of an old app.js being reused."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", FreshStaticFiles(directory=STATIC_DIR, html=True), name="static")
