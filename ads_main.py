"""
@UFFAdsBot - the Advertisement Management Bot for @Uff_GameBot.

Handles: advertisers submitting ad requests, owner/admin approval,
payment proof upload and confirmation, advertisement rates, and the
owner commands for browsing/searching/broadcasting to applications.

Shares its PostgreSQL database with @Uff_GameBot (same DATABASE_URL) via
ads_database.py - see that file's docstring for what's shared vs owned.
"""

import os
import re
import json
import asyncio
import html as html_lib
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.error import TimedOut, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

import ads_database as db

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def safe_call(coro_func, *args, retries=2, delay=2, **kwargs):
    """Retries a Telegram API call on transient timeouts/network
    hiccups instead of letting one bad request silently drop a step of
    the advertisement submission conversation (or any other reply) -
    mirrors @Uff_GameBot's safe_send() helper. Returns None (and logs)
    if every attempt fails; callers that don't need the result can
    just await it and ignore the return value."""
    for attempt in range(retries + 1):
        try:
            return await coro_func(*args, **kwargs)
        except (TimedOut, NetworkError) as e:
            if attempt == retries:
                logger.error(f"Telegram API call failed after retries: {e}")
                return None
            await asyncio.sleep(delay)


# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------

BOT_TOKEN = os.environ.get("ADS_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "ADS_BOT_TOKEN is not set. Set it in your environment (or .env file) "
        "to the token @BotFather gave this bot (must be different from "
        "@Uff_GameBot's token - they're two separate bots)."
    )

_owner_id_raw = os.environ.get("OWNER_ID")
if not _owner_id_raw:
    raise RuntimeError("OWNER_ID is not set - same value as @Uff_GameBot's OWNER_ID.")
try:
    OWNER_ID = int(_owner_id_raw)
except ValueError:
    raise RuntimeError(f"OWNER_ID must be a number, got: {_owner_id_raw!r}")

OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "")  # for the Contact Support button


def is_owner(user_id) -> bool:
    return user_id == OWNER_ID


def is_owner_or_admin(user_id) -> bool:
    return is_owner(user_id) or db.is_bot_admin(user_id)


def esc(text) -> str:
    """HTML-escape user-supplied text before it goes into a
    parse_mode='HTML' message - same rationale/pattern as @Uff_GameBot's
    esc() helper: unescaped '<'/'>'/'&' breaks Telegram's HTML parser."""
    if text is None:
        return ""
    return html_lib.escape(str(text), quote=False)


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not is_owner(user.id):
            if update.message:
                await update.message.reply_text("🚫 This command is restricted to the bot owner.")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


def owner_or_admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not is_owner_or_admin(user.id):
            if update.message:
                await update.message.reply_text("🚫 This command is restricted to the bot owner or bot admins.")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


# ---------------------------------------------------------------------
# Currency display helper (used by rate commands and the duration menu)
# ---------------------------------------------------------------------

CURRENCY_SYMBOLS = {"NGN": "₦", "USD": "$", "GBP": "£", "EUR": "€"}


def format_price(price, currency) -> str:
    currency = (currency or "NGN").upper()
    symbol = CURRENCY_SYMBOLS.get(currency)
    if symbol:
        return f"{symbol}{price}"
    return f"{price} {currency}"


# ---------------------------------------------------------------------
# Conversation states (the "Submit Advertisement" flow)
# ---------------------------------------------------------------------

(ASK_BUSINESS, ASK_LINK, ASK_TYPE, ASK_DURATION, ASK_TEXT, ASK_NOTES, PREVIEW) = range(7)

AD_TYPES = ["Group", "Channel", "Bot", "Business", "Website", "Other"]


def type_keyboard():
    row = [InlineKeyboardButton(t, callback_data=f"adtype:{t}") for t in AD_TYPES[:3]]
    row2 = [InlineKeyboardButton(t, callback_data=f"adtype:{t}") for t in AD_TYPES[3:]]
    return InlineKeyboardMarkup([row, row2])


def duration_keyboard():
    """Built live from whatever /setrate has configured - any duration
    is allowed, not just a fixed list, and a newly added/changed/removed
    rate is reflected immediately without a restart."""
    rows = db.get_rates_with_currency()
    keyboard = []
    for days, price, currency in rows:
        label = f"{days} Day" + ("s" if days != 1 else "") + f" - {format_price(price, currency)}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"adduration:{days}")])
    return InlineKeyboardMarkup(keyboard)


def preview_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Submit", callback_data="adpreview:submit")],
        [InlineKeyboardButton("✏ Edit", callback_data="adpreview:edit")],
        [InlineKeyboardButton("❌ Cancel", callback_data="adpreview:cancel")],
    ])


def preview_text(data: dict) -> str:
    return f"""
📋 <b>Advertisement Preview</b>

<b>Business:</b>
{esc(data.get('business_name'))}

<b>Link:</b>
{esc(data.get('ad_link'))}

<b>Type:</b>
{esc(data.get('ad_type'))}

<b>Duration:</b>
{data.get('duration_days')} Days

<b>Advertisement Text:</b>
{esc(data.get('ad_text'))}

<b>Additional Notes:</b>
{esc(data.get('notes')) or 'None'}
"""


# ---------------------------------------------------------------------
# /start and the main menu
# ---------------------------------------------------------------------

def _support_contact_url():
    """The Contact Support button's destination - a DB-backed override
    set via /changecontactsupport, falling back to the OWNER_USERNAME
    env var if nothing's been configured yet. Accepts either a bare
    @username or a full link."""
    contact = db.get_config("support_contact", OWNER_USERNAME)
    if not contact:
        return None
    contact = contact.strip()
    if contact.startswith("http://") or contact.startswith("https://"):
        return contact
    return f"https://t.me/{contact.lstrip('@')}"


def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("📝 Submit Advertisement", callback_data="menu:submit")],
        [InlineKeyboardButton("📋 Advertisement Rates", callback_data="menu:rates")],
        [InlineKeyboardButton("❓ Help", callback_data="menu:help")],
    ]
    contact_url = _support_contact_url()
    if contact_url:
        keyboard.append([InlineKeyboardButton("👤 Contact Support", url=contact_url)])
    return InlineKeyboardMarkup(keyboard)


@owner_only
async def changecontactsupport_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /changecontactsupport <@username or link>
    Persists to bot_config (survives restarts) and takes effect
    immediately everywhere the Contact Support button is shown. Safe to
    re-run any time - it updates the same setting rather than creating
    a new one."""
    if not context.args:
        current = db.get_config("support_contact", OWNER_USERNAME) or "(not set)"
        await update.message.reply_text(
            "Usage: /changecontactsupport <@username or link>\n"
            f"Current: {current}"
        )
        return

    value = " ".join(context.args).strip()
    db.set_config("support_contact", value)
    db.audit(update.effective_user.id, "change_contact_support", value)
    await update.message.reply_text(f"✅ Contact/support updated to: {value}")


WELCOME_TEXT = """
📢 <b>Welcome to the UFF Advertisement Center!</b>

Promote your:
• Telegram Groups
• Telegram Channels
• Telegram Bots
• Businesses
• Products
• Services
• Websites

Tap the button below to submit your advertisement request.
"""

HELP_TEXT = """
❓ <b>How This Works</b>

@UFFAdsBot lets you submit an advertisement for review, pay once approved, and go live.

<b>Steps:</b>
1️⃣ Tap 📝 Submit Advertisement, or run /submitad
2️⃣ Answer a few short questions (examples below)
3️⃣ Review your submission and confirm it
4️⃣ Wait for a decision - you'll be notified either way
5️⃣ If approved, pay using the instructions you're given, then send your payment proof (screenshot, photo, or PDF) right here in this chat
6️⃣ Once your payment is verified, your ad goes live

<b>What you'll be asked, with examples:</b>

📎 <b>Link or username</b> - what you're promoting
<i>Example: https://t.me/YourGroup or @YourChannel</i>

📁 <b>Type</b> - choose from Group, Channel, Bot, Business, Website, Other

🗓 <b>Duration</b> - pick how many days you want to advertise

✍️ <b>Advertisement text</b> - the actual words people will see
<i>Example: "Join our community for daily deals and giveaways!"</i>

📝 <b>Additional notes</b> (optional) - anything extra for the review team, or send /skip if there's nothing to add

<b>Other commands</b>
/submitad - start a new application
/myads - see your current/past ads and spending
/receipts - see your past invoices
/cancel - cancel an application in progress

⚠️ You can only have <b>one</b> application pending review at a time - wait for a decision before submitting another.

Use <b>Advertisement Rates</b> any time to see current prices.
"""


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /start, including the ?start=submitad deep link used by
    @Uff_GameBot's ad footer ("Place your links here 👉") - that link
    opens this bot with a submitad payload, which drops the tapper
    straight into the existing Submit Advertisement flow instead of
    the welcome menu. Registered as a submit_conversation entry point
    (not a separate CommandHandler) so the resulting ASK_LINK state is
    actually tracked - a bare /start still shows the normal welcome
    menu and ends any conversation tracking."""
    if context.args and context.args[0] == "submitad":
        return await submit_entry(update, context)

    await update.message.reply_html(WELCOME_TEXT, reply_markup=main_menu_keyboard())
    return ConversationHandler.END


def rates_text() -> str:
    rows = db.get_rates_with_currency()
    if not rows:
        return "📋 <b>Advertisement Rates</b>\n\nRates haven't been configured yet - check back soon."
    lines = ["📋 <b>Advertisement Rates</b>\n"]
    for days, price, currency in rows:
        lines.append(f"{days} Day{'s' if days != 1 else ''} = {format_price(price, currency)}")
    return "\n".join(lines)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Standalone /help - same content as the ❓ Help button on /start,
    just reachable directly as a command too."""
    await update.message.reply_html(HELP_TEXT, reply_markup=main_menu_keyboard())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start menu buttons that AREN'T part of the submit
    conversation (rates, help) - the submit button is handled by the
    ConversationHandler's own entry point below."""
    query = update.callback_query
    await query.answer()

    if query.data == "menu:rates":
        await query.edit_message_text(rates_text(), parse_mode="HTML", reply_markup=main_menu_keyboard())
    elif query.data == "menu:help":
        await query.edit_message_text(HELP_TEXT, parse_mode="HTML", reply_markup=main_menu_keyboard())


@owner_only
async def setrates_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy single-currency (NGN) rate setter - kept for backward
    compatibility. Prefer /setrate <days> <amount> [currency], which
    also supports other currencies and shows in /rates."""
    if not context.args:
        current = "\n".join(f"{d} Day{'s' if d != 1 else ''} = {p}" for d, p in db.get_rates())
        await update.message.reply_text(
            "Usage: /setrates <duration_days> <price>\n"
            "Example: /setrates 7 2500\n\n"
            f"Current rates:\n{current}"
        )
        return

    if len(context.args) != 2:
        await update.message.reply_text("Usage: /setrates <duration_days> <price>\nExample: /setrates 7 2500")
        return

    try:
        days = int(context.args[0])
        price = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Both duration_days and price must be whole numbers.")
        return

    if days <= 0 or price < 0:
        await update.message.reply_text("Duration must be positive and price can't be negative.")
        return

    db.set_rate(days, price)
    await update.message.reply_text(f"✅ Rate updated: {days} Day{'s' if days != 1 else ''} = ₦{price}")


@owner_only
async def setrate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /setrate <duration_days> <amount> [currency]
    Creates a new rate, or updates the existing one for that duration
    if it already exists. Any duration is allowed - not limited to a
    fixed list. currency defaults to NGN if omitted."""
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /setrate <duration_days> <amount> [currency]\n"
            "Examples:\n/setrate 1 500 NGN\n/setrate 7 10 USD\n/setrate 30 25000\n"
            "(currency defaults to NGN if omitted)"
        )
        return

    try:
        days = int(context.args[0])
        price = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Duration and amount must be whole numbers.")
        return

    if days <= 0 or price < 0:
        await update.message.reply_text("Duration must be positive and amount can't be negative.")
        return

    currency = context.args[2].upper() if len(context.args) >= 3 else "NGN"

    db.set_rate(days, price, currency)
    db.audit(update.effective_user.id, "set_rate", f"{days}d = {price} {currency}")

    await update.message.reply_text(
        f"✅ Rate saved: {days} Day{'s' if days != 1 else ''} = {format_price(price, currency)}\n"
        "This is now live on the advertisement submission menu."
    )


@owner_only
async def deleterate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /deleterate <duration_days>"""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /deleterate <duration_days>\nExample: /deleterate 14")
        return

    days = int(context.args[0])
    removed = db.delete_rate(days)
    db.audit(update.effective_user.id, "delete_rate", str(days))

    if removed:
        await update.message.reply_text(f"✅ Rate for {days} Day{'s' if days != 1 else ''} deleted.")
    else:
        await update.message.reply_text(f"❌ No rate configured for {days} Day{'s' if days != 1 else ''}.")


@owner_only
async def rates_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /rates - lists every currently configured advertisement
    rate (with currency), regardless of what the public-facing 'menu:rates'
    button on /start shows."""
    rows = db.get_rates_with_currency()
    if not rows:
        await update.message.reply_text("No advertisement rates configured yet. Use /setrate to add one.")
        return

    lines = ["📋 <b>Advertisement Rates</b>\n"]
    for days, price, currency in rows:
        lines.append(f"{days} Day{'s' if days != 1 else ''} — {format_price(price, currency)}")

    await update.message.reply_html("\n".join(lines))


# ---------------------------------------------------------------------
# Submit Advertisement conversation
# ---------------------------------------------------------------------

async def submit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point - reachable both from /submitad and from the 📝
    Submit Advertisement button on /start."""
    query = update.callback_query
    user = update.effective_user

    if query:
        await query.answer()

    if db.has_pending_application(user.id):
        text = (
            "⚠️ You already have an advertisement application awaiting a decision. "
            "Please wait for that one to be reviewed before submitting another."
        )
        if query:
            await safe_call(query.edit_message_text, text)
        else:
            await safe_call(update.message.reply_text, text)
        return ConversationHandler.END

    context.user_data["ad_draft"] = {}

    text = "📝 Let's get your advertisement set up.\n\nSend the link or username of what you want to advertise"
    if query:
        await safe_call(query.edit_message_text, text, parse_mode="HTML")
    else:
        await safe_call(update.message.reply_html, text)

    return ASK_LINK


async def ask_business(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await safe_call(update.message.reply_text, "Please send a business/project name.")
        return ASK_BUSINESS

    context.user_data["ad_draft"]["business_name"] = text[:200]
    await safe_call(update.message.reply_text, "Great! Send the link or username of what you want to advertise.")
    return ASK_LINK


async def ask_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await safe_call(update.message.reply_text, "Please send a Telegram link or username.")
        return ASK_LINK

    context.user_data["ad_draft"]["ad_link"] = text[:300]
    await safe_call(
        update.message.reply_text,
        "What type of advertisement is this?\n\nChoose from any of the buttons below 👇",
        reply_markup=type_keyboard(),
    )
    return ASK_TYPE


async def ask_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    ad_type = query.data.split(":", 1)[1]
    context.user_data["ad_draft"]["ad_type"] = ad_type

    await safe_call(
        query.edit_message_text,
        f"Type: {esc(ad_type)}\n\nHow long would you like to advertise for?\n\nChoose from any of the buttons below 👇",
        parse_mode="HTML",
        reply_markup=duration_keyboard(),
    )
    return ASK_DURATION


async def ask_duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    days = int(query.data.split(":", 1)[1])
    context.user_data["ad_draft"]["duration_days"] = days

    await safe_call(
        query.edit_message_text,
        f"Duration: {days} Days\n\nNow send me your advertisement text (this is what people will see).",
    )
    return ASK_TEXT


async def ask_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await safe_call(update.message.reply_text, "Please send your advertisement text.")
        return ASK_TEXT

    context.user_data["ad_draft"]["ad_text"] = text[:1000]
    await safe_call(
        update.message.reply_text,
        "Any additional notes for the review team? Send them now, or send /skip if there's nothing to add.",
    )
    return ASK_NOTES


async def ask_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["ad_draft"]["notes"] = text[:500]
    return await show_preview(update, context)


async def skip_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ad_draft"]["notes"] = ""
    return await show_preview(update, context)


async def show_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data["ad_draft"]
    await safe_call(update.message.reply_html, preview_text(data), reply_markup=preview_keyboard())
    return PREVIEW


async def preview_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    user = update.effective_user

    if action == "cancel":
        context.user_data.pop("ad_draft", None)
        await safe_call(query.edit_message_text, "❌ Advertisement submission cancelled.")
        return ConversationHandler.END

    if action == "edit":
        context.user_data["ad_draft"] = {}
        await safe_call(
            query.edit_message_text,
            "Let's redo it. What's your <b>Business/Project Name</b>?",
            parse_mode="HTML",
        )
        return ASK_BUSINESS

    # action == "submit"
    data = context.user_data["ad_draft"]
    application_id = db.create_application(
        applicant_id=user.id,
        applicant_name=user.first_name,
        applicant_username=user.username,
        business_name=data.get("business_name"),
        ad_link=data.get("ad_link"),
        ad_type=data.get("ad_type"),
        duration_days=data.get("duration_days"),
        ad_text=data.get("ad_text"),
        notes=data.get("notes"),
    )
    context.user_data.pop("ad_draft", None)

    await safe_call(
        query.edit_message_text,
        "✅ Your advertisement has been submitted for review. "
        "You'll hear back here as soon as a decision is made.",
    )

    await notify_owner_new_application(context, application_id)

    return ConversationHandler.END


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("ad_draft", None)
    await safe_call(update.message.reply_text, "❌ Advertisement submission cancelled.")
    return ConversationHandler.END


submit_conversation = ConversationHandler(
    entry_points=[
        CommandHandler("start", start_command),
        CommandHandler("submitad", submit_entry),
        CallbackQueryHandler(submit_entry, pattern="^menu:submit$"),
    ],
    states={
        ASK_BUSINESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_business)],
        ASK_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_link)],
        ASK_TYPE: [CallbackQueryHandler(ask_type_callback, pattern="^adtype:")],
        ASK_DURATION: [CallbackQueryHandler(ask_duration_callback, pattern="^adduration:")],
        ASK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_text)],
        ASK_NOTES: [
            CommandHandler("skip", skip_notes),
            MessageHandler(filters.TEXT & ~filters.COMMAND, ask_notes),
        ],
        PREVIEW: [CallbackQueryHandler(preview_callback, pattern="^adpreview:")],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_command),
        # "start" is also a fallback (not just an entry_point) so it
        # works even when a user is stuck mid-conversation (e.g. they
        # abandoned the form partway and come back later) - without
        # this, /start was unreachable once someone was inside any
        # ASK_* state, since fallbacks (not entry_points) are what get
        # checked for a currently-active conversation.
        CommandHandler("start", start_command),
    ],
    name="submit_ad_conversation",
    per_message=False,
)


# ---------------------------------------------------------------------
# Owner notification + approve/reject
# ---------------------------------------------------------------------

def application_detail_text(app_row) -> str:
    (aid, applicant_id, applicant_name, applicant_username, business_name,
     ad_link, ad_type, duration_days, ad_text, notes, status,
     reject_reason, submitted_at, decided_at, decided_by,
     coupon_code, final_price) = app_row

    uname = f"@{applicant_username}" if applicant_username else "—"

    try:
        submitted_display = datetime.fromisoformat(submitted_at).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        submitted_display = submitted_at or "—"

    return f"""
📢 <b>Advertisement Request #{aid}</b>

<b>Applicant:</b>
{esc(applicant_name)}

<b>Username:</b>
{esc(uname)}

<b>Telegram ID:</b>
<code>{applicant_id}</code>

<b>Business:</b>
{esc(business_name)}

<b>Type:</b>
{esc(ad_type)}

<b>Duration:</b>
{duration_days} Days

<b>Link:</b>
{esc(ad_link)}

<b>Advertisement Text:</b>
{esc(ad_text)}

<b>Additional Notes:</b>
{esc(notes) or 'None'}

<b>Status:</b> {esc(status)}
{f"<b>Coupon Applied:</b> {esc(coupon_code)} (Final Price: ₦{final_price})" + chr(10) if coupon_code else ""}
<b>Submitted:</b>
{submitted_display}
"""


def decision_keyboard(application_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"addecide:approve:{application_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"addecide:reject:{application_id}"),
        ],
        [InlineKeyboardButton("💬 Contact Advertiser", callback_data=f"adcontact:{application_id}")],
    ])


async def notify_owner_new_application(context: ContextTypes.DEFAULT_TYPE, application_id):
    app_row = db.get_application(application_id)
    if not app_row:
        return
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=application_detail_text(app_row),
            parse_mode="HTML",
            reply_markup=decision_keyboard(application_id),
        )
    except Exception as e:
        logger.error(f"Failed to notify owner of new application {application_id}: {e}")


def format_payment_method_instructions(name, config_json, price, currency="NGN") -> str:
    try:
        config = json.loads(config_json) if config_json else {}
    except json.JSONDecodeError:
        config = {}

    lines = [f"💳 <b>{esc(name)}</b>", f"Amount: {format_price(price, currency)}", ""]
    if config:
        for key, value in config.items():
            lines.append(f"{esc(key)}: {esc(value)}")
    else:
        lines.append("(Payment details for this method haven't been configured yet - contact support.)")
    return "\n".join(lines)


def payment_method_choice_keyboard(application_id, methods):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(name, callback_data=f"paymethod:{application_id}:{i}")]
        for i, (name, config) in enumerate(methods)
    ])


async def send_payment_instructions(context, application_id, applicant_id, price, currency="NGN"):
    methods = db.get_enabled_payment_methods()
    mode = db.get_config("payment_mode", "multiple")

    if not methods:
        await context.bot.send_message(
            chat_id=applicant_id,
            text=(
                f"💳 Amount due: {format_price(price, currency)}\n\n"
                "No payment methods are currently configured - please contact "
                "support to arrange payment."
            ),
        )
        return

    if mode == "single" or len(methods) == 1:
        name, config = methods[0]
        await context.bot.send_message(
            chat_id=applicant_id,
            text=format_payment_method_instructions(name, config, price, currency),
            parse_mode="HTML",
        )
    else:
        await context.bot.send_message(
            chat_id=applicant_id,
            text=f"💳 Amount due: {format_price(price, currency)}\n\nChoose a payment method:",
            reply_markup=payment_method_choice_keyboard(application_id, methods),
        )


async def payment_method_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fixed: the Telegram Stars branch previously had a misindented
    `return` that fell OUTSIDE its `if`, so it ran unconditionally right
    after the Stars check regardless of which method was actually
    chosen - meaning every method other than Stars silently short-
    circuited before ever reaching its own instructions. Each branch
    below now handles its own case and returns only from inside it.
    Telegram Gifts and Telegram Stars behaviour is unchanged."""
    query = update.callback_query
    await query.answer()

    _, aid_str, idx_str = query.data.split(":")
    methods = db.get_enabled_payment_methods()
    idx = int(idx_str)

    if idx >= len(methods):
        await query.edit_message_text("That payment method is no longer available - please ask for the list again.")
        return

    application_id = int(aid_str)
    app_row = db.get_application(application_id)

    rate_row = db.get_rate_with_currency(app_row[7]) if app_row else None
    currency = rate_row[1] if rate_row else "NGN"
    price = (app_row[16] if app_row and app_row[16] is not None
             else (rate_row[0] if rate_row else 0))

    method_name = methods[idx][0]

    if method_name == "Telegram Stars":
        await query.edit_message_text(
            "⭐ Telegram Stars\n\n"
            f"Please send the payment to @{db.get_config('stars_recipient', '')}.\n\n"
            "After payment, send your payment proof here.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🔙 Change Payment Method",
                    callback_data=f"paymethods:{application_id}"
                )]
            ]),
        )
        return

    if str(method_name).strip().lower() == "telegram gifts":
        recipient = db.get_config("gifts_recipient", "")
        await query.edit_message_text(
            "🎁 Telegram Gifts\n\n"
            f"Please send the gift to @{recipient}.\n\n"
            "After payment, send your payment proof here.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Change Payment Method",
                        callback_data=f"paymethods:{application_id}"
                    )
                ]
            ]),
        )
        return

    name, config = methods[idx]
    await query.edit_message_text(
        format_payment_method_instructions(name, config, price, currency),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🔙 Change Payment Method",
                callback_data=f"paymethods:{application_id}"
            )]
        ]),
    )


async def change_payment_method_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    application_id = int(query.data.split(":", 1)[1])
    methods = db.get_enabled_payment_methods()

    if not methods:
        await query.edit_message_text(
            "❌ No payment methods are currently available."
        )
        return

    await query.edit_message_text(
        "💳 Choose a payment method:",
        reply_markup=payment_method_choice_keyboard(application_id, methods),
    )


async def approve_application(context, application_id, actor_id):
    """Shared approval logic - marks approved, audits, notifies the
    applicant, and sends payment instructions. Used by both the ✅
    Approve button (decision_callback) and /approveall, so nothing is
    skipped or duplicated between the two paths. Returns False if the
    application no longer exists."""
    db.set_application_status(application_id, "approved", decided_by=actor_id)
    db.audit(actor_id, "approve_application", f"Application #{application_id}")

    app_row = db.get_application(application_id)
    if not app_row:
        return False

    applicant_id = app_row[1]
    duration_days = app_row[7]
    rate_row = db.get_rate_with_currency(duration_days)
    price, currency = rate_row if rate_row else (0, "NGN")

    try:
        await context.bot.send_message(
            chat_id=applicant_id,
            text=(
                "✅ Congratulations!\n\n"
                "We have received your advertisement."
                " To proceed, go ahead "
                "and pay using the instructions below, then send your payment "
                "proof (screenshot, photo, or PDF) right here in this chat."
            ),
        )
        await send_payment_instructions(context, application_id, applicant_id, price, currency)
    except Exception as e:
        logger.error(f"Failed to notify applicant {applicant_id} of approval: {e}")

    return True


async def reject_application(context, application_id, actor_id, reason="Application rejected."):
    """Shared rejection logic - marks rejected, audits, and notifies the
    applicant with the given reason. Used by both the free-text
    reject-reason flow (reject_reason_message) and /rejectall (which
    has no interactive reason step, so it falls back to a generic
    default reason unless one is given as command arguments). Returns
    False if the application no longer exists."""
    db.set_application_status(application_id, "rejected", decided_by=actor_id, reject_reason=reason)
    db.audit(actor_id, "reject_application", f"Application #{application_id}: {reason}")

    app_row = db.get_application(application_id)
    if not app_row:
        return False

    applicant_id = app_row[1]

    try:
        await context.bot.send_message(
            chat_id=applicant_id,
            text=f"❌ Unfortunately your advertisement was not approved.\n\nReason:\n{reason}",
        )
    except Exception as e:
        logger.error(f"Failed to notify applicant {applicant_id} of rejection: {e}")

    return True


async def decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if not is_owner_or_admin(user.id):
        await query.answer("🚫 Owner/admin only.", show_alert=True)
        return

    _, action, aid_str = query.data.split(":")
    application_id = int(aid_str)
    app_row = db.get_application(application_id)

    if not app_row:
        await query.answer("❌ Application not found.", show_alert=True)
        return

    applicant_id = app_row[1]

    if action == "approve":
        await approve_application(context, application_id, user.id)
        await query.answer()
        await query.edit_message_text(
            application_detail_text(db.get_application(application_id)) + "\n✅ <b>APPROVED</b>",
            parse_mode="HTML",
        )
        return

    if action == "reject":
        # Not a ConversationHandler state - "awaiting a reason" is
        # tracked via the user_data flag below, checked by the
        # standalone reject_reason_message handler on the owner's next
        # private text message.
        context.user_data["reject_application_id"] = application_id
        await query.answer()
        await query.edit_message_text(
            f"Application #{application_id}: please type the rejection reason to send to the applicant."
        )
        return


@owner_only
async def approveall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approves every application currently at status='pending', using
    the exact same approve_application() logic as the individual ✅
    Approve button - so nothing about notifications or payment
    instructions is skipped."""
    ids = db.get_pending_application_ids()
    if not ids:
        await update.message.reply_text("No pending applications to approve.")
        return

    count = 0
    for application_id in ids:
        if await approve_application(context, application_id, update.effective_user.id):
            count += 1

    await update.message.reply_text(f"✅ Approved {count} pending advertisement{'s' if count != 1 else ''}.")


@owner_only
async def rejectall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rejects every application currently at status='pending'. Any
    text after the command is used as the rejection reason sent to
    every applicant; otherwise a generic default is used (there's no
    interactive per-application reason step for a bulk action)."""
    ids = db.get_pending_application_ids()
    if not ids:
        await update.message.reply_text("No pending applications to reject.")
        return

    reason = " ".join(context.args).strip() if context.args else "Application rejected."

    count = 0
    for application_id in ids:
        if await reject_application(context, application_id, update.effective_user.id, reason):
            count += 1

    await update.message.reply_text(f"❌ Rejected {count} pending advertisement{'s' if count != 1 else ''}.")


async def reject_reason_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Standalone (not part of the submit ConversationHandler) - the
    owner types a free-text reason after tapping ❌ Reject."""
    application_id = context.user_data.pop("reject_application_id", None)
    if application_id is None:
        return False  # not currently awaiting a reject reason from this user

    reason = update.message.text.strip()
    await reject_application(context, application_id, update.effective_user.id, reason)
    await update.message.reply_text(f"✅ Application #{application_id} rejected and the applicant has been notified.")

    return True


async def coupon_code_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """If the sender has an application awaiting payment and what they
    just sent matches a real coupon code, apply it. Otherwise this is a
    no-op (not every stray message from an approved advertiser is a
    coupon attempt) - so unlike the other standalone handlers, "no
    match" here isn't distinguishable from "not a coupon", and both
    return False to let the dispatcher move on quietly."""
    user = update.effective_user
    text = update.message.text.strip()

    app_row = db.get_latest_approved_application(user.id)
    if not app_row:
        return False

    code = text.upper()
    coupon_row = db.get_coupon(code)
    if not coupon_row:
        return False  # not a coupon code - probably just a normal message, ignore quietly

    valid, reason = db.is_coupon_valid(code)
    if not valid:
        await update.message.reply_text(f"❌ {reason}")
        return True

    application_id = app_row[0]
    duration_days = app_row[7]
    rate_row = db.get_rate_with_currency(duration_days)
    base_price, currency = rate_row if rate_row else (0, "NGN")
    discounted_price = db.apply_coupon_to_price(code, base_price)

    db.set_application_coupon(application_id, code, discounted_price)

    await update.message.reply_text(
        f"✅ Coupon {esc(code)} applied! New price: {format_price(discounted_price, currency)} "
        f"(was {format_price(base_price, currency)})."
    )
    await send_payment_instructions(context, application_id, user.id, discounted_price, currency)
    return True


async def private_text_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Every plain private text message that ISN'T part of the submit
    ConversationHandler passes through here once, in a fixed order, so
    the stateful standalone flows (reject reason, custom start time,
    payment method config, coupon code) never fight over the same
    MessageHandler slot."""

    # Don't process messages here while the user is submitting an advertisement.
    if context.user_data.get("ad_draft") is not None:
        return
    if await reject_reason_message(update, context):
        return
    if await custom_start_time_message(update, context):
        return
    if await payment_config_message(update, context):
        return
    await coupon_code_message(update, context)


async def contact_advertiser_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if not is_owner_or_admin(user.id):
        await query.answer("🚫 Owner/admin only.", show_alert=True)
        return

    application_id = int(query.data.split(":", 1)[1])
    app_row = db.get_application(application_id)

    if not app_row:
        await query.answer("❌ Application not found.", show_alert=True)
        return

    applicant_username = app_row[3]
    applicant_id = app_row[1]

    if applicant_username:
        await query.answer(f"Message them at https://t.me/{applicant_username}", show_alert=True)
    else:
        await query.answer(f"No public username - their Telegram ID is {applicant_id}", show_alert=True)


# ---------------------------------------------------------------------
# Payment proof upload (advertiser side)
# ---------------------------------------------------------------------

def payment_decision_keyboard(payment_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Payment Confirmed", callback_data=f"paydecide:confirm:{payment_id}"),
        InlineKeyboardButton("❌ Payment Rejected", callback_data=f"paydecide:reject:{payment_id}"),
    ]])


def payment_review_keyboard(payment_id):
    """First-look gate shown to the owner the moment a receipt/photo
    arrives - separate from payment_decision_keyboard() above, which
    only appears afterward once the owner taps ▶️ Next here. Nothing is
    sent to the applicant until one of these two is tapped."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("▶️ Next", callback_data=f"payreview:next:{payment_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"payreview:decline:{payment_id}"),
    ]])


async def payment_proof_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires on any photo/document sent to the bot in DM. If the sender
    has an application awaiting payment (status='approved'), treat it as
    payment proof; otherwise ignore (not every DM upload is payment
    proof - e.g. it could be unrelated noise).

    Sends a generic "please wait" acknowledgment immediately (so the
    applicant isn't left wondering if anything happened), but the
    SPECIFIC verification outcome ("received, verifying" vs "invalid")
    still only goes out once the owner taps ▶️ Next / ❌ Decline on the
    receipt - see payment_review_callback."""
    user = update.effective_user
    message = update.message

    app_row = db.get_latest_approved_application(user.id)

    if not app_row:
        return  # no approved-and-awaiting-payment application - not payment proof

    application_id = app_row[0]

    if message.photo:
        file_id = message.photo[-1].file_id
        file_kind = "photo"
    elif message.document:
        file_id = message.document.file_id
        file_kind = "document"
    else:
        return

    await safe_call(message.reply_text, "⏳ Please wait...")

    payment_id = db.create_payment(application_id, file_id, file_kind)
    db.set_application_status(application_id, "awaiting_payment")

    try:
        caption = f"💳 Payment proof for Application #{application_id} (Payment #{payment_id})"
        if file_kind == "photo":
            await context.bot.send_photo(
                chat_id=OWNER_ID, photo=file_id, caption=caption,
                reply_markup=payment_review_keyboard(payment_id),
            )
        else:
            await context.bot.send_document(
                chat_id=OWNER_ID, document=file_id, caption=caption,
                reply_markup=payment_review_keyboard(payment_id),
            )
    except Exception as e:
        logger.error(f"Failed to forward payment proof {payment_id} to owner: {e}")


async def payment_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the ▶️ Next / ❌ Decline gate on a freshly-uploaded
    receipt - the applicant hears nothing until one of these is tapped.
    Next reveals the existing ✅ Payment Confirmed / ❌ Payment Rejected
    controls (payment_decision_keyboard) for the owner to actually
    verify the payment afterward; Decline ends it immediately and lets
    the applicant try uploading a different proof."""
    query = update.callback_query
    user = update.effective_user

    if not is_owner_or_admin(user.id):
        await query.answer("🚫 Owner/admin only.", show_alert=True)
        return

    _, action, pid_str = query.data.split(":")
    payment_id = int(pid_str)
    payment_row = db.get_payment(payment_id)

    if not payment_row:
        await query.answer("❌ Payment record not found.", show_alert=True)
        return

    application_id = payment_row[1]
    app_row = db.get_application(application_id)
    applicant_id = app_row[1] if app_row else None

    await query.answer()

    if action == "next":
        if applicant_id:
            try:
                await safe_call(
                    context.bot.send_message,
                    chat_id=applicant_id,
                    text="Payment proof received - the team will verify it shortly.",
                )
            except Exception as e:
                logger.error(f"Failed to notify applicant {applicant_id} that proof was received: {e}")
        try:
            await query.edit_message_reply_markup(reply_markup=payment_decision_keyboard(payment_id))
        except Exception as e:
            logger.error(f"Failed to reveal decision keyboard for payment {payment_id}: {e}")
        return

    # action == "decline"
    db.set_payment_status(payment_id, "rejected", confirmed_by=user.id)
    db.audit(user.id, "decline_payment_proof", f"Payment #{payment_id}, Application #{application_id}")
    if application_id:
        # Put the application back to 'approved' (awaiting payment) so
        # the applicant can upload a corrected proof instead of being
        # stuck at 'awaiting_payment' forever with no way to retry.
        db.set_application_status(application_id, "approved", decided_by=user.id)

    if applicant_id:
        try:
            await safe_call(context.bot.send_message, chat_id=applicant_id, text="Invalid Payment proof")
        except Exception as e:
            logger.error(f"Failed to notify applicant {applicant_id} of declined proof: {e}")

    try:
        if query.message.caption is not None:
            await query.edit_message_caption(caption=(query.message.caption or "") + "\n\n❌ DECLINED (invalid proof)")
        else:
            await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def payment_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if not is_owner_or_admin(user.id):
        await query.answer("🚫 Owner/admin only.", show_alert=True)
        return

    _, action, pid_str = query.data.split(":")
    payment_id = int(pid_str)
    payment_row = db.get_payment(payment_id)

    if not payment_row:
        await query.answer("❌ Payment record not found.", show_alert=True)
        return

    application_id = payment_row[1]
    app_row = db.get_application(application_id)
    applicant_id = app_row[1] if app_row else None

    await query.answer()

    if action == "confirm":
        db.set_payment_status(payment_id, "confirmed", confirmed_by=user.id)
        db.audit(user.id, "confirm_payment", f"Payment #{payment_id}, Application #{application_id}")
        try:
            await query.edit_message_caption(
                caption=(query.message.caption or "") + "\n\n✅ CONFIRMED - choose a start time below"
            )
        except Exception:
            pass

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("▶ Start Immediately", callback_data=f"paystart:{payment_id}:immediate"),
            InlineKeyboardButton("🗓 Custom Start Time", callback_data=f"paystart:{payment_id}:custom"),
        ]])
        await context.bot.send_message(
            chat_id=user.id,
            text=f"When should Application #{application_id}'s advertisement go live?",
            reply_markup=keyboard,
        )
    else:
        db.set_payment_status(payment_id, "rejected", confirmed_by=user.id)
        db.set_application_status(application_id, "payment_rejected", decided_by=user.id)
        db.audit(user.id, "reject_payment", f"Payment #{payment_id}, Application #{application_id}")
        try:
            await query.edit_message_caption(
                caption=(query.message.caption or "") + "\n\n❌ REJECTED"
            )
        except Exception:
            pass
        if applicant_id:
            try:
                await context.bot.send_message(
                    chat_id=applicant_id,
                    text="❌ Payment could not be verified.\n\nPlease contact support.",
                )
            except Exception as e:
                logger.error(f"Failed to notify applicant {applicant_id} of payment rejection: {e}")


async def paystart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if not is_owner_or_admin(user.id):
        await query.answer("🚫 Owner/admin only.", show_alert=True)
        return

    _, pid_str, mode = query.data.split(":")
    payment_id = int(pid_str)

    if mode == "custom":
        context.user_data["awaiting_custom_start_for_payment"] = payment_id
        await query.answer()
        await query.edit_message_text(
            "Send the start date/time as YYYY-MM-DD HH:MM (24-hour clock), "
            "e.g. 2026-08-10 14:00."
        )
        return

    await query.answer()
    subscription_id, status_value = await finalize_subscription(context, payment_id, datetime.now(), user.id)
    state_note = "🟢 LIVE now" if status_value == "active" else "🟡 scheduled"
    await query.edit_message_text(
        f"✅ Advertisement scheduled to start immediately.\n\n"
        f"Subscription #{subscription_id} - {state_note}.\n"
        f"Check with /subscription {subscription_id} any time, or it should already be "
        f"rotating into @Uff_GameBot's /leaderboard, /topplayers, and /topgroups."
    )


async def custom_start_time_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Standalone handler (not a ConversationHandler state) for the
    owner's reply after tapping 🗓 Custom Start Time - mirrors the
    reject-reason pattern: a user_data flag set by the button, consumed
    here on the owner's next private text message."""
    payment_id = context.user_data.pop("awaiting_custom_start_for_payment", None)
    if payment_id is None:
        return False  # not awaiting a custom start time - let other handlers try

    text = update.message.text.strip()
    try:
        start_at = datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        context.user_data["awaiting_custom_start_for_payment"] = payment_id  # let them retry
        await update.message.reply_text(
            "❌ Couldn't parse that. Please send it as YYYY-MM-DD HH:MM, e.g. 2026-08-10 14:00."
        )
        return True

    subscription_id, status_value = await finalize_subscription(context, payment_id, start_at, update.effective_user.id)
    state_note = "🟢 LIVE now" if status_value == "active" else "🟡 scheduled"
    await update.message.reply_text(
        f"✅ Advertisement scheduled to start {start_at.strftime('%Y-%m-%d %H:%M')}.\n\n"
        f"Subscription #{subscription_id} - {state_note}. Check with /subscription {subscription_id} any time."
    )
    return True


def _publish_to_sponsored_slot(ad_text, ad_link):
    """Drops an ad straight into the Sponsored Ad slot on
    @Uff_GameBot's /leaderboard, /topplayers, and /topgroups, using the
    applicant's own advertisement text as the visible label and their
    submitted link/username as the hidden target (Telegram's
    [label](target) hidden-link format - @Uff_GameBot's
    /changesponsoredad already parses this exact syntax). Both bots
    read/write the same bot_config table, so this takes effect
    immediately with no restart needed on either side. Shared by both
    'Start Immediately' (finalize_subscription) and the scheduled-
    activation background job (job_activate_scheduled_ads), so a
    Custom Start Time ad gets the identical handoff once its scheduled
    time actually arrives."""
    db.set_config("sponsored_ad_override", f"[{ad_text}]({ad_link})")
    db.set_config("sponsored_ad_enabled", "1")
    db.set_config("ads_enabled", "1")


async def finalize_subscription(context, payment_id, start_at, actor_id):
    """Creates the ad_subscriptions row, generates an invoice, redeems
    any applied coupon, and sends the advertiser their receipt. Shared
    by both the 'Start Immediately' and 'Custom Start Time' paths."""
    payment_row = db.get_payment(payment_id)
    application_id = payment_row[1]
    app_row = db.get_application(application_id)

    (aid, applicant_id, applicant_name, applicant_username, business_name,
     ad_link, ad_type, duration_days, ad_text, notes, status,
     reject_reason, submitted_at, decided_at, decided_by,
     coupon_code, final_price) = app_row

    rate_row = db.get_rate_with_currency(duration_days)
    base_price, currency = rate_row if rate_row else (0, "NGN")
    amount_paid = final_price if final_price is not None else base_price

    now = datetime.now()  # single reference point for this whole call,
    # so "Start Immediately" (start_at passed in as datetime.now() by
    # the caller) can never be evaluated against a LATER "now" here -
    # it's always active the instant it's created, not dependent on
    # how much time this function itself takes to run.
    end_at = start_at + timedelta(days=duration_days)
    status_value = "active" if start_at <= now else "scheduled"

    subscription_id = db.create_subscription(
        application_id=application_id, applicant_id=applicant_id, business_name=business_name,
        ad_link=ad_link, ad_text=ad_text, ad_type=ad_type, duration_days=duration_days,
        start_at=start_at.isoformat(), end_at=end_at.isoformat(), status=status_value,
    )

    if status_value == "active":
        # "Go live now" (Start Immediately) - drop this ad straight
        # into the Sponsored Ad slot immediately. Custom Start Time
        # ads get the same handoff later, from job_activate_scheduled_ads
        # once their scheduled time actually arrives.
        _publish_to_sponsored_slot(ad_text, ad_link)

    db.set_application_status(application_id, "payment_confirmed", decided_by=actor_id)

    methods = db.get_enabled_payment_methods()
    method_name = methods[0][0] if len(methods) == 1 else "Unspecified"

    invoice_id = db.create_invoice(
        application_id=application_id, subscription_id=subscription_id,
        business_name=business_name, advertiser_name=applicant_name,
        advertiser_username=applicant_username, advertiser_id=applicant_id,
        duration_days=duration_days, amount_paid=amount_paid, payment_method=method_name,
        ad_start=start_at.isoformat(), ad_end=end_at.isoformat(), currency=currency,
    )

    if coupon_code:
        db.redeem_coupon(coupon_code)

    db.audit(actor_id, "activate_subscription",
             f"Application #{application_id}, Subscription #{subscription_id}, Invoice #{invoice_id}")

    when_text = "immediately" if status_value == "active" else f"on {start_at.strftime('%Y-%m-%d %H:%M')}"

    receipt = f"""
✅ <b>Payment Confirmed</b>

Your advertisement will begin {when_text}.

🧾 <b>Receipt (Invoice #{invoice_id})</b>
Business: {esc(business_name)}
Duration: {duration_days} Days
Amount Paid: {format_price(amount_paid, currency)}
Ad Start: {start_at.strftime('%Y-%m-%d %H:%M')}
Ad End: {end_at.strftime('%Y-%m-%d %H:%M')}

Thank you for advertising with us! Use /myads any time to check on your ads,
and /receipts to see your past invoices.
"""
    try:
        await context.bot.send_message(chat_id=applicant_id, text=receipt, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to send receipt to {applicant_id}: {e}")

    return subscription_id, status_value


# ---------------------------------------------------------------------
# Owner commands: applications / open / approved / rejected / payments
# ---------------------------------------------------------------------

APPLICATIONS_PAGE_SIZE = 20


def format_application_row(row) -> str:
    aid, applicant_id, applicant_name, applicant_username, business_name, ad_type, duration_days, status, submitted_at = row
    uname = f"@{applicant_username}" if applicant_username else esc(applicant_name)
    return f"#{aid} - {esc(business_name)} ({esc(ad_type)}, {duration_days}d) - {uname} - {esc(status)}"


def applications_list_keyboard(status, page):
    total = db.count_applications_by_status(status)
    offset = page * APPLICATIONS_PAGE_SIZE
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅ Previous", callback_data=f"adlist:{status}:{page-1}"))
    if offset + APPLICATIONS_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("➡ Next", callback_data=f"adlist:{status}:{page+1}"))
    return InlineKeyboardMarkup([nav]) if nav else None, total


async def send_applications_page(update_or_query, status, page, edit=False):
    total = db.count_applications_by_status(status)
    offset = page * APPLICATIONS_PAGE_SIZE
    rows = db.get_applications_by_status(status, offset, APPLICATIONS_PAGE_SIZE)

    if not rows:
        text = f"No {status} applications." if page == 0 else "No more applications."
    else:
        total_pages = max(1, (total + APPLICATIONS_PAGE_SIZE - 1) // APPLICATIONS_PAGE_SIZE)
        lines = [f"📋 <b>{status.capitalize()} Applications</b> ({total} total) - Page {page+1}/{total_pages}\n"]
        lines.extend(format_application_row(r) for r in rows)
        text = "\n".join(lines)

    keyboard, _ = applications_list_keyboard(status, page)

    if edit:
        await update_or_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update_or_query.reply_html(text, reply_markup=keyboard)


@owner_or_admin_only
async def applications_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_applications_page(update.message, "pending", 0)


@owner_or_admin_only
async def approved_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_applications_page(update.message, "approved", 0)


@owner_or_admin_only
async def rejected_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_applications_page(update.message, "rejected", 0)


async def applications_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not is_owner_or_admin(user.id):
        await query.answer("🚫 Owner/admin only.", show_alert=True)
        return
    await query.answer()
    _, status, page_str = query.data.split(":")
    await send_applications_page(query, status, int(page_str), edit=True)


@owner_or_admin_only
async def open_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /open <Application ID>")
        return

    app_row = db.get_application(int(context.args[0]))
    if not app_row:
        await update.message.reply_text("❌ No application with that ID.")
        return

    await update.message.reply_html(
        application_detail_text(app_row),
        reply_markup=decision_keyboard(app_row[0]) if app_row[10] == "pending" else None,
    )


@owner_or_admin_only
async def payments_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.get_pending_payments()
    if not rows:
        await update.message.reply_text("No payments waiting for verification.")
        return

    lines = [f"💳 <b>Payments Awaiting Verification</b> ({db.count_pending_payments()} total)\n"]
    for pid, application_id, submitted_at, business_name, applicant_name in rows:
        lines.append(f"Payment #{pid} - Application #{application_id} - {esc(business_name)} ({esc(applicant_name)})")

    await update.message.reply_html("\n".join(lines))


@owner_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = db.get_stats()
    text = f"""
📊 <b>Advertisement Stats</b>

Total Applications: {s['total']}
Approved: {s['approved']}
Rejected: {s['rejected']}
Pending: {s['pending']}
Payments Waiting: {s['payments_waiting']}
Revenue Received: ₦{s['revenue']}
Today's Applications: {s['today']}
"""
    await update.message.reply_html(text)


@owner_only
async def searchads_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /searchads <username, Telegram ID, business name, link, or application ID>"
        )
        return

    query = " ".join(context.args)
    rows = db.search_applications(query)

    if not rows:
        await update.message.reply_text("No matching applications found.")
        return

    lines = [f"🔎 <b>Search Results</b> ({len(rows)})\n"]
    lines.extend(format_application_row(r) for r in rows[:50])
    if len(rows) > 50:
        lines.append(f"\n<i>+{len(rows) - 50} more - refine your search.</i>")

    await update.message.reply_html("\n".join(lines))


# ---------------------------------------------------------------------
# Payment Management System (owner-only)
# ---------------------------------------------------------------------

PAYMENT_METHOD_FIELDS = {
    "bank": ["Bank Name", "Account Name", "Account Number"],
    "paystack": ["Public Key", "Secret Key", "Webhook Secret"],
    "flutterwave": ["Public Key", "Secret Key", "Webhook Secret"],
    "telegram_stars": [],
    "telegram_gifts": [],
}


@owner_only
async def paymentmethods_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    methods = db.get_payment_methods()
    if not methods:
        await update.message.reply_text("💳 No payment methods have been added yet. Use /addpayment <name>.")
        return

    mode = db.get_config("payment_mode", "multiple")
    lines = [f"💳 <b>Payment Methods</b> (mode: {mode})\n"]
    for name, enabled, config in methods:
        lines.append(f"{'🟢' if enabled else '🔴'} {esc(name)}")

    await update.message.reply_html("\n".join(lines))


@owner_only
async def addpayment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addpayment <name>\nExample: /addpayment Bank Transfer")
        return

    name = " ".join(context.args)
    added = db.add_payment_method(name)
    db.audit(update.effective_user.id, "add_payment_method", name)

    if added:
        await update.message.reply_text(f"✅ Payment method added: {name}")
    else:
        await update.message.reply_text(f"⚠️ A payment method named '{name}' already exists.")


@owner_only
async def removepayment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removepayment <name>")
        return

    name = " ".join(context.args)
    removed = db.remove_payment_method(name)
    db.audit(update.effective_user.id, "remove_payment_method", name)

    if removed:
        await update.message.reply_text(f"✅ Payment method removed: {name}")
    else:
        await update.message.reply_text(f"❌ No payment method named '{name}' found.")


@owner_only
async def enablepayment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /enablepayment <name>")
        return

    name = " ".join(context.args)
    updated = db.set_payment_method_enabled(name, True)
    db.audit(update.effective_user.id, "enable_payment_method", name)

    if updated:
        await update.message.reply_text(f"✅ {name} is now enabled.")
    else:
        await update.message.reply_text(f"❌ No payment method named '{name}' found.")


@owner_only
async def disablepayment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /disablepayment <name>")
        return

    name = " ".join(context.args)
    updated = db.set_payment_method_enabled(name, False)
    db.audit(update.effective_user.id, "disable_payment_method", name)

    if updated:
        await update.message.reply_text(f"✅ {name} is now disabled.")
    else:
        await update.message.reply_text(f"❌ No payment method named '{name}' found.")


@owner_only
async def paymentmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() not in ("single", "multiple"):
        current = db.get_config("payment_mode", "multiple")
        await update.message.reply_text(f"Usage: /paymentmode single|multiple\nCurrent mode: {current}")
        return

    mode = context.args[0].lower()
    db.set_config("payment_mode", mode)
    db.audit(update.effective_user.id, "set_payment_mode", mode)
    await update.message.reply_text(f"✅ Payment mode set to: {mode}")

    
@owner_only
async def setstarsrecipient_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setstarsrecipient @username")
        return

    username = context.args[0].lstrip("@")
    db.set_config("stars_recipient", username)
    db.audit(update.effective_user.id, "set_stars_recipient", username)

    await update.message.reply_text(
        f"✅ Telegram Stars recipient set to @{username}"
    )

@owner_only
async def setgiftsrecipient_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setgiftsrecipient @username")
        return

    username = context.args[0].lstrip("@")
    db.set_config("gifts_recipient", username)
    db.audit(update.effective_user.id, "set_gifts_recipient", username)

    await update.message.reply_text(
        f"✅ Telegram Gifts recipient set to @{username}"
    )

async def _start_payment_config(update, context, method_key):
    fields = PAYMENT_METHOD_FIELDS[method_key]
    context.user_data["payment_config"] = {"method": method_key, "fields": fields, "answers": {}, "index": 0}
    await update.message.reply_text(f"Configuring {method_key.capitalize()}.\n\n{fields[0]}:")


@owner_only
async def setbank_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /setbank <Bank Name> | <Account Name> | <Account Number>
    One-line config (no back-and-forth) - saves immediately, enables
    Bank Transfer, and the details show automatically the next time
    someone selects Bank Transfer. Safe to re-run any time to update
    the details later."""
    text = update.message.text or ""
    _, _, rest = text.partition(" ")
    rest = rest.strip()

    if not rest or "|" not in rest:
        await update.message.reply_text(
            "Usage: /setbank <Bank Name> | <Account Name> | <Account Number>\n"
            "Example: /setbank GTBank | John Doe | 0123456789"
        )
        return

    parts = [p.strip() for p in rest.split("|")]
    if len(parts) != 3 or not all(parts):
        await update.message.reply_text(
            "Please provide exactly 3 parts separated by |.\n"
            "Example: /setbank GTBank | John Doe | 0123456789"
        )
        return

    bank_name, account_name, account_number = parts

    db.add_payment_method("Bank Transfer")
    db.set_payment_method_config("Bank Transfer", json.dumps({
        "Bank Name": bank_name,
        "Account Name": account_name,
        "Account Number": account_number,
    }))
    db.set_payment_method_enabled("Bank Transfer", True)
    db.audit(update.effective_user.id, "configure_payment_method", "Bank Transfer")

    await update.message.reply_text(
        "✅ Bank Transfer details saved and enabled.\n\n"
        f"Bank Name: {bank_name}\n"
        f"Account Name: {account_name}\n"
        f"Account Number: {account_number}"
    )


@owner_only
async def setpayment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /setpayment <Method Name> | <Field: Value> | <Field: Value> ...
    Generic one-line configurator for ANY payment method - new or
    existing (Paystack, Flutterwave, Crypto, or something you invent).
    Saves, enables, and immediately becomes visible to advertisers.
    Re-run any time with the same method name to update its details."""
    text = update.message.text or ""
    _, _, rest = text.partition(" ")
    rest = rest.strip()

    if not rest or "|" not in rest:
        await update.message.reply_text(
            "Usage: /setpayment <Method Name> | <Field: Value> | <Field: Value> ...\n"
            "Example: /setpayment Paystack | Public Key: pk_live_xxx | Secret Key: sk_live_xxx"
        )
        return

    segments = [s.strip() for s in rest.split("|")]
    method_name = segments[0]
    field_segments = segments[1:]

    if not method_name or not field_segments:
        await update.message.reply_text(
            "Usage: /setpayment <Method Name> | <Field: Value> | <Field: Value> ...\n"
            "Example: /setpayment Crypto (USDT) | Wallet Address: TXxxxx...xxxx"
        )
        return

    config = {}
    for segment in field_segments:
        if ":" not in segment:
            await update.message.reply_text(
                f"❌ Couldn't parse '{segment}' - each field must be written as 'Label: Value'."
            )
            return
        key, _, value = segment.partition(":")
        key, value = key.strip(), value.strip()
        if not key or not value:
            await update.message.reply_text(
                f"❌ Couldn't parse '{segment}' - each field must be written as 'Label: Value'."
            )
            return
        config[key] = value

    db.add_payment_method(method_name)
    db.set_payment_method_config(method_name, json.dumps(config))
    db.set_payment_method_enabled(method_name, True)
    db.audit(update.effective_user.id, "configure_payment_method", method_name)

    details = "\n".join(f"{k}: {v}" for k, v in config.items())
    await update.message.reply_text(f"✅ {method_name} saved and enabled.\n\n{details}")


@owner_only
async def setpaystack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _start_payment_config(update, context, "paystack")


@owner_only
async def setflutterwave_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _start_payment_config(update, context, "flutterwave")


@owner_only
async def setcrypto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["payment_config"] = {
        "method": "Crypto [TON Coin Only]",
        "fields": ["TON Wallet Address"],
        "answers": {},
        "index": 0,
    }

    await update.message.reply_text(
        "Configuring Crypto [TON Coin Only].\n\n"
        "TON Wallet Address:"
    )
@owner_only
async def fixcrypto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    updated = db.rename_payment_method(
        "crypto_ton",
        "Crypto [TON Coin Only]"
    )

    if updated:
        await update.message.reply_text(
            "✅ Crypto payment method renamed successfully."
        )
    else:
        await update.message.reply_text(
            "❌ Could not find crypto_ton."
        )

@owner_only
async def addstars_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    added = db.add_payment_method("Telegram Stars")
    db.set_payment_method_config(
        "Telegram Stars",
        json.dumps({
            "recipient": db.get_config("stars_recipient", "")
        })
    )
    db.audit(update.effective_user.id, "add_payment_method", "Telegram Stars")

    if added:
        await update.message.reply_text("✅ Telegram Stars added as a payment method.")
    else:
        await update.message.reply_text("⚠️ Telegram Stars is already a payment method.")


@owner_only
async def addgifts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    added = db.add_payment_method("Telegram Gifts")
    db.set_payment_method_config(
        "Telegram Gifts",
        json.dumps({
            "recipient": db.get_config("gifts_recipient", "")
        })
    )
    db.audit(update.effective_user.id, "add_payment_method", "Telegram Gifts")

    if added:
        await update.message.reply_text("✅ Telegram Gifts added as a payment method.")
    else:
        await update.message.reply_text("⚠️ Telegram Gifts is already a payment method.")

async def payment_config_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Standalone handler consuming the answers for /setpaystack,
    /setflutterwave, /setcrypto - one field per message, same pattern as
    the other stateful standalone flows. Returns True if it consumed
    the message."""
    state = context.user_data.get("payment_config")
    if not state:
        return False

    answer = update.message.text.strip()
    field_name = state["fields"][state["index"]]
    state["answers"][field_name] = answer
    state["index"] += 1

    if state["index"] < len(state["fields"]):
        await update.message.reply_text(f"{state['fields'][state['index']]}:")
        return True

    # All fields collected - save and finish.
    method_key = state["method"]
    db.add_payment_method(method_key)
    db.set_payment_method_config(method_key, json.dumps(state["answers"]))
    db.audit(update.effective_user.id, "configure_payment_method", method_key)
    context.user_data.pop("payment_config", None)

    await update.message.reply_text(f"✅ {method_key.capitalize()} configured and enabled.")
    return True


# ---------------------------------------------------------------------
# /broadcast
# ---------------------------------------------------------------------

BROADCAST_AUDIENCES = {
    "everyone": None,
    "approved": "approved",
    "pending": "pending",
    "rejected": "rejected",
}


@owner_or_admin_only
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /broadcast <everyone|approved|pending|rejected> <message>"
        )
        return

    audience = context.args[0].lower()
    if audience not in BROADCAST_AUDIENCES:
        await update.message.reply_text("Audience must be one of: everyone, approved, pending, rejected")
        return

    message_text = " ".join(context.args[1:])
    status_filter = BROADCAST_AUDIENCES[audience]
    recipient_ids = db.get_all_advertiser_ids(status_filter)

    sent = 0
    failed = 0
    for uid in recipient_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=message_text)
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(f"📣 Broadcast sent to {sent} recipient(s). {failed} failed to deliver.")


# ---------------------------------------------------------------------
# Subscription & Billing System
# ---------------------------------------------------------------------

STATUS_EMOJI = {
    "active": "🟢", "scheduled": "🟡", "expiring_soon": "🟠",
    "expired": "🔴", "paused": "⏸", "cancelled": "❌",
}


def subscription_summary_line(row) -> str:
    sid, applicant_id, business_name, duration_days, start_at, end_at, status = row
    emoji = STATUS_EMOJI.get(status, "•")
    return f"{emoji} #{sid} - {esc(business_name)} ({duration_days}d, {esc(status)})"


# --- Coupons (owner-only) ---

@owner_only
async def createcoupon_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /createcoupon <CODE> <20%|500> [max_uses] [expires_YYYY-MM-DD]\n"
            "Examples:\n/createcoupon UFF20 20%\n/createcoupon NEWUSER 500 10 2026-12-31"
        )
        return

    code = context.args[0].upper()
    value_arg = context.args[1]

    if value_arg.endswith("%"):
        discount_type = "percent"
        try:
            discount_value = int(value_arg[:-1])
        except ValueError:
            await update.message.reply_text("Percentage must be a whole number, e.g. 20%.")
            return
    else:
        discount_type = "fixed"
        try:
            discount_value = int(value_arg)
        except ValueError:
            await update.message.reply_text("Fixed discount must be a whole number, e.g. 500.")
            return

    max_uses = None
    expires_at = None
    for extra in context.args[2:]:
        if extra.isdigit():
            max_uses = int(extra)
        else:
            try:
                expires_at = datetime.strptime(extra, "%Y-%m-%d").isoformat()
            except ValueError:
                pass

    db.create_coupon(code, discount_type, discount_value, expires_at, max_uses, update.effective_user.id)
    db.audit(update.effective_user.id, "create_coupon", code)

    await update.message.reply_text(f"✅ Coupon {code} created.")


@owner_only
async def coupons_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coupons = db.get_all_coupons()
    if not coupons:
        await update.message.reply_text("No coupons have been created yet.")
        return

    lines = ["🎟 <b>Coupons</b>\n"]
    for code, dtype, dvalue, expires_at, max_uses, uses_count in coupons:
        value_display = f"{dvalue}%" if dtype == "percent" else f"₦{dvalue}"
        uses_display = f"{uses_count}/{max_uses}" if max_uses else f"{uses_count}/∞"
        expiry_display = expires_at[:10] if expires_at else "never"
        lines.append(f"{esc(code)} - {value_display} off - used {uses_display} - expires {expiry_display}")

    await update.message.reply_html("\n".join(lines))


@owner_only
async def deletecoupon_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /deletecoupon <CODE>")
        return
    code = context.args[0].upper()
    removed = db.delete_coupon(code)
    db.audit(update.effective_user.id, "delete_coupon", code)
    await update.message.reply_text(f"✅ Coupon {code} deleted." if removed else f"❌ No coupon named {code}.")


# --- Promotional offers (owner-only) ---

@owner_only
async def createoffer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /createoffer <YYYY-MM-DD> <YYYY-MM-DD> <description>\n"
            "Example: /createoffer 2026-08-10 2026-08-12 30% Off Weekend Sale"
        )
        return

    try:
        start_at = datetime.strptime(context.args[0], "%Y-%m-%d").isoformat()
        end_at = datetime.strptime(context.args[1], "%Y-%m-%d").isoformat()
    except ValueError:
        await update.message.reply_text("Dates must be YYYY-MM-DD.")
        return

    description = " ".join(context.args[2:])
    offer_id = db.create_offer(description, start_at, end_at, update.effective_user.id)
    db.audit(update.effective_user.id, "create_offer", f"#{offer_id}: {description}")

    await update.message.reply_text(f"✅ Offer #{offer_id} created: {description}")


@owner_only
async def offers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    offers = db.get_all_offers()
    if not offers:
        await update.message.reply_text("No promotional offers have been created yet.")
        return

    lines = ["🎁 <b>Promotional Offers</b>\n"]
    active_ids = {o[0] for o in db.get_active_offers()}
    for oid, description, start_at, end_at in offers:
        tag = "🟢 active" if oid in active_ids else "⚪ inactive"
        lines.append(f"#{oid} [{tag}] {esc(description)} ({start_at[:10]} to {end_at[:10]})")

    await update.message.reply_html("\n".join(lines))


@owner_only
async def deleteoffer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /deleteoffer <ID>")
        return
    removed = db.delete_offer(int(context.args[0]))
    db.audit(update.effective_user.id, "delete_offer", context.args[0])
    await update.message.reply_text("✅ Offer deleted." if removed else "❌ No offer with that ID.")


# --- Invoices / receipts ---

def invoice_text(row) -> str:
    (inv_id, application_id, subscription_id, business_name, advertiser_name,
     advertiser_username, advertiser_id, duration_days, amount_paid,
     payment_method, payment_date, ad_start, ad_end, currency) = row

    uname = f"@{advertiser_username}" if advertiser_username else "—"
    try:
        date_display = datetime.fromisoformat(payment_date).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        date_display = payment_date or "—"

    return f"""
🧾 <b>Invoice #{inv_id}</b>

Business: {esc(business_name)}
Advertiser: {esc(advertiser_name)} ({esc(uname)})
Telegram ID: <code>{advertiser_id}</code>
Advertisement Purchased: {duration_days} Days
Amount Paid: {format_price(amount_paid, currency)}
Payment Method: {esc(payment_method)}
Payment Date: {date_display}
Advertisement Start: {ad_start[:16] if ad_start else '—'}
Advertisement End: {ad_end[:16] if ad_end else '—'}
"""


@owner_only
async def invoice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /invoice <Invoice ID>")
        return

    row = db.get_invoice(int(context.args[0]))
    if not row:
        await update.message.reply_text("❌ No invoice with that ID.")
        return

    await update.message.reply_html(invoice_text(row))


async def receipts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Not owner-only - any advertiser can pull up their own receipts."""
    rows = db.get_advertiser_invoices(update.effective_user.id)
    if not rows:
        await update.message.reply_text("You don't have any receipts yet.")
        return

    lines = ["🧾 <b>Your Receipts</b>\n"]
    for inv_id, business_name, duration_days, amount_paid, payment_method, payment_date, currency in rows:
        date_display = (payment_date or "")[:10]
        lines.append(f"Invoice #{inv_id} - {esc(business_name)} - {format_price(amount_paid, currency)} - {date_display}")
    lines.append("\nContact support if you need a detailed copy of any receipt.")

    await update.message.reply_html("\n".join(lines))


# --- Owner financial reports ---

@owner_only
async def revenue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    period = context.args[0].lower() if context.args else "today"
    if period not in ("today", "week", "month", "year"):
        await update.message.reply_text("Usage: /revenue today|week|month|year")
        return

    r = db.get_revenue_report(period)
    text = f"""
📈 <b>Revenue Report ({period})</b>

Total Revenue: ₦{r['revenue']}
Advertisements Sold: {r['sold']}
Average Sale: ₦{r['avg_sale']}
Active Advertisements: {r['active']}
Renewals: {r['renewals']}
New Advertisers: {r['new_advertisers']}
"""
    await update.message.reply_html(text)


# --- Advertiser dashboard ---

async def myads_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    subs = db.get_advertiser_subscriptions(user.id)
    history = db.get_advertiser_history(user.id)
    invoices = db.get_advertiser_invoices(user.id)
    total_spent = sum(inv[3] for inv in invoices)
    my_pending = sum(1 for r in db.search_applications(str(user.id)) if r[7] == "pending")

    lines = ["📊 <b>My Advertisements</b>\n"]

    current = [s for s in subs if s[5] in ("active", "expiring_soon")]
    scheduled = [s for s in subs if s[5] == "scheduled"]
    expired = [s for s in subs if s[5] in ("expired", "cancelled")]

    if current:
        lines.append("<b>Current:</b>")
        for s in current:
            sid, business_name, duration_days, start_at, end_at, status, views, clicks = s
            try:
                remaining = datetime.fromisoformat(end_at) - datetime.now()
                remaining_display = f"{max(0, remaining.days)}d {max(0, remaining.seconds // 3600)}h left"
            except (ValueError, TypeError):
                remaining_display = "—"
            lines.append(f"{STATUS_EMOJI.get(status,'•')} #{sid} {esc(business_name)} - {remaining_display}")
        lines.append("")

    if scheduled:
        lines.append("<b>Scheduled:</b>")
        for s in scheduled:
            lines.append(f"🟡 #{s[0]} {esc(s[1])} - starts {s[3][:16] if s[3] else '—'}")
        lines.append("")

    if expired:
        lines.append(f"<b>Past:</b> {len(expired)} advertisement(s)")
        lines.append("")

    lines.append(f"📝 Pending Applications: {my_pending}")
    lines.append(f"💰 Total Spent: ₦{total_spent}")

    total_apps, approved_count, rejected_count, last_submitted = history
    if total_apps:
        lines.append(
            f"\n📜 History: {total_apps} submitted, {approved_count} approved, "
            f"{rejected_count} rejected"
        )

    await update.message.reply_html(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 Submit New Advertisement", callback_data="menu:submit")]]),
    )


# --- Renewal flow ---

async def renew_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    subscription_id = int(query.data.split(":", 1)[1])
    sub_row = db.get_subscription(subscription_id)

    if not sub_row:
        await query.edit_message_text("❌ That advertisement subscription no longer exists.")
        return

    context.user_data["renew_subscription_id"] = subscription_id
    await query.edit_message_text(
        "How long would you like to renew for?",
        reply_markup=duration_keyboard(),
    )


async def renew_duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reuses the adduration: callback pattern from the submit flow, but
    only proceeds down the renewal path if renew_subscription_id is set
    - otherwise it's a normal new-submission duration pick and this
    handler does nothing (the ConversationHandler's own state handler
    handles that case)."""
    subscription_id = context.user_data.get("renew_subscription_id")
    if subscription_id is None:
        return  # not a renewal in progress - let the submit conversation handle it

    query = update.callback_query
    await query.answer()

    days = int(query.data.split(":", 1)[1])
    context.user_data.pop("renew_subscription_id", None)

    sub_row = db.get_subscription(subscription_id)
    (sid, application_id, applicant_id, business_name, ad_link, ad_text, ad_type,
     old_duration, start_at, end_at, status, created_at, total_views, total_clicks,
     r7, r3, r24, rex, renewed_from) = sub_row

    # Renewal reuses the same ad content and skips straight to payment -
    # it's already been content-approved once.
    new_application_id = db.create_application(
        applicant_id=applicant_id, applicant_name=update.effective_user.first_name,
        applicant_username=update.effective_user.username, business_name=business_name,
        ad_link=ad_link, ad_type=ad_type, duration_days=days, ad_text=ad_text, notes="Renewal",
    )
    db.set_application_status(new_application_id, "approved", decided_by=None)
    db.audit(applicant_id, "renew_subscription", f"Subscription #{subscription_id} -> Application #{new_application_id}")

    rate_row = db.get_rate_with_currency(days)
    price, currency = rate_row if rate_row else (0, "NGN")
    await query.edit_message_text(f"✅ Renewal set up for {days} Days. Amount due: {format_price(price, currency)}")
    await send_payment_instructions(context, new_application_id, applicant_id, price, currency)


# --- Subscription management (owner commands) ---

@owner_or_admin_only
async def subscriptions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.get_all_subscriptions()
    if not rows:
        await update.message.reply_text("No advertisement subscriptions yet.")
        return

    lines = [f"📋 <b>Subscriptions</b> ({db.count_subscriptions()} total)\n"]
    lines.extend(subscription_summary_line(r) for r in rows)
    await update.message.reply_html("\n".join(lines))


@owner_or_admin_only
async def subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /subscription <ID>")
        return

    row = db.get_subscription(int(context.args[0]))
    if not row:
        await update.message.reply_text("❌ No subscription with that ID.")
        return

    (sid, application_id, applicant_id, business_name, ad_link, ad_text, ad_type,
     duration_days, start_at, end_at, status, created_at, total_views, total_clicks,
     r7, r3, r24, rex, renewed_from) = row

    ctr = f"{(total_clicks / total_views * 100):.1f}%" if total_views else "0%"

    text = f"""
📋 <b>Subscription #{sid}</b>

Business: {esc(business_name)}
Type: {esc(ad_type)}
Link: {esc(ad_link)}
Duration: {duration_days} Days
Status: {STATUS_EMOJI.get(status, '•')} {esc(status)}
Start: {start_at[:16] if start_at else '—'}
End: {end_at[:16] if end_at else '—'}

Views: {total_views}
Clicks: {total_clicks}
CTR: {ctr}
"""
    await update.message.reply_html(text)


@owner_or_admin_only
async def pausead_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /pausead <ID>")
        return
    subscription_id = int(context.args[0])
    db.set_subscription_status(subscription_id, "paused")
    db.audit(update.effective_user.id, "pause_ad", str(subscription_id))
    await update.message.reply_text(f"⏸ Subscription #{subscription_id} paused.")


@owner_or_admin_only
async def resumead_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /resumead <ID>")
        return
    subscription_id = int(context.args[0])
    db.set_subscription_status(subscription_id, "active")
    db.audit(update.effective_user.id, "resume_ad", str(subscription_id))
    await update.message.reply_text(f"▶ Subscription #{subscription_id} resumed.")


@owner_or_admin_only
async def extendad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2 or not context.args[0].isdigit() or not context.args[1].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /extendad <ID> <days>")
        return
    subscription_id = int(context.args[0])
    days = int(context.args[1])
    new_end = db.extend_subscription(subscription_id, days)
    if new_end is None:
        await update.message.reply_text("❌ No subscription with that ID.")
        return
    db.audit(update.effective_user.id, "extend_ad", f"#{subscription_id} by {days} days")
    await update.message.reply_text(f"✅ Subscription #{subscription_id} extended. New end date: {new_end[:16]}")


@owner_or_admin_only
async def cancelad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /cancelad <ID>")
        return
    subscription_id = int(context.args[0])
    db.set_subscription_status(subscription_id, "cancelled")
    db.audit(update.effective_user.id, "cancel_ad", str(subscription_id))
    await update.message.reply_text(f"❌ Subscription #{subscription_id} cancelled.")


@owner_or_admin_only
async def adreport_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /adreport <Advertisement ID>")
        return

    row = db.get_subscription(int(context.args[0]))
    if not row:
        await update.message.reply_text("❌ No subscription with that ID.")
        return

    (sid, application_id, applicant_id, business_name, ad_link, ad_text, ad_type,
     duration_days, start_at, end_at, status, created_at, total_views, total_clicks,
     r7, r3, r24, rex, renewed_from) = row

    ctr = f"{(total_clicks / total_views * 100):.1f}%" if total_views else "0%"

    try:
        start_dt = datetime.fromisoformat(start_at)
        end_dt = datetime.fromisoformat(end_at)
        active_days = max(0, (min(datetime.now(), end_dt) - start_dt).days)
        remaining_days = max(0, (end_dt - datetime.now()).days)
    except (ValueError, TypeError):
        active_days = remaining_days = 0

    text = f"""
📊 <b>Performance Report - #{sid}</b>

Business: {esc(business_name)}
Status: {STATUS_EMOJI.get(status, '•')} {esc(status)}

Total Views: {total_views}
Total Clicks: {total_clicks}
CTR: {ctr}
Active Days: {active_days}
Remaining Days: {remaining_days}
"""
    await update.message.reply_html(text)


# --- Exports (owner-only, CSV always available; XLSX if openpyxl is installed) ---

def _write_csv(path, header, rows):
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


async def _send_export(update, filename, header, rows):
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        path = tmp.name
    _write_csv(path, header, rows)
    with open(path, "rb") as f:
        await update.message.reply_document(document=f, filename=filename)


@owner_only
async def exportads_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.get_all_applications()
    await _send_export(
        update, "advertisements.csv",
        ["ID", "Applicant ID", "Applicant Name", "Username", "Business", "Link", "Type", "Duration", "Status", "Submitted"],
        rows,
    )


@owner_only
async def exportrevenue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.get_all_invoices()
    await _send_export(
        update, "revenue.csv",
        ["Invoice ID", "Business", "Advertiser", "Duration", "Amount Paid", "Payment Method", "Payment Date", "Currency"],
        rows,
    )


@owner_only
async def exportpayments_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.get_pending_payments()
    await _send_export(
        update, "payments.csv",
        ["Payment ID", "Application ID", "Submitted", "Business", "Applicant"],
        rows,
    )


# ---------------------------------------------------------------------
# /allcommands
# ---------------------------------------------------------------------

@owner_only
async def allcommands_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only categorized reference of every command registered in
    this bot. This list is maintained by hand alongside main() below -
    whenever a command is added there, it should be added here too so
    the two stay in sync. Never lists payment credentials/values, only
    command names."""
    text = """
📖 <b>UFFAdsBot Commands</b>

<b>PUBLIC</b>
/start - welcome menu
/submitad - submit an advertisement
/myads - your advertisements &amp; total spend
/receipts - your past invoices
/cancel - cancel an in-progress submission

<b>APPLICATIONS</b> (owner/admin)
/applications - pending applications (paginated)
/open &lt;id&gt; - view one application
/approved - approved applications
/rejected - rejected applications
/searchads &lt;query&gt; - search by user/business/link/ID (owner)
/broadcast &lt;everyone|approved|pending|rejected&gt; &lt;message&gt;
/approveall - approve every currently pending application (owner)
/rejectall [reason] - reject every currently pending application (owner)

<b>CONTACT</b> (owner)
/changecontactsupport &lt;@username or link&gt; - update the Contact Support button

<b>PAYMENT SETTINGS</b> (owner unless noted)
/paymentmethods - list configured payment methods (owner)
/addpayment &lt;name&gt; - add a bare payment method
/removepayment &lt;name&gt; - remove a payment method
/enablepayment &lt;name&gt; - enable a method
/disablepayment &lt;name&gt; - disable a method
/paymentmode single|multiple - one method shown vs. a picker
/setbank &lt;Bank&gt; | &lt;Account Name&gt; | &lt;Account Number&gt; - configure Bank Transfer
/setpayment &lt;Name&gt; | &lt;Field: Value&gt; | ... - configure ANY payment method
/setpaystack, /setflutterwave, /setcrypto - step-by-step config for those methods
/setstarsrecipient &lt;@username&gt; - Telegram Stars recipient
/setgiftsrecipient &lt;@username&gt; - Telegram Gifts recipient
/addstars, /addgifts, /fixcrypto - maintenance helpers

<b>ADVERTISEMENT RATES</b> (owner)
/setrate &lt;duration_days&gt; &lt;amount&gt; [currency] - add or update a rate
/deleterate &lt;duration_days&gt; - remove a rate
/rates - view all configured rates
/setrates - legacy single-rate setter (NGN only, kept for compatibility)

<b>SUBSCRIPTIONS &amp; BILLING</b> (owner/admin unless noted)
/subscriptions - list all ad subscriptions
/subscription &lt;id&gt; - view one subscription
/pausead, /resumead, /cancelad &lt;id&gt;
/extendad &lt;id&gt; &lt;days&gt;
/adreport &lt;id&gt; - performance report
/createcoupon, /coupons, /deletecoupon (owner)
/createoffer, /offers, /deleteoffer (owner)
/invoice &lt;id&gt; (owner)
/revenue today|week|month|year (owner)
/exportads, /exportrevenue, /exportpayments (owner)

<b>STATS &amp; OTHER</b> (owner)
/stats - advertisement stats
/allcommands - this list

<i>Bank details, API keys, and wallet addresses are never shown here - use /paymentmethods to see which methods are enabled without revealing their configured details.</i>
"""
    await update.message.reply_html(text)


# ---------------------------------------------------------------------
# Background jobs - scheduler, expiry, reminders
# ---------------------------------------------------------------------

async def job_activate_scheduled_ads(context: ContextTypes.DEFAULT_TYPE):
    for subscription_id in db.get_subscriptions_needing_activation():
        db.set_subscription_status(subscription_id, "active")
        logger.info(f"Activated subscription #{subscription_id}")

        # Same "go live" handoff as Start Immediately - now that this
        # Custom Start Time ad's scheduled moment has actually arrived,
        # drop it into the Sponsored Ad slot too.
        sub_row = db.get_subscription(subscription_id)
        if sub_row:
            ad_link, ad_text = sub_row[4], sub_row[5]
            _publish_to_sponsored_slot(ad_text, ad_link)


async def job_expire_ads(context: ContextTypes.DEFAULT_TYPE):
    for subscription_id in db.get_subscriptions_needing_expiry():
        db.set_subscription_status(subscription_id, "expired")
        logger.info(f"Expired subscription #{subscription_id}")


async def job_mark_expiring_soon(context: ContextTypes.DEFAULT_TYPE):
    for row in db.get_subscriptions_expiring_within(24):
        subscription_id = row[0]
        db.set_subscription_status(subscription_id, "expiring_soon")


async def job_send_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Checks every active/expiring_soon subscription against the
    7-day/3-day/24-hour/at-expiry reminder thresholds, sending each one
    at most once (tracked via the reminder_*_sent flags)."""
    now = datetime.now()

    for row in db.get_subscriptions_expiring_within(24 * 7 + 1):
        (subscription_id, applicant_id, business_name, end_at,
         r7, r3, r24, rex) = row

        try:
            end_dt = datetime.fromisoformat(end_at)
        except (ValueError, TypeError):
            continue

        remaining = end_dt - now
        hours_left = remaining.total_seconds() / 3600

        which = None
        if hours_left <= 0 and not rex:
            which = "expiry"
        elif hours_left <= 24 and not r24:
            which = "24h"
        elif hours_left <= 72 and not r3:
            which = "3d"
        elif hours_left <= 168 and not r7:
            which = "7d"

        if which is None:
            continue

        if which == "expiry":
            text = f"📢 Your advertisement for {business_name} has expired."
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Renew Advertisement", callback_data=f"renew:{subscription_id}"),
            ]])
        else:
            label = {"24h": "24 hours", "3d": "3 days", "7d": "7 days"}[which]
            text = f"📢 Your advertisement for {esc(business_name)} will expire in {label}.\n\nWould you like to renew it?"
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Renew Advertisement", callback_data=f"renew:{subscription_id}"),
                InlineKeyboardButton("❌ Not Now", callback_data="renew_dismiss"),
            ]])

        try:
            await context.bot.send_message(chat_id=applicant_id, text=text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Failed to send reminder for subscription #{subscription_id}: {e}")

        db.mark_reminder_sent(subscription_id, which)


async def renew_dismiss_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("No problem - you can renew any time from /myads.")


# ---------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log unhandled errors instead of letting them vanish silently -
    without this, a raised exception during e.g. the submission
    conversation just gets printed by PTB's default handling and the
    user never finds out their message was dropped. Logs only
    update_id/chat/user identifiers, never raw update content."""
    update_id = getattr(update, "update_id", "?")
    chat_id = getattr(getattr(update, "effective_chat", None), "id", "?")
    logger.error(
        f"Unhandled exception while processing update_id={update_id} chat_id={chat_id}",
        exc_info=context.error,
    )


def main():
    db.create_tables()

    app = (
    Application.builder()
    .token(BOT_TOKEN)
    .connect_timeout(30)
    .read_timeout(30)
    .write_timeout(30)
    .pool_timeout(30)
    .build()
)

    app.add_error_handler(error_handler)

    app.add_handler(submit_conversation)

    # NOTE: "start" is no longer registered as a separate CommandHandler
    # here - it's now one of submit_conversation's entry_points (above)
    # so that the ?start=submitad deep link's resulting ASK_LINK state
    # is properly tracked. A bare /start still works exactly as before;
    # start_command handles both cases.
    app.add_handler(CommandHandler("setrates", setrates_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("applications", applications_command))
    app.add_handler(CommandHandler("open", open_command))
    app.add_handler(CommandHandler("approved", approved_command))
    app.add_handler(CommandHandler("rejected", rejected_command))
    app.add_handler(CommandHandler("payments", payments_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("searchads", searchads_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("approveall", approveall_command))
    app.add_handler(CommandHandler("rejectall", rejectall_command))
    app.add_handler(CommandHandler("changecontactsupport", changecontactsupport_command))

    # Payment Management System
    app.add_handler(CommandHandler("paymentmethods", paymentmethods_command))
    app.add_handler(CommandHandler("addpayment", addpayment_command))
    app.add_handler(CommandHandler("removepayment", removepayment_command))
    app.add_handler(CommandHandler("enablepayment", enablepayment_command))
    app.add_handler(CommandHandler("disablepayment", disablepayment_command))
    app.add_handler(CommandHandler("paymentmode", paymentmode_command))
    app.add_handler(CommandHandler("setstarsrecipient", setstarsrecipient_command))
    app.add_handler(CommandHandler("setgiftsrecipient", setgiftsrecipient_command))
    app.add_handler(CommandHandler("setbank", setbank_command))
    app.add_handler(CommandHandler("setpayment", setpayment_command))
    app.add_handler(CommandHandler("setpaystack", setpaystack_command))
    app.add_handler(CommandHandler("setflutterwave", setflutterwave_command))
    app.add_handler(CommandHandler("setcrypto", setcrypto_command))
    app.add_handler(CommandHandler("addstars", addstars_command))
    app.add_handler(CommandHandler("addgifts", addgifts_command))
    app.add_handler(CommandHandler("fixcrypto", fixcrypto_command))

    # Advertisement Rates
    app.add_handler(CommandHandler("setrate", setrate_command))
    app.add_handler(CommandHandler("deleterate", deleterate_command))
    app.add_handler(CommandHandler("rates", rates_admin_command))

    # Subscription & Billing System
    app.add_handler(CommandHandler("createcoupon", createcoupon_command))
    app.add_handler(CommandHandler("coupons", coupons_command))
    app.add_handler(CommandHandler("deletecoupon", deletecoupon_command))
    app.add_handler(CommandHandler("createoffer", createoffer_command))
    app.add_handler(CommandHandler("offers", offers_command))
    app.add_handler(CommandHandler("deleteoffer", deleteoffer_command))
    app.add_handler(CommandHandler("invoice", invoice_command))
    app.add_handler(CommandHandler("receipts", receipts_command))
    app.add_handler(CommandHandler("revenue", revenue_command))
    app.add_handler(CommandHandler("myads", myads_command))
    app.add_handler(CommandHandler("subscriptions", subscriptions_command))
    app.add_handler(CommandHandler("subscription", subscription_command))
    app.add_handler(CommandHandler("pausead", pausead_command))
    app.add_handler(CommandHandler("resumead", resumead_command))
    app.add_handler(CommandHandler("extendad", extendad_command))
    app.add_handler(CommandHandler("cancelad", cancelad_command))
    app.add_handler(CommandHandler("adreport", adreport_command))
    app.add_handler(CommandHandler("exportads", exportads_command))
    app.add_handler(CommandHandler("exportrevenue", exportrevenue_command))
    app.add_handler(CommandHandler("exportpayments", exportpayments_command))

    app.add_handler(CommandHandler("allcommands", allcommands_command))

    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu:(rates|help)$"))
    app.add_handler(CallbackQueryHandler(decision_callback, pattern="^addecide:"))
    app.add_handler(CallbackQueryHandler(contact_advertiser_callback, pattern="^adcontact:"))
    app.add_handler(CallbackQueryHandler(payment_review_callback, pattern="^payreview:"))
    app.add_handler(CallbackQueryHandler(payment_decision_callback, pattern="^paydecide:"))
    app.add_handler(CallbackQueryHandler(paystart_callback, pattern="^paystart:"))
    app.add_handler(CallbackQueryHandler(payment_method_choice_callback, pattern="^paymethod:"))
    app.add_handler(
    CallbackQueryHandler(
        change_payment_method_callback,
        pattern="^paymethods:"
    )
)
    app.add_handler(CallbackQueryHandler(applications_page_callback, pattern="^adlist:"))
    app.add_handler(CallbackQueryHandler(renew_callback, pattern="^renew:"))
    app.add_handler(CallbackQueryHandler(renew_dismiss_callback, pattern="^renew_dismiss$"))
    # Renewal duration picks reuse the submit flow's adduration: pattern,
    # so this handler must be registered BEFORE the submit_conversation
    # would otherwise see it - but since submit_conversation only reacts
    # while a user is actually inside it, and renewal duration picks
    # happen outside that conversation, there's no real conflict; this
    # is registered explicitly for clarity and because
    # renew_duration_callback itself checks user_data before acting.
    app.add_handler(CallbackQueryHandler(renew_duration_callback, pattern="^adduration:"))

    # Reject-reason text, custom start-time text, payment-method config
    # answers, and coupon codes all arrive as plain private messages
    # outside the submit conversation, so they're funneled through one
    # dispatcher rather than as separate competing handlers.
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        private_text_dispatcher,
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.ALL),
        payment_proof_upload,
    ))

    # Background jobs - scheduler, expiry, reminders. Requires the
    # job-queue extra: pip install "python-telegram-bot[job-queue]"
    if app.job_queue is not None:
        app.job_queue.run_repeating(job_activate_scheduled_ads, interval=60, first=10)
        app.job_queue.run_repeating(job_expire_ads, interval=60, first=15)
        app.job_queue.run_repeating(job_mark_expiring_soon, interval=300, first=20)
        app.job_queue.run_repeating(job_send_reminders, interval=3600, first=30)
    else:
        logger.warning(
            "JobQueue is not available - scheduled ad activation/expiry/reminders "
            "will NOT run. Install with: pip install \"python-telegram-bot[job-queue]\""
        )

    print("📢 UFFAdsBot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()