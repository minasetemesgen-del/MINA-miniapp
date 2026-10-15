import os

# ---------------------------------------------------------------------------
# Everything is read from environment variables (safer for hosting — your
# token never sits in plain text in your code or on GitHub).
#
# ON RAILWAY: go to your project -> Variables tab -> add each of these:
#   BOT_TOKEN
#   ADMIN_TELEGRAM_ID
#   TELEBIRR_NUMBER / TELEBIRR_NAME / VERIFY_ET_API_KEY    (see README)
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
# Telebirr payments
# Players send the entry fee to YOUR Telebirr account, then submit the
# transaction number (or the whole SMS) in the app. The app asks verify.et
# (a third-party receipt-verification service) whether that payment really
# reached your account — no phone has to stay switched on.
# ---------------------------------------------------------------------------
# Shown to players in Wallet -> Deposit. Also sent to verify.et as the account
# the money must have arrived in, so a payment to anyone else is rejected.
TELEBIRR_NUMBER = os.environ.get("TELEBIRR_NUMBER", "")
TELEBIRR_NAME = os.environ.get("TELEBIRR_NAME", "")

# verify.et API key (starts with VERIFY_BANK_ET_). Create it on verify.et ->
# API Keys, with the "verification:write" permission (and "verification:read").
VERIFY_ET_API_KEY = os.environ.get("VERIFY_ET_API_KEY", "").strip()
VERIFY_ET_BASE_URL = os.environ.get("VERIFY_ET_BASE_URL", "https://verify.et").rstrip("/")

# A payment older than this (in hours) is refused, so an old receipt can't be
# recycled. Entry for a gameweek can be paid for up to about a week before its
# deadline, so the default leaves a little room beyond that.
MAX_RECEIPT_AGE_HOURS = int(os.environ.get("MAX_RECEIPT_AGE_HOURS", "240"))

# OPTIONAL old method — only needed if you still forward Telebirr SMS from a
# spare phone. A long random password the SMS-forwarder app sends with every
# request (header "X-Webhook-Secret"). Leave empty to switch the phone method off.
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
