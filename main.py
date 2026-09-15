import os
import ast
import random
import re
import asyncio
import logging
import importlib
import html as html_lib
import urllib.parse
from datetime import datetime, timedelta
from types import SimpleNamespace

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji, LinkPreviewOptions
from telegram.constants import ChatType
from telegram.error import Forbidden, BadRequest, TimedOut, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    TypeHandler,
    ContextTypes,
    ApplicationHandlerStop,
    filters,
)

import database as db
import medium_words_raw as mw

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

DOLLAR = "💵"

EXPLAIN_TIMEOUT_SECONDS = 90

MAX_LEADERBOARD_NAME_LEN = 20

_owner_id_raw = os.environ.get("OWNER_ID")
if not _owner_id_raw:
    raise RuntimeError(
        "OWNER_ID is not set. Set it in your environment (or .env file) "
        "to your numeric Telegram user ID - get it from @userinfobot."
    )
try:
    OWNER_ID = int(_owner_id_raw)
except ValueError:
    raise RuntimeError(f"OWNER_ID must be a number, got: {_owner_id_raw!r}")

START_TIME = datetime.now()

DEFAULT_REWARDS = {"win": 1, "cheat": -1}


def is_owner(user_id) -> bool:
    return user_id == OWNER_ID


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not is_owner(user.id):
            if update.message:
                await update.message.reply_text(
                    "🚫 This command is restricted to the bot owner."
                )
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


def is_bot_admin_or_owner(user_id) -> bool:
    return is_owner(user_id) or db.is_bot_admin(user_id)


def owner_or_admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not is_bot_admin_or_owner(user.id):
            if update.message:
                await update.message.reply_text(
                    "🚫 This command is restricted to the bot owner or bot admins."
                )
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


def get_reward(kind: str) -> int:
    val = db.get_config(f"reward_{kind}")
    if val is None:
        return DEFAULT_REWARDS[kind]
    try:
        return int(val)
    except ValueError:
        return DEFAULT_REWARDS[kind]


def format_uptime(delta) -> str:
    total_seconds = int(delta.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def esc(text) -> str:
    if text is None:
        return ""
    return html_lib.escape(str(text), quote=False)


def truncate_name(name: str) -> str:
    if name is None:
        return ""
    if len(name) > MAX_LEADERBOARD_NAME_LEN:
        name = name[:MAX_LEADERBOARD_NAME_LEN] + "..."
    return esc(name)


async def safe_send(bot, chat_id, retries=2, delay=2, **kwargs):
    for attempt in range(retries + 1):
        try:
            return await bot.send_message(chat_id=chat_id, **kwargs)
        except (TimedOut, NetworkError) as e:
            if attempt == retries:
                logger.error(f"send_message to {chat_id} failed after retries: {e}")
                return None
            await asyncio.sleep(delay)


async def safe_react(bot, chat_id, message_id, emoji):
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception as e:
        logger.debug(f"set_message_reaction failed in {chat_id}: {e}")


def apply_chat_credit_for_global_earning(user_id, name, amount, current_chat=None):
    if current_chat is not None and current_chat.type != ChatType.PRIVATE:
        db.credit_chat_score(user_id, current_chat.id, name, amount)
        return
    last_chat = db.get_last_chat(user_id)
    if last_chat:
        db.credit_chat_score(user_id, last_chat, name, amount)


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    previous_row = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current_row = [i]
        for j, cb in enumerate(b, start=1):
            insertions = previous_row[j] + 1
            deletions = current_row[j - 1] + 1
            substitutions = previous_row[j - 1] + (ca != cb)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


def is_close_guess(guess: str, answer: str) -> bool:
    if not guess or not answer or guess == answer:
        return False

    if guess + "s" == answer or answer + "s" == guess:
        return True
    if guess + "es" == answer or answer + "es" == guess:
        return True

    distance = _levenshtein(guess, answer)
    length = max(len(guess), len(answer))

    if length <= 5:
        return distance == 1
    elif length <= 9:
        return distance <= 2
    else:
        return distance <= 3


def build_message_link(chat, message_id):
    if message_id is None or chat is None:
        return None
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}/{message_id}"
    chat_id_str = str(chat.id)
    if chat_id_str.startswith("-100"):
        return f"https://t.me/c/{chat_id_str[4:]}/{message_id}"
    return None


def format_numbered_hints(log: str, chat, leader_message_id) -> str:
    lines = [line for line in log.split("\n") if line.strip()]
    link = build_message_link(chat, leader_message_id)

    formatted = []
    for i, line in enumerate(lines, start=1):
        number = f'<a href="{link}">{i}</a>' if link else str(i)
        formatted.append(f"{number}. {esc(line)}")
    return "\n".join(formatted)


BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not set. Set it in your environment (or .env file) "
        "to the token @BotFather gave you."
    )

BOT_USERNAME = os.environ.get("BOT_USERNAME", "")

ADS_BOT_USERNAME = os.environ.get("ADS_BOT_USERNAME", "UFFAdsBot")

EASY_WORDS = [
    "Apple", "Football", "Lion", "Tiger", "Laptop", "Banana", "School",
    "Doctor", "River", "Mountain", "Bottle", "Keyboard", "Elephant",
    "Phone", "Pizza", "Chair", "Table", "Window", "Guitar", "Bicycle",
    "Umbrella", "Rainbow", "Butterfly", "Sandwich", "Toothbrush",
    "Balloon", "Camera", "Candle", "Clock", "Dolphin", "Envelope",
    "Fireplace", "Giraffe", "Hammer", "Island", "Jacket", "Kangaroo",
    "Ladder", "Mirror", "Necklace", "Octopus", "Pillow", "Rocket",
    "Sandcastle", "Telescope", "Volcano", "Waterfall", "Airport",
    "Basketball", "Cupcake", "Diamond", "Eyeglasses", "Flashlight",
]

HARD_WORDS = [
    "Personification", "Bureaucracy", "Metamorphosis", "Renaissance",
    "Philosophy", "Democracy", "Photosynthesis", "Entrepreneur",
    "Constellation", "Archaeology", "Camouflage", "Serendipity",
    "Procrastination", "Hallucination", "Improvisation", "Nostalgia",
    "Diplomacy", "Infrastructure", "Ecosystem", "Algorithm",
    "Hypothesis", "Symmetry", "Turbulence", "Equilibrium",
    "Cryptography", "Existentialism", "Onomatopoeia", "Paradox",
    "Xenophobia", "Bibliophile", "Claustrophobia", "Ambiguity",
    "Juxtaposition", "Melancholy", "Resilience", "Obsolescence",
    "Aristocracy", "Chronology", "Extraterrestrial", "Introspection",
]


def _build_medium_words():
    lists = [
        mw.ANIMALS, mw.FOOD_DRINK, mw.SPORTS_GAMES, mw.PROFESSIONS,
        mw.HOUSEHOLD, mw.NATURE_WEATHER, mw.PLACES_BUILDINGS,
        mw.TRANSPORTATION, mw.TECH_TOOLS, mw.CLOTHING, mw.BODY_ABSTRACT,
        mw.ACTIONS, mw.MUSIC_INSTRUMENTS, mw.SCHOOL_OFFICE, mw.MISC,
        mw.COLORS_SHAPES, mw.EMOTIONS_EXTRA, mw.WEATHER_CLIMATE,
        mw.SPACE_ASTRONOMY, mw.VEHICLES_EXTRA, mw.WORKPLACE_JOBS,
        mw.FRUITS_VEGETABLES_EXTRA, mw.TOOLS_MATERIALS,
        mw.HEALTH_BODY_EXTRA, mw.GEOGRAPHY_EXTRA, mw.MUSIC_ARTS_EXTRA,
        mw.TECH_MODERN, mw.FANTASY_ADVENTURE_EXTRA, mw.SPORTS_EXTRA,
        mw.BUSINESS_ECONOMY, mw.LAW_GOVERNMENT, mw.MYTHOLOGY_LEGENDS,
        mw.GARDENING_PLANTS, mw.DESSERTS_SNACKS, mw.MUSIC_GENRES_DANCE,
        mw.PROGRAMMING_MATH, mw.FURNITURE_HOME_EXTRA, mw.WILDLIFE_EXTRA,
        mw.TRAVEL_TOURISM, mw.MYSTERY_CRIME, mw.CIRCUS_ENTERTAINMENT,
        mw.MILITARY_WARFARE, mw.WEDDING_FAMILY, mw.DINING_RESTAURANT,
        mw.PHOTOGRAPHY_MEDIA, mw.OUTDOORS_CAMPING, mw.FASHION_STYLE_EXTRA,
        mw.HOLIDAYS_FESTIVALS, mw.SCIENCE_LAB_EXTRA,
        getattr(mw, "OWNER_ADDED", []),
    ]
    seen = set()
    merged = []
    for lst in lists:
        for w in lst:
            key = w.lower()
            if key not in seen:
                seen.add(key)
                merged.append(w)
    return merged


MEDIUM_WORDS = _build_medium_words()


def _merge_all_words():
    seen = set()
    merged = []
    for w in EASY_WORDS + MEDIUM_WORDS + HARD_WORDS:
        key = w.lower()
        if key not in seen:
            seen.add(key)
            merged.append(w)
    return merged


WORDS = _merge_all_words()


WORDS_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "medium_words_raw.py")

_OWNER_ADDED_MARKER = "OWNER_ADDED = ["


def _read_owner_added_words():
    if not os.path.exists(WORDS_FILE_PATH):
        return []
    with open(WORDS_FILE_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    if _OWNER_ADDED_MARKER not in content:
        return []
    start = content.index(_OWNER_ADDED_MARKER) + len(_OWNER_ADDED_MARKER)
    end = content.index("]", start)
    body = content[start:end]
    try:
        words = ast.literal_eval("[" + body + "]")
    except Exception:
        words = []
    return [w for w in words if isinstance(w, str)]


def _write_owner_added_words(words):
    with open(WORDS_FILE_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    formatted = ",\n".join(f'    "{w}"' for w in words)
    new_block = f"OWNER_ADDED = [\n{formatted}\n]" if words else "OWNER_ADDED = [\n]"

    if _OWNER_ADDED_MARKER in content:
        start = content.index(_OWNER_ADDED_MARKER)
        end = content.index("]", start) + 1
        content = content[:start] + new_block + content[end:]
    else:
        content = content.rstrip("\n") + (
            "\n\n# Words added via the /addwords command - managed programmatically.\n"
            "# Do not edit this list by hand while the bot is running.\n"
            + new_block + "\n"
        )

    with open(WORDS_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(content)


@owner_only
async def addwords_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /addwords word1, word2, word3\n"
            "(comma or newline separated - single alphabetic words only)"
        )
        return

    raw = " ".join(context.args)
    candidates = [w.strip() for w in re.split(r"[,\n]", raw) if w.strip()]

    if not candidates:
        await update.message.reply_text("❌ No valid words provided.")
        return

    global MEDIUM_WORDS, WORDS, SCRAMBLE_WORDS
    existing_lower = {w.lower() for w in WORDS}
    seen_this_call = set()

    to_add = []
    already_existing = []
    invalid = []

    for w in candidates:
        cleaned = w.strip()
        if not re.fullmatch(r"[A-Za-z]+", cleaned):
            invalid.append(cleaned)
            continue
        normalized = cleaned.capitalize()
        key = normalized.lower()
        if key in existing_lower or key in seen_this_call:
            already_existing.append(normalized)
            continue
        seen_this_call.add(key)
        to_add.append(normalized)

    if not to_add:
        if len(candidates) == 1 and already_existing and not invalid:
            await update.message.reply_text(f"⚠️ {already_existing[0]} already existed.")
            return
        msg = "⚠️ No new words were added."
        if already_existing:
            msg += f"\n\n*These already existing words were not added*:\n{', '.join(already_existing)}"
        if invalid:
            msg += f"\n\n❌ Invalid (letters only, skipped): {', '.join(invalid)}"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    owner_added = _read_owner_added_words()
    owner_added_lower = {w.lower() for w in owner_added}
    for w in to_add:
        if w.lower() not in owner_added_lower:
            owner_added.append(w)
            owner_added_lower.add(w.lower())

    try:
        _write_owner_added_words(owner_added)
    except OSError as e:
        logger.error(f"Failed to write medium_words_raw.py: {e}")
        await update.message.reply_text("❌ Failed to save the new words - check the bot's file permissions.")
        return

    importlib.reload(mw)
    MEDIUM_WORDS = _build_medium_words()
    WORDS = _merge_all_words()
    SCRAMBLE_WORDS = _build_scramble_words()

    msg = f"✅ Successful. Added: {', '.join(to_add)}"
    if already_existing:
        msg += f"\n\n*These already existing words were not added*:\n{', '.join(already_existing)}"
    if invalid:
        msg += f"\n\n❌ Invalid (letters only, skipped): {', '.join(invalid)}"

    await update.message.reply_text(msg, parse_mode="Markdown")


# =======================================================================
# WORD SCRAMBLE
# =======================================================================

SCRAMBLE_ROUND_SECONDS = 120
DEFAULT_SCRAMBLE_REWARD = 5
SCRAMBLE_STREAK_MILESTONES = {3: 5, 5: 10, 10: 25, 20: 50}


def get_scramble_reward() -> int:
    val = db.get_config("reward_scramble_win")
    if val is None:
        return DEFAULT_SCRAMBLE_REWARD
    try:
        return int(val)
    except ValueError:
        return DEFAULT_SCRAMBLE_REWARD


def _build_scramble_words():
    seen = set()
    words = []
    for w in _build_medium_words():
        if not w or not re.fullmatch(r"[A-Za-z]+", w):
            continue
        if len(w) < 3:
            continue
        key = w.lower()
        if key in seen:
            continue
        seen.add(key)
        words.append(w)
    return words


SCRAMBLE_WORDS = _build_scramble_words()


def scramble_letters(word: str) -> str:
    letters = list(word)
    for _ in range(10):
        random.shuffle(letters)
        scrambled = "".join(letters)
        if scrambled.lower() != word.lower():
            return scrambled
    return "".join(letters)


def scramble_random_word(chat_id):
    if not SCRAMBLE_WORDS:
        return None

    used = db.scramble_get_used_words(chat_id)
    choices = [w for w in SCRAMBLE_WORDS if w not in used]

    if not choices:
        db.scramble_reset_used_words(chat_id)
        choices = list(SCRAMBLE_WORDS)

    word = random.choice(choices)
    db.scramble_mark_word_used(chat_id, word)
    return word


def _parse_duration(text):
    if not text:
        return None
    m = re.fullmatch(r"(\d+)([mhd])", text.strip().lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        return None
    mult = {"m": 60, "h": 3600, "d": 86400}[unit]
    return n * mult


def _format_duration(seconds):
    if seconds and seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _format_duration_words(seconds):
    if not seconds or seconds <= 0:
        return "0 seconds"
    if seconds % 86400 == 0:
        n, unit = seconds // 86400, "day"
    elif seconds % 3600 == 0:
        n, unit = seconds // 3600, "hour"
    elif seconds % 60 == 0:
        n, unit = seconds // 60, "minute"
    else:
        n, unit = seconds, "second"
    return f"{n} {unit}{'s' if n != 1 else ''}"


SCRAMBLE_GROUPS_ONLY_TEXT = """
🎮 <b>WORD SCRAMBLE</b> 🎮

This game is designed for group competition.

Add me to a group to start playing, challenge other players, and compete for rewards! 🏆

👥 Bring your friends and let the games begin!
"""


def _slowmode_enabled(chat_id):
    row = db.get_scramble_slowmode(chat_id)
    return bool(row and row[1])


def _scramble_timeout_job_name(chat_id):
    return f"scramble_timeout_{chat_id}"


def cancel_scramble_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id):
    if context.job_queue is None:
        return
    for job in context.job_queue.get_jobs_by_name(_scramble_timeout_job_name(chat_id)):
        job.schedule_removal()


def schedule_scramble_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id):
    if context.job_queue is None:
        return
    cancel_scramble_timeout(context, chat_id)
    context.job_queue.run_once(
        scramble_timeout_callback,
        SCRAMBLE_ROUND_SECONDS,
        chat_id=chat_id,
        name=_scramble_timeout_job_name(chat_id),
    )


def restore_stale_scramble_rounds(job_queue):
    if job_queue is None:
        return

    now = datetime.now()

    for chat_id, started_at in db.get_all_active_scramble_rounds_with_time():
        remaining = 0
        try:
            started_dt = datetime.fromisoformat(started_at)
            remaining = SCRAMBLE_ROUND_SECONDS - (now - started_dt).total_seconds()
        except (TypeError, ValueError):
            remaining = 0

        if remaining <= 0:
            # Already past its 2-minute window by the time we came back up.
            # Fire the real timeout callback almost immediately instead of
            # just deleting the round - that's what sends the "Time's up"
            # message and (if slowmode/automatic mode is on) starts the
            # next round, exactly as if it had timed out normally.
            remaining = 1

        job_queue.run_once(
            scramble_timeout_callback,
            remaining,
            chat_id=chat_id,
            name=_scramble_timeout_job_name(chat_id),
        )


def restore_stale_slowmode_waits(job_queue):
    """Resume the wait for the next slowmode round across a restart, using
    the persisted due time - so a chat that was 40 seconds from its next
    round before the restart is still ~40 seconds away after it, instead
    of the wait resetting to the full interval."""
    if job_queue is None:
        return

    now = datetime.now()
    handled = set()

    for chat_id, interval_seconds, due_at in db.get_pending_slowmode_waits():
        if db.scramble_get_round(chat_id):
            # A round is already active for this chat - the scramble-round
            # restore pass handles its own timeout; nothing to wait for.
            db.scramble_clear_next_round_due(chat_id)
            handled.add(chat_id)
            continue

        try:
            due_dt = datetime.fromisoformat(due_at)
            remaining = (due_dt - now).total_seconds()
        except (TypeError, ValueError):
            remaining = 0

        if remaining <= 0:
            remaining = 1

        cancel_slowmode_next_job(job_queue, chat_id)
        job_queue.run_once(
            slowmode_next_round_callback,
            remaining,
            chat_id=chat_id,
            name=_slowmode_next_job_name(chat_id),
        )
        handled.add(chat_id)

    # Chats with slowmode enabled but no persisted due time yet (e.g. the
    # gap started before this upgrade) - fall back to a fresh full
    # interval so they don't end up waiting forever.
    for chat_id, interval_seconds in db.get_all_active_slowmode_chats():
        if chat_id in handled or db.scramble_get_round(chat_id):
            continue
        cancel_slowmode_next_job(job_queue, chat_id)
        job_queue.run_once(
            slowmode_next_round_callback,
            interval_seconds,
            chat_id=chat_id,
            name=_slowmode_next_job_name(chat_id),
        )
        db.scramble_set_next_round_due(
            chat_id, (now + timedelta(seconds=interval_seconds)).isoformat()
        )


def restore_stale_word_games(job_queue):
    """End any /wordgame round left stuck mid-explain from before a
    restart and reopen the floor for a new leader - the same as if
    /wordgame had just been run fresh."""
    if job_queue is None:
        return

    for chat_id in db.get_all_active_word_game_chats():
        db.start_game(chat_id)
        job_queue.run_once(
            _resend_wordgame_prompt,
            1,
            chat_id=chat_id,
            name=f"wordgame_restore_{chat_id}",
        )


async def _resend_wordgame_prompt(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await safe_send(
        context.bot,
        chat_id,
        text=(
            "🎮 <b>UFF Word Game</b>\n\n"
            "Who wants to explain?"
        ),
        parse_mode="HTML",
        reply_markup=volunteer_keyboard(),
    )


async def scramble_timeout_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.chat_id

    word = await asyncio.to_thread(db.scramble_claim_round, chat_id)
    if not word:
        return

    slow = await asyncio.to_thread(db.get_scramble_slowmode, chat_id)
    slow_enabled = bool(slow and slow[1])

    text = (
        f"⏰ <b>Time's up!</b>\n\n"
        f"No one guessed it. The word was: <b>{esc(word)}</b>"
    )

    if slow_enabled:
        interval_seconds = slow[0]
        text += f"\n\n🐢New word will be sent in {_format_duration_words(interval_seconds)}."
        await safe_send(context.bot, chat_id, text=text, parse_mode="HTML")
        await schedule_slowmode_next_round(context.job_queue, chat_id, interval_seconds)
        return

    _, auto_enabled, _ = await asyncio.to_thread(db.scramble_get_auto_mode, chat_id)
    if not auto_enabled:
        text += "\n\nPlay again with /wordscramble"

    await safe_send(context.bot, chat_id, text=text, parse_mode="HTML")

    await _scramble_auto_continue(context, chat_id, won=False)


async def _scramble_auto_continue(context: ContextTypes.DEFAULT_TYPE, chat_id, won: bool, chat_obj=None):
    _, auto_enabled, _ = await asyncio.to_thread(db.scramble_get_auto_mode, chat_id)
    if not auto_enabled:
        return

    result = await asyncio.to_thread(db.scramble_record_round_result, chat_id, won)
    if result is None:
        return
    _, new_miss_streak = result

    if not won and new_miss_streak >= 3:
        await asyncio.to_thread(db.scramble_set_auto_enabled, chat_id, False)
        await safe_send(
            context.bot,
            chat_id,
            text="🤖 Automatic Mode turned off - no one answered the last 3 rounds in a row.",
        )
        return

    chat = chat_obj
    if chat is None:
        try:
            chat = await context.bot.get_chat(chat_id)
        except Exception:
            return

    await start_scramble_round(context, chat)


async def start_scramble_round(context: ContextTypes.DEFAULT_TYPE, chat):
    maintenance = await asyncio.to_thread(db.get_config, "scramble_maintenance_mode", "0")
    if maintenance == "1":
        return "maintenance"

    if await asyncio.to_thread(db.scramble_get_round, chat.id):
        return False

    word = await asyncio.to_thread(scramble_random_word, chat.id)
    if not word:
        return None

    scrambled = scramble_letters(word)
    await asyncio.to_thread(db.scramble_start_round, chat.id, word, scrambled)

    if chat.type != ChatType.PRIVATE:
        await asyncio.to_thread(db.scramble_register_group, chat.id, chat.title or str(chat.id))

    await safe_send(
        context.bot,
        chat.id,
        text=(
            f"🎮 <b>WORD SCRAMBLE</b> 🎮\n\n"
            f"Unscramble this word:\n\n"
            f"<b>{esc(scrambled)}</b>\n\n"
            f"First to type the correct word in the next 2 minutes wins {DOLLAR}{get_scramble_reward()}"
        ),
        parse_mode="HTML",
    )
    schedule_scramble_timeout(context, chat.id)
    return True


async def wordscramble_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    if chat.type == ChatType.PRIVATE:
        await update.message.reply_html(
            SCRAMBLE_GROUPS_ONLY_TEXT,
            reply_markup=add_to_group_keyboard(context),
        )
        return

    user = update.effective_user
    await asyncio.to_thread(db.scramble_increment_played, user.id, chat.id, user.first_name)

    if _slowmode_enabled(chat.id) and not await asyncio.to_thread(db.scramble_get_round, chat.id):
        interval = (await asyncio.to_thread(db.get_scramble_slowmode, chat.id))[0]
        await update.message.reply_text(
            f"🐢 Slow mode is on - a new round starts automatically every {_format_duration_words(interval)}. Please wait."
        )
        return

    result = await start_scramble_round(context, chat)

    if result is False:
        await update.message.reply_text(
            "🎮 A Word Scramble round is already in progress here - guess it, or wait for it to end!"
        )
    elif result is None:
        await update.message.reply_text(
            "⚠️ The Word Scramble word list is currently empty - please let the bot owner know."
        )
    elif result == "maintenance":
        await update.message.reply_text(
            "🛠️ Word Scramble is currently under maintenance. Please try again later."
        )


async def _scramble_handle_win(context: ContextTypes.DEFAULT_TYPE, chat, user, message, guessed_word_case):
    word = await asyncio.to_thread(db.scramble_claim_round, chat.id)
    if not word:
        return

    cancel_scramble_timeout(context, chat.id)

    await safe_react(context.bot, chat.id, message.message_id, "🎉")

    reward = get_scramble_reward()

    await asyncio.to_thread(db.scramble_reset_all_streaks_in_chat, chat.id, user.id)
    new_streak = await asyncio.to_thread(db.scramble_record_win, user.id, chat.id, user.first_name, reward)

    bonus = SCRAMBLE_STREAK_MILESTONES.get(new_streak)
    total = reward
    if bonus:
        await asyncio.to_thread(db.scramble_add_bonus, user.id, chat.id, user.first_name, bonus)
        total += bonus

    mention = f'<a href="tg://user?id={user.id}">{esc(user.first_name)}</a>'

    lines = [
        "🎉 <b>Winner!</b>",
        f"{mention} got it, <b>{esc(word)}</b>! +{DOLLAR}{reward}",
    ]
    if bonus:
        lines.append(f"🔥 {new_streak}-win streak! Bonus: +{DOLLAR}{bonus}")
        lines.append(f"Total: {DOLLAR}{total}")

    slow = await asyncio.to_thread(db.get_scramble_slowmode, chat.id)
    slow_enabled = bool(slow and slow[1])

    if slow_enabled:
        interval_seconds = slow[0]
        lines.append(f"🐢New word will be sent in {_format_duration_words(interval_seconds)}.")
        await safe_send(context.bot, chat.id, text="\n".join(lines), parse_mode="HTML")
        await schedule_slowmode_next_round(context.job_queue, chat.id, interval_seconds)
        return

    _, auto_enabled, _ = await asyncio.to_thread(db.scramble_get_auto_mode, chat.id)

    if auto_enabled:
        await safe_send(context.bot, chat.id, text="\n".join(lines), parse_mode="HTML")
        await _scramble_auto_continue(context, chat.id, won=True, chat_obj=chat)
        return

    lines.append("")
    lines.append("Play again with /wordscramble")
    await safe_send(context.bot, chat.id, text="\n".join(lines), parse_mode="HTML")


async def _scramble_process_guess(update: Update, context: ContextTypes.DEFAULT_TYPE, chat, user, message):
    round_row = await asyncio.to_thread(db.scramble_get_round, chat.id)
    if not round_row:
        return

    _, word, scrambled, started_at, _ = round_row
    text = message.text.strip()
    guess_clean = re.sub(r"[^\w\s]", "", text.lower()).strip()
    word_lower = word.lower()

    if not guess_clean:
        return

    await asyncio.to_thread(db.scramble_increment_played, user.id, chat.id, user.first_name)

    if guess_clean == word_lower:
        await _scramble_handle_win(context, chat, user, message, text)
        return

    if is_close_guess(guess_clean, word_lower):
        await safe_react(context.bot, chat.id, message.message_id, "👀")


async def scramble_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
    await _scramble_process_guess(update, context, update.effective_chat, update.effective_user, message)


async def scramble_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
    user = update.effective_user
    if await asyncio.to_thread(db.get_pending_input, user.id):
        return
    await _scramble_process_guess(update, context, update.effective_chat, user, message)


def scramble_help_text() -> str:
    reward = get_scramble_reward()
    return f"""
🔤 <b>Word Scramble - How to Play</b>

1️⃣ Run /wordscramble (or tap 🎮 Play Game) to start a round - works in groups and in my DM.
2️⃣ I'll show you a scrambled word - just type your guess as a normal message, no command needed.
3️⃣ First correct answer within 2 minutes wins {DOLLAR}{reward}.
4️⃣ Close but not quite? I'll react with 👀. Got it exactly right? 🎉

🔥 <b>Win Streaks</b> - win rounds back-to-back in the same chat for bonus 💵:
3 in a row: +{DOLLAR}5
5 in a row: +{DOLLAR}10
10 in a row: +{DOLLAR}25
20 in a row: +{DOLLAR}50
Your streak resets if someone else wins a round before you do.

📚 Words never repeat until every word in the pool has been used - tracked separately for every group and every DM, so nobody's progress affects anyone else's.

⚙️ Only one round can be active per chat at a time - if one's already running, guess it or wait for it to end.

🏆 <b>Rankings</b>
/scrambleleaderboard - this chat's Word Scramble leaderboard
/scrambletopplayers - top 15, this chat or global
/scrambletopgroups - the most active groups

💡 Word Scramble's rewards and rankings are completely separate from the Word Game's - winning one never affects your stats in the other.
"""


async def scramblehelp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(scramble_help_text())


def format_scramble_board(rows, title, position_info=None, requester_name=None) -> str:
    text = f"🔤 <b>{title}</b>\n\n"
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    if not rows:
        text += "No scores yet.\n"
    else:
        for i, (name, wins, earned, streak) in enumerate(rows, start=1):
            prefix = medals.get(i, f"{i}.")
            display_name = truncate_name(name)
            streak_display = f" 🔥{streak}" if streak and streak > 1 else ""
            text += f"{prefix} {display_name}\t{DOLLAR}{earned}{streak_display}\n"

    if position_info:
        pos, name, wins, earned, streak = position_info
        display_name = truncate_name(requester_name or name)
        text += f"\n  Your Position\n{pos}. {display_name} {DOLLAR}{earned}\n"

    text += ad_footer_html()
    return text


async def scrambleleaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    rows = await asyncio.to_thread(db.scramble_get_chat_leaderboard, chat_id)
    position_info = await asyncio.to_thread(db.scramble_get_chat_position, chat_id, user.id)

    await update.message.reply_html(
        format_scramble_board(rows, "Word Scramble - This Chat", position_info, user.first_name),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


def scramble_topplayers_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📍 In This Chat", callback_data="stp_chat"),
            InlineKeyboardButton("🌍 Global", callback_data="stp_global"),
        ]
    ])


def format_scramble_top_players(rows, title, position_info, requester_name, position_label) -> str:
    text = f"🔤 <b>{title}</b>\n\n"
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    if not rows:
        text += "No scores yet.\n"
    else:
        for i, row in enumerate(rows, start=1):
            prefix = medals.get(i, f"{i}.")
            if len(row) == 4:
                name, wins, earned, streak = row
                streak_display = f" 🔥{streak}" if streak and streak > 1 else ""
            else:
                name, wins, earned = row
                streak_display = ""
            display_name = truncate_name(name)
            text += f"{prefix} {display_name}\t{DOLLAR}{earned}{streak_display}\n"

    if position_info:
        if len(position_info) == 5:
            pos, name, wins, earned, streak = position_info
        else:
            pos, name, wins, earned = position_info
        display_name = truncate_name(requester_name or name)
        text += f"\n{position_label}:\n#{pos} • {display_name} • {DOLLAR}{earned}\n"

    text += ad_footer_html()
    return text


async def scrambletopplayers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "🔤 <b>Word Scramble - Top Players</b>\n\nChoose a scope:",
        reply_markup=scramble_topplayers_keyboard(),
    )


async def scrambletopgroups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.scramble_get_top_groups, 10)
    if not rows:
        await update.message.reply_text("No Word Scramble activity recorded yet.")
        return

    lines = ["🔤 <b>Word Scramble - Top 10 Active Groups</b>\n"]
    for i, (title, rounds_played) in enumerate(rows, start=1):
        lines.append(f"{i}. {truncate_name(title or 'Untitled Group')} — {rounds_played:,} Rounds")

    await update.message.reply_html(
        "\n".join(lines) + ad_footer_html(),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def scramblestopgame_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text("🛑 Run /scramblestopgame inside the group that's playing.")
        return

    if not (is_owner(update.effective_user.id) or await is_chat_admin(update, context)):
        await update.message.reply_text("❌ Only group admins (or the bot owner) can use /scramblestopgame.")
        return

    chat = update.effective_chat
    word = await asyncio.to_thread(db.scramble_claim_round, chat.id)
    cancel_scramble_timeout(context, chat.id)

    if not word:
        await update.message.reply_text("❌ There's no active Word Scramble round to stop.")
        return

    await update.message.reply_html(
        f"🛑 <b>Word Scramble round stopped.</b>\n\nThe word was: <b>{esc(word)}</b>\n\nStart a new one with /wordscramble"
    )


@owner_only
async def scramblestats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = await asyncio.to_thread(db.scramble_get_stats)
    text = f"""
📊 <b>Word Scramble Statistics</b>

👥 Players with a score: <b>{s['total_players']}</b>
🏆 Total Wins: <b>{s['total_wins']}</b>
{DOLLAR} Total Rewards Given: <b>{s['total_rewards']}</b>
🎮 Rounds Started (groups): <b>{s['total_group_rounds']}</b>
🟢 Active Rounds Right Now: <b>{s['active_rounds']}</b>
"""
    await update.message.reply_html(text)


@owner_only
async def scramblesetreward_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            f"Usage: /scramblesetreward <amount>\nCurrent: {DOLLAR}{get_scramble_reward()}"
        )
        return
    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Amount must be a whole number.")
        return
    if amount <= 0:
        await update.message.reply_text("❌ Amount must be positive.")
        return

    await asyncio.to_thread(db.set_config, "reward_scramble_win", amount)
    await update.message.reply_text(f"✅ Word Scramble win reward set to {DOLLAR}{amount}. Takes effect immediately.")


@owner_only
async def scramblereloadwords_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SCRAMBLE_WORDS
    importlib.reload(mw)
    SCRAMBLE_WORDS = _build_scramble_words()
    await update.message.reply_text(
        f"🔄 Word Scramble word list reloaded. Total words loaded: {len(SCRAMBLE_WORDS)}"
    )


@owner_only
async def scrambleforceend_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        try:
            target_chat_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Please provide a valid numeric Chat ID.")
            return
    elif update.effective_chat.type != ChatType.PRIVATE:
        target_chat_id = update.effective_chat.id
    else:
        await update.message.reply_text(
            "Usage: /scrambleforceend <Chat ID>  (or run this inside the target group/DM)"
        )
        return

    word = await asyncio.to_thread(db.scramble_claim_round, target_chat_id)
    cancel_scramble_timeout(context, target_chat_id)

    if not word:
        await update.message.reply_text("❌ No active Word Scramble round found there.")
        return

    await safe_send(
        context.bot,
        target_chat_id,
        text=f"🛑 This Word Scramble round was ended by the bot owner.\n\nThe word was: <b>{esc(word)}</b>",
        parse_mode="HTML",
    )

    if update.effective_chat.id != target_chat_id:
        await update.message.reply_text(f"✅ Word Scramble round in {target_chat_id} ended.")


@owner_only
async def scramblestopallgroups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(db.set_config, "scramble_maintenance_mode", "1")

    chat_ids = await asyncio.to_thread(db.get_all_active_scramble_chats)
    stopped = 0

    for chat_id in chat_ids:
        word = await asyncio.to_thread(db.scramble_claim_round, chat_id)
        cancel_scramble_timeout(context, chat_id)
        if word:
            stopped += 1
            await safe_send(
                context.bot,
                chat_id,
                text=(
                    f"🛑 <b>Word Scramble stopped everywhere.</b>\n\n"
                    f"The word here was: <b>{esc(word)}</b>"
                ),
                parse_mode="HTML",
            )

    await update.message.reply_text(
        f"🛑 Word Scramble stopped in {stopped} group(s)/chat(s).\n\n"
        f"New rounds (manual, Automatic Mode, Slow Mode, and /scramblemode) are now paused "
        f"everywhere. Use /scramblemaintenance off to resume."
    )


@owner_or_admin_only
async def scramblemaintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() not in ("on", "off"):
        current = await asyncio.to_thread(db.get_config, "scramble_maintenance_mode", "0")
        await update.message.reply_text(
            f"Usage: /scramblemaintenance on|off\nCurrently: {'on' if current == '1' else 'off'}"
        )
        return

    mode = context.args[0].lower()
    await asyncio.to_thread(db.set_config, "scramble_maintenance_mode", "1" if mode == "on" else "0")
    await update.message.reply_text(
        f"🛠️ Word Scramble maintenance mode {'enabled' if mode == 'on' else 'disabled'}. "
        f"(Word Game is unaffected.)"
    )


@owner_only
async def scrambleallcommands_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
📖 <b>All Word Scramble Commands</b>

<b>👥 Player Commands</b>
/wordscramble - start a round (works in groups and DM)
/scramblehelp - how to play
/scrambleleaderboard - this chat's leaderboard
/scrambletopplayers - top 15, this chat or global
/scrambletopgroups - the most active groups

<b>🛡️ Group Admin Commands</b>
/scramblestopgame - group admins (or the owner) can stop an active round instantly
/scrambleautomaticmode on|off - group admins (or the owner) can manually control Automatic Mode here (blocked while Slow Mode is on in that chat). The bot never turns this on by itself anymore.
/slowmode on &lt;time&gt;|off - group admins (or the owner) can control Slow Mode here; next round is sent exactly &lt;time&gt; after the previous one ends (e.g. /slowmode on 2m). Every round still lasts a maximum of 2 minutes, and turning this on also turns Automatic Mode off in this chat.

<b>👑 Owner Commands</b>
/scramblestats - Word Scramble bot-wide statistics
/scramblesetreward &lt;amount&gt; - set the win reward
/scramblereloadwords - reload the word pool from medium_words_raw.py
/scrambleforceend [Chat ID] - force-end a stuck round in one chat
/scramblestopallgroups - force-end the active round in EVERY chat at once and pause new ones (until /scramblemaintenance off)
/scramblemaintenance on|off - pause new Word Scramble rounds only (Word Game unaffected)
/scramblemode &lt;time&gt;|off - my DM only: auto-start a round in EVERY group on a repeating interval
/scrambletopup &lt;@user or ID&gt; &lt;+/-amount&gt; - adjust a player's Word Scramble balance only
/scrambleusers - every player who's used the command or guessed a word, tappable for details
/scrambleallcommands - this list

<i>Note: Word Scramble intentionally has no /scramblebanuser, /scramblebroadcast, /scrambletopup, /scrambleshare, or /scrambleadmins - banning, broadcasting, balance, and bot-admin management are bot-wide systems shared with the Word Game, so /banuser, /broadcast, /topup, /share, and /addadmin already cover Word Scramble too. There's also no daily-reward feature specific to Word Scramble - the 🎁 Daily Reward always credits the Word Game balance, and every player's displayed Balance/Level/Rank on /start combines both games automatically.</i>
"""
    await update.message.reply_html(text)


def _slowmode_next_job_name(chat_id):
    return f"slowmode_next_{chat_id}"


def cancel_slowmode_next_job(job_queue, chat_id):
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(_slowmode_next_job_name(chat_id)):
        job.schedule_removal()


async def schedule_slowmode_next_round(job_queue, chat_id, interval_seconds):
    if job_queue is None:
        return
    cancel_slowmode_next_job(job_queue, chat_id)
    job_queue.run_once(
        slowmode_next_round_callback,
        interval_seconds,
        chat_id=chat_id,
        name=_slowmode_next_job_name(chat_id),
    )
    due_at = (datetime.now() + timedelta(seconds=interval_seconds)).isoformat()
    await asyncio.to_thread(db.scramble_set_next_round_due, chat_id, due_at)


async def slowmode_next_round_callback(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id

    slow = await asyncio.to_thread(db.get_scramble_slowmode, chat_id)
    if not slow or not slow[1]:
        return

    interval_seconds = slow[0]

    maintenance = await asyncio.to_thread(db.get_config, "scramble_maintenance_mode", "0")
    if maintenance == "1":
        await schedule_slowmode_next_round(context.job_queue, chat_id, interval_seconds)
        return

    if await asyncio.to_thread(db.scramble_get_round, chat_id):
        await asyncio.to_thread(db.scramble_clear_next_round_due, chat_id)
        return

    try:
        chat = await context.bot.get_chat(chat_id)
    except Exception:
        await schedule_slowmode_next_round(context.job_queue, chat_id, interval_seconds)
        return

    await asyncio.to_thread(db.scramble_clear_next_round_due, chat_id)
    await start_scramble_round(context, chat)


async def slowmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text("🐢 Run /slowmode inside the group you want to apply it to.")
        return

    if not (is_owner(update.effective_user.id) or await is_chat_admin(update, context)):
        await update.message.reply_text("❌ Only group admins (or the bot owner) can use /slowmode.")
        return

    chat_id = update.effective_chat.id

    if not context.args:
        current = await asyncio.to_thread(db.get_scramble_slowmode, chat_id)
        status = f"on, every {_format_duration_words(current[0])}" if current and current[1] else "off"
        await update.message.reply_text(
            f"Usage: /slowmode on <time>  (e.g. /slowmode on 2m)\n/slowmode off\n\nCurrently: {status}"
        )
        return

    mode = context.args[0].lower()

    if mode == "off":
        await asyncio.to_thread(db.set_scramble_slowmode, chat_id, 0, False)
        cancel_slowmode_next_job(context.job_queue, chat_id)
        await update.message.reply_text("🐢 Slowmode turned off.")
        return

    if mode != "on" or len(context.args) < 2:
        await update.message.reply_text("Usage: /slowmode on <time>  (e.g. /slowmode on 2m)\n/slowmode off")
        return

    seconds = _parse_duration(context.args[1])
    if seconds is None:
        await update.message.reply_text("❌ Invalid duration. Use e.g. 2m, 5m, 1h.")
        return

    await asyncio.to_thread(db.set_scramble_slowmode, chat_id, seconds, True)
    await asyncio.to_thread(db.scramble_set_auto_enabled, chat_id, False)

    if not await asyncio.to_thread(db.scramble_get_round, chat_id):
        await schedule_slowmode_next_round(context.job_queue, chat_id, seconds)

    await update.message.reply_text(
        f"🐢 Slowmode on - manual starts are paused; a new Word Scramble round starts here "
        f"automatically every {_format_duration_words(seconds)}."
    )


SCRAMBLE_MODE_JOB_NAME = "scramble_mode_global"


def cancel_scramblemode_job(job_queue):
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(SCRAMBLE_MODE_JOB_NAME):
        job.schedule_removal()


def schedule_scramblemode_job(job_queue, interval_seconds):
    if job_queue is None:
        return
    cancel_scramblemode_job(job_queue)
    job_queue.run_repeating(
        scramblemode_tick_callback,
        interval=interval_seconds,
        first=interval_seconds,
        name=SCRAMBLE_MODE_JOB_NAME,
    )


async def scramblemode_tick_callback(context: ContextTypes.DEFAULT_TYPE):
    groups = await asyncio.to_thread(db.get_current_groups)
    for chat_id, title in groups:
        fake_chat = SimpleNamespace(id=chat_id, type=ChatType.GROUP, title=title)
        try:
            result = await start_scramble_round(context, fake_chat)
            if result is True:
                await asyncio.to_thread(db.scramble_reset_manual_streak, chat_id)
        except Exception as e:
            logger.error(f"scramblemode tick failed for chat {chat_id}: {e}")


@owner_only
async def scramblemode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("🌐 Run /scramblemode in my DM.")
        return

    if not context.args:
        enabled = await asyncio.to_thread(db.get_config, "scramble_mode_enabled", "0") == "1"
        interval = await asyncio.to_thread(db.get_config, "scramble_mode_interval_seconds", "")
        status = f"on, every {interval}s" if enabled else "off"
        await update.message.reply_text(
            "Usage: /scramblemode <time>  (e.g. /scramblemode 1h, 30m, 1d)\n"
            f"/scramblemode off\n\nCurrently: {status}"
        )
        return

    arg = context.args[0].lower()

    if arg == "off":
        await asyncio.to_thread(db.set_config, "scramble_mode_enabled", "0")
        cancel_scramblemode_job(context.job_queue)
        await update.message.reply_text("🌐 Scramble Mode turned off.")
        return

    seconds = _parse_duration(arg)
    if seconds is None:
        await update.message.reply_text(
            "Usage: /scramblemode <time>  (e.g. /scramblemode 1h, 30m, 1d)\n/scramblemode off"
        )
        return

    await asyncio.to_thread(db.set_config, "scramble_mode_enabled", "1")
    await asyncio.to_thread(db.set_config, "scramble_mode_interval_seconds", str(seconds))
    schedule_scramblemode_job(context.job_queue, seconds)
    await update.message.reply_text(
        f"🌐 Scramble Mode on - a new Word Scramble round auto-starts in every group every {arg}. "
        f"Manual /wordscramble still works normally in between."
    )


@owner_only
async def scrambletopup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /scrambletopup <@username or ID> <+amount|-amount>\n"
            "e.g. /scrambletopup @kinggabby7 +500"
        )
        return

    target_arg, amount_arg = context.args[0].lstrip("@"), context.args[1]

    if not re.fullmatch(r"[+-]?\d+", amount_arg):
        await update.message.reply_text("❌ Amount must look like +500 or -500.")
        return

    amount = int(amount_arg)

    if target_arg.isdigit():
        row = await asyncio.to_thread(db.get_user_by_id, int(target_arg))
    else:
        row = await asyncio.to_thread(db.get_user_by_username, target_arg)

    if not row:
        await update.message.reply_text(f"❌ No player found matching {context.args[0]}.")
        return

    target_id, target_name, target_username, _ = row

    new_balance = await asyncio.to_thread(db.scramble_adjust_global_earned, target_id, target_name, amount)

    action = "added" if amount >= 0 else "deducted"
    display = f"@{target_username}" if target_username else esc(target_name)

    await update.message.reply_html(
        f"✅ Successfully {action} {abs(amount)} to {display}'s Word Scramble balance.\n"
        f"New Word Scramble Balance: {DOLLAR}{new_balance}"
    )


@owner_only
async def scrambletopupspecific_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_html(
            "Usage:\n"
            "<code>/scrambletopupspecific &lt;@username or ID&gt; &lt;+amount|-amount&gt; "
            "&lt;group ID or @groupusername&gt;</code>\n\n"
            "e.g.\n"
            "<code>/scrambletopupspecific @kinggabby7 +500 -1001234567890</code>\n"
            "<code>/scrambletopupspecific 123456789 -50 @myuffgroup</code>\n\n"
            "The amount is credited as if the player earned it in that group, so it shows up "
            "under <b>In this chat</b> there — and in <b>Global</b> too."
        )
        return

    target_arg = context.args[0].lstrip("@")
    amount_arg = context.args[1]
    group_arg = context.args[2]

    if not re.fullmatch(r"[+-]?\d+", amount_arg):
        await update.message.reply_text("❌ Amount must look like +500 or -500.")
        return

    amount = int(amount_arg)

    if target_arg.isdigit():
        row = await asyncio.to_thread(db.get_user_by_id, int(target_arg))
    else:
        row = await asyncio.to_thread(db.get_user_by_username, target_arg)

    if not row:
        await update.message.reply_text(f"❌ No player found matching {context.args[0]}.")
        return

    target_id, target_name, target_username, _ = row

    group_chat_id = None
    group_title = None

    if re.fullmatch(r"-?\d+", group_arg):
        g = await asyncio.to_thread(db.get_group_details, int(group_arg))
        if g:
            group_chat_id, group_title = g[0], g[1]
    else:
        g = await asyncio.to_thread(db.get_group_by_username, group_arg)
        if g:
            group_chat_id, group_title = g[0], g[1]

    if group_chat_id is None:
        await update.message.reply_html(
            f"❌ No group found matching <code>{esc(group_arg)}</code>.\n\n"
            "Use the numeric group ID (tap a group in /botgroups to see it) "
            "or the group's @username."
        )
        return

    new_chat_balance, new_global_balance = await asyncio.to_thread(
        db.scramble_adjust_chat_earned, target_id, group_chat_id, target_name, amount
    )

    action = "Added" if amount >= 0 else "Deducted"
    display = f"@{target_username}" if target_username else esc(target_name)

    await update.message.reply_html(
        f"✅ {action} {abs(amount)} to {display}'s Word Scramble balance "
        f"in <b>{esc(group_title or 'Untitled Group')}</b>.\n\n"
        f"🆔 Group ID: <code>{group_chat_id}</code>\n"
        f"📍 In-that-chat balance: {DOLLAR}{new_chat_balance}\n"
        f"🌍 Global balance: {DOLLAR}{new_global_balance}"
    )


async def scrambleautomaticmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text("🤖 Run /scrambleautomaticmode inside the group you want to control.")
        return

    if not (is_owner(update.effective_user.id) or await is_chat_admin(update, context)):
        await update.message.reply_text(
            "❌ Only group admins (or the bot owner) can use /scrambleautomaticmode."
        )
        return

    chat_id = update.effective_chat.id

    if not context.args or context.args[0].lower() not in ("on", "off"):
        _, enabled, _ = await asyncio.to_thread(db.scramble_get_auto_mode, chat_id)
        await update.message.reply_text(
            f"Usage: /scrambleautomaticmode on|off\nCurrently: {'on' if enabled else 'off'}"
        )
        return

    mode = context.args[0].lower()

    if mode == "on" and _slowmode_enabled(chat_id):
        await update.message.reply_text(
            "❌ Slow Mode is currently on in this chat - turn it off with /slowmode off "
            "before enabling Automatic Mode. The two must never run at the same time."
        )
        return

    await asyncio.to_thread(db.scramble_set_auto_enabled, chat_id, mode == "on")
    await update.message.reply_text(f"🤖 Automatic Mode turned {mode}.")


SCRAMBLEUSERS_PAGE_SIZE = 20


def build_scrambleusers_list_page(page: int):
    total = db.get_scramble_participants_count()
    offset = page * SCRAMBLEUSERS_PAGE_SIZE
    rows = db.get_scramble_participants_page(offset, SCRAMBLEUSERS_PAGE_SIZE)

    if not rows:
        return None, None

    total_pages = max(1, (total + SCRAMBLEUSERS_PAGE_SIZE - 1) // SCRAMBLEUSERS_PAGE_SIZE)
    start_n = offset + 1
    end_n = offset + len(rows)

    header = (
        f"🔤 <b>Word Scramble Users</b> ({total} total) — Page {page + 1}/{total_pages}\n"
        f"<i>Entries {start_n}–{end_n} • Tap a player for full details.</i>"
    )

    keyboard = [
        [InlineKeyboardButton(
            f"{start_n + i}. {truncate_name(name)} — 🎮{played} 🏆{wins}",
            callback_data=f"su:{uid}:{chat_id}:{page}",
        )]
        for i, (uid, chat_id, name, played, wins, earned) in enumerate(rows)
    ]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"sulpg:{page - 1}"))
    if offset + SCRAMBLEUSERS_PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"sulpg:{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    return header, InlineKeyboardMarkup(keyboard)


@owner_only
async def scrambleusers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    header, keyboard = build_scrambleusers_list_page(0)
    if header is None:
        await update.message.reply_text("No Word Scramble activity recorded yet.")
        return
    await update.message.reply_html(header, reply_markup=keyboard)

_SYSTEM_DICTIONARY_CACHE = None
_SPELLCHECKER_CACHE = None


def _load_system_dictionary():
    global _SYSTEM_DICTIONARY_CACHE

    if _SYSTEM_DICTIONARY_CACHE is not None:
        return _SYSTEM_DICTIONARY_CACHE

    words = set()
    for path in ("/usr/share/dict/words", "/usr/dict/words"):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        words.add(line.lower())
        except OSError:
            continue
        if words:
            break

    _SYSTEM_DICTIONARY_CACHE = words
    return words


def _get_spellchecker():
    """pyspellchecker ships its own word-frequency dictionary as data
    bundled inside the pip package itself - unlike enchant (needs the
    system libenchant library, not present on Render) or
    /usr/share/dict/words (not present on Render's slim container
    either), so this is the one dictionary check that's actually
    guaranteed to work in production, not just on a local dev machine."""
    global _SPELLCHECKER_CACHE
    if _SPELLCHECKER_CACHE is None:
        from spellchecker import SpellChecker
        _SPELLCHECKER_CACHE = SpellChecker()
    return _SPELLCHECKER_CACHE


def _word_in_dictionary(word_lower: str) -> bool:
    try:
        spell = _get_spellchecker()
        if spell.known([word_lower]) or spell.known([word_lower.capitalize()]):
            return True
    except Exception:
        pass

    try:
        enchant = importlib.import_module("enchant")
        try:
            d = enchant.Dict("en_US")
            if d.check(word_lower) or d.check(word_lower.capitalize()):
                return True
        except Exception:
            pass
    except ImportError:
        pass

    system_dict = _load_system_dictionary()
    if system_dict:
        return word_lower in system_dict

    return word_lower in {w.lower() for w in WORDS}


def is_valid_english_word(text: str) -> bool:
    if text is None:
        return False

    word = text.strip()

    if not word:
        return False

    if not re.fullmatch(r"[A-Za-z]+", word):
        return False

    if len(word) > 32:
        return False

    return _word_in_dictionary(word.lower())


def random_word(chat_id, exclude=None):
    used = db.get_used_words(chat_id)

    choices = [w for w in WORDS if w not in used and w != exclude]

    if not choices:
        db.reset_used_words(chat_id)
        choices = [w for w in WORDS if w != exclude] or WORDS

    word = random.choice(choices)
    db.mark_word_used(chat_id, word)
    return word


def volunteer_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🙋 I'll Explain", callback_data="volunteer")]
    ])


def leader_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👁️ See Word", callback_data="see_word"),
            InlineKeyboardButton("✍️ Write Word", callback_data="write_word"),
        ],
        [
            InlineKeyboardButton("🔄 Change Word", callback_data="change_word"),
            InlineKeyboardButton("❌ Drop Lead", callback_data="drop_lead"),
        ],
    ])


def _timeout_job_name(chat_id):
    return f"explain_timeout_{chat_id}"


def cancel_explain_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id):
    if context.job_queue is None:
        return
    for job in context.job_queue.get_jobs_by_name(_timeout_job_name(chat_id)):
        job.schedule_removal()


def schedule_explain_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id, leader_id, leader_name):
    if context.job_queue is None:
        return
    cancel_explain_timeout(context, chat_id)
    context.job_queue.run_once(
        explain_timeout_callback,
        EXPLAIN_TIMEOUT_SECONDS,
        chat_id=chat_id,
        name=_timeout_job_name(chat_id),
        data={"leader_id": leader_id, "leader_name": leader_name},
    )


async def explain_timeout_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.chat_id
    leader_id = job.data["leader_id"]
    leader_name = job.data["leader_name"]

    game = await asyncio.to_thread(db.get_game, chat_id)
    if not game or game[1] != leader_id:
        return

    if await asyncio.to_thread(db.is_explaining_started, chat_id):
        return

    await asyncio.to_thread(db.clear_leader, chat_id)
    await asyncio.to_thread(db.set_explaining_started, chat_id, False)

    await safe_send(
        context.bot,
        chat_id,
        text=(
            f"⏰ <b>{esc(leader_name)}</b>\n\n"
            f"Ran out of time to explain.\n\n"
            f"Who wants to explain?"
        ),
        parse_mode="HTML",
        reply_markup=volunteer_keyboard(),
    )


# --- Feature 1: "Add to a Group" button -------------------------------------

def _bot_username(context) -> str:
    """Live bot username from Telegram, falling back to the BOT_USERNAME env var."""
    try:
        uname = context.bot.username
    except Exception:
        uname = None
    return uname or BOT_USERNAME


def add_to_group_keyboard(context):
    """Inline keyboard with a single [Add to a Group] button. Tapping it opens
    Telegram's group picker. Returns None if the bot username is unknown, in
    which case the message simply sends without a button (never crashes)."""
    uname = _bot_username(context)
    if not uname:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "➕ Add to a Group",
            url=f"https://t.me/{uname}?startgroup=true",
        )
    ]])


# --- Feature 3: group-join prompt message -----------------------------------

DEFAULT_GROUP_JOIN_PROMPT = (
    "Play word game 🎮, Word scramble 🔤, Earn rewards 💵, "
    "Claim daily rewards 🎁 — all in one bot 🤖\n\n"
    "Add me as an admin to enjoy all features."
)


def get_group_join_prompt() -> str:
    text = db.get_config("group_join_prompt_text", None)
    return text if text else DEFAULT_GROUP_JOIN_PROMPT


def group_join_prompt_enabled() -> bool:
    return db.get_config("group_join_prompt_enabled", "1") == "1"


# --- Feature 2: group add/remove owner alerts -------------------------------

def _format_event_time(dt=None) -> str:
    return (dt or datetime.now()).strftime("%d %b %Y, %I:%M %p")


def _format_stored_time(value) -> str:
    if not value:
        return "Unknown"
    return str(value)[:19].replace("T", " ")


def _format_actor(actor) -> str:
    """'@username (12345)' / 'Name (12345)' / 'Unknown' for a Telegram User."""
    if not actor:
        return "Unknown"
    label = f"@{actor.username}" if actor.username else esc(actor.first_name or "Unknown")
    return f"{label} (<code>{actor.id}</code>)"


def _format_stored_actor(uid, name) -> str:
    """Same, but from the values stored in the groups table."""
    if not uid and not name:
        return "Unknown"
    label = esc(name) if name else "Unknown"
    return f"{label} (<code>{uid}</code>)" if uid else label


async def _resolve_group_link(context, chat):
    """Best-effort public/invite link for a group. Never raises."""
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}"
    try:
        link_obj = await context.bot.create_chat_invite_link(chat.id)
        return link_obj.invite_link
    except Exception:
        pass
    try:
        full = await context.bot.get_chat(chat.id)
        return getattr(full, "invite_link", None)
    except Exception:
        return None


async def _notify_owner_group_event(context, kind, chat, actor, group_link):
    """kind is 'added' or 'removed'. Never raises - failures are logged only."""
    if kind == "added":
        header = "🟢 <b>Added to a group</b>"
        actor_label = "Added by"
    else:
        header = "🔴 <b>Removed from a group</b>"
        actor_label = "Removed by"

    chat_username = getattr(chat, "username", None)
    username_display = f"@{chat_username}" if chat_username else "None (private group)"
    link_display = (
        f'<a href="{group_link}">{esc(group_link)}</a>' if group_link else "Not available"
    )

    text = (
        f"{header}\n\n"
        f"Group name: {esc(chat.title or 'Untitled Group')}\n\n"
        f"Group ID: <code>{chat.id}</code>\n\n"
        f"Time and date: {_format_event_time()}\n\n"
        f"{actor_label}: {_format_actor(actor)}\n\n"
        f"Group username: {username_display}\n\n"
        f"Group link: {link_display}"
    )

    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Failed to notify owner of group {kind}: {e}")


def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎮 Play Game", callback_data="play"),
            InlineKeyboardButton("👤 Profile", callback_data="profile"),
        ],
        [
            InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard"),
            InlineKeyboardButton("🎁 Daily Reward", callback_data="daily_reward"),
        ],
    ])


def settings_keyboard(user_id):
    enabled = db.get_auto_lead(user_id)
    label = "☑ Auto Become Leader" if enabled else "☐ Auto Become Leader"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="toggle_auto_lead")]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    await asyncio.to_thread(db.add_user, user.id, user.first_name)

    if context.args:
        first_arg = context.args[0]

        if first_arg.startswith("adclick_"):
            try:
                subscription_id = int(first_arg.split("_", 1)[1])
            except ValueError:
                subscription_id = None

            if subscription_id is not None:
                ad_info = await asyncio.to_thread(db.get_ad_business_name, subscription_id)
                if ad_info:
                    business_name, ad_link = ad_info
                    await asyncio.to_thread(db.record_ad_click, subscription_id)
                    safe_href = html_lib.escape(str(ad_link), quote=True)
                    await update.message.reply_html(
                        f'👉 <a href="{safe_href}">Continue to {esc(business_name)}</a>'
                    )
                    return

        else:
            target_arg = first_arg.lstrip("@")

            if target_arg.isdigit():
                row = await asyncio.to_thread(db.get_user_by_id, int(target_arg))
            else:
                row = await asyncio.to_thread(db.get_user_by_username, target_arg)

            if not row:
                await update.message.reply_text(f"❌ No player found matching {first_arg}.")
                return

            target_player = await asyncio.to_thread(db.get_user, row[0])
            if not target_player:
                await update.message.reply_text(f"❌ No player found matching {first_arg}.")
                return

            await update.message.reply_html(f"""
👤 <b>Player Profile</b>

👤 Name: {esc(target_player[1])}

💵 Balance: <b>{target_player[2]}</b>
⭐ Level: <b>{target_player[3]}</b>
🏅 Rank: <b>{target_player[4]}</b>
""")
            return

    player = await asyncio.to_thread(db.get_user, user.id)

    welcome_text = f"""
🎮 <b>Welcome Back, Warrior!</b>

Hello, {esc(user.first_name)}! 👋

💵 Balance: <b>{player[2]}</b>
⭐ Level: <b>{player[3]}</b>
🏅 Rank: <b>{player[4]}</b>

Tap a button below to continue your adventure!
"""

    await update.message.reply_html(
        welcome_text,
        reply_markup=main_menu()
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text(
            "⚙️ Please open my private chat to change settings."
        )
        return

    user = update.effective_user
    await asyncio.to_thread(db.add_user, user.id, user.first_name)

    await update.message.reply_html(
        "⚙️ <b>Settings</b>\n\n"
        "When you win a round, should you automatically become the next leader?",
        reply_markup=settings_keyboard(user.id),
    )


async def claimdailyreward_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        amount = await asyncio.to_thread(db.get_config, "reward_daily", db.DAILY_REWARD)
        await update.message.reply_text(
            f"To claim your daily reward ({amount}), please message me privately "
            f"and use the /claimdailyreward command."
        )
        return

    user = update.effective_user
    await asyncio.to_thread(db.add_user, user.id, user.first_name)

    result = await asyncio.to_thread(db.claim_daily_reward, user.id, user.first_name, user.username)

    if result[0]:
        amount = int(await asyncio.to_thread(db.get_config, "reward_daily", db.DAILY_REWARD))
        apply_chat_credit_for_global_earning(user.id, user.first_name, amount)

    await update.message.reply_html(format_daily_reward_text(result))


async def is_chat_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except (Forbidden, BadRequest):
        return False


async def word_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text(
            "🎮 Add me to a group and run /wordgame there!",
            reply_markup=add_to_group_keyboard(context),
        )
        return

    chat_id = update.effective_chat.id

    existing = await asyncio.to_thread(db.get_game, chat_id)
    if existing and existing[1] is not None:
        if not await is_chat_admin(update, context):
            await update.message.reply_text(
                "❌ A round is in progress - only group admins can start a new game right now."
            )
            return
        cancel_explain_timeout(context, chat_id)

    await asyncio.to_thread(db.start_game, chat_id)

    await update.message.reply_html(
        """
🎮 <b>UFF Word Game</b>

Who wants to explain?
""",
        reply_markup=volunteer_keyboard()
    )


def format_daily_reward_text(result) -> str:
    if result[0]:
        balance = result[1]
        amount = db.get_config("reward_daily", db.DAILY_REWARD)
        return f"""
🎁 <b>Daily Reward</b>

Congratulations!

💵 +{amount} Balance

Current Balance:
💵 {balance}

Come back tomorrow!
"""

    remaining = result[1]
    hours = remaining.seconds // 3600
    minutes = (remaining.seconds % 3600) // 60

    return f"""
🎁 <b>Daily Reward</b>

❌ You have already claimed today's reward.

⏰ Next reward in:
{hours} hours {minutes} minutes

Come back in {hours} hrs {minutes} mins to claim again.
"""


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    user = query.from_user
    chat = query.message.chat

    if query.data == "noop":
        await query.answer()
        return

    if query.data == "scramble_start":
        if chat.type == ChatType.PRIVATE:
            await query.answer()
            await query.edit_message_text(SCRAMBLE_GROUPS_ONLY_TEXT, parse_mode="HTML")
            return
        if _slowmode_enabled(chat.id) and not await asyncio.to_thread(db.scramble_get_round, chat.id):
            interval = (await asyncio.to_thread(db.get_scramble_slowmode, chat.id))[0]
            await query.answer(
                f"🐢 Slow mode is on - a new round starts automatically every {_format_duration_words(interval)}.",
                show_alert=True,
            )
            return
        result = await start_scramble_round(context, chat)
        if result is False:
            await query.answer(
                "🎮 A Word Scramble round is already in progress here!", show_alert=True
            )
        elif result is None:
            await query.answer(
                "⚠️ The Word Scramble word list is currently empty.", show_alert=True
            )
        elif result == "maintenance":
            await query.answer(
                "🛠️ Word Scramble is currently under maintenance.", show_alert=True
            )
        else:
            await asyncio.to_thread(db.scramble_reset_manual_streak, chat.id)
            await query.answer("🎮 Word Scramble started - check the chat!")
        return

    if query.data == "stp_chat":
        await query.answer()
        if chat.type == ChatType.PRIVATE:
            await query.edit_message_text(
                "📍 'In This Chat' isn't available in a private chat with me - "
                "try /scrambletopplayers inside a group instead.",
                reply_markup=scramble_topplayers_keyboard(),
            )
            return
        rows = await asyncio.to_thread(db.scramble_get_chat_leaderboard, chat.id, 15)
        position_info = await asyncio.to_thread(db.scramble_get_chat_position, chat.id, user.id)
        await query.edit_message_text(
            format_scramble_top_players(rows, "Top 15 - This Chat", position_info, user.first_name, "Your Position"),
            parse_mode="HTML",
            reply_markup=scramble_topplayers_keyboard(),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    if query.data == "stp_global":
        await query.answer()
        rows = await asyncio.to_thread(db.scramble_get_global_leaderboard, 15)
        position_info = await asyncio.to_thread(db.scramble_get_global_position, user.id)
        await query.edit_message_text(
            format_scramble_top_players(rows, "Top 15 - Global", position_info, user.first_name, "Your Global Position"),
            parse_mode="HTML",
            reply_markup=scramble_topplayers_keyboard(),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    if query.data == "profile":
        await query.answer()
        player = await asyncio.to_thread(db.get_user, user.id)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅ Back", callback_data="home")]
        ])

        await query.edit_message_text(
            f"""
👤 <b>Your Profile</b>

🆔 User ID: <code>{player[0]}</code>

👤 Name: {esc(player[1])}

💵 Balance: {player[2]}
⭐ Level: {player[3]}
🏅 Rank: {player[4]}
""",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    if query.data == "home":
        await query.answer()
        player = await asyncio.to_thread(db.get_user, user.id)

        await query.edit_message_text(
            f"""
🎮 <b>Welcome Back, Warrior!</b>

Hello, {esc(user.first_name)}! 👋

💵 Balance: <b>{player[2]}</b>
⭐ Level: <b>{player[3]}</b>
🏅 Rank: <b>{player[4]}</b>

Tap a button below to continue your adventure!
""",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if query.data == "daily_reward":
        await query.answer()
        result = await asyncio.to_thread(db.claim_daily_reward, user.id, user.first_name, user.username)

        if result[0]:
            amount = int(await asyncio.to_thread(db.get_config, "reward_daily", db.DAILY_REWARD))
            apply_chat_credit_for_global_earning(user.id, user.first_name, amount, current_chat=chat)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅ Back", callback_data="home")]
        ])

        await query.edit_message_text(
            format_daily_reward_text(result),
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    if query.data == "play":
        if chat.type == ChatType.PRIVATE:
            await query.answer()
            await query.edit_message_text(SCRAMBLE_GROUPS_ONLY_TEXT, parse_mode="HTML")
            return
        if _slowmode_enabled(chat.id) and not await asyncio.to_thread(db.scramble_get_round, chat.id):
            interval = (await asyncio.to_thread(db.get_scramble_slowmode, chat.id))[0]
            await query.answer(
                f"🐢 Slow mode is on - a new round starts automatically every {_format_duration_words(interval)}.",
                show_alert=True,
            )
            return
        result = await start_scramble_round(context, chat)
        if result is False:
            await query.answer(
                "🎮 A Word Scramble round is already in progress here!", show_alert=True
            )
        elif result is None:
            await query.answer(
                "⚠️ The Word Scramble word list is currently empty.", show_alert=True
            )
        elif result == "maintenance":
            await query.answer(
                "🛠️ Word Scramble is currently under maintenance.", show_alert=True
            )
        else:
            await asyncio.to_thread(db.scramble_reset_manual_streak, chat.id)
            await query.answer("🎮 Word Scramble started - check the chat!")
        return

    if query.data == "leaderboard":
        await query.answer()
        rows = await asyncio.to_thread(db.get_combined_global_leaderboard)
        position_info = await asyncio.to_thread(db.get_combined_global_position, user.id)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅ Back", callback_data="home")]
        ])

        await query.edit_message_text(
            format_board(rows, "UFF Leaderboard", position_info, user.first_name),
            parse_mode="HTML",
            reply_markup=keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    if query.data == "toggle_auto_lead":
        await query.answer()
        await asyncio.to_thread(db.toggle_auto_lead, user.id)
        await query.edit_message_reply_markup(
            reply_markup=settings_keyboard(user.id)
        )
        return

    if query.data == "lb_chat":
        await query.answer()
        rows = await asyncio.to_thread(db.get_chat_leaderboard, chat.id)
        position_info = await asyncio.to_thread(db.get_chat_position, chat.id, user.id)
        await query.edit_message_text(
            format_board(rows, "Leaderboard - This Chat", position_info, user.first_name),
            parse_mode="HTML",
            reply_markup=leaderboard_keyboard(),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    if query.data == "lb_global":
        await query.answer()
        rows = await asyncio.to_thread(db.get_global_leaderboard)
        position_info = await asyncio.to_thread(db.get_global_position, user.id)
        await query.edit_message_text(
            format_board(rows, "Leaderboard - Global", position_info, user.first_name),
            parse_mode="HTML",
            reply_markup=leaderboard_keyboard(),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    if query.data == "tp_chat":
        await query.answer()
        if chat.type == ChatType.PRIVATE:
            await query.edit_message_text(
                "📍 'In This Chat' isn't available in a private chat with me - "
                "try /topplayers inside a group instead.",
                reply_markup=topplayers_keyboard(),
            )
            return

        if is_owner(user.id):
            keyboard = owner_top_players_keyboard("chat", chat.id)
            if keyboard is None:
                await query.edit_message_text(
                    "No scores yet in this chat.",
                    reply_markup=topplayers_keyboard(),
                )
                return
            await query.edit_message_text(
                "🏆 <b>Top 15 - This Chat</b>\n<i>Tap a player to view their profile.</i>",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return

        rows = await asyncio.to_thread(db.get_chat_leaderboard, chat.id, 15)
        position_info = await asyncio.to_thread(db.get_chat_position, chat.id, user.id)
        await query.edit_message_text(
            format_top_players(rows, "Top 15 - This Chat", position_info, user.first_name, "Your Position"),
            parse_mode="HTML",
            reply_markup=topplayers_keyboard(),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    if query.data == "tp_global":
        await query.answer()

        if is_owner(user.id):
            keyboard = owner_top_players_keyboard("global", 0)
            if keyboard is None:
                await query.edit_message_text(
                    "No scores yet.",
                    reply_markup=topplayers_keyboard(),
                )
                return
            await query.edit_message_text(
                "🏆 <b>Top 15 - Global</b>\n<i>Tap a player to view their profile.</i>",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return

        rows = await asyncio.to_thread(db.get_global_leaderboard, 15)
        position_info = await asyncio.to_thread(db.get_global_position, user.id)
        await query.edit_message_text(
            format_top_players(rows, "Top 15 - Global", position_info, user.first_name, "Your Global Position"),
            parse_mode="HTML",
            reply_markup=topplayers_keyboard(),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    if query.data.startswith("ulpg:"):
        if not is_owner(user.id):
            await query.answer("🚫 Owner only.", show_alert=True)
            return
        page = int(query.data.split(":", 1)[1])
        header, keyboard = build_users_list_page(page)
        await query.answer()
        if header is None:
            await query.edit_message_text("No users registered yet.")
            return
        await query.edit_message_text(header, parse_mode="HTML", reply_markup=keyboard)
        return

    if query.data.startswith("glpg:"):
        if not is_owner(user.id):
            await query.answer("🚫 Owner only.", show_alert=True)
            return
        page = int(query.data.split(":", 1)[1])
        header, keyboard = build_groups_list_page(page)
        await query.answer()
        if header is None:
            await query.edit_message_text("No groups registered yet.")
            return
        await query.edit_message_text(header, parse_mode="HTML", reply_markup=keyboard)
        return

    if query.data.startswith("bglpg:"):
        if not is_owner(user.id):
            await query.answer("🚫 Owner only.", show_alert=True)
            return
        page = int(query.data.split(":", 1)[1])
        header, keyboard = build_botgroups_list_page(page)
        await query.answer()
        if header is None:
            await query.edit_message_text("No groups registered yet.")
            return
        await query.edit_message_text(header, parse_mode="HTML", reply_markup=keyboard)
        return

    if query.data.startswith("bg:"):
        if not is_owner(user.id):
            await query.answer("🚫 Owner only.", show_alert=True)
            return
        _, chat_id_str, page_str = query.data.split(":")
        target_chat_id = int(chat_id_str)
        page = int(page_str)

        details = await asyncio.to_thread(db.get_group_full_details, target_chat_id)
        if not details:
            await query.answer("❌ Group data not found.", show_alert=True)
            return

        (g_chat_id, title, member_count, wg_played, first_seen, left_at, scr_played,
         g_username, g_stored_link, added_by_id, added_by_name,
         removed_by_id, removed_by_name) = details
        members_display = member_count if member_count else "Unknown"
        status_display = "🟢 Currently in group" if not left_at else f"🔴 Removed ({left_at[:16]})"

        invite_link = f"https://t.me/{g_username}" if g_username else g_stored_link
        if not invite_link and not left_at:
            try:
                link_obj = await context.bot.create_chat_invite_link(g_chat_id)
                invite_link = link_obj.invite_link
            except Exception:
                invite_link = None

        text = f"""
👨‍👩‍👧‍👦 <b>Group Details</b>

📛 Name: {esc(title or 'Untitled Group')}
🆔 Group ID: <code>{g_chat_id}</code>
👥 Members: {members_display}
📅 Bot Joined: {first_seen or 'Unknown'}
{status_display}

🎮 Word Game — Games Played: <b>{wg_played}</b>
🔤 Word Scramble — Rounds Played: <b>{scr_played}</b>
"""
        text += "\n━━━━━━━━━━━━━━━\n"
        text += f"📛 Group name: {esc(title or 'Untitled Group')}\n"
        text += f"🆔 Group ID: <code>{g_chat_id}</code>\n"
        text += f"🕒 Time and date: {_format_stored_time(first_seen)}\n"
        text += f"➕ Added by: {_format_stored_actor(added_by_id, added_by_name)}\n"
        if left_at:
            text += f"➖ Removed by: {_format_stored_actor(removed_by_id, removed_by_name)}\n"
        text += f"🔗 Group username: {'@' + g_username if g_username else 'None (private group)'}\n"
        text += f"🌐 Group link: {esc(invite_link) if invite_link else 'Not available'}\n"

        if not invite_link:
            text += "\n<i>No invite link available - tap the Group ID above to copy it.</i>\n"

        keyboard_rows = []
        if invite_link:
            keyboard_rows.append([InlineKeyboardButton("🔗 Join Group", url=invite_link)])
        keyboard_rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"bglpg:{page}")])

        await query.answer()
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard_rows)
        )
        return

    if query.data == "tp_back":
        await query.answer()
        await query.edit_message_text(
            "🏆 <b>Top Players</b>\n\nChoose a scope:",
            parse_mode="HTML",
            reply_markup=topplayers_keyboard(),
        )
        return

    if query.data.startswith("op_back:"):
        if not is_owner(user.id):
            await query.answer()
            return
        _, scope, ctx_str = query.data.split(":")
        await query.answer()

        if scope == "userslist":
            header, keyboard = build_users_list_page(int(ctx_str))
            if header is None:
                await query.edit_message_text("No users registered yet.")
                return
            await query.edit_message_text(header, parse_mode="HTML", reply_markup=keyboard)
            return

        keyboard = owner_top_players_keyboard(scope, int(ctx_str))
        title = "Top 15 - This Chat" if scope == "chat" else "Top 15 - Global"
        await query.edit_message_text(
            f"🏆 <b>{title}</b>\n<i>Tap a player to view their profile.</i>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    if query.data.startswith("op:"):
        if not is_owner(user.id):
            await query.answer("🚫 Owner only.", show_alert=True)
            return
        _, uid_str, scope, ctx_str = query.data.split(":")
        target_id = int(uid_str)
        profile = await asyncio.to_thread(db.get_full_player_profile, target_id)

        if not profile:
            await query.answer("❌ Player data not found.", show_alert=True)
            return

        pid, name, username, balance, games_played, games_won = profile
        uname_display = f"@{username}" if username else "—"
        id_mention = f'<a href="tg://user?id={pid}">{pid}</a>'

        text = f"""
👤 <b>Player Profile</b>

👤 Display Name: {esc(name)}
🔖 Username: {uname_display}
🆔 Telegram ID: <code>{pid}</code> (tap name to open chat: {id_mention})
{DOLLAR} Current Balance: {balance}
🎮 Total Games Played: {games_played}
🏆 Games Won: {games_won}
"""
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅ Back", callback_data=f"op_back:{scope}:{ctx_str}")]
        ])
        await query.answer()
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        return

    if query.data == "og_back" or query.data.startswith("og_back:"):
        if not is_owner(user.id):
            await query.answer()
            return
        parts = query.data.split(":")
        backscope = parts[1] if len(parts) > 1 else "top"
        backpage = int(parts[2]) if len(parts) > 2 else 0
        await query.answer()

        if backscope == "gl":
            header, keyboard = build_groups_list_page(backpage)
            if header is None:
                await query.edit_message_text("No groups registered yet.")
                return
            await query.edit_message_text(header, parse_mode="HTML", reply_markup=keyboard)
            return

        rows, keyboard = owner_top_groups_keyboard()
        if not rows:
            await query.edit_message_text("No group activity recorded yet.")
            return
        await query.edit_message_text(
            "🏆 <b>Top 10 Active Groups</b>\n<i>Tap a group to view its details.</i>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    if query.data.startswith("og:"):
        if not is_owner(user.id):
            await query.answer("🚫 Owner only.", show_alert=True)
            return
        parts = query.data.split(":")
        target_chat_id = int(parts[1])
        backscope = parts[2] if len(parts) > 2 else "top"
        backpage = int(parts[3]) if len(parts) > 3 else 0

        details = await asyncio.to_thread(db.get_group_full_details, target_chat_id)

        if not details:
            await query.answer("❌ Group data not found.", show_alert=True)
            return

        (g_chat_id, title, member_count, games_played, first_seen, left_at, _scr_played,
         g_username, g_stored_link, added_by_id, added_by_name,
         removed_by_id, removed_by_name) = details
        members_display = member_count if member_count else "Unknown"
        joined_display = first_seen or "Unknown"

        invite_link = f"https://t.me/{g_username}" if g_username else g_stored_link
        if not invite_link and not left_at:
            try:
                link_obj = await context.bot.create_chat_invite_link(g_chat_id)
                invite_link = link_obj.invite_link
            except Exception:
                invite_link = None

        text = f"""
👨‍👩‍👧‍👦 <b>Group Details</b>

📛 Name: {esc(title or 'Untitled Group')}
🆔 Group ID: <code>{g_chat_id}</code>
👥 Members: {members_display}
🎮 Total Games Played: {games_played}
📅 Bot Joined: {joined_display}
"""
        text += "\n━━━━━━━━━━━━━━━\n"
        text += f"📛 Group name: {esc(title or 'Untitled Group')}\n"
        text += f"🆔 Group ID: <code>{g_chat_id}</code>\n"
        text += f"🕒 Time and date: {_format_stored_time(first_seen)}\n"
        text += f"➕ Added by: {_format_stored_actor(added_by_id, added_by_name)}\n"
        if left_at:
            text += f"➖ Removed by: {_format_stored_actor(removed_by_id, removed_by_name)}\n"
        text += f"🔗 Group username: {'@' + g_username if g_username else 'None (private group)'}\n"
        text += f"🌐 Group link: {esc(invite_link) if invite_link else 'Not available'}\n"

        if not invite_link:
            text += "\n<i>No invite link available - tap the Group ID above to copy it.</i>\n"

        keyboard_rows = []
        if invite_link:
            keyboard_rows.append([InlineKeyboardButton("🔗 Join Group", url=invite_link)])
        keyboard_rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"og_back:{backscope}:{backpage}")])

        await query.answer()
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard_rows)
        )
        return

    if query.data.startswith("sulpg:"):
        if not is_owner(user.id):
            await query.answer("🚫 Owner only.", show_alert=True)
            return
        page = int(query.data.split(":", 1)[1])
        header, keyboard = build_scrambleusers_list_page(page)
        await query.answer()
        if header is None:
            await query.edit_message_text("No Word Scramble activity recorded yet.")
            return
        await query.edit_message_text(header, parse_mode="HTML", reply_markup=keyboard)
        return

    if query.data.startswith("su:"):
        if not is_owner(user.id):
            await query.answer("🚫 Owner only.", show_alert=True)
            return
        _, uid_str, chat_id_str, page_str = query.data.split(":")
        target_id = int(uid_str)
        target_chat_id = int(chat_id_str)
        page = int(page_str)

        detail = await asyncio.to_thread(db.get_scramble_participant_detail, target_id, target_chat_id)
        if not detail:
            await query.answer("❌ Not found.", show_alert=True)
            return

        name, played, wins, earned_in_chat = detail
        global_balance = await asyncio.to_thread(db.scramble_get_global_earned, target_id)
        group_details = await asyncio.to_thread(db.get_group_details, target_chat_id)
        group_label = esc(group_details[1] or "Untitled Group") if group_details else "DM"

        text = f"""
👤 <b>{esc(name)}</b>

🎮 Word Scramble Games Played: <b>{played}</b>
🏆 Games Won: <b>{wins}</b>
{DOLLAR} Word Scramble Balance: <b>{global_balance}</b>
📍 Group: {group_label}
"""
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data=f"sulpg:{page}")]])
        await query.answer()
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        return

    if query.data == "volunteer":
        game = await asyncio.to_thread(db.get_game, chat.id)

        if game and game[1] is not None:
            await query.answer(
                f"{game[2]} is already explaining!", show_alert=True
            )
            return

        word = await asyncio.to_thread(random_word, chat.id)
        await asyncio.to_thread(db.set_leader, chat.id, user.id, user.first_name, word)
        await asyncio.to_thread(db.set_explaining_started, chat.id, False)
        await asyncio.to_thread(db.start_explain_log, chat.id, user.id)
        schedule_explain_timeout(context, chat.id, user.id, user.first_name)

        await query.answer()
        await query.edit_message_text(
            f"""
<b>{esc(user.first_name)} is explaining the word!</b>

<i>ᵀᵒ ᵈⁱˢᵃᵇˡᵉ ᵃᵘᵗᵒ⁻ˡᵉᵃᵈ, ᵒᵖᵉⁿ ˢᵉᵗᵗⁱⁿᵍˢ ⁱⁿ ᵐʸ ᴾᴹ</i>
""",
            parse_mode="HTML",
            reply_markup=leader_keyboard(),
        )
        return

    if query.data in ("see_word", "write_word", "change_word", "drop_lead"):
        game = await asyncio.to_thread(db.get_game, chat.id)

        if not game or game[1] is None:
            await query.answer("❌ There's no active round.", show_alert=True)
            return

        leader_id = game[1]

        if user.id != leader_id:
            await query.answer(
                "❌ Only the current leader can do that.", show_alert=True
            )
            return

        if query.data == "see_word":
            word = game[3]
            await asyncio.to_thread(db.set_explaining_started, chat.id, True)
            cancel_explain_timeout(context, chat.id)
            await query.answer(
                f"🎯 Your word is:\n\n{word}",
                show_alert=True,
            )
            return

        if query.data == "write_word":
            await asyncio.to_thread(db.set_explaining_started, chat.id, True)
            cancel_explain_timeout(context, chat.id)
            await asyncio.to_thread(db.set_pending_input, user.id, chat.id, "write_word")
            try:
                await context.bot.send_message(
                    chat_id=user.id,
                    text="✍️ Send me the word you want players to guess.",
                )
                await query.answer(
                    "📩 Check your private messages with me.",
                    show_alert=True,
                )
            except Forbidden:
                await query.answer(
                    "⚠️ Please start a private chat with me first, then try again.",
                    show_alert=True,
                )
            return

        if query.data == "change_word":
            new_word = await asyncio.to_thread(random_word, chat.id, game[3])
            await asyncio.to_thread(db.set_word, chat.id, new_word)

            await query.answer(
                "🔄 Word changed successfully!",
                show_alert=True,
            )
            await safe_send(
                context.bot,
                chat.id,
                text=f"🔄 {user.first_name} changed the word.",
            )
            return

        if query.data == "drop_lead":
            leader_name = game[2]
            await asyncio.to_thread(db.clear_leader, chat.id)
            await asyncio.to_thread(db.set_explaining_started, chat.id, False)
            cancel_explain_timeout(context, chat.id)

            await query.answer()
            await query.edit_message_text(
                f"""
❌ <b>{esc(leader_name)} dropped the lead.</b>

Who wants to explain?
""",
                parse_mode="HTML",
                reply_markup=volunteer_keyboard(),
            )
            return


async def private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    pending = await asyncio.to_thread(db.get_pending_input, user.id)

    if not pending:
        return

    chat_id, action = pending

    if action == "write_word":
        word = update.message.text.strip()

        if not is_valid_english_word(word):
            await update.message.reply_text(
                "❌ Invalid word. Please enter a single valid English word using letters only."
            )
            return

        game = await asyncio.to_thread(db.get_game, chat_id)
        if not game or game[1] != user.id:
            await update.message.reply_text(
                "❌ You're not currently leading a round in that group."
            )
            await asyncio.to_thread(db.clear_pending_input, user.id)
            return

        await asyncio.to_thread(db.set_word, chat_id, word)
        await asyncio.to_thread(db.clear_pending_input, user.id)

        await update.message.reply_text(
            f"✅ Word set! Players in the group will now try to guess it."
        )

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🔄 {user.first_name} changed the word.",
            )
        except (Forbidden, BadRequest):
            pass


async def group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    chat = update.effective_chat
    user = update.effective_user

    game = await asyncio.to_thread(db.get_game, chat.id)
    if not game or game[1] is None or not game[4]:
        return

    _, leader_id, leader_name, secret_word, _ = game
    if not secret_word:
        return

    text = message.text.strip()
    text_lower = text.lower()
    word_lower = secret_word.lower()
    word_pattern = re.compile(r"\b" + re.escape(word_lower) + r"\b")

    if text_lower == "hint" and user.id != leader_id:
        log = await asyncio.to_thread(db.get_explain_log, chat.id, leader_id)
        if log:
            leader_message_id = await asyncio.to_thread(db.get_leader_message_id, chat.id)
            hints_text = format_numbered_hints(log, chat, leader_message_id)
            await safe_send(
                context.bot,
                chat.id,
                text=f"💡 <b>Hints from {esc(leader_name)}:</b>\n\n{hints_text}",
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        else:
            await safe_send(
                context.bot,
                chat.id,
                text=f"💡 {leader_name} hasn't given any hints yet.",
            )
        return

    if user.id == leader_id:
        if word_pattern.search(text_lower):
            try:
                await message.delete()
            except (Forbidden, BadRequest):
                pass

            await asyncio.to_thread(db.award_coins, leader_id, chat.id, leader_name, -1)
            await asyncio.to_thread(db.clear_leader, chat.id)
            await asyncio.to_thread(db.set_explaining_started, chat.id, False)
            cancel_explain_timeout(context, chat.id)

            await safe_send(
                context.bot,
                chat.id,
                text=(
                    f"-1 💵\n\n"
                    f"{leader_name} revealed the word.\n\n"
                    f"Who wants to explain?"
                ),
                reply_markup=volunteer_keyboard(),
            )
        else:
            existing_log = await asyncio.to_thread(db.get_explain_log, chat.id, leader_id)
            if not existing_log:
                await asyncio.to_thread(db.set_leader_message_id, chat.id, message.message_id)
            await asyncio.to_thread(db.append_explain_log, chat.id, leader_id, text)
        return

    guess_clean = re.sub(r"[^\w\s]", "", text_lower).strip()
    if guess_clean == word_lower:
        winner = user

        if not await asyncio.to_thread(db.is_explaining_started, chat.id):
            cheat_penalty = get_reward("cheat")

            try:
                await message.delete()
            except (Forbidden, BadRequest):
                pass

            await asyncio.to_thread(db.award_coins, winner.id, chat.id, winner.first_name, cheat_penalty)
            await asyncio.to_thread(db.clear_leader, chat.id)
            await asyncio.to_thread(db.set_explaining_started, chat.id, False)
            cancel_explain_timeout(context, chat.id)
            await asyncio.to_thread(db.clear_stop_votes, chat.id)

            await safe_send(
                context.bot,
                chat.id,
                text=(
                    f"🚨 {winner.first_name} cheated. {cheat_penalty}\n\n"
                    f"Who wants to explain?"
                ),
                reply_markup=volunteer_keyboard(),
            )
            return

        await safe_react(context.bot, chat.id, message.message_id, "🎉")

        win_reward = get_reward("win")
        await asyncio.to_thread(db.award_coins, winner.id, chat.id, winner.first_name, win_reward)
        await asyncio.to_thread(db.award_coins, leader_id, chat.id, leader_name, win_reward)

        await asyncio.to_thread(db.increment_stat, "games_played", 1)
        await asyncio.to_thread(db.increment_stat, "words_solved", 1)
        await asyncio.to_thread(db.increment_group_games, chat.id)
        await asyncio.to_thread(db.increment_user_game_stats, winner.id, 1, 1)
        await asyncio.to_thread(db.increment_user_game_stats, leader_id, 1, 0)

        cancel_explain_timeout(context, chat.id)

        auto_lead = await asyncio.to_thread(db.get_auto_lead, winner.id)

        result_text = (
            f"🎉 {winner.first_name} found the word!\n\n"
            f"📝 Word: {secret_word}\n\n"
            f"{DOLLAR} {winner.first_name} +{win_reward}\n"
            f"🎤 {leader_name} +{win_reward}\n"
        )

        if auto_lead:
            new_word = await asyncio.to_thread(random_word, chat.id)
            await asyncio.to_thread(db.set_leader, chat.id, winner.id, winner.first_name, new_word)
            await asyncio.to_thread(db.set_explaining_started, chat.id, False)
            await asyncio.to_thread(db.start_explain_log, chat.id, winner.id)

            await safe_send(context.bot, chat.id, text=result_text)

            sent = await safe_send(
                context.bot,
                chat.id,
                text=(
                    f"{esc(winner.first_name)} is explaining the next word!\n\n"
                    f"<i>ᵀᵒ ᵈⁱˢᵃᵇˡᵉ ᵃᵘᵗᵒ⁻ˡᵉᵃᵈ, ᵒᵖᵉⁿ ˢᵉᵗᵗⁱⁿᵍˢ ⁱⁿ ᵐʸ ᴾᴹ</i>"
                ),
                parse_mode="HTML",
                reply_markup=leader_keyboard(),
            )

            if sent is None:
                await asyncio.to_thread(db.clear_leader, chat.id)
                await safe_send(
                    context.bot,
                    chat.id,
                    text="⚠️ Connection hiccup - who wants to explain?",
                    reply_markup=volunteer_keyboard(),
                )
            else:
                schedule_explain_timeout(context, chat.id, winner.id, winner.first_name)
        else:
            await asyncio.to_thread(db.clear_leader, chat.id)
            await asyncio.to_thread(db.set_explaining_started, chat.id, False)

            await safe_send(context.bot, chat.id, text=result_text)
            await safe_send(
                context.bot,
                chat.id,
                text="Who wants to explain?",
                reply_markup=volunteer_keyboard(),
            )
        return

    if await asyncio.to_thread(db.is_explaining_started, chat.id) and is_close_guess(guess_clean, word_lower):
        await safe_react(context.bot, chat.id, message.message_id, "👀")
        return


async def hint_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text(
            "💡 Run /hint inside the group that's playing."
        )
        return

    chat = update.effective_chat
    game = await asyncio.to_thread(db.get_game, chat.id)

    if not game or game[1] is None:
        await update.message.reply_text("❌ There's no active round right now.")
        return

    leader_id, leader_name = game[1], game[2]
    log = await asyncio.to_thread(db.get_explain_log, chat.id, leader_id)

    if log:
        leader_message_id = await asyncio.to_thread(db.get_leader_message_id, chat.id)
        hints_text = format_numbered_hints(log, chat, leader_message_id)
        await update.message.reply_html(
            f"💡 <b>Hints from {esc(leader_name)}:</b>\n\n{hints_text}",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    else:
        await update.message.reply_text(
            f"💡 {leader_name} hasn't given any hints yet."
        )


async def autolead_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text(
            "⚙️ Please open my private chat and use /autolead there."
        )
        return

    user = update.effective_user
    await asyncio.to_thread(db.add_user, user.id, user.first_name)

    await update.message.reply_html(
        "⚙️ <b>Auto-Lead</b>\n\n"
        "When you win a round, should you automatically become the next leader?",
        reply_markup=settings_keyboard(user.id),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"""
📖 <b>How to Play</b>

1️⃣ Someone runs /wordgame in the group to start a round.
2️⃣ Tap <b>"I'll Explain"</b> to volunteer as leader.
3️⃣ The leader taps <b>See Word</b> (view a random word) or <b>Write Word</b> (DM the bot your own word).
4️⃣ The leader gives clues in the group <u>without saying the word itself</u> - saying it by accident costs them {DOLLAR}1.
5️⃣ Anyone can type the exact word in the group to guess it.
6️⃣ Guess correctly and both you and the leader earn {DOLLAR}1.
7️⃣ Stuck? Type <code>hint</code> or use /hint to see everything the leader has said so far.

⚙️ <b>Other commands</b>
/wordgame - start a round (admins only once a round is live)
/hint - see the leader's clues so far
/stopvote - vote to stop a stuck round (3 votes needed)
/stopgame - admins can stop a round instantly
/leaderboard - this chat's leaderboard
/chattop - top players in this chat
/globaltop - top players across all chats
/topplayers - top 15 players, this chat or global
/topgroups - the 10 most active groups
/autolead - turn auto-lead on/off (open my PM)
/settings - same as /autolead

💡 <b>Auto-lead</b>: if it's on, winning a round automatically makes you the next leader. Turn it off anytime in my PM with /autolead.

⏰ If the leader doesn't tap See Word/Write Word in time, the round auto-ends and asks who wants to explain next.
"""
    await update.message.reply_html(text)


def format_board(rows, title, position_info=None, requester_name=None):
    text = f"🏆 <b>{title}</b>\n\n"

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    if not rows:
        text += "No scores yet.\n"
    else:
        for i, (name, coins) in enumerate(rows, start=1):
            prefix = medals.get(i, f"{i}.")
            display_name = truncate_name(name)
            text += f"{prefix} {display_name}\t{DOLLAR}{coins}\n"

    if position_info:
        pos, name, coins = position_info
        display_name = truncate_name(requester_name or name)
        text += f"\n  Your Position\n{pos}. {display_name} {DOLLAR}{coins}\n"

    text += ad_footer_html()

    return text


def leaderboard_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📍 In This Chat", callback_data="lb_chat"),
            InlineKeyboardButton("🌍 Global", callback_data="lb_global"),
        ]
    ])


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    rows = await asyncio.to_thread(db.get_chat_leaderboard, chat_id)
    position_info = await asyncio.to_thread(db.get_chat_position, chat_id, user.id)

    await update.message.reply_html(
        format_board(rows, "Leaderboard - This Chat", position_info, user.first_name),
        reply_markup=leaderboard_keyboard(),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def chat_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text(
            "🏆 Run this inside a group to see that group's leaderboard."
        )
        return

    user = update.effective_user
    rows = await asyncio.to_thread(db.get_chat_leaderboard, update.effective_chat.id)
    position_info = await asyncio.to_thread(db.get_chat_position, update.effective_chat.id, user.id)
    await update.message.reply_html(
        format_board(rows, "Group Leaderboard", position_info, user.first_name),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def global_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rows = await asyncio.to_thread(db.get_global_leaderboard)
    position_info = await asyncio.to_thread(db.get_global_position, user.id)
    await update.message.reply_html(
        format_board(rows, "Global Leaderboard", position_info, user.first_name),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    update_id = getattr(update, "update_id", "?")
    chat_id = getattr(getattr(update, "effective_chat", None), "id", "?")
    logger.error(
        f"Unhandled exception while processing update_id={update_id} chat_id={chat_id}",
        exc_info=context.error,
    )


async def pre_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if user is None:
        return

    user_banned = await asyncio.to_thread(db.is_user_banned, user.id)
    if not is_owner(user.id) and user_banned:
        is_command = bool(
            update.effective_message
            and update.effective_message.text
            and update.effective_message.text.startswith("/")
        )
        if is_command:
            try:
                await update.effective_message.reply_text(
                    "🚫 You have been banned from using this bot."
                )
            except (Forbidden, BadRequest):
                pass
        elif update.callback_query:
            try:
                await update.callback_query.answer(
                    "🚫 You have been banned from using this bot.",
                    show_alert=True,
                )
            except (Forbidden, BadRequest):
                pass
        raise ApplicationHandlerStop

    if chat is not None and chat.type != ChatType.PRIVATE:
        group_banned = await asyncio.to_thread(db.is_group_banned, chat.id)
        if group_banned:
            try:
                await context.bot.leave_chat(chat.id)
            except (Forbidden, BadRequest):
                pass
            raise ApplicationHandlerStop

        await asyncio.to_thread(
            db.register_group,
            chat.id,
            chat.title or str(chat.id),
            getattr(chat, "username", None),
        )

    maintenance_mode = await asyncio.to_thread(db.get_config, "maintenance_mode", "0")
    if not is_bot_admin_or_owner(user.id) and maintenance_mode == "1":
        is_command = bool(
            update.effective_message
            and update.effective_message.text
            and update.effective_message.text.startswith("/")
        )
        if is_command:
            try:
                await update.effective_message.reply_text(
                    "🛠️ The bot is currently under maintenance. Please try again later."
                )
            except (Forbidden, BadRequest):
                pass
        elif update.callback_query:
            try:
                await update.callback_query.answer(
                    "🛠️ Under maintenance. Please try again later.",
                    show_alert=True,
                )
            except (Forbidden, BadRequest):
                pass
        raise ApplicationHandlerStop

    await asyncio.to_thread(db.add_user, user.id, user.first_name, user.username)


async def on_bot_membership_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if result.new_chat_member.user.id != context.bot.id:
        return

    chat = update.effective_chat
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    actor = result.from_user

    if new_status in ("member", "administrator"):
        if await asyncio.to_thread(db.is_group_banned, chat.id):
            try:
                await context.bot.leave_chat(chat.id)
            except (Forbidden, BadRequest):
                pass
            return

        is_fresh_add = old_status in ("left", "kicked")

        group_link = await _resolve_group_link(context, chat)

        await asyncio.to_thread(
            db.register_group,
            chat.id,
            chat.title or str(chat.id),
            getattr(chat, "username", None),
            group_link,
            actor.id if actor else None,
            (actor.first_name if actor else None),
        )

        if is_fresh_add:
            # Feature 3: the configurable welcome/prompt message
            if group_join_prompt_enabled():
                try:
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=get_group_join_prompt(),
                    )
                except Exception as e:
                    logger.error(f"Failed to send group join prompt: {e}")

            await _notify_owner_group_event(context, "added", chat, actor, group_link)
        return

    if new_status in ("left", "kicked"):
        was_in_group = old_status in ("member", "administrator", "restricted")

        # Read the stored link BEFORE marking left - we can't create an invite
        # link for a group we're no longer in.
        stored_link = None
        try:
            details = await asyncio.to_thread(db.get_group_full_details, chat.id)
            if details:
                stored_link = details[8]
        except Exception:
            stored_link = None

        if not stored_link and getattr(chat, "username", None):
            stored_link = f"https://t.me/{chat.username}"

        await asyncio.to_thread(
            db.mark_group_left,
            chat.id,
            actor.id if actor else None,
            (actor.first_name if actor else None),
        )

        if was_in_group:
            await _notify_owner_group_event(context, "removed", chat, actor, stored_link)


def owner_top_groups_keyboard():
    rows = db.get_top_groups_full(10)
    keyboard = [
        [InlineKeyboardButton(
            f"{i}. {truncate_name(title or 'Untitled Group')} — {games:,} Games",
            callback_data=f"og:{chat_id}",
        )]
        for i, (chat_id, title, games) in enumerate(rows, start=1)
    ]
    if keyboard:
        keyboard.extend(ad_footer_keyboard_rows())
    return rows, InlineKeyboardMarkup(keyboard) if keyboard else None


async def topgroups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if is_owner(user.id):
        rows, keyboard = owner_top_groups_keyboard()
        if not rows:
            await update.message.reply_text("No group activity recorded yet.")
            return
        await update.message.reply_html(
            "🏆 <b>Top 10 Active Groups</b>\n<i>Tap a group to view its details.</i>",
            reply_markup=keyboard,
        )
        return

    rows = await asyncio.to_thread(db.get_top_groups, 10)

    if not rows:
        await update.message.reply_text("No group activity recorded yet.")
        return

    lines = ["🏆 <b>Top 10 Active Groups</b>\n"]
    for i, (title, games) in enumerate(rows, start=1):
        lines.append(f"{i}. {truncate_name(title or 'Untitled Group')} — {games:,} Games")

    await update.message.reply_html(
        "\n".join(lines) + ad_footer_html(),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


def format_top_players(rows, title, position_info, requester_name, position_label):
    text = f"🏆 <b>{title}</b>\n\n"

    if not rows:
        text += "No scores yet.\n"
    else:
        for i, (name, coins) in enumerate(rows, start=1):
            display_name = truncate_name(name)
            text += f"{i}. {display_name}\t{DOLLAR}{coins}\n"

    if position_info:
        pos, name, coins = position_info
        display_name = truncate_name(requester_name or name)
        text += f"\n{position_label}:\n#{pos} • {display_name} • {DOLLAR}{coins}\n"

    text += ad_footer_html()

    return text


def topplayers_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📍 In This Chat", callback_data="tp_chat"),
            InlineKeyboardButton("🌍 Global", callback_data="tp_global"),
        ]
    ])


def owner_top_players_keyboard(scope: str, ctx_chat_id: int):
    if scope == "chat":
        rows = db.get_chat_leaderboard_with_ids(ctx_chat_id, limit=15)
    else:
        rows = db.get_global_leaderboard_with_ids(limit=15)

    if not rows:
        return None

    keyboard = [
        [InlineKeyboardButton(
            f"{i}. {truncate_name(name)} — {DOLLAR}{coins}",
            callback_data=f"op:{uid}:{scope}:{ctx_chat_id}",
        )]
        for i, (uid, name, coins) in enumerate(rows, start=1)
    ]
    keyboard.extend(ad_footer_keyboard_rows())
    keyboard.append([InlineKeyboardButton("⬅ Back", callback_data="tp_back")])
    return InlineKeyboardMarkup(keyboard)


async def topplayers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "🏆 <b>Top Players</b>\n\nChoose a scope:",
        reply_markup=topplayers_keyboard(),
    )


def _join_names(names):
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


async def stopvote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text("🗳️ Run /stopvote inside the group that's playing.")
        return

    chat = update.effective_chat
    user = update.effective_user

    game = await asyncio.to_thread(db.get_game, chat.id)
    if not game or game[1] is None:
        await update.message.reply_text("❌ There's no active round to stop.")
        return

    added = await asyncio.to_thread(db.add_stop_vote, chat.id, user.id, user.first_name)
    if not added:
        await update.message.reply_text("⚠️ You've already voted to stop this game.")
        return

    count = await asyncio.to_thread(db.get_stop_vote_count, chat.id)

    if count >= 3:
        names = await asyncio.to_thread(db.get_stop_vote_names, chat.id)

        await asyncio.to_thread(db.clear_leader, chat.id)
        await asyncio.to_thread(db.set_explaining_started, chat.id, False)
        cancel_explain_timeout(context, chat.id)
        await asyncio.to_thread(db.clear_stop_votes, chat.id)

        await safe_send(
            context.bot,
            chat.id,
            text=(
                f"🛑 Game stopped.\n\n"
                f"{_join_names(names)} voted to stop the game.\n\n"
                f"Who wants to explain?"
            ),
            reply_markup=volunteer_keyboard(),
        )
    else:
        await update.message.reply_text(
            f"🗳️ {user.first_name} voted to stop the game. ({count}/3)"
        )


async def stopgame_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text("🛑 Run /stopgame inside the group that's playing.")
        return

    if not (is_owner(update.effective_user.id) or await is_chat_admin(update, context)):
        await update.message.reply_text("❌ Only group admins (or the bot owner) can use /stopgame.")
        return

    chat = update.effective_chat
    game = await asyncio.to_thread(db.get_game, chat.id)
    if not game or game[1] is None:
        await update.message.reply_text("❌ There's no active round to stop.")
        return

    await asyncio.to_thread(db.clear_leader, chat.id)
    await asyncio.to_thread(db.set_explaining_started, chat.id, False)
    cancel_explain_timeout(context, chat.id)
    await asyncio.to_thread(db.clear_stop_votes, chat.id)

    await update.message.reply_html(
        "🛑 <b>Game stopped.</b>\n\nWho wants to explain?",
        reply_markup=volunteer_keyboard(),
    )


def _ad_inquiry_message() -> str:
    return (
        "Hi! I'd like to advertise on @Uff_GameBot. Here are the details:\n\n"
        "Business/Project Name: \n"
        "Telegram Username or Link: \n"
        "Preferred Advertisement Duration: \n"
        "Budget: \n"
        "Advertisement Text or Description: \n"
        "Additional Information: \n\n"
        "Thanks, looking forward to hearing from you!"
    )


def _ad_divider() -> str:
    return "______________________________"


def _submit_ad_link_html() -> str:
    link = f"https://t.me/{ADS_BOT_USERNAME}?start=submitad"
    return f'<a href="{link}">Place your advert here 👈</a>'


def _parse_sponsored_ad_target(target: str) -> str:
    target = target.strip()
    if target.startswith("http://") or target.startswith("https://"):
        return target
    return f"https://t.me/{target.lstrip('@')}"


def _build_sponsored_ad_html(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""

    bracket_match = re.search(r"\[([^\]]+)\]\(([^)]+)\)", raw)
    if bracket_match:
        label, target = bracket_match.group(1).strip(), bracket_match.group(2).strip()
        url = _parse_sponsored_ad_target(target)
        link_html = f'<a href="{html_lib.escape(url, quote=True)}">{esc(label)}</a>'
        before, after = raw[:bracket_match.start()], raw[bracket_match.end():]
        return f'{esc(before)}{link_html}{esc(after)}'

    if "|" in raw:
        label, _, target = raw.partition("|")
        label, target = label.strip(), target.strip()
        url = _parse_sponsored_ad_target(target)
        return f'<a href="{html_lib.escape(url, quote=True)}">{esc(label) or esc(url)}</a>'

    if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", raw):
        return f'<a href="{_parse_sponsored_ad_target(raw)}">{esc(raw)}</a>'

    if re.fullmatch(r"https?://\S+", raw):
        return f'<a href="{html_lib.escape(raw, quote=True)}">{esc(raw)}</a>'

    url_match = re.search(r"https?://\S+", raw)
    if url_match:
        url = url_match.group(0)
        before, after = raw[:url_match.start()], raw[url_match.end():]
        return f'{esc(before)}<a href="{html_lib.escape(url, quote=True)}">{esc(url)}</a>{esc(after)}'

    at_match = re.search(r"@[A-Za-z0-9_]{5,32}", raw)
    if at_match:
        handle = at_match.group(0)
        before, after = raw[:at_match.start()], raw[at_match.end():]
        return f'{esc(before)}<a href="{_parse_sponsored_ad_target(handle)}">{esc(handle)}</a>{esc(after)}'

    return esc(raw)


def _build_sponsored_ad_button(raw: str):
    raw = raw.strip()
    if not raw:
        return raw, None

    bracket_match = re.search(r"\[([^\]]+)\]\(([^)]+)\)", raw)
    if bracket_match:
        label = bracket_match.group(1).strip()
        url = _parse_sponsored_ad_target(bracket_match.group(2).strip())
        return (label or url), url

    if "|" in raw:
        label, _, target = raw.partition("|")
        label, target = label.strip(), target.strip()
        url = _parse_sponsored_ad_target(target)
        return (label or url), url

    if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", raw) or re.fullmatch(r"https?://\S+", raw):
        return raw, _parse_sponsored_ad_target(raw)

    url_match = re.search(r"https?://\S+", raw) or re.search(r"@[A-Za-z0-9_]{5,32}", raw)
    if url_match:
        return raw, _parse_sponsored_ad_target(url_match.group(0))

    return raw, None


def _sponsored_ad_line_html() -> str:
    if db.get_config("sponsored_ad_enabled", "1") != "1":
        return ""

    override = db.get_config("sponsored_ad_override", "")
    if override:
        return _build_sponsored_ad_html(override)

    active_ads = db.get_active_ads_for_rotation()
    if not active_ads:
        return ""

    counter = int(db.get_config("ad_rotation_counter", "0"))
    subscription_id, business_name, ad_link, ad_text = active_ads[counter % len(active_ads)]
    db.set_config("ad_rotation_counter", str(counter + 1))
    db.record_ad_impression(subscription_id)

    if BOT_USERNAME:
        link = f"https://t.me/{BOT_USERNAME}?start=adclick_{subscription_id}"
    else:
        link = html_lib.escape(str(ad_link), quote=True)

    return f'<a href="{link}">{esc(business_name) or "Sponsored Ad"}</a>'


def ad_footer_html() -> str:
    if db.get_config("ads_enabled", "1") != "1":
        return ""

    sponsored_line = _sponsored_ad_line_html()

    parts = ["\n\nAdvertisements", _ad_divider()]
    if sponsored_line:
        parts.append(sponsored_line)
        parts.append(_ad_divider())
    parts.append(_submit_ad_link_html())

    return "\n".join(parts)


def ad_footer_keyboard_rows():
    if db.get_config("ads_enabled", "1") != "1":
        return []

    rows = [[InlineKeyboardButton("📢  Advertisements  📢", callback_data="noop")]]

    if db.get_config("sponsored_ad_enabled", "1") == "1":
        override = db.get_config("sponsored_ad_override", "")
        if override:
            label, url = _build_sponsored_ad_button(override)
            if url:
                rows.append([InlineKeyboardButton(label, url=url)])
            else:
                rows.append([InlineKeyboardButton(label, callback_data="noop")])
        else:
            active_ads = db.get_active_ads_for_rotation()
            if active_ads:
                counter = int(db.get_config("ad_rotation_counter", "0"))
                subscription_id, business_name, ad_link, ad_text = active_ads[counter % len(active_ads)]
                db.set_config("ad_rotation_counter", str(counter + 1))
                db.record_ad_impression(subscription_id)

                if BOT_USERNAME:
                    link = f"https://t.me/{BOT_USERNAME}?start=adclick_{subscription_id}"
                else:
                    link = str(ad_link)
                rows.append([InlineKeyboardButton(f"📢 {business_name or 'Sponsored Ad'}", url=link)])

    submit_link = f"https://t.me/{ADS_BOT_USERNAME}?start=submitad"
    rows.append([InlineKeyboardButton("Place your advert here 👈", url=submit_link)])

    return rows


def _parse_ad_manager_arg(context):
    if not context.args:
        return None
    username = context.args[0].lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return None
    return username


@owner_or_admin_only
async def addadmanager_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = _parse_ad_manager_arg(context)
    if username is None:
        await update.message.reply_text(
            "Usage: /addadmanager @username\n"
            "(letters, numbers, underscores - 5 to 32 characters, like any Telegram username)"
        )
        return

    await asyncio.to_thread(db.set_config, "ad_manager_username", username)
    await update.message.reply_text(
        f"✅ @{username} is now the Advertisement Manager. "
        f"The advertisement link will open https://t.me/{username}."
    )


@owner_or_admin_only
async def changeadmanager_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = _parse_ad_manager_arg(context)
    if username is None:
        await update.message.reply_text(
            "Usage: /changeadmanager @username\n"
            "(letters, numbers, underscores - 5 to 32 characters, like any Telegram username)"
        )
        return

    await asyncio.to_thread(db.set_config, "ad_manager_username", username)
    await update.message.reply_text(
        f"✅ Advertisement Manager updated to @{username}. "
        f"The advertisement link now opens https://t.me/{username} everywhere in the bot."
    )


@owner_or_admin_only
async def removeadmanager_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = await asyncio.to_thread(db.get_config, "ad_manager_username", "")
    if not current:
        await update.message.reply_text("⚠️ There's no Advertisement Manager currently assigned.")
        return

    await asyncio.to_thread(db.set_config, "ad_manager_username", "")
    await update.message.reply_text(
        "✅ Advertisement Manager removed. The advertisement section is now hidden "
        "from /leaderboard, /topplayers, and /topgroups until a new one is set."
    )


@owner_or_admin_only
async def admanager_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = await asyncio.to_thread(db.get_config, "ad_manager_username", "")
    if not current:
        await update.message.reply_text("No Advertisement Manager is currently assigned.")
        return

    await update.message.reply_text(f"Current Advertisement Manager:\n@{current}")


async def ad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner(user.id):
        await update.message.reply_text("🚫 You are not authorized to use this command.")
        return

    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Usage: /ad on  or  /ad off")
        return

    mode = context.args[0].lower()
    await asyncio.to_thread(db.set_config, "ads_enabled", "1" if mode == "on" else "0")

    if mode == "on":
        await update.message.reply_text("✅ Advertisements have been enabled successfully.")
    else:
        await update.message.reply_text("✅ Advertisements have been disabled successfully.")


@owner_only
async def sponsoredad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() not in ("on", "off"):
        current = await asyncio.to_thread(db.get_config, "sponsored_ad_enabled", "1")
        await update.message.reply_text(
            "Usage: /sponsoredad on|off\n"
            f"Currently: {'on' if current == '1' else 'off'}"
        )
        return

    mode = context.args[0].lower()
    await asyncio.to_thread(db.set_config, "sponsored_ad_enabled", "1" if mode == "on" else "0")
    await update.message.reply_text(f"✅ Sponsored Ad slot turned {mode}.")


@owner_only
async def changesponsoredad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        current = await asyncio.to_thread(db.get_config, "sponsored_ad_override", "")
        await update.message.reply_text(
            "Usage: /changesponsoredad <value>\n\n"
            "Formats accepted:\n"
            "• [Visible text](https://link.or/@username) - hides the link, shows blue text\n"
            "• Visible text | https://link.or/@username\n"
            "• a bare link or @username\n"
            "• plain text\n\n"
            "Or: /changesponsoredad clear - to remove the override\n\n"
            f"Current override: {current or '(none - showing the real rotating ad, if any)'}"
        )
        return

    value = " ".join(context.args).strip()

    if value.lower() in ("clear", "none", "reset"):
        await asyncio.to_thread(db.set_config, "sponsored_ad_override", "")
        await update.message.reply_text("✅ Sponsored Ad override cleared.")
        return

    await asyncio.to_thread(db.set_config, "sponsored_ad_override", value)
    await update.message.reply_text(f"✅ Sponsored Ad updated to: {value}")


@owner_only
async def prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = get_group_join_prompt()

    if not context.args:
        state = "🟢 ON" if group_join_prompt_enabled() else "🔴 OFF"
        await update.message.reply_html(
            f"⚙️ <b>Group Join Prompt</b>\n\n"
            f"Status: <b>{state}</b>\n\n"
            f"Current message:\n<i>{esc(current)}</i>\n\n"
            f"Usage:\n"
            f"<code>/prompt on</code> — send it whenever the bot is added to a group\n"
            f"<code>/prompt off</code> — stop sending it\n"
            f"<code>/editprompt (new prompt)</code> — change the message"
        )
        return

    arg = context.args[0].lower()

    if arg == "on":
        await asyncio.to_thread(db.set_config, "group_join_prompt_enabled", "1")
        await update.message.reply_html(
            f"🟢 Group join prompt is now <b>ON</b>.\n\n"
            f"It will be sent every time the bot is added to a group:\n\n<i>{esc(current)}</i>"
        )
    elif arg == "off":
        await asyncio.to_thread(db.set_config, "group_join_prompt_enabled", "0")
        await update.message.reply_html(
            "🔴 Group join prompt is now <b>OFF</b>.\n\n"
            "The bot will stay silent when it's added to a group."
        )
    else:
        await update.message.reply_text("Usage: /prompt on  |  /prompt off")


@owner_only
async def editprompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text or ""
    parts = raw.split(None, 1)

    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_html(
            "Usage: <code>/editprompt (new prompt message)</code>\n\n"
            f"Current message:\n<i>{esc(get_group_join_prompt())}</i>"
        )
        return

    new_text = parts[1].strip()
    await asyncio.to_thread(db.set_config, "group_join_prompt_text", new_text)

    state_note = (
        "" if group_join_prompt_enabled()
        else "\n\n⚠️ Note: the prompt is currently <b>OFF</b> — run <code>/prompt on</code> to start sending it."
    )
    await update.message.reply_html(
        f"✅ Group join prompt updated.\n\nNew message:\n<i>{esc(new_text)}</i>{state_note}"
    )


@owner_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_users = await asyncio.to_thread(db.get_user_count)
    total_groups = await asyncio.to_thread(db.get_group_count)
    games_played = await asyncio.to_thread(db.get_stat, "games_played")
    words_solved = await asyncio.to_thread(db.get_stat, "words_solved")
    total_rewards = await asyncio.to_thread(db.get_stat, "total_rewards_given")
    active_games = await asyncio.to_thread(db.get_active_games_count)
    uptime = format_uptime(datetime.now() - START_TIME)

    text = f"""
📊 <b>Bot Statistics</b>

👥 Total Users: <b>{total_users}</b>
👨‍👩‍👧‍👦 Total Groups: <b>{total_groups}</b>
🎮 Games Played: <b>{games_played}</b>
{DOLLAR} Total Rewards Given: <b>{total_rewards}</b>
🟢 Active Games: <b>{active_games}</b>
⏱️ Bot Uptime: <b>{uptime}</b>
📝 Registered Players: <b>{total_users}</b>
✅ Total Words Solved: <b>{words_solved}</b>
"""
    await update.message.reply_html(text)


OWNER_LIST_PAGE_SIZE = 50
BOTGROUPS_PAGE_SIZE = 20

MAX_SEARCH_RESULTS = 50


def build_users_list_page(page: int):
    total = db.get_users_count()
    offset = page * OWNER_LIST_PAGE_SIZE
    rows = db.get_users_page(offset, OWNER_LIST_PAGE_SIZE)

    if not rows:
        return None, None

    total_pages = max(1, (total + OWNER_LIST_PAGE_SIZE - 1) // OWNER_LIST_PAGE_SIZE)
    start_n = offset + 1
    end_n = offset + len(rows)

    header = (
        f"👥 <b>Users</b> ({total} total) — Page {page + 1}/{total_pages}\n"
        f"<i>Entries {start_n}–{end_n} • Tap a player for full details.</i>"
    )
    if offset + OWNER_LIST_PAGE_SIZE >= total:
        header += "\n<i>This is the end of the player list.</i>"

    keyboard = [
        [InlineKeyboardButton(
            f"{start_n + i}. {truncate_name(name)} — {DOLLAR}{coins}",
            callback_data=f"op:{uid}:userslist:{page}",
        )]
        for i, (uid, name, username, coins, last_active) in enumerate(rows)
    ]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"ulpg:{page - 1}"))
    if offset + OWNER_LIST_PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"ulpg:{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    return header, InlineKeyboardMarkup(keyboard)


def build_groups_list_page(page: int):
    total = db.get_groups_count()
    offset = page * OWNER_LIST_PAGE_SIZE
    rows = db.get_groups_page(offset, OWNER_LIST_PAGE_SIZE)

    if not rows:
        return None, None

    total_pages = max(1, (total + OWNER_LIST_PAGE_SIZE - 1) // OWNER_LIST_PAGE_SIZE)
    start_n = offset + 1
    end_n = offset + len(rows)

    header = (
        f"👨‍👩‍👧‍👦 <b>Groups</b> ({total} total) — Page {page + 1}/{total_pages}\n"
        f"<i>Entries {start_n}–{end_n} • Tap a group for full details.</i>"
    )
    if offset + OWNER_LIST_PAGE_SIZE >= total:
        header += "\n<i>This is the end of the group list.</i>"

    keyboard = [
        [InlineKeyboardButton(
            f"{start_n + i}. {truncate_name(title or 'Untitled Group')} — {games:,} Games",
            callback_data=f"og:{chat_id}:gl:{page}",
        )]
        for i, (chat_id, title, member_count, games, last_active) in enumerate(rows)
    ]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"glpg:{page - 1}"))
    if offset + OWNER_LIST_PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"glpg:{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    return header, InlineKeyboardMarkup(keyboard)


def build_botgroups_list_page(page: int):
    total = db.get_botgroups_count()
    offset = page * BOTGROUPS_PAGE_SIZE
    rows = db.get_botgroups_page(offset, BOTGROUPS_PAGE_SIZE)

    if not rows:
        return None, None

    total_pages = max(1, (total + BOTGROUPS_PAGE_SIZE - 1) // BOTGROUPS_PAGE_SIZE)
    start_n = offset + 1
    end_n = offset + len(rows)

    s = db.get_bot_groups_stats()
    header = (
        f"👥 <b>Bot Groups</b>\n"
        f"📊 Total Ever: <b>{s['total']}</b> | 🟢 Current: <b>{s['current']}</b> | 🔴 Removed: <b>{s['removed']}</b>\n\n"
        f"Page {page + 1}/{total_pages} • <code>🎮 Word Game | 🔤 Scramble</code>\n"
        f"<i>Entries {start_n}–{end_n} • Tap a group to inspect/join.</i>"
    )

    keyboard = []
    for i, (chat_id, title, wg_played, left_at, scr_played) in enumerate(rows):
        status = "🟢" if not left_at else "🔴"
        label = f"{start_n + i}. {status} {truncate_name(title or 'Untitled Group')} — 🎮{wg_played} 🔤{scr_played}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"bg:{chat_id}:{page}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"bglpg:{page - 1}"))
    if offset + BOTGROUPS_PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"bglpg:{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    return header, InlineKeyboardMarkup(keyboard)


@owner_only
async def botgroups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    header, keyboard = build_botgroups_list_page(0)
    if header is None:
        await update.message.reply_text("No groups registered yet.")
        return
    await update.message.reply_html(header, reply_markup=keyboard)


@owner_only
async def groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        query = " ".join(context.args)
        rows = await asyncio.to_thread(db.search_groups, query)

        if not rows:
            await update.message.reply_text("No matching groups found.")
            return

        shown = rows[:MAX_SEARCH_RESULTS]
        lines = [f"👨‍👩‍👧‍👦 <b>Groups</b> (showing {len(shown)} of {len(rows)})\n"]
        for chat_id, title, member_count, games_played in shown:
            members = member_count if member_count else "—"
            lines.append(
                f"• <b>{truncate_name(title or 'Untitled Group')}</b>\n"
                f"  ID: <code>{chat_id}</code> | Members: {members} | Games: {games_played}"
            )
        if len(rows) > MAX_SEARCH_RESULTS:
            lines.append(f"\n<i>+{len(rows) - MAX_SEARCH_RESULTS} more - refine your search to narrow it down.</i>")

        await update.message.reply_html("\n".join(lines))
        return

    header, keyboard = build_groups_list_page(0)
    if header is None:
        await update.message.reply_text("No groups registered yet.")
        return

    await update.message.reply_html(header, reply_markup=keyboard)


@owner_only
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        query = " ".join(context.args)
        rows = await asyncio.to_thread(db.search_users, query)

        if not rows:
            await update.message.reply_text("No matching users found.")
            return

        shown = rows[:MAX_SEARCH_RESULTS]
        lines = [f"👥 <b>Users</b> (showing {len(shown)} of {len(rows)})\n"]
        for uid, name, username, coins in shown:
            uname = f"@{username}" if username else "—"
            lines.append(
                f"• {truncate_name(name)} ({uname})\n"
                f"  ID: <code>{uid}</code> | Balance: {DOLLAR}{coins}"
            )
        if len(rows) > MAX_SEARCH_RESULTS:
            lines.append(f"\n<i>+{len(rows) - MAX_SEARCH_RESULTS} more - refine your search to narrow it down.</i>")

        await update.message.reply_html("\n".join(lines))
        return

    header, keyboard = build_users_list_page(0)
    if header is None:
        await update.message.reply_text("No users registered yet.")
        return

    await update.message.reply_html(header, reply_markup=keyboard)


@owner_or_admin_only
async def banuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /banuser <Telegram ID>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid numeric Telegram ID.")
        return
    if is_owner(target_id):
        await update.message.reply_text("❌ You can't ban yourself.")
        return

    await asyncio.to_thread(db.ban_user, target_id, update.effective_user.id)
    await update.message.reply_text(f"🚫 User <code>{target_id}</code> has been banned.", parse_mode="HTML")


@owner_or_admin_only
async def unbanuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unbanuser <Telegram ID>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid numeric Telegram ID.")
        return

    await asyncio.to_thread(db.unban_user, target_id)
    await update.message.reply_text(f"✅ User <code>{target_id}</code> has been unbanned.", parse_mode="HTML")


@owner_or_admin_only
async def bangroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /bangroup <Group ID>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid numeric Group ID.")
        return

    await asyncio.to_thread(db.ban_group, target_id, update.effective_user.id)
    await update.message.reply_text(f"🚫 Group <code>{target_id}</code> has been banned.", parse_mode="HTML")

    try:
        await context.bot.leave_chat(target_id)
    except (Forbidden, BadRequest):
        pass


@owner_or_admin_only
async def unbangroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unbangroup <Group ID>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid numeric Group ID.")
        return

    await asyncio.to_thread(db.unban_group, target_id)
    await update.message.reply_text(f"✅ Group <code>{target_id}</code> has been unbanned.", parse_mode="HTML")


@owner_or_admin_only
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() not in ("users", "groups"):
        await update.message.reply_text(
            "Usage: /broadcast users <message>\n/broadcast groups <message>\n"
            "(or reply to a message with /broadcast users|groups)"
        )
        return

    target = context.args[0].lower()
    message_text = " ".join(context.args[1:]).strip()

    if not message_text and update.message.reply_to_message:
        message_text = (
            update.message.reply_to_message.text
            or update.message.reply_to_message.caption
            or ""
        )

    if not message_text:
        await update.message.reply_text(
            "❌ Provide a message, or reply to the message you want to broadcast."
        )
        return

    if target == "users":
        recipients = [row[0] for row in await asyncio.to_thread(db.get_all_users)]
    else:
        recipients = [row[0] for row in await asyncio.to_thread(db.get_all_groups)]

    status = await update.message.reply_text(
        f"📣 Broadcasting to {len(recipients)} {target}..."
    )

    success, failed = 0, 0
    for recipient_id in recipients:
        try:
            await context.bot.send_message(chat_id=recipient_id, text=message_text)
            success += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await status.edit_text(
        f"📣 <b>Broadcast Complete</b>\n\n"
        f"✅ Successful: {success}\n"
        f"❌ Failed: {failed}\n"
        f"👥 Total Recipients: {len(recipients)}",
        parse_mode="HTML",
    )


@owner_or_admin_only
async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Usage: /maintenance on|off")
        return

    mode = context.args[0].lower()
    await asyncio.to_thread(db.set_config, "maintenance_mode", "1" if mode == "on" else "0")

    await update.message.reply_text(
        f"🛠️ Maintenance mode {'enabled' if mode == 'on' else 'disabled'}."
    )


@owner_only
async def setreward_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /setreward win|cheat|daily <amount>\n"
            "e.g. /setreward win 10, /setreward cheat -1, /setreward daily 20"
        )
        return

    reward_type = context.args[0].lower()
    if reward_type not in ("win", "cheat", "daily"):
        await update.message.reply_text("❌ Type must be win, cheat, or daily.")
        return

    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Amount must be a whole number (e.g. 10 or -1).")
        return

    await asyncio.to_thread(db.set_config, f"reward_{reward_type}", amount)

    await update.message.reply_text(
        f"✅ {reward_type.capitalize()} reward set to {amount}. Takes effect immediately."
    )


@owner_only
async def reloadwords_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MEDIUM_WORDS, WORDS

    importlib.reload(mw)
    MEDIUM_WORDS = _build_medium_words()
    WORDS = _merge_all_words()

    await update.message.reply_text(
        f"🔄 Word list reloaded. Total words loaded: {len(WORDS)}"
    )


@owner_only
async def forceend_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        try:
            target_chat_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Please provide a valid numeric Group ID.")
            return
    elif update.effective_chat.type != ChatType.PRIVATE:
        target_chat_id = update.effective_chat.id
    else:
        await update.message.reply_text(
            "Usage: /forceend <Group ID>  (or run this inside the target group)"
        )
        return

    await asyncio.to_thread(db.end_game, target_chat_id)
    cancel_explain_timeout(context, target_chat_id)
    await asyncio.to_thread(db.clear_stop_votes, target_chat_id)

    await safe_send(
        context.bot,
        target_chat_id,
        text="🛑 This game was ended by the bot owner.",
    )

    if update.effective_chat.id != target_chat_id:
        await update.message.reply_text(f"✅ Game in {target_chat_id} ended.")


@owner_only
async def forceleader_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text("⚠️ Run /forceleader inside the target group.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /forceleader <@username or Telegram ID>")
        return

    target_arg = context.args[0].lstrip("@")

    if target_arg.isdigit():
        row = await asyncio.to_thread(db.get_user_by_id, int(target_arg))
    else:
        row = await asyncio.to_thread(db.get_user_by_username, target_arg)

    if not row:
        await update.message.reply_text(
            "❌ That player hasn't interacted with me yet - ask them to send /start in my PM first."
        )
        return

    target_id, target_name = row[0], row[1]
    chat_id = update.effective_chat.id

    cancel_explain_timeout(context, chat_id)
    word = random_word(chat_id)
    await asyncio.to_thread(db.set_leader, chat_id, target_id, target_name, word)
    await asyncio.to_thread(db.set_explaining_started, chat_id, False)
    await asyncio.to_thread(db.start_explain_log, chat_id, target_id)
    schedule_explain_timeout(context, chat_id, target_id, target_name)

    await safe_send(
        context.bot,
        chat_id,
        text=(
            f"👑 The owner assigned <b>{esc(target_name)}</b> as the new leader!\n\n"
            f"<i>ᵀᵒ ᵈⁱˢᵃᵇˡᵉ ᵃᵘᵗᵒ⁻ˡᵉᵃᵈ, ᵒᵖᵉⁿ ˢᵉᵗᵗⁱⁿᵍˢ ⁱⁿ ᵐʸ ᴾᴹ</i>"
        ),
        parse_mode="HTML",
        reply_markup=leader_keyboard(),
    )


@owner_only
async def topup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /topup <@username or ID> <+amount|-amount>\n"
            "e.g. /topup @John +50"
        )
        return

    target_arg, amount_arg = context.args[0].lstrip("@"), context.args[1]

    if not re.fullmatch(r"[+-]?\d+", amount_arg):
        await update.message.reply_text("❌ Amount must look like +50 or -20.")
        return

    amount = int(amount_arg)

    if target_arg.isdigit():
        row = await asyncio.to_thread(db.get_user_by_id, int(target_arg))
    else:
        row = await asyncio.to_thread(db.get_user_by_username, target_arg)

    if not row:
        await update.message.reply_text(f"❌ No player found matching {context.args[0]}.")
        return

    target_id, target_name, target_username, _ = row

    await asyncio.to_thread(db.award_global_coins, target_id, target_name, amount)

    apply_chat_credit_for_global_earning(
        target_id, target_name, amount, current_chat=update.effective_chat
    )

    new_balance = await asyncio.to_thread(db.get_global_coins, target_id)
    await asyncio.to_thread(db.log_topup, target_id, target_name, amount, update.effective_user.id)

    action = "added" if amount >= 0 else "deducted"
    display = f"@{target_username}" if target_username else esc(target_name)

    confirmation = (
        f"✅ Successfully {action} {abs(amount)} to {display}.\n"
        f"New Balance: {DOLLAR}{new_balance}"
    )

    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=confirmation, parse_mode="HTML")
    except (Forbidden, BadRequest):
        pass

    if update.effective_chat.id != OWNER_ID:
        await update.message.reply_text("✅ Done - check your DM for details.")
    else:
        await update.message.reply_html(confirmation)


@owner_only
async def share_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage:\n"
            "/share allusers +<amount> [message]\n"
            "/share top<N> +<amount> [message]\n\n"
            "Examples:\n"
            "/share allusers +50\n"
            "/share allusers +50 Apology from admin.\n"
            "/share top10 +50\n"
            "/share top50 +100 Bonus from admin."
        )
        return

    target_arg = context.args[0].lower()
    top_match = re.fullmatch(r"top(\d+)", target_arg)

    if target_arg == "allusers":
        top_n = None
    elif top_match:
        top_n = int(top_match.group(1))
        if top_n <= 0:
            await update.message.reply_text("❌ topN must be a positive number, e.g. top10.")
            return
    else:
        await update.message.reply_text(
            "❌ First argument must be 'allusers' or 'topN' (e.g. top10, top50)."
        )
        return

    amount_str = context.args[1].lstrip("+")
    if not amount_str.isdigit() or int(amount_str) <= 0:
        await update.message.reply_text("❌ Amount must be a positive whole number, e.g. +50.")
        return
    amount = int(amount_str)

    message_text = " ".join(context.args[2:]).strip()

    if top_n is None:
        recipients = [(uid, name) for uid, name, username, coins in await asyncio.to_thread(db.get_all_users)]
        label = "all users"
    else:
        recipients = [(uid, name) for uid, name, coins in await asyncio.to_thread(db.get_global_leaderboard_with_ids, limit=top_n)]
        label = f"top {top_n}"

    if not recipients:
        await update.message.reply_text("No eligible users to share with.")
        return

    status = await update.message.reply_text(
        f"📣 Sharing {DOLLAR}{amount} with {label} ({len(recipients)} user{'s' if len(recipients) != 1 else ''})..."
    )

    delivered = 0
    failed = 0
    total_distributed = 0

    for uid, name in recipients:
        await asyncio.to_thread(db.award_global_coins, uid, name, amount)
        new_balance = await asyncio.to_thread(db.get_global_coins, uid)
        total_distributed += amount

        body = f"{DOLLAR}{amount} has been added to your account.\n\nYour new account balance is {DOLLAR}{new_balance}."
        text = f"{message_text}\n\n{body}" if message_text else body

        try:
            await context.bot.send_message(chat_id=uid, text=text)
            delivered += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await status.edit_text(
        f"✅ Share completed ({label}).\n\n"
        f"Amount: {DOLLAR}{amount}\n"
        f"Users credited: {len(recipients)}\n"
        f"Messages delivered: {delivered}\n"
        f"Messages failed: {failed}\n"
        f"Total distributed: {DOLLAR}{total_distributed}"
    )


@owner_only
async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addadmin <@username or Telegram ID>")
        return

    target_arg = context.args[0].lstrip("@")

    if target_arg.isdigit():
        row = await asyncio.to_thread(db.get_user_by_id, int(target_arg))
    else:
        row = await asyncio.to_thread(db.get_user_by_username, target_arg)

    if not row:
        await update.message.reply_text(
            "❌ That user hasn't interacted with me yet - ask them to send /start in my PM first."
        )
        return

    target_id, target_name, target_username, _ = row

    if is_owner(target_id):
        await update.message.reply_text("❌ The bot owner can't be added as an admin.")
        return

    if await asyncio.to_thread(db.is_bot_admin, target_id):
        await update.message.reply_text("⚠️ That user is already a bot admin.")
        return

    await asyncio.to_thread(db.add_bot_admin, target_id, target_name, target_username, update.effective_user.id)

    display = f"@{target_username}" if target_username else target_name
    await update.message.reply_text(
        f"✅ {display} (ID: {target_id}) has been added as a Bot Admin."
    )


@owner_only
async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removeadmin <@username or Telegram ID>")
        return

    target_arg = context.args[0].lstrip("@")

    if target_arg.isdigit():
        target_id = int(target_arg)
        row = await asyncio.to_thread(db.get_user_by_id, target_id)
    else:
        row = await asyncio.to_thread(db.get_user_by_username, target_arg)
        target_id = row[0] if row else None

    if target_id is None or not await asyncio.to_thread(db.is_bot_admin, target_id):
        await update.message.reply_text("❌ That user is not currently a Bot Admin.")
        return

    await asyncio.to_thread(db.remove_bot_admin, target_id)
    display = f"@{row[2]}" if row and row[2] else (row[1] if row else target_id)
    await update.message.reply_text(f"✅ Bot Admin privileges revoked for {display} (ID: {target_id}).")


@owner_only
async def admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.get_bot_admins)

    if not rows:
        await update.message.reply_text("👮 There are currently no Bot Admins.")
        return

    lines = [f"👮 <b>Bot Admins</b> ({len(rows)})\n"]
    for uid, name, username, added_at in rows:
        uname = f"@{username}" if username else "—"
        lines.append(
            f"• {truncate_name(name or 'Unknown')} ({uname})\n"
            f"  ID: <code>{uid}</code>"
        )

    await update.message.reply_html("\n".join(lines))


@owner_only
async def allcommands_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"""
📖 <b>All Bot Commands</b>

<b>👥 Public Commands</b>
/start - open the main menu
/help - how to play
/leaderboard - this chat's leaderboard (with a Global switch)
/chattop - top players in this chat
/globaltop - top players across all chats
/topplayers - top 15 players, this chat or global
/topgroups - the 10 most active groups

<b>🎮 Group Commands</b>
/wordgame - start a round (admins only once a round is live)
/hint - see the current leader's clues so far (numbered, clickable)
/stopvote - vote to stop a stuck round (3 votes needed)

<b>💬 Private Message Commands</b>
/settings - toggle auto-lead
/autolead - same as /settings

<b>🛡️ Group Admin Commands</b>
/stopgame - group admins (or the owner) can stop a round instantly

<b>👑 Owner Commands</b>
/stats - bot-wide statistics
/botgroups - total/current/removed groups, tappable, with per-group Word Game and Scramble activity
/groups [query] - list/search every group the bot has seen
/users [query] - list/search every registered user
/banuser &lt;ID&gt; - ban a user from the bot
/unbanuser &lt;ID&gt; - unban a user
/bangroup &lt;ID&gt; - ban a group (bot leaves immediately)
/unbangroup &lt;ID&gt; - unban a group
/broadcast users|groups &lt;message&gt; - mass message everyone
/maintenance on|off - lock the bot to owner/admins only
/setreward win|cheat|daily &lt;amount&gt; - tune reward amounts
/reloadwords - reload the word pool from disk
/addwords word1, word2, ... - add new words to the pool (never deletes existing ones)
/forceend [Group ID] - force-end a stuck round
/forceleader &lt;@user or ID&gt; - assign a leader directly
/topup &lt;@user or ID&gt; &lt;+/-amount&gt; - adjust a player's balance
/share allusers +&lt;amount&gt; [message] - add balance to EVERY registered user
/share top&lt;N&gt; +&lt;amount&gt; [message] - add balance to the top N players on the global leaderboard
/addadmin &lt;@user or ID&gt; - appoint a Bot Admin
/removeadmin &lt;@user or ID&gt; - revoke a Bot Admin
/admins - list current Bot Admins
/allcommands - this list
/claimeddailyreward [YYYY-MM-DD] - who claimed the daily reward
/addadmanager &lt;@username&gt; - assign the Advertisement Manager
/changeadmanager &lt;@username&gt; - replace the Advertisement Manager
/removeadmanager - clear the Advertisement Manager (hides the ad section)
/admanager - show the current Advertisement Manager
/ad on|off - owner only: globally show/hide the advertisement section
/sponsoredad on|off - owner only: show/hide just the Sponsored Ad line
/changesponsoredad &lt;text, link, @username, or 'label | link'&gt; - owner only: replace the Sponsored Ad content (or 'clear' to reset)

<i>Note: banuser/unbanuser/bangroup/unbangroup/broadcast/maintenance/addadmanager/changeadmanager/removeadmanager/admanager can also be used by Bot Admins. /ad on|off, /botgroups, and /addwords are owner-only. Word Scramble has its own command list - see /scrambleallcommands (includes /scramblestopallgroups). The 🎁 Daily Reward always credits the Word Game balance, and every player's Balance/Level/Rank on /start combines their Word Game + Word Scramble balances automatically.</i>
"""
    await update.message.reply_html(text)


@owner_only
async def claimeddailyreward_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        date_str = context.args[0]
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("❌ Invalid date format. Use YYYY-MM-DD, e.g. 2026-08-05.")
            return
    else:
        date_str = datetime.now().strftime("%Y-%m-%d")

    rows = await asyncio.to_thread(db.get_daily_claims, date_str)

    if not rows:
        await update.message.reply_text(f"📋 No daily reward claims recorded for {date_str}.")
        return

    lines = [f"🎁 <b>Daily Reward Claims ({date_str})</b>\n"]

    for i, (uid, name, username, amount, claimed_at) in enumerate(rows, start=1):
        display = f"@{username}" if username else truncate_name(name or "Unknown")
        try:
            time_display = datetime.fromisoformat(claimed_at).strftime("%I:%M %p")
        except ValueError:
            time_display = claimed_at

        lines.append(
            f"{i}. {display}\n"
            f"ID: <code>{uid}</code>\n"
            f"Claimed: {DOLLAR}{amount}\n"
            f"Time: {time_display}\n"
        )

    total_players, total_amount = await asyncio.to_thread(db.get_daily_totals, date_str)
    lines.append(f"👥 Total Players Who Claimed Today: <b>{total_players}</b>")
    lines.append(f"{DOLLAR} Total Distributed Today: <b>{total_amount}</b>")

    await update.message.reply_html("\n".join(lines))


def main():
    print("GAME BOT: main() started", flush=True)

    db.create_tables()
    print("GAME BOT: database tables created", flush=True)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(20)
        .read_timeout(20)
        .write_timeout(20)
        .pool_timeout(20)
        .build()
    )

    print("GAME BOT: application built", flush=True)

    app.add_error_handler(error_handler)

    app.add_handler(TypeHandler(Update, pre_process), group=-1)
    app.add_handler(ChatMemberHandler(on_bot_membership_change, ChatMemberHandler.MY_CHAT_MEMBER))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("autolead", autolead_command))
    app.add_handler(CommandHandler("claimdailyreward", claimdailyreward_command))
    app.add_handler(CommandHandler("wordgame", word_game))
    app.add_handler(CommandHandler("wordscramble", wordscramble_command))
    app.add_handler(CommandHandler("scramblehelp", scramblehelp_command))
    app.add_handler(CommandHandler("scrambleleaderboard", scrambleleaderboard_command))
    app.add_handler(CommandHandler("scrambletopplayers", scrambletopplayers_command))
    app.add_handler(CommandHandler("scrambletopgroups", scrambletopgroups_command))
    app.add_handler(CommandHandler("scramblestopgame", scramblestopgame_command))
    app.add_handler(CommandHandler("scramblestats", scramblestats_command))
    app.add_handler(CommandHandler("scramblesetreward", scramblesetreward_command))
    app.add_handler(CommandHandler("scramblereloadwords", scramblereloadwords_command))
    app.add_handler(CommandHandler("scrambleforceend", scrambleforceend_command))
    app.add_handler(CommandHandler("scramblestopallgroups", scramblestopallgroups_command))
    app.add_handler(CommandHandler("scramblemaintenance", scramblemaintenance_command))
    app.add_handler(CommandHandler("scrambleallcommands", scrambleallcommands_command))
    app.add_handler(CommandHandler("slowmode", slowmode_command))
    app.add_handler(CommandHandler("scramblemode", scramblemode_command))
    app.add_handler(CommandHandler("scrambletopup", scrambletopup_command))
    app.add_handler(CommandHandler("scrambletopupspecific", scrambletopupspecific_command))
    app.add_handler(CommandHandler("scrambleautomaticmode", scrambleautomaticmode_command))
    app.add_handler(CommandHandler("scrambleusers", scrambleusers_command))
    app.add_handler(CommandHandler("hint", hint_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("chattop", chat_top))
    app.add_handler(CommandHandler("globaltop", global_top))
    app.add_handler(CommandHandler("topgroups", topgroups_command))
    app.add_handler(CommandHandler("topplayers", topplayers_command))
    app.add_handler(CommandHandler("stopvote", stopvote_command))
    app.add_handler(CommandHandler("stopgame", stopgame_command))

    # --- Owner admin panel -------------------------------------------
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("botgroups", botgroups_command))
    app.add_handler(CommandHandler("botsgroups", botgroups_command))
    app.add_handler(CommandHandler("groups", groups_command))
    app.add_handler(CommandHandler("prompt", prompt_command))
    app.add_handler(CommandHandler("editprompt", editprompt_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("banuser", banuser_command))
    app.add_handler(CommandHandler("unbanuser", unbanuser_command))
    app.add_handler(CommandHandler("bangroup", bangroup_command))
    app.add_handler(CommandHandler("unbangroup", unbangroup_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("maintenance", maintenance_command))
    app.add_handler(CommandHandler("setreward", setreward_command))
    app.add_handler(CommandHandler("reloadwords", reloadwords_command))
    app.add_handler(CommandHandler("addwords", addwords_command))
    app.add_handler(CommandHandler("forceend", forceend_command))
    app.add_handler(CommandHandler("forceleader", forceleader_command))
    app.add_handler(CommandHandler("topup", topup_command))
    app.add_handler(CommandHandler("share", share_command))
    app.add_handler(CommandHandler("addadmin", addadmin_command))
    app.add_handler(CommandHandler("removeadmin", removeadmin_command))
    app.add_handler(CommandHandler("admins", admins_command))
    app.add_handler(CommandHandler("allcommands", allcommands_command))
    app.add_handler(CommandHandler("claimeddailyreward", claimeddailyreward_command))
    app.add_handler(CommandHandler("addadmanager", addadmanager_command))
    app.add_handler(CommandHandler("changeadmanager", changeadmanager_command))
    app.add_handler(CommandHandler("removeadmanager", removeadmanager_command))
    app.add_handler(CommandHandler("admanager", admanager_command))
    app.add_handler(CommandHandler("ad", ad_command))
    app.add_handler(CommandHandler("sponsoredad", sponsoredad_command))
    app.add_handler(CommandHandler("changesponsoredad", changesponsoredad_command))

    app.add_handler(CallbackQueryHandler(button))

    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        private_text,
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
        group_text,
    ))

    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        scramble_private_text,
    ), group=1)
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
        scramble_group_text,
    ), group=1)

    if app.job_queue is not None:
        restore_stale_scramble_rounds(app.job_queue)
        restore_stale_slowmode_waits(app.job_queue)
        restore_stale_word_games(app.job_queue)

        if db.get_config("scramble_mode_enabled", "0") == "1":
            try:
                interval = int(db.get_config("scramble_mode_interval_seconds", "3600"))
            except ValueError:
                interval = 3600
            schedule_scramblemode_job(app.job_queue, interval)

    return app


if __name__ == "__main__":
    pass