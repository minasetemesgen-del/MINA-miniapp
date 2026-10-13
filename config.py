import os

# ---------------------------------------------------------------------------
# Everything is read from environment variables (safer for hosting — your
# token never sits in plain text in your code or on GitHub).
#
# ON RAILWAY: go to your project -> Variables tab -> add each of these:
#   BOT_TOKEN
#   ADMIN_TELEGRAM_ID
#   TELEBIRR_NUMBER / TELEBIRR_NAME / SMS_WEBHOOK_SECRET   (see README)
#   WEBAPP_URL              (the public https URL of this service — see README)
#   DB_PATH                 (optional, e.g. /data/fpl_contest.db with a volume)
#   ENTRY_FEE_BIRR          (optional, defaults to 100)
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError(
        "BOT_TOKEN is not set. Add it as an environment variable "
        "(Railway: Project -> Variables -> BOT_TOKEN)."
    )

_admin_id = os.environ.get("ADMIN_TELEGRAM_ID")
if not _admin_id:
    raise ValueError(
        "ADMIN_TELEGRAM_ID is not set. Add it as an environment variable "
        "(Railway: Project -> Variables -> ADMIN_TELEGRAM_ID)."
    )
ADMIN_TELEGRAM_ID = int(_admin_id)

ENTRY_FEE_BIRR = int(os.environ.get("ENTRY_FEE_BIRR", "100"))

# ---------------------------------------------------------------------------
# Telebirr payments (SMS gateway)
# Players send the entry fee to YOUR Telebirr account, then submit the
# transaction number (or the whole SMS) in the app. A spare Android phone with
# your receiving SIM forwards each incoming Telebirr SMS to /api/sms-hook, and
# the app matches the number against those messages.
# ---------------------------------------------------------------------------
# Shown to players in Wallet -> Deposit.
TELEBIRR_NUMBER = os.environ.get("TELEBIRR_NUMBER", "")
TELEBIRR_NAME = os.environ.get("TELEBIRR_NAME", "")

# A long random password. The phone's SMS-forwarder app must send it with every
# request (header "X-Webhook-Secret"). Without it, anyone could fake payments.
SMS_WEBHOOK_SECRET = os.environ.get("SMS_WEBHOOK_SECRET", "")

# Only SMS from these senders are accepted (comma separated, not case sensitive).
SMS_ALLOWED_SENDERS = {
    x.strip().lower() for x in os.environ.get("SMS_ALLOWED_SENDERS", "telebirr,127").split(",") if x.strip()
}
# Extra words that mark an SMS as "money received" (comma separated, optional).
SMS_EXTRA_KEYWORDS = [x.strip().lower() for x in os.environ.get("SMS_EXTRA_KEYWORDS", "").split(",") if x.strip()]

# Smallest amount a winner can withdraw.
MIN_WITHDRAWAL = int(os.environ.get("MIN_WITHDRAWAL", "100"))

# ---------------------------------------------------------------------------
# Mini App settings
# ---------------------------------------------------------------------------
# Public HTTPS address of this service, e.g. https://etfpl-production.up.railway.app
# Telegram requires HTTPS. Leave empty to skip the menu button / "Open app" buttons.
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")

# DEV_MODE=1 lets you open the app in a normal browser with ?dev=<any number>
# (no Telegram needed). NEVER enable this on the live server.
DEV_MODE = os.environ.get("DEV_MODE", "0") == "1"

# Set RUN_BOT=0 to run only the website/API (handy for UI work).
RUN_BOT = os.environ.get("RUN_BOT", "1") == "1"
