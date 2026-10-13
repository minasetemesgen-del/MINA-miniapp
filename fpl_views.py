"""
Turns raw FPL API data into the compact shapes the Mini App screens need:
league table, fixtures, my team (squad), and transfers.
"""

import email.utils
import logging
import threading
import time
import xml.etree.ElementTree as ET

import requests

import core
from core import bootstrap, fpl_get, fpl_index

POS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


def _team_ref(team):
    return {"id": team["id"], "name": team["name"], "short": team["short_name"], "code": team["code"]}


def _player(el, teams):
    team = teams[el["team"]]
    return {
        "id": el["id"],
        "name": el["web_name"],
        "pos": POS.get(el["element_type"], "?"),
        "team": team["short_name"],
        "team_code": team["code"],
        "code": el["code"],
        "points": el.get("event_points", 0),
        "price": el["now_cost"] / 10,
        "status": el.get("status", "a"),
        "news": el.get("news") or "",
    }


# ---------------------------------------------------------------------------
# League table — computed from finished fixtures
# ---------------------------------------------------------------------------
def build_table():
    b = bootstrap()
    stats = {
        t["id"]: {**_team_ref(t), "p": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "results": []}
        for t in b["teams"]
    }
    for f in fpl_get("fixtures/", 60):
        if not (f.get("finished") or f.get("finished_provisional")):
            continue
        hs, as_ = f.get("team_h_score"), f.get("team_a_score")
        if hs is None or as_ is None:
            continue
        when = f.get("kickoff_time") or ""
        for side, gf, ga in ((f["team_h"], hs, as_), (f["team_a"], as_, hs)):
            s = stats[side]
            s["p"] += 1
            s["gf"] += gf
            s["ga"] += ga
            if gf > ga:
                s["w"] += 1
                result = "W"
            elif gf == ga:
                s["d"] += 1
                result = "D"
            else:
                s["l"] += 1
                result = "L"
            s["results"].append((when, result))

    rows = []
    for s in stats.values():
        s["gd"] = s["gf"] - s["ga"]
        s["pts"] = s["w"] * 3 + s["d"]
        s["form"] = [r for _, r in sorted(s.pop("results"))[-5:]]
        rows.append(s)
    rows.sort(key=lambda r: (-r["pts"], -r["gd"], -r["gf"], r["name"]))
    for i, r in enumerate(rows):
        r["pos"] = i + 1
    return rows


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def build_fixtures(gameweek=None):
    b = bootstrap()
    events = b["events"]
    teams = {t["id"]: t for t in b["teams"]}
    current = core.get_live_gameweek() or 1
    gw = gameweek or current
    gw = max(1, min(gw, len(events)))
    event = next(e for e in events if e["id"] == gw)

    matches = []
    for f in fpl_get("fixtures/", 60):
        if f.get("event") != gw:
            continue
        finished = bool(f.get("finished") or f.get("finished_provisional"))
        status = "finished" if finished else "live" if f.get("started") else "scheduled"
        matches.append(
            {
                "id": f["id"],
                "kickoff": f.get("kickoff_time"),
                "status": status,
                "minutes": f.get("minutes", 0),
                "home": {**_team_ref(teams[f["team_h"]]), "score": f.get("team_h_score")},
                "away": {**_team_ref(teams[f["team_a"]]), "score": f.get("team_a_score")},
            }
        )
    matches.sort(key=lambda m: (m["kickoff"] or "9999", m["id"]))
    return {
        "gameweek": gw,
        "current": current,
        "total": len(events),
        "deadline": event["deadline_time"],
        "finished": event["finished"],
        "matches": matches,
    }


# ---------------------------------------------------------------------------
# My team (squad for the live gameweek)
# ---------------------------------------------------------------------------
def build_my_team(team_id):
    gw = core.get_live_gameweek()
    if gw is None:
        return {"gameweek": None}
    elements, teams = fpl_index()

    data = None
    for try_gw in (gw, gw - 1):
        if try_gw < 1:
            break
        try:
            data = fpl_get(f"entry/{team_id}/event/{try_gw}/picks/", 60)
            gw = try_gw
            break
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code != 404:
                raise
    if data is None:
        return {"gameweek": gw, "empty": True}

    lines = {"GKP": [], "DEF": [], "MID": [], "FWD": []}
    bench = []
    for pick in sorted(data["picks"], key=lambda p: p["position"]):
        el = elements.get(pick["element"])
        if not el:
            continue
        p = _player(el, teams)
        p["multiplier"] = pick["multiplier"]
        p["captain"] = pick["is_captain"]
        p["vice"] = pick["is_vice_captain"]
        p["shown_points"] = p["points"] * max(pick["multiplier"], 1) if pick["position"] <= 11 else p["points"]
        if pick["position"] <= 11:
            lines[p["pos"]].append(p)
        else:
            bench.append(p)

    eh = data.get("entry_history", {})
    return {
        "gameweek": gw,
        "summary": {
            "points": eh.get("points", 0),
            "total_points": eh.get("total_points", 0),
            "rank": eh.get("rank"),
            "value": eh.get("value", 0) / 10,
            "bank": eh.get("bank", 0) / 10,
            "transfers": eh.get("event_transfers", 0),
            "transfer_cost": eh.get("event_transfers_cost", 0),
            "bench_points": eh.get("points_on_bench", 0),
            "chip": data.get("active_chip"),
        },
        "lines": lines,
        "bench": bench,
    }


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------
def build_transfers(team_id):
    elements, teams = fpl_index()

    mine = []
    history = []
    if team_id:
        try:
            history = fpl_get(f"entry/{team_id}/transfers/", 120)
        except requests.HTTPError:
            history = []
    for t in reversed(history[-40:]):
        el_in, el_out = elements.get(t["element_in"]), elements.get(t["element_out"])
        if not el_in or not el_out:
            continue
        mine.append(
            {
                "gameweek": t["event"],
                "time": t.get("time"),
                "in": {**_player(el_in, teams), "cost": t.get("element_in_cost", 0) / 10},
                "out": {**_player(el_out, teams), "cost": t.get("element_out_cost", 0) / 10},
            }
        )

    def trend(field):
        top = sorted(elements.values(), key=lambda e: e.get(field, 0), reverse=True)[:6]
        return [{**_player(e, teams), "count": e.get(field, 0)} for e in top]

    return {
        "mine": mine,
        "trending": {"in": trend("transfers_in_event"), "out": trend("transfers_out_event")},
    }


# ---------------------------------------------------------------------------
# Statistics — season leaders from FPL's player data
# ---------------------------------------------------------------------------
STAT_DEFS = [
    ("goals", "Goals", "goals_scored"),
    ("assists", "Assists", "assists"),
    ("clean_sheets", "Clean sheets", "clean_sheets"),
    ("points", "FPL points", "total_points"),
    ("bonus", "Bonus", "bonus"),
]


def build_stats():
    elements, teams = fpl_index()
    cats = []
    for key, label, field in STAT_DEFS:
        top = sorted(elements.values(), key=lambda e: (-(e.get(field) or 0), e["web_name"]))[:10]
        cats.append(
            {
                "key": key,
                "label": label,
                "items": [{**_player(e, teams), "value": e.get(field) or 0} for e in top],
            }
        )
    return {"categories": cats}


# ---------------------------------------------------------------------------
# News — headlines from public RSS feeds + FPL's own injury/availability news
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)
NEWS_FEEDS = [
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/football/premier-league/rss.xml"),
    ("Sky Sports", "https://www.skysports.com/rss/11095"),
]
_news = {"t": 0.0, "items": []}
_news_lock = threading.Lock()


def _parse_feed(source, url):
    res = core.HTTP.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 ETFPL"})
    res.raise_for_status()
    out = []
    for item in ET.fromstring(res.content).iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link.startswith(("http://", "https://")):
            continue
        ts = 0
        try:
            ts = int(email.utils.parsedate_to_datetime(item.findtext("pubDate")).timestamp())
        except Exception:
            pass
        out.append({"title": title, "url": link, "source": source, "ts": ts})
    return out


def fetch_headlines(limit=15):
    with _news_lock:
        if time.time() - _news["t"] < 600 and _news["items"]:
            return _news["items"][:limit]
        items = []
        for source, url in NEWS_FEEDS:
            try:
                items += _parse_feed(source, url)
            except Exception as e:
                logger.warning("News feed failed (%s): %s", source, e)
        if items:
            seen, unique = set(), []
            for it in sorted(items, key=lambda x: -x["ts"]):
                if it["title"].lower() not in seen:
                    seen.add(it["title"].lower())
                    unique.append(it)
            _news["items"], _news["t"] = unique, time.time()
        else:
            _news["t"] = time.time() - 540  # nothing came back: retry in about a minute
        return _news["items"][:limit]


def build_news():
    elements, teams = fpl_index()
    flagged = [
        e for e in elements.values()
        if (e.get("news") or "").strip() and float(e.get("selected_by_percent") or 0) >= 1.0
    ]
    flagged.sort(key=lambda e: e.get("news_added") or "", reverse=True)
    injuries = [
        {**_player(e, teams), "chance": e.get("chance_of_playing_next_round")} for e in flagged[:25]
    ]
    return {"headlines": fetch_headlines(), "injuries": injuries}
