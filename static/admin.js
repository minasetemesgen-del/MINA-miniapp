/* ETFPL Mini App — Admin dashboard. Only ever opened by admins (the server checks every call). */
(() => {
  "use strict";
  const { api, esc, toast, haptic, hapticNotify, $, view, fmt2, ago, skeleton, errorView, emptyState, busy, avatar, copyText } = window.ETFPL;
  const tg = window.Telegram && window.Telegram.WebApp;

  const A = { sec: null, arg: null, pollTimer: null, pmFilter: "all", wdFilter: "pending", gw: null, ov: null };
  const SECTIONS = [
    ["withdrawals", "💸", "Withdrawals", "Pay or reject requests"],
    ["settings", "⚙️", "Settings", "Prize split, fee, reminders, pause"],
    ["messages", "✉️", "Messages", "Edit what the bot says, add a picture"],
    ["broadcast", "📣", "Announcements", "Message your players"],
    ["gameweek", "🏁", "Gameweek & winners", "Preview and announce winners"],
    ["players", "👥", "Players", "Search, block, fix, wallet"],
    ["payments", "🧾", "Payments", "Every verified payment"],
    ["log", "📜", "Activity log & export", "Who changed what, download data"],
  ];

  /* ----------------------------------------------------------------- helpers */
  const num = (v) => (v == null ? "–" : Number(v).toLocaleString());
  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }) : "–");
  const val = (id) => ($("#" + id) ? $("#" + id).value : "");
  const ask = (msg) =>
    new Promise((resolve) => {
      if (tg && tg.showConfirm) { try { return tg.showConfirm(msg, (ok) => resolve(!!ok)); } catch (_) {} }
      resolve(window.confirm(msg));
    });
  const sw = (id, on) => `<button class="switch ${on ? "on" : ""}" id="${id}" data-a="toggle" role="switch" aria-checked="${!!on}"></button>`;
  const stillHere = (sec) => window.ETFPL.state.tab === "admin" && A.sec === sec;

  function shell(title, body, back = "‹ Admin") {
    view.innerHTML = `<div class="stack fade-in adm">
      <div class="row between"><button class="link-btn" data-a="back">${back}</button><b style="font-size:18px">${title}</b><span style="width:60px"></span></div>
      ${body}</div>`;
  }
  const loading = (title) => shell(title, skeleton(3));
  const fail = (title, e) => shell(title, errorView(e.message).replace('data-act="retry"', 'data-a="reload"'));

  async function guard(btn, fn) {
    busy(btn, true);
    try { await fn(); hapticNotify("success"); }
    catch (e) { hapticNotify("error"); toast(e.message, true); }
    busy(btn, false);
  }

  const post = (path, body) => api("/admin" + path, { method: "POST", body: JSON.stringify(body || {}) });
  const get = (path) => api("/admin" + path);

  /* ------------------------------------------------------------------ pictures */
  function pickImage() {
    return new Promise((resolve, reject) => {
      const input = document.createElement("input");
      input.type = "file";
      input.accept = "image/png,image/jpeg,image/webp";
      input.onchange = () => {
        const file = input.files && input.files[0];
        if (!file) return resolve(null);
        const reader = new FileReader();
        reader.onerror = () => reject(new Error("Couldn't read that picture."));
        reader.onload = () => {
          const img = new Image();
          img.onerror = () => reject(new Error("That file isn't a picture."));
          img.onload = () => {
            const max = 1280, k = Math.min(1, max / Math.max(img.width, img.height));
            const c = document.createElement("canvas");
            c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
            c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
            // keep PNG (transparent prize pictures) as PNG, everything else as a small JPEG
            resolve(file.type === "image/png" ? c.toDataURL("image/png") : c.toDataURL("image/jpeg", 0.88));
          };
          img.src = reader.result;
        };
        reader.readAsDataURL(file);
      };
      input.click();
    });
  }
  async function uploadPicture() {
    const dataUrl = await pickImage();
    if (!dataUrl) return null;
    return post("/media", { data_url: dataUrl });
  }
  const pic = (url, id = "") => (url ? `<img class="adm-pic" ${id ? `id="${id}"` : ""} src="${esc(url)}" alt="">` : "");

  /* ------------------------------------------------------------------ router */
  function open() { A.sec = null; renderHome(); }
  async function go(sec, arg) {
    clearInterval(A.pollTimer);
    A.sec = sec; A.arg = arg || null;
    window.scrollTo({ top: 0 });
    try { await SEC[sec](arg); } catch (e) { if (stillHere(sec)) fail(titleOf(sec), e); }
  }
  const titleOf = (sec) => (SECTIONS.find((s) => s[0] === sec) || [0, 0, "Admin"])[2];

  /* ------------------------------------------------------------------ home */
  async function renderHome() {
    A.sec = null;
    clearInterval(A.pollTimer);
    view.innerHTML = skeleton(4);
    let ov;
    try { ov = await get("/overview"); } catch (e) { view.innerHTML = errorView(e.message).replace('data-act="retry"', 'data-a="reload"'); return; }
    if (A.sec !== null) return;
    A.ov = ov;
    const v = ov.verify, wd = ov.withdrawals_pending, oe = ov.open_entries;
    const vState = !v.enabled ? ["warn", "Off"] : v.last_error && (!v.last_ok || v.last_error >= v.last_ok) ? ["live", `Problem: ${esc(v.last_error_code)}`] : v.last_ok ? ["ok", "Working"] : ["", "No checks yet"];
    view.innerHTML = `<div class="stack fade-in adm">
      <div><h2 style="font-size:26px">Admin</h2><div class="muted small">Only you (and co-admins) can see this.</div></div>
      ${ov.maintenance ? `<div class="card notice"><div class="row between"><div><b>⏸️ Entries are paused</b><div class="muted small">Players can't enter right now.</div></div><button class="btn small" data-a="resume">Resume</button></div></div>` : ""}
      <div class="adm-grid">
        <div class="adm-stat"><span>Players</span><b>${num(ov.players)}</b>${ov.banned ? `<i>${ov.banned} blocked</i>` : ""}</div>
        <div class="adm-stat"><span>${oe ? `GW${oe.gameweek} entered` : "Open gameweek"}</span><b>${oe ? num(oe.entrants) : "–"}</b>${oe ? `<i>pot ${num(oe.pot)} birr</i>` : ""}</div>
        <div class="adm-stat"><span>Owed to players</span><b>${num(ov.owed_to_players)}</b><i>birr in wallets</i></div>
        <div class="adm-stat ${wd.count ? "hot" : ""}" data-a="go" data-sec="withdrawals"><span>Withdrawals waiting</span><b>${wd.count}</b><i>${wd.count ? num(wd.amount) + " birr" : "none"}</i></div>
        <div class="adm-stat"><span>Collected so far</span><b>${num(ov.collected_total)}</b><i>birr</i></div>
        <div class="adm-stat"><span>Prizes awarded</span><b>${num(ov.prizes_awarded)}</b><i>${num(ov.withdrawals_paid)} paid out</i></div>
      </div>
      <div class="card row between"><div><b>Payment checks (verify.et)</b><div class="muted small">${v.calls} check${v.calls === 1 ? "" : "s"} since last restart${v.errors ? ` · ${v.errors} failed` : ""} · ${num(ov.payments_24h)} payments in 24h · ${num(ov.payments_unused)} unused</div></div><span class="pill ${vState[0]}">${vState[1]}</span></div>
      ${ov.recent.length ? `<div class="card"><b>Recent gameweeks</b>${ov.recent.map((r) => `<div class="row between adm-line"><span>GW${r.gameweek}</span><span class="muted">${r.entrants} players</span><b>${num(r.pot)} birr</b></div>`).join("")}</div>` : ""}
      <div>${SECTIONS.map(([k, ico, t, sub]) => `<div class="lb-row adm-menu" data-a="go" data-sec="${k}"><div class="rank" style="font-size:22px">${ico}</div><div class="grow"><b>${t}</b><span class="muted small" style="display:block">${sub}</span></div>${k === "withdrawals" && wd.count ? `<span class="dot2">${wd.count}</span>` : ""}<span class="muted">›</span></div>`).join("")}</div>
    </div>`;
  }

  /* ------------------------------------------------------------------ withdrawals */
  async function secWithdrawals() {
    loading("Withdrawals");
    const d = await get(`/withdrawals?status=${A.wdFilter}`);
    if (!stillHere("withdrawals")) return;
    const seg = [["pending", "Waiting"], ["paid", "Paid"], ["rejected", "Rejected"], ["all", "All"]]
      .map(([k, l]) => `<button class="${A.wdFilter === k ? "active" : ""}" data-a="wdfilter" data-k="${k}">${l}</button>`).join("");
    const cards = d.items.map((w) => `<div class="card adm-card">
      <div class="row between"><b>${esc(w.name)}</b><b style="color:var(--accent);font-size:19px">${num(w.amount)} birr</b></div>
      <div class="muted small">${esc(w.team_name || "")} · #${w.id} · ${ago(w.created_at)}</div>
      <div class="kv" style="margin-top:10px"><div><span>Send to Telebirr</span><b>${esc(w.account)}</b><button class="copy" data-a="copy" data-v="${esc(w.account)}">Copy</button></div></div>
      ${w.status === "pending"
        ? `<div class="row" style="margin-top:12px;gap:8px"><button class="btn small" style="flex:1" data-a="wd" data-id="${w.id}" data-do="paid" data-amt="${w.amount}" data-acc="${esc(w.account)}">✅ Mark paid</button>
           <button class="btn ghost small" style="flex:1" data-a="wd" data-id="${w.id}" data-do="rejected" data-amt="${w.amount}" data-acc="">❌ Reject</button></div>`
        : `<div style="margin-top:8px"><span class="pill ${w.status === "paid" ? "ok" : "warn"}">${w.status === "paid" ? "Paid" : "Rejected"}</span> <span class="muted small">${ago(w.resolved_at || w.created_at)}</span></div>`}
    </div>`).join("");
    shell("Withdrawals", `<div class="segment four">${seg}</div>
      <div class="muted small">Send the money yourself on Telebirr first, then tap <b>Mark paid</b>. <b>Reject</b> returns it to the player's wallet.</div>
      ${cards || emptyState("💸", "Nothing here", A.wdFilter === "pending" ? "No withdrawal requests are waiting." : "No requests in this list.")}`);
  }

  /* ------------------------------------------------------------------ settings */
  async function secSettings() {
    loading("Settings");
    const d = await get("/settings");
    if (!stillHere("settings")) return;
    const s = d.settings;
    A.prizeMedia = s.prize_media; A.prizeMediaUrl = d.prize_media_url;
    const pot = (A.ov && A.ov.open_entries && A.ov.open_entries.pot) || s.entry_fee * 10;
    shell("Settings", `
      <div class="card stack">
        <div><b>Prize split</b><div class="muted small">Percent of the pot for 1st, 2nd, 3rd… Add or remove numbers to change how many win (up to 10).</div></div>
        <input id="st-prizes" class="input" value="${esc(s.prize_percents.join(", "))}" placeholder="25, 15, 10" inputmode="decimal" data-a-input="prizes">
        <div id="st-prizes-hint" class="small muted"></div>
        <button class="btn small" data-a="save" data-what="prizes">Save prize split</button>
      </div>
      <div class="card stack">
        <div><b>Money</b></div>
        <div><div class="field-label">Entry fee (birr)</div><input id="st-fee" class="input" inputmode="numeric" value="${s.entry_fee}"></div>
        <div><div class="field-label">Minimum withdrawal (birr)</div><input id="st-minwd" class="input" inputmode="numeric" value="${s.min_withdrawal}"></div>
        <div class="muted small">A new fee applies to new entries. Gameweeks already paid keep the fee they were paid at.</div>
        <button class="btn small" data-a="save" data-what="money">Save</button>
      </div>
      <div class="card stack">
        <div><b>Reminder times</b><div class="muted small">Hours before the FPL deadline when players who haven't entered get a reminder. Up to 3, e.g. <b>24, 3</b>. Leave empty for none.</div></div>
        <input id="st-rem" class="input" value="${esc(s.reminder_hours.join(", "))}" placeholder="24, 3" inputmode="numeric">
        <button class="btn small" data-a="save" data-what="reminders">Save</button>
      </div>
      <div class="card stack">
        <div class="row between"><div><b>Pause entries</b><div class="muted small">Stops new entries and reminders. Players see your note.</div></div>${sw("st-maint", s.maintenance)}</div>
        <input id="st-maint-text" class="input" maxlength="300" value="${esc(s.maintenance_text)}">
        <button class="btn ghost small" data-a="save" data-what="maint-text">Save note</button>
      </div>
      <div class="card stack">
        <div class="row between"><div><b>Tell everyone the winners</b><div class="muted small">When you announce winners, also message every player who entered (the winners always get their own message).</div></div>${sw("st-all", s.announce_winners_to_all)}</div>
      </div>
      <div class="card stack">
        <div><b>Prize players see on Home</b><div class="muted small">Text and a picture, e.g. “PS5 Slim”. Change it any time.</div></div>
        <input id="st-ptext" class="input" maxlength="200" value="${esc(s.prize_text)}" placeholder="e.g. PS5 Slim">
        <div id="st-ppic">${pic(d.prize_media_url)}</div>
        <div class="row" style="gap:8px"><button class="btn ghost small" data-a="prizepic">${d.prize_media_url ? "Change picture" : "Add picture"}</button>${d.prize_media_url ? `<button class="btn ghost small" data-a="prizepic-rm">Remove picture</button>` : ""}</div>
        <button class="btn small" data-a="save" data-what="prize">Save prize</button>
      </div>
      <div class="card stack">
        <div><b>Player limit</b><div class="muted small">Stop new players linking a team after this many. 0 = no limit.</div></div>
        <input id="st-cap" class="input" inputmode="numeric" value="${s.registration_cap}">
        <button class="btn small" data-a="save" data-what="cap">Save</button>
      </div>
      <div class="card stack">
        <div><b>Co-admins</b><div class="muted small">Telegram IDs of people who can also use this dashboard (they can pay withdrawals and announce winners). ${d.is_owner ? "Only you, the owner, can change this." : "Only the owner can change this."}</div></div>
        <input id="st-admins" class="input" value="${esc(s.extra_admins.join(", "))}" placeholder="e.g. 123456789" inputmode="numeric" ${d.is_owner ? "" : "disabled"}>
        ${d.is_owner ? `<button class="btn small" data-a="save" data-what="admins">Save co-admins</button>` : ""}
        <div class="muted small">Your ID: owner ${d.owner_id}</div>
      </div>`);
    prizeHint();
  }
  function prizeHint() {
    const el = $("#st-prizes-hint");
    if (!el) return;
    const parts = val("st-prizes").split(/[,\s/]+/).filter(Boolean).map(Number);
    const bad = !parts.length || parts.some((n) => !(n > 0 && n <= 100));
    const sum = parts.reduce((a, b) => a + b, 0);
    const pot = (A.ov && A.ov.open_entries && A.ov.open_entries.pot) || 1000;
    if (bad) { el.textContent = "Enter numbers like 25, 15, 10."; el.style.color = "var(--danger)"; return; }
    el.style.color = sum > 100 ? "var(--danger)" : "";
    el.innerHTML = `${parts.length} winner${parts.length === 1 ? "" : "s"} · ${sum}% of the pot paid out${sum > 100 ? " — <b>more than 100%!</b>" : ""}<br>On a ${num(pot)} birr pot: ` +
      parts.map((p, i) => `${["🥇", "🥈", "🥉"][i] || i + 1 + "."} ${num(Math.round((pot * p) / 100))}`).join(" · ");
  }
  const SAVE = {
    prizes: () => ({ prize_percents: val("st-prizes") }),
    money: () => ({ entry_fee: Number(val("st-fee")), min_withdrawal: Number(val("st-minwd")) }),
    reminders: () => ({ reminder_hours: val("st-rem") }),
    "maint-text": () => ({ maintenance_text: val("st-maint-text") }),
    prize: () => ({ prize_text: val("st-ptext"), prize_media: A.prizeMedia || "" }),
    cap: () => ({ registration_cap: Number(val("st-cap")) }),
    admins: () => ({ extra_admins: val("st-admins") }),
  };

  /* ------------------------------------------------------------------ messages */
  async function loadTemplates() {
    const d = await get("/templates");
    A.tpl = Object.fromEntries(d.items.map((t) => [t.key, t]));
    A.tplOrder = d.items.map((t) => t.key);
    A.captionLimit = d.caption_limit;
  }
  async function secMessages() {
    loading("Messages");
    await loadTemplates();
    if (!stillHere("messages")) return;
    const items = A.tplOrder.map((k) => A.tpl[k]);
    shell("Messages", `<div class="muted small">Everything the bot says by itself. Tap one to edit its words and add a picture — with a picture, the text is sent as the picture's caption.</div>
      <div>${items.map((t) => `<div class="lb-row adm-menu" data-a="tpl" data-key="${t.key}"><div class="grow"><b>${esc(t.label)}</b><span class="muted small ellipsis" style="display:block">${esc(t.text.replace(/\n/g, " "))}</span></div>
        ${t.media_url ? "🖼️" : ""}${t.custom ? `<span class="pill ok">Edited</span>` : ""}<span class="muted">›</span></div>`).join("")}</div>`);
  }
  const fill = (text, sample) => String(text).replace(/\{(\w+)\}/g, (m, k) => (k in sample ? sample[k] : m));
  function tplPreview() {
    const t = A.tpl[A.arg], box = $("#tp-preview");
    if (!box) return;
    const text = fill(val("tp-text"), t.sample);
    box.textContent = text;
    const n = text.length, over = n > A.captionLimit;
    $("#tp-count").innerHTML = t.media_url ? `${n}/${A.captionLimit} characters in the caption${over ? ` — <b style="color:var(--warn)">too long: the picture is sent first, then the text</b>` : ""}` : `${n} characters`;
  }
  async function secTemplate(key) {
    if (!A.tpl) { loading("Message"); await loadTemplates(); }
    if (!stillHere("template")) return;
    A.arg = key;
    const t = A.tpl[key];
    shell(esc(t.label), `
      <div class="muted small">${esc(t.help)}</div>
      <div class="card stack">
        <textarea id="tp-text" class="input" rows="9" data-a-input="tpl">${esc(t.text)}</textarea>
        <div><div class="field-label">Tap to add a placeholder (filled in when sent)</div><div class="chips">${t.vars.map((v) => `<button class="chip" data-a="chip" data-v="{${v}}">{${v}}</button>`).join("")}</div></div>
      </div>
      <div class="card stack"><b>Picture</b>
        <div id="tp-pic">${pic(t.media_url)}</div>
        <div class="row" style="gap:8px"><button class="btn ghost small" data-a="tplpic">${t.media_url ? "Change picture" : "Add picture"}</button>${t.media_url ? `<button class="btn ghost small" data-a="tplpic-rm">Remove picture</button>` : ""}</div>
      </div>
      <div><div class="section-title" style="margin:2px 4px 8px">What players see</div>
        <div class="bubble">${t.media_url ? `<img src="${esc(t.media_url)}" alt="">` : ""}<div id="tp-preview" class="bubble-text"></div></div>
        <div id="tp-count" class="muted small" style="margin:6px 4px 0"></div></div>
      <button class="btn" data-a="tplsave">Save message</button>
      <div class="row" style="gap:8px"><button class="btn ghost small" style="flex:1" data-a="tpltest">Send me a test</button>${t.custom ? `<button class="btn ghost small" style="flex:1" data-a="tplreset">Reset words to default</button>` : ""}</div>`, "‹ Messages");
    tplPreview();
  }

  /* ------------------------------------------------------------------ broadcast */
  async function secBroadcast() {
    loading("Announcements");
    const d = await get("/broadcasts");
    if (!stillHere("broadcast")) return;
    A.bcMedia = A.bcMedia || ""; A.bcMediaUrl = A.bcMediaUrl || "";
    const aud = Object.entries(d.audiences).map(([k, l]) => `<option value="${k}">${esc(l)}</option>`).join("");
    shell("Announcements", `
      <div class="card stack">
        <textarea id="bc-text" class="input" rows="6" maxlength="3500" placeholder="Write your message…" data-a-input="bc"></textarea>
        <div id="bc-pic">${pic(A.bcMediaUrl)}</div>
        <div class="row" style="gap:8px"><button class="btn ghost small" data-a="bcpic">${A.bcMediaUrl ? "Change picture" : "Add picture"}</button>${A.bcMediaUrl ? `<button class="btn ghost small" data-a="bcpic-rm">Remove picture</button>` : ""}</div>
        <div><div class="field-label">Send to</div><select id="bc-aud" class="input" data-a-input="bcaud">${aud}</select><div id="bc-count" class="muted small" style="margin-top:6px"></div></div>
        <div class="muted small">With a picture, your words are sent as its caption (up to 1,024 characters; longer text follows the picture as a second message).</div>
        <div class="row" style="gap:8px"><button class="btn ghost small" style="flex:1" data-a="bctest">Send me a test</button><button class="btn small" style="flex:1" data-a="bcsend">Send</button></div>
      </div>
      <div id="bc-progress"></div>
      <div class="section-title" style="margin:2px 4px">Recent</div>
      ${d.items.length ? d.items.map((b) => `<div class="card adm-card"><div class="muted small">${when(b.created_at)} · ${esc(d.audiences[b.audience] || b.audience)}</div><div style="margin:4px 0">${esc(b.text.slice(0, 140))}${b.text.length > 140 ? "…" : ""}</div>
        <span class="pill ${b.status === "done" ? "ok" : b.status === "error" ? "live" : "warn"}">${b.status === "done" ? "Sent" : b.status === "error" ? "Stopped" : "Sending…"}</span> <span class="muted small">${b.sent}/${b.total} delivered${b.failed ? ` · ${b.failed} couldn't be reached` : ""}</span></div>`).join("") : `<div class="muted small" style="padding:0 4px">Nothing sent yet.</div>`}`);
    audCount();
  }
  async function audCount() {
    const el = $("#bc-count");
    if (!el) return;
    try { const c = await get(`/audience?audience=${val("bc-aud")}`); A.bcCount = c.count; if ($("#bc-count")) $("#bc-count").textContent = `${c.count} ${c.count === 1 ? "person" : "people"} will get it.`; }
    catch (e) { el.textContent = e.message; A.bcCount = 0; }
  }
  function pollBroadcast(id) {
    clearInterval(A.pollTimer);
    const tick = async () => {
      if (!stillHere("broadcast")) return clearInterval(A.pollTimer);
      try {
        const b = await get(`/broadcasts/${id}`);
        const box = $("#bc-progress");
        if (box) box.innerHTML = `<div class="card"><b>${b.status === "sending" ? "Sending…" : b.status === "done" ? "Done ✅" : "Stopped part-way ⚠️"}</b><div class="bar-track"><div class="bar-fill" style="width:${b.total ? Math.round(((b.sent + b.failed) / b.total) * 100) : 100}%"></div></div><div class="muted small">${b.sent} delivered${b.failed ? ` · ${b.failed} couldn't be reached (blocked the bot)` : ""} of ${b.total}</div></div>`;
        if (b.status !== "sending") clearInterval(A.pollTimer);
      } catch (_) { clearInterval(A.pollTimer); }
    };
    tick();
    A.pollTimer = setInterval(tick, 1500);
  }

  /* ------------------------------------------------------------------ gameweek */
  async function secGameweek(gw) {
    loading("Gameweek & winners");
    const d = await get("/gameweek" + (gw ? `?gw=${gw}` : ""));
    if (!stillHere("gameweek")) return;
    A.gw = d.gameweek; A.gwLive = d.live;
    shell("Gameweek & winners", `
      <div class="gw-nav"><button class="round-btn" data-a="gwgo" data-gw="${d.gameweek - 1}" ${d.gameweek <= 1 ? "disabled" : ""}>‹</button>
        <div class="title"><b>Gameweek ${d.gameweek}</b><span class="muted small">${d.gameweek === d.live ? "live / current" : ""}</span></div>
        <button class="round-btn" data-a="gwgo" data-gw="${d.gameweek + 1}" ${d.gameweek >= 38 ? "disabled" : ""}>›</button></div>
      <div class="adm-grid"><div class="adm-stat"><span>Entered</span><b>${num(d.entrants)}</b></div><div class="adm-stat"><span>Pot (birr)</span><b>${num(d.pot)}</b></div></div>
      <div class="card"><b>Prize split</b><div class="muted small">${d.percents.map((p, i) => `${["🥇", "🥈", "🥉"][i] || i + 1 + "."} ${p}%`).join(" · ")} <span class="muted">(change in Settings)</span></div></div>
      ${d.winners.length ? `<div class="card"><b>${d.announced ? "Winners right now" : "Winners if announced now"}</b>
        ${d.winners.map((w) => `<div class="row between adm-line"><span>${w.medal} #${w.rank} ${esc(w.name)}</span><span class="muted small">${w.points} pts · FPL ${w.team_id}</span><b style="color:var(--accent)">${num(w.prize)}</b></div>`).join("")}</div>`
        : emptyState("⏳", "No entries yet", "Nobody has entered this gameweek.")}
      ${d.top.length ? `<div class="card"><b>Top 10</b>${d.top.map((r) => `<div class="row between adm-line"><span>${r.rank}. ${esc(r.name)}</span><b>${r.points}</b></div>`).join("")}</div>` : ""}
      ${d.entrants ? `${d.announced ? `<div class="note">Already announced. Announcing again recalculates and <b>replaces</b> the prizes in the players' wallets (money already withdrawn isn't affected).</div>` : ""}
      <button class="btn" data-a="announce" data-gw="${d.gameweek}">${d.announced ? "Announce again" : "Announce winners"}</button>
      <div class="muted small center">Winners get a private message; everyone entered gets an in-app notification.</div>` : ""}
      <div id="gw-result"></div>`);
  }

  /* ------------------------------------------------------------------ players */
  let searchTimer;
  async function secPlayers() {
    shell("Players", `<input id="pl-q" class="input" placeholder="Search name, team, FPL ID or Telegram ID" data-a-input="plq" autocomplete="off"><div id="pl-list">${skeleton(3)}</div>`);
    loadPlayers("");
  }
  async function loadPlayers(q) {
    try {
      const d = await get(`/players?q=${encodeURIComponent(q)}`);
      if (!$("#pl-list")) return;
      $("#pl-list").innerHTML = d.items.length ? d.items.map((p) => `<div class="lb-row adm-menu" data-a="player" data-id="${p.id}">${avatar(p.name)}<div class="grow"><b class="ellipsis" style="display:block">${esc(p.name)}${p.banned ? ' <span class="pill live">Blocked</span>' : ""}</b>
        <span class="muted small ellipsis" style="display:block">${esc(p.team_name || "")} · FPL ${p.fpl_id} · ${num(p.balance)} birr</span></div><span class="muted">›</span></div>`).join("") : emptyState("🔍", "No players found", "Try another name or ID.");
    } catch (e) { if ($("#pl-list")) $("#pl-list").innerHTML = errorView(e.message).replace('data-act="retry"', 'data-a="reload"'); }
  }
  async function secPlayer(id) {
    loading("Player");
    const p = await get(`/players/${id}`);
    if (!stillHere("player")) return;
    A.arg = id;
    const gwDefault = (A.ov && A.ov.open_entries && A.ov.open_entries.gameweek) || A.gwLive || "";
    shell(esc(p.name), `
      <div class="card"><div class="row">${avatar(p.name)}<div class="grow"><b>${esc(p.name)}</b> ${p.banned ? '<span class="pill live">Blocked</span>' : ""}<div class="muted small">${esc(p.team_name || "")} · FPL ${p.fpl_id}</div><div class="muted small">Telegram ID ${p.id}</div></div><div style="text-align:right"><div class="muted small">Wallet</div><b style="font-size:20px;color:var(--accent)">${num(p.balance)}</b></div></div>
        <div style="height:12px"></div><button class="btn ${p.banned ? "" : "ghost"} small" data-a="ban" data-banned="${p.banned ? 0 : 1}">${p.banned ? "Unblock player" : "Block player"}</button></div>
      <div class="card stack"><b>FPL team ID</b><div class="muted small">Fix a wrong ID. The team name is re-read from FPL.</div>
        <input id="pp-fpl" class="input" inputmode="numeric" value="${p.fpl_id}"><button class="btn ghost small" data-a="setfpl">Save FPL ID</button></div>
      <div class="card stack"><b>Entries</b>
        ${p.entries.length ? p.entries.map((e) => `<div class="row between adm-line"><span>GW${e.gameweek} ${e.paid ? "" : "(unpaid)"}</span><span class="muted small">${esc(e.tx || "")} · ${e.fee} birr</span>
          <span><button class="link-btn" data-a="rmentry" data-gw="${e.gameweek}" data-refund="0">Remove</button> <button class="link-btn" data-a="rmentry" data-gw="${e.gameweek}" data-refund="1">+ refund</button></span></div>`).join("") : `<div class="muted small">No entries yet.</div>`}
        <div class="field-label">Enter by hand (e.g. paid cash)</div>
        <div class="row" style="gap:8px"><input id="pp-gw" class="input" inputmode="numeric" placeholder="GW" value="${gwDefault}" style="width:90px"><button class="btn ghost small" style="flex:1" data-a="addentry">Enter in this gameweek</button></div></div>
      <div class="card stack"><b>Wallet</b><div class="muted small">Add money (e.g. 100) or take it off (e.g. -50). A reason is required and is kept in the log.</div>
        <div class="row" style="gap:8px"><input id="pp-amt" class="input" inputmode="numeric" placeholder="± birr" style="width:110px"><input id="pp-why" class="input" placeholder="Reason" maxlength="120"></div>
        <button class="btn small" data-a="wallet">Apply</button></div>
      ${p.history.length ? `<div class="card"><b>Recent wallet activity</b>${p.history.map((h) => `<div class="row between adm-line"><span>${esc(h.type)}${h.gameweek ? " GW" + h.gameweek : ""}</span><span class="muted small">${h.ts ? ago(h.ts) : ""}</span><b>${h.type === "withdrawal" ? "−" : h.amount > 0 && h.type !== "entry" ? "+" : ""}${fmt2(Math.abs(h.amount))}</b></div>`).join("")}</div>` : ""}`, "‹ Players");
  }

  /* ------------------------------------------------------------------ payments */
  async function secPayments() {
    loading("Payments");
    const d = await get(`/payments?status=${A.pmFilter}`);
    if (!stillHere("payments")) return;
    const seg = [["all", "All"], ["unused", "Unused"], ["used", "Used"]].map(([k, l]) => `<button class="${A.pmFilter === k ? "active" : ""}" data-a="pmfilter" data-k="${k}">${l}</button>`).join("");
    shell("Payments", `<div class="segment" style="grid-template-columns:repeat(3,1fr)">${seg}</div>
      <input id="pm-q" class="input" placeholder="Search transaction no. or name" data-a-input="pmq" autocomplete="off">
      <div class="card stack"><b>Record a payment by hand</b><div class="muted small">For when verify.et is down: record it, then the player submits the number as usual.</div>
        <div class="row" style="gap:8px"><input id="pm-tx" class="input" placeholder="Transaction no." autocapitalize="characters"><input id="pm-amt" class="input" inputmode="numeric" placeholder="Birr" style="width:90px"></div>
        <button class="btn small" data-a="addpay">Record payment</button></div>
      <div id="pm-list">${payList(d.items)}</div>`);
  }
  const payList = (items) => items.length ? items.map((p) => `<div class="card adm-card"><div class="row between"><b style="word-break:break-all">${esc(p.tx)}</b><b>${num(p.amount)} birr</b></div>
      <div class="muted small">${esc(p.sender || "")} · ${when(p.received_at)}</div>
      <div class="row between" style="margin-top:6px">${p.status === "used" ? `<span class="pill ok">Used by ${esc(p.used_by_name || p.used_by)}</span>` : `<span class="pill warn">Unused</span>`}
      ${p.status === "unused" ? `<button class="link-btn" data-a="delpay" data-tx="${esc(p.tx)}">Delete</button>` : `<span class="muted small">${when(p.used_at)}</span>`}</div></div>`).join("") : emptyState("🧾", "No payments", "Nothing matches.");

  /* ------------------------------------------------------------------ log & export */
  const ACT = { settings: "Changed settings", message: "Edited a message", upload_picture: "Uploaded a picture", withdrawal_paid: "Marked a withdrawal paid", withdrawal_rejected: "Rejected a withdrawal", announce: "Announced winners", broadcast: "Sent an announcement", wallet: "Changed a wallet", ban: "Blocked a player", unban: "Unblocked a player", fpl_id: "Changed an FPL ID", add_entry: "Entered a player", remove_entry: "Removed an entry", add_payment: "Recorded a payment", delete_payment: "Deleted a payment", export: "Exported data" };
  async function secLog() {
    loading("Activity log & export");
    const d = await get("/audit");
    if (!stillHere("log")) return;
    const ex = [["players", "Players"], ["entries", "Entries"], ["payments", "Payments"], ["withdrawals", "Withdrawals"], ["prizes", "Prizes"]]
      .map(([k, l]) => `<button class="btn ghost small" data-a="export" data-k="${k}">${l}</button>`).join("");
    shell("Activity log & export", `
      <div class="card stack"><b>Download data (CSV for Excel / Sheets)</b><div class="muted small">The file is sent to you in the bot chat on Telegram.</div><div class="chips">${ex}</div></div>
      <div class="section-title" style="margin:2px 4px">Who changed what</div>
      ${d.items.length ? d.items.map((x) => `<div class="card adm-card"><div class="row between"><b>${esc(ACT[x.action] || x.action)}</b><span class="muted small">${when(x.at)}</span></div>
        <div class="muted small" style="word-break:break-word">${esc(x.detail || "")}</div><div class="muted small">by ${x.admin_id === d.owner_id ? "owner" : "co-admin " + x.admin_id}</div></div>`).join("") : emptyState("📜", "Nothing yet", "Changes you make will be listed here.")}`);
  }

  const SEC = { withdrawals: secWithdrawals, settings: secSettings, messages: secMessages, template: secTemplate, broadcast: secBroadcast, gameweek: secGameweek, players: secPlayers, player: secPlayer, payments: secPayments, log: secLog };

  /* ------------------------------------------------------------------ clicks */
  view.addEventListener("click", async (ev) => {
    const el = ev.target.closest("[data-a]");
    if (!el || window.ETFPL.state.tab !== "admin") return;
    const d = el.dataset;
    haptic();
    switch (d.a) {
      case "back":
        if (A.sec === "template") return go("messages");
        if (A.sec === "player") return go("players");
        return renderHome();
      case "reload": return A.sec ? go(A.sec, A.arg) : renderHome();
      case "go": return go(d.sec);
      case "copy": return copyText(d.v);
      case "resume": return guard(el, async () => { await post("/settings", { maintenance: false }); await renderHome(); toast("Entries resumed ✅"); });

      /* withdrawals */
      case "wdfilter": A.wdFilter = d.k; return go("withdrawals");
      case "wd": {
        const paid = d.do === "paid";
        if (!(await ask(paid ? `Mark ${d.amt} birr to ${d.acc} as PAID? Only do this after you've sent the money.` : `Reject this ${d.amt} birr request? It goes back to the player's wallet.`))) return;
        return guard(el, async () => { await post(`/withdrawals/${d.id}`, { action: d.do }); toast(paid ? "Marked as paid ✅" : "Rejected — returned to wallet"); await go("withdrawals"); });
      }

      /* settings */
      case "toggle": {
        const on = !el.classList.contains("on");
        const map = { "st-maint": "maintenance", "st-all": "announce_winners_to_all" };
        if (!map[el.id]) return;
        el.classList.toggle("on", on);
        try { await post("/settings", { [map[el.id]]: on }); toast(on ? "Turned on ✅" : "Turned off"); }
        catch (e) { el.classList.toggle("on", !on); toast(e.message, true); }
        return;
      }
      case "save": {
        const body = SAVE[d.what]();
        return guard(el, async () => { const r = await post("/settings", body); toast("Saved ✅"); if (d.what === "prizes") A.ov = null; if (A.sec === "settings") { const keep = window.scrollY; await go("settings"); window.scrollTo({ top: keep }); } return r; });
      }
      case "prizepic": return guard(el, async () => { const r = await uploadPicture(); if (!r) return; A.prizeMedia = r.token; await post("/settings", { prize_media: r.token }); toast("Picture saved ✅"); await go("settings"); });
      case "prizepic-rm": return guard(el, async () => { await post("/settings", { prize_media: "" }); toast("Picture removed"); await go("settings"); });

      /* messages */
      case "tpl": return go("template", d.key);
      case "chip": { const ta = $("#tp-text"); const i = ta.selectionStart || ta.value.length; ta.value = ta.value.slice(0, i) + d.v + ta.value.slice(ta.selectionEnd || i); ta.focus(); ta.selectionStart = ta.selectionEnd = i + d.v.length; return tplPreview(); }
      case "tplsave": return guard(el, async () => { await post(`/templates/${A.arg}`, { text: val("tp-text") }); toast("Message saved ✅"); A.tpl = null; await go("template", A.arg); });
      case "tplreset": if (!(await ask("Go back to the original wording?"))) return; return guard(el, async () => { await post(`/templates/${A.arg}`, { text: "" }); toast("Reset ✅"); A.tpl = null; await go("template", A.arg); });
      case "tplpic": return guard(el, async () => { const r = await uploadPicture(); if (!r) return; await post(`/templates/${A.arg}`, { media_token: r.token, text: val("tp-text") }); toast("Picture added ✅"); A.tpl = null; await go("template", A.arg); });
      case "tplpic-rm": return guard(el, async () => { await post(`/templates/${A.arg}`, { media_token: "", text: val("tp-text") }); toast("Picture removed"); A.tpl = null; await go("template", A.arg); });
      case "tpltest": return guard(el, async () => { await post(`/templates/${A.arg}`, { text: val("tp-text") }); await post(`/templates/${A.arg}/test`); toast("Sent to you on Telegram ✅"); });

      /* broadcast */
      case "bcpic": return guard(el, async () => { const r = await uploadPicture(); if (!r) return; A.bcMedia = r.token; A.bcMediaUrl = r.url; const keep = val("bc-text"); await go("broadcast"); $("#bc-text").value = keep; });
      case "bcpic-rm": { const keep = val("bc-text"); A.bcMedia = ""; A.bcMediaUrl = ""; await go("broadcast"); $("#bc-text").value = keep; return; }
      case "bctest": return guard(el, async () => { await post("/broadcast", { text: val("bc-text"), media_token: A.bcMedia, audience: "me" }); toast("Test sent to you ✅"); });
      case "bcsend": {
        if (!val("bc-text").trim()) return toast("Write a message first.", true);
        if (!(await ask(`Send this to ${A.bcCount} ${A.bcCount === 1 ? "person" : "people"} now? It can't be undone.`))) return;
        return guard(el, async () => { const r = await post("/broadcast", { text: val("bc-text"), media_token: A.bcMedia, audience: val("bc-aud") }); toast("Sending…"); pollBroadcast(r.id); });
      }

      /* gameweek */
      case "gwgo": return go("gameweek", Number(d.gw));
      case "announce": {
        if (!(await ask(`Announce the winners of GW${d.gw} and add the prizes to their wallets?`))) return;
        return guard(el, async () => { const r = await post("/gameweek/announce", { gameweek: Number(d.gw) }); toast("Winners announced 🏆"); await go("gameweek", Number(d.gw)); $("#gw-result").innerHTML = `<div class="card"><b>Done</b><pre class="adm-pre">${esc(r.summary)}</pre></div>`; });
      }

      /* players */
      case "player": return go("player", Number(d.id));
      case "ban": {
        const ban = d.banned === "1";
        if (!(await ask(ban ? "Block this player? They won't be able to enter or withdraw." : "Unblock this player?"))) return;
        return guard(el, async () => { await post(`/players/${A.arg}/ban`, { banned: ban }); toast(ban ? "Blocked" : "Unblocked ✅"); await go("player", A.arg); });
      }
      case "setfpl": return guard(el, async () => { const r = await post(`/players/${A.arg}/fpl-id`, { team_id: Number(val("pp-fpl")) }); toast(`Linked "${r.team_name}" ✅`); await go("player", A.arg); });
      case "addentry": return guard(el, async () => { await post(`/players/${A.arg}/entry`, { gameweek: Number(val("pp-gw")), action: "add" }); toast("Entered ✅"); await go("player", A.arg); });
      case "rmentry": {
        const refund = d.refund === "1";
        if (!(await ask(`Remove this player from GW${d.gw}${refund ? " and refund the fee to their wallet" : " (no refund)"}?`))) return;
        return guard(el, async () => { await post(`/players/${A.arg}/entry`, { gameweek: Number(d.gw), action: "remove", refund }); toast("Entry removed"); await go("player", A.arg); });
      }
      case "wallet": {
        const amount = Number(val("pp-amt"));
        if (!Number.isInteger(amount) || !amount) return toast("Enter a whole number of birr, like 100 or -50.", true);
        if (!(await ask(`${amount > 0 ? "Add" : "Take off"} ${Math.abs(amount)} birr ${amount > 0 ? "to" : "from"} this wallet?`))) return;
        return guard(el, async () => { await post(`/players/${A.arg}/wallet`, { amount, reason: val("pp-why") }); toast("Wallet updated ✅"); await go("player", A.arg); });
      }

      /* payments */
      case "pmfilter": A.pmFilter = d.k; return go("payments");
      case "addpay": return guard(el, async () => { await post("/payments", { tx: val("pm-tx"), amount: Number(val("pm-amt")) }); toast("Payment recorded ✅"); await go("payments"); });
      case "delpay": if (!(await ask(`Delete payment ${d.tx}?`))) return; return guard(el, async () => { await api(`/admin/payments/${encodeURIComponent(d.tx)}`, { method: "DELETE" }); toast("Deleted"); await go("payments"); });

      /* log */
      case "export": return guard(el, async () => { const r = await post("/export", { kind: d.k }); toast(`${r.filename} sent to your Telegram chat ✅`); });
    }
  });

  view.addEventListener("input", (ev) => {
    const k = ev.target && ev.target.dataset && ev.target.dataset.aInput;
    if (!k || window.ETFPL.state.tab !== "admin") return;
    if (k === "prizes") prizeHint();
    else if (k === "tpl") tplPreview();
    else if (k === "bcaud") audCount();
    else if (k === "plq") { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadPlayers(ev.target.value), 300); }
    else if (k === "pmq") {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(async () => {
        try { const d = await get(`/payments?status=${A.pmFilter}&q=${encodeURIComponent(ev.target.value)}`); if ($("#pm-list")) $("#pm-list").innerHTML = payList(d.items); } catch (_) {}
      }, 300);
    }
  });
  view.addEventListener("change", (ev) => { if (ev.target && ev.target.dataset && ev.target.dataset.aInput === "bcaud") audCount(); });

  window.ETFPLAdmin = { open };
})();
