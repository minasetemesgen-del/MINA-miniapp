/* ETFPL Mini App — front end (no build step, no dependencies). */
(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  const $ = (sel, root = document) => root.querySelector(sel);
  const view = $("#view");

  const state = {
    tab: "home",
    cache: {},
    fixturesGw: null,
    teamSeg: "squad",
    plSeg: "table",
    statKey: "goals",
    countdown: null,
    payPoll: null,
  };

  /* ------------------------------------------------------------------ */
  /* Images: real FPL pictures with graceful placeholders                */
  /* ------------------------------------------------------------------ */
  // If the Premier League changes its image addresses, edit these two lists.
  // Each list is tried in order; if none load, a placeholder picture is shown.
  const PHOTO_URLS = [
    (c) => `https://resources.premierleague.com/premierleague25/photos/players/110x140/${c}.png`,
    (c) => `https://resources.premierleague.com/premierleague/photos/players/110x140/p${c}.png`,
  ];
  const BADGE_URLS = [
    (c) => `https://resources.premierleague.com/premierleague/badges/70/t${c}.png`,
    (c) => `https://resources.premierleague.com/premierleague25/badges/70/t${c}.png`,
  ];
  const svgUri = (svg) => "data:image/svg+xml;utf8," + encodeURIComponent(svg);
  const PH_PLAYER = svgUri(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 110 140"><rect width="110" height="140" fill="#d9cfe3"/><circle cx="55" cy="52" r="24" fill="#8f7aa3"/><path d="M8 140c2-34 22-52 47-52s45 18 47 52z" fill="#8f7aa3"/></svg>`
  );
  const PH_BADGE = svgUri(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><path d="M32 4l24 8v18c0 16-10 26-24 30C18 56 8 46 8 30V12z" fill="#5a0a63"/><path d="M32 10l18 6v14c0 12-7 20-18 24-11-4-18-12-18-24V16z" fill="none" stroke="#00ff85" stroke-width="3"/></svg>`
  );

  const playerImg = (code) =>
    `<img src="${PHOTO_URLS[0](code)}" data-k="p" data-c="${code}" data-n="0" alt="" loading="lazy">`;
  const badgeImg = (code, cls = "") =>
    `<img class="badge ${cls}" src="${BADGE_URLS[0](code)}" data-k="b" data-c="${code}" data-n="0" alt="" loading="lazy">`;

  document.addEventListener(
    "error",
    (e) => {
      const t = e.target;
      if (!t || t.tagName !== "IMG" || !t.dataset.k) return;
      const kind = t.dataset.k;
      const list = kind === "p" ? PHOTO_URLS : BADGE_URLS;
      const next = Number(t.dataset.n) + 1;
      if (next < list.length) {
        t.dataset.n = next;
        t.src = list[next](t.dataset.c);
      } else {
        t.removeAttribute("data-k");
        t.src = kind === "p" ? PH_PLAYER : PH_BADGE;
      }
    },
    true
  );

  /* ------------------------------------------------------------------ */
  /* Small helpers                                                       */
  /* ------------------------------------------------------------------ */
  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const haptic = (kind = "light") => {
    try { tg.HapticFeedback.impactOccurred(kind); } catch (_) {}
  };
  const hapticNotify = (kind = "success") => {
    try { tg.HapticFeedback.notificationOccurred(kind); } catch (_) {}
  };

  let toastTimer;
  function toast(msg, isError = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast" + (isError ? " err" : "");
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.hidden = true), 3800);
  }

  function initials(name) {
    const clean = String(name || "?").replace(/[^\p{L}\p{N} ]/gu, "").trim();
    const parts = clean.split(/\s+/);
    return ((parts[0] || "?")[0] + (parts[1] ? parts[1][0] : (parts[0] || "")[1] || "")).toUpperCase();
  }
  function avatar(name, extraStyle = "") {
    let h = 0;
    for (const ch of String(name)) h = (h * 31 + ch.codePointAt(0)) % 360;
    return `<div class="avatar" style="--c1:hsl(${h} 70% 48%);--c2:hsl(${(h + 50) % 360} 75% 42%);${extraStyle}">${esc(initials(name))}</div>`;
  }

  const fmtCountdown = (ms) => {
    if (ms <= 0) return "Closed";
    const s = Math.floor(ms / 1000);
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const p = (n) => String(n).padStart(2, "0");
    return d > 0 ? `${d}d ${p(h)}h ${p(m)}m` : `${p(h)}:${p(m)}:${p(sec)}`;
  };
  const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) : "TBC");
  const fmtDayKey = (iso) => (iso ? new Date(iso).toLocaleDateString([], { weekday: "long", day: "numeric", month: "short" }) : "Date to be confirmed");
  const fmtDeadline = (iso) =>
    new Date(iso).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
  const fmtRank = (n) => (n == null ? "–" : n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e4 ? Math.round(n / 1e3) + "k" : n.toLocaleString());
  const ago = (ts) => {
    const s = Math.max(1, Math.floor(Date.now() / 1000 - ts));
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  };

  async function api(path, opts = {}) {
    const headers = { "Content-Type": "application/json", "X-Telegram-Init-Data": (tg && tg.initData) || "" };
    const dev = new URLSearchParams(location.search).get("dev");
    if (dev) headers["X-Dev-User"] = dev;
    const res = await fetch("/api" + path, { ...opts, headers });
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Something went wrong. Please try again.");
    return data;
  }

  const ICONS = {
    home: '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h5v-6h4v6h5V10"/>',
    trophy: '<path d="M8 21h8M12 17v4M7 4h10v5a5 5 0 0 1-10 0z"/><path d="M17 5h3v2a3 3 0 0 1-3 3M7 5H4v2a3 3 0 0 0 3 3"/>',
    table: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 10h18M9 4v16"/>',
    calendar: '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M8 3v4M16 3v4M3 11h18"/>',
    shirt: '<path d="M8 3l-5 3 2 5 3-1v11h8V10l3 1 2-5-5-3a4 4 0 0 1-8 0z"/>',
    wallet: '<path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/><path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/>',
  };
  function paintIcons() {
    document.querySelectorAll("i[data-icon]").forEach((el) => {
      el.outerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[el.dataset.icon]}</svg>`;
    });
  }

  function applyTheme() {
    const scheme = (tg && tg.colorScheme) || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
    document.documentElement.dataset.theme = scheme;
    const bg = scheme === "light" ? "#f5f1f8" : "#13001a";
    try { tg.setHeaderColor(bg); tg.setBackgroundColor(bg); tg.setBottomBarColor && tg.setBottomBarColor(bg); } catch (_) {}
  }

  const skeleton = (n = 4) => `<div>${'<div class="skeleton"></div>'.repeat(n)}</div>`;
  const emptyState = (emoji, title, text) =>
    `<div class="empty"><div class="big-emoji">${emoji}</div><b>${esc(title)}</b><p>${esc(text)}</p></div>`;
  const errorView = (msg) =>
    `<div class="card center stack"><div class="big-emoji" style="font-size:34px">⚠️</div><b>${esc(msg)}</b><button class="btn ghost small" data-act="retry">Try again</button></div>`;

  /* ------------------------------------------------------------------ */
  /* Tabs                                                                */
  /* ------------------------------------------------------------------ */
  const cacheKey = (tab) => {
    if (tab !== "pl") return tab;
    return state.plSeg === "fixtures" ? `fixtures:${state.fixturesGw || "now"}` : state.plSeg;
  };
  // Where a tab draws itself: the Premier League tab draws inside its own body, under its section switcher.
  const surface = (tab) => (tab === "pl" && $("#pl-body")) || view;
  const PL_SEGS = [["table", "Table"], ["fixtures", "Fixtures"], ["stats", "Stats"], ["news", "News"]];
  function ensurePLShell() {
    if (!$("#pl-seg")) {
      view.innerHTML = `<div class="fade-in"><div class="segment four" id="pl-seg">${PL_SEGS.map(([k, l]) => `<button data-act="plseg" data-seg="${k}">${l}</button>`).join("")}</div><div id="pl-body"></div></div>`;
    }
    document.querySelectorAll("#pl-seg button").forEach((b) => b.classList.toggle("active", b.dataset.seg === state.plSeg));
  }

  async function fetchTab(tab) {
    switch (tab) {
      case "home": return api("/home");
      case "board": return api("/leaderboard");
      case "wallet": return api("/wallet");
      case "pl":
        if (state.plSeg === "fixtures") return api("/fixtures" + (state.fixturesGw ? `?gw=${state.fixturesGw}` : ""));
        return api("/" + state.plSeg); // table, stats or news
      case "team": return api("/my-team");
    }
  }
  const painters = { home: paintHome, board: paintBoard, wallet: paintWallet, pl: paintPL, team: paintTeam };

  async function showTab(tab, { silent = false } = {}) {
    state.tab = tab;
    document.querySelectorAll("#tabbar button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    stopCountdown();
    if (tab === "pl") ensurePLShell();
    const key = cacheKey(tab);
    const cached = state.cache[key];
    if (cached) painters[tab](cached);
    else if (!silent) surface(tab).innerHTML = skeleton();
    try {
      const data = await fetchTab(tab);
      state.cache[key] = data;
      if (state.tab === tab && cacheKey(tab) === key) painters[tab](data);
    } catch (e) {
      if (!cached && state.tab === tab) surface(tab).innerHTML = errorView(e.message);
    }
  }

  /* ------------------------------------------------------------------ */
  /* Home                                                                */
  /* ------------------------------------------------------------------ */
  function paintHome(d) {
    const e = d.entry;
    let html = '<div class="stack fade-in">';

    if (!d.registered) {
      html += `
        <div class="card">
          <h3>Link your FPL team</h3>
          <p class="muted small" style="margin:6px 0 12px">Enter your FPL Team ID to join the weekly contest. Find it in the address bar when you open <b>Points</b> on the FPL website:
          <br><span style="word-break:break-all">fantasy.premierleague.com/entry/<b style="color:var(--accent)">1234567</b>/event/1</span></p>
          <input id="team-id" class="input" inputmode="numeric" pattern="[0-9]*" placeholder="Your Team ID, e.g. 1234567" autocomplete="off">
          <div style="height:10px"></div>
          <button class="btn" data-act="register">Link my team</button>
        </div>`;
    } else if (d.team && d.team.name) {
      html += `<div class="row"><div class="avatar">${esc(initials(d.team.name))}</div><div class="grow"><b class="ellipsis" style="display:block">${esc(d.team.name)}</b><span class="muted small">FPL team #${esc(d.team.id)}</span></div><span class="pill ok">Linked</span></div>`;
    }

    html += heroHtml(d);

    const l = d.live;
    if (l) {
      html += `<div class="card">
        <div class="row between"><b>Gameweek ${l.gameweek} · live standings</b>${d.is_admin ? `<span class="pill">${l.entrants} entered</span>` : ""}</div>
        ${l.entered
          ? `<div class="row between" style="margin-top:12px"><div><div class="muted small">Your rank</div><div style="font-size:28px;font-weight:900">#${l.rank}${d.is_admin ? `<span class="muted small" style="font-weight:600"> of ${l.entrants}</span>` : ""}</div></div>
             <div style="text-align:right"><div class="muted small">Points</div><div style="font-size:28px;font-weight:900;color:var(--accent)">${l.points}</div></div></div>
             <div style="height:12px"></div><button class="btn ghost small" data-act="goto" data-tab="board">See the Top 10</button>`
          : `<p class="muted small" style="margin:8px 0 12px">You're not entered in this gameweek. Follow along — and enter the next one!</p><button class="btn ghost small" data-act="goto" data-tab="board">See the Top 10</button>`}
      </div>`;
    }

    html += `<div class="card"><h3 style="margin-bottom:6px">How it works</h3><ol class="steps">
      <li><div><b>Link your FPL team</b><div class="muted small">One time only.</div></div></li>
      <li><div><b>Pay ${d.fee} birr</b><div class="muted small">Telebirr, CBE Birr or card — confirmed automatically.</div></div></li>
      <li><div><b>Top 3 win</b><div class="muted small">The three best gameweek scores win prizes.</div></div></li>
    </ol></div></div>`;

    view.innerHTML = html;
    $("#bell-dot").hidden = !d.unread;
    $("#bell-dot").textContent = d.unread > 9 ? "9+" : d.unread;
    if (e) startCountdown(e.deadline);
    if (e && e.pending && !state.payPoll) checkPay(true);
  }

  function heroHtml(d) {
    const e = d.entry;
    if (!e) {
      return `<div class="card hero"><div class="eyebrow">Season break</div><div class="big" style="font-size:26px">No gameweek open</div><div class="sub">New entries open when the next gameweek is announced.</div></div>`;
    }
    const closed = new Date(e.deadline).getTime() <= Date.now();
    let cta;
    if (e.paid) cta = `<div class="center"><span class="pill ok" style="font-size:14px;padding:9px 16px">✓ You're in for Gameweek ${e.gameweek}</span></div>`;
    else if (closed) cta = `<div class="center"><span class="pill warn">Entries closed</span></div>`;
    else if (!d.registered) cta = `<button class="btn" data-act="focus-reg">Link your team to enter</button>`;
    else if (e.pending)
      cta = `<button class="btn" data-act="pay">Complete payment · ${d.fee} birr</button>
             <button class="btn ghost small" style="margin-top:10px;width:100%;background:rgba(255,255,255,.12);color:#fff;border:0" data-act="pay-check">I've paid — check now</button>`;
    else cta = `<button class="btn" data-act="pay">Enter Gameweek ${e.gameweek} · ${d.fee} birr</button>`;

    return `<div class="card hero">
      <div class="eyebrow">Gameweek ${e.gameweek} · entries close in</div>
      <div class="big" data-countdown="${esc(e.deadline)}">${fmtCountdown(new Date(e.deadline) - Date.now())}</div>
      <div class="sub">Deadline ${esc(fmtDeadline(e.deadline))}</div>
      <div class="stats${d.is_admin ? "" : " single"}">
        <div class="stat"><b>${d.fee}</b><span>Entry (birr)</span></div>
        ${d.is_admin ? `<div class="stat"><b>${e.pot.toLocaleString()}</b><span>Pot (birr)</span></div>
        <div class="stat"><b>${e.prize.toLocaleString()}</b><span>Prizes (birr)</span></div>` : ""}
      </div>
      ${cta}
      ${d.is_admin ? `<div class="sub center" style="margin-top:10px">${e.entrants} player${e.entrants === 1 ? "" : "s"} entered so far</div>` : ""}
    </div>`;
  }

  function startCountdown(deadline) {
    stopCountdown();
    const target = new Date(deadline).getTime();
    state.countdown = setInterval(() => {
      const left = target - Date.now();
      document.querySelectorAll("[data-countdown]").forEach((el) => (el.textContent = fmtCountdown(left)));
      if (left <= 0) { stopCountdown(); delete state.cache.home; if (state.tab === "home") showTab("home", { silent: true }); }
    }, 1000);
  }
  function stopCountdown() { clearInterval(state.countdown); state.countdown = null; }

  async function registerTeam(btn) {
    const input = $("#team-id");
    const id = Number((input.value || "").trim());
    if (!Number.isInteger(id) || id <= 0) return toast("Please enter your numeric FPL Team ID.", true);
    busy(btn, true);
    try {
      const r = await api("/register", { method: "POST", body: JSON.stringify({ team_id: id }) });
      hapticNotify("success");
      toast(`Linked "${r.team.name}" ✅`);
      state.cache = {};
      showTab("home");
    } catch (e) { hapticNotify("error"); toast(e.message, true); }
    busy(btn, false);
  }

  function busy(btn, on) {
    if (!btn) return;
    if (on) { btn.dataset.label = btn.innerHTML; btn.innerHTML = '<span class="spinner"></span> Please wait'; btn.disabled = true; }
    else { btn.innerHTML = btn.dataset.label || btn.innerHTML; btn.disabled = false; }
  }

  /* ----- payment ----- */
  function openExternal(url) {
    if (tg && tg.openLink) tg.openLink(url);
    else window.open(url, "_blank");
  }
  async function startPay(btn) {
    busy(btn, true);
    try {
      const r = await api("/pay", { method: "POST" });
      haptic("medium");
      openExternal(r.checkout_url);
      toast("Finish the payment, then come back — it confirms automatically.");
      beginPayPolling();
    } catch (e) { hapticNotify("error"); toast(e.message, true); }
    busy(btn, false);
  }
  function beginPayPolling() {
    clearInterval(state.payPoll);
    let tries = 0;
    state.payPoll = setInterval(() => {
      if (++tries > 60) return stopPayPolling();
      checkPay(true);
    }, 4000);
  }
  function stopPayPolling() { clearInterval(state.payPoll); state.payPoll = null; }
  async function checkPay(quiet) {
    try {
      const r = await api("/pay/check", { method: "POST" });
      if (r.paid) {
        stopPayPolling();
        hapticNotify("success");
        toast("Payment confirmed — you're in! ✅");
        state.cache = {};
        if (state.tab === "home") showTab("home", { silent: true });
      } else if (!quiet) toast("Not confirmed yet — give it a few seconds after paying.");
    } catch (e) { if (!quiet) toast(e.message, true); }
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.payPoll) checkPay(true); });

  /* ------------------------------------------------------------------ */
  /* Leaderboard (Top 10)                                                */
  /* ------------------------------------------------------------------ */
  function lbRow(r) {
    return `<div class="lb-row ${r.is_me ? "me" : ""}">
      <div class="rank">${r.rank}</div>${avatar(r.name)}
      <div class="grow"><b class="ellipsis" style="display:block">${esc(r.name)}${r.is_me ? ' <span class="pill ok" style="margin-left:4px">You</span>' : ""}</b>
      <span class="muted small ellipsis" style="display:block">${esc(r.team_name || "")}${r.fpl_id ? ` · ID ${r.fpl_id}` : ""}</span></div>
      <div style="text-align:right"><div class="score">${r.points}<span class="muted small" style="font-weight:600"> pts</span></div>
      ${r.prize ? `<div class="small" style="font-weight:800;color:var(--accent)">${r.prize.toLocaleString()} birr</div>` : ""}</div></div>`;
  }
  function paintBoard(d) {
    if (!d.gameweek) { view.innerHTML = emptyState("🏆", "No gameweek yet", "The leaderboard appears once the season starts."); return; }
    let html = `<div class="stack fade-in">
      <div class="card"><div class="row between"><div><div class="muted small">Gameweek ${d.gameweek}</div><h2 style="font-size:22px">Top 10</h2></div>
        <div style="text-align:right"><div class="muted small" style="margin-bottom:2px">Prizes</div>
          ${["🥇", "🥈", "🥉"].map((m, i) => `<div style="font-weight:800;font-size:15px;line-height:1.45;color:var(--accent)">${m} ${(d.prizes[i] || 0).toLocaleString()} <span class="muted small" style="font-weight:600">birr</span></div>`).join("")}</div></div>
        ${d.is_admin ? `<div class="muted small" style="margin-top:6px">${d.entrants} player${d.entrants === 1 ? "" : "s"} · pot ${d.pot.toLocaleString()} birr</div>` : ""}</div>`;

    if (!d.top.length) {
      html += emptyState("⏳", "No entries yet", "Be the first to enter this gameweek.");
      view.innerHTML = html + "</div>";
      return;
    }

    let rest = d.top;
    if (d.top.length >= 3) {
      const [a, b, c] = d.top;
      const spot = (p, cls, color, h) =>
        `<div class="spot ${cls}" style="--ring:${color}">${avatar(p.name)}<div class="name ellipsis">${esc(p.name)}</div><div class="pts">${p.points} pts</div>${p.prize ? `<div class="pts" style="color:var(--accent);font-weight:800">${p.prize.toLocaleString()} birr</div>` : ""}${p.fpl_id ? `<div class="pts">ID ${p.fpl_id}</div>` : ""}<div class="bar" style="height:${h}px">${p.rank}</div></div>`;
      html += `<div class="card"><div class="podium">${spot(b, "second", "var(--silver)", 64)}${spot(a, "first", "var(--gold)", 92)}${spot(c, "third", "var(--bronze)", 46)}</div></div>`;
      rest = d.top.slice(3);
    }
    html += `<div>${rest.map(lbRow).join("")}</div>`;
    if (d.me) html += `<div class="muted center small">· · ·</div>${lbRow(d.me)}`;
    view.innerHTML = html + "</div>";
  }

  /* ------------------------------------------------------------------ */
  /* League table                                                        */
  /* ------------------------------------------------------------------ */
  function paintTable(d) {
    const zone = (pos, total) =>
      pos <= 4 ? "#04f5ff" : pos === 5 ? "#ff8a00" : pos > total - 3 ? "#ff4d6d" : "transparent";
    const rows = d.rows.map((r) => `
      <tr><td class="pos" style="--zone:${zone(r.pos, d.rows.length)}">${r.pos}</td>
      <td><div class="club">${badgeImg(r.code)}<div><div class="nm">${esc(r.name)}</div>
        <div class="form">${r.form.map((f) => `<i class="${f}"></i>`).join("")}</div></div></div></td>
      <td>${r.p}</td><td>${r.w}</td><td>${r.d}</td><td>${r.l}</td><td>${r.gd > 0 ? "+" : ""}${r.gd}</td><td class="pts">${r.pts}</td></tr>`).join("");
    $("#pl-body").innerHTML = `<div class="stack fade-in"><div class="card" style="padding:12px 10px">
      <h2 style="font-size:20px;margin:2px 6px 8px">League table</h2>
      <table class="tbl"><thead><tr><th></th><th>Club</th><th>P</th><th>W</th><th>D</th><th>L</th><th>GD</th><th>Pts</th></tr></thead><tbody>${rows}</tbody></table>
      <div class="legend"><span style="--c:#04f5ff">Champions League</span><span style="--c:#ff8a00">Europa League</span><span style="--c:#ff4d6d">Relegation</span></div>
    </div></div>`;
  }

  /* ------------------------------------------------------------------ */
  /* Fixtures                                                            */
  /* ------------------------------------------------------------------ */
  function matchHtml(m) {
    let mid;
    if (m.status === "scheduled") mid = `<div class="time">${fmtTime(m.kickoff)}</div>`;
    else mid = `<div class="score">${m.home.score ?? 0} – ${m.away.score ?? 0}</div><div class="state ${m.status === "live" ? "live" : ""}">${m.status === "live" ? `Live ${m.minutes}'` : "Full time"}</div>`;
    return `<div class="match">
      <div class="side">${badgeImg(m.home.code, "lg")}<span class="ellipsis">${esc(m.home.short)}</span></div>
      <div class="mid">${mid}</div>
      <div class="side away">${badgeImg(m.away.code, "lg")}<span class="ellipsis">${esc(m.away.short)}</span></div></div>`;
  }
  function paintFixtures(d) {
    const days = [];
    const map = new Map();
    d.matches.forEach((m) => {
      const k = fmtDayKey(m.kickoff);
      if (!map.has(k)) { map.set(k, []); days.push(k); }
      map.get(k).push(m);
    });
    $("#pl-body").innerHTML = `<div class="fade-in">
      <div class="gw-nav">
        <button class="round-btn" data-act="gw-prev" ${d.gameweek <= 1 ? "disabled" : ""} aria-label="Previous gameweek">‹</button>
        <div class="title"><b>Gameweek ${d.gameweek}</b>
          <span class="muted small">${d.gameweek === d.current ? "Current · " : ""}Deadline ${esc(fmtDeadline(d.deadline))}</span></div>
        <button class="round-btn" data-act="gw-next" ${d.gameweek >= d.total ? "disabled" : ""} aria-label="Next gameweek">›</button>
      </div>
      ${d.gameweek !== d.current ? `<div class="center"><button class="link-btn small" data-act="gw-now">Back to current gameweek</button></div>` : ""}
      ${days.length ? days.map((k) => `<div class="day-label">${esc(k)}</div>${map.get(k).map(matchHtml).join("")}`).join("") : emptyState("📅", "No fixtures", "Nothing scheduled for this gameweek yet.")}
    </div>`;
    state.fixturesCurrent = d.current;
    state.fixturesTotal = d.total;
  }
  function gotoGw(gw) {
    state.fixturesGw = gw;
    haptic();
    showTab("pl");
  }

  /* ------------------------------------------------------------------ */
  /* Premier League tab: Table · Fixtures · Stats · News                 */
  /* ------------------------------------------------------------------ */
  function paintPL(d) {
    ensurePLShell();
    ({ table: paintTable, fixtures: paintFixtures, stats: paintStats, news: paintNews })[state.plSeg](d);
  }

  function paintStats(d) {
    const cat = d.categories.find((c) => c.key === state.statKey) || d.categories[0];
    let rank = 0, prev = null;
    const rows = cat.items.map((p, i) => {
      if (p.value !== prev) { rank = i + 1; prev = p.value; }
      return `<div class="lb-row"><div class="rank">${rank}</div><div class="tr-ph">${playerImg(p.code)}</div>
        <div class="grow"><b class="ellipsis" style="display:block">${esc(p.name)}</b><span class="muted small">${esc(p.team)} · ${p.pos}</span></div>
        <div class="score">${p.value}</div></div>`;
    }).join("");
    $("#pl-body").innerHTML = `<div class="fade-in">
      <div class="chips">${d.categories.map((c) => `<button class="chip ${c.key === cat.key ? "active" : ""}" data-act="statkey" data-key="${c.key}">${esc(c.label)}</button>`).join("")}</div>
      ${rows}</div>`;
  }

  const AVAIL = { i: "Injured", s: "Suspended", u: "Unavailable", d: "Doubtful", n: "Not in squad" };
  function paintNews(d) {
    const heads = d.headlines.length
      ? d.headlines.map((h) => `<button class="news-item" data-act="open" data-url="${esc(h.url)}"><b>${esc(h.title)}</b>
          <span class="muted small">${esc(h.source)}${h.ts ? " · " + ago(h.ts) : ""}</span></button>`).join("")
      : `<div class="card center muted small">Headlines aren't available right now — try again in a few minutes.</div>`;
    const inj = d.injuries.length
      ? d.injuries.map((p) => `<div class="tr-card"><div class="tr-row"><div class="tr-ph">${playerImg(p.code)}</div>
          <div class="grow"><b class="ellipsis" style="display:block">${esc(p.name)}</b><span class="muted small">${esc(p.team)} · ${p.pos}</span>
          <div class="muted small" style="margin-top:2px">${esc(p.news)}</div></div>
          <span class="pill ${p.status === "d" ? "warn" : "live"}">${p.chance != null ? p.chance + "%" : AVAIL[p.status] || "Check"}</span></div></div>`).join("")
      : emptyState("✅", "No injury news", "Everyone looks fit right now.");
    $("#pl-body").innerHTML = `<div class="fade-in"><div class="section-title" style="margin-top:4px">Headlines</div>${heads}
      <div class="section-title">Injuries &amp; availability</div>${inj}</div>`;
  }

  /* ------------------------------------------------------------------ */
  /* Wallet                                                              */
  /* ------------------------------------------------------------------ */
  function paintWallet(d) {
    const sign = (n) => (n > 0 ? "+" : n < 0 ? "−" : "");
    const rows = d.items.map((t) => {
      const prize = t.type === "prize";
      const sub = prize ? `Winner of gameweek ${t.gameweek}` : t.status === "paid" ? "Paid" : "Waiting for payment";
      return `<div class="lb-row"><div class="rank" style="font-size:20px">${prize ? "🏆" : "🎟️"}</div>
        <div class="grow"><b>${prize ? "Prize" : "Entry fee"} · GW${t.gameweek}</b><span class="muted small" style="display:block">${sub}</span></div>
        <div class="score" style="${prize ? "color:var(--accent)" : ""}">${sign(t.amount)}${Math.abs(t.amount).toLocaleString()}<span class="muted small" style="font-weight:600"> birr</span></div></div>`;
    }).join("");
    view.innerHTML = `<div class="stack fade-in">
      <div class="hero"><div class="eyebrow">Total winnings</div>
        <div class="big">${d.won.toLocaleString()} <span style="font-size:18px;font-weight:700">birr</span></div>
        <div class="sub">${d.wins} gameweek win${d.wins === 1 ? "" : "s"} · ${d.entries} entr${d.entries === 1 ? "y" : "ies"} played</div>
        <div class="stats" style="grid-template-columns:1fr 1fr;margin-bottom:0">
          <div class="stat"><b>${d.spent.toLocaleString()}</b><span>Entry fees paid</span></div>
          <div class="stat"><b>${sign(d.net)}${Math.abs(d.net).toLocaleString()}</b><span>Net (birr)</span></div></div></div>
      <div class="section-title" style="margin:6px 4px 0">Activity</div>
      ${rows || emptyState("👛", "No activity yet", "Your entry fees and prizes will show up here.")}
      <p class="muted small center" style="margin-top:4px">Prizes appear here once the admin announces a gameweek winner.</p></div>`;
  }

  /* ------------------------------------------------------------------ */
  /* My team + transfers                                                 */
  /* ------------------------------------------------------------------ */
  function plTile(p, showPos = false) {
    const flag = p.status && p.status !== "a" ? `<div class="flag ${p.status === "d" ? "" : "r"}" title="${esc(p.news)}"></div>` : "";
    const cap = p.captain ? '<div class="cap">C</div>' : p.vice ? '<div class="cap">V</div>' : "";
    return `<div class="pl">${showPos ? `<div class="pos">${p.pos}</div>` : ""}${cap}${flag}
      <div class="ph">${playerImg(p.code)}</div><div class="nm">${esc(p.name)}</div><div class="pt">${p.shown_points}</div></div>`;
  }
  function paintTeam(d) {
    if (!d.registered) {
      view.innerHTML = `<div class="card center stack fade-in"><div class="big-emoji" style="font-size:40px">👕</div><b>Link your FPL team first</b><p class="muted small">Your squad and transfers show up here once your team is linked.</p><button class="btn" data-act="goto" data-tab="home">Go to Home</button></div>`;
      return;
    }
    if (!d.gameweek || d.empty) {
      view.innerHTML = emptyState("👕", "No squad yet", "Your picks appear here once the gameweek is underway.");
      return;
    }
    const s = d.summary;
    view.innerHTML = `<div class="fade-in">
      <div class="row between" style="margin:2px 2px 12px"><div><b style="font-size:18px">${esc(d.team.name || "My team")}</b><div class="muted small">Gameweek ${d.gameweek}${s.chip ? ` · chip: ${esc(s.chip)}` : ""}</div></div></div>
      <div class="strip">
        <div class="cell"><b style="color:var(--accent)">${s.points}</b><span>GW pts</span></div>
        <div class="cell"><b>${fmtRank(s.rank)}</b><span>GW rank</span></div>
        <div class="cell"><b>£${s.value.toFixed(1)}m</b><span>Value</span></div>
        <div class="cell"><b>£${s.bank.toFixed(1)}m</b><span>Bank</span></div>
      </div>
      <div class="muted small center" style="margin-top:10px">${s.transfers} transfer${s.transfers === 1 ? "" : "s"}${s.transfer_cost ? ` (−${s.transfer_cost} pts)` : ""} · ${s.bench_points} pts left on the bench</div>
      <div class="segment"><button data-act="seg" data-seg="squad" class="${state.teamSeg === "squad" ? "active" : ""}">Squad</button>
        <button data-act="seg" data-seg="transfers" class="${state.teamSeg === "transfers" ? "active" : ""}">Transfers</button></div>
      <div id="team-body"></div></div>`;
    paintTeamBody(d);
  }
  async function paintTeamBody(d) {
    const body = $("#team-body");
    if (!body) return;
    if (state.teamSeg === "squad") {
      const order = ["FWD", "MID", "DEF", "GKP"];
      body.innerHTML = `<div class="pitch">${order.map((pos) => `<div class="pitch-line">${(d.lines[pos] || []).map((p) => plTile(p)).join("")}</div>`).join("")}</div>
        <div class="section-title">Bench</div><div class="bench">${d.bench.map((p) => plTile(p, true)).join("")}</div>`;
      return;
    }
    const cached = state.cache.transfers;
    if (cached) paintTransfers(body, cached);
    else body.innerHTML = skeleton(3);
    try {
      const t = await api("/transfers");
      state.cache.transfers = t;
      if (state.tab === "team" && state.teamSeg === "transfers" && $("#team-body")) paintTransfers($("#team-body"), t);
    } catch (e) { if (!cached) body.innerHTML = errorView(e.message); }
  }
  function trendCard(p, dir) {
    return `<div class="trend"><div class="tr-ph">${playerImg(p.code)}</div><b class="ellipsis">${esc(p.name)}</b>
      <span class="muted small">${esc(p.team)} · £${p.price.toFixed(1)}m</span>
      <div class="count" style="color:${dir === "in" ? "var(--accent)" : "var(--danger)"}">${dir === "in" ? "▲" : "▼"} ${p.count.toLocaleString()}</div></div>`;
  }
  function trRow(p, dir) {
    return `<div class="tr-row"><div class="arrow ${dir}">${dir === "in" ? "▲" : "▼"}</div><div class="tr-ph">${playerImg(p.code)}</div>
      <div class="grow"><b class="ellipsis" style="display:block">${esc(p.name)}</b><span class="muted small">${esc(p.team)} · ${p.pos}</span></div>
      <div class="muted small">£${p.cost.toFixed(1)}m</div></div>`;
  }
  function paintTransfers(el, t) {
    const groups = new Map();
    t.mine.forEach((x) => { if (!groups.has(x.gameweek)) groups.set(x.gameweek, []); groups.get(x.gameweek).push(x); });
    let html = `<div class="section-title" style="margin-top:4px">Most transferred in this week</div><div class="hscroll">${t.trending.in.map((p) => trendCard(p, "in")).join("")}</div>
      <div class="section-title" style="margin-top:6px">Most transferred out</div><div class="hscroll">${t.trending.out.map((p) => trendCard(p, "out")).join("")}</div>
      <div class="section-title">Your transfers</div>`;
    if (!groups.size) html += emptyState("🔁", "No transfers yet", "Moves you make will be listed here.");
    groups.forEach((list, gw) => {
      html += `<div class="muted small" style="margin:12px 4px 6px;font-weight:800">GAMEWEEK ${gw}</div>` +
        list.map((x) => `<div class="tr-card">${trRow(x.out, "out")}${trRow(x.in, "in")}</div>`).join("");
    });
    el.innerHTML = html;
  }

  /* ------------------------------------------------------------------ */
  /* Notifications sheet                                                 */
  /* ------------------------------------------------------------------ */
  const KIND_ICON = { deadline: "⏰", payment: "✅", winner: "🏆" };
  async function openSheet() {
    haptic();
    $("#backdrop").hidden = false;
    $("#sheet").hidden = false;
    const body = $("#sheet-body");
    body.innerHTML = skeleton(3);
    try {
      const d = await api("/notifications");
      body.innerHTML = `
        <div class="card row" style="margin-bottom:12px"><div class="grow"><b>Deadline reminders</b><div class="muted small">A message before entries close, if you haven't entered.</div></div>
          <button class="switch ${d.settings.notify_deadline ? "on" : ""}" id="sw" role="switch" aria-checked="${d.settings.notify_deadline}" aria-label="Deadline reminders"></button></div>
        ${d.items.length ? d.items.map((n) => `<div class="notif ${n.read ? "" : "unread"}"><div class="ico">${KIND_ICON[n.kind] || "🔔"}</div>
          <div class="grow"><b>${esc(n.title)}</b><div class="muted small">${esc(n.body)}</div><div class="muted small" style="margin-top:4px;opacity:.8">${ago(n.created_at)}</div></div></div>`).join("")
          : emptyState("🔔", "All caught up", "Payment confirmations, deadline reminders and winners show up here.")}`;
      if (d.unread) { await api("/notifications/read", { method: "POST" }); $("#bell-dot").hidden = true; if (state.cache.home) state.cache.home.unread = 0; }
    } catch (e) { body.innerHTML = errorView(e.message); }
  }
  function closeSheet() { $("#backdrop").hidden = true; $("#sheet").hidden = true; }

  /* ------------------------------------------------------------------ */
  /* Events                                                              */
  /* ------------------------------------------------------------------ */
  view.addEventListener("click", (ev) => {
    const el = ev.target.closest("[data-act]");
    if (!el) return;
    switch (el.dataset.act) {
      case "register": return registerTeam(el);
      case "focus-reg": { const i = $("#team-id"); if (i) i.focus(); return; }
      case "pay": return startPay(el);
      case "pay-check": return checkPay(false);
      case "goto": return showTab(el.dataset.tab);
      case "retry": delete state.cache[cacheKey(state.tab)]; return showTab(state.tab);
      case "gw-prev": return gotoGw((state.fixturesGw || state.fixturesCurrent) - 1);
      case "gw-next": return gotoGw((state.fixturesGw || state.fixturesCurrent) + 1);
      case "gw-now": return gotoGw(null);
      case "seg": haptic(); state.teamSeg = el.dataset.seg; return paintTeam(state.cache.team);
      case "plseg": haptic(); state.plSeg = el.dataset.seg; return showTab("pl");
      case "statkey": haptic(); state.statKey = el.dataset.key; return paintStats(state.cache.stats);
      case "open": return openExternal(el.dataset.url);
    }
  });
  $("#sheet").addEventListener("click", async (ev) => {
    if (ev.target.id === "sw") {
      const on = !ev.target.classList.contains("on");
      ev.target.classList.toggle("on", on);
      try { await api("/settings", { method: "POST", body: JSON.stringify({ notify_deadline: on }) }); haptic(); }
      catch (e) { ev.target.classList.toggle("on", !on); toast(e.message, true); }
    } else if (ev.target.closest("[data-act='retry']")) openSheet();
  });
  $("#tabbar").addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-tab]");
    if (b) { haptic(); window.scrollTo({ top: 0 }); showTab(b.dataset.tab); }
  });
  $("#bell").addEventListener("click", openSheet);
  $("#backdrop").addEventListener("click", closeSheet);
  $("#sheet-close").addEventListener("click", closeSheet);

  /* ------------------------------------------------------------------ */
  /* Start                                                               */
  /* ------------------------------------------------------------------ */
  if (tg) { tg.ready(); tg.expand(); tg.onEvent("themeChanged", applyTheme); }
  applyTheme();
  paintIcons();
  showTab("home");
})();
