# ETFPL — Telegram Mini App + Bot

One service runs **the website (Mini App), its API, and the Telegram bot** together,
sharing one database. Users open the app from a button in your bot.

**Screens:** Home (enter & pay) · Top 10 leaderboard · Premier League table ·
Fixtures (any gameweek) · My Team (pitch view) + Transfers · Notifications (🔔)

## Stack (and why)

| Part | Chosen | Alternative |
|---|---|---|
| Backend | **FastAPI (Python)** — reuses your bot code, same SQLite database, one language | Node/Express or Next.js API routes (would mean rewriting the bot logic) |
| Front end | **Plain HTML/CSS/JS** served by the same service — no build step | React/Vite (nicer for a big team, overkill here) |
| Hosting | **Railway, one service** — already your host, free HTTPS (Telegram requires it) | Vercel/Netlify for the website + Railway for the API (two deployments, CORS, more to break) |

## 1. Try it on your computer (no Telegram needed)

```
pip install -r requirements.txt
python dev/test_smoke.py          # self-check of the API
python dev/mock_server.py         # fake data, open http://localhost:8000/?dev=888
```
Other test players: `?dev=777` (brand new), `?dev=889` (ranked outside the Top 10).

## 2. Deploy on Railway

1. Replace your GitHub repo's files with this folder's contents and push.
2. **Add a Volume** (Railway → your service → Settings → Volumes) mounted at `/data`.
   Without it, Railway wipes your database on every redeploy — all users and payments lost.
3. In **Variables**, set:
   - `BOT_TOKEN`, `ADMIN_TELEGRAM_ID` (as before). `CHAPA_SECRET_KEY` is no longer used — you can delete it.
   - `TELEBIRR_NUMBER`, `TELEBIRR_NAME`, `SMS_WEBHOOK_SECRET` — see **Telebirr payments** below
   - `DB_PATH` = `/data/fpl_contest.db`
   - `WEBAPP_URL` = your public address (next step), e.g. `https://etfpl-production.up.railway.app`
4. Settings → Networking → **Generate Domain**, copy it into `WEBAPP_URL`, redeploy.
5. Logs should show `Bot started` and `Menu button set to https://...`.
6. Open your bot in Telegram → send `/start` → tap **Open ETFPL App**. The blue menu
   button next to the message box also opens it.

Optional: in @BotFather send `/newapp` to get a shareable `t.me/yourbot/app` link.

**Moving your existing database?** Upload your old `fpl_contest.db` into the volume as
`/data/fpl_contest.db`. The new tables/columns are added automatically.

## What's new for players

- `/app` command and "Open ETFPL App" buttons in the bot
- Registration now **checks the team exists on FPL** and stores the team name
- Paying inside the app confirms the moment Chapa says yes
- ⏰ Deadline reminders ~24h and ~3h before entries close (for people who haven't entered);
  each person can switch them off in the app (🔔 → Deadline reminders)
- `/leaderboard` in the bot now shows the Top 10 too

## Telebirr payments (replaces Chapa)

Players send the entry fee to **your** Telebirr account, then paste the transaction number
(or the whole SMS) in **Wallet → Deposit**. The app checks it against the SMS your phone received.

**One-time setup**
1. In Railway → Variables add:
   - `TELEBIRR_NUMBER` — the receiving Telebirr number players pay to
   - `TELEBIRR_NAME` — the account name shown to players
   - `SMS_WEBHOOK_SECRET` — a long random password (e.g. 40 random letters/numbers)
   - optional: `MIN_WITHDRAWAL` (default 100), `SMS_ALLOWED_SENDERS` (default `telebirr,127`)
2. Keep a spare Android phone with the receiving SIM, powered, online, with battery
   optimisation switched off for the SMS-forwarder app.
3. In an SMS-forwarder app, forward SMS from **telebirr** (or **127**) as an HTTP POST to
   `https://YOUR-DOMAIN/api/sms-hook` with the header `X-Webhook-Secret: <your secret>`
   and a JSON body like `{"sender": "telebirr", "message": "<the SMS text>"}`.
   (Other common field names — `from`, `text`, `body` — also work.)
4. **Check it understands your real SMS:** in the bot send `/parsesms <paste a real Telebirr SMS>`.
   It must say "This SMS would be accepted". Then make a small real payment end to end.

**How verification works:** each received-payment SMS is stored once as *unused*. A player's
submission must match an unused payment of at least the entry fee; it is then marked *used*
and they're entered. A wrong number shows "Transaction number incorrect". Six wrong tries in an
hour locks that player out for a while (stops people guessing numbers). If they sent more than
the entry fee, the extra goes to their wallet.

**Withdrawals:** only people with a balance (prizes are added when you run `/announcewinner`)
can request one. You get a Telegram message with **✅ Mark paid / ❌ Reject** buttons: send the
money yourself on Telebirr, then tap Mark paid. Reject returns it to their wallet.
`/withdrawals` lists requests still waiting.

**Admin commands:** `/parsesms`, `/addpayment <txid> <amount>` (record a payment by hand if the
phone was offline), `/withdrawals`, `/announcewinner [gw]`.

## Important notes

- **Never set `DEV_MODE=1` on the live server** — it lets anyone pretend to be any user.
  The live app only trusts Telegram's signed login data.
- Run **either** `server.py` (website + bot) **or** `python bot.py` (bot only) — never both
  with the same token, or Telegram rejects the second one.
- **Entries now open for the *upcoming* gameweek.** The old code used FPL's "current"
  gameweek, whose deadline has already passed, so `/pay` would have said "closed" during
  the season. Entries/payment → next gameweek; leaderboard/winner → the gameweek being played.
- Player photos and club badges load from the Premier League's image servers. If they
  ever change addresses, a placeholder silhouette/shield shows instead — update the two
  URL lists at the top of `static/app.js`.
- Before taking real money, check Ethiopia's rules on cash-prize contests (as in your original README).
