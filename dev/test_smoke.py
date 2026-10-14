"""
Quick self-check of the API using fake FPL data:   python dev/test_smoke.py
Covers Telegram sign-in, registration, verify.et payment checks (fake service), wallet and every screen's data.
"""

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mock_server as mock  # noqa: E402  (sets env + patches FPL + verify.et)

mock.seed()

import logging  # noqa: E402

logging.disable(logging.INFO)  # keep the test output readable

from fastapi.testclient import TestClient  # noqa: E402

import core  # noqa: E402
import server  # noqa: E402

TOKEN = mock.os.environ["BOT_TOKEN"]


def sign(user_id, auth_date=None, token=TOKEN):
    user = json.dumps({"id": user_id, "first_name": "Test", "username": f"user{user_id}"}, separators=(",", ":"))
    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAH", "user": user}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
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


with TestClient(server.app) as client:
    H = lambda uid: {"X-Telegram-Init-Data": sign(uid)}  # noqa: E731

    print("Telegram sign-in")
    check("no header → 401", client.get("/api/home").status_code == 401)
    check("valid signature → 200", client.get("/api/home", headers=H(777)).status_code == 200)
    bad = sign(777).replace("user=", "user=%7B%7D&x=")  # tampered payload
    check("tampered data → 401", client.get("/api/home", headers={"X-Telegram-Init-Data": bad}).status_code == 401)
    check("wrong bot token → 401", client.get("/api/home", headers={"X-Telegram-Init-Data": sign(777, token="1:OTHER")}).status_code == 401)
    check("expired (2 days old) → 401", client.get("/api/home", headers={"X-Telegram-Init-Data": sign(777, auth_date=int(time.time()) - 2 * 86400)}).status_code == 401)
    server.DEV_MODE = False
    check("dev header ignored when DEV_MODE off", client.get("/api/home", headers={"X-Dev-User": "777"}).status_code == 401)
    server.DEV_MODE = True
    check("dev header works when DEV_MODE on", client.get("/api/home", headers={"X-Dev-User": "777"}).status_code == 200)

    print("Registration (new player 777)")
    home = client.get("/api/home", headers=H(777)).json()
    check("starts unregistered", home["registered"] is False)
    check("entry window is GW8", home["entry"]["gameweek"] == 8 and not home["entry"]["paid"])
    dep = lambda uid, text: client.post("/api/deposit", headers=H(uid), json={"text": text})  # noqa: E731
    check("deposit before registering → 400", dep(777, "OK0000000001").status_code == 400)
    r = client.post("/api/register", headers=H(777), json={"team_id": 999})
    check("unknown FPL team → 400 with message", r.status_code == 400 and "couldn't find" in r.json()["detail"])
    r = client.post("/api/register", headers=H(777), json={"team_id": 5555})
    check("register ok, team name stored", r.status_code == 200 and r.json()["team"]["name"] == "Team 5555")

    print("Payment via verify.et")
    calls0 = len(mock.FAKE_HTTP.calls)
    r = dep(777, "OK0000000001")
    check("verified payment enters GW8", r.status_code == 200 and r.json()["gameweek"] == 8 and r.json()["credit"] == 0)
    post = [c for c in mock.FAKE_HTTP.calls[calls0:] if c[0] == "POST"][0]
    check("request carries tx number, bank and our account", post[2] == {"bank": "telebirr", "transactionNumber": "OK0000000001", "settlementAccount": "0939080897"})
    check("API key sent in x-api-key header", post[3]["x-api-key"] == "VERIFY_BANK_ET_test")
    home = client.get("/api/home", headers=H(777)).json()
    check("home shows paid", home["entry"]["paid"] and home["entry"]["entrants"] == 13)
    r = dep(777, "OK0000000001")
    check("already entered → refused", r.status_code == 400 and r.json()["code"] == "already_paid")

    # fresh players for the failure cases
    for uid, tid in ((771, 6001), (772, 6002), (773, 6003)):
        client.post("/api/register", headers=H(uid), json={"team_id": tid})
    r = dep(771, "Dear Abebe you have received ETB 250.00 from Kebede. Transaction number BIG0000000002 on 2026-10-13")
    check("pasting a whole SMS works; extra goes to wallet", r.status_code == 200 and r.json()["credit"] == 150)
    check("wallet shows +150", client.get("/api/wallet", headers=H(771)).json()["balance"] == 150)
    n0 = len(mock.FAKE_HTTP.calls)
    r = dep(772, "OK0000000001")
    check("same transaction can't be reused", r.status_code == 400 and r.json()["code"] == "used")
    check("…and cost no verify.et call (saved locally)", len(mock.FAKE_HTTP.calls) == n0)

    def refused(uid, tx, code):
        r = dep(uid, tx)
        return r.status_code == 400 and r.json()["code"] == code

    check("too small (50 birr) refused", refused(772, "SMALL000000003", "too_small"))
    check("paid to someone else refused", refused(772, "OTHER000000004", "wrong_receiver"))
    check("no verdict + masked number matching ours accepted", dep(773, "MASK000000005").status_code == 200)
    check("30-day-old receipt refused", refused(772, "OLD0000000006", "too_old"))
    check("future-dated receipt refused", refused(772, "FUTURE00000007", "bad_date"))
    check("non-birr receipt refused", refused(772, "USD0000000008", "bad_currency"))
    check("unknown transaction → not found", refused(772, "NOPE0000000009", "not_found"))
    check("rejected receipts were not stored", core.db_execute("SELECT COUNT(*) FROM incoming_payments WHERE tx_id IN ('OTHER000000004','OLD0000000006','FUTURE00000007')", fetchone=True)[0] == 0)
    client.post("/api/register", headers=H(778), json={"team_id": 6008})
    check("queued (202) check completes after polling", dep(778, "QUEUE00000010").status_code == 200)

    print("verify.et trouble is never the player's fault")
    for uid, tid in ((774, 6004), (775, 6005)):
        client.post("/api/register", headers=H(uid), json={"team_id": tid})
    for tx, code in (("DOWN0000000011", "provider_down"), ("BUSY0000000012", "provider_busy"),
                     ("BOOM0000000013", "provider_down"), ("FOREVER0000014", "provider_busy"),
                     ("NOKEY000000015", "provider_error"), ("NOCRED00000016", "provider_error")):
        r = dep(774, tx)
        check(f"{tx[:6]} → {code}", r.status_code in (502, 503, 400, 429) and r.json()["code"] == code)
    for _ in range(8):
        dep(774, "DOWN0000000011")
    check("outages don't lock the player out", dep(774, "OK0000000017").status_code == 200)
    err = core.ContestError("provider_error", "x", alert="key broke")
    core._alert_sent.clear()
    check("admin alert sent once per hour", core.admin_alert_for(err) and core.admin_alert_for(err) is None)

    print("Wrong guesses are rate limited")
    for i in range(6):
        dep(775, f"NOPE00000{i:03d}")
    r = dep(775, "OK0000000018")
    check("7th attempt blocked even with a good number", r.status_code == 400 and r.json()["code"] == "rate_limited")

    print("Admin fallback (/addpayment) and SMS hook")
    client.post("/api/register", headers=H(776), json={"team_id": 6006})
    core.store_incoming("MANUAL00000019", 100, "added by admin", "manual")
    check("hand-recorded payment works with verify.et on", dep(776, "MANUAL00000019").status_code == 200)
    r = client.post("/api/sms-hook", json={"sender": "Telebirr", "message": "You have received ETB 100.00 from Sara. Transaction number HOOK0000000020"})
    check("SMS hook without secret → 401", r.status_code == 401)
    r = client.post("/api/sms-hook", headers={"X-Webhook-Secret": "test-secret"}, json={"sender": "Telebirr", "message": "You have received ETB 100.00 from Sara. Transaction number HOOK0000000020"})
    check("SMS hook with secret stores payment", r.status_code == 200 and core.db_execute("SELECT 1 FROM incoming_payments WHERE tx_id='HOOK0000000020'", fetchone=True))

    print("Receiver number matching")
    check("exact / +251 / masked all match", all(core.receiver_matches("0939080897", x) for x in ("0939080897", "+251939080897", "2519****0897")))
    check("different number rejected", core.receiver_matches("0939080897", "2519****1111") is False)
    check("unreadable → can't tell", core.receiver_matches("0939080897", "") is None)

    print("Notifications")
    n = client.get("/api/notifications", headers=H(777)).json()
    check("payment notification created once", len(n["items"]) == 1 and n["unread"] == 1)
    client.post("/api/notifications/read", headers=H(777))
    check("mark read", client.get("/api/notifications", headers=H(777)).json()["unread"] == 0)
    client.post("/api/settings", headers=H(777), json={"notify_deadline": False})
    check("reminder toggle saved", client.get("/api/notifications", headers=H(777)).json()["settings"]["notify_deadline"] is False)
    check("opted-out user gets no reminder", 777 not in core.reminder_targets(8, 24))
    check("registered unpaid user gets reminder", 888 in core.reminder_targets(8, 24))

    print("Top 10 leaderboard & prizes")
    lb = client.get("/api/leaderboard", headers=H(889)).json()
    check("shows exactly 10 rows", len(lb["top"]) == 10 and lb["entrants"] == 14)
    check("ranks are descending by points", [r["points"] for r in lb["top"]] == sorted([r["points"] for r in lb["top"]], reverse=True))
    check("player outside Top 10 gets own row", lb["me"] is not None and lb["me"]["rank"] > 10)
    check("pot hidden from players", "pot" not in lb and lb["is_admin"] is False)
    check("FPL IDs hidden from players", all("fpl_id" not in r for r in lb["top"]))
    check("prize split is 25/15/10% of a 1400 pot", lb["prizes"] == [350, 210, 140])
    check("top three rows carry a prize", [r["prize"] for r in lb["top"][:3]] == [350, 210, 140] or sum(1 for r in lb["top"] if r["prize"]) >= 3)
    adm = client.get("/api/leaderboard", headers=H(1)).json()
    check("admin sees the pot", adm["pot"] == 1400 and adm["is_admin"] is True)
    check("admin sees FPL IDs of winners only", {bool(r.get("fpl_id")) for r in adm["top"][:3]} == {True} and all("fpl_id" not in r for r in adm["top"][3:] if not r["prize"]))

    print("Premier League data")
    table = client.get("/api/table", headers=H(777)).json()["rows"]
    check("20 clubs, positions 1..20", [r["pos"] for r in table] == list(range(1, 21)))
    check("sorted by points", [r["pts"] for r in table] == sorted([r["pts"] for r in table], reverse=True))
    check("points = 3W + D, GD = GF - GA", all(r["pts"] == 3 * r["w"] + r["d"] and r["gd"] == r["gf"] - r["ga"] for r in table))
    # GW1-6 fully played + 9 of 10 GW7 matches finished (one is still live and not counted)
    check("live match isn't counted in the table", sorted(r["p"] for r in table).count(6) == 2 and all(r["p"] in (6, 7) for r in table))
    fx = client.get("/api/fixtures", headers=H(777)).json()
    check("fixtures default to current GW7 with 10 matches", fx["gameweek"] == 7 and len(fx["matches"]) == 10 and fx["total"] == 38)
    check("one live match", sum(m["status"] == "live" for m in fx["matches"]) == 1)
    check("fixtures for GW9 are all scheduled", all(m["status"] == "scheduled" for m in client.get("/api/fixtures?gw=9", headers=H(777)).json()["matches"]))
    check("out-of-range GW is clamped", client.get("/api/fixtures?gw=99", headers=H(777)).json()["gameweek"] == 38)

    print("My team & transfers")
    check("unregistered has no team", client.get("/api/my-team", headers=H(12345)).json()["registered"] is False)
    team = client.get("/api/my-team", headers=H(888)).json()
    sizes = {k: len(v) for k, v in team["lines"].items()}
    check("11 starters + 4 bench", sum(sizes.values()) == 11 and sizes["GKP"] == 1 and len(team["bench"]) == 4)
    caps = [p for line in team["lines"].values() for p in line if p["captain"]]
    check("exactly one captain, points doubled", len(caps) == 1 and caps[0]["shown_points"] == caps[0]["points"] * 2)
    tr = client.get("/api/transfers", headers=H(888)).json()
    check("transfers list + trending", len(tr["mine"]) == 4 and len(tr["trending"]["in"]) == 6 and len(tr["trending"]["out"]) == 6)
    tr0 = client.get("/api/transfers", headers=H(12345)).json()
    check("unregistered still sees trending", tr0["registered"] is False and tr0["mine"] == [] and len(tr0["trending"]["in"]) == 6)

    print("Website files")
    for path in ("/", "/app.js", "/style.css", "/health"):
        check(f"GET {path}", client.get(path).status_code == 200)

print(f"\nAll {passed} checks passed.")
