"""
Cloudflare DNS Manager — Telegram Bot
-------------------------------------
Long-polling Telegram bot for managing Cloudflare DNS records from Telegram.

Supported flows:
  /cname   Create a CNAME (subdomain -> zone -> target -> proxy choice)
  /list    Browse a zone, inspect a record, toggle proxy, delete
  /delete  Pick a zone, pick a record, confirm, delete
  /cancel  Abort the /cname conversation

Includes a lightweight HTTP health endpoint so the process stays alive on
Render free tier (paired with UptimeRobot).

Compatible with Python 3.11 – 3.14 and python-telegram-bot >= 21.6
"""

import asyncio
import html
import json
import logging
import math
import os
import re
import secrets
from typing import Any

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
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
    int(part.strip())
    for part in os.environ.get("ALLOWED_CHAT_IDS", "").split(",")
    if part.strip()
}
PORT = int(os.environ.get("PORT", "8080"))

CF_API = "https://api.cloudflare.com/client/v4"
CF_HEADERS = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json",
}
CF_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)
NETWORK_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError)

PAGE_SIZE = 8
MAX_RECORDS = 1000

# Conversation states for /cname
CNAME_SUBDOMAIN, CNAME_ZONE, CNAME_TARGET, CNAME_PROXY = range(4)

LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

CNAME_KEYS = (
    "cname_token",
    "cname_zones",
    "cname_subdomain",
    "cname_zone",
    "cname_fqdn",
    "cname_target",
)

UI_KEYS = (
    "ui_token",
    "ui_zones",
    "ui_zone",
    "ui_records",
    "ui_page",
    "ui_mode",
    "ui_selected",
)

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
# Errors
# --------------------------------------------------------------------------- #
class CloudflareError(RuntimeError):
    """A user-presentable failure returned by the Cloudflare API."""


def format_cf_errors(errors: Any) -> str:
    """Turn Cloudflare's error array into a short, readable block."""
    if not isinstance(errors, list) or not errors:
        return "Cloudflare returned an unknown error."
    lines = []
    for error in errors[:5]:
        if not isinstance(error, dict):
            continue
        code = error.get("code", "?")
        message = error.get("message", "Unknown error")
        lines.append(f"• [{code}] {message}")
    return "\n".join(lines) or "Cloudflare returned an unknown error."


# --------------------------------------------------------------------------- #
# Cloudflare API
# --------------------------------------------------------------------------- #
async def cf_request(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Perform one Cloudflare API call and normalise failures.

    Always reads the body as text first: a 429/502/503 from Cloudflare may be
    HTML, and ``response.json()`` would raise a decoder error that hides the
    real HTTP status.
    """
    url = f"{CF_API}{path}"
    async with session.request(method, url, headers=CF_HEADERS, **kwargs) as response:
        body = await response.text()
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise CloudflareError(
                f"Cloudflare returned HTTP {response.status} with a non-JSON body."
            ) from exc
        if not isinstance(data, dict) or not data.get("success"):
            errors = data.get("errors") if isinstance(data, dict) else None
            raise CloudflareError(format_cf_errors(errors))
        return data


async def cf_get_zones(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    """List every zone visible to the token, following pagination."""
    zones: list[dict[str, Any]] = []
    page = 1
    while True:
        data = await cf_request(
            session,
            "GET",
            "/zones",
            params={"page": page, "per_page": 50},
        )
        zones.extend(data.get("result") or [])
        total_pages = int((data.get("result_info") or {}).get("total_pages") or 1)
        if page >= total_pages:
            return zones
        page += 1


async def cf_list_records(
    session: aiohttp.ClientSession,
    zone_id: str,
) -> list[dict[str, Any]]:
    """List records in a zone, following pagination up to MAX_RECORDS."""
    records: list[dict[str, Any]] = []
    page = 1
    while len(records) < MAX_RECORDS:
        data = await cf_request(
            session,
            "GET",
            f"/zones/{zone_id}/dns_records",
            params={
                "page": page,
                "per_page": 100,
                "order": "name",
                "direction": "asc",
            },
        )
        records.extend(data.get("result") or [])
        total_pages = int((data.get("result_info") or {}).get("total_pages") or 1)
        if page >= total_pages:
            break
        page += 1
    return records[:MAX_RECORDS]


async def cf_create_cname(
    session: aiohttp.ClientSession,
    zone_id: str,
    name: str,
    content: str,
    proxied: bool,
    ttl: int = 1,
) -> dict[str, Any]:
    data = await cf_request(
        session,
        "POST",
        f"/zones/{zone_id}/dns_records",
        json={
            "type": "CNAME",
            "name": name,
            "content": content,
            "proxied": proxied,
            "ttl": ttl,
        },
    )
    return data["result"]


async def cf_delete_record(
    session: aiohttp.ClientSession,
    zone_id: str,
    record_id: str,
) -> None:
    await cf_request(
        session,
        "DELETE",
        f"/zones/{zone_id}/dns_records/{record_id}",
    )


async def cf_set_proxied(
    session: aiohttp.ClientSession,
    zone_id: str,
    record_id: str,
    proxied: bool,
) -> dict[str, Any]:
    data = await cf_request(
        session,
        "PATCH",
        f"/zones/{zone_id}/dns_records/{record_id}",
        json={"proxied": proxied},
    )
    return data["result"]


# --------------------------------------------------------------------------- #
# Validation / formatting helpers
# --------------------------------------------------------------------------- #
def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def short(value: Any, limit: int = 48) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def humanize_ttl(ttl: Any) -> str:
    try:
        value = int(ttl)
    except (TypeError, ValueError):
        return "Auto"
    return "Auto" if value <= 1 else f"{value}s"


def is_valid_subdomain(value: str) -> bool:
    return bool(LABEL_RE.match(value))


def is_valid_hostname(value: str) -> bool:
    """Validate a CNAME target: 2+ labels, each 1–63 chars, no empty labels."""
    if not value or len(value) > 253 or ".." in value:
        return False
    labels = value.rstrip(".").split(".")
    return len(labels) >= 2 and all(LABEL_RE.match(label) for label in labels)


def new_token() -> str:
    """Short per-flow token so stale inline buttons cannot act on new state."""
    return secrets.token_hex(3)


def is_authorized(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.id in ALLOWED_CHAT_IDS)


def clear_cname(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in CNAME_KEYS:
        context.user_data.pop(key, None)


def clear_ui(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in UI_KEYS:
        context.user_data.pop(key, None)


async def safe_edit(query: CallbackQuery, text: str, **kwargs: Any) -> None:
    """Edit a message, ignoring Telegram's harmless 'message is not modified'."""
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def fetch_zones(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, Any]]:
    return await cf_get_zones(context.bot_data["http"])


async def fetch_records(
    context: ContextTypes.DEFAULT_TYPE,
    zone_id: str,
) -> list[dict[str, Any]]:
    return await cf_list_records(context.bot_data["http"], zone_id)


# --------------------------------------------------------------------------- #
# UI builders
# --------------------------------------------------------------------------- #
def zone_keyboard(
    zones: list[dict[str, Any]],
    prefix: str,
    token: str,
    cancel_data: str,
    cancel_label: str = "✖️ Close",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, zone in enumerate(zones):
        name = zone.get("name") or "—"
        status = zone.get("status")
        label = name if status in (None, "active") else f"{name} ({status})"
        rows.append(
            [
                InlineKeyboardButton(
                    short(label, 60),
                    callback_data=f"{prefix}:{token}:{index}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(cancel_label, callback_data=cancel_data)])
    return InlineKeyboardMarkup(rows)


def proxy_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🟠 Proxied — orange cloud",
                    callback_data=f"cproxy:{token}:yes",
                )
            ],
            [
                InlineKeyboardButton(
                    "⚪ DNS only — grey cloud",
                    callback_data=f"cproxy:{token}:no",
                )
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"ccancel:{token}")],
        ]
    )


def record_list_view(
    records: list[dict[str, Any]],
    page: int,
    mode: str,
    token: str,
) -> tuple[InlineKeyboardMarkup, int, int]:
    prefix = "l" if mode == "list" else "d"
    total_pages = max(1, math.ceil(len(records) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    visible = records[start : start + PAGE_SIZE]

    rows: list[list[InlineKeyboardButton]] = []
    for offset, record in enumerate(visible):
        index = start + offset
        record_type = record.get("type") or "?"
        name = record.get("name") or "—"
        content = record.get("content") or ""
        label = f"{record_type}  {short(name, 28)}"
        if record_type != "TXT" and content:
            label += f" → {short(content, 16)}"
        rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"{prefix}rec:{token}:{index}",
                )
            ]
        )

    if records:
        navigation: list[InlineKeyboardButton] = []
        if page > 0:
            navigation.append(
                InlineKeyboardButton(
                    "‹ Prev",
                    callback_data=f"{prefix}page:{token}:{page - 1}",
                )
            )
        navigation.append(
            InlineKeyboardButton(
                f"{page + 1} / {total_pages}",
                callback_data=f"{prefix}noop:{token}",
            )
        )
        if page < total_pages - 1:
            navigation.append(
                InlineKeyboardButton(
                    "Next ›",
                    callback_data=f"{prefix}page:{token}:{page + 1}",
                )
            )
        rows.append(navigation)

    rows.append(
        [
            InlineKeyboardButton(
                "🔄 Refresh",
                callback_data=f"{prefix}refresh:{token}",
            ),
            InlineKeyboardButton(
                "✖️ Close",
                callback_data=f"{prefix}close:{token}",
            ),
        ]
    )
    return InlineKeyboardMarkup(rows), page, total_pages


def record_list_text(
    zone: dict[str, Any],
    records: list[dict[str, Any]],
    page: int,
    mode: str,
) -> str:
    zone_name = zone.get("name") or "—"
    header = f"📋 <b>{esc(zone_name)}</b>"
    if len(records) >= MAX_RECORDS:
        header += f"\n<i>⚠️ Showing the first {MAX_RECORDS} records.</i>"
    if not records:
        return f"{header}\n\n📭 No DNS records found in this zone."

    start = page * PAGE_SIZE + 1
    end = min(len(records), (page + 1) * PAGE_SIZE)
    verb = "inspect" if mode == "list" else "delete"
    return (
        f"{header}\n"
        f"<i>{len(records)} record(s) · showing {start}–{end}</i>\n\n"
        f"Tap a record to {verb} it."
    )


def record_detail_text(record: dict[str, Any]) -> str:
    proxied = "🟠 Yes" if record.get("proxied") else "⚪ No"
    name = record.get("name") or "—"
    record_type = record.get("type") or "—"
    content = record.get("content") or "—"
    record_id = record.get("id") or "—"
    return (
        "🔎 <b>DNS record</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Name:</b> <code>{esc(name)}</code>\n"
        f"• <b>Type:</b> <code>{esc(record_type)}</code>\n"
        f"• <b>Content:</b> <code>{esc(short(content, 120))}</code>\n"
        f"• <b>Proxied:</b> {proxied}\n"
        f"• <b>TTL:</b> <code>{esc(humanize_ttl(record.get('ttl')))}</code>\n"
        f"• <b>ID:</b> <code>{esc(record_id)}</code>"
    )


def record_detail_keyboard(
    record: dict[str, Any],
    token: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if record.get("proxiable"):
        action = "Disable proxy" if record.get("proxied") else "Enable proxy"
        rows.append(
            [
                InlineKeyboardButton(
                    f"🟠 {action}",
                    callback_data=f"ltoggle:{token}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("🗑 Delete record", callback_data=f"ldel:{token}")]
    )
    rows.append(
        [InlineKeyboardButton("‹ Back to list", callback_data=f"lback:{token}")]
    )
    return InlineKeyboardMarkup(rows)


def confirm_text(record: dict[str, Any]) -> str:
    name = record.get("name") or "—"
    record_type = record.get("type") or "—"
    content = record.get("content") or "—"
    return (
        "🗑 <b>Delete this record?</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Name:</b> <code>{esc(name)}</code>\n"
        f"• <b>Type:</b> <code>{esc(record_type)}</code>\n"
        f"• <b>Content:</b> <code>{esc(short(content, 120))}</code>\n\n"
        "<i>This cannot be undone.</i>"
    )


def confirm_keyboard(prefix: str, token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Yes, delete",
                    callback_data=f"{prefix}confirm:{token}:yes",
                ),
                InlineKeyboardButton(
                    "↩️ No, go back",
                    callback_data=f"{prefix}confirm:{token}:no",
                ),
            ]
        ]
    )


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #
HELP_TEXT = (
    "🤖 <b>Cloudflare DNS Manager</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "Manage Cloudflare DNS from Telegram — no dashboard, no wasted data.\n\n"
    "<b>Commands</b>\n"
    "• /cname — Create a CNAME record\n"
    "• /list — Browse, inspect, and manage records\n"
    "• /delete — Delete a DNS record\n"
    "• /cancel — Cancel the current /cname flow\n"
    "• /help — Show this message\n\n"
    "💡 <i>Example:</i> create <code>mysub</code> and point it to "
    "<code>user.github.io</code>."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    clear_cname(context)
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update):
        return ConversationHandler.END
    clear_cname(context)
    await update.message.reply_text(
        "✅ Cancelled. Send /cname whenever you're ready."
    )
    return ConversationHandler.END


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_ui(update, context, "list", "🌐 <b>Pick a domain to browse:</b>")


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_ui(
        update,
        context,
        "delete",
        "🌐 <b>Pick a domain to delete from:</b>",
    )


async def _start_ui(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    mode: str,
    prompt: str,
) -> None:
    if not is_authorized(update):
        return

    try:
        zones = await fetch_zones(context)
    except CloudflareError as exc:
        await update.message.reply_text(
            f"❌ Could not load domains:\n{esc(exc)}",
            parse_mode=ParseMode.HTML,
        )
        return
    except NETWORK_ERRORS:
        logger.exception("Network error while listing zones")
        await update.message.reply_text(
            "❌ Could not reach Cloudflare. Try again in a moment."
        )
        return

    if not zones:
        await update.message.reply_text(
            "❌ No domains found in your Cloudflare account."
        )
        return

    clear_ui(context)
    token = new_token()
    context.user_data["ui_token"] = token
    context.user_data["ui_zones"] = zones
    context.user_data["ui_mode"] = mode

    prefix = "lzone" if mode == "list" else "dzone"
    close_data = f"lclose:{token}" if mode == "list" else f"dclose:{token}"
    await update.message.reply_text(
        prompt,
        reply_markup=zone_keyboard(zones, prefix, token, close_data),
        parse_mode=ParseMode.HTML,
    )


# --------------------------------------------------------------------------- #
# /cname conversation
# --------------------------------------------------------------------------- #
async def cmd_cname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update):
        return ConversationHandler.END

    clear_cname(context)
    context.user_data["cname_token"] = new_token()

    await update.message.reply_text(
        "📝 <b>Step 1/4</b> — Send the <b>subdomain</b> you want to create.\n\n"
        "Example: <code>mysub</code>\n\n"
        "<i>Send /cancel to abort.</i>",
        parse_mode=ParseMode.HTML,
    )
    return CNAME_SUBDOMAIN


async def handle_subdomain(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not is_authorized(update):
        return ConversationHandler.END

    subdomain = (update.message.text or "").strip().lower()
    if not is_valid_subdomain(subdomain):
        await update.message.reply_text(
            "❌ Invalid subdomain. Use lowercase letters, digits, and hyphens; "
            "it must start and end with a letter or digit.\n\nTry again:"
        )
        return CNAME_SUBDOMAIN

    context.user_data["cname_subdomain"] = subdomain
    token = context.user_data["cname_token"]

    try:
        zones = await fetch_zones(context)
    except CloudflareError as exc:
        clear_cname(context)
        await update.message.reply_text(
            f"❌ Could not load your domains:\n{esc(exc)}",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END
    except NETWORK_ERRORS:
        logger.exception("Network error while listing zones")
        clear_cname(context)
        await update.message.reply_text(
            "❌ Could not reach Cloudflare. Try again in a moment."
        )
        return ConversationHandler.END

    if not zones:
        clear_cname(context)
        await update.message.reply_text(
            "❌ No domains found in your Cloudflare account."
        )
        return ConversationHandler.END

    context.user_data["cname_zones"] = zones
    await update.message.reply_text(
        f"🌐 <b>Step 2/4</b> — Pick the domain for "
        f"<code>{esc(subdomain)}</code>:",
        reply_markup=zone_keyboard(
            zones,
            "czone",
            token,
            f"ccancel:{token}",
            cancel_label="❌ Cancel",
        ),
        parse_mode=ParseMode.HTML,
    )
    return CNAME_ZONE


async def handle_cname_zone(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    await query.answer()
    if not is_authorized(update):
        return ConversationHandler.END

    parts = query.data.split(":")
    if len(parts) != 3 or parts[1] != context.user_data.get("cname_token"):
        await query.edit_message_text("⚠️ This menu expired. Run /cname again.")
        clear_cname(context)
        return ConversationHandler.END

    try:
        index = int(parts[2])
        zone = context.user_data["cname_zones"][index]
    except (ValueError, KeyError, IndexError):
        await query.edit_message_text("⚠️ This menu expired. Run /cname again.")
        clear_cname(context)
        return ConversationHandler.END

    subdomain = context.user_data["cname_subdomain"]
    fqdn = f"{subdomain}.{zone['name']}"
    context.user_data["cname_zone"] = zone
    context.user_data["cname_fqdn"] = fqdn

    await query.edit_message_text(
        f"🎯 <b>Step 3/4</b> — Selected <code>{esc(fqdn)}</code>\n\n"
        "Now send the <b>target hostname</b>.\n"
        "Example: <code>user.github.io</code>\n"
        "<i>Do not include https:// or a trailing path.</i>",
        parse_mode=ParseMode.HTML,
    )
    return CNAME_TARGET


async def handle_target(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not is_authorized(update):
        return ConversationHandler.END

    raw = (update.message.text or "").strip().lower()
    target = re.sub(r"^https?://", "", raw).rstrip("/")
    if not is_valid_hostname(target):
        await update.message.reply_text(
            "❌ Invalid target. Send a hostname like "
            "<code>user.github.io</code>:",
            parse_mode=ParseMode.HTML,
        )
        return CNAME_TARGET

    context.user_data["cname_target"] = target
    token = context.user_data["cname_token"]

    await update.message.reply_text(
        "🟠 <b>Step 4/4</b> — Proxy this record through Cloudflare?\n\n"
        "• <b>Proxied</b> hides your origin and enables Cloudflare's CDN/WAF.\n"
        "• <b>DNS only</b> exposes the target directly.",
        reply_markup=proxy_keyboard(token),
        parse_mode=ParseMode.HTML,
    )
    return CNAME_PROXY


async def handle_cname_proxy(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    await query.answer()
    if not is_authorized(update):
        return ConversationHandler.END

    parts = query.data.split(":")
    if len(parts) != 3 or parts[1] != context.user_data.get("cname_token"):
        await query.edit_message_text("⚠️ This menu expired. Run /cname again.")
        clear_cname(context)
        return ConversationHandler.END

    choice = parts[2]
    if choice not in {"yes", "no"}:
        return CNAME_PROXY
    proxied = choice == "yes"

    zone = context.user_data.get("cname_zone")
    fqdn = context.user_data.get("cname_fqdn")
    target = context.user_data.get("cname_target")
    if not zone or not fqdn or not target:
        await query.edit_message_text("⚠️ Session expired. Run /cname again.")
        clear_cname(context)
        return ConversationHandler.END

    await query.edit_message_text("⏳ Creating the record…")

    try:
        record = await cf_create_cname(
            context.bot_data["http"],
            zone["id"],
            fqdn,
            target,
            proxied=proxied,
        )
    except CloudflareError as exc:
        await query.edit_message_text(
            f"❌ <b>Cloudflare rejected the request:</b>\n{esc(exc)}",
            parse_mode=ParseMode.HTML,
        )
        clear_cname(context)
        return ConversationHandler.END
    except NETWORK_ERRORS:
        logger.exception("Network error while creating a CNAME")
        await query.edit_message_text(
            "❌ Could not reach Cloudflare. Try again in a moment."
        )
        clear_cname(context)
        return ConversationHandler.END

    proxy_label = "🟠 Yes" if record.get("proxied") else "⚪ No"
    await query.edit_message_text(
        "✅ <b>DNS record created successfully!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Domain:</b> <code>{esc(record.get('name') or fqdn)}</code>\n"
        f"• <b>Type:</b> <code>{esc(record.get('type') or 'CNAME')}</code>\n"
        f"• <b>Target:</b> <code>{esc(record.get('content') or target)}</code>\n"
        f"• <b>Proxied:</b> {proxy_label}\n"
        f"• <b>TTL:</b> <code>{esc(humanize_ttl(record.get('ttl')))}</code>\n\n"
        "<i>Propagation may take a few minutes.</i>",
        parse_mode=ParseMode.HTML,
    )
    logger.info(
        "Created %s %s -> %s (proxied=%s)",
        record.get("type"),
        fqdn,
        target,
        proxied,
    )
    clear_cname(context)
    return ConversationHandler.END


async def handle_cname_cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    await query.answer()
    if not is_authorized(update):
        return ConversationHandler.END

    parts = query.data.split(":")
    if len(parts) != 2 or parts[1] != context.user_data.get("cname_token"):
        await query.edit_message_text("⚠️ This menu expired. Run /cname again.")
        return ConversationHandler.END

    clear_cname(context)
    await query.edit_message_text("❌ Cancelled.")
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# /list and /delete callbacks
# --------------------------------------------------------------------------- #
async def render_ui_list(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    zone = context.user_data.get("ui_zone")
    records = context.user_data.get("ui_records") or []
    mode = context.user_data.get("ui_mode", "list")
    token = context.user_data.get("ui_token") or ""
    if not zone or not token:
        await safe_edit(query, "⚠️ This menu expired. Run the command again.")
        return

    keyboard, page, _ = record_list_view(
        records,
        context.user_data.get("ui_page", 0),
        mode,
        token,
    )
    context.user_data["ui_page"] = page
    await safe_edit(
        query,
        record_list_text(zone, records, page, mode),
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def ui_zone_chosen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()
    if not is_authorized(update):
        return

    parts = query.data.split(":")
    if len(parts) != 3 or parts[1] != context.user_data.get("ui_token"):
        await safe_edit(query, "⚠️ This menu expired. Run the command again.")
        return

    prefix, _, raw_index = parts
    mode = "list" if prefix == "lzone" else "delete"

    try:
        index = int(raw_index)
        zone = context.user_data["ui_zones"][index]
    except (ValueError, KeyError, IndexError):
        await safe_edit(query, "⚠️ This menu expired. Run the command again.")
        return

    await safe_edit(query, "⏳ Loading records…")
    try:
        records = await fetch_records(context, zone["id"])
    except CloudflareError as exc:
        await safe_edit(
            query,
            f"❌ Could not load records:\n{esc(exc)}",
            parse_mode=ParseMode.HTML,
        )
        return
    except NETWORK_ERRORS:
        logger.exception("Network error while listing records")
        await safe_edit(
            query,
            "❌ Could not reach Cloudflare. Try again in a moment.",
        )
        return

    context.user_data["ui_mode"] = mode
    context.user_data["ui_zone"] = zone
    context.user_data["ui_records"] = records
    context.user_data["ui_page"] = 0
    context.user_data.pop("ui_selected", None)

    await render_ui_list(query, context)


async def ui_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_authorized(update):
        return

    parts = query.data.split(":")
    if len(parts) != 3 or parts[1] != context.user_data.get("ui_token"):
        await safe_edit(query, "⚠️ This menu expired. Run the command again.")
        return

    try:
        context.user_data["ui_page"] = int(parts[2])
    except ValueError:
        return
    await render_ui_list(query, context)


async def ui_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


async def ui_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_authorized(update):
        return

    parts = query.data.split(":")
    if len(parts) != 2 or parts[1] != context.user_data.get("ui_token"):
        await safe_edit(query, "⚠️ This menu expired. Run the command again.")
        return

    zone = context.user_data.get("ui_zone")
    if not zone:
        await safe_edit(query, "⚠️ This menu expired. Run the command again.")
        return

    try:
        records = await fetch_records(context, zone["id"])
    except CloudflareError as exc:
        await safe_edit(
            query,
            f"❌ Could not refresh:\n{esc(exc)}",
            parse_mode=ParseMode.HTML,
        )
        return
    except NETWORK_ERRORS:
        logger.exception("Network error while refreshing records")
        await safe_edit(
            query,
            "❌ Could not reach Cloudflare. Try again in a moment.",
        )
        return

    context.user_data["ui_records"] = records
    await render_ui_list(query, context)


async def ui_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_authorized(update):
        return
    clear_ui(context)
    await safe_edit(query, "✖️ Closed.")


async def ui_record(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_authorized(update):
        return

    parts = query.data.split(":")
    if len(parts) != 3 or parts[1] != context.user_data.get("ui_token"):
        await safe_edit(query, "⚠️ This menu expired. Run the command again.")
        return

    try:
        index = int(parts[2])
        record = context.user_data["ui_records"][index]
    except (ValueError, KeyError, IndexError):
        await safe_edit(query, "⚠️ This menu expired. Run the command again.")
        return

    context.user_data["ui_selected"] = index
    token = context.user_data["ui_token"]

    if parts[0] == "drec":
        await safe_edit(
            query,
            confirm_text(record),
            reply_markup=confirm_keyboard("d", token),
            parse_mode=ParseMode.HTML,
        )
    else:
        await safe_edit(
            query,
            record_detail_text(record),
            reply_markup=record_detail_keyboard(record, token),
            parse_mode=ParseMode.HTML,
        )


async def ui_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_authorized(update):
        return

    parts = query.data.split(":")
    if len(parts) != 2 or parts[1] != context.user_data.get("ui_token"):
        await safe_edit(query, "⚠️ This menu expired. Run /list again.")
        return

    index = context.user_data.get("ui_selected")
    records = context.user_data.get("ui_records") or []
    zone = context.user_data.get("ui_zone")
    if index is None or not zone or index >= len(records):
        await safe_edit(query, "⚠️ This menu expired. Run /list again.")
        return

    record = records[index]
    target_proxied = not bool(record.get("proxied"))
    try:
        updated = await cf_set_proxied(
            context.bot_data["http"],
            zone["id"],
            record["id"],
            target_proxied,
        )
    except CloudflareError as exc:
        await safe_edit(
            query,
            f"❌ Could not update proxy:\n{esc(exc)}",
            reply_markup=record_detail_keyboard(
                record,
                context.user_data["ui_token"],
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    except NETWORK_ERRORS:
        logger.exception("Network error while toggling proxy")
        await safe_edit(
            query,
            "❌ Could not reach Cloudflare. Try again in a moment.",
        )
        return

    records[index] = updated
    context.user_data["ui_records"] = records
    await safe_edit(
        query,
        record_detail_text(updated),
        reply_markup=record_detail_keyboard(
            updated,
            context.user_data["ui_token"],
        ),
        parse_mode=ParseMode.HTML,
    )


async def ui_delete_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()
    if not is_authorized(update):
        return

    parts = query.data.split(":")
    if len(parts) != 2 or parts[1] != context.user_data.get("ui_token"):
        await safe_edit(query, "⚠️ This menu expired. Run /list again.")
        return

    index = context.user_data.get("ui_selected")
    records = context.user_data.get("ui_records") or []
    if index is None or index >= len(records):
        await safe_edit(query, "⚠️ This menu expired. Run /list again.")
        return

    await safe_edit(
        query,
        confirm_text(records[index]),
        reply_markup=confirm_keyboard("l", context.user_data["ui_token"]),
        parse_mode=ParseMode.HTML,
    )


async def ui_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_authorized(update):
        return

    parts = query.data.split(":")
    if len(parts) != 2 or parts[1] != context.user_data.get("ui_token"):
        await safe_edit(query, "⚠️ This menu expired. Run the command again.")
        return

    await render_ui_list(query, context)


async def ui_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_authorized(update):
        return

    parts = query.data.split(":")
    if len(parts) != 3 or parts[1] != context.user_data.get("ui_token"):
        await safe_edit(query, "⚠️ This menu expired. Run the command again.")
        return

    if parts[2] != "yes":
        await render_ui_list(query, context)
        return

    index = context.user_data.get("ui_selected")
    records = context.user_data.get("ui_records") or []
    zone = context.user_data.get("ui_zone")
    if index is None or not zone or index >= len(records):
        await safe_edit(query, "⚠️ This menu expired. Run the command again.")
        return

    record = records[index]
    await safe_edit(query, "⏳ Deleting…")

    try:
        await cf_delete_record(
            context.bot_data["http"],
            zone["id"],
            record["id"],
        )
    except CloudflareError as exc:
        await safe_edit(
            query,
            f"❌ Could not delete:\n{esc(exc)}",
            parse_mode=ParseMode.HTML,
        )
        return
    except NETWORK_ERRORS:
        logger.exception("Network error while deleting a record")
        await safe_edit(
            query,
            "❌ Could not reach Cloudflare. Try again in a moment.",
        )
        return

    logger.info(
        "Deleted %s %s (%s)",
        record.get("type"),
        record.get("name"),
        record.get("id"),
    )

    try:
        records = await fetch_records(context, zone["id"])
    except (CloudflareError, *NETWORK_ERRORS):
        records = [
            item for item in records if item.get("id") != record.get("id")
        ]

    context.user_data["ui_records"] = records
    context.user_data.pop("ui_selected", None)

    page = context.user_data.get("ui_page", 0)
    total_pages = max(1, math.ceil(len(records) / PAGE_SIZE))
    context.user_data["ui_page"] = min(page, total_pages - 1)

    await render_ui_list(query, context)


async def handle_unknown_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_authorized(update):
        return
    await update.message.reply_text("🤔 Unknown command. Try /help.")


# --------------------------------------------------------------------------- #
# Health endpoint (for Render / UptimeRobot)
# --------------------------------------------------------------------------- #
async def _health_handler(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "cf-telegram-bot"})


async def start_health_server(application: Application) -> None:
    app = web.Application()
    for method in ("GET", "HEAD"):
        app.router.add_route(method, "/", _health_handler)
        app.router.add_route(method, "/health", _health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    application.bot_data["health_runner"] = runner
    logger.info("Health server listening on port %s", PORT)


# --------------------------------------------------------------------------- #
# App bootstrap
# --------------------------------------------------------------------------- #
async def post_init(application: Application) -> None:
    application.bot_data["http"] = aiohttp.ClientSession(timeout=CF_TIMEOUT)
    await application.bot.set_my_commands(
        [
            ("cname", "Create a CNAME record"),
            ("list", "Browse DNS records"),
            ("delete", "Delete a DNS record"),
            ("cancel", "Cancel the current /cname flow"),
            ("help", "Show help"),
            ("start", "Show help"),
        ]
    )
    await start_health_server(application)


async def post_shutdown(application: Application) -> None:
    session = application.bot_data.pop("http", None)
    if session is not None:
        await session.close()
    runner = application.bot_data.pop("health_runner", None)
    if runner is not None:
        await runner.cleanup()


def build_application() -> Application:
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    cname_conversation = ConversationHandler(
        entry_points=[CommandHandler("cname", cmd_cname)],
        states={
            CNAME_SUBDOMAIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_subdomain)
            ],
            CNAME_ZONE: [
                CallbackQueryHandler(handle_cname_zone, pattern=r"^czone:"),
                CallbackQueryHandler(handle_cname_cancel, pattern=r"^ccancel:"),
            ],
            CNAME_TARGET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_target)
            ],
            CNAME_PROXY: [
                CallbackQueryHandler(handle_cname_proxy, pattern=r"^cproxy:"),
                CallbackQueryHandler(handle_cname_cancel, pattern=r"^ccancel:"),
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

    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("delete", cmd_delete))
    application.add_handler(cname_conversation)
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_start))
    application.add_handler(CommandHandler("cancel", cmd_cancel))

    application.add_handler(
        CallbackQueryHandler(ui_zone_chosen, pattern=r"^(lzone|dzone):")
    )
    application.add_handler(
        CallbackQueryHandler(ui_page, pattern=r"^(lpage|dpage):")
    )
    application.add_handler(
        CallbackQueryHandler(ui_noop, pattern=r"^(lnoop|dnoop):")
    )
    application.add_handler(
        CallbackQueryHandler(ui_refresh, pattern=r"^(lrefresh|drefresh):")
    )
    application.add_handler(
        CallbackQueryHandler(ui_close, pattern=r"^(lclose|dclose):")
    )
    application.add_handler(
        CallbackQueryHandler(ui_record, pattern=r"^(lrec|drec):")
    )
    application.add_handler(
        CallbackQueryHandler(ui_toggle, pattern=r"^ltoggle:")
    )
    application.add_handler(
        CallbackQueryHandler(ui_delete_prompt, pattern=r"^ldel:")
    )
    application.add_handler(
        CallbackQueryHandler(ui_back, pattern=r"^(lback|dback):")
    )
    application.add_handler(
        CallbackQueryHandler(ui_confirm, pattern=r"^(lconfirm|dconfirm):")
    )

    application.add_handler(
        MessageHandler(filters.COMMAND, handle_unknown_command)
    )
    return application


def main() -> None:
    logger.info("Starting Cloudflare DNS Telegram bot (long polling)…")
    application = build_application()
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    # Explicit loop creation for Python 3.14+ where get_event_loop() no longer
    # auto-creates a loop on the main thread.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    main()
