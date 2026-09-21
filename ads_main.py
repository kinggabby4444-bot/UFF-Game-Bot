"""
@UFFAdsBot - the Advertisement Management Bot for @Uff_GameBot.
"""

import os
import re
import json
import asyncio
import html as html_lib
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.constants import ChatType
from telegram.error import TimedOut, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

import ads_database as db

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def safe_call(coro_func, *args, retries=2, delay=2, **kwargs):
    for attempt in range(retries + 1):
        try:
            return await coro_func(*args, **kwargs)
        except (TimedOut, NetworkError) as e:
            if attempt == retries:
                logger.error(f"Telegram API call failed after retries: {e}")
                return None
            await asyncio.sleep(delay)


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

OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "")


def is_owner(user_id) -> bool:
    return user_id == OWNER_ID


def is_owner_or_admin(user_id) -> bool:
    return is_owner(user_id) or db.is_bot_admin(user_id)


def esc(text) -> str:
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


CURRENCY_SYMBOLS = {"NGN": "₦", "USD": "$", "GBP": "£", "EUR": "€", "XTR": "⭐"}


def format_price(price, currency) -> str:
    currency = (currency or "NGN").upper()
    symbol = CURRENCY_SYMBOLS.get(currency)
    if symbol:
        return f"{symbol}{price}"
    return f"{price} {currency}"


(ASK_BUSINESS, ASK_LINK, ASK_TYPE, ASK_DURATION, ASK_TEXT, ASK_NOTES, PREVIEW) = range(7)

AD_TYPES = ["Group", "Channel", "Bot", "Business", "Website", "Other"]


def type_keyboard():
    row = [InlineKeyboardButton(t, callback_data=f"adtype:{t}") for t in AD_TYPES[:3]]
    row2 = [InlineKeyboardButton(t, callback_data=f"adtype:{t}") for t in AD_TYPES[3:]]
    return InlineKeyboardMarkup([row, row2])


def duration_keyboard():
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


def _support_contact_url():
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
    if not context.args:
        current = await asyncio.to_thread(db.get_config, "support_contact", OWNER_USERNAME) or "(not set)"
        await update.message.reply_text(
            "Usage: /changecontactsupport <@username or link>\n"
            f"Current: {current}"
        )
        return

    value = " ".join(context.args).strip()
    await asyncio.to_thread(db.set_config, "support_contact", value)
    await asyncio.to_thread(db.audit, update.effective_user.id, "change_contact_support", value)
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
    await update.message.reply_html(HELP_TEXT, reply_markup=main_menu_keyboard())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "menu:rates":
        await query.edit_message_text(rates_text(), parse_mode="HTML", reply_markup=main_menu_keyboard())
    elif query.data == "menu:help":
        await query.edit_message_text(HELP_TEXT, parse_mode="HTML", reply_markup=main_menu_keyboard())


@owner_only
async def setrates_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        current = "\n".join(f"{d} Day{'s' if d != 1 else ''} = {p}" for d, p in await asyncio.to_thread(db.get_rates))
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

    await asyncio.to_thread(db.set_rate, days, price)
    await update.message.reply_text(f"✅ Rate updated: {days} Day{'s' if days != 1 else ''} = ₦{price}")


@owner_only
async def setrate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    await asyncio.to_thread(db.set_rate, days, price, currency)
    await asyncio.to_thread(db.audit, update.effective_user.id, "set_rate", f"{days}d = {price} {currency}")

    await update.message.reply_text(
        f"✅ Rate saved: {days} Day{'s' if days != 1 else ''} = {format_price(price, currency)}\n"
        "This is now live on the advertisement submission menu."
    )


@owner_only
async def deleterate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /deleterate <duration_days>\nExample: /deleterate 14")
        return

    days = int(context.args[0])
    removed = await asyncio.to_thread(db.delete_rate, days)
    await asyncio.to_thread(db.audit, update.effective_user.id, "delete_rate", str(days))

    if removed:
        await update.message.reply_text(f"✅ Rate for {days} Day{'s' if days != 1 else ''} deleted.")
    else:
        await update.message.reply_text(f"❌ No rate configured for {days} Day{'s' if days != 1 else ''}.")


@owner_only
async def rates_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.get_rates_with_currency)
    if not rows:
        await update.message.reply_text("No advertisement rates configured yet. Use /setrate to add one.")
        return

    lines = ["📋 <b>Advertisement Rates</b>\n"]
    for days, price, currency in rows:
        lines.append(f"{days} Day{'s' if days != 1 else ''} — {format_price(price, currency)}")

    await update.message.reply_html("\n".join(lines))


# ---------------------------------------------------------------------------
# Native Telegram Stars payments (send_invoice -> pre_checkout_query ->
# successful_payment). Pricing tiers live in their own star_rates table so
# nothing that reads ad_rates has to change.
# ---------------------------------------------------------------------------

STARS_METHOD_NAME = "Telegram Stars"
STARS_PAYLOAD_PREFIX = "starpay:"


def _format_usd(value) -> str:
    if value is None:
        return "—"
    try:
        return "$" + format(Decimal(value).normalize(), "f")
    except (InvalidOperation, ValueError, TypeError):
        return f"${value}"


def _format_star_tier(days, usd_price, stars) -> str:
    return f"{days} Day{'s' if days != 1 else ''} — {_format_usd(usd_price)} — ⭐{stars}"


@owner_only
async def configurestarpaymentmethod_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    usage = (
        "Usage:\n"
        "/configurestarpaymentmethod - list current Stars tiers\n"
        "/configurestarpaymentmethod <duration_days> <usd_price> <stars> - add or update a tier\n"
        "/configurestarpaymentmethod <duration_days> remove - remove a tier\n\n"
        "Example: /configurestarpaymentmethod 7 20 800"
    )

    if not args:
        rows = await asyncio.to_thread(db.get_star_rates)
        if not rows:
            await update.message.reply_text("No Telegram Stars tiers configured yet.\n\n" + usage)
            return
        lines = ["⭐ Telegram Stars tiers\n"] + [_format_star_tier(d, u, st) for d, u, st in rows]
        await update.message.reply_text("\n".join(lines))
        return

    if not args[0].isdigit() or int(args[0]) <= 0:
        await update.message.reply_text("❌ duration_days must be a positive whole number.\n\n" + usage)
        return
    days = int(args[0])

    if len(args) == 2 and args[1].lower() == "remove":
        removed = await asyncio.to_thread(db.delete_star_rate, days)
        await asyncio.to_thread(db.audit, update.effective_user.id, "delete_star_rate", str(days))
        if removed:
            await update.message.reply_text(f"✅ Stars tier for {days} Day{'s' if days != 1 else ''} removed.")
        else:
            await update.message.reply_text(f"❌ No Stars tier configured for {days} Day{'s' if days != 1 else ''}.")
        return

    if len(args) != 3:
        await update.message.reply_text(usage)
        return

    try:
        usd_price = Decimal(args[1].lstrip("$"))
        if not usd_price.is_finite() or usd_price < 0:
            raise InvalidOperation
    except InvalidOperation:
        await update.message.reply_text("❌ usd_price must be a number like 20 or 12.50.\n\n" + usage)
        return

    if not args[2].isdigit() or int(args[2]) <= 0:
        await update.message.reply_text("❌ stars must be a positive whole number.\n\n" + usage)
        return
    stars = int(args[2])

    await asyncio.to_thread(db.set_star_rate, days, usd_price, stars)
    await asyncio.to_thread(
        db.audit, update.effective_user.id, "set_star_rate",
        f"{days}d = {_format_usd(usd_price)} / {stars} stars",
    )

    reply = f"✅ Stars tier saved: {_format_star_tier(days, usd_price, stars)}"
    if not await asyncio.to_thread(db.get_rate, days):
        reply += (
            f"\n\n⚠️ There's no advertisement rate for {days} Day{'s' if days != 1 else ''} yet, "
            f"so applicants can't pick that duration. Add one with /setrate {days} <amount>."
        )
    await update.message.reply_text(reply)


async def submit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if query:
        await query.answer()

    if await asyncio.to_thread(db.has_pending_application, user.id):
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

    data = context.user_data["ad_draft"]
    application_id = await asyncio.to_thread(db.create_application, 
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
        CommandHandler("start", start_command),
    ],
    name="submit_ad_conversation",
    per_message=False,
)


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
<a href="tg://user?id={applicant_id}">{esc(applicant_name)}</a>

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
    app_row = await asyncio.to_thread(db.get_application, application_id)
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
    methods = await asyncio.to_thread(db.get_enabled_payment_methods)
    mode = await asyncio.to_thread(db.get_config, "payment_mode", "multiple")

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
        if name == STARS_METHOD_NAME:
            # No picker in single-method mode, so the usual "pick a different
            # method" advice doesn't apply - point them at support instead.
            error = await start_stars_payment(context, application_id)
            if error:
                await context.bot.send_message(
                    chat_id=applicant_id,
                    text=f"⭐ Telegram Stars\n\n{error}\n\nPlease contact support to arrange payment.",
                )
            return
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
    query = update.callback_query
    await query.answer()

    _, aid_str, idx_str = query.data.split(":")
    methods = await asyncio.to_thread(db.get_enabled_payment_methods)
    idx = int(idx_str)

    if idx >= len(methods):
        await query.edit_message_text("That payment method is no longer available - please ask for the list again.")
        return

    application_id = int(aid_str)
    app_row = await asyncio.to_thread(db.get_application, application_id)

    rate_row = await asyncio.to_thread(db.get_rate_with_currency, app_row[7]) if app_row else None
    currency = rate_row[1] if rate_row else "NGN"
    price = (app_row[16] if app_row and app_row[16] is not None
             else (rate_row[0] if rate_row else 0))

    method_name = methods[idx][0]

    if method_name == STARS_METHOD_NAME:
        error = await start_stars_payment(context, application_id)
        if error:
            text = f"⭐ Telegram Stars\n\n{error}\n\nPlease choose a different payment method."
        else:
            text = (
                "⭐ Telegram Stars\n\n"
                "Your invoice is right below - tap its Pay button to complete the payment. "
                "It's confirmed instantly, no screenshot needed."
            )
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🔙 Change Payment Method",
                    callback_data=f"paymethods:{application_id}"
                )]
            ]),
        )
        return

    if str(method_name).strip().lower() == "telegram gifts":
        recipient = await asyncio.to_thread(db.get_config, "gifts_recipient", "")
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


async def _alert_owner(context, text):
    """Best-effort plain-text message to the owner; never raises."""
    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=text)
    except Exception as e:
        logger.error(f"Failed to alert owner: {e}")


def _parse_star_payload(payload):
    """payment_id encoded in an invoice payload ("starpay:<id>"), or None."""
    if not payload or not payload.startswith(STARS_PAYLOAD_PREFIX):
        return None
    rest = payload[len(STARS_PAYLOAD_PREFIX):]
    return int(rest) if rest.isdigit() else None


async def start_stars_payment(context, application_id):
    """Send the applicant a native Telegram Stars invoice for this application.

    Returns None when the invoice was sent, or a short user-facing reason it
    couldn't be (callers decide what to offer next)."""
    app_row = await asyncio.to_thread(db.get_application, application_id)
    if not app_row:
        return "This application could not be found."

    applicant_id, business_name, duration_days, status = app_row[1], app_row[4], app_row[7], app_row[10]

    if status != "approved":
        return "There's no payment due for this application right now."

    tier = await asyncio.to_thread(db.get_star_rate, duration_days)
    if not tier:
        return f"Telegram Stars isn't available for the {duration_days}-day duration."
    _usd_price, stars = tier

    payment_id = await asyncio.to_thread(db.get_or_create_star_payment, application_id)

    day_word = "Day" if duration_days == 1 else "Days"
    title = f"{duration_days} {day_word} Advertisement"
    description = f"Advertisement for {business_name} - {duration_days} {day_word}"

    try:
        await context.bot.send_invoice(
            chat_id=applicant_id,
            title=title[:32],
            description=description[:255],
            payload=f"{STARS_PAYLOAD_PREFIX}{payment_id}",
            provider_token="",  # must be empty for Telegram Stars
            currency="XTR",
            # Stars amounts are the raw star count - NOT multiplied by 100 like
            # normal currencies - and Stars invoices allow exactly one price line.
            prices=[LabeledPrice(label=title[:32], amount=int(stars))],
        )
    except Exception as e:
        logger.error(f"Failed to send Stars invoice for application {application_id}: {e}")
        await _alert_owner(
            context,
            f"⚠️ Couldn't send a Stars invoice for Application #{application_id} "
            f"({stars} stars, {duration_days}d): {e}",
        )
        return "Couldn't create the Stars invoice right now. Please try again in a moment."

    return None


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telegram waits ~10 seconds for this answer, so it must always answer -
    an unhandled exception here would show the payer a vague failure."""
    query = update.pre_checkout_query

    async def refuse(reason):
        await query.answer(ok=False, error_message=reason)

    try:
        payment_id = _parse_star_payload(query.invoice_payload)
        if payment_id is None or query.currency != "XTR":
            await refuse("This payment isn't recognised. Please request a new invoice.")
            return

        info = await asyncio.to_thread(db.get_payment_star_info, payment_id)
        if not info:
            await refuse("This invoice no longer exists. Please request a new one.")
            return

        _pid, application_id, status, payment_method, _charge_id, _amt, _cur = info
        if payment_method != STARS_METHOD_NAME:
            await refuse("This payment isn't recognised. Please request a new invoice.")
            return
        if status != "pending":
            await refuse("This invoice has already been paid or is no longer valid.")
            return

        app_row = await asyncio.to_thread(db.get_application, application_id)
        if not app_row or app_row[10] != "approved":
            await refuse("This advertisement is no longer awaiting payment.")
            return

        # Refunds go back to the applicant's own account, so only they may pay.
        if query.from_user.id != app_row[1]:
            await refuse("This invoice belongs to a different account.")
            return

        await query.answer(ok=True)
    except Exception as e:
        logger.error(f"pre_checkout_query failed: {e}", exc_info=True)
        try:
            await query.answer(ok=False, error_message="Something went wrong. Please try again in a moment.")
        except Exception:
            pass


async def send_start_time_choice(context, chat_id, payment_id, application_id):
    """Ask the owner/admin when a paid-for advertisement should go live.
    Shared by the manual "Payment Confirmed" button and the automatic Stars path."""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("▶ Start Immediately", callback_data=f"paystart:{payment_id}:immediate"),
        InlineKeyboardButton("🗓 Custom Start Time", callback_data=f"paystart:{payment_id}:custom"),
    ]])
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"When should Application #{application_id}'s advertisement go live?",
        reply_markup=keyboard,
    )


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    payment = message.successful_payment
    payment_id = _parse_star_payload(payment.invoice_payload)
    if payment_id is None:
        logger.warning(f"successful_payment with unrecognised payload {payment.invoice_payload!r}")
        return

    charge_id = payment.telegram_payment_charge_id
    payer_id = message.from_user.id

    try:
        recorded = await asyncio.to_thread(
            db.confirm_star_payment, payment_id, charge_id, payment.total_amount, payment.currency
        )

        if not recorded:
            # The row wasn't 'pending'. Either Telegram redelivered an update we
            # already handled (same charge id - nothing to do), or the payer was
            # charged for something we can't fulfil - refund that charge in full.
            info = await asyncio.to_thread(db.get_payment_star_info, payment_id)
            if info and info[4] == charge_id:
                return
            try:
                await context.bot.refund_star_payment(user_id=payer_id, telegram_payment_charge_id=charge_id)
                outcome = "It was refunded automatically."
                await context.bot.send_message(
                    chat_id=payer_id,
                    text="⚠️ That payment couldn't be applied (this invoice was already settled), "
                         "so it has been refunded to you in full.",
                )
            except Exception as e:
                outcome = f"AUTOMATIC REFUND FAILED ({e}) - refund it manually."
            await _alert_owner(
                context,
                f"⚠️ Stars charge that couldn't be recorded against Payment #{payment_id} "
                f"(status: {info[2] if info else 'missing'}).\nCharge ID: {charge_id}\n{outcome}",
            )
            return

        info = await asyncio.to_thread(db.get_payment_star_info, payment_id)
        application_id = info[1]
        app_row = await asyncio.to_thread(db.get_application, application_id)

        # Same state the manual flow is in once an owner has confirmed a payment
        # but hasn't chosen a start time yet; finalize_subscription moves it on.
        await asyncio.to_thread(db.set_application_status, application_id, "awaiting_payment")
        await asyncio.to_thread(
            db.audit, None, "stars_payment_confirmed",
            f"Payment #{payment_id}, Application #{application_id}, {payment.total_amount} XTR, charge {charge_id}",
        )
    except Exception as e:
        logger.error(f"Failed to process successful Stars payment {payment_id}: {e}", exc_info=True)
        await _alert_owner(
            context,
            f"🚨 A Stars payment went through but the bot failed to record it: {e}\n"
            f"Payment #{payment_id}\nCharge ID: {charge_id}\nPayer ID: {payer_id}",
        )
        return

    try:
        await context.bot.send_message(
            chat_id=payer_id,
            text=(
                f"✅ Payment received - {format_price(payment.total_amount, payment.currency)}. Thank you!\n\n"
                "Your advertisement is being scheduled. You'll get your receipt as soon as its start time is set."
            ),
        )
    except Exception as e:
        logger.error(f"Failed to confirm Stars payment {payment_id} to payer {payer_id}: {e}")

    business_name = app_row[4] if app_row else "—"
    applicant_name = app_row[2] if app_row else "—"
    applicant_username = f"@{app_row[3]}" if app_row and app_row[3] else "—"
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                "⭐ <b>Telegram Stars payment received automatically</b>\n\n"
                f"Payment #{payment_id} - Application #{application_id}\n"
                f"Business: {esc(business_name)}\n"
                f"Advertiser: {esc(applicant_name)} ({esc(applicant_username)})\n"
                f"Amount: {format_price(payment.total_amount, payment.currency)}\n"
                f"Charge ID: <code>{esc(charge_id)}</code>\n\n"
                "No manual confirmation is needed - Telegram already confirmed this payment. "
                "Choose a start time in the next message."
            ),
            parse_mode="HTML",
        )
        await send_start_time_choice(context, OWNER_ID, payment_id, application_id)
    except Exception as e:
        logger.error(f"Failed to notify owner of Stars payment {payment_id}: {e}")


@owner_or_admin_only
async def refundstars_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /refundstars <payment_id>")
        return

    payment_id = int(context.args[0])
    info = await asyncio.to_thread(db.get_payment_star_info, payment_id)
    if not info:
        await update.message.reply_text("❌ No payment with that ID.")
        return

    _pid, application_id, status, _method, charge_id, paid_amount, paid_currency = info

    if not charge_id:
        await update.message.reply_text(
            f"❌ Payment #{payment_id} wasn't paid through native Telegram Stars, "
            "so it can't be refunded with this command."
        )
        return
    if status == "refunded":
        await update.message.reply_text(f"⚠️ Payment #{payment_id} has already been refunded.")
        return

    app_row = await asyncio.to_thread(db.get_application, application_id)
    if not app_row:
        await update.message.reply_text(f"❌ Application #{application_id} for this payment was not found.")
        return
    applicant_id = app_row[1]

    note = ""
    try:
        # Stars refunds are always for the full amount - there is no partial option.
        await context.bot.refund_star_payment(user_id=applicant_id, telegram_payment_charge_id=charge_id)
    except TelegramError as e:
        if "already_refunded" in str(e).lower():
            note = "\n(Telegram reports this charge was already refunded - records updated to match.)"
        else:
            logger.error(f"refund_star_payment failed for payment {payment_id}: {e}")
            await update.message.reply_text(f"❌ Telegram refused the refund: {e}")
            return

    await asyncio.to_thread(db.set_payment_refunded, payment_id)

    # Cancelling is the same mechanism /cancelad uses; get_active_ads_for_rotation
    # only serves 'active'/'expiring_soon' subscriptions, so this pulls it out.
    cancelled = await asyncio.to_thread(db.get_subscription_ids_for_application, application_id)
    for subscription_id in cancelled:
        await asyncio.to_thread(db.set_subscription_status, subscription_id, "cancelled")

    await asyncio.to_thread(
        db.audit, update.effective_user.id, "refund_stars_payment",
        f"Payment #{payment_id}, Application #{application_id}, charge {charge_id}"
        + (f", cancelled subscription(s) {cancelled}" if cancelled else ""),
    )

    amount_text = format_price(paid_amount, paid_currency or "XTR") if paid_amount is not None else "your payment"
    try:
        await context.bot.send_message(
            chat_id=applicant_id,
            text=(
                f"💸 Your Telegram Stars payment ({amount_text}) for Application #{application_id} "
                "has been refunded in full."
                + (" Your advertisement has been cancelled." if cancelled else "")
            ),
        )
    except Exception as e:
        logger.error(f"Failed to notify applicant {applicant_id} of refund: {e}")

    sub_text = (
        f"\nSubscription(s) marked cancelled: {', '.join('#' + str(i) for i in cancelled)}"
        if cancelled else "\nNo subscription had been created from this payment yet."
    )
    await update.message.reply_text(f"✅ Payment #{payment_id} refunded and the advertiser notified.{sub_text}{note}")


async def _payment_finalize_overrides(payment_id):
    """kwargs for finalize_subscription: {} for ordinary payments, the real Stars
    amount/currency/method for native Stars payments, or None if the payment has
    been refunded (its ad must not be started)."""
    info = await asyncio.to_thread(db.get_payment_star_info, payment_id)
    if not info:
        return {}
    _pid, _aid, status, _method, charge_id, paid_amount, paid_currency = info
    if status == "refunded":
        return None
    if charge_id:
        return {
            "override_amount": paid_amount,
            "override_currency": paid_currency or "XTR",
            "override_method_name": STARS_METHOD_NAME,
        }
    return {}


async def change_payment_method_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    application_id = int(query.data.split(":", 1)[1])
    methods = await asyncio.to_thread(db.get_enabled_payment_methods)

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
    await asyncio.to_thread(db.set_application_status, application_id, "approved", decided_by=actor_id)
    await asyncio.to_thread(db.audit, actor_id, "approve_application", f"Application #{application_id}")

    app_row = await asyncio.to_thread(db.get_application, application_id)
    if not app_row:
        return False

    applicant_id = app_row[1]
    duration_days = app_row[7]
    rate_row = await asyncio.to_thread(db.get_rate_with_currency, duration_days)
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
    await asyncio.to_thread(db.set_application_status, application_id, "rejected", decided_by=actor_id, reject_reason=reason)
    await asyncio.to_thread(db.audit, actor_id, "reject_application", f"Application #{application_id}: {reason}")

    app_row = await asyncio.to_thread(db.get_application, application_id)
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
    app_row = await asyncio.to_thread(db.get_application, application_id)

    if not app_row:
        await query.answer("❌ Application not found.", show_alert=True)
        return

    applicant_id = app_row[1]

    if action == "approve":
        await approve_application(context, application_id, user.id)
        await query.answer()
        await query.edit_message_text(
            application_detail_text(await asyncio.to_thread(db.get_application, application_id)) + "\n✅ <b>APPROVED</b>",
            parse_mode="HTML",
        )
        return

    if action == "reject":
        context.user_data["reject_application_id"] = application_id
        await query.answer()
        await query.edit_message_text(
            f"Application #{application_id}: please type the rejection reason to send to the applicant."
        )
        return


@owner_only
async def approveall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ids = await asyncio.to_thread(db.get_pending_application_ids)
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
    ids = await asyncio.to_thread(db.get_pending_application_ids)
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
    application_id = context.user_data.pop("reject_application_id", None)
    if application_id is None:
        return False

    reason = update.message.text.strip()
    await reject_application(context, application_id, update.effective_user.id, reason)
    await update.message.reply_text(f"✅ Application #{application_id} rejected and the applicant has been notified.")

    return True


async def coupon_code_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()

    app_row = await asyncio.to_thread(db.get_latest_approved_application, user.id)
    if not app_row:
        return False

    code = text.upper()
    coupon_row = await asyncio.to_thread(db.get_coupon, code)
    if not coupon_row:
        return False

    valid, reason = await asyncio.to_thread(db.is_coupon_valid, code)
    if not valid:
        await update.message.reply_text(f"❌ {reason}")
        return True

    application_id = app_row[0]
    duration_days = app_row[7]
    rate_row = await asyncio.to_thread(db.get_rate_with_currency, duration_days)
    base_price, currency = rate_row if rate_row else (0, "NGN")
    discounted_price = await asyncio.to_thread(db.apply_coupon_to_price, code, base_price)

    await asyncio.to_thread(db.set_application_coupon, application_id, code, discounted_price)

    await update.message.reply_text(
        f"✅ Coupon {esc(code)} applied! New price: {format_price(discounted_price, currency)} "
        f"(was {format_price(base_price, currency)})."
    )
    await send_payment_instructions(context, application_id, user.id, discounted_price, currency)
    return True


async def private_text_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    app_row = await asyncio.to_thread(db.get_application, application_id)

    if not app_row:
        await query.answer("❌ Application not found.", show_alert=True)
        return

    applicant_username = app_row[3]
    applicant_id = app_row[1]

    if applicant_username:
        await query.answer(f"Message them at https://t.me/{applicant_username}", show_alert=True)
    else:
        await query.answer(f"No public username - their Telegram ID is {applicant_id}", show_alert=True)


def payment_decision_keyboard(payment_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Payment Confirmed", callback_data=f"paydecide:confirm:{payment_id}"),
        InlineKeyboardButton("❌ Payment Rejected", callback_data=f"paydecide:reject:{payment_id}"),
    ]])


def payment_review_keyboard(payment_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("▶️ Next", callback_data=f"payreview:next:{payment_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"payreview:decline:{payment_id}"),
    ]])


async def payment_proof_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    app_row = await asyncio.to_thread(db.get_latest_approved_application, user.id)

    if not app_row:
        return

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

    payment_id = await asyncio.to_thread(db.create_payment, application_id, file_id, file_kind)
    await asyncio.to_thread(db.set_application_status, application_id, "awaiting_payment")

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
    query = update.callback_query
    user = update.effective_user

    if not is_owner_or_admin(user.id):
        await query.answer("🚫 Owner/admin only.", show_alert=True)
        return

    _, action, pid_str = query.data.split(":")
    payment_id = int(pid_str)
    payment_row = await asyncio.to_thread(db.get_payment, payment_id)

    if not payment_row:
        await query.answer("❌ Payment record not found.", show_alert=True)
        return

    application_id = payment_row[1]
    app_row = await asyncio.to_thread(db.get_application, application_id)
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

    await asyncio.to_thread(db.set_payment_status, payment_id, "rejected", confirmed_by=user.id)
    await asyncio.to_thread(db.audit, user.id, "decline_payment_proof", f"Payment #{payment_id}, Application #{application_id}")
    if application_id:
        await asyncio.to_thread(db.set_application_status, application_id, "approved", decided_by=user.id)

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
    payment_row = await asyncio.to_thread(db.get_payment, payment_id)

    if not payment_row:
        await query.answer("❌ Payment record not found.", show_alert=True)
        return

    application_id = payment_row[1]
    app_row = await asyncio.to_thread(db.get_application, application_id)
    applicant_id = app_row[1] if app_row else None

    await query.answer()

    if action == "confirm":
        await asyncio.to_thread(db.set_payment_status, payment_id, "confirmed", confirmed_by=user.id)
        await asyncio.to_thread(db.audit, user.id, "confirm_payment", f"Payment #{payment_id}, Application #{application_id}")
        try:
            await query.edit_message_caption(
                caption=(query.message.caption or "") + "\n\n✅ CONFIRMED - choose a start time below"
            )
        except Exception:
            pass

        await send_start_time_choice(context, user.id, payment_id, application_id)
    else:
        await asyncio.to_thread(db.set_payment_status, payment_id, "rejected", confirmed_by=user.id)
        await asyncio.to_thread(db.set_application_status, application_id, "payment_rejected", decided_by=user.id)
        await asyncio.to_thread(db.audit, user.id, "reject_payment", f"Payment #{payment_id}, Application #{application_id}")
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
    overrides = await _payment_finalize_overrides(payment_id)
    if overrides is None:
        await query.edit_message_text("⚠️ This payment was refunded, so its advertisement can't be started.")
        return
    subscription_id, status_value = await finalize_subscription(
        context, payment_id, datetime.now(), user.id, **overrides
    )
    state_note = "🟢 LIVE now" if status_value == "active" else "🟡 scheduled"
    await query.edit_message_text(
        f"✅ Advertisement scheduled to start immediately.\n\n"
        f"Subscription #{subscription_id} - {state_note}.\n"
        f"Check with /subscription {subscription_id} any time, or it should already be "
        f"rotating into @Uff_GameBot's /leaderboard, /topplayers, and /topgroups."
    )


async def custom_start_time_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment_id = context.user_data.pop("awaiting_custom_start_for_payment", None)
    if payment_id is None:
        return False

    text = update.message.text.strip()
    try:
        start_at = datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        context.user_data["awaiting_custom_start_for_payment"] = payment_id
        await update.message.reply_text(
            "❌ Couldn't parse that. Please send it as YYYY-MM-DD HH:MM, e.g. 2026-08-10 14:00."
        )
        return True

    overrides = await _payment_finalize_overrides(payment_id)
    if overrides is None:
        await update.message.reply_text("⚠️ This payment was refunded, so its advertisement can't be started.")
        return True
    subscription_id, status_value = await finalize_subscription(
        context, payment_id, start_at, update.effective_user.id, **overrides
    )
    state_note = "🟢 LIVE now" if status_value == "active" else "🟡 scheduled"
    await update.message.reply_text(
        f"✅ Advertisement scheduled to start {start_at.strftime('%Y-%m-%d %H:%M')}.\n\n"
        f"Subscription #{subscription_id} - {state_note}. Check with /subscription {subscription_id} any time."
    )
    return True


def _publish_to_sponsored_slot(ad_text, ad_link):
    db.set_config("sponsored_ad_override", f"[{ad_text}]({ad_link})")
    db.set_config("sponsored_ad_enabled", "1")
    db.set_config("ads_enabled", "1")


async def finalize_subscription(context, payment_id, start_at, actor_id,
                                override_amount=None, override_currency=None, override_method_name=None):
    payment_row = await asyncio.to_thread(db.get_payment, payment_id)
    application_id = payment_row[1]
    app_row = await asyncio.to_thread(db.get_application, application_id)

    (aid, applicant_id, applicant_name, applicant_username, business_name,
     ad_link, ad_type, duration_days, ad_text, notes, status,
     reject_reason, submitted_at, decided_at, decided_by,
     coupon_code, final_price) = app_row

    rate_row = await asyncio.to_thread(db.get_rate_with_currency, duration_days)
    base_price, currency = rate_row if rate_row else (0, "NGN")
    amount_paid = final_price if final_price is not None else base_price
    if override_amount is not None:
        amount_paid = override_amount
    if override_currency is not None:
        currency = override_currency

    now = datetime.now()
    end_at = start_at + timedelta(days=duration_days)
    status_value = "active" if start_at <= now else "scheduled"

    subscription_id = await asyncio.to_thread(db.create_subscription, 
        application_id=application_id, applicant_id=applicant_id, business_name=business_name,
        ad_link=ad_link, ad_text=ad_text, ad_type=ad_type, duration_days=duration_days,
        start_at=start_at.isoformat(), end_at=end_at.isoformat(), status=status_value,
    )

    if status_value == "active":
        _publish_to_sponsored_slot(ad_text, ad_link)

    await asyncio.to_thread(db.set_application_status, application_id, "payment_confirmed", decided_by=actor_id)

    if override_method_name is not None:
        method_name = override_method_name
    else:
        methods = await asyncio.to_thread(db.get_enabled_payment_methods)
        method_name = methods[0][0] if len(methods) == 1 else "Unspecified"

    invoice_id = await asyncio.to_thread(db.create_invoice, 
        application_id=application_id, subscription_id=subscription_id,
        business_name=business_name, advertiser_name=applicant_name,
        advertiser_username=applicant_username, advertiser_id=applicant_id,
        duration_days=duration_days, amount_paid=amount_paid, payment_method=method_name,
        ad_start=start_at.isoformat(), ad_end=end_at.isoformat(), currency=currency,
    )

    if coupon_code and override_amount is None:
        await asyncio.to_thread(db.redeem_coupon, coupon_code)

    await asyncio.to_thread(db.audit, actor_id, "activate_subscription",
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
    total = await asyncio.to_thread(db.count_applications_by_status, status)
    offset = page * APPLICATIONS_PAGE_SIZE
    rows = await asyncio.to_thread(db.get_applications_by_status, status, offset, APPLICATIONS_PAGE_SIZE)

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

    app_row = await asyncio.to_thread(db.get_application, int(context.args[0]))
    if not app_row:
        await update.message.reply_text("❌ No application with that ID.")
        return

    await update.message.reply_html(
        application_detail_text(app_row),
        reply_markup=decision_keyboard(app_row[0]) if app_row[10] == "pending" else None,
    )


@owner_or_admin_only
async def payments_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.get_pending_payments)
    if not rows:
        await update.message.reply_text("No payments waiting for verification.")
        return

    lines = [f"💳 <b>Payments Awaiting Verification</b> ({await asyncio.to_thread(db.count_pending_payments)} total)\n"]
    for pid, application_id, submitted_at, business_name, applicant_name in rows:
        lines.append(f"Payment #{pid} - Application #{application_id} - {esc(business_name)} ({esc(applicant_name)})")

    await update.message.reply_html("\n".join(lines))


@owner_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = await asyncio.to_thread(db.get_stats)
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
    rows = await asyncio.to_thread(db.search_applications, query)

    if not rows:
        await update.message.reply_text("No matching applications found.")
        return

    lines = [f"🔎 <b>Search Results</b> ({len(rows)})\n"]
    lines.extend(format_application_row(r) for r in rows[:50])
    if len(rows) > 50:
        lines.append(f"\n<i>+{len(rows) - 50} more - refine your search.</i>")

    await update.message.reply_html("\n".join(lines))


PAYMENT_METHOD_FIELDS = {
    "bank": ["Bank Name", "Account Name", "Account Number"],
    "paystack": ["Public Key", "Secret Key", "Webhook Secret"],
    "flutterwave": ["Public Key", "Secret Key", "Webhook Secret"],
    "telegram_stars": [],
    "telegram_gifts": [],
}


@owner_only
async def paymentmethods_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    methods = await asyncio.to_thread(db.get_payment_methods)
    if not methods:
        await update.message.reply_text("💳 No payment methods have been added yet. Use /addpayment <name>.")
        return

    mode = await asyncio.to_thread(db.get_config, "payment_mode", "multiple")
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
    added = await asyncio.to_thread(db.add_payment_method, name)
    await asyncio.to_thread(db.audit, update.effective_user.id, "add_payment_method", name)

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
    removed = await asyncio.to_thread(db.remove_payment_method, name)
    await asyncio.to_thread(db.audit, update.effective_user.id, "remove_payment_method", name)

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
    updated = await asyncio.to_thread(db.set_payment_method_enabled, name, True)
    await asyncio.to_thread(db.audit, update.effective_user.id, "enable_payment_method", name)

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
    updated = await asyncio.to_thread(db.set_payment_method_enabled, name, False)
    await asyncio.to_thread(db.audit, update.effective_user.id, "disable_payment_method", name)

    if updated:
        await update.message.reply_text(f"✅ {name} is now disabled.")
    else:
        await update.message.reply_text(f"❌ No payment method named '{name}' found.")


@owner_only
async def paymentmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() not in ("single", "multiple"):
        current = await asyncio.to_thread(db.get_config, "payment_mode", "multiple")
        await update.message.reply_text(f"Usage: /paymentmode single|multiple\nCurrent mode: {current}")
        return

    mode = context.args[0].lower()
    await asyncio.to_thread(db.set_config, "payment_mode", mode)
    await asyncio.to_thread(db.audit, update.effective_user.id, "set_payment_mode", mode)
    await update.message.reply_text(f"✅ Payment mode set to: {mode}")


@owner_only
async def setstarsrecipient_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setstarsrecipient @username")
        return

    username = context.args[0].lstrip("@")
    await asyncio.to_thread(db.set_config, "stars_recipient", username)
    await asyncio.to_thread(db.audit, update.effective_user.id, "set_stars_recipient", username)

    await update.message.reply_text(
        f"✅ Telegram Stars recipient set to @{username}"
    )


@owner_only
async def setgiftsrecipient_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setgiftsrecipient @username")
        return

    username = context.args[0].lstrip("@")
    await asyncio.to_thread(db.set_config, "gifts_recipient", username)
    await asyncio.to_thread(db.audit, update.effective_user.id, "set_gifts_recipient", username)

    await update.message.reply_text(
        f"✅ Telegram Gifts recipient set to @{username}"
    )


async def _start_payment_config(update, context, method_key):
    fields = PAYMENT_METHOD_FIELDS[method_key]
    context.user_data["payment_config"] = {"method": method_key, "fields": fields, "answers": {}, "index": 0}
    await update.message.reply_text(f"Configuring {method_key.capitalize()}.\n\n{fields[0]}:")


@owner_only
async def setbank_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    await asyncio.to_thread(db.add_payment_method, "Bank Transfer")
    await asyncio.to_thread(db.set_payment_method_config, "Bank Transfer", json.dumps({
        "Bank Name": bank_name,
        "Account Name": account_name,
        "Account Number": account_number,
    }))
    await asyncio.to_thread(db.set_payment_method_enabled, "Bank Transfer", True)
    await asyncio.to_thread(db.audit, update.effective_user.id, "configure_payment_method", "Bank Transfer")

    await update.message.reply_text(
        "✅ Bank Transfer details saved and enabled.\n\n"
        f"Bank Name: {bank_name}\n"
        f"Account Name: {account_name}\n"
        f"Account Number: {account_number}"
    )


@owner_only
async def setpayment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    await asyncio.to_thread(db.add_payment_method, method_name)
    await asyncio.to_thread(db.set_payment_method_config, method_name, json.dumps(config))
    await asyncio.to_thread(db.set_payment_method_enabled, method_name, True)
    await asyncio.to_thread(db.audit, update.effective_user.id, "configure_payment_method", method_name)

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
    updated = await asyncio.to_thread(db.rename_payment_method, 
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
    added = await asyncio.to_thread(db.add_payment_method, "Telegram Stars")
    stars_recipient = await asyncio.to_thread(db.get_config, "stars_recipient", "")
    await asyncio.to_thread(db.set_payment_method_config,
        "Telegram Stars",
        json.dumps({
            "recipient": stars_recipient
        })
    )
    await asyncio.to_thread(db.audit, update.effective_user.id, "add_payment_method", "Telegram Stars")

    if added:
        await update.message.reply_text("✅ Telegram Stars added as a payment method.")
    else:
        await update.message.reply_text("⚠️ Telegram Stars is already a payment method.")


@owner_only
async def addgifts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    added = await asyncio.to_thread(db.add_payment_method, "Telegram Gifts")
    gifts_recipient = await asyncio.to_thread(db.get_config, "gifts_recipient", "")
    await asyncio.to_thread(db.set_payment_method_config,
        "Telegram Gifts",
        json.dumps({
            "recipient": gifts_recipient
        })
    )
    await asyncio.to_thread(db.audit, update.effective_user.id, "add_payment_method", "Telegram Gifts")

    if added:
        await update.message.reply_text("✅ Telegram Gifts added as a payment method.")
    else:
        await update.message.reply_text("⚠️ Telegram Gifts is already a payment method.")


async def payment_config_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    method_key = state["method"]
    await asyncio.to_thread(db.add_payment_method, method_key)
    await asyncio.to_thread(db.set_payment_method_config, method_key, json.dumps(state["answers"]))
    await asyncio.to_thread(db.audit, update.effective_user.id, "configure_payment_method", method_key)
    context.user_data.pop("payment_config", None)

    await update.message.reply_text(f"✅ {method_key.capitalize()} configured and enabled.")
    return True


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
    recipient_ids = await asyncio.to_thread(db.get_all_advertiser_ids, status_filter)

    sent = 0
    failed = 0
    for uid in recipient_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=message_text)
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(f"📣 Broadcast sent to {sent} recipient(s). {failed} failed to deliver.")


STATUS_EMOJI = {
    "active": "🟢", "scheduled": "🟡", "expiring_soon": "🟠",
    "expired": "🔴", "paused": "⏸", "cancelled": "❌",
}


def subscription_summary_line(row) -> str:
    sid, applicant_id, business_name, duration_days, start_at, end_at, status = row
    emoji = STATUS_EMOJI.get(status, "•")
    return f"{emoji} #{sid} - {esc(business_name)} ({duration_days}d, {esc(status)})"


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

    await asyncio.to_thread(db.create_coupon, code, discount_type, discount_value, expires_at, max_uses, update.effective_user.id)
    await asyncio.to_thread(db.audit, update.effective_user.id, "create_coupon", code)

    await update.message.reply_text(f"✅ Coupon {code} created.")


@owner_only
async def coupons_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coupons = await asyncio.to_thread(db.get_all_coupons)
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
    removed = await asyncio.to_thread(db.delete_coupon, code)
    await asyncio.to_thread(db.audit, update.effective_user.id, "delete_coupon", code)
    await update.message.reply_text(f"✅ Coupon {code} deleted." if removed else f"❌ No coupon named {code}.")


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
    offer_id = await asyncio.to_thread(db.create_offer, description, start_at, end_at, update.effective_user.id)
    await asyncio.to_thread(db.audit, update.effective_user.id, "create_offer", f"#{offer_id}: {description}")

    await update.message.reply_text(f"✅ Offer #{offer_id} created: {description}")


@owner_only
async def offers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    offers = await asyncio.to_thread(db.get_all_offers)
    if not offers:
        await update.message.reply_text("No promotional offers have been created yet.")
        return

    lines = ["🎁 <b>Promotional Offers</b>\n"]
    active_ids = {o[0] for o in await asyncio.to_thread(db.get_active_offers)}
    for oid, description, start_at, end_at in offers:
        tag = "🟢 active" if oid in active_ids else "⚪ inactive"
        lines.append(f"#{oid} [{tag}] {esc(description)} ({start_at[:10]} to {end_at[:10]})")

    await update.message.reply_html("\n".join(lines))


@owner_only
async def deleteoffer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /deleteoffer <ID>")
        return
    removed = await asyncio.to_thread(db.delete_offer, int(context.args[0]))
    await asyncio.to_thread(db.audit, update.effective_user.id, "delete_offer", context.args[0])
    await update.message.reply_text("✅ Offer deleted." if removed else "❌ No offer with that ID.")


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

    row = await asyncio.to_thread(db.get_invoice, int(context.args[0]))
    if not row:
        await update.message.reply_text("❌ No invoice with that ID.")
        return

    await update.message.reply_html(invoice_text(row))


async def receipts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.get_advertiser_invoices, update.effective_user.id)
    if not rows:
        await update.message.reply_text("You don't have any receipts yet.")
        return

    lines = ["🧾 <b>Your Receipts</b>\n"]
    for inv_id, business_name, duration_days, amount_paid, payment_method, payment_date, currency in rows:
        date_display = (payment_date or "")[:10]
        lines.append(f"Invoice #{inv_id} - {esc(business_name)} - {format_price(amount_paid, currency)} - {date_display}")
    lines.append("\nContact support if you need a detailed copy of any receipt.")

    await update.message.reply_html("\n".join(lines))


@owner_only
async def revenue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    period = context.args[0].lower() if context.args else "today"
    if period not in ("today", "week", "month", "year"):
        await update.message.reply_text("Usage: /revenue today|week|month|year")
        return

    r = await asyncio.to_thread(db.get_revenue_report, period)
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


async def myads_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    subs = await asyncio.to_thread(db.get_advertiser_subscriptions, user.id)
    history = await asyncio.to_thread(db.get_advertiser_history, user.id)
    invoices = await asyncio.to_thread(db.get_advertiser_invoices, user.id)
    total_spent = sum(inv[3] for inv in invoices)
    my_pending = sum(1 for r in await asyncio.to_thread(db.search_applications, str(user.id)) if r[7] == "pending")

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


async def renew_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    subscription_id = int(query.data.split(":", 1)[1])
    sub_row = await asyncio.to_thread(db.get_subscription, subscription_id)

    if not sub_row:
        await query.edit_message_text("❌ That advertisement subscription no longer exists.")
        return

    context.user_data["renew_subscription_id"] = subscription_id
    await query.edit_message_text(
        "How long would you like to renew for?",
        reply_markup=duration_keyboard(),
    )


async def renew_duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscription_id = context.user_data.get("renew_subscription_id")
    if subscription_id is None:
        return

    query = update.callback_query
    await query.answer()

    days = int(query.data.split(":", 1)[1])
    context.user_data.pop("renew_subscription_id", None)

    sub_row = await asyncio.to_thread(db.get_subscription, subscription_id)
    (sid, application_id, applicant_id, business_name, ad_link, ad_text, ad_type,
     old_duration, start_at, end_at, status, created_at, total_views, total_clicks,
     r7, r3, r24, rex, renewed_from) = sub_row

    new_application_id = await asyncio.to_thread(db.create_application, 
        applicant_id=applicant_id, applicant_name=update.effective_user.first_name,
        applicant_username=update.effective_user.username, business_name=business_name,
        ad_link=ad_link, ad_type=ad_type, duration_days=days, ad_text=ad_text, notes="Renewal",
    )
    await asyncio.to_thread(db.set_application_status, new_application_id, "approved", decided_by=None)
    await asyncio.to_thread(db.audit, applicant_id, "renew_subscription", f"Subscription #{subscription_id} -> Application #{new_application_id}")

    rate_row = await asyncio.to_thread(db.get_rate_with_currency, days)
    price, currency = rate_row if rate_row else (0, "NGN")
    await query.edit_message_text(f"✅ Renewal set up for {days} Days. Amount due: {format_price(price, currency)}")
    await send_payment_instructions(context, new_application_id, applicant_id, price, currency)


@owner_or_admin_only
async def subscriptions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.get_all_subscriptions)
    if not rows:
        await update.message.reply_text("No advertisement subscriptions yet.")
        return

    lines = [f"📋 <b>Subscriptions</b> ({await asyncio.to_thread(db.count_subscriptions)} total)\n"]
    lines.extend(subscription_summary_line(r) for r in rows)
    await update.message.reply_html("\n".join(lines))


@owner_or_admin_only
async def subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /subscription <ID>")
        return

    row = await asyncio.to_thread(db.get_subscription, int(context.args[0]))
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
    await asyncio.to_thread(db.set_subscription_status, subscription_id, "paused")
    await asyncio.to_thread(db.audit, update.effective_user.id, "pause_ad", str(subscription_id))
    await update.message.reply_text(f"⏸ Subscription #{subscription_id} paused.")


@owner_or_admin_only
async def resumead_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /resumead <ID>")
        return
    subscription_id = int(context.args[0])
    await asyncio.to_thread(db.set_subscription_status, subscription_id, "active")
    await asyncio.to_thread(db.audit, update.effective_user.id, "resume_ad", str(subscription_id))
    await update.message.reply_text(f"▶ Subscription #{subscription_id} resumed.")


@owner_or_admin_only
async def extendad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2 or not context.args[0].isdigit() or not context.args[1].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /extendad <ID> <days>")
        return
    subscription_id = int(context.args[0])
    days = int(context.args[1])
    new_end = await asyncio.to_thread(db.extend_subscription, subscription_id, days)
    if new_end is None:
        await update.message.reply_text("❌ No subscription with that ID.")
        return
    await asyncio.to_thread(db.audit, update.effective_user.id, "extend_ad", f"#{subscription_id} by {days} days")
    await update.message.reply_text(f"✅ Subscription #{subscription_id} extended. New end date: {new_end[:16]}")


@owner_or_admin_only
async def cancelad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /cancelad <ID>")
        return
    subscription_id = int(context.args[0])
    await asyncio.to_thread(db.set_subscription_status, subscription_id, "cancelled")
    await asyncio.to_thread(db.audit, update.effective_user.id, "cancel_ad", str(subscription_id))
    await update.message.reply_text(f"❌ Subscription #{subscription_id} cancelled.")


@owner_or_admin_only
async def adreport_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /adreport <Advertisement ID>")
        return

    row = await asyncio.to_thread(db.get_subscription, int(context.args[0]))
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
    rows = await asyncio.to_thread(db.get_all_applications)
    await _send_export(
        update, "advertisements.csv",
        ["ID", "Applicant ID", "Applicant Name", "Username", "Business", "Link", "Type", "Duration", "Status", "Submitted"],
        rows,
    )


@owner_only
async def exportrevenue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.get_all_invoices)
    await _send_export(
        update, "revenue.csv",
        ["Invoice ID", "Business", "Advertiser", "Duration", "Amount Paid", "Payment Method", "Payment Date", "Currency"],
        rows,
    )


@owner_only
async def exportpayments_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.get_pending_payments)
    await _send_export(
        update, "payments.csv",
        ["Payment ID", "Application ID", "Submitted", "Business", "Applicant"],
        rows,
    )


@owner_only
async def allcommands_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
📖 <b>UFFAdsBot Commands</b>

<b>PUBLIC</b>
/start - welcome menu
/help - how the bot works and how to submit an advertisement
/submitad - submit an advertisement
/skip - skip the optional "Additional notes" question while submitting
/myads - your advertisements &amp; total spend
/receipts - your past invoices
/cancel - cancel an in-progress submission

<b>APPLICATIONS</b> (owner/admin)
/applications - pending applications (paginated)
/open &lt;id&gt; - view one application
/approved - approved applications
/rejected - rejected applications
/payments - payments waiting for verification (owner/admin)
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
/refundstars &lt;payment_id&gt; - refund a native Telegram Stars payment in full (owner/admin)
/setgiftsrecipient &lt;@username&gt; - Telegram Gifts recipient
/addstars, /addgifts, /fixcrypto - maintenance helpers

<b>ADVERTISEMENT RATES</b> (owner)
/setrate &lt;duration_days&gt; &lt;amount&gt; [currency] - add or update a rate
/deleterate &lt;duration_days&gt; - remove a rate
/rates - view all configured rates
/configurestarpaymentmethod [&lt;duration_days&gt; &lt;usd_price&gt; &lt;stars&gt; | &lt;duration_days&gt; remove] - list, set, or remove Telegram Stars tiers
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


async def job_activate_scheduled_ads(context: ContextTypes.DEFAULT_TYPE):
    for subscription_id in await asyncio.to_thread(db.get_subscriptions_needing_activation):
        await asyncio.to_thread(db.set_subscription_status, subscription_id, "active")
        logger.info(f"Activated subscription #{subscription_id}")

        sub_row = await asyncio.to_thread(db.get_subscription, subscription_id)
        if sub_row:
            ad_link, ad_text = sub_row[4], sub_row[5]
            _publish_to_sponsored_slot(ad_text, ad_link)


async def job_expire_ads(context: ContextTypes.DEFAULT_TYPE):
    for subscription_id in await asyncio.to_thread(db.get_subscriptions_needing_expiry):
        await asyncio.to_thread(db.set_subscription_status, subscription_id, "expired")
        logger.info(f"Expired subscription #{subscription_id}")


async def job_mark_expiring_soon(context: ContextTypes.DEFAULT_TYPE):
    for row in await asyncio.to_thread(db.get_subscriptions_expiring_within, 24):
        subscription_id = row[0]
        await asyncio.to_thread(db.set_subscription_status, subscription_id, "expiring_soon")


async def job_send_reminders(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()

    for row in await asyncio.to_thread(db.get_subscriptions_expiring_within, 24 * 7 + 1):
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

        await asyncio.to_thread(db.mark_reminder_sent, subscription_id, which)


async def renew_dismiss_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("No problem - you can renew any time from /myads.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
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

    app.add_handler(CommandHandler("setrate", setrate_command))
    app.add_handler(CommandHandler("deleterate", deleterate_command))
    app.add_handler(CommandHandler("rates", rates_admin_command))
    app.add_handler(CommandHandler("configurestarpaymentmethod", configurestarpaymentmethod_command))
    app.add_handler(CommandHandler("refundstars", refundstars_command))

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
    app.add_handler(CallbackQueryHandler(renew_duration_callback, pattern="^adduration:"))

    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        private_text_dispatcher,
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.ALL),
        payment_proof_upload,
    ))

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

    return app


if __name__ == "__main__":
    pass