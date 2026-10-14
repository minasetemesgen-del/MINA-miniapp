"""
The Admin dashboard's API (/api/admin/...). Every route checks, ON THE SERVER, that the
Telegram user is an admin — hiding the tab in the app is only for looks, this is the lock.
"""

import asyncio
import base64
import binascii
import logging
from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import admin_core
import core
import messaging
from config import ADMIN_TELEGRAM_ID, WEBAPP_URL

logger = logging.getLogger("admin")

_background = set()  # keeps running broadcast tasks alive until they finish


def create_admin_router(current_user):
    router = APIRouter(prefix="/api/admin")

    def admin(user: dict = Depends(current_user)) -> dict:
        if not core.is_admin_id(user["id"]):
            raise HTTPException(403, "Admins only.")
        return user

    def bot_of(request: Request):
        tg_app = getattr(request.app.state, "tg_app", None)
        return tg_app.bot if tg_app else None

    def need_bot(request: Request):
        bot = bot_of(request)
        if not bot:
            raise core.ContestError("no_bot", "The Telegram bot isn't running on this server, so messages can't be sent.")
        return bot

    def media_out(token):
        return core.media_url(token)

    # ------------------------------------------------------------------ overview
    @router.get("/overview")
    async def overview(user: dict = Depends(admin)):
        return await asyncio.to_thread(admin_core.overview)

    # ------------------------------------------------------------------ settings
    @router.get("/settings")
    async def get_settings(user: dict = Depends(admin)):
        s = core.all_settings()
        return {
            "settings": s,
            "prize_media_url": media_out(s["prize_media"]),
            "is_owner": user["id"] == ADMIN_TELEGRAM_ID,
            "owner_id": ADMIN_TELEGRAM_ID,
            "webapp_url": WEBAPP_URL,
        }

    class SettingsBody(BaseModel):
        entry_fee: Optional[int] = None
        prize_percents: Optional[Union[list, str]] = None
        min_withdrawal: Optional[int] = None
        reminder_hours: Optional[Union[list, str]] = None
        maintenance: Optional[bool] = None
        maintenance_text: Optional[str] = None
        registration_cap: Optional[int] = None
        prize_text: Optional[str] = None
        prize_media: Optional[str] = None
        announce_winners_to_all: Optional[bool] = None
        extra_admins: Optional[Union[list, str]] = None

    @router.post("/settings")
    async def save_settings(body: SettingsBody, user: dict = Depends(admin)):
        changes = body.model_dump(exclude_none=True)
        if "extra_admins" in changes and user["id"] != ADMIN_TELEGRAM_ID:
            raise core.ContestError("owner_only", "Only the owner can add or remove co-admins.")
        s = await asyncio.to_thread(core.update_settings, changes, user["id"])
        return {"ok": True, "settings": s, "prize_media_url": media_out(s["prize_media"])}

    # ------------------------------------------------------------------ pictures
    class MediaBody(BaseModel):
        data_url: str

    @router.post("/media")
    async def upload_media(body: MediaBody, user: dict = Depends(admin)):
        raw = body.data_url
        if len(raw) > 9_000_000:
            raise core.ContestError("bad_image", "That picture is too big (6 MB at most).")
        b64 = raw.split(",", 1)[1] if raw.startswith("data:") and "," in raw else raw
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise core.ContestError("bad_image", "That file couldn't be read as a picture.")
        token = await asyncio.to_thread(core.save_media, data)
        core.audit(user["id"], "upload_picture", token)
        return {"ok": True, "token": token, "url": media_out(token)}

    # ------------------------------------------------------------------ messages
    @router.get("/templates")
    async def templates(user: dict = Depends(admin)):
        items = await asyncio.to_thread(admin_core.list_templates)
        for t in items:
            t["sample"] = messaging.sample_vars(t["key"])
        return {"items": items, "caption_limit": messaging.CAPTION_LIMIT}

    class TemplateBody(BaseModel):
        text: Optional[str] = None  # "" resets to the default text
        media_token: Optional[str] = None  # "" removes the picture

    @router.post("/templates/{key}")
    async def save_template(key: str, body: TemplateBody, user: dict = Depends(admin)):
        await asyncio.to_thread(core.set_template, key, user["id"], body.text, body.media_token)
        return {"ok": True}

    @router.post("/templates/{key}/test")
    async def test_template(key: str, request: Request, user: dict = Depends(admin)):
        """Sends the saved message to the admin, filled with example values."""
        if key not in core.TEMPLATES:
            raise core.ContestError("bad_template", "Unknown message.")
        bot = need_bot(request)
        t = await asyncio.to_thread(core.get_template, key)
        text = core.render(t["text"], **messaging.sample_vars(key))
        ok = await messaging.send_rich(bot, user["id"], text, t["media_token"], reply_markup=messaging.app_button())
        if not ok:
            raise core.ContestError("send_failed", "Telegram didn't accept it — open the bot and press Start first.")
        return {"ok": True}

    # ------------------------------------------------------------------ withdrawals
    @router.get("/withdrawals")
    async def withdrawals(status: str = "pending", user: dict = Depends(admin)):
        return {"items": await asyncio.to_thread(admin_core.list_withdrawals, status)}

    class WithdrawalAction(BaseModel):
        action: str  # "paid" | "rejected"

    @router.post("/withdrawals/{wid}")
    async def withdrawal_action(wid: int, body: WithdrawalAction, request: Request, user: dict = Depends(admin)):
        if body.action not in ("paid", "rejected"):
            raise core.ContestError("bad_action", "Unknown action.")
        w = await messaging.process_withdrawal(bot_of(request), wid, body.action == "paid", user["id"])
        if not w:
            raise core.ContestError("handled", "That request was already handled.")
        return {"ok": True, "status": w["status"]}

    # ------------------------------------------------------------------ players
    @router.get("/players")
    async def players(q: str = "", user: dict = Depends(admin)):
        return {"items": await asyncio.to_thread(admin_core.list_players, q)}

    @router.get("/players/{tid}")
    async def player(tid: int, user: dict = Depends(admin)):
        return await asyncio.to_thread(admin_core.player_detail, tid)

    class BanBody(BaseModel):
        banned: bool

    @router.post("/players/{tid}/ban")
    async def ban(tid: int, body: BanBody, user: dict = Depends(admin)):
        await asyncio.to_thread(admin_core.set_banned, tid, body.banned, user["id"])
        return {"ok": True}

    class FplBody(BaseModel):
        team_id: int

    @router.post("/players/{tid}/fpl-id")
    async def fpl_id(tid: int, body: FplBody, user: dict = Depends(admin)):
        info = await asyncio.to_thread(admin_core.set_fpl_id, tid, body.team_id, user["id"])
        return {"ok": True, "team_name": info["name"]}

    class EntryBody(BaseModel):
        gameweek: int
        action: str  # "add" | "remove"
        refund: bool = False

    @router.post("/players/{tid}/entry")
    async def entry(tid: int, body: EntryBody, user: dict = Depends(admin)):
        if body.action == "add":
            await asyncio.to_thread(admin_core.add_entry, tid, body.gameweek, user["id"])
        elif body.action == "remove":
            await asyncio.to_thread(admin_core.remove_entry, tid, body.gameweek, body.refund, user["id"])
        else:
            raise core.ContestError("bad_action", "Unknown action.")
        return {"ok": True}

    class WalletBody(BaseModel):
        amount: int
        reason: str = ""

    @router.post("/players/{tid}/wallet")
    async def wallet(tid: int, body: WalletBody, user: dict = Depends(admin)):
        balance = await asyncio.to_thread(admin_core.adjust_wallet, tid, body.amount, body.reason, user["id"])
        return {"ok": True, "balance": balance}

    # ------------------------------------------------------------------ payments
    @router.get("/payments")
    async def payments(status: str = "all", q: str = "", user: dict = Depends(admin)):
        return {"items": await asyncio.to_thread(admin_core.list_payments, status, q)}

    class PaymentBody(BaseModel):
        tx: str
        amount: float

    @router.post("/payments")
    async def add_payment(body: PaymentBody, user: dict = Depends(admin)):
        await asyncio.to_thread(admin_core.add_payment, body.tx, body.amount, user["id"])
        return {"ok": True}

    @router.delete("/payments/{tx}")
    async def delete_payment(tx: str, user: dict = Depends(admin)):
        await asyncio.to_thread(admin_core.delete_payment, tx.upper(), user["id"])
        return {"ok": True}

    # ------------------------------------------------------------------ gameweek
    @router.get("/gameweek")
    async def gameweek(gw: Optional[int] = None, user: dict = Depends(admin)):
        def build():
            live = core.get_live_gameweek()
            use = gw or live
            if not use:
                raise core.ContestError("closed", "No gameweek yet.")
            info = admin_core.gameweek_info(use)
            prev = admin_core.announcement_preview(use)
            medals = messaging.MEDALS
            info.update({
                "live": live,
                "places": prev["places"],
                "percents": core.get_setting("prize_percents"),
                "winners": [{**w, "medal": medals.get(w["rank"], "🏅")} for w in prev["winners"]],
                "top": [{"rank": r["rank"], "name": r["username"], "points": r["points"]} for r in prev["results"][:10]],
            })
            return info

        return await asyncio.to_thread(build)

    class AnnounceBody(BaseModel):
        gameweek: int

    @router.post("/gameweek/announce")
    async def announce(body: AnnounceBody, request: Request, user: dict = Depends(admin)):
        result = await messaging.run_announcement(bot_of(request), body.gameweek, user["id"])
        return {"ok": True, "summary": result["summary"], "winners": len(result["winners"])}

    # ------------------------------------------------------------------ broadcast
    @router.get("/broadcasts")
    async def broadcasts(user: dict = Depends(admin)):
        return {
            "items": await asyncio.to_thread(admin_core.list_broadcasts),
            "audiences": admin_core.AUDIENCES,
        }

    class BroadcastBody(BaseModel):
        text: str
        media_token: str = ""
        audience: str = "all"  # all | not_entered | entered | me (test)

    @router.post("/broadcast")
    async def broadcast(body: BroadcastBody, request: Request, user: dict = Depends(admin)):
        text = body.text.strip()
        if not text:
            raise core.ContestError("empty", "Write a message first.")
        if len(text) > 3500:
            raise core.ContestError("too_long", "That message is too long (3500 characters at most).")
        if body.media_token and not await asyncio.to_thread(core.get_media, body.media_token):
            raise core.ContestError("bad_image", "That picture no longer exists — upload it again.")
        bot = need_bot(request)
        if body.audience == "me":  # a test: only to the admin, nothing recorded
            ok = await messaging.send_rich(bot, user["id"], text, body.media_token, reply_markup=messaging.app_button())
            if not ok:
                raise core.ContestError("send_failed", "Telegram didn't accept it — open the bot and press Start first.")
            return {"ok": True, "test": True}
        ids = await asyncio.to_thread(admin_core.audience_ids, body.audience)
        if not ids:
            raise core.ContestError("nobody", "Nobody is in that audience right now.")
        bid = await asyncio.to_thread(admin_core.create_broadcast, user["id"], text, body.media_token, body.audience, len(ids))
        core.audit(user["id"], "broadcast", f"#{bid} to {body.audience} ({len(ids)} people)")
        task = asyncio.create_task(messaging.run_broadcast(bot, bid))
        _background.add(task)
        task.add_done_callback(_background.discard)
        return {"ok": True, "id": bid, "total": len(ids)}

    @router.get("/audience")
    async def audience(audience: str = "all", user: dict = Depends(admin)):
        return {"count": len(await asyncio.to_thread(admin_core.audience_ids, audience))}

    @router.get("/broadcasts/{bid}")
    async def broadcast_status(bid: int, user: dict = Depends(admin)):
        b = await asyncio.to_thread(admin_core.get_broadcast, bid)
        if not b:
            raise HTTPException(404, "Not found.")
        return b

    # ------------------------------------------------------------------ log / export
    @router.get("/audit")
    async def audit(user: dict = Depends(admin)):
        return {"items": await asyncio.to_thread(admin_core.audit_list), "owner_id": ADMIN_TELEGRAM_ID}

    class ExportBody(BaseModel):
        kind: str

    @router.post("/export")
    async def export(body: ExportBody, request: Request, user: dict = Depends(admin)):
        """Sends the data as a CSV file to the admin's Telegram chat (downloads are unreliable inside Telegram)."""
        bot = need_bot(request)
        filename, data = await asyncio.to_thread(admin_core.export_csv, body.kind)
        import io

        try:
            await bot.send_document(user["id"], io.BytesIO(data), filename=filename, caption=f"📄 {filename}")
        except Exception:
            logger.exception("Could not send the export")
            raise core.ContestError("send_failed", "Telegram didn't accept it — open the bot and press Start first.")
        core.audit(user["id"], "export", body.kind)
        return {"ok": True, "filename": filename}

    return router
