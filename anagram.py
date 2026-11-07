"""
anagram.py - the Anagram game for UFF-Game-Bot (Stage 3).

Put this file next to main.py. main.py loads it from main() inside a try/except, so if this file has any
problem the rest of the bot keeps working exactly as before.

How a round works
  * /anagram shows a long word (8+ letters) shuffled in CAPITAL letters. The group has 2 minutes.
  * Anyone sends a plain word. It counts when it only uses the shown letters (each letter as many times as it
    is shown), has 5+ letters, is a real English word, is not the shown word and was not found yet.
  * Every new word pays 💵 (default 1, /setanagramearnings) to the Word Scramble balance. The money is
    credited FIRST, then the word gets a 👍 reaction (no message). A word that is not accepted (not a real word,
    letters not available, already found) gets a 👎 reaction. Anything under 5 letters is ignored silently. One round per chat. Rounds do not run next to another game.
Hot path: while no round is running in a chat, a group message costs ONE dictionary lookup and no database call.
"""
import asyncio
import logging
import random
import re
import secrets
from collections import Counter, deque
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

ROUND_SECONDS = 120
MIN_WORD_LETTERS = 5
MIN_BASE_LETTERS = 8
MAX_BASE_LETTERS = 12
DEFAULT_REWARD = 1
REWARD_CONFIG_KEY = "anagram_reward"
MAX_REWARD = 1_000_000
MISSED_WORDS_SHOWN = 8

ACTIVE = {}            # chat_id -> round dict (the ONLY thing the message handler looks at)
_RECENT_BASES = {}     # chat_id -> last base words (so the same word does not come back soon)
_DICT_CACHE = {}       # word -> bool (dictionary answers never change while the bot runs)
_POOL_CACHE = {"key": None, "words": []}
_WORD_RE = re.compile(r"[a-z]+")
D = None               # the helpers main.py hands over in install()


# ---------------------------------------------------------------- small helpers
def _mention(user_id, name):
    return f'<a href="tg://user?id={user_id}">{D.esc_name(name)}</a>'


def _reply_params(message_id):
    try:
        from telegram import ReplyParameters
        return ReplyParameters(message_id=message_id, allow_sending_without_reply=True)
    except Exception:
        return None


def _now():
    return datetime.now()


def get_reward():
    try:
        value = int(D.db.get_config(REWARD_CONFIG_KEY, str(DEFAULT_REWARD)))
    except (TypeError, ValueError):
        value = DEFAULT_REWARD
    return value if 1 <= value <= MAX_REWARD else DEFAULT_REWARD


def _long_words():
    """8-12 letter words from the same pool Word Scramble uses (rebuilt when the pool is reloaded)."""
    pool = D.get_scramble_words() or []
    key = (id(pool), len(pool))
    if _POOL_CACHE["key"] != key:
        words, seen = [], set()
        for w in pool:
            w = (w or "").strip().lower()
            if w.isalpha() and w.isascii() and MIN_BASE_LETTERS <= len(w) <= MAX_BASE_LETTERS and w not in seen:
                seen.add(w)
                words.append(w)
        _POOL_CACHE["key"], _POOL_CACHE["words"] = key, words
    return _POOL_CACHE["words"]


def _pick_base(chat_id):
    words = _long_words()
    if not words:
        return None
    recent = _RECENT_BASES.setdefault(chat_id, deque(maxlen=60))
    choices = [w for w in words if w not in recent] or words
    word = random.choice(choices)
    recent.append(word)
    return word


def _shuffle(word):
    letters = list(word)
    for _ in range(20):
        random.shuffle(letters)
        if "".join(letters) != word:
            break
    return "".join(letters).upper()


def is_real_word(word):
    hit = _DICT_CACHE.get(word)
    if hit is None:
        hit = bool(D.is_valid_english_word(word))
        _DICT_CACHE[word] = hit
    return hit


def _missed_words(rnd):
    """Real words that could still be made (optional extra in the results; never raises)."""
    try:
        base_counter = rnd["letters"]
        found = rnd["found"]
        out = []
        for w, _freq in D.dictionary_words():
            if (MIN_WORD_LETTERS <= len(w) <= len(rnd["base"]) and w != rnd["base"] and w not in found
                    and w.isalpha() and w.isascii() and not (Counter(w) - base_counter)):
                out.append((len(w), _freq, w))
        out.sort(reverse=True)
        return [w for _l, _f, w in out[:MISSED_WORDS_SHOWN]]
    except Exception:
        return []


# ---------------------------------------------------------------- results
def _results_text(rnd, header):
    lines = [header, "", f"The word was: <b>{D.esc(rnd['base'].upper())}</b>", ""]
    players = sorted(rnd["players"].items(), key=lambda kv: (-len(kv[1]["words"]), kv[0]))
    if not players:
        lines.append("Nobody found a word this time.")
    else:
        lines.append("🏆 <b>Results</b>")
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for i, (uid, p) in enumerate(players[:10], start=1):
            n = len(p["words"])
            lines.append(f"{medals.get(i, str(i) + '.')} {_mention(uid, p['name'])} - "
                         f"{n} word{'s' if n != 1 else ''} (+{D.DOLLAR}{D.fmt_coins(p['earned'])})")
        total = sum(len(p["words"]) for p in rnd["players"].values())
        lines.append("")
        lines.append(f"Words found: {total}")
    return lines


async def _finish(context, chat_id, rnd, header, status, with_results=True, missed=True):
    """Ends a round exactly once (the caller already removed it from ACTIVE)."""
    job = rnd.get("job")
    if job is not None:
        try:
            job.schedule_removal()
        except Exception:
            pass
    try:
        await asyncio.to_thread(D.db.anagram_round_finish, rnd["id"], status, sum(len(p["words"]) for p in rnd["players"].values()))
    except Exception:
        logger.exception("anagram: could not mark round %s as %s", rnd.get("id"), status)
    if not with_results:
        return
    lines = _results_text(rnd, header)
    if missed:
        extra = await asyncio.to_thread(_missed_words, rnd)
        if extra:
            lines += ["", "💡 Words you missed: " + ", ".join(extra)]
    lines += ["", "Play again with /anagram"]
    try:
        await D.safe_send(context.bot, chat_id, text="\n".join(lines), parse_mode="HTML")
    except Exception:
        logger.exception("anagram: results could not be sent to %s", chat_id)


async def _end_job(context):
    chat_id = context.job.chat_id
    rid = (context.job.data or {}).get("rid")
    rnd = ACTIVE.get(chat_id)
    if rnd is None or rnd["id"] != rid:
        return                      # a stale timer: that round is already over
    ACTIVE.pop(chat_id, None)       # no await before this line: the round can only end once
    await _finish(context, chat_id, rnd, "⏱ <b>ANAGRAM - Time's up!</b>", "ended")


# ---------------------------------------------------------------- guesses (hot path)
async def on_text(update, context):
    chat = update.effective_chat
    rnd = ACTIVE.get(chat.id) if chat is not None else None
    if rnd is None:
        return                                      # nothing running here: no work, no database
    message, user = update.message, update.effective_user
    if message is None or not message.text or user is None or getattr(user, "is_bot", False):
        return
    word = message.text.strip().lower()
    if len(word) < MIN_WORD_LETTERS or not _WORD_RE.fullmatch(word):
        return                                      # shorter than 5 letters, or not one plain word: ignored
    bot, mid = context.bot, message.message_id
    if (len(word) > len(rnd["base"]) or word == rnd["base"] or (Counter(word) - rnd["letters"])
            or word in rnd["found"]):
        await D.safe_react(bot, chat.id, mid, "👎")     # letters not available, the shown word, or already found
        return
    if not await asyncio.to_thread(is_real_word, word):
        await D.safe_react(bot, chat.id, mid, "👎")     # not an accepted English word
        return
    if ACTIVE.get(chat.id) is not rnd:
        return                                      # the round ended while we were checking
    if word in rnd["found"]:
        await D.safe_react(bot, chat.id, mid, "👎")
        return
    rnd["found"][word] = user.id                    # claimed before any await: two players can never both get it
    reward = rnd["reward"]
    try:
        await asyncio.to_thread(D.db.anagram_credit, user.id, chat.id, user.first_name or "Player", reward)
    except Exception:
        rnd["found"].pop(word, None)
        logger.exception("anagram: crediting %s failed in %s", user.id, chat.id)
        return
    p = rnd["players"].setdefault(user.id, {"name": user.first_name or "Player", "words": [], "earned": 0})
    p["words"].append(word)
    p["earned"] += reward
    await D.safe_react(bot, chat.id, mid, "👍")        # the money is already in; the only feedback is the 👍 (no "+1" message)


# ---------------------------------------------------------------- commands
async def _blocked_by_other_game(chat_id):
    """Text explaining which other game is running here, or None."""
    db = D.db
    if chat_id in ACTIVE:
        return "🔀 An Anagram game is already running here - find words, or wait for it to end!"
    if await asyncio.to_thread(db.scramble_get_round, chat_id):
        return "🎮 A Word Scramble round is in progress here - wait for it to end before starting an Anagram."
    game = await asyncio.to_thread(db.get_game, chat_id)
    if game and game[1] is not None:
        return "🎮 A Word Game is in progress here - wait for it to end before starting an Anagram."
    if await asyncio.to_thread(db.tournament_get_open_for_chat, chat_id):
        return "🏟️ A tournament is in progress here - wait for it to end before starting an Anagram."
    if await asyncio.to_thread(db.elim_get_open_for_chat, chat_id):
        return "🔥 An elimination is in progress here - wait for it to end before starting an Anagram."
    if await asyncio.to_thread(db.hack_get_open_for_chat, chat_id):
        return "💻 A hack game is in progress here - wait for it to end before starting an Anagram."
    return None


async def start_command(update, context):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("🔀 Anagram is played in groups - add me to a group and run /anagram there!")
        return
    db = D.db
    if await asyncio.to_thread(db.get_config, "scramble_maintenance_mode", "0") == "1":
        await update.message.reply_text("🛠️ Games are currently under maintenance. Please try again later.")
        return
    if chat.id in ACTIVE:                                     # fast answer, no database
        await update.message.reply_text(await _blocked_by_other_game(chat.id))
        return
    if await asyncio.to_thread(db.get_effective_admin_mode, chat.id):
        if not await D.bot_is_admin_in_chat(context, chat.id):
            await update.message.reply_text(
                "🙏 To start a game here, I'll need to be a group admin. Please make me an admin, then run /anagram again!")
            return
    blocked = await _blocked_by_other_game(chat.id)
    if blocked:
        await update.message.reply_text(blocked)
        return
    base = _pick_base(chat.id)
    if not base:
        await update.message.reply_text("⚠️ There are no long words available right now - please tell the bot owner.")
        return

    user = update.effective_user
    now = _now()
    reward = await asyncio.to_thread(get_reward)
    rnd = {"id": None, "base": base, "letters": Counter(base), "found": {}, "players": {},
           "reward": reward, "started_by": user.id, "started_at": now, "job": None}
    ACTIVE[chat.id] = rnd                                     # reserved BEFORE any await: no second start can slip in
    try:
        rnd["id"] = await asyncio.to_thread(
            db.anagram_round_start, chat.id, base, user.id, now.isoformat(),
            (now + timedelta(seconds=ROUND_SECONDS)).isoformat())
        if rnd["id"] is None:
            raise RuntimeError("another round is already recorded for this chat")
        sent = await D.safe_send(
            context.bot, chat.id, raise_permanent=True, parse_mode="HTML",
            text=(f"🔀 <b>ANAGRAM</b> 🔀\n\n"
                  f"Make as many words as you can from these letters:\n\n"
                  f"<b>{_shuffle(base)}</b>\n\n"
                  f"⏱ 2 minutes • 5+ letters • 💵{D.fmt_coins(reward)} per word\n"
                  f"Just type a word - no command needed."))
        if sent is None:
            raise RuntimeError("the start message was not delivered")
    except Exception:
        logger.exception("anagram: could not start in %s", chat.id)
        if ACTIVE.get(chat.id) is rnd:
            ACTIVE.pop(chat.id, None)
        if rnd["id"] is not None:
            try:
                await asyncio.to_thread(db.anagram_round_finish, rnd["id"], "cancelled", 0)
            except Exception:
                pass
        await update.message.reply_text("⚠️ Couldn't start the Anagram right now - try /anagram again in a moment.")
        return
    jq = context.job_queue
    if jq is not None:
        rnd["job"] = jq.run_once(_end_job, ROUND_SECONDS, chat_id=chat.id, name=f"anagram_end_{chat.id}",
                                 data={"rid": rnd["id"]})


def _need_round(chat_id):
    return ACTIVE.get(chat_id)


async def words_command(update, context):
    rnd = _need_round(update.effective_chat.id)
    if rnd is None:
        await update.message.reply_text("No Anagram game is running here - start one with /anagram")
        return
    words = list(rnd["found"])
    if not words:
        await update.message.reply_text("Nobody has found a word yet.")
        return
    await update.message.reply_html(f"🔀 <b>Words found so far ({len(words)})</b>\n" + ", ".join(D.esc(w) for w in words))


async def score_command(update, context):
    rnd = _need_round(update.effective_chat.id)
    if rnd is None:
        await update.message.reply_text("No Anagram game is running here - start one with /anagram")
        return
    lines = _results_text(rnd, "🔀 <b>Anagram - scores so far</b>")
    lines = [l for l in lines if not l.startswith("The word was")]       # never reveal the word mid-round
    await update.message.reply_html("\n".join(lines))


async def stop_command(update, context):
    chat, user = update.effective_chat, update.effective_user
    if chat.type == "private":
        await update.message.reply_text("🔀 Run /stopanagram inside the group.")
        return
    if not (D.is_owner(user.id) or await D.is_chat_admin(update, context)):
        await update.message.reply_text("🚫 Only group admins (or the bot owner) can stop an Anagram game.")
        return
    rnd = ACTIVE.pop(chat.id, None)                          # no await between get and pop: stops exactly once
    if rnd is None:
        await update.message.reply_text("No Anagram game is running here.")
        return
    await _finish(context, chat.id, rnd, "🛑 <b>ANAGRAM - stopped by an admin</b>", "stopped")


def help_text():
    return (
        "🔀 <b>Anagram - How to Play</b>\n\n"
        "1️⃣ Run /anagram - I show a long word with its letters mixed up.\n"
        "2️⃣ Within 2 minutes, type as many real English words as you can using ONLY those letters "
        "(each letter as many times as it is shown).\n"
        "3️⃣ Words need 5+ letters (shorter ones are ignored). A word I accept gets a 👍 and pays you 💵" + D.fmt_coins(get_reward()) + ". "
        "A word I don't accept (or one that was already found) gets a 👎.\n"
        "4️⃣ At the end I show the word, the ranking and some words you missed.\n\n"
        "/anagramwords - words found so far\n"
        "/anagramscore - the scores so far\n"
        "/stopanagram - end the game (group admins / bot owner)\n\n"
        "Only one game runs per group at a time."
    )


async def help_command(update, context):
    await update.message.reply_html(help_text())


async def setearnings_command(update, context):
    user = update.effective_user
    if not D.is_owner(user.id):
        await update.message.reply_text("🚫 This command is for the bot owner.")
        return
    current = await asyncio.to_thread(get_reward)
    arg = (context.args[0] if context.args else "").replace(",", "")
    if not arg.isdigit() or not (1 <= int(arg) <= MAX_REWARD):
        await update.message.reply_text(
            f"Usage: /setanagramearnings <amount>  (1 - {MAX_REWARD:,})\nCurrently: {D.DOLLAR}{current} per word")
        return
    await asyncio.to_thread(D.db.set_config, REWARD_CONFIG_KEY, str(int(arg)))
    await update.message.reply_text(f"✅ Each Anagram word now pays {D.DOLLAR}{int(arg):,}.")


# ---------------------------------------------------------------- stop-all hook and restart behaviour
async def stop_all(context):
    """/stopallactivities: ends every running Anagram quietly (main.py sends the one notice per group)."""
    out = {}
    for chat_id in list(ACTIVE):
        rnd = ACTIVE.pop(chat_id, None)
        if rnd is None:
            continue
        await _finish(context, chat_id, rnd, "", "stopped", with_results=False)
        out[chat_id] = "Anagram game"
    return out


async def restore_job(context):
    """At startup: a round recorded as active belongs to a bot that was restarted - end it politely (once)."""
    rows = await asyncio.to_thread(D.db.anagram_interrupt_active, (_now() - timedelta(seconds=30)).isoformat())
    for chat_id, base in rows:
        try:
            await D.safe_send(context.bot, chat_id, parse_mode="HTML",
                              text=f"🔀 The Anagram round ended because the bot restarted. The word was "
                                   f"<b>{D.esc(base.upper())}</b>. Words already found were paid. Start a new one with /anagram")
        except Exception:
            logger.info("anagram: restart notice for %s not delivered", chat_id)
        await asyncio.sleep(0.3)


def is_running(chat_id):
    return chat_id in ACTIVE


def install(app, deps):
    """Called once from main.py. Registers the commands and the word handler (handler group 4)."""
    global D
    D = deps
    from telegram.ext import CommandHandler, MessageHandler, filters
    for name, fn in (("anagram", start_command), ("anagramwords", words_command), ("anagramscore", score_command),
                     ("stopanagram", stop_command), ("anagramhelp", help_command),
                     ("setanagramearnings", setearnings_command)):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, on_text), group=4)
    deps.register_stopall_hook(stop_all)
    if app.job_queue is not None:
        app.job_queue.run_once(restore_job, 10, name="anagram_restore")
