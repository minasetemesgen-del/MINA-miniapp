"""
Shared logic used by BOTH the Telegram bot and the Mini App API:
database, FPL API access (with caching), Chapa payments, notifications and
the contest leaderboard. Keeping it in one place means the bot and the app
can never disagree about who is entered or who is winning.
"""

import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from config import CHAPA_RETURN_URL, CHAPA_SECRET_KEY, ENTRY_FEE_BIRR

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "fpl_contest.db")
PRIZE_SHARE = 0.5  # winner gets 50% of the pot

FPL_API = "https://fantasy.premierleague.com/api"
CHAPA_INITIALIZE_URL = "https://api.chapa.co/v1/transaction/initialize"
CHAPA_VERIFY_URL = "https://api.chapa.co/v1/transaction/verify/{tx_ref}"

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ETFPL-MiniApp/1.0)"})


class ContestError(Exception):
    """A problem the user should be told about in plain words."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def _connect():
    return sqlite3.connect(DB_PATH, timeout=15)


def init_db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = _connect()
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(
        """CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            fpl_team_id INTEGER
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS entries (
            telegram_id INTEGER,
            gameweek INTEGER,
            paid INTEGER DEFAULT 0,
            tx_reference TEXT,
            PRIMARY KEY (telegram_id, gameweek)
        )"""
    )
    # Added for the Mini App — safe to run on an existing database.
    try:
        c.execute("ALTER TABLE users ADD COLUMN team_name TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    c.execute(
        """CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            kind TEXT,
            title TEXT,
            body TEXT,
            created_at INTEGER,
            is_read INTEGER DEFAULT 0
        )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications (telegram_id, id)")
    c.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            telegram_id INTEGER PRIMARY KEY,
            notify_deadline INTEGER DEFAULT 1
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS reminders_sent (
            telegram_id INTEGER,
            gameweek INTEGER,
            kind INTEGER,
            PRIMARY KEY (telegram_id, gameweek, kind)
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS prizes (
            telegram_id INTEGER,
            gameweek INTEGER,
            amount INTEGER,
            created_at INTEGER,
            PRIMARY KEY (telegram_id, gameweek)
        )"""
    )
    conn.commit()
    conn.close()


def db_execute(query, params=(), fetch=False, fetchone=False):
    conn = _connect()
    try:
        c = conn.cursor()
        c.execute(query, params)
        result = None
        if fetchone:
            result = c.fetchone()
        elif fetch:
            result = c.fetchall()
        conn.commit()
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# FPL API (cached so many users don't hammer the official servers)
# ---------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()


def fpl_get(path, ttl=60):
    """GET a path on the FPL API. Results are cached for `ttl` seconds, and if
    FPL is briefly down we serve the last good copy instead of failing."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(path)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        resp = HTTP.get(f"{FPL_API}/{path}", timeout=12)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        if hit:
            logger.warning("FPL request failed for %s, serving stale copy", path)
            return hit[1]
        raise
    with _cache_lock:
        _cache[path] = (now, data)
    return data


def bootstrap():
    return fpl_get("bootstrap-static/", 60)


def parse_deadline(value):
    # FPL gives ISO 8601 UTC, e.g. "2026-10-17T10:00:00Z"
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def get_live_gameweek():
    """The gameweek whose scores are being played / were just played
    (FPL's 'current' event). Used for leaderboards and winners."""
    events = bootstrap()["events"]
    for event in events:
        if event["is_current"]:
            return event["id"]
    for event in events:
        if event["is_next"]:
            return event["id"]
    return None


def get_entry_window():
    """The gameweek that is OPEN for entries right now: the first one whose
    FPL deadline is still in the future. Returns {"gameweek", "deadline"} or
    None if the season is over.

    Entries close at FPL's own deadline — the moment squads lock — so nobody
    can watch live scores and only pay once they know they're winning."""
    now = datetime.now(timezone.utc)
    for event in bootstrap()["events"]:
        deadline = parse_deadline(event["deadline_time"])
        if deadline > now:
            return {"gameweek": event["id"], "deadline": deadline}
    return None


def _history_score(team_id, gameweek):
    """Official score from FPL's history. Only filled in once a gameweek has finished."""
    try:
        data = fpl_get(f"entry/{team_id}/history/", 60)
    except Exception:
        return None
    for row in data.get("current", []):
        if row["event"] == gameweek:
            return row["points"]
    return None


def _live_score(team_id, gameweek):
    """Running score while a gameweek is being played: each starter's live
    points x multiplier (captain), with automatic substitutions applied.
    Returns None if FPL has no picks/live data yet."""
    try:
        live = fpl_get(f"event/{gameweek}/live/", 45)
        picks = fpl_get(f"entry/{team_id}/event/{gameweek}/picks/", 45)
    except Exception:
        return None
    pts = {e["id"]: e["stats"]["total_points"] for e in live.get("elements", [])}
    chip = picks.get("active_chip")
    weights = {}
    for p in picks["picks"]:
        if p["position"] <= 11:
            weights[p["element"]] = p["multiplier"]
        elif chip == "bboost":
            weights[p["element"]] = 1
    for sub in picks.get("automatic_subs") or []:
        weights.pop(sub["element_out"], None)
        weights[sub["element_in"]] = 1
    return sum(pts.get(el, 0) * w for el, w in weights.items())


def get_gameweek_score(team_id, gameweek):
    """A team's points for a gameweek (live while it's being played, official
    once finished), or None if unavailable."""
    try:
        event = next((e for e in bootstrap()["events"] if e["id"] == gameweek), None)
    except Exception:
        event = None
    if event and not event.get("finished"):
        score = _live_score(team_id, gameweek)
        if score is not None:
            return score
    return _history_score(team_id, gameweek)


def fetch_team_info(team_id):
    """Looks up an FPL team. Returns None if it doesn't exist; raises if FPL
    can't be reached (so callers can say 'try again' instead of 'invalid')."""
    try:
        data = fpl_get(f"entry/{team_id}/", 300)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise
    manager = f"{data.get('player_first_name', '')} {data.get('player_last_name', '')}".strip()
    return {"id": team_id, "name": data.get("name"), "manager": manager}


_idx = {"src": None, "elements": {}, "teams": {}}


def fpl_index():
    """(elements_by_id, teams_by_id) built from the cached bootstrap data."""
    b = bootstrap()
    if _idx["src"] is not b:
        _idx["elements"] = {e["id"]: e for e in b["elements"]}
        _idx["teams"] = {t["id"]: t for t in b["teams"]}
        _idx["src"] = b
    return _idx["elements"], _idx["teams"]


# ---------------------------------------------------------------------------
# Users & entries
# ---------------------------------------------------------------------------
def get_user(telegram_id):
    row = db_execute(
        "SELECT telegram_id, username, fpl_team_id, team_name FROM users WHERE telegram_id=?",
        (telegram_id,),
        fetchone=True,
    )
    if not row:
        return None
    return {"telegram_id": row[0], "username": row[1], "team_id": row[2], "team_name": row[3]}


def register_user(telegram_id, username, team_id, team_name):
    db_execute(
        "INSERT INTO users (telegram_id, username, fpl_team_id, team_name) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET fpl_team_id=excluded.fpl_team_id, "
        "username=excluded.username, team_name=excluded.team_name",
        (telegram_id, username, team_id, team_name),
    )


def get_entry(telegram_id, gameweek):
    row = db_execute(
        "SELECT paid, tx_reference FROM entries WHERE telegram_id=? AND gameweek=?",
        (telegram_id, gameweek),
        fetchone=True,
    )
    if not row:
        return None
    return {"paid": bool(row[0]), "tx_reference": row[1]}


def count_paid_entries(gameweek):
    row = db_execute(
        "SELECT COUNT(*) FROM entries WHERE gameweek=? AND paid=1", (gameweek,), fetchone=True
    )
    return row[0] if row else 0


def mark_paid(telegram_id, gameweek):
    """Marks an entry paid. True only the FIRST time (so we never double-notify)."""
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE entries SET paid=1 WHERE telegram_id=? AND gameweek=? AND paid=0",
            (telegram_id, gameweek),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Chapa payments
# ---------------------------------------------------------------------------
def chapa_headers():
    return {"Authorization": f"Bearer {CHAPA_SECRET_KEY}"}


def create_payment_link(tx_ref, amount, first_name, last_name, telegram_id):
    """Asks Chapa for a checkout URL the user can tap to pay."""
    payload = {
        "amount": str(amount),
        "currency": "ETB",
        # Chapa requires an email; users don't give us one, so we use a
        # placeholder tied to their Telegram ID. example.com is rejected by
        # Chapa's validator, so we use a real, universally-recognized domain.
        # Chapa never actually emails this address.
        "email": f"fplcontest.user{telegram_id}@gmail.com",
        "first_name": first_name or "FPL",
        "last_name": last_name or "Player",
        "tx_ref": tx_ref,
        "return_url": CHAPA_RETURN_URL,
        # Chapa limits customization.title to 16 characters — keep this short.
        "customization": {
            "title": "FPL Contest",
            "description": f"GW entry fee - {amount} ETB",
        },
    }
    resp = requests.post(CHAPA_INITIALIZE_URL, json=payload, headers=chapa_headers(), timeout=15)
    if resp.status_code != 200:
        logger.error(f"Chapa initialize rejected ({resp.status_code}): {resp.text}")
        return None
    data = resp.json()
    if data.get("status") == "success":
        return data["data"]["checkout_url"]
    logger.error(f"Chapa initialize returned non-success: {data}")
    return None


def verify_payment(tx_ref):
    """True if Chapa confirms this transaction succeeded."""
    resp = requests.get(CHAPA_VERIFY_URL.format(tx_ref=tx_ref), headers=chapa_headers(), timeout=15)
    if resp.status_code != 200:
        return False
    data = resp.json()
    return data.get("status") == "success" and data.get("data", {}).get("status") == "success"


def start_payment(telegram_id, first_name, last_name):
    """Shared by /pay and the app's Pay button. Returns
    {"checkout_url", "gameweek", "deadline"} or raises ContestError."""
    if not get_user(telegram_id):
        raise ContestError("not_registered", "Please register your FPL team ID first.")

    try:
        window = get_entry_window()
    except Exception:
        # Fail safe: if we can't confirm the deadline, block entry rather
        # than risk letting someone pay in after results are known.
        logger.exception("Could not check FPL deadline")
        raise ContestError("deadline_unknown", "Couldn't confirm the FPL deadline right now — please try again in a moment.")
    if not window:
        raise ContestError("closed", "⛔ No gameweek is open for entries right now.")

    gw = window["gameweek"]
    existing = get_entry(telegram_id, gw)
    if existing and existing["paid"]:
        raise ContestError("already_paid", f"You're already confirmed and entered for GW{gw}! ✅")

    tx_ref = f"fplgw{gw}-{telegram_id}-{int(time.time())}"
    try:
        checkout_url = create_payment_link(tx_ref, ENTRY_FEE_BIRR, first_name, last_name, telegram_id)
    except Exception:
        logger.exception("Chapa initialize failed")
        checkout_url = None
    if not checkout_url:
        raise ContestError("gateway", "Sorry, couldn't create a payment link right now — please try again in a moment.")

    db_execute(
        "INSERT INTO entries (telegram_id, gameweek, paid, tx_reference) VALUES (?, ?, 0, ?) "
        "ON CONFLICT(telegram_id, gameweek) DO UPDATE SET tx_reference=excluded.tx_reference",
        (telegram_id, gw, tx_ref),
    )
    return {"checkout_url": checkout_url, "gameweek": gw, "deadline": window["deadline"]}


def confirm_pending_for_user(telegram_id):
    """Checks this user's unpaid entries with Chapa right now. Returns the
    list of gameweeks newly confirmed."""
    rows = db_execute(
        "SELECT gameweek, tx_reference FROM entries WHERE telegram_id=? AND paid=0 AND tx_reference IS NOT NULL",
        (telegram_id,),
        fetch=True,
    )
    confirmed = []
    for gw, ref in rows:
        try:
            if verify_payment(ref) and mark_paid(telegram_id, gw):
                add_notification(
                    telegram_id, "payment", "Payment confirmed ✅",
                    f"You're entered in Gameweek {gw}. Good luck!",
                )
                confirmed.append(gw)
        except Exception:
            logger.exception(f"Payment verify failed for tx_ref {ref}")
    return confirmed


# ---------------------------------------------------------------------------
# Notifications & settings
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Wallet — a player's entry fees and prizes
# ---------------------------------------------------------------------------
def record_prize(telegram_id, gameweek, amount):
    """Called when the admin announces a winner. Safe to run twice for the
    same gameweek (it replaces the earlier record instead of duplicating)."""
    db_execute(
        "INSERT OR REPLACE INTO prizes (telegram_id, gameweek, amount, created_at) VALUES (?, ?, ?, ?)",
        (telegram_id, gameweek, int(round(amount)), int(time.time())),
    )


def wallet_data(telegram_id):
    entries = db_execute(
        "SELECT gameweek, paid FROM entries WHERE telegram_id=? ORDER BY gameweek DESC",
        (telegram_id,), fetch=True,
    )
    prizes = db_execute(
        "SELECT gameweek, amount FROM prizes WHERE telegram_id=? ORDER BY gameweek DESC",
        (telegram_id,), fetch=True,
    )
    spent = sum(ENTRY_FEE_BIRR for _, paid in entries if paid)
    won = sum(amount for _, amount in prizes)
    items = [
        {"type": "prize", "gameweek": gw, "amount": amount, "status": "won"} for gw, amount in prizes
    ] + [
        {"type": "entry", "gameweek": gw, "amount": -ENTRY_FEE_BIRR, "status": "paid" if paid else "pending"}
        for gw, paid in entries
    ]
    items.sort(key=lambda x: (-x["gameweek"], 0 if x["type"] == "prize" else 1))
    return {
        "won": won,
        "spent": spent,
        "net": won - spent,
        "entries": sum(1 for _, paid in entries if paid),
        "wins": len(prizes),
        "items": items,
    }


def add_notification(telegram_id, kind, title, body):
    db_execute(
        "INSERT INTO notifications (telegram_id, kind, title, body, created_at) VALUES (?, ?, ?, ?, ?)",
        (telegram_id, kind, title, body, int(time.time())),
    )


def list_notifications(telegram_id, limit=30):
    rows = db_execute(
        "SELECT id, kind, title, body, created_at, is_read FROM notifications "
        "WHERE telegram_id=? ORDER BY id DESC LIMIT ?",
        (telegram_id, limit),
        fetch=True,
    )
    return [
        {"id": r[0], "kind": r[1], "title": r[2], "body": r[3], "created_at": r[4], "read": bool(r[5])}
        for r in rows
    ]


def unread_count(telegram_id):
    row = db_execute(
        "SELECT COUNT(*) FROM notifications WHERE telegram_id=? AND is_read=0", (telegram_id,), fetchone=True
    )
    return row[0] if row else 0


def mark_all_read(telegram_id):
    db_execute("UPDATE notifications SET is_read=1 WHERE telegram_id=? AND is_read=0", (telegram_id,))


def get_notify_deadline(telegram_id):
    row = db_execute("SELECT notify_deadline FROM settings WHERE telegram_id=?", (telegram_id,), fetchone=True)
    return True if row is None else bool(row[0])


def set_notify_deadline(telegram_id, value):
    db_execute(
        "INSERT INTO settings (telegram_id, notify_deadline) VALUES (?, ?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET notify_deadline=excluded.notify_deadline",
        (telegram_id, 1 if value else 0),
    )


def reminder_targets(gameweek, kind):
    """Registered users who still need a `kind`-hour reminder for this gameweek."""
    rows = db_execute(
        "SELECT u.telegram_id FROM users u "
        "LEFT JOIN settings s ON s.telegram_id = u.telegram_id "
        "WHERE COALESCE(s.notify_deadline, 1) = 1 "
        "AND NOT EXISTS (SELECT 1 FROM entries e WHERE e.telegram_id = u.telegram_id AND e.gameweek = ? AND e.paid = 1) "
        "AND NOT EXISTS (SELECT 1 FROM reminders_sent r WHERE r.telegram_id = u.telegram_id AND r.gameweek = ? AND r.kind = ?)",
        (gameweek, gameweek, kind),
        fetch=True,
    )
    return [r[0] for r in rows]


def mark_reminded(telegram_id, gameweek, kind):
    db_execute(
        "INSERT OR IGNORE INTO reminders_sent (telegram_id, gameweek, kind) VALUES (?, ?, ?)",
        (telegram_id, gameweek, kind),
    )


# ---------------------------------------------------------------------------
# Contest leaderboard
# ---------------------------------------------------------------------------
_lb_cache = {}
_lb_lock = threading.Lock()


def contest_leaderboard(gameweek, ttl=45):
    """Everyone with a confirmed entry for `gameweek`, ranked by FPL points.
    Ties share a rank. Cached briefly because scores come from the FPL API."""
    now = time.time()
    with _lb_lock:
        hit = _lb_cache.get(gameweek)
    if ttl and hit and now - hit[0] < ttl:
        return hit[1]

    rows = db_execute(
        "SELECT u.telegram_id, u.username, u.fpl_team_id, u.team_name FROM entries e "
        "JOIN users u ON u.telegram_id = e.telegram_id WHERE e.gameweek=? AND e.paid=1",
        (gameweek,),
        fetch=True,
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        scores = list(pool.map(lambda r: get_gameweek_score(r[2], gameweek), rows))

    results = []
    for (tid, username, team_id, team_name), score in zip(rows, scores):
        results.append(
            {
                "telegram_id": tid,
                "username": username or "Player",
                "team_id": team_id,
                "team_name": team_name,
                "points": score if score is not None else 0,
            }
        )
    results.sort(key=lambda r: (-r["points"], r["username"].lower()))
    for i, r in enumerate(results):
        r["rank"] = results[i - 1]["rank"] if i and results[i - 1]["points"] == r["points"] else i + 1

    with _lb_lock:
        _lb_cache[gameweek] = (now, results)
    return results
