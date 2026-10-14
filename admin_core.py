"""
Admin dashboard logic (no web or Telegram code here — just database + rules).
Every action that changes something is written to the audit log, so you can
always see who did what and when.
"""

import csv
import io
import logging
import time

import core
from core import ContestError, db_execute

logger = logging.getLogger(__name__)


def _name(row_name, tid):
    return row_name or str(tid)


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------
def overview():
    fee = core.entry_fee()
    one = lambda q, p=(): (db_execute(q, p, fetchone=True) or [0])[0] or 0  # noqa: E731

    prizes_total = one("SELECT COALESCE(SUM(amount),0) FROM prizes")
    credits_total = one("SELECT COALESCE(SUM(amount),0) FROM wallet_credits")
    wd_out = one("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status IN ('pending','paid')")
    wd_paid = one("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='paid'")
    wd_pending_n = one("SELECT COUNT(*) FROM withdrawals WHERE status='pending'")
    wd_pending_sum = one("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='pending'")

    rows = db_execute(
        "SELECT gameweek, COUNT(*), COALESCE(SUM(COALESCE(fee, ?)),0) FROM entries WHERE paid=1 "
        "GROUP BY gameweek ORDER BY gameweek DESC LIMIT 6",
        (fee,), fetch=True,
    )
    recent = [{"gameweek": g, "entrants": n, "pot": int(p)} for g, n, p in rows]
    window = None
    try:
        w = core.get_entry_window()
        if w:
            window = {"gameweek": w["gameweek"], "entrants": core.count_paid_entries(w["gameweek"]),
                      "pot": core.gameweek_pot(w["gameweek"])}
    except Exception:
        logger.warning("overview: could not read the FPL deadline")

    day_ago = int(time.time()) - 86400
    h = core.verify_health
    return {
        "players": one("SELECT COUNT(*) FROM users"),
        "banned": one("SELECT COUNT(*) FROM users WHERE COALESCE(banned,0)=1"),
        "open_entries": window,
        "recent": recent,
        "collected_total": int(one("SELECT COALESCE(SUM(COALESCE(fee, ?)),0) FROM entries WHERE paid=1", (fee,))),
        "prizes_awarded": int(prizes_total),
        "withdrawals_paid": int(wd_paid),
        "withdrawals_pending": {"count": int(wd_pending_n), "amount": int(wd_pending_sum)},
        "owed_to_players": int(prizes_total + credits_total - wd_out),
        "payments_24h": one("SELECT COUNT(*) FROM incoming_payments WHERE received_at>=?", (day_ago,)),
        "payments_unused": one("SELECT COUNT(*) FROM incoming_payments WHERE status='unused'"),
        "verify": {
            "enabled": core.VERIFY_ET_ENABLED,
            "last_ok": h["last_ok"], "last_error": h["last_error"], "last_error_code": h["last_error_code"],
            "calls": h["calls"], "errors": h["errors"],
        },
        "maintenance": core.get_setting("maintenance"),
    }


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------
def list_players(query="", limit=40):
    q = (query or "").strip()
    sql = "SELECT telegram_id, username, fpl_team_id, team_name, COALESCE(banned,0) FROM users"
    params = []
    if q:
        sql += " WHERE username LIKE ? OR team_name LIKE ? OR CAST(telegram_id AS TEXT)=? OR CAST(fpl_team_id AS TEXT)=?"
        params = [f"%{q}%", f"%{q}%", q.lstrip("@"), q]
    sql += " ORDER BY rowid DESC LIMIT ?"
    params.append(max(1, min(limit, 100)))
    return [
        {"id": r[0], "name": _name(r[1], r[0]), "fpl_id": r[2], "team_name": r[3], "banned": bool(r[4]),
         "balance": core.wallet_balance(r[0])}
        for r in db_execute(sql, params, fetch=True)
    ]


def player_detail(tid):
    me = core.get_user(tid)
    if not me:
        raise ContestError("no_player", "Player not found.")
    entries = db_execute(
        "SELECT gameweek, paid, tx_reference, COALESCE(fee, ?) FROM entries WHERE telegram_id=? ORDER BY gameweek DESC LIMIT 12",
        (core.entry_fee(), tid), fetch=True,
    )
    w = core.wallet_data(tid)
    return {
        "id": tid, "name": _name(me["username"], tid), "fpl_id": me["team_id"], "team_name": me["team_name"],
        "banned": me["banned"],
        "entries": [{"gameweek": g, "paid": bool(p), "tx": tx, "fee": f} for g, p, tx, f in entries],
        "balance": w["balance"],
        "history": w["items"][:12],
    }


def set_banned(tid, banned, admin_id):
    if not core.get_user(tid):
        raise ContestError("no_player", "Player not found.")
    if core.is_admin_id(tid):
        raise ContestError("bad_action", "You can't block an admin.")
    db_execute("UPDATE users SET banned=? WHERE telegram_id=?", (1 if banned else 0, tid))
    core.audit(admin_id, "ban" if banned else "unban", f"player {tid}")


def set_fpl_id(tid, team_id, admin_id):
    me = core.get_user(tid)
    if not me:
        raise ContestError("no_player", "Player not found.")
    info = core.fetch_team_info(team_id)
    if info is None:
        raise ContestError("no_team", "FPL has no team with that ID.")
    db_execute("UPDATE users SET fpl_team_id=?, team_name=? WHERE telegram_id=?", (team_id, info["name"], tid))
    core.audit(admin_id, "fpl_id", f"player {tid}: {me['team_id']} → {team_id} ({info['name']})")
    return info


def add_entry(tid, gameweek, admin_id):
    """Enters a player by hand (e.g. they paid cash). Counts the current fee."""
    if not core.get_user(tid):
        raise ContestError("no_player", "Player not found.")
    db_execute(
        "INSERT INTO entries (telegram_id, gameweek, paid, tx_reference, fee) VALUES (?, ?, 1, 'admin', ?) "
        "ON CONFLICT(telegram_id, gameweek) DO UPDATE SET paid=1, tx_reference='admin', fee=excluded.fee",
        (tid, gameweek, core.entry_fee()),
    )
    core.audit(admin_id, "add_entry", f"player {tid} GW{gameweek}")
    core.add_notification(tid, "payment", "You're entered ✅", f"The admin entered you in Gameweek {gameweek}.")


def remove_entry(tid, gameweek, refund, admin_id):
    """Takes a player out of a gameweek. With refund=True the entry fee goes to their wallet."""
    row = db_execute(
        "SELECT paid, COALESCE(fee, ?) FROM entries WHERE telegram_id=? AND gameweek=?",
        (core.entry_fee(), tid, gameweek), fetchone=True,
    )
    if not row or not row[0]:
        raise ContestError("no_entry", "That player isn't entered in that gameweek.")
    db_execute("DELETE FROM entries WHERE telegram_id=? AND gameweek=?", (tid, gameweek))
    if refund:
        db_execute(
            "INSERT INTO wallet_credits (telegram_id, amount, reason, created_at) VALUES (?, ?, ?, ?)",
            (tid, row[1], f"Refund: GW{gameweek} entry removed by admin", int(time.time())),
        )
    core.audit(admin_id, "remove_entry", f"player {tid} GW{gameweek}" + (f", refunded {row[1]}" if refund else ""))
    core._lb_cache.pop(gameweek, None)


def adjust_wallet(tid, amount, reason, admin_id):
    """Adds (positive) or removes (negative) birr from a player's wallet. Needs a reason; kept in the log."""
    reason = (reason or "").strip()
    if not core.get_user(tid):
        raise ContestError("no_player", "Player not found.")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount == 0 or abs(amount) > 1_000_000:
        raise ContestError("bad_amount", "Enter a whole number of birr (not zero).")
    if not reason:
        raise ContestError("no_reason", "Please write a reason — it is kept in the log.")
    balance = core.wallet_balance(tid)
    if amount < 0 and balance + amount < 0:
        raise ContestError("insufficient", f"That would take the wallet below zero (it has {balance} birr).")
    db_execute(
        "INSERT INTO wallet_credits (telegram_id, amount, reason, created_at) VALUES (?, ?, ?, ?)",
        (tid, amount, f"Admin: {reason}"[:200], int(time.time())),
    )
    core.audit(admin_id, "wallet", f"player {tid}: {amount:+d} birr — {reason}")
    return core.wallet_balance(tid)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------
def list_payments(status="all", query="", limit=60):
    sql = (
        "SELECT p.tx_id, p.amount, p.sender_name, p.received_at, p.status, p.used_by, p.used_at, u.username "
        "FROM incoming_payments p LEFT JOIN users u ON u.telegram_id = p.used_by"
    )
    where, params = [], []
    if status in ("used", "unused"):
        where.append("p.status=?")
        params.append(status)
    q = (query or "").strip()
    if q:
        where.append("(p.tx_id LIKE ? OR p.sender_name LIKE ? OR u.username LIKE ?)")
        params += [f"%{q.upper()}%", f"%{q}%", f"%{q}%"]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.received_at DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    return [
        {"tx": r[0], "amount": r[1], "sender": r[2], "received_at": r[3], "status": r[4],
         "used_by": r[5], "used_by_name": r[7], "used_at": r[6]}
        for r in db_execute(sql, params, fetch=True)
    ]


def add_payment(tx_id, amount, admin_id):
    tx_id = (tx_id or "").strip().upper()
    if not core._TX_ONLY.match(tx_id):
        raise ContestError("bad_tx", "A transaction number is 8–14 letters/digits.")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not 0 < amount <= 1_000_000:
        raise ContestError("bad_amount", "Enter the amount in birr.")
    new = core.store_incoming(tx_id, float(amount), "added by admin", "manual")
    if not new:
        raise ContestError("exists", "That transaction number is already recorded.")
    core.audit(admin_id, "add_payment", f"{tx_id} {amount:g} birr")


def delete_payment(tx_id, admin_id):
    """Removes an UNUSED payment (a used one is part of someone's entry and can't be deleted)."""
    row = db_execute("SELECT status FROM incoming_payments WHERE tx_id=?", (tx_id,), fetchone=True)
    if not row:
        raise ContestError("no_payment", "Payment not found.")
    if row[0] != "unused":
        raise ContestError("used", "That payment was already used for an entry — remove the entry instead.")
    db_execute("DELETE FROM incoming_payments WHERE tx_id=? AND status='unused'", (tx_id,))
    core.audit(admin_id, "delete_payment", tx_id)


# ---------------------------------------------------------------------------
# Withdrawals
# ---------------------------------------------------------------------------
def list_withdrawals(status="pending", limit=60):
    sql = (
        "SELECT w.id, w.telegram_id, w.amount, w.account, w.status, w.created_at, w.resolved_at, u.username, u.team_name "
        "FROM withdrawals w LEFT JOIN users u ON u.telegram_id = w.telegram_id"
    )
    params = []
    if status in ("pending", "paid", "rejected"):
        sql += " WHERE w.status=?"
        params.append(status)
    sql += " ORDER BY w.id DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    return [
        {"id": r[0], "player_id": r[1], "amount": r[2], "account": r[3], "status": r[4], "created_at": r[5],
         "resolved_at": r[6], "name": _name(r[7], r[1]), "team_name": r[8]}
        for r in db_execute(sql, params, fetch=True)
    ]


# ---------------------------------------------------------------------------
# Gameweek control
# ---------------------------------------------------------------------------
def gameweek_info(gameweek):
    entrants = core.count_paid_entries(gameweek)
    announced = db_execute("SELECT COUNT(*) FROM prizes WHERE gameweek=?", (gameweek,), fetchone=True)[0]
    return {"gameweek": gameweek, "entrants": entrants, "pot": core.gameweek_pot(gameweek), "announced": announced > 0}


def announcement_preview(gameweek):
    """Who would win what right now — nothing is saved or sent."""
    results = core.contest_leaderboard(gameweek, 0)  # fresh scores
    pot = core.gameweek_pot(gameweek)
    prizes = core.prize_allocation(results, pot)
    winners = [
        {"rank": r["rank"], "telegram_id": r["telegram_id"], "name": r["username"], "team_id": r["team_id"],
         "points": r["points"], "prize": prizes[r["telegram_id"]]}
        for r in results if r["telegram_id"] in prizes
    ]
    return {"gameweek": gameweek, "entrants": len(results), "pot": pot, "results": results,
            "prizes": prizes, "winners": winners, "places": len(core.prize_split())}


# ---------------------------------------------------------------------------
# Broadcast audiences
# ---------------------------------------------------------------------------
AUDIENCES = {
    "all": "Everyone who linked a team",
    "not_entered": "Linked but NOT entered in the open gameweek",
    "entered": "Entered in the open gameweek",
}


def audience_ids(audience, gameweek=None):
    base = "SELECT telegram_id FROM users WHERE COALESCE(banned,0)=0"
    if audience == "all":
        rows = db_execute(base, fetch=True)
    elif audience in ("entered", "not_entered"):
        if gameweek is None:
            w = core.get_entry_window()
            gameweek = w["gameweek"] if w else None
        if gameweek is None:
            raise ContestError("closed", "No gameweek is open right now.")
        op = "IN" if audience == "entered" else "NOT IN"
        rows = db_execute(
            f"{base} AND telegram_id {op} (SELECT telegram_id FROM entries WHERE gameweek=? AND paid=1)",
            (gameweek,), fetch=True,
        )
    else:
        raise ContestError("bad_audience", "Unknown audience.")
    return [r[0] for r in rows]


def create_broadcast(admin_id, text, media_token, audience, total):
    cur = core._connect()
    try:
        c = cur.execute(
            "INSERT INTO broadcasts (admin_id, text, media_token, audience, total, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (admin_id, text, media_token or "", audience, total, int(time.time())),
        )
        cur.commit()
        return c.lastrowid
    finally:
        cur.close()


def update_broadcast(bid, sent, failed, status=None):
    if status:
        db_execute("UPDATE broadcasts SET sent=?, failed=?, status=? WHERE id=?", (sent, failed, status, bid))
    else:
        db_execute("UPDATE broadcasts SET sent=?, failed=? WHERE id=?", (sent, failed, bid))


def get_broadcast(bid):
    r = db_execute(
        "SELECT id, text, media_token, audience, total, sent, failed, status, created_at FROM broadcasts WHERE id=?",
        (bid,), fetchone=True,
    )
    if not r:
        return None
    return dict(zip(("id", "text", "media_token", "audience", "total", "sent", "failed", "status", "created_at"), r))


def list_broadcasts(limit=10):
    rows = db_execute("SELECT id FROM broadcasts ORDER BY id DESC LIMIT ?", (limit,), fetch=True)
    return [get_broadcast(r[0]) for r in rows]


# ---------------------------------------------------------------------------
# Messages (templates) for the editor
# ---------------------------------------------------------------------------
def list_templates():
    out = []
    for key, d in core.TEMPLATES.items():
        t = core.get_template(key)
        out.append({
            "key": key, "label": d["label"], "help": d["help"], "vars": d["vars"],
            "text": t["text"], "default": d["default"], "custom": t["custom"],
            "media_token": t["media_token"], "media_url": core.media_url(t["media_token"]),
        })
    return out


# ---------------------------------------------------------------------------
# Log and exports
# ---------------------------------------------------------------------------
def audit_list(limit=60):
    rows = db_execute(
        "SELECT id, admin_id, action, detail, created_at FROM audit_log ORDER BY id DESC LIMIT ?",
        (max(1, min(limit, 300)),), fetch=True,
    )
    return [{"id": r[0], "admin_id": r[1], "action": r[2], "detail": r[3], "at": r[4]} for r in rows]


EXPORTS = {
    "players": (
        "SELECT telegram_id, username, fpl_team_id, team_name, COALESCE(banned,0) FROM users ORDER BY rowid",
        ["telegram_id", "username", "fpl_team_id", "team_name", "banned"],
    ),
    "entries": (
        "SELECT e.gameweek, e.telegram_id, u.username, u.fpl_team_id, e.paid, e.tx_reference, e.fee "
        "FROM entries e LEFT JOIN users u ON u.telegram_id=e.telegram_id ORDER BY e.gameweek, e.telegram_id",
        ["gameweek", "telegram_id", "username", "fpl_team_id", "paid", "tx_reference", "fee"],
    ),
    "payments": (
        "SELECT tx_id, amount, sender_name, received_at, status, used_by, used_at FROM incoming_payments ORDER BY received_at",
        ["tx_id", "amount", "sender", "received_at", "status", "used_by", "used_at"],
    ),
    "withdrawals": (
        "SELECT id, telegram_id, amount, account, status, created_at, resolved_at FROM withdrawals ORDER BY id",
        ["id", "telegram_id", "amount", "telebirr_account", "status", "created_at", "resolved_at"],
    ),
    "prizes": (
        "SELECT p.gameweek, p.telegram_id, u.username, u.fpl_team_id, p.amount, p.created_at "
        "FROM prizes p LEFT JOIN users u ON u.telegram_id=p.telegram_id ORDER BY p.gameweek, p.amount DESC",
        ["gameweek", "telegram_id", "username", "fpl_team_id", "amount", "created_at"],
    ),
}


def export_csv(kind):
    if kind not in EXPORTS:
        raise ContestError("bad_export", "Unknown export.")
    sql, header = EXPORTS[kind]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    safe = lambda v: "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v  # noqa: E731  (no spreadsheet formulas)
    for row in db_execute(sql, fetch=True):
        w.writerow([safe(v) for v in row])
    return f"etfpl_{kind}.csv", ("﻿" + buf.getvalue()).encode("utf-8")  # BOM so Excel reads it right
