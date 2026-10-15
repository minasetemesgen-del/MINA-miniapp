"""
Self-check of the Admin dashboard API and the photo-with-caption sender:   python dev/test_admin.py
Uses fake FPL data, a fake verify.et and a FAKE Telegram bot (nothing is really sent).
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mock_server as mock  # noqa: E402  (sets env, patches FPL + verify.et)

mock.seed()
logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient  # noqa: E402
from telegram.error import Forbidden  # noqa: E402

import admin_core  # noqa: E402
import bot as bot_module  # noqa: E402
import core  # noqa: E402
import messaging  # noqa: E402
import server  # noqa: E402

TOKEN = mock.os.environ["BOT_TOKEN"]
OWNER = 1  # ADMIN_TELEGRAM_ID in the mock
# a real, tiny 1x1 PNG
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


def sign(user_id):
    user = json.dumps({"id": user_id, "first_name": "Test", "username": f"user{user_id}"}, separators=(",", ":"))
    fields = {"auth_date": str(int(time.time())), "query_id": "AAH", "user": user}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


passed = 0


def check(name, cond):
    global passed
    if not cond:
        print(f"  FAIL  {name}")
        sys.exit(1)
    passed += 1
    print(f"  ok    {name}")


class FakeBot:
    """Records what would have been sent to Telegram."""

    def __init__(self):
        self.sent = []  # (kind, chat_id, payload dict)
        self.blocked = set()
        self.n = 0

    def _check(self, chat_id):
        if chat_id in self.blocked:
            raise Forbidden("Forbidden: bot was blocked by the user")

    async def send_message(self, chat_id, text, **kw):
        self._check(chat_id)
        self.sent.append(("message", chat_id, {"text": text, **kw}))
        return SimpleNamespace(message_id=len(self.sent))

    async def send_photo(self, chat_id, photo, **kw):
        self._check(chat_id)
        self.n += 1
        self.sent.append(("photo", chat_id, {"photo": photo, **kw}))
        return SimpleNamespace(photo=[SimpleNamespace(file_id=f"FILEID{self.n}")])

    async def send_document(self, chat_id, document, **kw):
        self._check(chat_id)
        self.sent.append(("document", chat_id, {"document": document.read(), **kw}))
        return SimpleNamespace()

    def to(self, chat_id, kind=None):
        return [s for s in self.sent if s[1] == chat_id and (kind is None or s[0] == kind)]


BOT = FakeBot()
run = asyncio.run


with TestClient(server.app) as client:
    server.app.state.tg_app = SimpleNamespace(bot=BOT)
    H = lambda uid: {"X-Telegram-Init-Data": sign(uid)}  # noqa: E731
    get = lambda path, uid=OWNER: client.get("/api/admin" + path, headers=H(uid))  # noqa: E731
    post = lambda path, body=None, uid=OWNER: client.post("/api/admin" + path, headers=H(uid), json=body or {})  # noqa: E731

    print("Only admins get in")
    check("player → 403", get("/overview", 888).status_code == 403)
    check("player cannot save settings", post("/settings", {"entry_fee": 1}, 888).status_code == 403)
    check("player cannot send a broadcast", post("/broadcast", {"text": "hi"}, 888).status_code == 403)
    check("no login at all → 401", client.get("/api/admin/overview").status_code == 401)
    check("owner → 200", get("/overview").status_code == 200)
    check("home says who is admin", client.get("/api/home", headers=H(OWNER)).json()["is_admin"] and not client.get("/api/home", headers=H(888)).json()["is_admin"])

    print("Settings: prize split, fee, withdrawal minimum")
    s = get("/settings").json()
    check("defaults are 25/15/10, fee 100", s["settings"]["prize_percents"] == [25, 15, 10] and s["settings"]["entry_fee"] == 100)
    check("saving the split works", post("/settings", {"prize_percents": [40, 20, 10, 5]}).status_code == 200)
    lb = client.get("/api/leaderboard", headers=H(OWNER)).json()
    check("leaderboard now pays 4 places (pot 1400)", lb["prizes"] == [560, 280, 140, 70])
    check("percentages can be typed as text", post("/settings", {"prize_percents": "25, 15 / 10"}).json()["settings"]["prize_percents"] == [25, 15, 10])
    r = post("/settings", {"prize_percents": [60, 30, 20]})
    check("over 100% refused, in plain words", r.status_code == 400 and "110" in r.json()["detail"])
    check("zero / negative refused", post("/settings", {"prize_percents": [25, 0]}).status_code == 400 and post("/settings", {"entry_fee": -5}).status_code == 400)
    check("more than 10 places refused", post("/settings", {"prize_percents": [5] * 11}).status_code == 400)
    check("nothing half-saved after refusals", get("/settings").json()["settings"]["prize_percents"] == [25, 15, 10])
    check("one winner (100%) is allowed", post("/settings", {"prize_percents": [100]}).status_code == 200 and post("/settings", {"prize_percents": [25, 15, 10]}).status_code == 200)

    post("/settings", {"entry_fee": 150})
    h = client.get("/api/home", headers=H(888)).json()
    check("new fee shown to players", h["fee"] == 150)
    check("old gameweeks keep the fee they were paid at", client.get("/api/leaderboard", headers=H(OWNER)).json()["pot"] == 1400)
    check("minimum withdrawal editable", post("/settings", {"min_withdrawal": 250}).status_code == 200 and client.get("/api/wallet", headers=H(888)).json()["min_withdrawal"] == 250)
    post("/settings", {"entry_fee": 100, "min_withdrawal": 100})

    print("Winners' prize text and picture")
    up = post("/media", {"data_url": PNG_URL})
    check("picture upload", up.status_code == 200 and up.json()["token"])
    tok = up.json()["token"]
    img = client.get(f"/media/{tok}")
    check("picture is served", img.status_code == 200 and img.headers["content-type"] == "image/png" and img.content == PNG)
    check("unknown picture → 404", client.get("/media/deadbeef").status_code == 404)
    check("a non-picture is refused", post("/media", {"data_url": "data:image/png;base64," + base64.b64encode(b"hello world").decode()}).status_code == 400)
    check("garbage is refused", post("/media", {"data_url": "data:image/png;base64,@@@"}).status_code == 400)
    post("/settings", {"prize_text": "PS5 Slim", "prize_media": tok})
    h = client.get("/api/home", headers=H(888)).json()
    check("players see the prize text + picture", h["prize"]["text"] == "PS5 Slim" and h["prize"]["image"] == f"/media/{tok}")
    check("bogus picture token refused", post("/settings", {"prize_media": "nope"}).status_code == 400)

    print("Pause entries (maintenance) and player limit")
    client.post("/api/register", headers=H(901), json={"team_id": 7001})
    post("/settings", {"maintenance": True, "maintenance_text": "Back in 10 minutes"})
    r = client.post("/api/deposit", headers=H(901), json={"text": "OK0000000101"})
    check("deposits refused while paused", r.status_code == 400 and r.json()["code"] == "maintenance" and "Back in 10" in r.json()["detail"])
    check("home shows the pause notice", client.get("/api/home", headers=H(901)).json()["paused"] == "Back in 10 minutes")
    post("/settings", {"maintenance": False})
    check("works again after", client.post("/api/deposit", headers=H(901), json={"text": "OK0000000101"}).status_code == 200)
    n_users = core.db_execute("SELECT COUNT(*) FROM users", fetchone=True)[0]
    post("/settings", {"registration_cap": n_users})
    r = client.post("/api/register", headers=H(902), json={"team_id": 7002})
    check("new player blocked at the limit", r.status_code == 400 and r.json()["code"] == "full")
    check("existing player can still re-link", client.post("/api/register", headers=H(901), json={"team_id": 7001}).status_code == 200)
    post("/settings", {"registration_cap": 0})
    check("limit off → registration works", client.post("/api/register", headers=H(902), json={"team_id": 7002}).status_code == 200)

    print("Co-admins")
    check("only the owner can add co-admins", post("/settings", {"extra_admins": [902]}, 888).status_code == 403)
    r = post("/settings", {"extra_admins": "902, 903"})
    check("owner adds co-admins", r.status_code == 200 and r.json()["settings"]["extra_admins"] == [902, 903])
    check("co-admin gets in", get("/overview", 902).status_code == 200)
    check("co-admin cannot change co-admins", post("/settings", {"extra_admins": []}, 902).status_code == 400 or post("/settings", {"extra_admins": []}, 902).status_code == 403)
    check("co-admin sees admin figures", client.get("/api/home", headers=H(902)).json()["is_admin"])

    print("Block a player")
    client.post("/api/register", headers=H(903), json={"team_id": 7003})
    check("cannot block an admin", post("/players/903/ban", {"banned": True}).status_code == 400)
    post("/settings", {"extra_admins": []})
    check("block works", post("/players/903/ban", {"banned": True}).status_code == 200)
    r = client.post("/api/deposit", headers=H(903), json={"text": "OK0000000102"})
    check("blocked player cannot enter", r.status_code == 400 and r.json()["code"] == "banned")
    check("blocked player cannot re-link", client.post("/api/register", headers=H(903), json={"team_id": 7003}).json()["code"] == "banned")
    check("home flags the block", client.get("/api/home", headers=H(903)).json()["banned"] is True)
    post("/players/903/ban", {"banned": False})
    check("unblock works", client.post("/api/deposit", headers=H(903), json={"text": "OK0000000102"}).status_code == 200)

    print("Messages: edit text, attach a picture (photo + caption)")
    t = get("/templates").json()
    keys = [i["key"] for i in t["items"]]
    check("all editable messages listed", {"welcome", "pay_instructions", "deposit_success", "reminder", "winner_dm", "winners_all"} <= set(keys))
    check("each lists its placeholders and a sample", all(i["vars"] and i["sample"] for i in t["items"]))
    check("default text is used until edited", not [i for i in t["items"] if i["custom"]])
    check("bad key refused", post("/templates/nope", {"text": "x"}).status_code == 400)

    BOT.sent.clear()
    run(messaging.send_template(BOT, 888, "welcome", **messaging.vars_welcome()))
    kind, chat, p = BOT.to(888)[0]
    check("default welcome is plain text with the live fee and split", kind == "message" and "100 birr" in p["text"] and "25% / 15% / 10%" in p["text"])

    post("/templates/welcome", {"text": "Karibu! Fee is {fee} birr. Top {winners} win. {unknown}"})
    BOT.sent.clear()
    run(messaging.send_template(BOT, 888, "welcome", **messaging.vars_welcome()))
    check("edited text is used, placeholders filled, unknown left alone", BOT.to(888)[0][2]["text"] == "Karibu! Fee is 100 birr. Top 3 win. {unknown}")
    check("braces that make no sense never crash a send", core.render("{ oops {fee", fee=5) == "{ oops {fee" and core.render("a {fee} {x}", fee=5) == "a 5 {x}")

    post("/templates/welcome", {"media_token": tok})
    BOT.sent.clear()
    run(messaging.send_template(BOT, 888, "welcome", **messaging.vars_welcome()))
    kind, chat, p = BOT.to(888)[0]
    check("with a picture it is sent as a PHOTO with the text as CAPTION", kind == "photo" and p["caption"].startswith("Karibu!") and len(BOT.to(888)) == 1)
    check("first send uploads the file", hasattr(p["photo"], "input_file_content") or not isinstance(p["photo"], str))
    run(messaging.send_template(BOT, 889, "welcome", **messaging.vars_welcome()))
    check("next sends reuse Telegram's saved file id (fast)", BOT.to(889)[0][2]["photo"] == "FILEID1")

    long_text = "x" * 1500
    post("/templates/pay_instructions", {"text": long_text, "media_token": tok})
    BOT.sent.clear()
    run(messaging.send_template(BOT, 888, "pay_instructions", **messaging.vars_pay({"gameweek": 8, "deadline": core.get_entry_window()["deadline"]})))
    sent = BOT.to(888)
    check("text longer than a caption: photo, then the full text", [s[0] for s in sent] == ["photo", "message"] and sent[1][2]["text"] == long_text and "caption" not in sent[0][2])

    r = post("/templates/welcome/test")
    check("'send me a test' reaches the admin as photo+caption", r.status_code == 200 and BOT.to(OWNER)[-1][0] == "photo" and "Fee is 100" in BOT.to(OWNER)[-1][2]["caption"])
    BOT.blocked.add(888)
    check("a blocked player doesn't crash anything", run(messaging.send_template(BOT, 888, "welcome", **messaging.vars_welcome())) is False)
    BOT.blocked.clear()

    post("/templates/welcome", {"media_token": ""})
    BOT.sent.clear()
    run(messaging.send_template(BOT, 888, "welcome", **messaging.vars_welcome()))
    check("removing the picture → plain text again", BOT.to(888)[0][0] == "message")
    post("/templates/welcome", {"text": ""})
    post("/templates/pay_instructions", {"text": "", "media_token": ""})
    t = {i["key"]: i for i in get("/templates").json()["items"]}
    check("reset brings the default back", not t["welcome"]["custom"] and "Welcome to the FPL Weekly Contest" in t["welcome"]["text"])
    check("too-long message refused", post("/templates/welcome", {"text": "y" * 4000}).status_code == 400)

    print("The real bot commands use the editable messages")

    class Upd:
        def __init__(self, uid):
            self.effective_user = SimpleNamespace(id=uid, username=f"u{uid}", first_name="T")
            self.effective_chat = SimpleNamespace(id=uid)
            self.message = SimpleNamespace(reply_text=self._reply)
            self.replies = []

        async def _reply(self, text, **kw):
            self.replies.append(text)

    post("/templates/welcome", {"text": "Hello {fee}!", "media_token": tok})
    BOT.sent.clear()
    run(bot_module.start(Upd(888), SimpleNamespace(bot=BOT)))
    check("/start sends the edited welcome as photo+caption", BOT.to(888)[0][0] == "photo" and BOT.to(888)[0][2]["caption"] == "Hello 100!")
    post("/templates/welcome", {"text": "", "media_token": ""})
    BOT.sent.clear()
    run(bot_module.pay(Upd(888), SimpleNamespace(bot=BOT)))
    check("/pay shows the live fee, number and deadline", "100 birr" in BOT.to(888)[0][2]["text"] and "0939080897" in BOT.to(888)[0][2]["text"] and "EAT" in BOT.to(888)[0][2]["text"])

    print("Deadline reminders follow the settings")
    post("/settings", {"reminder_hours": [72, 24, 3]})
    BOT.sent.clear()
    ctx = SimpleNamespace(bot=BOT)
    run(bot_module.deadline_reminders(ctx))
    n1 = len(BOT.sent)
    run(bot_module.deadline_reminders(ctx))
    check("reminder sent once to players not entered (GW8 closes in ~52h)", n1 > 0 and len(BOT.sent) == n1 and all("hours left" in s[2]["text"] or "left to enter" in s[2]["text"] for s in BOT.sent))
    check("…and says the configured time", all("72 hours" in s[2]["text"] for s in BOT.sent))
    post("/settings", {"reminder_hours": []})
    BOT.sent.clear()
    run(bot_module.deadline_reminders(ctx))
    check("no reminder times → nothing sent", not BOT.sent)
    check("more than 3 reminder times refused", post("/settings", {"reminder_hours": [1, 2, 3, 4]}).status_code == 400)
    post("/settings", {"reminder_hours": [24, 3]})

    print("Withdrawals in the dashboard")
    check("give 888 prize money (wallet +400)", post("/players/888/wallet", {"amount": 400, "reason": "test prize"}).json()["balance"] == 400)
    BOT.sent.clear()
    r = client.post("/api/withdraw", headers=H(888), json={"amount": 250, "account": "0912345678"})
    check("player requests a withdrawal", r.status_code == 200)
    adm_msgs = BOT.to(OWNER)
    check("admin got the Telegram message with Mark paid / Reject", adm_msgs and "Withdrawal request" in adm_msgs[0][2]["text"] and adm_msgs[0][2]["reply_markup"] is not None)
    w = get("/withdrawals").json()["items"]
    check("it is listed in the dashboard", len(w) == 1 and w[0]["amount"] == 250 and w[0]["account"] == "0912345678" and w[0]["status"] == "pending")
    wid = w[0]["id"]
    check("overview counts it", get("/overview").json()["withdrawals_pending"] == {"count": 1, "amount": 250})
    BOT.sent.clear()
    r = post(f"/withdrawals/{wid}", {"action": "paid"})
    check("Mark paid works", r.status_code == 200 and r.json()["status"] == "paid")
    check("player is told in Telegram and in the app", any("Withdrawal sent" in s[2]["text"] for s in BOT.to(888)) and any("Withdrawal sent" in n["title"] for n in core.list_notifications(888)))
    check("handling twice is refused", post(f"/withdrawals/{wid}", {"action": "paid"}).status_code == 400)
    check("player's wallet is down by the amount", client.get("/api/wallet", headers=H(888)).json()["balance"] == 150)
    client.post("/api/withdraw", headers=H(888), json={"amount": 150, "account": "0912345678"})
    wid2 = get("/withdrawals").json()["items"][0]["id"]
    post(f"/withdrawals/{wid2}", {"action": "rejected"})
    check("rejecting returns the money", client.get("/api/wallet", headers=H(888)).json()["balance"] == 150)
    check("history filter works", len(get("/withdrawals?status=paid").json()["items"]) == 1 and len(get("/withdrawals?status=all").json()["items"]) == 2)
    check("bad action refused", post(f"/withdrawals/{wid2}", {"action": "delete"}).status_code == 400)

    print("Players")
    p = get("/players?q=dev888").json()["items"]
    check("search by name", len(p) == 1 and p[0]["id"] == 888)
    check("search by FPL id", get("/players?q=1888").json()["items"][0]["id"] == 888)
    check("search by Telegram id", get("/players?q=888").json()["items"][0]["id"] == 888)
    d = get("/players/888").json()
    check("detail has entries, balance and history", d["balance"] == 150 and d["entries"] and d["history"])
    check("unknown player → clear error", get("/players/424242").status_code == 400)
    check("fix FPL id (checks it exists)", post("/players/888/fpl-id", {"team_id": 4444}).json()["team_name"] == "Team 4444")
    check("unknown FPL id refused", post("/players/888/fpl-id", {"team_id": 999}).status_code == 400)
    check("add an entry by hand", post("/players/888/entry", {"gameweek": 8, "action": "add"}).status_code == 200 and core.get_entry(888, 8)["paid"])
    check("pot grows by the current fee", core.gameweek_pot(8) >= 100)
    before = core.wallet_balance(888)
    post("/players/888/entry", {"gameweek": 8, "action": "remove", "refund": True})
    check("remove with refund → fee to wallet", core.get_entry(888, 8) is None and core.wallet_balance(888) == before + 100)
    check("removing an entry that doesn't exist is refused", post("/players/888/entry", {"gameweek": 8, "action": "remove"}).status_code == 400)
    check("wallet adjustment needs a reason", post("/players/888/wallet", {"amount": 10, "reason": " "}).status_code == 400)
    check("wallet can't go below zero", post("/players/888/wallet", {"amount": -99999, "reason": "x"}).status_code == 400)
    check("zero adjustment refused", post("/players/888/wallet", {"amount": 0, "reason": "x"}).status_code == 400)
    b0 = core.wallet_balance(888)
    check("take money off with a reason", post("/players/888/wallet", {"amount": -50, "reason": "paid in cash"}).json()["balance"] == b0 - 50)
    hist = client.get("/api/wallet", headers=H(888)).json()["items"]
    check("player sees the adjustment with its reason", any(i["type"] == "credit" and i["amount"] == -50 and "paid in cash" in i["reason"] for i in hist))

    print("Payments")
    check("add a payment by hand", post("/payments", {"tx": "hand000000001", "amount": 100}).status_code == 200)
    check("same one again refused", post("/payments", {"tx": "HAND000000001", "amount": 100}).status_code == 400)
    check("bad transaction number refused", post("/payments", {"tx": "x", "amount": 100}).status_code == 400 and post("/payments", {"tx": "HAND000000002", "amount": 0}).status_code == 400)
    pl = get("/payments?status=unused").json()["items"]
    check("listed and filterable", any(x["tx"] == "HAND000000001" for x in pl) and all(x["status"] == "unused" for x in pl))
    check("search works", get("/payments?q=hand000000001").json()["items"][0]["tx"] == "HAND000000001")
    used = [x for x in get("/payments?status=used").json()["items"]]
    check("used payments show who used them", used and used[0]["used_by_name"])
    check("an unused payment can be deleted", client.delete("/api/admin/payments/hand000000001", headers=H(OWNER)).status_code == 200)
    check("a used one cannot", client.delete(f"/api/admin/payments/{used[0]['tx']}", headers=H(OWNER)).status_code == 400)

    print("Announce winners from the dashboard")
    gw = get("/gameweek").json()
    check("preview shows the three winners and their prizes", gw["gameweek"] == 7 and len(gw["winners"]) == 3 and sum(w["prize"] for w in gw["winners"]) == 700 and not gw["announced"])
    check("preview saved nothing", core.db_execute("SELECT COUNT(*) FROM prizes WHERE gameweek=7", fetchone=True)[0] == 0)
    check("preview can look at another gameweek", get("/gameweek?gw=6").json()["gameweek"] == 6)
    BOT.sent.clear()
    r = post("/gameweek/announce", {"gameweek": 7})
    check("announcing works", r.status_code == 200 and r.json()["winners"] == 3 and "FPL ID" in r.json()["summary"])
    winners = [w["telegram_id"] for w in gw["winners"]]
    check("each winner got a private congratulation", all(any("Congrats" in s[2]["text"] for s in BOT.to(w)) for w in winners))
    check("prizes are in the winners' wallets", all(client.get("/api/wallet", headers=H(w)).json()["balance"] >= 140 for w in winners))
    check("everyone entered got an in-app notification", all(core.list_notifications(r["telegram_id"])[0]["kind"] == "winner" for r in core.contest_leaderboard(7)))
    check("others were NOT messaged (setting off)", not [s for s in BOT.sent if s[1] not in winners and s[1] != OWNER])
    check("announcing twice doesn't double the prizes", post("/gameweek/announce", {"gameweek": 7}).status_code == 200 and core.db_execute("SELECT COUNT(*) FROM prizes WHERE gameweek=7", fetchone=True)[0] == 3)
    post("/settings", {"announce_winners_to_all": True})
    post("/templates/winners_all", {"media_token": tok})
    BOT.sent.clear()
    post("/gameweek/announce", {"gameweek": 7})
    others = [s for s in BOT.sent if s[1] not in winners and s[1] != OWNER]
    check("with 'tell everyone' on, the rest get it as photo+caption", len(others) == 11 and all(s[0] == "photo" and "GW7 winners" in s[2]["caption"] for s in others))
    post("/settings", {"announce_winners_to_all": False})
    post("/templates/winners_all", {"media_token": ""})
    check("the bot command works the same way", run(bot_module.announce_winner(SimpleNamespace(effective_user=SimpleNamespace(id=OWNER), message=SimpleNamespace(reply_text=lambda *a, **k: asyncio.sleep(0)), effective_chat=SimpleNamespace(id=OWNER)), SimpleNamespace(bot=BOT, args=["7"]))) is None)
    post("/settings", {"prize_percents": [100]})
    check("one winner takes everything when set to 100%", get("/gameweek?gw=7").json()["winners"][0]["prize"] == 1400 and len(get("/gameweek?gw=7").json()["winners"]) == 1)
    post("/settings", {"prize_percents": [25, 15, 10]})

    print("Broadcast")
    check("empty message refused", post("/broadcast", {"text": "  "}).status_code == 400)
    BOT.sent.clear()
    r = post("/broadcast", {"text": "Test message", "audience": "me"})
    check("test goes only to me", r.status_code == 200 and r.json()["test"] and [s[1] for s in BOT.sent] == [OWNER])
    check("a test is not recorded as a broadcast", not get("/broadcasts").json()["items"])
    BOT.sent.clear()
    ids = admin_core.audience_ids("not_entered")
    r = post("/broadcast", {"text": "Enter now! GW8 closes soon", "media_token": tok, "audience": "not_entered"})
    check("broadcast starts and counts the audience", r.status_code == 200 and r.json()["total"] == len(ids) and len(ids) > 0)
    bid = r.json()["id"]
    for _ in range(100):
        st = get(f"/broadcasts/{bid}").json()
        if st["status"] != "sending":
            break
        time.sleep(0.1)
    check("it finishes and everyone got a photo with the caption", st["status"] == "done" and st["sent"] == len(ids) and st["failed"] == 0
          and all(s[0] == "photo" and s[2]["caption"] == "Enter now! GW8 closes soon" for s in BOT.sent))
    entered = set(admin_core.audience_ids("entered"))
    check("audiences really differ", not entered & set(ids))
    BOT.sent.clear()
    BOT.blocked.add(ids[0])
    r = post("/broadcast", {"text": "Second", "audience": "not_entered"})
    bid = r.json()["id"]
    for _ in range(100):
        st = get(f"/broadcasts/{bid}").json()
        if st["status"] != "sending":
            break
        time.sleep(0.1)
    BOT.blocked.clear()
    check("someone who blocked the bot is counted as failed, others still get it", st["failed"] == 1 and st["sent"] == len(ids) - 1)
    post("/players/903/ban", {"banned": True})
    check("blocked players are skipped from audiences", 903 not in admin_core.audience_ids("all") and 902 in admin_core.audience_ids("all"))
    post("/players/903/ban", {"banned": False})
    check("unknown audience refused", post("/broadcast", {"text": "x", "audience": "everyone-ever"}).status_code == 400)
    check("history lists them", len(get("/broadcasts").json()["items"]) == 2)

    print("Overview, log and exports")
    ov = get("/overview").json()
    check("overview numbers add up", ov["players"] >= 14 and ov["prizes_awarded"] == 700 and ov["collected_total"] > 0 and ov["recent"] and ov["verify"]["enabled"])
    check("verify.et health is tracked", ov["verify"]["calls"] >= 1 and ov["verify"]["last_ok"])
    check("owed to players = wallet balances", ov["owed_to_players"] == sum(core.wallet_balance(r[0]) for r in core.db_execute("SELECT telegram_id FROM users", fetch=True)))
    log = get("/audit").json()["items"]
    actions = {x["action"] for x in log}
    check("everything that changed something is in the log", {"settings", "message", "withdrawal_paid", "withdrawal_rejected", "announce", "broadcast", "wallet", "ban", "add_payment"} <= actions)
    check("log says who did it", log[0]["admin_id"] in (OWNER, 902))
    BOT.sent.clear()
    core.db_execute("UPDATE users SET username='=HYPERLINK(\"x\")' WHERE telegram_id=100")
    r = post("/export", {"kind": "players"})
    doc = BOT.to(OWNER, "document")[0][2]
    csv_text = doc["document"].decode("utf-8-sig")
    check("export is sent to the admin's Telegram as a CSV file", r.status_code == 200 and doc["filename"] == "etfpl_players.csv" and csv_text.startswith("telegram_id,username"))
    check("spreadsheet formulas are defused", "'=HYPERLINK" in csv_text and ",=HYPERLINK" not in csv_text)
    check("every export kind works", all(post("/export", {"kind": k}).status_code == 200 for k in admin_core.EXPORTS))
    check("unknown export refused", post("/export", {"kind": "passwords"}).status_code == 400)

    print("Without the bot running")
    server.app.state.tg_app = None
    check("sending needs the bot — clear message, no crash", post("/broadcast", {"text": "x", "audience": "me"}).json()["code"] == "no_bot")
    check("withdrawals can still be handled in the dashboard", client.post("/api/withdraw", headers=H(888), json={"amount": 100, "account": "0912345678"}).status_code == 200
          and post(f"/withdrawals/{get('/withdrawals').json()['items'][0]['id']}", {"action": "paid"}).status_code == 200)

print(f"\nAll {passed} checks passed.")
