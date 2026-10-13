"""
Shared logic used by BOTH the Telegram bot and the Mini App API:
database, FPL API access (with caching), Telebirr payments, wallet, notifications and
the contest leaderboard. Keeping it in one place means the bot and the app
can never disagree about who is entered or who is winning.
"""

import logging
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from config import (
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
PRIZE_SPLIT = (0.25, 0.15, 0.10)  # 1st, 2nd, 3rd place share of the pot
PRIZE_SHARE = sum(PRIZE_SPLIT)  # total paid out (50% of the pot)

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


def verify_et_lookup(tx_id):
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
    if not get_user(telegram_id):
        raise ContestError("not_registered", "Please link your FPL team first (on the Home screen).")
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
        if amount < ENTRY_FEE_BIRR:
            conn.execute("ROLLBACK")
            raise ContestError(
                "too_small",
                f"That payment is {amount:g} birr but the entry fee is {ENTRY_FEE_BIRR} birr. "
                "Please contact the admin.",
            )
        now = int(time.time())
        conn.execute(
            "UPDATE incoming_payments SET status='used', used_by=?, used_at=? WHERE tx_id=?",
            (telegram_id, now, tx_id),
        )
        conn.execute(
            "INSERT INTO entries (telegram_id, gameweek, paid, tx_reference) VALUES (?, ?, 1, ?) "
            "ON CONFLICT(telegram_id, gameweek) DO UPDATE SET paid=1, tx_reference=excluded.tx_reference",
            (telegram_id, gw, tx_id),
        )
        credit = int(amount - ENTRY_FEE_BIRR)  # anything extra they sent goes to their wallet
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
    player is 3rd."""
    out = {}
    i = 0
    while i < len(results):
        rank = results[i]["rank"]
        group = [r for r in results[i:] if r["rank"] == rank]
        share = sum(PRIZE_SPLIT[p - 1] for p in range(rank, rank + len(group)) if p <= len(PRIZE_SPLIT))
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
        "SELECT e.gameweek, e.paid, COALESCE(p.used_at, 0) FROM entries e "
        "LEFT JOIN incoming_payments p ON p.tx_id = e.tx_reference "
        "WHERE e.telegram_id=? AND e.paid=1",
        (telegram_id,), fetch=True,
    )
    prizes = db_execute("SELECT gameweek, amount, created_at FROM prizes WHERE telegram_id=?", (telegram_id,), fetch=True)
    credits = db_execute("SELECT amount, created_at FROM wallet_credits WHERE telegram_id=?", (telegram_id,), fetch=True)
    wds = db_execute(
        "SELECT id, amount, status, created_at FROM withdrawals WHERE telegram_id=?", (telegram_id,), fetch=True
    )
    items = (
        [{"type": "prize", "gameweek": gw, "amount": amt, "ts": ts} for gw, amt, ts in prizes]
        + [{"type": "credit", "amount": amt, "ts": ts} for amt, ts in credits]
        + [{"type": "entry", "gameweek": gw, "amount": ENTRY_FEE_BIRR, "ts": ts} for gw, _, ts in entries]
        + [{"type": "withdrawal", "id": i, "amount": amt, "status": st, "ts": ts} for i, amt, st, ts in wds]
    )
    items.sort(key=lambda x: (-(x["ts"] or 0), -(x.get("gameweek") or 0)))
    return {
        "balance": wallet_balance(telegram_id),
        "min_withdrawal": MIN_WITHDRAWAL,
        "has_pending_withdrawal": any(w[2] == "pending" for w in wds),
        "items": items,
    }


_PHONE = re.compile(r"^(?:\+?251|0)?(9|7)\d{8}$")


def request_withdrawal(telegram_id, amount, account):
    """Winner asks to withdraw from their wallet. Returns the new request or raises ContestError."""
    account = re.sub(r"[\s-]", "", account or "")
    if not _PHONE.match(account):
        raise ContestError("bad_account", "Please enter a valid Telebirr phone number, e.g. 0912345678.")
    if amount < MIN_WITHDRAWAL:
        raise ContestError("too_small", f"The minimum withdrawal is {MIN_WITHDRAWAL} birr.")
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
    me = get_user(w["telegram_id"])
    who = f"{me['username']} · FPL \"{me['team_name']}\"" if me else str(w["telegram_id"])
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
