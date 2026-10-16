"""
Shared logic used by BOTH the Telegram bot and the Mini App API:
database, FPL API access (with caching), Telebirr payments, wallet, notifications and
the contest leaderboard. Keeping it in one place means the bot and the app
can never disagree about who is entered or who is winning.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from config import (
    BOT_TOKEN,
    ADMIN_TELEGRAM_ID,
    ENTRY_FEE_BIRR,
    MAX_RECEIPT_AGE_HOURS,
    MIN_WITHDRAWAL,
    SMS_ALLOWED_SENDERS,
    SMS_EXTRA_KEYWORDS,
    TELEBIRR_NUMBER,
    VERIFY_ET_API_KEY,
    VERIFY_ET_BASE_URL,
)

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "fpl_contest.db")
# The prize split, entry fee and minimum withdrawal are now EDITABLE in the Admin
# dashboard (see "Admin-editable settings" below) — no code change needed.

FPL_API = "https://fantasy.premierleague.com/api"

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ETFPL-MiniApp/1.0)"})


class ContestError(Exception):
    """A problem the user should be told about in plain words."""

    def __init__(self, code, message, alert=None):
        super().__init__(message)
        self.code = code
        self.message = message
        # Optional note for the ADMIN only (e.g. "verify.et is out of credits").
        # Never shown to players.
        self.alert = alert


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
    c.execute(
        """CREATE TABLE IF NOT EXISTS incoming_payments (
            tx_id TEXT PRIMARY KEY,
            amount REAL,
            sender_name TEXT,
            message TEXT,
            received_at INTEGER,
            status TEXT DEFAULT 'unused',
            used_by INTEGER,
            used_at INTEGER
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            amount INTEGER,
            account TEXT,
            status TEXT DEFAULT 'pending',
            created_at INTEGER,
            resolved_at INTEGER
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS wallet_credits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            amount INTEGER,
            reason TEXT,
            created_at INTEGER
        )"""
    )
    for ddl in (
        "ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0",
        "ALTER TABLE entries ADD COLUMN fee INTEGER",  # fee paid at the time (so changing the fee later never rewrites old pots)
    ):
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists
    c.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute(
        """CREATE TABLE IF NOT EXISTS message_templates (
            key TEXT PRIMARY KEY,
            text TEXT,
            media_token TEXT,
            updated_at INTEGER
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS media (
            token TEXT PRIMARY KEY,
            mime TEXT,
            data BLOB,
            tg_file_id TEXT,
            created_at INTEGER
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            action TEXT,
            detail TEXT,
            created_at INTEGER
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            text TEXT,
            media_token TEXT,
            audience TEXT,
            total INTEGER DEFAULT 0,
            sent INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            status TEXT DEFAULT 'sending',
            created_at INTEGER
        )"""
    )
    conn.commit()
    conn.close()
    _settings_cache["data"] = None
    # Entries made before the fee was recorded keep today's fee, so changing the fee later never rewrites old pots.
    db_execute("UPDATE entries SET fee=? WHERE fee IS NULL", (entry_fee(),))


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
# Admin-editable settings, message templates, pictures and audit log
# ---------------------------------------------------------------------------
_settings_cache = {"data": None}
_settings_lock = threading.Lock()

SETTING_DEFAULTS = {
    "entry_fee": ENTRY_FEE_BIRR,
    "prize_percents": [25, 15, 10],  # 1st, 2nd, 3rd … share of the pot, in percent
    "min_withdrawal": MIN_WITHDRAWAL,
    "reminder_hours": [24, 3],  # hours before the FPL deadline
    "maintenance": False,  # True = entries paused
    "maintenance_text": "Entries are paused for a moment. Please try again soon.",
    "registration_cap": 0,  # 0 = unlimited players
    "prize_text": "",  # e.g. "PS5 Slim" — shown to players on Home
    "prize_media": "",  # picture token for the prize
    "announce_winners_to_all": False,  # also message every entrant when winners are announced
    "extra_admins": [],  # Telegram IDs of co-admins (only the owner can change this)
}


def _clean_int(v, name, lo, hi):
    try:
        if isinstance(v, bool):
            raise ValueError
        n = float(v)
        if n != int(n):
            raise ValueError
        n = int(n)
    except (TypeError, ValueError):
        raise ContestError("bad_setting", f"{name} must be a whole number.")
    if not lo <= n <= hi:
        raise ContestError("bad_setting", f"{name} must be between {lo} and {hi}.")
    return n


def _clean_setting(key, v):
    if key == "entry_fee":
        return _clean_int(v, "Entry fee", 1, 100000)
    if key == "min_withdrawal":
        return _clean_int(v, "Minimum withdrawal", 1, 1000000)
    if key == "registration_cap":
        return _clean_int(v, "Player limit", 0, 1000000)
    if key == "prize_percents":
        if isinstance(v, str):
            v = [x for x in re.split(r"[,\s/]+", v.strip()) if x]
        if not isinstance(v, list) or not 1 <= len(v) <= 10:
            raise ContestError("bad_setting", "Give between 1 and 10 prize percentages, e.g. 25, 15, 10.")
        out = []
        for x in v:
            try:
                n = float(x)
            except (TypeError, ValueError):
                raise ContestError("bad_setting", "Prize percentages must be numbers.")
            if not 0 < n <= 100:
                raise ContestError("bad_setting", "Each prize percentage must be above 0 and at most 100.")
            out.append(int(n) if n == int(n) else round(n, 2))
        if sum(out) > 100:
            raise ContestError("bad_setting", f"The prizes add up to {sum(out):g}% — that is more than the whole pot (100%).")
        return out
    if key == "reminder_hours":
        if isinstance(v, str):
            v = [x for x in re.split(r"[,\s/]+", v.strip()) if x]
        if not isinstance(v, list) or len(v) > 3:
            raise ContestError("bad_setting", "Give up to 3 reminder times in hours, e.g. 24, 3 (or leave empty for none).")
        out = sorted({_clean_int(x, "Reminder time", 1, 168) for x in v}, reverse=True)
        return out
    if key in ("maintenance", "announce_winners_to_all"):
        return bool(v)
    if key == "maintenance_text":
        return str(v or "").strip()[:300] or SETTING_DEFAULTS["maintenance_text"]
    if key == "prize_text":
        return str(v or "").strip()[:200]
    if key == "prize_media":
        v = str(v or "").strip()
        if v and not db_execute("SELECT 1 FROM media WHERE token=?", (v,), fetchone=True):
            raise ContestError("bad_setting", "That picture no longer exists — upload it again.")
        return v
    if key == "extra_admins":
        if isinstance(v, str):
            v = [x for x in re.split(r"[,\s]+", v.strip()) if x]
        out = []
        for x in v or []:
            n = _clean_int(x, "Admin Telegram ID", 1, 10**13)
            if n != ADMIN_TELEGRAM_ID and n not in out:
                out.append(n)
        if len(out) > 5:
            raise ContestError("bad_setting", "At most 5 co-admins.")
        return out
    raise ContestError("bad_setting", f"Unknown setting: {key}")


def all_settings():
    with _settings_lock:
        if _settings_cache["data"] is None:
            data = {k: (list(v) if isinstance(v, list) else v) for k, v in SETTING_DEFAULTS.items()}
            try:
                rows = db_execute("SELECT key, value FROM app_settings", fetch=True)
            except sqlite3.OperationalError:
                rows = []  # tables not created yet
            for k, raw in rows:
                if k in data:
                    try:
                        data[k] = _clean_setting(k, json.loads(raw))
                    except (ContestError, ValueError):
                        pass  # a bad stored value falls back to the default
            _settings_cache["data"] = data
        return dict(_settings_cache["data"])


def get_setting(key):
    return all_settings()[key]


def update_settings(changes, admin_id):
    """Validates and saves settings. Returns the full settings dict.
    Raises ContestError("bad_setting", …) with a plain-words reason."""
    clean = {}
    for k, v in changes.items():
        if k not in SETTING_DEFAULTS:
            raise ContestError("bad_setting", f"Unknown setting: {k}")
        clean[k] = _clean_setting(k, v)
    before = all_settings()
    conn = _connect()
    try:
        for k, v in clean.items():
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (k, json.dumps(v)))
        conn.commit()
    finally:
        conn.close()
    with _settings_lock:
        _settings_cache["data"] = None
    changed = [f"{k}: {before[k]} → {v}" for k, v in clean.items() if before[k] != v]
    if changed:
        audit(admin_id, "settings", "; ".join(changed)[:900])
    return all_settings()


def entry_fee():
    return get_setting("entry_fee")


def min_withdrawal():
    return get_setting("min_withdrawal")


def prize_split():
    """1st, 2nd, 3rd … share of the pot as fractions, e.g. (0.25, 0.15, 0.10)."""
    return tuple(p / 100 for p in get_setting("prize_percents"))


def prize_share():
    return sum(prize_split())


def is_admin_id(telegram_id):
    return telegram_id == ADMIN_TELEGRAM_ID or telegram_id in get_setting("extra_admins")


def admin_ids():
    return [ADMIN_TELEGRAM_ID] + [a for a in get_setting("extra_admins") if a != ADMIN_TELEGRAM_ID]


def audit(admin_id, action, detail=""):
    db_execute(
        "INSERT INTO audit_log (admin_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
        (admin_id, action, str(detail)[:1000], int(time.time())),
    )


# --- message templates ------------------------------------------------------
# Every message the bot sends on its own. {words in braces} are filled in when
# sent. The admin can edit the text and attach a picture (sent as photo + caption).
TEMPLATES = {
    "welcome": {
        "label": "Welcome (/start)",
        "help": "First message a player sees.",
        "vars": ["fee", "winners", "split"],
        "default": (
            "Welcome to the FPL Weekly Contest! 🏆\n\n"
            "Entry fee: {fee} birr per gameweek. The {winners} best scores each gameweek win prizes ({split} of the pot).\n\n"
            "Tap the button below for the full app — leaderboard, league table, fixtures, your team and transfers.\n\n"
            "Or use commands:\n"
            "/register <your_FPL_team_id> - link your FPL team\n"
            "/pay - how to pay the entry fee (Telebirr)\n"
            "/deposit <transaction no. or SMS> - confirm your payment\n"
            "/logout - unlink a wrong FPL ID so you can link the right one\n"
            "/mystatus - check your registration & payment status\n"
            "/leaderboard - see this gameweek's top 10\n"
            "/app - open the Mini App\n\n"
            "Pay on Telebirr, then submit your transaction number — you're entered the moment it's verified."
        ),
    },
    "pay_instructions": {
        "label": "How to pay (/pay)",
        "help": "Payment steps shown after /pay.",
        "vars": ["fee", "gw", "deadline", "number", "name", "account"],
        "default": (
            "💰 Entry fee: {fee} birr for Gameweek {gw}\n"
            "⏰ Entries close {deadline}\n\n"
            "1. Send {fee} birr on Telebirr to:\n{account}\n"
            "2. Copy the transaction number from the Telebirr SMS you get.\n"
            "3. Send it here:  /deposit <transaction number>\n"
            "    (or paste it in the app: Wallet → Deposit)\n\n"
            "Your payment is checked automatically — you're entered within seconds."
        ),
    },
    "deposit_success": {
        "label": "Payment confirmed",
        "help": "Sent after a deposit is verified. {credit_line} is filled only when the player sent extra money.",
        "vars": ["gw", "credit", "credit_line"],
        "default": "✅ Payment verified! You're entered in GW{gw}. Good luck!{credit_line}",
    },
    "reminder": {
        "label": "Deadline reminder",
        "help": "Sent before entries close to players who haven't entered. (Timing: Settings → Reminder times.)",
        "vars": ["gw", "left", "deadline"],
        "default": (
            "⏰ GW{gw} entries close soon\n{left} left to enter Gameweek {gw} ({deadline}).\n\n"
            "You can switch these reminders off in the app (🔔)."
        ),
    },
    "winner_dm": {
        "label": "Winner congratulation",
        "help": "Sent privately to each winner when you announce winners.",
        "vars": ["gw", "rank", "points", "prize"],
        "default": "🎉 Congrats! You finished #{rank} in GW{gw} with {points} points. Prize: {prize} birr!",
    },
    "winners_all": {
        "label": "Winners announcement (to everyone)",
        "help": "Sent to every entrant when winners are announced — only if Settings → 'Tell everyone the winners' is on.",
        "vars": ["gw", "winners"],
        "default": "🏆 GW{gw} winners\n{winners}\n\nThanks for playing — see you next gameweek!",
    },
}


def render(text, **vars):
    """Fills {name} placeholders. Unknown or malformed ones are left as they are, never an error."""
    return re.sub(r"\{(\w+)\}", lambda m: str(vars[m.group(1)]) if m.group(1) in vars else m.group(0), text or "")


def get_template(key):
    """{"key","text","media_token","custom"} — the saved text, or the built-in default."""
    row = db_execute("SELECT text, media_token FROM message_templates WHERE key=?", (key,), fetchone=True)
    text = row[0] if row and row[0] else None
    return {
        "key": key,
        "text": text if text else TEMPLATES[key]["default"],
        "media_token": (row[1] if row else None) or "",
        "custom": bool(text),
    }


def set_template(key, admin_id, text=None, media_token=None):
    """text: new text ('' resets to the default); media_token: new picture ('' removes it). None = leave alone."""
    if key not in TEMPLATES:
        raise ContestError("bad_template", "Unknown message.")
    cur = get_template(key)
    if text is not None:
        text = text.strip()
        if len(text) > 3500:
            raise ContestError("bad_template", "That message is too long (3500 characters at most).")
    new_text = cur["text"] if text is None else text
    if text == "":
        new_text = ""
    new_media = cur["media_token"] if media_token is None else media_token
    if new_media and not db_execute("SELECT 1 FROM media WHERE token=?", (new_media,), fetchone=True):
        raise ContestError("bad_template", "That picture no longer exists — upload it again.")
    if not cur["custom"] and text is None:
        new_text = ""  # only the picture changed; keep using the default text
    db_execute(
        "INSERT OR REPLACE INTO message_templates (key, text, media_token, updated_at) VALUES (?, ?, ?, ?)",
        (key, new_text, new_media, int(time.time())),
    )
    audit(admin_id, "message", f"{key}: " + ("text reset" if text == "" else "text edited" if text is not None else "picture changed"))


# --- pictures -----------------------------------------------------------------
MAX_IMAGE_BYTES = 6 * 1024 * 1024


def sniff_image(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def save_media(data):
    mime = sniff_image(data)
    if not mime:
        raise ContestError("bad_image", "Please choose a PNG, JPG or WEBP picture.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ContestError("bad_image", "That picture is too big (6 MB at most).")
    token = secrets.token_hex(8)
    db_execute(
        "INSERT INTO media (token, mime, data, created_at) VALUES (?, ?, ?, ?)", (token, mime, data, int(time.time()))
    )
    return token


def get_media(token):
    """(mime, bytes, telegram_file_id) or None."""
    if not token:
        return None
    row = db_execute("SELECT mime, data, tg_file_id FROM media WHERE token=?", (token,), fetchone=True)
    return (row[0], bytes(row[1]), row[2]) if row else None


def set_media_file_id(token, file_id):
    db_execute("UPDATE media SET tg_file_id=? WHERE token=?", (file_id, token))


def media_url(token):
    return f"/media/{token}" if token else ""


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


def lb_key(gameweek, telegram_id):
    """A private tag for one leaderboard row, so the app can ask for a player's
    team without ever being shown their Telegram ID. Only this server can make it."""
    msg = f"lb:{gameweek}:{telegram_id}".encode()
    return hmac.new(BOT_TOKEN.encode(), msg, hashlib.sha256).hexdigest()[:16]


def gameweek_started(gameweek):
    """True once FPL's deadline for this gameweek has passed (squads are locked and public).
    Before that, other players' squads stay hidden so nobody can copy them."""
    for event in bootstrap()["events"]:
        if event["id"] == gameweek:
            return parse_deadline(event["deadline_time"]) <= datetime.now(timezone.utc)
    return False


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
def get_user(telegram_id, include_unlinked=False):
    """The player, or None. Someone who logged out (no FPL team linked) counts as
    not registered unless include_unlinked=True (used for blocks, wallets, admin)."""
    row = db_execute(
        "SELECT telegram_id, username, fpl_team_id, team_name, COALESCE(banned, 0) FROM users WHERE telegram_id=?",
        (telegram_id,),
        fetchone=True,
    )
    if not row or (row[2] is None and not include_unlinked):
        return None
    return {"telegram_id": row[0], "username": row[1], "team_id": row[2], "team_name": row[3], "banned": bool(row[4])}


def check_can_register(telegram_id):
    """Raises ContestError if this person may not link a team (blocked, or the player limit is reached)."""
    me = get_user(telegram_id, include_unlinked=True)
    if me and me["banned"]:
        raise ContestError("banned", "Your account has been blocked. Please contact the admin.")
    cap = get_setting("registration_cap")
    if not me and cap:
        n = db_execute("SELECT COUNT(*) FROM users", fetchone=True)[0]
        if n >= cap:
            raise ContestError("full", "Registration is full right now. Please contact the admin.")


def team_change_lock(telegram_id):
    """The gameweek that stops this player changing their FPL team, or None.
    A paid entry is tied to the team it was entered with, so the team can't be
    swapped once that gameweek's deadline has passed until the winners are
    announced (otherwise someone could switch to whichever team scored best)."""
    gws = [r[0] for r in db_execute("SELECT gameweek FROM entries WHERE telegram_id=? AND paid=1", (telegram_id,), fetch=True)]
    if not gws:
        return None
    try:
        deadlines = {e["id"]: parse_deadline(e["deadline_time"]) for e in bootstrap()["events"]}
    except Exception:
        logger.exception("Could not check deadlines for a team change")
        raise ContestError("deadline_unknown", "Couldn't check the FPL deadline right now — please try again in a moment.")
    now = datetime.now(timezone.utc)
    for gw in sorted(gws):
        if deadlines.get(gw) and deadlines[gw] <= now and not db_execute(
            "SELECT 1 FROM prizes WHERE gameweek=? LIMIT 1", (gw,), fetchone=True
        ):
            return gw
    return None


def _locked_error(gw):
    return ContestError(
        "team_locked",
        f"You can't change your FPL team while Gameweek {gw} is still being scored — your entry belongs to the team "
        "you entered with. If you linked the wrong one, please contact the admin.",
    )


def check_can_change_team(telegram_id, new_team_id):
    """Raises if linking `new_team_id` would swap the team behind a paid, unscored entry."""
    me = get_user(telegram_id)
    if me and me["team_id"] != new_team_id:
        gw = team_change_lock(telegram_id)
        if gw:
            raise _locked_error(gw)


def unlink_team(telegram_id):
    """'Log out': forgets the linked FPL team so the player can link a different one.
    Keeps their wallet, entries and any block. Returns the team that was unlinked."""
    me = get_user(telegram_id)
    if not me:
        raise ContestError("not_linked", "No FPL team is linked to your account.")
    gw = team_change_lock(telegram_id)
    if gw:
        raise _locked_error(gw)
    db_execute("UPDATE users SET fpl_team_id=NULL, team_name=NULL WHERE telegram_id=?", (telegram_id,))
    _lb_cache.clear()
    logger.info("Player %s logged out of FPL team %s", telegram_id, me["team_id"])
    return {"team_id": me["team_id"], "team_name": me["team_name"]}


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


def gameweek_pot(gameweek):
    """Total entry fees collected for a gameweek (each entry counts the fee it was paid at)."""
    row = db_execute(
        "SELECT COALESCE(SUM(COALESCE(fee, ?)), 0) FROM entries WHERE gameweek=? AND paid=1",
        (entry_fee(), gameweek), fetchone=True,
    )
    return int(row[0]) if row else 0


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
# Telebirr payments — matched against SMS forwarded from the receiving phone
# ---------------------------------------------------------------------------
_TX_LABELED = re.compile(
    r"(?:transaction|txn|trx|የግብይት)\s*(?:number|no\.?|id|code|ቁጥር\w*)?\s*(?:is|:|-)?\s*((?=[A-Za-z0-9]*\d)[A-Za-z0-9]{8,14})\b",
    re.I,
)
_TX_LOOSE = re.compile(r"\b((?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{8,14})\b")
_TX_ONLY = re.compile(r"^[A-Za-z0-9]{8,14}$")
_AMOUNT_RECEIVED = re.compile(r"(?:received|ተቀብለዋል)\s+(?:an\s+amount\s+of\s+)?(?:ETB|Birr|ብር)\s*([\d,]+(?:\.\d+)?)", re.I)
_AMOUNTS = (
    re.compile(r"(?:ETB|Birr|ብር)\s*([\d,]+(?:\.\d+)?)", re.I),
    re.compile(r"([\d,]+(?:\.\d+)?)\s*(?:ETB|Birr|ብር)", re.I),
)
_SENDER = re.compile(r"from\s+(.+?)\s*(?:\(|\[|-\s|\d{6,}|on\s|\.)", re.I)
_INCOMING_WORDS = ("received", "ተቀብለዋል", "ተቀብለዋል") + tuple(SMS_EXTRA_KEYWORDS)


def extract_tx_id(text):
    """Pulls a Telebirr transaction number out of what the player typed:
    either the bare number or the whole SMS. Returns UPPERCASE or None."""
    text = (text or "").strip()
    if _TX_ONLY.match(text):
        return text.upper()
    m = _TX_LABELED.search(text) or _TX_LOOSE.search(text.upper())
    return m.group(1).upper() if m else None


def parse_sms(message):
    """Reads a Telebirr SMS. Returns {tx_id, amount, sender_name, incoming}."""
    message = message or ""
    m = _AMOUNT_RECEIVED.search(message)
    if not m:
        for pat in _AMOUNTS:
            m = pat.search(message)
            if m:
                break
    amount = None
    if m:
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    sm = _SENDER.search(message)
    low = message.lower()
    return {
        "tx_id": extract_tx_id(message),
        "amount": amount,
        "sender_name": sm.group(1).strip() if sm else None,
        "incoming": any(w in low for w in _INCOMING_WORDS),
    }


def store_incoming(tx_id, amount, sender_name, message, received_at=None):
    """Saves a received payment as 'unused'. False if we already had it."""
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO incoming_payments (tx_id, amount, sender_name, message, received_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'unused')",
            (tx_id.upper(), amount, sender_name, message, int(received_at or time.time())),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def handle_sms(sender, message):
    """Called for every SMS the phone forwards. Only genuine 'money received'
    messages from Telebirr are stored."""
    if (sender or "").strip().lower() not in SMS_ALLOWED_SENDERS:
        return {"stored": False, "reason": "sender not allowed"}
    p = parse_sms(message)
    if not p["incoming"]:
        return {"stored": False, "reason": "not a received-payment SMS"}
    if not p["tx_id"] or p["amount"] is None:
        logger.warning("Incoming SMS matched but no transaction number/amount could be read")
        return {"stored": False, "reason": "could not read transaction number or amount"}
    new = store_incoming(p["tx_id"], p["amount"], p["sender_name"], message)
    logger.info("SMS payment %s %s birr (%s)", p["tx_id"], p["amount"], "new" if new else "duplicate")
    return {"stored": new, "tx_id": p["tx_id"], "amount": p["amount"]}


# ---------------------------------------------------------------------------
# verify.et — a third-party service that confirms a Telebirr payment is real
# (no phone has to stay on). We send the transaction number and OUR number;
# it answers with the amount, who received it, and when.
# Docs: https://verify.et/docs/api
# ---------------------------------------------------------------------------
VERIFY_ET_ENABLED = bool(VERIFY_ET_API_KEY)
_sleep = time.sleep  # swapped out in tests so polling doesn't actually wait

NOT_FOUND_TEXT = (
    "❌ Transaction number incorrect — we couldn't confirm that payment. Check the number and try again "
    "(a brand-new payment can take a minute to show up)."
)
_DOWN_TEXT = "Payment verification is temporarily unavailable. Your payment is safe — please try again in a few minutes."
_ERROR_TEXT = (
    "Payment verification is temporarily unavailable and the admin has been told. "
    "Your payment is safe — please try again later."
)
_BUSY_TEXT = "Verification is busy right now — please try again in a minute. Nothing has been lost."
# Wrong answers count towards the hourly limit; "service down / busy" never does.
_FAIL_CODES = {"bad_tx", "not_found", "used", "wrong_receiver", "too_old", "bad_date", "bad_currency"}


def _canonical_phone(value):
    """An Ethiopian mobile number as '2519XXXXXXXX' ('*' kept for masked digits),
    or None if it doesn't look like one. Accepts 09…, +2519…, 2519…, 9…"""
    s = re.sub(r"[^0-9*]", "", value or "")
    if s.startswith("0"):
        s = "251" + s[1:]
    elif len(s) == 9:
        s = "251" + s
    return s if len(s) == 12 and s.startswith("251") else None


def receiver_matches(ours, theirs):
    """Is `theirs` (possibly masked, like 2519****0897) our number?
    True / False, or None when we can't tell (missing or unreadable number)."""
    a, b = _canonical_phone(ours), _canonical_phone(theirs)
    if not a or not b or "*" in a:
        return None
    return all(y == "*" or x == y for x, y in zip(a, b))


def _headers_et():
    return {"x-api-key": VERIFY_ET_API_KEY, "Accept": "application/json"}


def _body_of(resp):
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _still_processing(resp):
    if resp.status_code == 202:
        return True
    v = _body_of(resp).get("verification")
    return isinstance(v, dict) and str(v.get("processingStatus", "")).lower() in ("queued", "running")


def _status_url(info):
    """Where to poll for a queued check. Only ever our verify.et host, so the
    API key can't be sent anywhere else."""
    links = info.get("links") if isinstance(info.get("links"), dict) else {}
    url = links.get("statusUrl") or info.get("statusUrl") or ""
    if url.startswith("/"):
        return VERIFY_ET_BASE_URL + url
    if url.startswith(VERIFY_ET_BASE_URL + "/"):
        return url
    rid = info.get("requestId") or (info.get("verification") or {}).get("requestId")
    return f"{VERIFY_ET_BASE_URL}/api/verify/{rid}" if rid else None


def _parse_when(value):
    """verify.et gives ISO 8601 UTC, e.g. 2026-02-20T12:00:00.000Z → epoch seconds (or None)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _verify_et_lookup(tx_id):
    """Asks verify.et about a Telebirr transaction number.

    Returns the receipt as a plain dict, or None if verify.et says it can't
    find / confirm that payment. Raises ContestError when verify.et itself
    can't be used right now (down, busy, out of credits, wrong API key)."""
    payload = {"bank": "telebirr", "transactionNumber": tx_id}
    ours = _canonical_phone(TELEBIRR_NUMBER)
    if ours:
        payload["settlementAccount"] = "0" + ours[3:]  # e.g. 0939080897 — the account the money must have reached
    try:
        resp = HTTP.post(
            f"{VERIFY_ET_BASE_URL}/api/verify",
            params={"waitMs": 8000},  # let verify.et answer straight away when it can
            json=payload,
            headers=_headers_et(),
            timeout=20,
        )
        polls = 0
        while _still_processing(resp) and polls < 6:  # queued: check back a few times
            polls += 1
            info = _body_of(resp)
            url = _status_url(info)
            if not url:
                break
            wait = ((info.get("links") or {}).get("pollAfterMs") or 1500) / 1000
            _sleep(min(max(wait, 1.0), 3.0))
            resp = HTTP.get(url, headers=_headers_et(), timeout=15)
    except requests.RequestException as e:
        logger.warning("verify.et unreachable: %s", e)
        raise ContestError("provider_down", _DOWN_TEXT)

    code = resp.status_code
    if _still_processing(resp):
        raise ContestError("provider_busy", "Still being checked — please try again in a minute. Nothing has been lost.")
    if code in (401, 403):
        raise ContestError(
            "provider_error", _ERROR_TEXT,
            alert=f"verify.et refused the API key (HTTP {code}). Check VERIFY_ET_API_KEY on Railway, and that the key "
                  "has the 'verification:write' permission. Players can't be verified until this is fixed.",
        )
    if code == 402:
        raise ContestError(
            "provider_error", _ERROR_TEXT,
            alert="verify.et says you are out of verification credits. Top up at verify.et — "
                  "players can't be verified until you do (you can still use /addpayment by hand).",
        )
    if code in (408, 429):
        raise ContestError("provider_busy", _BUSY_TEXT)
    if code >= 500:
        logger.warning("verify.et server error %s", code)
        raise ContestError("provider_down", _DOWN_TEXT)
    if code != 200:
        # 400 / 404 / 409 / 422 …: verify.et didn't recognise the payment.
        logger.info("verify.et said HTTP %s for %s: %s", code, tx_id, (resp.text or "")[:300])
        return None

    data = _body_of(resp).get("data")
    rows = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
    row = rows[0] if rows and isinstance(rows[0], dict) else None
    if not row:
        return None
    if row.get("verified") is not True or str(row.get("status") or "success").lower() != "success":
        return None

    try:
        amount = float(str(row.get("amount")).replace(",", ""))
    except ValueError:
        amount = None
    return {
        "amount": amount,
        "currency": row.get("currency"),
        "sender_name": row.get("senderName"),
        "receiver_name": row.get("receiverName"),
        "receiver_account": row.get("receiverAccount"),
        "paid_at": _parse_when(row.get("timestamp")),
        "settlement": row.get("settlementAccountMatch"),
    }


verify_health = {"last_ok": None, "last_error": None, "last_error_code": None, "calls": 0, "errors": 0}


def verify_et_lookup(tx_id):
    """Same as _verify_et_lookup, but remembers whether verify.et is healthy (shown on the Admin dashboard)."""
    verify_health["calls"] += 1
    try:
        r = _verify_et_lookup(tx_id)
    except ContestError as e:
        verify_health["errors"] += 1
        verify_health["last_error"] = int(time.time())
        verify_health["last_error_code"] = e.code
        raise
    verify_health["last_ok"] = int(time.time())
    return r


def validate_receipt(r):
    """Safety checks on a verified receipt. Raises ContestError if it must not
    be accepted: unreadable, wrong currency, paid to someone else, or too old."""
    if not TELEBIRR_NUMBER:
        raise ContestError("not_configured", "Payments aren't set up yet — please contact the admin.")
    if r["amount"] is None:
        raise ContestError(
            "unreadable", "We confirmed the payment but couldn't read its amount — please contact the admin.",
            alert="verify.et confirmed a payment but sent no readable amount. Check it on verify.et and use /addpayment.",
        )
    if r["currency"] and str(r["currency"]).strip().upper() not in ("ETB", "BIRR"):
        raise ContestError("bad_currency", "❌ That payment wasn't in birr.")

    sam = r["settlement"]
    api_ok = isinstance(sam, dict) and sam.get("matched") is True
    own = receiver_matches(TELEBIRR_NUMBER, r["receiver_account"]) if r["receiver_account"] else None
    # verify.et must confirm the receiver (or, if it sent no verdict, our own comparison must);
    # and if we can tell it's a different number ourselves, that always wins.
    if not ((api_ok or (sam is None and own is True)) and own is not False):
        raise ContestError(
            "wrong_receiver",
            f"❌ That payment wasn't sent to our Telebirr account ({TELEBIRR_NUMBER}). "
            "Please send the entry fee to the number shown and submit that payment's transaction number.",
        )

    paid_at = r["paid_at"]
    if paid_at is None:
        logger.warning("verify.et receipt had no readable date — age check skipped")
    elif paid_at > time.time() + 600:
        raise ContestError("bad_date", "❌ That payment has a date in the future, so it can't be accepted.")
    elif time.time() - paid_at > MAX_RECEIPT_AGE_HOURS * 3600:
        raise ContestError(
            "too_old", "❌ That payment is too old to use for this entry. Please send a new payment and submit its number."
        )


_alert_sent = {}


def admin_alert_for(err):
    """Text to send the admin about this error — at most once an hour per kind,
    so a busy day doesn't spam them. None if there's nothing to send now."""
    if not getattr(err, "alert", None):
        return None
    now = time.time()
    if now - _alert_sent.get(err.code, 0) < 3600:
        return None
    _alert_sent[err.code] = now
    return "⚠️ " + err.alert


def _ensure_payment_known(tx_id):
    """Makes sure this transaction is in our payments table. If we've never seen
    it (no SMS, nothing added by hand) and verify.et is switched on, ask verify.et;
    a payment it confirms is checked and then saved as 'unused' — the normal
    claim step below then marks it used. Saved results mean a repeat attempt
    never costs another verify.et credit."""
    if db_execute("SELECT 1 FROM incoming_payments WHERE tx_id=?", (tx_id,), fetchone=True) or not VERIFY_ET_ENABLED:
        return
    receipt = verify_et_lookup(tx_id)
    if receipt is None:
        raise ContestError("not_found", NOT_FOUND_TEXT)
    validate_receipt(receipt)
    store_incoming(
        tx_id, receipt["amount"], receipt["sender_name"],
        f"verify.et · to {receipt['receiver_account'] or '?'}", receipt["paid_at"],
    )


def check_can_enter(telegram_id):
    """Raises ContestError if this player can't enter right now; otherwise
    returns the open entry window {"gameweek", "deadline"}."""
    me = get_user(telegram_id)
    if not me:
        raise ContestError("not_registered", "Please link your FPL team first (on the Home screen).")
    if me["banned"]:
        raise ContestError("banned", "Your account has been blocked from entering. Please contact the admin.")
    if get_setting("maintenance"):
        raise ContestError("maintenance", "⏸️ " + get_setting("maintenance_text"))
    try:
        window = get_entry_window()
    except Exception:
        # Fail safe: if we can't confirm the deadline, block entry rather
        # than risk letting someone pay in after results are known.
        logger.exception("Could not check FPL deadline")
        raise ContestError("deadline_unknown", "Couldn't confirm the FPL deadline right now — please try again in a moment.")
    if not window:
        raise ContestError("closed", "⛔ No gameweek is open for entries right now.")
    existing = get_entry(telegram_id, window["gameweek"])
    if existing and existing["paid"]:
        raise ContestError("already_paid", f"You're already confirmed and entered for GW{window['gameweek']}! ✅")
    return window


_fail_log = {}
_fail_lock = threading.Lock()
MAX_FAILS_PER_HOUR = 6


def _too_many_fails(telegram_id):
    now = time.time()
    with _fail_lock:
        recent = [t for t in _fail_log.get(telegram_id, []) if now - t < 3600]
        _fail_log[telegram_id] = recent
        return len(recent) >= MAX_FAILS_PER_HOUR


def _record_fail(telegram_id):
    with _fail_lock:
        _fail_log.setdefault(telegram_id, []).append(time.time())


def claim_deposit(telegram_id, text):
    """The player submitted a transaction number / SMS. If it matches an
    unused received payment of at least the entry fee, they're entered.
    Returns {"gameweek", "amount", "credit"} or raises ContestError."""
    window = check_can_enter(telegram_id)
    gw = window["gameweek"]
    if _too_many_fails(telegram_id):
        raise ContestError("rate_limited", "Too many wrong attempts. Please wait a while and try again.")

    tx_id = extract_tx_id(text)
    if not tx_id:
        _record_fail(telegram_id)
        raise ContestError("bad_tx", "❌ I couldn't find a transaction number in that. Paste the full Telebirr SMS or just the transaction number.")

    try:
        _ensure_payment_known(tx_id)  # asks verify.et if we've never seen this payment
    except ContestError as e:
        if e.code in _FAIL_CODES:
            _record_fail(telegram_id)
        raise

    conn = _connect()
    conn.isolation_level = None  # we run the transaction ourselves
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount, status FROM incoming_payments WHERE tx_id=?", (tx_id,)
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            _record_fail(telegram_id)
            raise ContestError("not_found", NOT_FOUND_TEXT)
        amount, status = row
        if status != "unused":
            conn.execute("ROLLBACK")
            _record_fail(telegram_id)
            raise ContestError("used", "❌ That transaction number has already been used.")
        fee = entry_fee()
        if amount < fee:
            conn.execute("ROLLBACK")
            raise ContestError(
                "too_small",
                f"That payment is {amount:g} birr but the entry fee is {fee} birr. "
                "Please contact the admin.",
            )
        now = int(time.time())
        conn.execute(
            "UPDATE incoming_payments SET status='used', used_by=?, used_at=? WHERE tx_id=?",
            (telegram_id, now, tx_id),
        )
        conn.execute(
            "INSERT INTO entries (telegram_id, gameweek, paid, tx_reference, fee) VALUES (?, ?, 1, ?, ?) "
            "ON CONFLICT(telegram_id, gameweek) DO UPDATE SET paid=1, tx_reference=excluded.tx_reference, fee=excluded.fee",
            (telegram_id, gw, tx_id, fee),
        )
        credit = int(amount - fee)  # anything extra they sent goes to their wallet
        if credit > 0:
            conn.execute(
                "INSERT INTO wallet_credits (telegram_id, amount, reason, created_at) VALUES (?, ?, ?, ?)",
                (telegram_id, credit, f"Extra sent with GW{gw} entry ({tx_id})", now),
            )
        conn.execute("COMMIT")
    except ContestError:
        raise
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()

    add_notification(telegram_id, "payment", "Payment confirmed ✅", f"You're entered in Gameweek {gw}. Good luck!")
    return {"gameweek": gw, "amount": amount, "credit": credit}


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


def prize_allocation(results, pot):
    """Who wins what: {telegram_id: birr}. `results` is the ranked leaderboard
    (ties share a rank). Players tied across prize places split those places
    equally — e.g. two tied for 1st share the 1st + 2nd prizes, and the next
    player is 3rd. The split comes from the Admin settings."""
    split = prize_split()
    out = {}
    i = 0
    while i < len(results):
        rank = results[i]["rank"]
        group = [r for r in results[i:] if r["rank"] == rank]
        share = sum(split[p - 1] for p in range(rank, rank + len(group)) if p <= len(split))
        if share:
            for r in group:
                out[r["telegram_id"]] = round(pot * share / len(group))
        i += len(group)
    return out


def set_gameweek_prizes(gameweek, allocation):
    """Saves a gameweek's prizes (replacing any earlier announcement of the same gameweek)."""
    db_execute("DELETE FROM prizes WHERE gameweek=?", (gameweek,))
    for telegram_id, amount in allocation.items():
        record_prize(telegram_id, gameweek, amount)


def wallet_balance(telegram_id, conn=None):
    """Prizes + credits − withdrawals (pending or paid). Entry fees are paid
    outside the app via Telebirr, so they don't touch the balance."""
    own = conn is None
    conn = conn or _connect()
    try:
        won = conn.execute("SELECT COALESCE(SUM(amount),0) FROM prizes WHERE telegram_id=?", (telegram_id,)).fetchone()[0]
        cred = conn.execute("SELECT COALESCE(SUM(amount),0) FROM wallet_credits WHERE telegram_id=?", (telegram_id,)).fetchone()[0]
        out = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE telegram_id=? AND status IN ('pending','paid')",
            (telegram_id,),
        ).fetchone()[0]
        return won + cred - out
    finally:
        if own:
            conn.close()


def wallet_data(telegram_id):
    entries = db_execute(
        "SELECT e.gameweek, COALESCE(e.fee, ?), COALESCE(p.used_at, 0) FROM entries e "
        "LEFT JOIN incoming_payments p ON p.tx_id = e.tx_reference "
        "WHERE e.telegram_id=? AND e.paid=1",
        (entry_fee(), telegram_id), fetch=True,
    )
    prizes = db_execute("SELECT gameweek, amount, created_at FROM prizes WHERE telegram_id=?", (telegram_id,), fetch=True)
    credits = db_execute("SELECT amount, created_at, reason FROM wallet_credits WHERE telegram_id=?", (telegram_id,), fetch=True)
    wds = db_execute(
        "SELECT id, amount, status, created_at FROM withdrawals WHERE telegram_id=?", (telegram_id,), fetch=True
    )
    items = (
        [{"type": "prize", "gameweek": gw, "amount": amt, "ts": ts} for gw, amt, ts in prizes]
        + [{"type": "credit", "amount": amt, "ts": ts, "reason": reason or ""} for amt, ts, reason in credits]
        + [{"type": "entry", "gameweek": gw, "amount": fee, "ts": ts} for gw, fee, ts in entries]
        + [{"type": "withdrawal", "id": i, "amount": amt, "status": st, "ts": ts} for i, amt, st, ts in wds]
    )
    items.sort(key=lambda x: (-(x["ts"] or 0), -(x.get("gameweek") or 0)))
    return {
        "balance": wallet_balance(telegram_id),
        "min_withdrawal": min_withdrawal(),
        "has_pending_withdrawal": any(w[2] == "pending" for w in wds),
        "items": items,
    }


_PHONE = re.compile(r"^(?:\+?251|0)?(9|7)\d{8}$")


def request_withdrawal(telegram_id, amount, account):
    """Winner asks to withdraw from their wallet. Returns the new request or raises ContestError."""
    account = re.sub(r"[\s-]", "", account or "")
    if not _PHONE.match(account):
        raise ContestError("bad_account", "Please enter a valid Telebirr phone number, e.g. 0912345678.")
    me = get_user(telegram_id, include_unlinked=True)
    if me and me["banned"]:
        raise ContestError("banned", "Your account has been blocked. Please contact the admin.")
    if amount < min_withdrawal():
        raise ContestError("too_small", f"The minimum withdrawal is {min_withdrawal()} birr.")
    conn = _connect()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM withdrawals WHERE telegram_id=? AND status='pending'", (telegram_id,)).fetchone():
            conn.execute("ROLLBACK")
            raise ContestError("pending", "You already have a withdrawal waiting for the admin.")
        balance = wallet_balance(telegram_id, conn)
        if amount > balance:
            conn.execute("ROLLBACK")
            raise ContestError("insufficient", f"You only have {balance} birr available.")
        cur = conn.execute(
            "INSERT INTO withdrawals (telegram_id, amount, account, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (telegram_id, amount, account, int(time.time())),
        )
        wid = cur.lastrowid
        conn.execute("COMMIT")
    except ContestError:
        raise
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return {"id": wid, "amount": amount, "account": account, "balance": wallet_balance(telegram_id)}


def get_withdrawal(wid):
    row = db_execute(
        "SELECT id, telegram_id, amount, account, status FROM withdrawals WHERE id=?", (wid,), fetchone=True
    )
    if not row:
        return None
    return dict(zip(("id", "telegram_id", "amount", "account", "status"), row))


def pending_withdrawals():
    rows = db_execute("SELECT id FROM withdrawals WHERE status='pending' ORDER BY id", fetch=True)
    return [get_withdrawal(r[0]) for r in rows]


def resolve_withdrawal(wid, status):
    """Admin marks a request 'paid' or 'rejected' (a rejected amount returns to
    the wallet automatically). None if it was already handled."""
    assert status in ("paid", "rejected")
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE withdrawals SET status=?, resolved_at=? WHERE id=? AND status='pending'",
            (status, int(time.time()), wid),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_withdrawal(wid)


def withdrawal_text(w):
    me = get_user(w["telegram_id"], include_unlinked=True)
    who = (f"{me['username']} · FPL \"{me['team_name']}\"" if me["team_name"] else f"{me['username']} (no FPL team linked)") if me else str(w["telegram_id"])
    return f"💸 Withdrawal request #{w['id']}\nPlayer: {who}\nAmount: {w['amount']} birr\nSend to Telebirr: {w['account']}"


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
        "WHERE u.fpl_team_id IS NOT NULL AND COALESCE(s.notify_deadline, 1) = 1 "
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
        "JOIN users u ON u.telegram_id = e.telegram_id WHERE e.gameweek=? AND e.paid=1 AND u.fpl_team_id IS NOT NULL",
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
