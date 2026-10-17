import asyncio
import logging
import os
import re

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CF_API_TOKEN = os.environ["CF_API_TOKEN"]
ALLOWED_CHAT_IDS = {
    int(x.strip())
    for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",")
    if x.strip()
}
PORT = int(os.environ.get("PORT", "8080"))

CF_API = "https://api.cloudflare.com/client/v4"
CF_HEADERS = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json",
}

# Conversation states
ASK_SUBDOMAIN, CHOOSE_ZONE, ASK_TARGET = range(3)

SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
TARGET_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logger = logging.getLogger("cf-bot")

if not ALLOWED_CHAT_IDS:
    logger.warning(
        "ALLOWED_CHAT_IDS is empty — the bot will not respond to anyone. "
        "Set it in your environment to your Telegram user ID."
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def is_authorized(update: Update) -> bool:
    """Only allow whitelisted Telegram chat IDs."""
    chat = update.effective_chat
    return bool(chat and chat.id in ALLOWED_CHAT_IDS)


async def cf_get_zones(session: aiohttp.ClientSession) -> list:
    """Fetch all Cloudflare zones across every available page.

    The previous implementation only read the first page of results, which meant
    users with more than 50 zones would see an incomplete list in the Telegram UI.
    """
    zones: list[dict] = []
    page = 1

    while True:
        url = f"{CF_API}/zones?per_page=100&page={page}"
        async with session.get(url, headers=CF_HEADERS) as r:
            data = await r.json()

        if not data.get("success"):
            raise RuntimeError(data.get("errors") or "Cloudflare API error")

        page_zones = data.get("result") or []
        zones.extend(page_zones)

        result_info = data.get("result_info") or {}
        total_pages = int(result_info.get("total_pages") or 1)
        if page >= total_pages or not page_zones:
            break
        page += 1

    return zones


async def cf_create_cname(
    session: aiohttp.ClientSession,
    zone_id: str,
    name: str,
    content: str,
    proxied: bool = False,
    ttl: int = 1,
) -> dict:
    url = f"{CF_API}/zones/{zone_id}/dns_records"
    payload = {
        "type": "CNAME",
        "name": name,
        "content": content,
        "proxied": proxied,
        "ttl": ttl,
    }
    async with session.post(url, headers=CF_HEADERS, json=payload) as r:
        return await r.json()


def humanize_ttl(ttl) -> str:
    try:
        ttl = int(ttl)
    except (TypeError, ValueError):
        return "Auto"
    return "Auto" if ttl == 1 else f"{ttl}s"


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #
HELP_TEXT = (
    "🤖 *Cloudflare DNS Manager*\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "I help you manage Cloudflare DNS records right from Telegram, so you "
    "never have to open the heavy Cloudflare dashboard on mobile data.\n\n"
    "*Commands*\n"
    "• /cname — Create a CNAME record\n"
    "• /cancel — Cancel current operation\n"
    "• /help — Show this message\n\n"
    "💡 _Example:_ create `mysub` and point it to `user.github.io`."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update):
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "✅ Cancelled. Send /cname whenever you're ready."
    )
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# Conversation: /cname
# --------------------------------------------------------------------------- #
async def cmd_cname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update):
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "📝 *Step 1/3* — Send the *subdomain name* you want to create.\n\n"
        "Example: `mysub`\n\n"
        "_Send /cancel to abort._",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_SUBDOMAIN


async def handle_subdomain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update):
        return ConversationHandler.END

    sub = (update.message.text or "").strip().lower()
    if not SUBDOMAIN_RE.match(sub):
        await update.message.reply_text(
            "❌ Invalid subdomain. Use only lowercase letters, digits, and "
            "hyphens — must start and end with a letter or digit.\n\nTry again:"
        )
        return ASK_SUBDOMAIN

    context.user_data["subdomain"] = sub

    try:
        async with aiohttp.ClientSession() as session:
            zones = await cf_get_zones(session)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to fetch Cloudflare zones")
        await update.message.reply_text(
            f"❌ Could not fetch your domains from Cloudflare.\n`{exc}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data.clear()
        return ConversationHandler.END

    if not zones:
        await update.message.reply_text(
            "❌ No domains found in your Cloudflare account."
        )
        context.user_data.clear()
        return ConversationHandler.END

    # Cache zones by numeric index (callback_data has a 64-byte limit).
    context.user_data["zones"] = {str(i): z for i, z in enumerate(zones)}

    rows = [
        [InlineKeyboardButton(f"{sub}.{z['name']}", callback_data=f"zone:{i}")]
        for i, z in enumerate(zones)
    ]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])

    await update.message.reply_text(
        f"🌐 *Step 2/3* — Pick the full domain for `{sub}`:",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.MARKDOWN,
    )
    return CHOOSE_ZONE


async def handle_zone_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not is_authorized(update):
        return ConversationHandler.END

    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    try:
        idx = query.data.split(":", 1)[1]
        zone = context.user_data["zones"][idx]
    except (KeyError, IndexError):
        await query.edit_message_text(
            "⚠️ Session expired. Please run /cname again."
        )
        return ConversationHandler.END

    sub = context.user_data["subdomain"]
    fqdn = f"{sub}.{zone['name']}"
    context.user_data["zone"] = zone
    context.user_data["fqdn"] = fqdn

    await query.edit_message_text(
        f"🎯 *Step 3/3* — Selected: `{fqdn}`\n\n"
        "Now send the *target* hostname.\n"
        "Example: `user.github.io`\n"
        "_Do not include `https://` or a trailing path._",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_TARGET


async def handle_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update):
        return ConversationHandler.END

    raw = (update.message.text or "").strip().lower()
    target = re.sub(r"^https?://", "", raw).rstrip("/")
    if not TARGET_RE.match(target) or " " in target:
        await update.message.reply_text(
            "❌ Invalid target. Send a hostname like "
            "`user.github.io`:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ASK_TARGET

    zone = context.user_data.get("zone")
    fqdn = context.user_data.get("fqdn")
    if not zone or not fqdn:
        await update.message.reply_text(
            "⚠️ Session expired. Please run /cname again."
        )
        return ConversationHandler.END

    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        async with aiohttp.ClientSession() as session:
            result = await cf_create_cname(session, zone["id"], fqdn, target)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Cloudflare create failed")
        await update.message.reply_text(
            f"❌ Request failed:\n`{exc}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data.clear()
        return ConversationHandler.END

    if result.get("success"):
        rec = result["result"]
        proxied = "Yes" if rec.get("proxied") else "No"
        await update.message.reply_text(
            "✅ *DNS record created successfully!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Domain:* `{rec['name']}`\n"
            f"• *Type:* `{rec['type']}`\n"
            f"• *Target:* `{rec['content']}`\n"
            f"• *Proxied:* `{proxied}`\n"
            f"• *TTL:* `{humanize_ttl(rec.get('ttl'))}`\n\n"
            "_Propagation may take a few minutes._",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        errors = result.get("errors") or []
        lines = (
            "\n".join(
                f"• `{e.get('code')}` — {e.get('message')}"
                for e in errors
            )
            or "Unknown error"
        )
        await update.message.reply_text(
            f"❌ *Failed to create record:*\n{lines}",
            parse_mode=ParseMode.MARKDOWN,
        )

    context.user_data.clear()
    return ConversationHandler.END


async def handle_unknown_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "🤔 Unknown command. Try /help to see what I can do."
    )


# --------------------------------------------------------------------------- #
# Health endpoint (for Render / UptimeRobot)
# --------------------------------------------------------------------------- #
async def _health_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "cf-telegram-bot"})


async def start_health_server() -> None:
    app = web.Application()
    for method in ("GET", "HEAD"):
        app.router.add_route(method, "/", _health_handler)
        app.router.add_route(method, "/health", _health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Health server listening on port %s", PORT)


# --------------------------------------------------------------------------- #
# App bootstrap
# --------------------------------------------------------------------------- #
async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            ("cname", "Create a CNAME record"),
            ("cancel", "Cancel current operation"),
            ("help", "Show help"),
            ("start", "Show help"),
        ]
    )
    await start_health_server()


def build_application() -> Application:
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    conversation = ConversationHandler(
        entry_points=[CommandHandler("cname", cmd_cname)],
        states={
            ASK_SUBDOMAIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_subdomain)
            ],
            CHOOSE_ZONE: [CallbackQueryHandler(handle_zone_choice)],
            ASK_TARGET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_target)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start", cmd_start),
            CommandHandler("help", cmd_start),
        ],
        per_user=True,
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_start))
    application.add_handler(conversation)
    application.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))

    return application


def main() -> None:
    logger.info("Starting Cloudflare DNS Telegram bot (long polling)...")
    application = build_application()
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    # Explicitly create an event loop for maximum compatibility with
    # Python 3.14+ where asyncio.get_event_loop() no longer auto-creates
    # a loop on the main thread.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    main()
