"""
hiddenletter.py - the Hidden Letter game for UFF-Game-Bot (Stage 5).

Put this file next to main.py. main.py loads it from main() inside a try/except, so if this file has any
problem the rest of the bot keeps working exactly as before.

How it works
  * Three times a day (SLOTS, UTC) the bot posts "I'm thinking of a letter from A to Z!" in every active group
    (bot still inside, not banned, activity in the last 7 days, not switched off with /hiddenletter off).
  * The group has 10 guesses in total and every player 1 guess. A guess is a single letter: "/g K", "/guess K"
    or just "K" while a round is running. A wrong letter gets 👎, a letter that was already tried is refused and
    costs nothing, the right letter gets 👍 and wins 💵100 (Word Scramble balance, counted once).
  * 10 wrong guesses or 30 minutes without a winner: "Nobody found it. The letter was K."
  * Each (group, time slot) is sent exactly once (database claim), also across restarts.
  * Hot path: while no round runs in a chat, a message costs ONE dictionary lookup and no database call.
"""
import asyncio
import logging
import random
import re
import string
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SLOTS = ((7, 0), (14, 0), (20, 0))      # UTC times of the daily rounds
LATE_MINUTES = 30                       # a slot is still sent up to this long late (restart / busy group), else skipped
ROUND_SECONDS = 30 * 60
MAX_GUESSES = 10
PRIZE = 100
ACTIVE_DAYS = 7
STAGGER = (0.2, 0.5)                    # seconds between two groups ...
STAGGER_JITTER = 0.2                    # ... plus up to this much random extra
RETRY_SECONDS = 90                      # a group busy with another game is tried again after this

MESSAGE = "🔠 HIDDEN LETTER 🔠\n\nI'm thinking of a letter from A to Z!\n\n🎯 10 guesses for the whole group\n👤 1 guess per player"
NOBODY = "😔 Nobody found it. The letter was {letter}."
ALREADY_TRIED = "👎 already tried"

ACTIVE = {}          # chat_id -> round dict (the ONLY thing the message handler looks at)
_BAGS = {}           # chat_id -> letters not used yet in the current shuffled bag
_LAST = {}           # chat_id -> the last letter used
_SENT_SLOTS = []     # slot keys this process already handled
_RNG = random.SystemRandom()
D = None             # the helpers main.py hands over in install()


def _mention(user_id, name):
    return f'<a href="tg://user?id={user_id}">{D.esc_name(name)}</a>'


def _reply_params(message_id):
    try:
        from telegram import ReplyParameters
        return ReplyParameters(message_id=message_id, allow_sending_without_reply=True)
    except Exception:
        return None


def parse_letter(raw):
    """'k', ' K ' -> 'K'. Anything else -> None."""
    raw = (raw or "").strip()
    if len(raw) == 1 and raw.isascii() and raw.isalpha():
        return raw.upper()
    return None


def pick_letter(chat_id):
    """Next letter for this chat: a shuffled bag of A-Z that is refilled when empty; never the same letter twice in a row."""
    bag = _BAGS.get(chat_id)
    if not bag:
        bag = list(string.ascii_uppercase)
        _RNG.shuffle(bag)
        last = _LAST.get(chat_id)
        if last is None:
            try:
                last = D.db.hl_last_letter(chat_id)
            except Exception:
                last = None
        if bag[-1] == last:
            bag[-1], bag[0] = bag[0], bag[-1]
        _BAGS[chat_id] = bag
    letter = bag.pop()
    _LAST[chat_id] = letter
    return letter


def is_running(chat_id):
    return chat_id in ACTIVE


# ---------------------------------------------------------------- the round
def _cancel_job(rnd):
    job = rnd.get("job")
    if job is not None:
        try:
            job.schedule_removal()
        except Exception:
            pass


def _schedule_expiry(job_queue, rnd, seconds):
    if job_queue is None:
        return
    rnd["job"] = job_queue.run_once(_expire_job, max(seconds, 1), chat_id=rnd["chat_id"], data={"rid": rnd["id"]},
                                    name=f"hl_expire_{rnd['chat_id']}")


async def _expire_job(context):
    chat_id = context.job.chat_id
    rnd = ACTIVE.get(chat_id)
    if rnd is None or rnd["id"] != (context.job.data or {}).get("rid"):
        return                                              # a stale timer
    ACTIVE.pop(chat_id, None)                               # no await before this line: a round can only end once
    await _finish_unsolved(context.bot, chat_id, rnd, "expired")


async def _finish_unsolved(bot, chat_id, rnd, status):
    try:
        letter = await asyncio.to_thread(D.db.hl_end, rnd["id"], status)
    except Exception:
        logger.exception("hiddenletter: could not close round %s", rnd["id"])
        letter = rnd["letter"]
    if letter is None:
        return                                              # already ended elsewhere
    await D.safe_send(bot, chat_id, text=NOBODY.format(letter=letter))


async def _thumb(update_message, bot, chat_id, emoji):
    """Reacts with the emoji; if Telegram refuses the reaction, replies with it instead."""
    try:
        from telegram import ReactionTypeEmoji
        await bot.set_message_reaction(chat_id=chat_id, message_id=update_message.message_id,
                                       reaction=[ReactionTypeEmoji(emoji=emoji)])
        return True
    except Exception:
        try:
            await update_message.reply_text(emoji)
        except Exception:
            pass
        return False


async def process_guess(update, context, letter):
    """Handles one letter guess for a chat that has a running round. Returns True if it was handled."""
    chat, user, message = update.effective_chat, update.effective_user, update.message
    rnd = ACTIVE.get(chat.id)
    if rnd is None or message is None or user is None or getattr(user, "is_bot", False):
        return False
    if user.id in rnd["players"]:
        return True                                         # already used their guess: ignored silently
    if letter in rnd["tried"]:
        try:
            await message.reply_text(ALREADY_TRIED)         # costs no guess
        except Exception:
            pass
        return True
    # reserve everything BEFORE the first await: two players at the same moment cannot both win or both use guess #10
    rnd["players"].add(user.id)
    rnd["tried"].add(letter)
    rnd["used"] += 1
    correct = letter == rnd["letter"]
    last_wrong = (not correct) and rnd["used"] >= MAX_GUESSES
    if correct or last_wrong:
        ACTIVE.pop(chat.id, None)
        _cancel_job(rnd)
    name = user.first_name or "Player"
    bot = context.bot

    if not correct:
        await _thumb(message, bot, chat.id, "👎")
        try:
            await asyncio.to_thread(D.db.hl_record_guess, rnd["id"], user.id, letter)
        except Exception:
            logger.exception("hiddenletter: could not store a guess")
        if last_wrong:
            await _finish_unsolved(bot, chat.id, rnd, "failed")
        return True

    # the right letter
    try:
        balance = await asyncio.to_thread(D.db.hl_win, rnd["id"], user.id, name, PRIZE)
    except Exception:
        logger.exception("hiddenletter: paying the winner failed - the round stays open")
        rnd["players"].discard(user.id)
        rnd["tried"].discard(letter)
        rnd["used"] -= 1
        ACTIVE.setdefault(chat.id, rnd)
        remaining = (rnd["expires"] - datetime.now()).total_seconds()
        _schedule_expiry(context.job_queue, rnd, remaining)
        return True
    if balance is None:
        return True                                         # the round had already ended: nothing paid
    await _thumb(message, bot, chat.id, "👍")
    text = (f"🎉 {_mention(user.id, name)} found the hidden letter <b>{letter}</b>!\n"
            f"💵{D.fmt_coins(PRIZE)} has been added to their balance.")
    await D.send_winner_message(bot, chat.id, text, winner_uid=user.id, headline="🔠 HIDDEN LETTER 🔠")
    return True


async def on_text(update, context):
    """Plain letter in a group (handler group 5). No round in this chat = return at once, no database."""
    chat = update.effective_chat
    if chat is None or chat.id not in ACTIVE:
        return
    message = update.message
    letter = parse_letter(message.text if message else None)
    if letter is None:
        return
    await process_guess(update, context, letter)


async def handle_command_guess(update, context):
    """Called first thing by /g and /guess. True = it was a Hidden Letter guess (Hack must not see it).
    Anything else (no round here, or not a single letter) returns False and Hack behaves exactly as before."""
    chat = update.effective_chat
    if chat is None or chat.id not in ACTIVE:
        return False
    letter = parse_letter((context.args or [""])[0])
    if letter is None:
        return False
    return await process_guess(update, context, letter)


# ---------------------------------------------------------------- starting rounds
async def start_round(context, chat_id, slot_key):
    """Returns "sent", "dup" (this slot was already sent here), "busy" or "fail"."""
    if chat_id in ACTIVE:
        return "busy"
    letter = await asyncio.to_thread(pick_letter, chat_id)
    now = datetime.now()
    expires = now + timedelta(seconds=ROUND_SECONDS)
    status, rid = await asyncio.to_thread(D.db.hl_start_round, chat_id, slot_key, letter, now.isoformat(), expires.isoformat())
    if status != "ok":
        return status
    rnd = {"id": rid, "chat_id": chat_id, "letter": letter, "used": 0, "tried": set(), "players": set(),
           "expires": expires, "job": None}
    ACTIVE[chat_id] = rnd
    _schedule_expiry(context.job_queue, rnd, ROUND_SECONDS)
    try:
        await D.safe_send(context.bot, chat_id, text=MESSAGE, raise_permanent=True)
    except Exception as e:
        logger.info("hiddenletter: could not post in %s (%s)", chat_id, e)
        if ACTIVE.get(chat_id) is rnd:
            ACTIVE.pop(chat_id, None)
        _cancel_job(rnd)
        try:
            await asyncio.to_thread(D.db.hl_end, rid, "failed_send")
        except Exception:
            pass
        return "fail"
    return "sent"


async def _send_all(context, slot_key, deadline, chats):
    try:
        busy = await asyncio.to_thread(D.db.hl_busy_chats)
    except Exception:
        logger.exception("hiddenletter: busy check failed - trying again shortly")
        busy, chats_retry = set(), list(chats)
        _schedule_retry(context, slot_key, deadline, chats_retry)
        return
    pending = []
    for chat_id in chats:
        if datetime.now(timezone.utc) > deadline:
            return
        if chat_id in ACTIVE:
            continue
        if chat_id in busy or D.anagram_running(chat_id):
            pending.append(chat_id)                         # tournament / elimination / hack / anagram: postponed
            continue
        try:
            res = await start_round(context, chat_id, slot_key)
        except Exception:
            logger.exception("hiddenletter: round in %s failed", chat_id)
            continue
        if res == "sent":
            await asyncio.sleep(_RNG.uniform(*STAGGER) + _RNG.uniform(0, STAGGER_JITTER))
    if pending:
        _schedule_retry(context, slot_key, deadline, pending)


def _schedule_retry(context, slot_key, deadline, chats):
    if context.job_queue is None or datetime.now(timezone.utc) + timedelta(seconds=RETRY_SECONDS) > deadline or not chats:
        return
    context.job_queue.run_once(_retry_job, RETRY_SECONDS, data={"slot": slot_key, "deadline": deadline, "chats": list(chats)},
                               name="hl_retry")


async def _retry_job(context):
    data = context.job.data
    try:
        eligible = set(await asyncio.to_thread(D.db.hl_eligible_groups, _cutoff()))
    except Exception:
        logger.exception("hiddenletter: retry could not read the group list")
        return
    chats = [c for c in data["chats"] if c in eligible]
    await _send_all(context, data["slot"], data["deadline"], chats)


def _cutoff():
    return (datetime.now() - timedelta(days=ACTIVE_DAYS)).isoformat()


async def dispatch(context, slot_dt):
    """Sends one time slot to every eligible group (each group at most once per slot, enforced by the database)."""
    slot_key = slot_dt.strftime("%Y%m%d-%H%M")
    if slot_key in _SENT_SLOTS:
        return
    _SENT_SLOTS.append(slot_key)
    del _SENT_SLOTS[:-10]
    deadline = slot_dt + timedelta(minutes=LATE_MINUTES)
    if datetime.now(timezone.utc) > deadline:
        return
    try:
        chats = await asyncio.to_thread(D.db.hl_eligible_groups, _cutoff())
    except Exception:
        logger.exception("hiddenletter: could not read the group list")
        _SENT_SLOTS.remove(slot_key)
        return
    await _send_all(context, slot_key, deadline, chats)


async def _slot_job(context):
    h, m = context.job.data["h"], context.job.data["m"]
    now = datetime.now(timezone.utc)
    await dispatch(context, now.replace(hour=h, minute=m, second=0, microsecond=0))


async def _catchup_job(context):
    """At startup: a slot that began less than LATE_MINUTES ago and was not sent yet is sent now."""
    now = datetime.now(timezone.utc)
    for h, m in SLOTS:
        slot_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if timedelta(0) <= now - slot_dt <= timedelta(minutes=LATE_MINUTES):
            await dispatch(context, slot_dt)


async def restore_job(context):
    """At startup: rounds still marked active continue if they have not expired, otherwise they end politely."""
    try:
        rounds = await asyncio.to_thread(D.db.hl_load_active)
    except Exception:
        logger.exception("hiddenletter: could not restore rounds")
        return
    for r in rounds:
        try:
            expires = datetime.fromisoformat(r["expires_at"])
        except Exception:
            expires = datetime.now()
        if expires <= datetime.now():
            letter = await asyncio.to_thread(D.db.hl_end, r["id"], "expired")
            if letter is not None:
                await D.safe_send(context.bot, r["chat_id"], text=NOBODY.format(letter=letter))
                await asyncio.sleep(0.3)
            continue
        rnd = {"id": r["id"], "chat_id": r["chat_id"], "letter": r["letter"], "used": len(r["guessed"]),
               "tried": {l for _u, l in r["guessed"]}, "players": {u for u, _l in r["guessed"]},
               "expires": expires, "job": None}
        ACTIVE[r["chat_id"]] = rnd
        _schedule_expiry(context.job_queue, rnd, (expires - datetime.now()).total_seconds())


async def stop_all(context):
    """/stopallactivities: ends every running round (no payout). Future slots are controlled with /hiddenletter."""
    out = {}
    for chat_id in list(ACTIVE):
        rnd = ACTIVE.pop(chat_id, None)
        if rnd is not None:
            _cancel_job(rnd)
            out[chat_id] = "Hidden Letter"
    try:
        await asyncio.to_thread(D.db.hl_stop_rounds, None)
    except Exception:
        logger.exception("hiddenletter: could not mark rounds as stopped")
    return out


# ---------------------------------------------------------------- /hiddenletter
USAGE = ("🔠 Hidden Letter\n\n"
         "In a group: /hiddenletter on | off | (nothing = status) - only the group creator and the bot owner.\n"
         "Bot owner in private: /hiddenletter on|off <group id | @username | allgroups>")


def _slots_text():
    return ", ".join(f"{h:02d}:{m:02d}" for h, m in SLOTS) + " UTC"


async def _apply(chat_ids, enabled):
    if chat_ids is None:                                    # allgroups
        await asyncio.to_thread(D.db.hl_set_everywhere, enabled)
        if not enabled:
            for chat_id in list(ACTIVE):
                _cancel_job(ACTIVE.pop(chat_id))
            await asyncio.to_thread(D.db.hl_stop_rounds, None)
        return
    await asyncio.to_thread(D.db.hl_set_enabled, chat_ids, enabled)
    if not enabled:
        for chat_id in chat_ids:
            rnd = ACTIVE.pop(chat_id, None)
            if rnd is not None:
                _cancel_job(rnd)
        await asyncio.to_thread(D.db.hl_stop_rounds, chat_ids)


async def hiddenletter_command(update, context):
    chat, user, message = update.effective_chat, update.effective_user, update.message
    args = [a for a in (context.args or [])]
    action = args[0].lower() if args else ""
    if chat.type == "private":
        if not D.is_owner(user.id):
            await message.reply_text("Use /hiddenletter inside your group.")
            return
        if action not in ("on", "off") or len(args) != 2:
            await message.reply_text(USAGE)
            return
        enabled = action == "on"
        if args[1].lower() == "allgroups":
            await _apply(None, enabled)
            await message.reply_text(f"🔠 Hidden Letter is now {'ON' if enabled else 'OFF'} in ALL groups.")
            return
        resolved = await D.resolve_group_arg(args[1])
        if not resolved:
            await message.reply_text("❌ No group found with that id or @username that I'm currently in.")
            return
        await _apply([resolved[0]], enabled)
        await message.reply_text(f"🔠 Hidden Letter is now {'ON' if enabled else 'OFF'} in {resolved[1]}.")
        return
    # inside a group
    if action in ("on", "off"):
        if not (D.is_owner(user.id) or await D.is_chat_creator(update, context)):
            await message.reply_text("Only the group creator can switch Hidden Letter on or off.")
            return
        await _apply([chat.id], action == "on")
        await message.reply_text("🔠 Hidden Letter is now ON in this group." if action == "on"
                                 else "🔠 Hidden Letter is now OFF in this group.")
        return
    if action:
        await message.reply_text(USAGE)
        return
    enabled, global_off = await asyncio.to_thread(D.db.hl_get_setting, chat.id)
    on = enabled and not global_off
    await message.reply_text(
        f"🔠 Hidden Letter is {'ON' if on else 'OFF'} in this group.\n"
        f"Rounds are sent in active groups at {_slots_text()}."
        + ("\nIt is switched off for all groups by the bot owner." if global_off else "")
        + "\nThe group creator can use /hiddenletter on or /hiddenletter off.")


def install(app, deps):
    """Called once from main.py. Registers /hiddenletter, the letter handler (handler group 5), the daily slots."""
    global D
    D = deps
    from telegram.ext import CommandHandler, MessageHandler, filters
    app.add_handler(CommandHandler("hiddenletter", hiddenletter_command))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND & filters.Regex(r"^\s*[A-Za-z]\s*$"), on_text), group=5)
    deps.register_stopall_hook(stop_all)
    if app.job_queue is not None:
        for h, m in SLOTS:
            app.job_queue.run_daily(_slot_job, time=datetime(2000, 1, 1, h, m, tzinfo=timezone.utc).timetz(),
                                    data={"h": h, "m": m}, name=f"hl_slot_{h:02d}{m:02d}")
        app.job_queue.run_once(restore_job, 14, name="hl_restore")
        app.job_queue.run_once(_catchup_job, 30, name="hl_catchup")
