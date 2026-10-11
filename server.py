"""
ETFPL Mini App server.

One service does everything:
  • serves the website (static/) that Telegram opens as a Mini App
  • exposes a small JSON API (/api/...) that the website calls
  • runs the Telegram bot in the background (same process, same database)

Start it with:  uvicorn server:app --host 0.0.0.0 --port $PORT
"""

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
from config import ADMIN_TELEGRAM_ID, BOT_TOKEN, DEV_MODE, ENTRY_FEE_BIRR, RUN_BOT, WEBAPP_URL

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
    tg_app = None
    if RUN_BOT:
        from telegram import MenuButtonWebApp, WebAppInfo

        import bot

        tg_app = bot.build_application()
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


@app.post("/api/pay")
def pay(user: dict = Depends(current_user)):
    result = core.start_payment(user["id"], user.get("first_name"), user.get("last_name"))
    return {
        "checkout_url": result["checkout_url"],
        "gameweek": result["gameweek"],
        "deadline": _iso(result["deadline"]),
    }


@app.post("/api/pay/check")
def pay_check(user: dict = Depends(current_user)):
    """The app calls this while the user is paying, so the entry is confirmed
    the instant Chapa says yes instead of waiting for the background job."""
    confirmed = core.confirm_pending_for_user(user["id"])
    window = core.get_entry_window()
    paid = False
    if window:
        entry = core.get_entry(user["id"], window["gameweek"])
        paid = bool(entry and entry["paid"])
    return {"paid": paid, "confirmed": confirmed}


@app.get("/api/leaderboard")
def leaderboard(user: dict = Depends(current_user)):
    gw = core.get_live_gameweek()
    board = core.contest_leaderboard(gw) if gw else []
    tid = user["id"]
    top = [
        {
            "rank": r["rank"],
            "name": r["username"],
            "team_name": r["team_name"],
            "points": r["points"],
            "is_me": r["telegram_id"] == tid,
        }
        for r in board[:10]
    ]
    mine = next((r for r in board if r["telegram_id"] == tid), None)
    me = None
    if mine and mine["rank"] > 10:
        me = {
            "rank": mine["rank"], "name": mine["username"], "team_name": mine["team_name"],
            "points": mine["points"], "is_me": True,
        }
    pot = len(board) * ENTRY_FEE_BIRR
    out = {
        "gameweek": gw,
        "entrants": len(board),
        "top": top,
        "me": me,
        "is_admin": _is_admin(user),
        "prize": round(pot * core.PRIZE_SHARE),  # everyone sees the prize
    }
    if _is_admin(user):
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
