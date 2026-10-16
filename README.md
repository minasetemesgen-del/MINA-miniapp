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
   - `TELEBIRR_NUMBER`, `TELEBIRR_NAME`, `VERIFY_ET_API_KEY` — see **Telebirr payments** below
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
- Paying: send the entry fee on Telebirr, paste the transaction number, and it's verified automatically within seconds
- ⏰ Deadline reminders ~24h and ~3h before entries close (for people who haven't entered);
  each person can switch them off in the app (🔔 → Deadline reminders)
- `/leaderboard` in the bot now shows the Top 10 too

## Telebirr payments (verified by verify.et — no spare phone needed)

Players send the entry fee to **your** Telebirr account, then paste the transaction number
(or the whole SMS) in **Wallet → Deposit** (or `/deposit <number>` in the bot). The app asks
[verify.et](https://verify.et) whether that payment really reached your account, and enters the player.

**One-time setup**
1. Create an account on verify.et and make an **API key** with the `verification:write`
   (and `verification:read`) permissions. It starts with `VERIFY_BANK_ET_`.
2. In Railway → Variables add:
   - `VERIFY_ET_API_KEY` — that key
   - `TELEBIRR_NUMBER` — the Telebirr number players pay to (e.g. `0939080897`). It is also sent
     to verify.et so a payment made to anyone else is rejected.
   - `TELEBIRR_NAME` — the account name shown to players
   - optional: `MAX_RECEIPT_AGE_HOURS` (default 240 = 10 days; older receipts are refused),
     `MIN_WITHDRAWAL` (default 100), `VERIFY_ET_BASE_URL` (default `https://verify.et`)
3. Redeploy, then **test it**: in the bot send `/verifytest <a real transaction number>`.
   It shows the amount, receiver and date verify.et returns and whether the app would accept it
   (nothing is saved). Then make one small real payment end to end.

**What is checked:** the payment exists, it reached *your* number, it is in birr, it is at least the
entry fee, its date is not in the future or too old, and the transaction number was never used
before. Verified payments are saved locally, so a repeat attempt costs no extra verify.et credit.
Six wrong tries in an hour locks that player out for a while. Extra money (sent more than the fee)
goes to the player's wallet. If verify.et is down, busy, out of credits or the key is wrong, the
player just sees "try again later" (it never counts against them) and **you get a Telegram alert**
(at most one per hour).

**Cost:** verify.et has a free plan with a monthly allowance and paid plans beyond that — check
verify.et/pricing for current numbers, and note how many credits one check uses.

**Emergency / optional old method:** if verify.et is unavailable you can still record a payment by
hand with `/addpayment <txid> <amount>`; the player then submits it as usual. The old SMS-forwarder
phone still works too if you set `SMS_WEBHOOK_SECRET` (forward SMS to `/api/sms-hook` with header
`X-Webhook-Secret`), but it is no longer needed — you can delete that variable.

**Withdrawals:** only people with a balance (prizes are added when you run `/announcewinner`)
can request one. You get a Telegram message with **✅ Mark paid / ❌ Reject** buttons: send the
money yourself on Telebirr, then tap Mark paid. Reject returns it to their wallet.
`/withdrawals` lists requests still waiting.

**Admin commands:** `/verifytest <txid>`, `/addpayment <txid> <amount>` (record a payment by hand),
`/parsesms` (only for the old SMS method), `/withdrawals`, `/announcewinner [gw]`.

## Wrong FPL ID? Players can log out

On the Home screen, next to the linked team, there is a **"Wrong ID? Log out"** button (or `/logout` in the bot).
It unlinks the team so the player can link the right one. Their wallet, entries and any block stay as they are.

**Safety rule:** once a gameweek's deadline has passed and its winners haven't been announced, a player with
a paid entry for it can't log out or switch teams (otherwise they could pick whichever team scored best).
Before the deadline they can change freely. If someone is locked out by mistake, fix their ID yourself in
**Admin → Players**.

## Top 10: tap a name to see their team

In **Top 10**, tapping a player (podium or list) opens their squad for the gameweek — pitch, captain, bench and points.
- Only the **Top 10** (and your own row) can be opened; everyone else stays private.
- Squads are shown **only after the FPL deadline** has passed, so nobody can copy a team before it locks.
- The app never receives other players' Telegram IDs — each row carries a private tag made by the server.

## Admin dashboard (no code edits needed)

If your Telegram account is the `ADMIN_TELEGRAM_ID`, the Mini App shows an **Admin** button on Home.
The server checks admin rights on every call (hiding the button is not the protection). Everything
you change is saved in the database and takes effect straight away — no redeploy.

| Section | What you can do |
|---|---|
| **Overview** | Players, pot, money owed in wallets, waiting withdrawals, money collected, verify.et health |
| **Withdrawals** | Mark paid / Reject (same as the Telegram buttons) and see past ones |
| **Settings** | Prize percentages (1st, 2nd, 3rd… up to 10 places; must total 100% or less), entry fee, minimum withdrawal, reminder timing, pause entries, player limit, co-admins, winners' prize text and picture |
| **Messages** | Edit every message the bot sends by itself (welcome, payment instructions, reminders, winner messages…), with a live preview, a test-send to yourself and "reset to default" |
| **Announcements** | Message everyone, only players who haven't entered, or only entered players — with an optional picture |
| **Gameweek & winners** | Preview and announce winners for any gameweek |
| **Players** | Search, block/unblock, fix a wrong FPL ID, add or remove an entry, adjust a wallet (with a reason) |
| **Payments** | Every verified payment, record one by hand, remove an unused one |
| **Activity log & export** | Who changed what and when; download Players / Entries / Payments / Withdrawals / Prizes as CSV (sent to you in the bot chat) |

**Photo + caption:** attach a picture to any message (Messages or Announcements). The bot then sends
**a photo with the text as its caption**; with no picture it sends a normal text message. Telegram
limits a caption to 1024 characters — a longer text is sent as the picture followed by the full text.
If a picture ever fails to send, the text is sent on its own, so nothing is lost. Messages can use
placeholders such as `{fee}`, `{gw}`, `{deadline}`, `{prize}`; the editor lists the ones each message supports.

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
