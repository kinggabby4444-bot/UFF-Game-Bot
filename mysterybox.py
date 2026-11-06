"""
mysterybox.py - the Mystery Box game for UFF-Game-Bot (Stage 4).

Put this file (and mysterybox.jpg) next to main.py. main.py loads it from main() inside a try/except, so a
problem here can never stop the rest of the bot.

  /mysterybox  posts the box (photo + caption). It stays open for 2 minutes, one box per group.
  /open        any member opens it ONCE per box. Each open costs 💵100 (Word Scramble money first, then
               Word Game money) and the prize goes to the Word Scramble balance, counted once.
  After 2 minutes the box closes by itself and opens ONE time for the person who started it
  (if they did not already use /open).
Daily limit per UTC day, over all groups: 3 opens (Premium: 10). A try is only used up when the open really
happens - not enough cash never costs a try. Fee, try and prize are one database transaction.
While no box is open in a chat, /open answers without touching the database.
"""
import asyncio
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ---- everything you may want to tune is here --------------------------------------------------------
PRIZES = ((1000, 1), (200, 8), (50, 21), (20, 33), (10, 37))   # (prize in 💵, chance in %) - must add up to 100
OPEN_FEE = 100
BOX_SECONDS = 120
DAILY_LIMIT_NORMAL = 3
DAILY_LIMIT_PREMIUM = 10
TOP_PRIZE = PRIZES[0][0]
PHOTO_FILE = "mysterybox.jpg"
PHOTO_CONFIG_KEY = "mysterybox_photo_file_id"
# ------------------------------------------------------------------------------------------------------

assert sum(p for _a, p in PRIZES) == 100

ACTIVE = {}   # chat_id -> box dict ({"id", "starter_id", "starter_name", "opened": set(), "job"})
D = None      # helpers handed over by main.py in install()


def roll_prize():
    """One secure random roll, using the PRIZES table."""
    n = secrets.randbelow(100)
    total = 0
    for amount, chance in PRIZES:
        total += chance
        if n < total:
            return amount
    return PRIZES[-1][0]


def caption_text():
    prizes = " | ".join(f"💵 {amount:,}" for amount, _c in PRIZES)
    return ("🎁 MYSTERY BOX 🎁\n\n"
            "Open the box for a chance to win exciting cash rewards!\n"
            f"{prizes}\n\n"
            "Use /open to try your luck.\n"
            f"💵{OPEN_FEE} per attempt")


def _mention(user_id, name):
    return f'<a href="tg://user?id={user_id}">{D.esc_name(name)}</a>'


def _utc_date():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _limit_for(premium):
    return DAILY_LIMIT_PREMIUM if premium else DAILY_LIMIT_NORMAL


def limit_text(premium):
    if premium:
        return f"You've used all {DAILY_LIMIT_PREMIUM} of your box opens today. Come back tomorrow (resets 00:00 UTC)."
    return (f"You've used your {DAILY_LIMIT_NORMAL} box opens today. Come back tomorrow (resets 00:00 UTC) "
            f"or use /buypremium and you can open up to {DAILY_LIMIT_PREMIUM} a day.")


NO_BOX_TEXT = "No box is open here - start one with /mysterybox"


async def _send_box(context, chat_id):
    """Photo + caption. Falls back to the plain caption if the photo is missing or cannot be sent."""
    bot, caption = context.bot, caption_text()
    file_id = await asyncio.to_thread(D.db.get_config, PHOTO_CONFIG_KEY, None)
    if file_id:
        try:
            return await bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption)
        except Exception as e:
            logger.info("mysterybox: cached photo id not usable (%s); trying the file", e)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), PHOTO_FILE)
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                msg = await bot.send_photo(chat_id=chat_id, photo=f, caption=caption)
            try:
                await asyncio.to_thread(D.db.set_config, PHOTO_CONFIG_KEY, msg.photo[-1].file_id)
            except Exception:
                pass
            return msg
        except Exception as e:
            logger.warning("mysterybox: photo could not be sent (%s); sending text only", e)
    else:
        logger.warning("mysterybox: %s not found next to main.py; sending text only", PHOTO_FILE)
    return await D.safe_send(bot, chat_id, text=caption, raise_permanent=True)


async def start_command(update, context):
    chat, user = update.effective_chat, update.effective_user
    if chat.type == "private":
        await update.message.reply_text("🎁 Mystery Boxes are for groups - add me to a group and run /mysterybox there!")
        return
    if chat.id in ACTIVE:
        await update.message.reply_text("🎁 A Mystery Box is already open here - use /open to try your luck.")
        return
    db = D.db
    if await asyncio.to_thread(db.get_effective_admin_mode, chat.id):
        if not await D.bot_is_admin_in_chat(context, chat.id):
            await update.message.reply_text(
                "🙏 To start a game here, I'll need to be a group admin. Please make me an admin, then run /mysterybox again!")
            return
    now = datetime.now()
    box = {"id": None, "starter_id": user.id, "starter_name": user.first_name or "Player", "opened": set(), "job": None}
    ACTIVE[chat.id] = box                      # reserved BEFORE any await: a second /mysterybox can never slip in
    try:
        box["id"] = await asyncio.to_thread(
            db.mystery_box_start, chat.id, user.id, box["starter_name"], now.isoformat(),
            (now + timedelta(seconds=BOX_SECONDS)).isoformat())
        if box["id"] is None:
            raise RuntimeError("a box is already recorded for this chat")
        await _send_box(context, chat.id)
    except Exception:
        logger.exception("mysterybox: could not start in %s", chat.id)
        if ACTIVE.get(chat.id) is box:
            ACTIVE.pop(chat.id, None)
        if box["id"] is not None:
            try:
                await asyncio.to_thread(db.mystery_box_close, box["id"], "cancelled")
            except Exception:
                pass
        await update.message.reply_text("⚠️ Couldn't start the Mystery Box right now - try /mysterybox again in a moment.")
        return
    if context.job_queue is not None:
        box["job"] = context.job_queue.run_once(
            _expire_job, BOX_SECONDS, chat_id=chat.id, name=f"mysterybox_{chat.id}", data={"bid": box["id"]})


async def _announce(context, chat_id, user_id, name, res, prize, premium, reply_to=None):
    """Turns a database result into the message the group sees."""
    status = res["status"]
    if status == "ok":
        left = max(_limit_for(premium) - res["opens_today"], 0)
        tries = f"\n🎟 Box opens left today: {left}"
        if prize == TOP_PRIZE:
            text = (f"🎉 <b>JACKPOT!</b> 🎉\n{_mention(user_id, name)} opened the Mystery Box and won "
                    f"<b>{D.DOLLAR}{D.fmt_coins(prize)}</b>!{tries}")
            await D.send_winner_message(context.bot, chat_id, text, winner_uid=user_id, headline="🎉 JACKPOT! 🎉")
        else:
            text = f"🎁 {_mention(user_id, name)} opened the box and won {D.DOLLAR}{D.fmt_coins(prize)}!{tries}"
            await D.safe_send(context.bot, chat_id, text=text, parse_mode="HTML")
        return
    if status == "insufficient":
        text, mode = D.insufficient_cash_text(user_id, name), "HTML"
    elif status == "limit":
        text, mode = limit_text(premium), None
    elif status == "already":
        text, mode = "You already opened this box.", None
    else:
        text, mode = NO_BOX_TEXT, None
    kwargs = {"parse_mode": mode} if mode else {}
    if reply_to is not None:
        kwargs["reply_parameters"] = _reply_params(reply_to)
    await D.safe_send(context.bot, chat_id, text=text, **kwargs)


def _reply_params(message_id):
    try:
        from telegram import ReplyParameters
        return ReplyParameters(message_id=message_id, allow_sending_without_reply=True)
    except Exception:
        return None


async def open_command(update, context):
    chat, user = update.effective_chat, update.effective_user
    box = ACTIVE.get(chat.id) if chat is not None else None
    if box is None:
        await update.message.reply_text(NO_BOX_TEXT)         # nothing open: no database, no work
        return
    if user.id in box["opened"]:
        await update.message.reply_text("You already opened this box.")
        return
    premium = await asyncio.to_thread(D.is_premium, user.id)
    prize = roll_prize()
    name = user.first_name or "Player"
    res = await asyncio.to_thread(
        D.db.mystery_box_open, box["id"], chat.id, user.id, name, _utc_date(), _limit_for(premium), OPEN_FEE, prize, False)
    if res["status"] in ("ok", "already"):
        box["opened"].add(user.id)
    await _announce(context, chat.id, user.id, name, res, prize, premium, reply_to=update.message.message_id)


async def _expire_job(context):
    """2 minutes are over: the box closes and opens once for whoever started it (if they did not already)."""
    chat_id = context.job.chat_id
    box = ACTIVE.get(chat_id)
    if box is None or box["id"] != (context.job.data or {}).get("bid"):
        return                                              # a stale timer
    ACTIVE.pop(chat_id, None)                               # no await before this line: the box can only close once
    try:
        if box["starter_id"] in box["opened"]:
            await asyncio.to_thread(D.db.mystery_box_close, box["id"], "closed")
            return
        premium = await asyncio.to_thread(D.is_premium, box["starter_id"])
        prize = roll_prize()
        res = await asyncio.to_thread(
            D.db.mystery_box_open, box["id"], chat_id, box["starter_id"], box["starter_name"], _utc_date(),
            _limit_for(premium), OPEN_FEE, prize, True)
        if res["status"] == "already":
            return                                          # they used /open at the very last moment: nothing extra
        await _announce(context, chat_id, box["starter_id"], box["starter_name"], res, prize, premium)
    except Exception:
        logger.exception("mysterybox: closing the box in %s failed", chat_id)
        try:
            await asyncio.to_thread(D.db.mystery_box_close, box["id"], "closed")
        except Exception:
            pass


async def stop_all(context):
    """/stopallactivities: closes every open box without opening it (nobody is charged)."""
    out = {}
    for chat_id in list(ACTIVE):
        box = ACTIVE.pop(chat_id, None)
        if box is None:
            continue
        if box.get("job") is not None:
            try:
                box["job"].schedule_removal()
            except Exception:
                pass
        try:
            await asyncio.to_thread(D.db.mystery_box_close, box["id"], "stopped")
        except Exception:
            logger.exception("mysterybox: could not mark box %s as stopped", box["id"])
        out[chat_id] = "Mystery Box"
    return out


async def restore_job(context):
    """At startup: a box still marked open belongs to a bot that was restarted - close it politely (once)."""
    rows = await asyncio.to_thread(D.db.mystery_interrupt_open, (datetime.now() - timedelta(seconds=30)).isoformat())
    for (chat_id,) in rows:
        try:
            await D.safe_send(context.bot, chat_id, text="🎁 The Mystery Box closed because the bot restarted. "
                                                         "Nobody was charged. Start a new one with /mysterybox")
        except Exception:
            logger.info("mysterybox: restart notice for %s not delivered", chat_id)
        await asyncio.sleep(0.3)


def is_running(chat_id):
    return chat_id in ACTIVE


def install(app, deps):
    global D
    D = deps
    from telegram.ext import CommandHandler
    app.add_handler(CommandHandler("mysterybox", start_command))
    app.add_handler(CommandHandler("open", open_command))
    deps.register_stopall_hook(stop_all)
    if app.job_queue is not None:
        app.job_queue.run_once(restore_job, 12, name="mysterybox_restore")
