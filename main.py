import os
import ast
import math
import random
import re
import asyncio
import functools
import time
import json
import logging
import importlib
import html as html_lib
import urllib.parse
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from collections import defaultdict

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji, LinkPreviewOptions, LabeledPrice
from telegram.constants import ChatType
from telegram.error import Forbidden, BadRequest, TimedOut, NetworkError, RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    TypeHandler,
    ContextTypes,
    ApplicationHandlerStop,
    PreCheckoutQueryHandler,
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

# --- Invite & Earn feature ---------------------------------------------
INVITE_REWARD_MIN_MEMBERS = 200
INVITE_REWARD_HOURS = 24
INVITE_REWARD_SECONDS = INVITE_REWARD_HOURS * 3600
DEFAULT_INVITE_REWARD_AMOUNT = 1000


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


async def get_invite_reward_amount() -> int:
    """Live invite_reward_amount config value (default 1000) - always read
    fresh at the moment it's needed for display or payout, never hardcoded
    or cached, per the Invite & Earn spec."""
    val = await asyncio.to_thread(db.get_config, "invite_reward_amount", str(DEFAULT_INVITE_REWARD_AMOUNT))
    try:
        return int(val)
    except (TypeError, ValueError):
        return DEFAULT_INVITE_REWARD_AMOUNT


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


# Matches emoji / pictograph characters (plus variation selectors and the
# zero-width joiner used to glue multi-codepoint emoji together) so they can
# be stripped from a player's live Telegram display name before it's shown
# anywhere - players can rename themselves with emoji at any time.
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"  # regional indicator symbols (flag emoji)
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport & map symbols
    "\U0001F700-\U0001F77F"  # alchemical symbols
    "\U0001F780-\U0001F7FF"  # geometric shapes extended
    "\U0001F800-\U0001F8FF"  # supplemental arrows-C
    "\U0001F900-\U0001F9FF"  # supplemental symbols & pictographs
    "\U0001FA00-\U0001FA6F"  # chess symbols, symbols & pictographs extended-A (1)
    "\U0001FA70-\U0001FAFF"  # symbols & pictographs extended-A (2)
    "\U00002600-\U000026FF"  # misc symbols
    "\U00002700-\U000027BF"  # dingbats
    "\U00002B00-\U00002BFF"  # misc symbols and arrows (includes ⭐ ⚡ etc.)
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0000200D"             # zero-width joiner
    "]+",
    flags=re.UNICODE,
)


def sanitize_display_name(name):
    """Strip emoji from a player's current Telegram display name before it's
    printed anywhere (leaderboards, /start, /profile, win/explain
    announcements, etc). Shared by every name-display call site so the
    stripping logic only lives in one place."""
    if not name:
        return name
    cleaned = _EMOJI_PATTERN.sub("", str(name))
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned if cleaned else str(name)


def esc_name(name) -> str:
    """HTML-escaped, emoji-stripped display name - use this instead of esc()
    for any player name shown in an HTML-parse_mode message."""
    return esc(sanitize_display_name(name))


def truncate_name(name: str) -> str:
    if name is None:
        return ""
    name = sanitize_display_name(name)
    if len(name) > MAX_LEADERBOARD_NAME_LEN:
        name = name[:MAX_LEADERBOARD_NAME_LEN] + "..."
    return esc(name)


# --- Shared medal (top-3 combined-balance) + level/rank badge helpers ------
# Both reuse db.get_combined_global_position()/db.compute_level_rank(), which
# are themselves built on db.get_combined_balance() - the SAME Word Game +
# Word Scramble combined balance /start already uses. This guarantees there
# is only ever one global #1/#2/#3, shared identically between Word Scramble
# and Word Game.

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


async def get_global_medal(user_id) -> str:
    """Returns 🥇/🥈/🥉 if this user is currently top-3 globally by combined
    balance, else ''. Does not check any /showmedal-style toggle itself."""
    pos = await asyncio.to_thread(db.get_combined_global_position, user_id)
    if not pos:
        return ""
    return MEDALS.get(pos[0], "")


async def medal_prefix_if_enabled(user_id, config_key) -> str:
    """Medal prefix for user_id, gated behind the given global on/off
    bot_config toggle (defaults to on)."""
    enabled = await asyncio.to_thread(db.get_config, config_key, "1")
    if enabled != "1":
        return ""
    return await get_global_medal(user_id)


async def level_badge_html(user_id) -> str:
    """' · <b>💎 Lvl 4</b>' when /showlevel is on, else '' (gated behind the
    show_level bot_config toggle, defaults to on)."""
    enabled = await asyncio.to_thread(db.get_config, "show_level", "1")
    if enabled != "1":
        return ""
    balance = await asyncio.to_thread(db.get_combined_balance, user_id)
    level, rank_name = db.compute_level_rank(balance)
    emoji = rank_name.split(" ", 1)[0] if rank_name else ""
    return f" · <b>{emoji} Lvl {level}</b>"


SAFE_SEND_MAX_FLOOD_WAITS = 6
SAFE_SEND_MAX_FLOOD_SLEEP = 90


async def safe_send(bot, chat_id, retries=2, delay=2, **kwargs):
    attempt = 0
    flood_waits = 0
    while True:
        try:
            return await bot.send_message(chat_id=chat_id, **kwargs)
        except RetryAfter as e:
            # Telegram flood control (roughly 20 messages/minute per group).
            # Automatic Mode sends 2 messages per round, so fast rounds hit
            # this limit after ~10 rounds. It used to escape as an uncaught
            # exception and silently kill the whole round chain; now we wait
            # exactly as long as Telegram asks and send again.
            flood_waits += 1
            if flood_waits > SAFE_SEND_MAX_FLOOD_WAITS:
                logger.error(f"send_message to {chat_id}: still rate-limited after {flood_waits - 1} waits, giving up")
                return None
            wait = getattr(e, "retry_after", 5)
            if hasattr(wait, "total_seconds"):
                wait = wait.total_seconds()
            wait = min(float(wait), SAFE_SEND_MAX_FLOOD_SLEEP) + 1
            logger.warning(f"send_message to {chat_id}: flood control, waiting {wait:.0f}s")
            await asyncio.sleep(wait)
        except (TimedOut, NetworkError) as e:
            if attempt >= retries:
                logger.error(f"send_message to {chat_id} failed after retries: {e}")
                return None
            attempt += 1
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


def format_numbered_hints(log: str, chat, message_ids: str) -> str:
    log_lines = log.split("\n")
    id_lines = message_ids.split("\n") if message_ids else []

    # Pair every log line with the message id at the SAME position first, and only
    # then drop blank hint lines - so removing blanks can't shift ids onto the
    # wrong hint. A missing/short/blank id (e.g. a round that was already under
    # way when per-hint ids were introduced) just means that hint gets no link.
    formatted = []
    number = 0
    for position, line in enumerate(log_lines):
        if not line.strip():
            continue
        number += 1

        raw_id = id_lines[position].strip() if position < len(id_lines) else ""
        message_id = int(raw_id) if raw_id.isdigit() else None
        link = build_message_link(chat, message_id)

        label = f'<a href="{link}">{number}</a>' if link else str(number)
        formatted.append(f"{label}. {esc(line)}")
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


# Words added via /addwords, and words hidden via /removewords - both
# database-backed (see database.py's custom_words / removed_words tables)
# since Render wipes local file changes on every redeploy/restart. Populated
# from the DB at startup and kept live-updated by their respective commands,
# same pattern as everything else that feeds _build_medium_words().
CUSTOM_WORDS_CACHE = []
REMOVED_WORDS_CACHE = []


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
        CUSTOM_WORDS_CACHE,
    ]
    seen = set()
    merged = []
    for lst in lists:
        for w in lst:
            key = w.lower()
            if key not in seen:
                seen.add(key)
                merged.append(w)
    if REMOVED_WORDS_CACHE:
        removed_lower = {w.lower() for w in REMOVED_WORDS_CACHE}
        merged = [w for w in merged if w.lower() not in removed_lower]
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


def _rebuild_word_pools():
    """Rebuilds MEDIUM_WORDS/WORDS/SCRAMBLE_WORDS from the current
    medium_words_raw.py categories, CUSTOM_WORDS_CACHE, and
    REMOVED_WORDS_CACHE. Callers that just changed CUSTOM_WORDS_CACHE or
    REMOVED_WORDS_CACHE (e.g. after a DB write) should call this right after
    so the live pools reflect it immediately, with no restart needed."""
    global MEDIUM_WORDS, WORDS, SCRAMBLE_WORDS
    MEDIUM_WORDS = _build_medium_words()
    WORDS = _merge_all_words()
    SCRAMBLE_WORDS = _build_scramble_words()
    _invalidate_difficulty_cache()


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
    """Kept only as dead-code documentation of the old (buggy) write path -
    no longer called. /addwords now writes to the custom_words table via
    db.add_custom_words() instead, since Render wipes this file on every
    redeploy/restart. _read_owner_added_words() above is still used once, at
    startup, to migrate any words already sitting in this file into the
    database."""
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

    global CUSTOM_WORDS_CACHE
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

    try:
        await asyncio.to_thread(db.add_custom_words, to_add)
    except Exception as e:
        logger.error(f"Failed to save new words to the database: {e}")
        await update.message.reply_text("❌ Failed to save the new words - database error.")
        return

    CUSTOM_WORDS_CACHE = await asyncio.to_thread(db.get_custom_words)
    _rebuild_word_pools()

    msg = f"✅ Successful. Added: {', '.join(to_add)}"
    if already_existing:
        msg += f"\n\n*These already existing words were not added*:\n{', '.join(already_existing)}"
    if invalid:
        msg += f"\n\n❌ Invalid (letters only, skipped): {', '.join(invalid)}"

    await update.message.reply_text(msg, parse_mode="Markdown")


@owner_only
async def removewords_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /removewords word1, word2, word3\n"
            "(comma or newline separated - single alphabetic words only)"
        )
        return

    raw = " ".join(context.args)
    candidates = [w.strip() for w in re.split(r"[,\n]", raw) if w.strip()]

    if not candidates:
        await update.message.reply_text("❌ No valid words provided.")
        return

    global CUSTOM_WORDS_CACHE, REMOVED_WORDS_CACHE
    pool_lower = {w.lower() for w in WORDS}
    seen_this_call = set()

    to_remove = []
    not_found = []
    invalid = []

    for w in candidates:
        cleaned = w.strip()
        if not re.fullmatch(r"[A-Za-z]+", cleaned):
            invalid.append(cleaned)
            continue
        normalized = cleaned.capitalize()
        key = normalized.lower()
        if key in seen_this_call:
            continue
        if key not in pool_lower:
            not_found.append(normalized)
            continue
        seen_this_call.add(key)
        to_remove.append(normalized)

    if not to_remove:
        if len(candidates) == 1 and not_found and not invalid:
            await update.message.reply_text(f"⚠️ {not_found[0]} isn't in the word pool.")
            return
        msg = "⚠️ No words were removed."
        if not_found:
            msg += f"\n\n*Not found in the word pool*:\n{', '.join(not_found)}"
        if invalid:
            msg += f"\n\n❌ Invalid (letters only, skipped): {', '.join(invalid)}"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    try:
        await asyncio.to_thread(db.add_removed_words, to_remove)
    except Exception as e:
        logger.error(f"Failed to save removed words to the database: {e}")
        await update.message.reply_text("❌ Failed to remove the words - database error.")
        return

    REMOVED_WORDS_CACHE = await asyncio.to_thread(db.get_removed_words)
    CUSTOM_WORDS_CACHE = await asyncio.to_thread(db.get_custom_words)
    _rebuild_word_pools()

    msg = f"✅ Successful. Removed: {', '.join(to_remove)}"
    if not_found:
        msg += f"\n\n*Not found in the word pool*:\n{', '.join(not_found)}"
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

    difficulty = db.get_effective_game_difficulty(chat_id)
    pool = _get_difficulty_pool("scramble", SCRAMBLE_WORDS, difficulty)
    if not pool:
        return None

    used = db.scramble_get_used_words(chat_id)
    choices = [w for w in pool if w not in used]

    if not choices:
        db.scramble_reset_used_words(chat_id)
        choices = list(pool)

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


# chat_id -> time.monotonic() of the last time a round started/ended there.
# Used only by the watchdog so it never races a round change that is still
# in flight.
_scramble_last_activity = {}
_scramble_start_locks = {}
_PROCESS_BOOT_MONOTONIC = time.monotonic()


def _scramble_touch(chat_id):
    _scramble_last_activity[chat_id] = time.monotonic()


def _scramble_start_lock(chat_id):
    lock = _scramble_start_locks.get(chat_id)
    if lock is None:
        lock = _scramble_start_locks[chat_id] = asyncio.Lock()
    return lock


async def scramble_timeout_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.chat_id

    try:
        word = await asyncio.to_thread(db.scramble_claim_round, chat_id)
    except Exception:
        # Couldn't even reach the database: the round is still live, so try
        # the timeout again shortly instead of leaving it with no timer.
        logger.exception(f"scramble timeout: claim failed for {chat_id}; retrying in 5s")
        if context.job_queue is not None:
            context.job_queue.run_once(
                scramble_timeout_callback, 5, chat_id=chat_id,
                name=_scramble_timeout_job_name(chat_id),
            )
        return
    if not word:
        return

    _scramble_touch(chat_id)
    try:
        await _scramble_timeout_after_claim(context, chat_id, word)
    except Exception:
        logger.exception(f"scramble timeout: post-claim step failed for {chat_id}; recovering")
        await _scramble_recover_chain(context, chat_id, won=False)


async def _scramble_recover_chain(context, chat_id, won=False, chat_obj=None):
    """The round was already closed but something failed before the next
    round was queued. Make sure the chat is never left stuck."""
    try:
        slow = await asyncio.to_thread(db.get_scramble_slowmode, chat_id)
        if slow and slow[1]:
            if context.job_queue is not None and not context.job_queue.get_jobs_by_name(_slowmode_next_job_name(chat_id)):
                await schedule_slowmode_next_round(context.job_queue, chat_id, slow[0])
            return
        await _scramble_auto_continue(context, chat_id, won=won, chat_obj=chat_obj)
    except Exception:
        logger.exception(f"scramble recover chain failed for {chat_id}")
        try:
            schedule_scramble_send_retry(context.job_queue, chat_id, "auto")
        except Exception:
            pass


async def _scramble_timeout_after_claim(context, chat_id, word):
    slow = await asyncio.to_thread(db.get_scramble_slowmode, chat_id)
    slow_enabled = bool(slow and slow[1])

    text = (
        f"⏰ <b>Time's up!</b>\n\n"
        f"No one guessed it. The word was: <b>{esc(word)}</b>"
    )

    if slow_enabled:
        interval_seconds = slow[0]
        text += f"\n\n🐢New word will be sent in {_format_duration_words(interval_seconds)}."
        sent = await safe_send(context.bot, chat_id, text=text, parse_mode="HTML")
        round_on, _ = await asyncio.to_thread(db.get_autodelete_settings, chat_id)
        if round_on and sent:
            await _flush_round_messages(context, chat_id, keep_message_id=sent.message_id)
        await schedule_slowmode_next_round(context.job_queue, chat_id, interval_seconds)
        return

    _, auto_enabled, _ = await asyncio.to_thread(db.scramble_get_auto_mode, chat_id)
    if not auto_enabled:
        text += "\n\nPlay again with /wordscramble"

    sent = await safe_send(context.bot, chat_id, text=text, parse_mode="HTML")
    round_on, _ = await asyncio.to_thread(db.get_autodelete_settings, chat_id)
    if round_on and sent:
        await _flush_round_messages(context, chat_id, keep_message_id=sent.message_id)

    await _scramble_auto_continue(context, chat_id, won=False)


SCRAMBLE_SEND_RETRY_SECONDS = 8
SCRAMBLE_SEND_RETRY_MAX_ATTEMPTS = 60


def _scramble_send_retry_job_name(chat_id):
    return f"scramble_send_retry_{chat_id}"


def schedule_scramble_send_retry(job_queue, chat_id, kind, attempt=1):
    """Short retry after a round's word message failed to send, so the chat
    isn't left stuck for a whole 2-minute cycle. kind: 'auto' (Automatic
    Mode) or 'global' (owner's /scramblemode)."""
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(_scramble_send_retry_job_name(chat_id)):
        job.schedule_removal()
    job_queue.run_once(
        scramble_send_retry_callback,
        SCRAMBLE_SEND_RETRY_SECONDS,
        chat_id=chat_id,
        name=_scramble_send_retry_job_name(chat_id),
        data={"kind": kind, "attempt": attempt},
    )


async def scramble_send_retry_callback(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    data = context.job.data or {}
    kind = data.get("kind", "auto")
    attempt = data.get("attempt", 1)

    if kind == "auto":
        _, auto_enabled, _ = await asyncio.to_thread(db.scramble_get_auto_mode, chat_id)
        if not auto_enabled:
            return
    else:
        if await asyncio.to_thread(db.get_config, "scramble_mode_enabled", "0") != "1":
            return

    def _again():
        if attempt >= SCRAMBLE_SEND_RETRY_MAX_ATTEMPTS:
            logger.error(f"scramble send retry: giving up on chat {chat_id} after {attempt} attempts")
            return
        schedule_scramble_send_retry(context.job_queue, chat_id, kind, attempt + 1)

    try:
        chat = await context.bot.get_chat(chat_id)
    except Exception:
        _again()
        return

    try:
        result = await start_scramble_round(context, chat)
    except (Forbidden,) as e:
        logger.error(f"scramble send retry: bot can't post in {chat_id} ({e}); turning Automatic Mode off there")
        if kind == "auto":
            try:
                await asyncio.to_thread(db.scramble_set_auto_enabled, chat_id, False)
            except Exception:
                pass
        return
    except Exception as e:
        logger.error(f"scramble send retry failed for chat {chat_id}: {e}")
        _again()
        return

    if result == "send_failed":
        logger.warning(f"scramble send retry: still can't reach chat {chat_id} (attempt {attempt})")
        _again()
    elif result == "tournament" and kind == "auto":
        _again()
    elif result is True and kind == "global":
        await asyncio.to_thread(db.scramble_reset_manual_streak, chat_id)


async def _scramble_auto_continue(context: ContextTypes.DEFAULT_TYPE, chat_id, won: bool, chat_obj=None):
    """Never raises. Any failure while queuing the next round (stale DB
    connection, Telegram hiccup, flood control...) used to end the chain for
    good, leaving Automatic Mode 'on' but idle until someone typed
    /wordscramble. Now a failure just schedules a short retry."""
    try:
        await _scramble_auto_continue_inner(context, chat_id, won, chat_obj)
    except Exception:
        logger.exception(f"Automatic Mode: continue step failed for {chat_id}; scheduling retry")
        try:
            schedule_scramble_send_retry(context.job_queue, chat_id, "auto")
        except Exception:
            logger.exception("could not schedule Automatic Mode retry")


async def _scramble_auto_continue_inner(context: ContextTypes.DEFAULT_TYPE, chat_id, won: bool, chat_obj=None):
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
            # Transient lookup failure: retry instead of silently stopping.
            schedule_scramble_send_retry(context.job_queue, chat_id, "auto")
            return

    result = await start_scramble_round(context, chat)
    if result == "send_failed":
        logger.warning(f"Automatic Mode: couldn't send next round to {chat_id}; retrying in {SCRAMBLE_SEND_RETRY_SECONDS}s")
        schedule_scramble_send_retry(context.job_queue, chat_id, "auto")
    elif result == "tournament":
        # A tournament took over this chat - keep polling until it's over.
        schedule_scramble_send_retry(context.job_queue, chat_id, "auto")


async def start_scramble_round(context: ContextTypes.DEFAULT_TYPE, chat):
    # One start at a time per chat, so the normal chain, the retry job and
    # the watchdog can never double-post a round.
    async with _scramble_start_lock(chat.id):
        result = await _start_scramble_round_locked(context, chat)
    if result is True:
        _scramble_touch(chat.id)
    return result


async def _start_scramble_round_locked(context: ContextTypes.DEFAULT_TYPE, chat):
    maintenance = await asyncio.to_thread(db.get_config, "scramble_maintenance_mode", "0")
    if maintenance == "1":
        return "maintenance"

    if chat.type != ChatType.PRIVATE and await asyncio.to_thread(db.tournament_get_open_for_chat, chat.id):
        return "tournament"

    if await asyncio.to_thread(db.scramble_get_round, chat.id):
        return False

    word = await asyncio.to_thread(scramble_random_word, chat.id)
    if not word:
        return None

    scrambled = scramble_letters(word)
    reward_now = await asyncio.to_thread(get_scramble_reward)
    await asyncio.to_thread(db.scramble_start_round, chat.id, word, scrambled)

    if chat.type != ChatType.PRIVATE:
        await asyncio.to_thread(db.scramble_register_group, chat.id, chat.title or str(chat.id))

    try:
        sent = await safe_send(
            context.bot,
            chat.id,
            text=(
                f"🎮 <b>WORD SCRAMBLE</b> 🎮\n\n"
                f"Unscramble this word:\n\n"
                f"<b>{esc(scrambled)}</b>\n\n"
                f"First to type the correct word in the next 2 minutes wins {DOLLAR}{reward_now}"
            ),
            parse_mode="HTML",
        )
    except Exception:
        # Non-transient send error (e.g. bot removed / forbidden): never leave
        # a phantom active round behind. Roll back, then let the caller's
        # existing error handling see the exception exactly as before.
        await asyncio.to_thread(db.scramble_claim_round, chat.id)
        raise

    if sent is None:
        # The group never received the word - roll the round back so the DB
        # (and the timeout job) don't believe a round is live.
        await asyncio.to_thread(db.scramble_claim_round, chat.id)
        logger.error(f"start_scramble_round: word message not delivered to {chat.id}; round rolled back")
        return "send_failed"

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

    if await asyncio.to_thread(db.get_effective_admin_mode, chat.id):
        if not await _bot_is_admin_in_chat(context, chat.id):
            await update.message.reply_text(
                "🙏 To start a round here, I'll need to be a group admin - that way I "
                "can manage rounds and announce winners properly. Please make me an admin, "
                "then just run this command again!"
            )
            return

    _open_t = await asyncio.to_thread(db.tournament_get_open_for_chat, chat.id)
    if _open_t:
        if _open_t["status"] == "paused":
            await update.message.reply_text(TOURNAMENT_PAUSED_BLOCK_TEXT)
        else:
            await update.message.reply_text(
                "🏟️ A tournament is in progress here - Word Scramble rounds are paused until it ends."
            )
        return

    user = update.effective_user
    await asyncio.to_thread(db.scramble_increment_played, user.id, chat.id, user.first_name)

    if await asyncio.to_thread(_slowmode_enabled, chat.id) and not await asyncio.to_thread(db.scramble_get_round, chat.id):
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
    elif result == "send_failed":
        await update.message.reply_text(
            "⚠️ Couldn't reach the chat right now — try /wordscramble again in a moment."
        )
    elif result == "tournament":
        await update.message.reply_text(
            "🏟️ A tournament is in progress here - Word Scramble rounds are paused until it ends."
        )


async def _scramble_handle_win(context: ContextTypes.DEFAULT_TYPE, chat, user, message, guessed_word_case):
    word = await asyncio.to_thread(db.scramble_claim_round, chat.id)
    if not word:
        return

    cancel_scramble_timeout(context, chat.id)
    _scramble_touch(chat.id)

    try:
        await _scramble_handle_win_after_claim(context, chat, user, message, word)
    except Exception:
        logger.exception(f"scramble win: post-claim step failed in {chat.id}; recovering")
        await _scramble_recover_chain(context, chat.id, won=True, chat_obj=chat)


async def _scramble_handle_win_after_claim(context: ContextTypes.DEFAULT_TYPE, chat, user, message, word):
    await safe_react(context.bot, chat.id, message.message_id, "🎉")

    reward = await asyncio.to_thread(get_scramble_reward)
    winner_is_premium = await asyncio.to_thread(is_premium, user.id)
    if winner_is_premium:
        reward *= 2

    await asyncio.to_thread(db.scramble_reset_all_streaks_in_chat, chat.id, user.id)
    new_streak = await asyncio.to_thread(db.scramble_record_win, user.id, chat.id, user.first_name, reward)

    bonus = SCRAMBLE_STREAK_MILESTONES.get(new_streak)
    if bonus and winner_is_premium:
        bonus *= 2
    total = reward
    if bonus:
        await asyncio.to_thread(db.scramble_add_bonus, user.id, chat.id, user.first_name, bonus)
        total += bonus

    medal = await medal_prefix_if_enabled(user.id, "show_scramble_medal")
    badge = await level_badge_html(user.id)
    name_html = f'<b><a href="tg://user?id={user.id}">{esc_name(user.first_name)}</a></b>'

    lines = [
        "🎉 Winner!",
        f"{medal}{name_html}{badge}",
        f"Got it, <b>{esc(word)}</b>! +{DOLLAR}{reward}",
    ]
    if bonus:
        lines.append(f"🔥 {new_streak}-win streak! Bonus: +{DOLLAR}{bonus}")
        lines.append(f"Total: {DOLLAR}{total}")

    slow = await asyncio.to_thread(db.get_scramble_slowmode, chat.id)
    slow_enabled = bool(slow and slow[1])

    if slow_enabled:
        interval_seconds = slow[0]
        lines.append(f"🐢New word will be sent in {_format_duration_words(interval_seconds)}.")
        sent = await safe_send(context.bot, chat.id, text="\n".join(lines), parse_mode="HTML")
        round_on, _ = await asyncio.to_thread(db.get_autodelete_settings, chat.id)
        if round_on and sent:
            await _flush_round_messages(context, chat.id, keep_message_id=sent.message_id)
        await schedule_slowmode_next_round(context.job_queue, chat.id, interval_seconds)
        return

    _, auto_enabled, _ = await asyncio.to_thread(db.scramble_get_auto_mode, chat.id)

    if auto_enabled:
        sent = await safe_send(context.bot, chat.id, text="\n".join(lines), parse_mode="HTML")
        round_on, _ = await asyncio.to_thread(db.get_autodelete_settings, chat.id)
        if round_on and sent:
            await _flush_round_messages(context, chat.id, keep_message_id=sent.message_id)
        await _scramble_auto_continue(context, chat.id, won=True, chat_obj=chat)
        return

    lines.append("")
    lines.append("Play again with /wordscramble")
    sent = await safe_send(context.bot, chat.id, text="\n".join(lines), parse_mode="HTML")
    round_on, _ = await asyncio.to_thread(db.get_autodelete_settings, chat.id)
    if round_on and sent:
        await _flush_round_messages(context, chat.id, keep_message_id=sent.message_id)


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
    _premium_price, _premium_dur = get_premium_settings()
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

⭐ <b>Premium</b>: /buypremium ({_premium_price} ⭐ for {_format_duration_words(_premium_dur)}) doubles your {DOLLAR} rewards here (streak bonuses too). Check /premiumstatus.
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

    open_tournament = await asyncio.to_thread(db.tournament_get_open_for_chat, chat.id)
    if open_tournament:
        if not await asyncio.to_thread(is_bot_admin_or_owner, update.effective_user.id):
            await update.message.reply_text(
                "🏟️ A tournament is running here - only the bot owner or bot admins can stop it."
            )
            return
        await _tournament_abort_and_announce(
            context, open_tournament["id"], chat.id,
            "Tournament stopped by an admin.",
        )
        return

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
async def gamedifficulty_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    valid = ("easy", "easy3", "easy4", "easy5", "easy6", "medium", "hard", "off")

    if not context.args or context.args[0].lower() not in valid:
        current_global = await asyncio.to_thread(db.get_config, "game_difficulty_global", "off")
        await update.message.reply_html(
            "Usage:\n"
            "<code>/gamedifficulty easy</code> — mix of 3/4/5/6-letter words\n"
            "<code>/gamedifficulty easy3</code> (or easy4/easy5/easy6) — that exact length only\n"
            "<code>/gamedifficulty medium</code> — words of 8+ letters, compounds excluded\n"
            "<code>/gamedifficulty hard</code> — words of 11+ letters, compounds included\n"
            "<code>/gamedifficulty off</code> — use the full word pool\n\n"
            "Add a group ID or @username at the end to target just that group.\n\n"
            f"Current global default: <b>{esc(current_global)}</b>"
        )
        return

    label = context.args[0].lower()
    group_arg = context.args[1] if len(context.args) > 1 else None

    if group_arg:
        resolved = await resolve_group_arg(group_arg)
        if not resolved:
            await update.message.reply_html(
                f"❌ No group found matching <code>{esc(group_arg)}</code> that I'm currently in."
            )
            return
        target_chat_id, target_title = resolved
        await asyncio.to_thread(db.set_group_game_difficulty_override, target_chat_id, label)
        await update.message.reply_html(
            f"✅ Difficulty for <b>{esc(target_title)}</b> set to <b>{label}</b>."
        )
    else:
        await asyncio.to_thread(db.set_config, "game_difficulty_global", label)
        await update.message.reply_html(f"✅ Global default difficulty set to <b>{label}</b>.")


@owner_only
async def scramblereloadwords_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SCRAMBLE_WORDS, CUSTOM_WORDS_CACHE, REMOVED_WORDS_CACHE
    importlib.reload(mw)
    CUSTOM_WORDS_CACHE = await asyncio.to_thread(db.get_custom_words)
    REMOVED_WORDS_CACHE = await asyncio.to_thread(db.get_removed_words)
    SCRAMBLE_WORDS = _build_scramble_words()
    _invalidate_difficulty_cache()
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

    tournaments_stopped = 0
    for t in await asyncio.to_thread(db.tournament_get_all_open):
        try:
            res = await _tournament_abort_and_announce(
                context, t["id"], t["chat_id"],
                "Tournament stopped - Word Scramble was stopped everywhere.",
            )
            if res:
                tournaments_stopped += 1
        except Exception as e:
            logger.error(f"stopallgroups: failed to abort tournament {t['id']}: {e}")

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
        f"🛑 Word Scramble stopped in {stopped} group(s)/chat(s)."
        + (f" {tournaments_stopped} tournament(s) cancelled and refunded." if tournaments_stopped else "")
        + "\n\n"
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


async def reply_html_in_parts(message, text, limit=3800):
    """Telegram rejects any message over 4096 characters. Split a long HTML text
    into several messages, breaking only between blank-line-separated blocks
    (or, if one block is itself too long, between its lines) so no section
    heading gets separated from its entries and no <b>/<i> tag is cut in half."""
    blocks = [b for b in text.strip("\n").split("\n\n") if b.strip()]

    pieces = []
    for block in blocks:
        if len(block) <= limit:
            pieces.append(block)
            continue
        current = ""
        for line in block.split("\n"):
            if current and len(current) + 1 + len(line) > limit:
                pieces.append(current)
                current = line
            else:
                current = (current + "\n" + line) if current else line
        if current:
            pieces.append(current)

    parts, current = [], ""
    for piece in pieces:
        if current and len(current) + 2 + len(piece) > limit:
            parts.append(current)
            current = piece
        else:
            current = (current + "\n\n" + piece) if current else piece
    if current:
        parts.append(current)

    for part in parts:
        await message.reply_html(part)


def _scrambleallcommands_text() -> str:
    return """
📖 <b>All Word Scramble Commands</b>

<b>👥 Player Commands</b>
/wordscramble - start a round (works in groups and DM)
/scramblehelp - how to play
/scrambleleaderboard - this chat's leaderboard
/ranks - the full rank/level ladder and what balance unlocks each one (shared with Word Game)
/scrambletopplayers - top 15, this chat or global
/scrambletopgroups - the most active groups
/inviteandearn - add me to a 200+ member group and keep me there 24 hours for a Word Scramble reward
/claim - manual fallback to claim the Invite &amp; Earn reward for a qualifying group that was never automatically tracked
/profile - view your own profile (balance, level, rank)
/buypremium - premium doubles your 💵 Word Scramble and Word Game rewards (paid with Telegram Stars; used in a group it moves you to my DM, and the invoice has a ➡️ Premium features button)
/premiumstatus - see whether premium is active and how much time is left
/buycash - premium only: buy 💵 cash with Telegram Stars; it increases your balance, level and rank (pack sizes and prices are shown live; used in a group it moves you to my DM)
/boostrank - premium only: same as /buycash - buy 💵 cash to boost your rank
/boostlevel - premium only: same as /buycash - buy 💵 cash to boost your level
/transfer &lt;@username|id&gt; &lt;amount&gt; - premium only: send 💵 from your Word Scramble balance to another player (or reply to their message with /transfer &lt;amount&gt;)
/request &lt;@username|id&gt; &lt;amount&gt; - premium only: ask another player for 💵; they get Yes/No buttons (or reply to their message with /request &lt;amount&gt;)
/settournament &lt;fee&gt; &lt;difficulty&gt; &lt;rounds&gt;rounds - host a Word Scramble tournament in a group (e.g. /settournament 2000 Medium 3rounds); everyone pays the entry fee from their Word Scramble balance and the winner takes the pot
/tournament - quick-start a tournament instantly: same as /settournament 5 easy 20rounds
/join - join the tournament lobby that's open in this group
/searchcommand &lt;text&gt; - search the command list by name or description (only shows commands you're allowed to see)
/fullhelp - a complete, paged guide to the bot: games, tournaments, premium, coins and more

<b>🛡️ Group Admin Commands</b>
/scramblestopgame - group admins (or the owner) can stop an active round instantly
/scrambleautomaticmode on|off - group admins (or the owner) can manually control Automatic Mode here (blocked while Slow Mode is on in that chat). The bot never turns this on by itself anymore.
/slowmode on &lt;time&gt;|off - group admins (or the owner) can control Slow Mode here; next round is sent exactly &lt;time&gt; after the previous one ends (e.g. /slowmode on 2m). Every round still lasts a maximum of 2 minutes, and turning this on also turns Automatic Mode off in this chat.
/deleterequestprompt off|on [&lt;1-9&gt;s|m] - group admins (or the owner) can control whether resolved /request prompts are deleted here and after how long (default on, 30 seconds; no args shows the current setting)

<b>👑 Owner Commands</b>
/scramblestats - Word Scramble bot-wide statistics
/scramblesetreward &lt;amount&gt; - set the win reward
/scramblereloadwords - reload the word pool from medium_words_raw.py
/shufflewords - reshuffle the shared word pool (Word Game and Word Scramble both draw from it)
/gamedifficulty easy|easy3-6|medium|hard|off [group] - control which word lengths get picked (shared with Word Game)
/scrambleforceend [Chat ID] - force-end a stuck round in one chat
/scramblestopallgroups - force-end the active round in EVERY chat at once and pause new ones (until /scramblemaintenance off)
/scramblemaintenance on|off - pause new Word Scramble rounds only (Word Game unaffected)
/scramblemode &lt;time&gt;|off - my DM only: auto-start a round in EVERY group on a repeating interval
/controlgroup automaticmode on|off &lt;group&gt; | slowmode on &lt;time&gt;|off &lt;group&gt; - owner only: remotely control Slow Mode / Automatic Mode in any group (&lt;group&gt; = ID, @username or allgroups)
/starttournament - tournament host, bot owner or Bot Admins: start the lobby early
/stoptournament - tournament host, bot owner or Bot Admins: cancel a lobby (or a paused tournament) and refund everyone
/resumetournament - tournament host, bot owner or Bot Admins: continue a paused or stuck tournament from where it stopped (owner in DM: /resumetournament &lt;group id|@username&gt; or /resumetournament allgroups)
/extendlobby &lt;2-9&gt;m - owner only: add minutes to the open tournament lobby in this group (e.g. /extendlobby 5m)
/tournamentban &lt;username|ID&gt; - owner/Bot Admins: ban a player (or reply to them) from tournaments only; also accepts a group username/ID to ban that group from running tournaments
/tournamentunban &lt;username|ID&gt; - owner/Bot Admins: undo a tournament ban (player or group)
/setpremium &lt;stars&gt; &lt;duration&gt; - owner only: set the premium price and length (no args shows current)
/giftpremium &lt;username|ID&gt; - Premium members only: gift Premium to another player (you pay the Stars, or reply to their message; used in a group it moves you to my DM). The owner gifts 7 days free
/setdollarpack &lt;1-5&gt; &lt;dollars&gt; &lt;stars&gt; - owner only: edit one /buycash pack (no args lists the current packs)
/editbuycashtext &lt;text&gt; - owner only, my DM: change the message above the /buycash, /boostrank and /boostlevel packs (send "reset" for the default; no args shows the current text)
/seepremiumplayers - owner only: every current premium player with time remaining
/starbalance - owner only: the bot's current Telegram Stars balance
/withdraw - owner only: Stars balance + how to withdraw it via @BotFather
/scrambletopup &lt;@user or ID&gt; &lt;+/-amount&gt; - adjust a player's Word Scramble balance only
/scrambletopupspecific &lt;@user or ID&gt; &lt;+/-amount&gt; &lt;group ID or @username&gt; - adjust a player's balance as if earned in that specific group (reflects in that group's "in this chat" total and Global)
/scrambleusers - every player who's used the command or guessed a word, tappable for details
/showlevel on|off - bot owner or Bot Admins: toggle the Level/Rank badge on Word Scramble winner messages (global, no args shows current status)
/showmedal on|off - bot owner or Bot Admins: toggle the 🥇🥈🥉 medal prefix on Word Scramble messages (global, no args shows current status)
/invitereward &lt;amount&gt; - owner only: set the Invite &amp; Earn reward amount (no args shows the current amount, default 💵1000)
/scrambleallcommands - this list

<i>Note: Word Scramble intentionally has no /scramblebanuser, /scramblebroadcast, /scrambleshare, or /scrambleadmins - banning, broadcasting, and bot-admin management are bot-wide systems shared with the Word Game, so /banuser, /broadcast, /share, and /addadmin already cover Word Scramble too. There's also no daily-reward feature specific to Word Scramble - the 🎁 Daily Reward always credits the Word Game balance, and every player's displayed Balance/Level/Rank on /start combines both games automatically. This list and /allcommands are visible to the bot owner, Bot Admins, and each group's own Telegram creator. The owner is alerted in DM whenever someone buys Premium, gifts Premium, buys 💵 cash or boosts their rank/level.</i>
"""


async def scrambleallcommands_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin_or_owner(update.effective_user.id) and not await is_chat_creator(update, context):
        await update.message.reply_text(
            "🚫 This command is for the bot owner or this group's creator."
        )
        return
    text = _scrambleallcommands_text()
    await reply_html_in_parts(update.message, text)


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
    result = await start_scramble_round(context, chat)
    if result == "send_failed":
        # Word never reached the group: retry shortly (same callback) instead
        # of waiting a whole slowmode interval; due-time is kept near-term.
        logger.warning(f"Slowmode: couldn't send round to {chat_id}; retrying in {SCRAMBLE_SEND_RETRY_SECONDS}s")
        await schedule_slowmode_next_round(context.job_queue, chat_id, SCRAMBLE_SEND_RETRY_SECONDS)
    elif result == "tournament":
        # Slow Mode stays on; check again once the tournament has had time to run.
        await schedule_slowmode_next_round(context.job_queue, chat_id, 60)


async def apply_slowmode_off(job_queue, chat_id):
    """Shared by /slowmode off and /controlgroup slowmode off."""
    await asyncio.to_thread(db.set_scramble_slowmode, chat_id, 0, False)
    cancel_slowmode_next_job(job_queue, chat_id)


async def apply_slowmode_on(job_queue, chat_id, seconds):
    """Shared by /slowmode on and /controlgroup slowmode on. Turning Slow Mode
    on always turns Automatic Mode off in that chat (never both at once)."""
    await asyncio.to_thread(db.set_scramble_slowmode, chat_id, seconds, True)
    await asyncio.to_thread(db.scramble_set_auto_enabled, chat_id, False)

    if not await asyncio.to_thread(db.scramble_get_round, chat_id):
        await schedule_slowmode_next_round(job_queue, chat_id, seconds)


async def apply_automode(chat_id, enabled: bool) -> bool:
    """Shared by /scrambleautomaticmode and /controlgroup automaticmode.
    Returns False (and changes nothing) if turning it ON while Slow Mode is on."""
    if enabled:
        slow = await asyncio.to_thread(db.get_scramble_slowmode, chat_id)
        if slow and slow[1]:
            return False
    await asyncio.to_thread(db.scramble_set_auto_enabled, chat_id, enabled)
    return True


async def slowmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text("🐢 Run /slowmode inside the group you want to apply it to.")
        return

    if not (is_owner(update.effective_user.id) or await is_chat_admin(update, context)):
        await update.message.reply_text("❌ Only group admins (or the bot owner) can use /slowmode.")
        return

    chat_id = update.effective_chat.id

    _open_t = await asyncio.to_thread(db.tournament_get_open_for_chat, chat_id) if context.args else None
    if _open_t:
        if _open_t["status"] == "paused":
            await update.message.reply_text(
                "🏟️ A tournament is paused here and can be resumed with /resumetournament - "
                "Slow Mode can't be changed until it ends or is cancelled."
            )
        else:
            await update.message.reply_text(
                "🏟️ A tournament is in progress here - Slow Mode can't be changed until it ends."
            )
        return

    if not context.args:
        current = await asyncio.to_thread(db.get_scramble_slowmode, chat_id)
        status = f"on, every {_format_duration_words(current[0])}" if current and current[1] else "off"
        await update.message.reply_text(
            f"Usage: /slowmode on <time>  (e.g. /slowmode on 2m)\n/slowmode off\n\nCurrently: {status}"
        )
        return

    mode = context.args[0].lower()

    if mode == "off":
        await apply_slowmode_off(context.job_queue, chat_id)
        await update.message.reply_text("🐢 Slowmode turned off.")
        return

    if mode != "on" or len(context.args) < 2:
        await update.message.reply_text("Usage: /slowmode on <time>  (e.g. /slowmode on 2m)\n/slowmode off")
        return

    seconds = _parse_duration(context.args[1])
    if seconds is None:
        await update.message.reply_text("❌ Invalid duration. Use e.g. 2m, 5m, 1h.")
        return

    await apply_slowmode_on(context.job_queue, chat_id, seconds)

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
            elif result == "send_failed":
                logger.warning(f"scramblemode tick: couldn't send round to {chat_id}; retrying shortly")
                schedule_scramble_send_retry(context.job_queue, chat_id, "global")
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

    _open_t = await asyncio.to_thread(db.tournament_get_open_for_chat, chat_id) if context.args else None
    if _open_t:
        if _open_t["status"] == "paused":
            await update.message.reply_text(
                "🏟️ A tournament is paused here and can be resumed with /resumetournament - "
                "Automatic Mode can't be changed until it ends or is cancelled."
            )
        else:
            await update.message.reply_text(
                "🏟️ A tournament is in progress here - Automatic Mode can't be changed until it ends."
            )
        return

    if not context.args or context.args[0].lower() not in ("on", "off"):
        _, enabled, _ = await asyncio.to_thread(db.scramble_get_auto_mode, chat_id)
        await update.message.reply_text(
            f"Usage: /scrambleautomaticmode on|off\nCurrently: {'on' if enabled else 'off'}"
        )
        return

    mode = context.args[0].lower()

    if not await apply_automode(chat_id, mode == "on"):
        await update.message.reply_text(
            "❌ Slow Mode is currently on in this chat - turn it off with /slowmode off "
            "before enabling Automatic Mode. The two must never run at the same time."
        )
        return

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


# --- Feature: word difficulty ------------------------------------------

_DIFFICULTY_CACHE = {}  # (pool_name, label) -> filtered word list


def _is_compound_word(word: str) -> bool:
    """Heuristic only: True if `word` splits into two dictionary words of
    3+ letters each (e.g. 'weightlifting' -> 'weight' + 'lifting'). Used to
    keep compounds out of the 'medium' difficulty tier. Reuses the same
    dictionary check the write-word validator already relies on."""
    w = word.lower()
    if len(w) < 6:
        return False
    for i in range(3, len(w) - 2):
        left, right = w[:i], w[i:]
        try:
            if _word_in_dictionary(left) and _word_in_dictionary(right):
                return True
        except Exception:
            return False
    return False


def _filter_by_difficulty(pool, label):
    if not label or label == "off":
        return pool
    if label == "easy":
        result = [w for w in pool if len(w) in (3, 4, 5, 6)]
    elif label in ("easy3", "easy4", "easy5", "easy6"):
        n = int(label[-1])
        result = [w for w in pool if len(w) == n]
    elif label == "medium":
        result = [w for w in pool if len(w) >= 8 and not _is_compound_word(w)]
    elif label == "hard":
        result = [w for w in pool if len(w) >= 11]
    else:
        result = pool
    return result or pool  # never hand back an empty pool


def _get_difficulty_pool(pool_name, base_pool, label):
    key = (pool_name, label or "off")
    if key not in _DIFFICULTY_CACHE:
        _DIFFICULTY_CACHE[key] = _filter_by_difficulty(base_pool, label)
    return _DIFFICULTY_CACHE[key]


def _invalidate_difficulty_cache():
    _DIFFICULTY_CACHE.clear()


def random_word(chat_id, exclude=None):
    difficulty = db.get_effective_game_difficulty(chat_id)
    pool = _get_difficulty_pool("wordgame", WORDS, difficulty)
    used = db.get_used_words(chat_id)

    choices = [w for w in pool if w not in used and w != exclude]

    if not choices:
        db.reset_used_words(chat_id)
        choices = [w for w in pool if w != exclude] or pool

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


# --- Feature: auto-delete -----------------------------------------------

_autodelete_tracker = defaultdict(list)  # chat_id -> [message_id, ...] pending possible deletion


def _parse_autodelete_interval(text):
    """'9m' -> 540, '30s' -> 30, etc. Accepts s/m/h/d, number 1-50. None if invalid."""
    if not text:
        return None
    m = re.fullmatch(r"([1-9]|[1-4][0-9]|50)([smhd])", text.strip().lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return n * mult


async def _flush_round_messages(context, chat_id, keep_message_id=None):
    """Deletes every tracked bot message for this chat except keep_message_id,
    then resets tracking to just that kept message (if any)."""
    ids = _autodelete_tracker.pop(chat_id, [])
    if not ids:
        return
    for mid in ids:
        if keep_message_id is not None and mid == keep_message_id:
            continue
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass
    if keep_message_id is not None:
        _autodelete_tracker[chat_id] = [keep_message_id]


def _autodelete_job_name(chat_id):
    return f"autodelete_{chat_id}"


def cancel_autodelete_job(job_queue, chat_id):
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(_autodelete_job_name(chat_id)):
        job.schedule_removal()


def schedule_autodelete_job(job_queue, chat_id, interval_seconds):
    if job_queue is None:
        return
    cancel_autodelete_job(job_queue, chat_id)
    job_queue.run_repeating(
        autodelete_interval_tick,
        interval=interval_seconds,
        first=interval_seconds,
        chat_id=chat_id,
        name=_autodelete_job_name(chat_id),
    )


async def autodelete_interval_tick(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    ids = _autodelete_tracker.get(chat_id, [])
    if len(ids) <= 1:
        return
    keep = ids[-1]
    for mid in ids[:-1]:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass
    _autodelete_tracker[chat_id] = [keep]


def restore_autodelete_jobs(job_queue):
    for chat_id, interval_seconds in db.get_all_autodelete_interval_groups():
        schedule_autodelete_job(job_queue, chat_id, interval_seconds)


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

    sent = await safe_send(
        context.bot,
        chat_id,
        text=(
            f"⏰ <b>{esc_name(leader_name)}</b>\n\n"
            f"Ran out of time to explain.\n\n"
            f"Who wants to explain?"
        ),
        parse_mode="HTML",
        reply_markup=volunteer_keyboard(),
    )
    round_on, _ = await asyncio.to_thread(db.get_autodelete_settings, chat_id)
    if round_on and sent:
        await _flush_round_messages(context, chat_id, keep_message_id=sent.message_id)


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


# --- Invite & Earn: job scheduling + payout processing ---------------------
# Mirrors the restore_stale_scramble_rounds/_slowmode_waits/_word_games and
# schedule_scramble_timeout/cancel_scramble_timeout patterns already used
# elsewhere in this file, for both scheduling style and restart recovery.

def _invite_reward_job_name(row_id):
    return f"invite_reward_payout_{row_id}"


def cancel_invite_reward_job(job_queue, row_id):
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(_invite_reward_job_name(row_id)):
        job.schedule_removal()


def schedule_invite_reward_payout(job_queue, row_id, delay_seconds):
    if job_queue is None:
        return
    cancel_invite_reward_job(job_queue, row_id)
    job_queue.run_once(
        invite_reward_payout_callback,
        max(delay_seconds, 1),
        data=row_id,
        name=_invite_reward_job_name(row_id),
    )


def restore_stale_invite_rewards(job_queue):
    """Re-reads every still-pending invite_rewards row on startup and either
    reschedules the remaining time until its 72-hour check, or - if that
    window already elapsed while the bot was down - runs the payout check
    almost immediately (mirrors restore_stale_scramble_rounds' `remaining=1`
    catch-up pattern)."""
    if job_queue is None:
        return

    now = datetime.now()

    for row_id, chat_id, added_at in db.get_all_pending_invite_reward_ids_with_time():
        remaining = INVITE_REWARD_SECONDS
        try:
            added_dt = datetime.fromisoformat(added_at)
            remaining = INVITE_REWARD_SECONDS - (now - added_dt).total_seconds()
        except (TypeError, ValueError):
            remaining = INVITE_REWARD_SECONDS

        if remaining <= 0:
            remaining = 1

        job_queue.run_once(
            invite_reward_payout_callback,
            remaining,
            data=row_id,
            name=_invite_reward_job_name(row_id),
        )


def build_invite_claim_message(inviter_id, inviter_name, group_title, amount, new_balance) -> str:
    mention = f'<b><a href="tg://user?id={inviter_id}">{esc_name(inviter_name)}</a></b>'
    return (
        f"🎉 {mention}! Your invite to <b>{esc(group_title)}</b> just paid off — "
        f"{DOLLAR}{amount} has been added. Your new balance: {DOLLAR}{new_balance}."
    )


async def send_invite_claim_message(context, inviter_id, inviter_name, group_title, amount, new_balance):
    text = build_invite_claim_message(inviter_id, inviter_name, group_title, amount, new_balance)
    try:
        await context.bot.send_message(chat_id=inviter_id, text=text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Invite & Earn: failed to DM claim message to {inviter_id}: {e}")


async def _run_invite_reward_payout(context: ContextTypes.DEFAULT_TYPE, row_id):
    """The actual payout check - shared by both the timed job and the
    startup catch-up pass. Re-verifies BOTH that the bot is still in the
    group AND that the member count is still ≥200 right now (never trusts
    the count captured at add-time)."""
    row = await asyncio.to_thread(db.get_invite_reward, row_id)
    if not row:
        return

    (_id, chat_id, group_title, inviter_id, inviter_name,
     _member_count_at_add, _added_at, status, _amount_paid, _rewarded_at) = row

    if status != "pending":
        return

    still_present = await asyncio.to_thread(db.is_group_currently_present, chat_id)
    if not still_present:
        await asyncio.to_thread(db.mark_invite_reward_failed_by_id, row_id)
        return

    try:
        member_count = await context.bot.get_chat_member_count(chat_id)
    except Exception as e:
        logger.warning(f"Invite & Earn: get_chat_member_count failed for {chat_id}: {e}")
        member_count = None

    if member_count is None or member_count < INVITE_REWARD_MIN_MEMBERS:
        await asyncio.to_thread(db.mark_invite_reward_failed_by_id, row_id)
        return

    amount = await get_invite_reward_amount()
    new_balance = await asyncio.to_thread(db.scramble_adjust_global_earned, inviter_id, inviter_name, amount)
    await asyncio.to_thread(db.mark_invite_reward_rewarded_by_id, row_id, amount)

    await send_invite_claim_message(context, inviter_id, inviter_name, group_title, amount, new_balance)


async def invite_reward_payout_callback(context: ContextTypes.DEFAULT_TYPE):
    row_id = context.job.data
    await _run_invite_reward_payout(context, row_id)


async def _retry_invite_attribution(context: ContextTypes.DEFAULT_TYPE):
    """One-shot retry for the /start invite_<id> relay arriving into the
    group before on_bot_membership_change had a chance to create the
    pending row it needs to attach to."""
    data = context.job.data
    chat_id = data["chat_id"]
    referred_id = data["referred_id"]

    row_id = await asyncio.to_thread(db.get_recent_pending_invite_reward_for_chat, chat_id)
    if not row_id:
        return

    referred_user = await asyncio.to_thread(db.get_user_by_id, referred_id)
    referred_name = referred_user[1] if referred_user else str(referred_id)
    await asyncio.to_thread(db.update_invite_reward_inviter, row_id, referred_id, referred_name)


# --- Invite & Earn: "Copy your invite link" button revert -------------------

def _invite_copy_revert_job_name(chat_id, message_id):
    return f"invite_copy_revert_{chat_id}_{message_id}"


def cancel_invite_copy_revert(job_queue, chat_id, message_id):
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(_invite_copy_revert_job_name(chat_id, message_id)):
        job.schedule_removal()


def schedule_invite_copy_revert(job_queue, chat_id, message_id, user_id):
    if job_queue is None:
        return
    cancel_invite_copy_revert(job_queue, chat_id, message_id)
    job_queue.run_once(
        _invite_copy_revert_callback,
        20,
        data={"chat_id": chat_id, "message_id": message_id, "user_id": user_id},
        name=_invite_copy_revert_job_name(chat_id, message_id),
    )


async def _invite_copy_revert_callback(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    amount = await get_invite_reward_amount()
    uname = _bot_username(context)
    rows = _invite_earn_keyboard_rows(amount, uname, data["user_id"])
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=data["chat_id"],
            message_id=data["message_id"],
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except Exception:
        pass


# --- Invite & Earn: menu ----------------------------------------------------

def _invite_earn_keyboard_rows(amount, uname, user_id, copy_label="Copy your invite link 🔗"):
    rows = []
    if uname:
        rows.append([InlineKeyboardButton(
            f"➕ Add to a group to earn {DOLLAR}{amount}",
            url=f"https://t.me/{uname}?startgroup=invite_{user_id}",
        )])
    rows.append([InlineKeyboardButton(copy_label, callback_data="invite_copy_link")])
    rows.append([InlineKeyboardButton("My Invites", callback_data="invite_my_invites")])
    rows.append([InlineKeyboardButton("How to Invite & Earn", callback_data="invite_how_to")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="home")])
    return rows


async def build_invite_earn_keyboard(context, user_id, copy_label="Copy your invite link 🔗"):
    amount = await get_invite_reward_amount()
    uname = _bot_username(context)
    return InlineKeyboardMarkup(_invite_earn_keyboard_rows(amount, uname, user_id, copy_label))


async def build_invite_earn_text() -> str:
    amount = await get_invite_reward_amount()
    return (
        f"{DOLLAR} <b>Invite & Earn</b>\n\n"
        f"Add me to a group with 200+ members and keep me there for a full "
        f"{INVITE_REWARD_HOURS} hours, and you'll earn {DOLLAR}{amount} straight into your Word "
        f"Scramble balance - once per group, credited to whoever's link "
        f"actually did the inviting.\n\n"
        f"Use the buttons below to grab your personal invite link, check "
        f"your invites, or see exactly how it works."
    )


def invite_how_to_text(amount) -> str:
    return (
        "📖 <b>How to Invite & Earn</b>\n\n"
        "1️⃣ Already an admin somewhere with 200+ members? Tap \"Add to a "
        "group\" above - Telegram will show you a picker of eligible groups, "
        "and you're automatically credited as soon as it's added.\n\n"
        "2️⃣ Not an admin anywhere yourself? Tap \"Copy your invite link\" and "
        "send it to someone who IS a group admin. As long as they add me "
        "using YOUR link, you get the reward, not them - Telegram just shows "
        "them the group picker, it doesn't matter whose finger does the "
        "tapping.\n\n"
        f"3️⃣ OR, send the bot's username to a group admin to add me to his/her group. "
        f"After {INVITE_REWARD_HOURS} hours, use /claim in that group to claim your {DOLLAR}{amount} reward.\n\n"
        f"Either way, keep the group above 200 members and don't let me get "
        f"removed - after {INVITE_REWARD_HOURS} straight hours, {DOLLAR}{amount} lands in your "
        f"Word Scramble balance automatically, no need to ask."
    )


def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎮 Play Game", callback_data="play"),
            InlineKeyboardButton(f"{DOLLAR} Invite & Earn", callback_data="invite_earn"),
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


async def _start_gift_from_deeplink(update: Update, context: ContextTypes.DEFAULT_TYPE, target_id):
    user = update.effective_user
    if not is_owner(user.id) and not await asyncio.to_thread(is_premium, user.id):
        await update.message.reply_text(
            "⭐ Only Premium members can gift Premium to others - use /buypremium to get Premium first."
        )
        return
    if is_owner(user.id):
        await update.message.reply_text("👑 As the owner, use /giftpremium <username|ID> to gift Premium for free.")
        return
    if target_id == user.id:
        await update.message.reply_text("ℹ️ That's you! Use /buypremium to extend your own Premium.")
        return
    if is_owner(target_id):
        await update.message.reply_text("👑 The bot owner always has Premium - nothing to gift!")
        return
    row = await asyncio.to_thread(db.get_user_by_id, target_id)
    target_name = (row[1] if row else None) or str(target_id)
    err = await send_gift_invoice(context, user.id, target_id, target_name)
    if err:
        await update.message.reply_text(err)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    await asyncio.to_thread(db.add_user, user.id, user.first_name)

    if context.args:
        first_arg = context.args[0]
        chat = update.effective_chat

        # Invite & Earn referral attribution (point 5) - check chat type
        # FIRST, before any of the private-chat-only branches below, since
        # this is the /start invite_<id> relay Telegram posts into a group
        # right after it's added via a player's ?startgroup=invite_<id> link.
        # The literal "invite_" prefix can never collide with "adclick_" or
        # the bare-digit profile-view fallback further down.
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) and re.fullmatch(r"invite_\d+", first_arg):
            referred_id = int(first_arg.split("_", 1)[1])

            row_id = await asyncio.to_thread(db.get_recent_pending_invite_reward_for_chat, chat.id)
            if row_id:
                referred_user = await asyncio.to_thread(db.get_user_by_id, referred_id)
                referred_name = referred_user[1] if referred_user else str(referred_id)
                await asyncio.to_thread(db.update_invite_reward_inviter, row_id, referred_id, referred_name)
            elif context.job_queue is not None:
                # my_chat_member may not have created the pending row yet -
                # retry once, a few seconds later, rather than giving up.
                context.job_queue.run_once(
                    _retry_invite_attribution,
                    5,
                    data={"chat_id": chat.id, "referred_id": referred_id},
                    name=f"invite_attrib_retry_{chat.id}_{referred_id}",
                )
            return

        # Group -> DM hand-offs (t.me/<bot>?start=<flow>): continue the command here.
        if chat.type == ChatType.PRIVATE:
            dm_flows = {
                "buypremium": buypremium_command,
                "buycash": buycash_command,
                "boostrank": boostrank_command,
                "boostlevel": boostlevel_command,
            }
            if first_arg in dm_flows:
                await dm_flows[first_arg](update, context)
                return
            gift_match = re.fullmatch(r"gift_(\d+)", first_arg)
            if gift_match:
                await _start_gift_from_deeplink(update, context, int(gift_match.group(1)))
                return

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

    # Reply-to-message: /start (no args) shows the replied-to person's profile.
    reply_msg = update.message.reply_to_message
    if (
        reply_msg is not None
        and reply_msg.from_user is not None
        and not getattr(reply_msg, "forum_topic_created", None)  # forum topics auto-set a "reply" to the topic root
    ):
        replied_user = reply_msg.from_user
        target_player = await asyncio.to_thread(db.get_user, replied_user.id)
        if not target_player:
            await update.message.reply_text("❌ No player found for that user.")
            return
        await update.message.reply_html(f"""
👤 <b>Player Profile</b>

👤 Name: {esc(target_player[1])}

💵 Balance: <b>{target_player[2]}</b>
⭐ Level: <b>{target_player[3]}</b>
🏅 Rank: <b>{target_player[4]}</b>

Tap a button below to continue your adventure!
""", reply_markup=main_menu())
        return

    player = await asyncio.to_thread(db.get_user, user.id)

    welcome_text = f"""
🎮 <b>Welcome Back, Warrior!</b>

Hello, {esc_name(user.first_name)}! 👋

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


# --- Feature: bot-admin requirement toggle -----------------------------------

DEFAULT_ADMIN_MODE_PROMPT_TEXT = (
    "🎮 NEW UPDATE! 🚀\n\n"
    "We've added a new feature to make gameplay ⚡ faster and smoother, record scores "
    "🏆 instantly, and automatically delete ONLY the bot's messages 🧹 to keep "
    "the group neat and organized.\n\n"
    "👨‍💼 Make the bot an admin to unlock and enjoy all game features! 🔤🎮"
)


def get_admin_mode_default_text() -> str:
    text = db.get_config("adminmode_default_text", None)
    return text if text else DEFAULT_ADMIN_MODE_PROMPT_TEXT


async def _bot_is_admin_in_chat(context, chat_id) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, context.bot.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def admin_prompt_keyboard(context):
    uname = _bot_username(context)
    if not uname:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👨‍💼 Add as an Admin", url=f"https://t.me/{uname}")
    ]])


async def _broadcast_admin_prompt(context, chat_ids, text) -> int:
    kb = admin_prompt_keyboard(context)
    sent = 0
    for cid in chat_ids:
        try:
            await context.bot.send_message(chat_id=cid, text=text, reply_markup=kb)
            sent += 1
        except Exception as e:
            logger.error(f"Failed to send admin-mode prompt to {cid}: {e}")
        await asyncio.sleep(0.05)
    return sent


def admin_mode_confirm_keyboard(scope, value, target=""):
    suffix = f":{target}" if target else ""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Send without added text", callback_data=f"amc:{scope}:{value}:none{suffix}")],
        [InlineKeyboardButton("Add a text to show", callback_data=f"amc:{scope}:{value}:addtext{suffix}")],
        [InlineKeyboardButton("Send default text", callback_data=f"amc:{scope}:{value}:default{suffix}")],
        [InlineKeyboardButton("Change default text", callback_data=f"amc:{scope}:{value}:changedefault{suffix}")],
    ])


async def resolve_group_arg(arg: str):
    """Resolve a group the bot is CURRENTLY in, by numeric chat ID or @username.
    Returns (chat_id, title) or None."""
    arg = (arg or "").strip()
    if not arg:
        return None
    if re.fullmatch(r"-?\d+", arg):
        details = await asyncio.to_thread(db.get_group_full_details, int(arg))
    else:
        g = await asyncio.to_thread(db.get_group_by_username, arg.lstrip("@"))
        if not g:
            return None
        details = await asyncio.to_thread(db.get_group_full_details, g[0])
    if not details or details[5] is not None:  # details[5] is left_at
        return None
    return details[0], details[1]


async def is_chat_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except (Forbidden, BadRequest):
        return False


async def is_chat_creator(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True only for the Telegram creator/owner of the CURRENT group - not
    regular admins, and not meaningful outside a group chat."""
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == ChatType.PRIVATE:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status == "creator"
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

    if await asyncio.to_thread(db.get_effective_admin_mode, chat_id):
        if not await _bot_is_admin_in_chat(context, chat_id):
            await update.message.reply_text(
                "🙏 To start a game here, I'll need to be a group admin - that way I "
                "can manage rounds and announce winners properly. Please make me an admin, "
                "then just run this command again!"
            )
            return

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

    if query.data.startswith("amc:"):
        if not is_bot_admin_or_owner(user.id):
            await query.answer("🚫 Owner or bot admins only.", show_alert=True)
            return

        parts = query.data.split(":")
        scope = parts[1]
        value = parts[2]
        action = parts[3]
        target = parts[4] if len(parts) > 4 else None

        if action == "none":
            await query.answer()
            if scope == "adminmode":
                await asyncio.to_thread(db.set_config, "require_bot_admin_global", "1")
                await query.edit_message_text(
                    "🟢 Global Admin Mode is now ON. No message was sent to groups."
                )
            else:
                await asyncio.to_thread(db.set_group_admin_mode_override, int(target), "on")
                await query.edit_message_text(
                    "🟢 Admin Mode override is now ON for that group. No message was sent."
                )
            return

        if action == "default":
            await query.answer()
            text = get_admin_mode_default_text()
            if scope == "adminmode":
                await asyncio.to_thread(db.set_config, "require_bot_admin_global", "1")
                groups = await asyncio.to_thread(db.get_current_groups)
                sent = await _broadcast_admin_prompt(context, [g[0] for g in groups], text)
                await query.edit_message_text(
                    f"🟢 Global Admin Mode is now ON. Default message sent to {sent} group(s)."
                )
            else:
                await asyncio.to_thread(db.set_group_admin_mode_override, int(target), "on")
                await _broadcast_admin_prompt(context, [int(target)], text)
                await query.edit_message_text(
                    "🟢 Admin Mode override is now ON for that group. Default message sent."
                )
            return

        if action == "addtext":
            pending_action = f"amode_text:{scope}:{value}" + (f":{target}" if target else "")
            await asyncio.to_thread(db.set_pending_input, user.id, 0, pending_action)
            prompt = (
                "✍️ Send me the message you'd like shown "
                "(I'll automatically add an 'Add as an Admin' button below it)."
            )
            try:
                await context.bot.send_message(chat_id=user.id, text=prompt)
                if chat.type == ChatType.PRIVATE:
                    await query.answer()
                    await query.edit_message_text(prompt)
                else:
                    await query.answer("📩 Check your DM with me.", show_alert=True)
            except Forbidden:
                await query.answer(
                    "⚠️ Please start a DM with me first, then try again.", show_alert=True
                )
            return

        if action == "changedefault":
            await asyncio.to_thread(db.set_pending_input, user.id, 0, "amode_default")
            prompt = "✍️ Send me the new default message."
            try:
                await context.bot.send_message(chat_id=user.id, text=prompt)
                if chat.type == ChatType.PRIVATE:
                    await query.answer()
                    await query.edit_message_text(prompt)
                else:
                    await query.answer("📩 Check your DM with me.", show_alert=True)
            except Forbidden:
                await query.answer(
                    "⚠️ Please start a DM with me first, then try again.", show_alert=True
                )
            return

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
        elif result == "tournament":
            await query.answer(
                "🏟️ A tournament is in progress here - rounds are paused until it ends.", show_alert=True
            )
        elif result == "send_failed":
            await query.answer(
                "⚠️ Couldn't reach the chat right now — try again in a moment.", show_alert=True
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

    if query.data == "invite_earn":
        await query.answer()
        text = await build_invite_earn_text()
        keyboard = await build_invite_earn_keyboard(context, user.id)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        return

    if query.data == "invite_copy_link":
        uname = _bot_username(context)
        if not uname:
            await query.answer(
                "⚠️ Couldn't build your invite link right now - try again shortly.",
                show_alert=True,
            )
            return

        link = f"https://t.me/{uname}?startgroup=invite_{user.id}"

        try:
            await context.bot.send_message(chat_id=user.id, text=link)
        except Forbidden:
            await query.answer(
                "⚠️ Please start a DM with me first, then try again.", show_alert=True
            )
            return

        await query.answer()
        copied_keyboard = await build_invite_earn_keyboard(context, user.id, copy_label="✅ Copied")
        await query.edit_message_reply_markup(reply_markup=copied_keyboard)
        schedule_invite_copy_revert(context.job_queue, chat.id, query.message.message_id, user.id)
        return

    if query.data == "invite_my_invites":
        await query.answer()
        claimed, pending = await asyncio.to_thread(db.get_user_invite_rewards, user.id)

        lines = [f"{DOLLAR} <b>My Invites</b>\n", "<b>Claimed</b>"]
        if claimed:
            for group_title, amount_paid in claimed:
                lines.append(f"• {esc(group_title)} — {DOLLAR}{amount_paid}")
        else:
            lines.append("— none yet —")

        lines.append("\n<b>Pending</b>")
        if pending:
            for (group_title,) in pending:
                lines.append(f"• {esc(group_title)} — — —")
        else:
            lines.append("— none —")

        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data="invite_earn")]])
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)
        return

    if query.data == "invite_how_to":
        await query.answer()
        amount = await get_invite_reward_amount()
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data="invite_earn")]])
        await query.edit_message_text(
            invite_how_to_text(amount), parse_mode="HTML", reply_markup=keyboard
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

👤 Name: {esc_name(player[1])}

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

Hello, {esc_name(user.first_name)}! 👋

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
            await query.edit_message_text(
                SCRAMBLE_GROUPS_ONLY_TEXT,
                parse_mode="HTML",
                reply_markup=add_to_group_keyboard(context),
            )
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
        elif result == "tournament":
            await query.answer(
                "🏟️ A tournament is in progress here - rounds are paused until it ends.", show_alert=True
            )
        elif result == "send_failed":
            await query.answer(
                "⚠️ Couldn't reach the chat right now — try again in a moment.", show_alert=True
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

    if query.data.startswith("gsort:"):
        if not is_owner(user.id):
            await query.answer("🚫 Owner only.", show_alert=True)
            return
        _, which, sort = query.data.split(":")
        if which == "bot":
            header, keyboard = build_botgroups_list_page(0, sort)
        else:
            header, keyboard = build_groups_list_page(0, sort)
        await query.answer()
        if header is None:
            await query.edit_message_text("No groups registered yet.")
            return
        await query.edit_message_text(header, parse_mode="HTML", reply_markup=keyboard)
        return

    if query.data.startswith("glpg:"):
        if not is_owner(user.id):
            await query.answer("🚫 Owner only.", show_alert=True)
            return
        _, sort, page_str = query.data.split(":")
        page = int(page_str)
        header, keyboard = build_groups_list_page(page, sort)
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
        _, sort, page_str = query.data.split(":")
        page = int(page_str)
        header, keyboard = build_botgroups_list_page(page, sort)
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
        _, chat_id_str, bg_sort, page_str = query.data.split(":")
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
        keyboard_rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"bglpg:{bg_sort}:{page}")])

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
        ogb_sort = parts[2] if len(parts) > 2 else "games"
        backpage = int(parts[3]) if len(parts) > 3 else 0
        await query.answer()

        if backscope == "gl":
            header, keyboard = build_groups_list_page(backpage, ogb_sort)
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
        if backscope == "gl":
            og_sort = parts[3] if len(parts) > 3 else "games"
            backpage = int(parts[4]) if len(parts) > 4 else 0
        else:
            og_sort = "games"
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
        keyboard_rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"og_back:{backscope}:{og_sort}:{backpage}")])

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
<b>{esc_name(user.first_name)} is explaining the word!</b>

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
                text=f"🔄 {sanitize_display_name(user.first_name)} changed the word.",
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
❌ <b>{esc_name(leader_name)} dropped the lead.</b>

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
                text=f"🔄 {sanitize_display_name(user.first_name)} changed the word.",
            )
        except (Forbidden, BadRequest):
            pass
        return

    if action.startswith("amode_text:"):
        parts = action.split(":")
        scope = parts[1]
        target = parts[3] if len(parts) > 3 else None
        custom_text = update.message.text.strip()
        await asyncio.to_thread(db.clear_pending_input, user.id)

        if scope == "adminmode":
            await asyncio.to_thread(db.set_config, "require_bot_admin_global", "1")
            groups = await asyncio.to_thread(db.get_current_groups)
            sent = await _broadcast_admin_prompt(context, [g[0] for g in groups], custom_text)
            await update.message.reply_text(
                f"🟢 Global Admin Mode is now ON. Message sent to {sent} group(s)."
            )
        else:
            target_id = int(target)
            await asyncio.to_thread(db.set_group_admin_mode_override, target_id, "on")
            await _broadcast_admin_prompt(context, [target_id], custom_text)
            await update.message.reply_text(
                "🟢 Admin Mode override is now ON for that group. Message sent."
            )
        return

    if action == "amode_default":
        new_default = update.message.text.strip()
        await asyncio.to_thread(db.set_config, "adminmode_default_text", new_default)
        await asyncio.to_thread(db.clear_pending_input, user.id)
        await update.message.reply_html(
            "✅ Default message updated.\n\nRun <code>/adminmode on</code> or "
            "<code>/groupadminmode on &lt;group&gt;</code> again to apply it."
        )
        return


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
        log, message_ids = await asyncio.to_thread(db.get_explain_log, chat.id, leader_id)
        if log:
            hints_text = format_numbered_hints(log, chat, message_ids)
            await safe_send(
                context.bot,
                chat.id,
                text=f"💡 <b>Hints from {esc_name(leader_name)}:</b>\n\n{hints_text}",
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        else:
            await safe_send(
                context.bot,
                chat.id,
                text=f"💡 {sanitize_display_name(leader_name)} hasn't given any hints yet.",
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
                    f"{sanitize_display_name(leader_name)} revealed the word.\n\n"
                    f"Who wants to explain?"
                ),
                reply_markup=volunteer_keyboard(),
            )
        else:
            await asyncio.to_thread(db.append_explain_log, chat.id, leader_id, text, message.message_id)
        return

    guess_clean = re.sub(r"[^\w\s]", "", text_lower).strip()
    if guess_clean == word_lower:
        # Atomically claim the round FIRST, before anything else - this is
        # the fix for the double-winner race. Modeled on
        # db.scramble_claim_round() (Word Scramble's existing atomic
        # DELETE...RETURNING claim): only the FIRST concurrent correct guess
        # (even by milliseconds) gets a row back; any other guess that
        # matched at nearly the same instant gets None and backs off with no
        # reward, no announcement, and no leader change.
        claim = await asyncio.to_thread(db.claim_word_game_win, chat.id)
        if not claim:
            try:
                await message.delete()
            except (Forbidden, BadRequest):
                pass
            return

        leader_id, leader_name, secret_word, was_explaining_started = claim
        winner = user

        if not was_explaining_started:
            cheat_penalty = get_reward("cheat")

            try:
                await message.delete()
            except (Forbidden, BadRequest):
                pass

            await asyncio.to_thread(db.award_coins, winner.id, chat.id, winner.first_name, cheat_penalty)
            cancel_explain_timeout(context, chat.id)
            await asyncio.to_thread(db.clear_stop_votes, chat.id)

            await safe_send(
                context.bot,
                chat.id,
                text=(
                    f"🚨 {sanitize_display_name(winner.first_name)} cheated. {cheat_penalty}\n\n"
                    f"Who wants to explain?"
                ),
                reply_markup=volunteer_keyboard(),
            )
            return

        await safe_react(context.bot, chat.id, message.message_id, "🎉")

        win_reward = get_reward("win")
        # Premium doubles each person's own payout, computed independently.
        winner_reward = win_reward * 2 if await asyncio.to_thread(is_premium, winner.id) else win_reward
        leader_reward = win_reward * 2 if await asyncio.to_thread(is_premium, leader_id) else win_reward
        await asyncio.to_thread(db.award_coins, winner.id, chat.id, winner.first_name, winner_reward)
        await asyncio.to_thread(db.award_coins, leader_id, chat.id, leader_name, leader_reward)

        await asyncio.to_thread(db.increment_stat, "games_played", 1)
        await asyncio.to_thread(db.increment_stat, "words_solved", 1)
        await asyncio.to_thread(db.increment_group_games, chat.id)
        await asyncio.to_thread(db.increment_user_game_stats, winner.id, 1, 1)
        await asyncio.to_thread(db.increment_user_game_stats, leader_id, 1, 0)

        cancel_explain_timeout(context, chat.id)

        auto_lead = await asyncio.to_thread(db.get_auto_lead, winner.id)

        winner_medal = await medal_prefix_if_enabled(winner.id, "show_game_medal")
        leader_medal = await medal_prefix_if_enabled(leader_id, "show_game_medal")
        winner_display = sanitize_display_name(winner.first_name)
        leader_display = sanitize_display_name(leader_name)

        result_text = (
            f"🎉 {winner_medal}{winner_display} found the word!\n\n"
            f"📝 Word: {secret_word}\n\n"
            f"{DOLLAR} {winner_medal}{winner_display} +{winner_reward}\n"
            f"🎤 {leader_medal}{leader_display} +{leader_reward}\n"
        )

        if auto_lead:
            new_word = await asyncio.to_thread(random_word, chat.id)
            await asyncio.to_thread(db.set_leader, chat.id, winner.id, winner.first_name, new_word)
            await asyncio.to_thread(db.set_explaining_started, chat.id, False)
            await asyncio.to_thread(db.start_explain_log, chat.id, winner.id)

            result_msg = await safe_send(context.bot, chat.id, text=result_text)
            round_on, _ = await asyncio.to_thread(db.get_autodelete_settings, chat.id)
            if round_on and result_msg:
                await _flush_round_messages(context, chat.id, keep_message_id=result_msg.message_id)


            sent = await safe_send(
                context.bot,
                chat.id,
                text=(
                    f"{winner_medal}{esc_name(winner.first_name)} is explaining the next word!\n\n"
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
            result_msg = await safe_send(context.bot, chat.id, text=result_text)
            round_on, _ = await asyncio.to_thread(db.get_autodelete_settings, chat.id)
            if round_on and result_msg:
                await _flush_round_messages(context, chat.id, keep_message_id=result_msg.message_id)

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
    log, message_ids = await asyncio.to_thread(db.get_explain_log, chat.id, leader_id)

    if log:
        hints_text = format_numbered_hints(log, chat, message_ids)
        await update.message.reply_html(
            f"💡 <b>Hints from {esc_name(leader_name)}:</b>\n\n{hints_text}",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    else:
        await update.message.reply_text(
            f"💡 {sanitize_display_name(leader_name)} hasn't given any hints yet."
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
    _premium_price, _premium_dur = await asyncio.to_thread(get_premium_settings)
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

⭐ <b>Premium</b>: /buypremium ({_premium_price} ⭐ for {_format_duration_words(_premium_dur)}) doubles your {DOLLAR} rewards in Word Game and Word Scramble. Check /premiumstatus.
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

    # A completed Stars payment must ALWAYS reach its handler - money has
    # already moved, so the ban/maintenance gates below must never swallow it.
    if update.message is not None and update.message.successful_payment is not None:
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

            # Invite & Earn (point 4) - this is the ONLY place that creates
            # invite_rewards rows. Skip entirely for bot actors.
            if actor and not actor.is_bot:
                already_rewarded = await asyncio.to_thread(db.has_rewarded_invite, chat.id)
                if not already_rewarded:
                    try:
                        member_count = await context.bot.get_chat_member_count(chat.id)
                    except Exception as e:
                        logger.warning(f"Invite & Earn: get_chat_member_count failed for {chat.id}: {e}")
                        member_count = None

                    if member_count is not None:
                        if member_count >= INVITE_REWARD_MIN_MEMBERS:
                            new_id = await asyncio.to_thread(
                                db.create_pending_invite_reward,
                                chat.id,
                                chat.title or str(chat.id),
                                actor.id,
                                actor.first_name,
                                member_count,
                            )
                            if new_id:
                                schedule_invite_reward_payout(
                                    context.job_queue, new_id, INVITE_REWARD_SECONDS
                                )
                        else:
                            try:
                                await context.bot.send_message(
                                    chat_id=chat.id,
                                    text=(
                                        f"ℹ️ This group needs at least "
                                        f"{INVITE_REWARD_MIN_MEMBERS} members to qualify for "
                                        f"the Invite & Earn reward (currently {member_count})."
                                    ),
                                )
                            except Exception:
                                pass
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

        # Invite & Earn (point 4) - don't wait for the 72-hour check to
        # discover the group already left; fail it and cancel the job now.
        failed_id = await asyncio.to_thread(db.fail_pending_invite_reward_for_chat, chat.id)
        if failed_id:
            cancel_invite_reward_job(context.job_queue, failed_id)

        # Word Scramble tournament - never lose anyone's money if the bot is removed.
        try:
            open_tournament = await asyncio.to_thread(db.tournament_get_open_for_chat, chat.id)
            if open_tournament:
                await _tournament_abort_and_announce(
                    context, open_tournament["id"], chat.id,
                    "Tournament cancelled - I was removed from this group.",
                    dm_players=True, group_title=chat.title,
                )
        except Exception as e:
            logger.error(f"Failed to refund tournament in removed chat {chat.id}: {e}")

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


async def ranks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [
        "🏆 <b>Rank Ladder</b>\n",
        "<i>Based on your combined balance - Word Game + Word Scramble earnings added together.</i>\n",
    ]
    for threshold, rank_name, level in db.RANKS:
        lines.append(f"Level {level} — {rank_name} — {DOLLAR}{threshold:,}")

    text = "\n".join(lines) + ad_footer_html()

    await update.message.reply_html(
        text,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
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
        names = [sanitize_display_name(n) for n in await asyncio.to_thread(db.get_stop_vote_names, chat.id)]

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
            f"🗳️ {sanitize_display_name(user.first_name)} voted to stop the game. ({count}/3)"
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


@owner_or_admin_only
async def showlevel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() not in ("on", "off"):
        current = await asyncio.to_thread(db.get_config, "show_level", "1")
        await update.message.reply_text(
            "Usage: /showlevel on|off\n"
            f"Currently: {'on' if current == '1' else 'off'}"
        )
        return

    mode = context.args[0].lower()
    await asyncio.to_thread(db.set_config, "show_level", "1" if mode == "on" else "0")
    await update.message.reply_text(f"✅ Level/Rank badge in Word Scramble winner messages turned {mode}.")


@owner_or_admin_only
async def showmedal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() not in ("on", "off"):
        current = await asyncio.to_thread(db.get_config, "show_scramble_medal", "1")
        await update.message.reply_text(
            "Usage: /showmedal on|off\n"
            f"Currently: {'on' if current == '1' else 'off'}"
        )
        return

    mode = context.args[0].lower()
    await asyncio.to_thread(db.set_config, "show_scramble_medal", "1" if mode == "on" else "0")
    await update.message.reply_text(f"✅ Medal prefix (🥇🥈🥉) in Word Scramble messages turned {mode}.")


@owner_or_admin_only
async def showgamemedal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() not in ("on", "off"):
        current = await asyncio.to_thread(db.get_config, "show_game_medal", "1")
        await update.message.reply_text(
            "Usage: /showgamemedal on|off\n"
            f"Currently: {'on' if current == '1' else 'off'}"
        )
        return

    mode = context.args[0].lower()
    await asyncio.to_thread(db.set_config, "show_game_medal", "1" if mode == "on" else "0")
    await update.message.reply_text(f"✅ Medal prefix (🥇🥈🥉) in Word Game messages turned {mode}.")


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


def build_groups_list_page(page: int, sort: str = "games"):
    if sort not in ("games", "time"):
        sort = "games"
    total = db.get_groups_count()
    offset = page * OWNER_LIST_PAGE_SIZE
    rows = db.get_groups_page(offset, OWNER_LIST_PAGE_SIZE, sort)

    if not rows:
        return None, None

    total_pages = max(1, (total + OWNER_LIST_PAGE_SIZE - 1) // OWNER_LIST_PAGE_SIZE)
    start_n = offset + 1
    end_n = offset + len(rows)
    sort_label = "🎮 Games Played" if sort == "games" else "🕒 Time Added"

    header = (
        f"👨‍👩‍👧‍👦 <b>Groups</b> ({total} total) — Page {page + 1}/{total_pages}\n"
        f"<i>Sorted by: {sort_label} • Entries {start_n}–{end_n} • Tap a group for full details.</i>"
    )
    if offset + OWNER_LIST_PAGE_SIZE >= total:
        header += "\n<i>This is the end of the group list.</i>"

    keyboard = [
        [InlineKeyboardButton(
            f"{start_n + i}. {truncate_name(title or 'Untitled Group')} — {games:,} Games",
            callback_data=f"og:{chat_id}:gl:{sort}:{page}",
        )]
        for i, (chat_id, title, member_count, games, last_active) in enumerate(rows)
    ]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"glpg:{sort}:{page - 1}"))
    if offset + OWNER_LIST_PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"glpg:{sort}:{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    return header, InlineKeyboardMarkup(keyboard)


def build_botgroups_list_page(page: int, sort: str = "games"):
    if sort not in ("games", "time"):
        sort = "games"
    total = db.get_botgroups_count()
    offset = page * BOTGROUPS_PAGE_SIZE
    rows = db.get_botgroups_page(offset, BOTGROUPS_PAGE_SIZE, sort)

    if not rows:
        return None, None

    total_pages = max(1, (total + BOTGROUPS_PAGE_SIZE - 1) // BOTGROUPS_PAGE_SIZE)
    start_n = offset + 1
    end_n = offset + len(rows)
    sort_label = "🎮 Games Played" if sort == "games" else "🕒 Time Added"

    s = db.get_bot_groups_stats()
    header = (
        f"👥 <b>Bot Groups</b>\n"
        f"📊 Total Ever: <b>{s['total']}</b> | 🟢 Current: <b>{s['current']}</b> | 🔴 Removed: <b>{s['removed']}</b>\n\n"
        f"Page {page + 1}/{total_pages} • Sorted by: {sort_label} • <code>🎮 Word Game | 🔤 Scramble</code>\n"
        f"<i>Entries {start_n}–{end_n} • Tap a group to inspect/join.</i>"
    )

    keyboard = []
    for i, (chat_id, title, wg_played, left_at, scr_played) in enumerate(rows):
        status = "🟢" if not left_at else "🔴"
        label = f"{start_n + i}. {status} {truncate_name(title or 'Untitled Group')} — 🎮{wg_played} 🔤{scr_played}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"bg:{chat_id}:{sort}:{page}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"bglpg:{sort}:{page - 1}"))
    if offset + BOTGROUPS_PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"bglpg:{sort}:{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    return header, InlineKeyboardMarkup(keyboard)


@owner_only
async def botgroups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await asyncio.to_thread(db.get_botgroups_count) == 0:
        await update.message.reply_text("No groups registered yet.")
        return
    await update.message.reply_html(
        "👥 <b>Bot Groups</b>\n\nHow would you like the list ordered?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 Games Played", callback_data="gsort:bot:games")],
            [InlineKeyboardButton("🕒 Time Added", callback_data="gsort:bot:time")],
        ]),
    )


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

    if await asyncio.to_thread(db.get_groups_count) == 0:
        await update.message.reply_text("No groups registered yet.")
        return

    await update.message.reply_html(
        "👨‍👩‍👧‍👦 <b>Groups</b>\n\nHow would you like the list ordered?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 Games Played", callback_data="gsort:gl:games")],
            [InlineKeyboardButton("🕒 Time Added", callback_data="gsort:gl:time")],
        ]),
    )


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


CONTROLGROUP_USAGE = (
    "Usage:\n"
    "/controlgroup automaticmode on|off <group>\n"
    "/controlgroup slowmode on <time> <group>\n"
    "/controlgroup slowmode off <group>\n\n"
    "<group> = numeric chat ID, @username, or allgroups.\n"
    "Example: /controlgroup slowmode on 2m allgroups"
)


@owner_only
async def controlgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(CONTROLGROUP_USAGE)
        return

    feature = args[0].lower()
    action = args[1].lower()

    if feature in ("automaticmode", "automatic"):
        feature = "automaticmode"
        if action not in ("on", "off") or len(args) != 3:
            await update.message.reply_text(CONTROLGROUP_USAGE)
            return
        target_arg = args[2]
        seconds = None
    elif feature == "slowmode":
        if action == "on":
            if len(args) != 4:
                await update.message.reply_text(CONTROLGROUP_USAGE)
                return
            seconds = _parse_duration(args[2])
            if seconds is None:
                await update.message.reply_text("❌ Invalid duration. Use e.g. 2m, 5m, 1h.")
                return
            target_arg = args[3]
        elif action == "off":
            if len(args) != 3:
                await update.message.reply_text(CONTROLGROUP_USAGE)
                return
            seconds = None
            target_arg = args[2]
        else:
            await update.message.reply_text(CONTROLGROUP_USAGE)
            return
    else:
        await update.message.reply_text(CONTROLGROUP_USAGE)
        return

    if target_arg.lower() == "allgroups":
        targets = list(await asyncio.to_thread(db.get_current_groups))
        is_all = True
    else:
        resolved = await resolve_group_arg(target_arg)
        if not resolved:
            await update.message.reply_html(
                f"❌ No group found matching <code>{esc(target_arg)}</code> that I'm currently in."
            )
            return
        targets = [resolved]
        is_all = False

    if not targets:
        await update.message.reply_text("No groups to apply this to.")
        return

    changed, skipped, failed = 0, 0, 0
    for chat_id, _title in targets:
        try:
            if feature == "automaticmode":
                ok = await apply_automode(chat_id, action == "on")
                if ok:
                    changed += 1
                else:
                    skipped += 1
            elif action == "on":
                await apply_slowmode_on(context.job_queue, chat_id, seconds)
                changed += 1
            else:
                await apply_slowmode_off(context.job_queue, chat_id)
                changed += 1
        except Exception as e:
            logger.error(f"/controlgroup failed for chat {chat_id}: {e}")
            failed += 1

    label = "Automatic Mode" if feature == "automaticmode" else "Slow Mode"
    state = action if action == "off" else (f"on ({_format_duration_words(seconds)})" if seconds else "on")

    if not is_all:
        _cid, title = targets[0]
        if changed:
            await update.message.reply_html(
                f"✅ {label} turned {state} in <b>{esc(title)}</b> (<code>{_cid}</code>)."
            )
        elif skipped:
            await update.message.reply_html(
                f"⚠️ Not changed in <b>{esc(title)}</b>: Slow Mode is currently on there, so Automatic "
                f"Mode can't be enabled. Turn Slow Mode off there first."
            )
        else:
            await update.message.reply_text(f"❌ Couldn't update {label} in that group.")
        return

    lines = [
        f"🎛️ <b>{label} → {state}</b>\n",
        f"✅ Updated: {changed}",
    ]
    if skipped:
        lines.append(f"⚠️ Skipped (Slow Mode is on there): {skipped}")
    lines.append(f"❌ Failed: {failed}")
    lines.append(f"👥 Total: {len(targets)}")
    await update.message.reply_html("\n".join(lines))


async def _resolve_tournament_ban_target(update: Update, usage: str):
    """Returns (target_type, target_id, label) or None (an error was already sent).
    Reply-to-message (no args) targets that user; otherwise the first arg is
    a numeric ID (negative = group, positive = user) or an @username
    (looked up as a player first, then as a group)."""
    args = update.message.text.split()[1:]
    reply_msg = update.message.reply_to_message

    if not args:
        if (
            reply_msg is not None
            and reply_msg.from_user is not None
            and not getattr(reply_msg, "forum_topic_created", None)
        ):
            u = reply_msg.from_user
            return "user", u.id, u.first_name or str(u.id)
        await update.message.reply_text(usage)
        return None

    arg = args[0]
    if re.fullmatch(r"-?\d+", arg):
        tid = int(arg)
        if tid < 0:
            return "group", tid, str(tid)
        return "user", tid, str(tid)

    uname = arg.lstrip("@")
    row = await asyncio.to_thread(db.get_user_by_username, uname)
    if row:
        return "user", row[0], row[1] or arg
    g = await asyncio.to_thread(db.get_group_by_username, uname)
    if g:
        return "group", g[0], g[1] or arg

    await update.message.reply_text(f"❌ No player or group found matching {arg}.")
    return None


@owner_or_admin_only
async def tournamentban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usage = (
        "Usage: /tournamentban <username|ID>  (or reply to a user's message)\n"
        "/tournamentban <group username|group ID>\n\n"
        "Bans a player from /settournament, /tournament and /join (they can still play the normal games), "
        "or bans a group from running tournaments."
    )
    target = await _resolve_tournament_ban_target(update, usage)
    if target is None:
        return
    target_type, target_id, label = target

    if target_type == "user" and is_owner(target_id):
        await update.message.reply_text("❌ You can't tournament-ban the bot owner.")
        return

    await asyncio.to_thread(db.tournament_ban, target_type, target_id, update.effective_user.id)

    if target_type == "user":
        await update.message.reply_html(
            f"🚫 <b>{esc(label)}</b> (<code>{target_id}</code>) is now banned from tournaments. "
            f"They can still play Word Game and Word Scramble."
        )
    else:
        await update.message.reply_html(
            f"🚫 Group <b>{esc(label)}</b> (<code>{target_id}</code>) can no longer run tournaments."
        )


@owner_or_admin_only
async def tournamentunban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usage = (
        "Usage: /tournamentunban <username|ID>  (or reply to a user's message)\n"
        "/tournamentunban <group username|group ID>"
    )
    target = await _resolve_tournament_ban_target(update, usage)
    if target is None:
        return
    target_type, target_id, label = target

    removed = await asyncio.to_thread(db.tournament_unban, target_type, target_id)
    kind = "Player" if target_type == "user" else "Group"
    if removed:
        await update.message.reply_html(
            f"✅ {kind} <b>{esc(label)}</b> (<code>{target_id}</code>) is no longer tournament-banned."
        )
    else:
        await update.message.reply_html(
            f"ℹ️ {kind} <b>{esc(label)}</b> (<code>{target_id}</code>) wasn't tournament-banned."
        )


@owner_only
async def sendgroupmessage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_html(
            "Usage:\n"
            "<code>/sendgroupmessage &lt;group ID or @username&gt; &lt;message&gt;</code>\n"
            "<code>/sendgroupmessage allgroups &lt;message&gt;</code>\n\n"
            "The message is sent as this bot, not from your own account."
        )
        return

    target_arg = context.args[0]
    message_text = " ".join(context.args[1:]).strip()

    if not message_text:
        await update.message.reply_text("❌ Provide a message to send.")
        return

    if target_arg.lower() == "allgroups":
        groups = await asyncio.to_thread(db.get_current_groups)
        recipients = list(groups)
    else:
        resolved = await resolve_group_arg(target_arg)
        if not resolved:
            await update.message.reply_html(
                f"❌ No group found matching <code>{esc(target_arg)}</code> that I'm currently in."
            )
            return
        recipients = [resolved]

    status = await update.message.reply_text(
        f"📣 Sending to {len(recipients)} group(s)..."
    )

    success, failed = 0, 0
    for chat_id, _title in recipients:
        try:
            await context.bot.send_message(chat_id=chat_id, text=message_text)
            success += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await status.edit_text(
        f"📣 <b>Message Sent</b>\n\n"
        f"✅ Successful: {success}\n"
        f"❌ Failed: {failed}\n"
        f"👥 Total: {len(recipients)}",
        parse_mode="HTML",
    )


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


async def autodelete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if not context.args:
        if chat.type == ChatType.PRIVATE:
            await update.message.reply_html(
                "Usage (inside a group, by that group's admins or the bot owner):\n"
                "<code>/autodelete on</code> — clean up bot messages after each round, "
                "keep the winner announcement\n"
                "<code>/autodelete off</code>\n"
                "<code>/autodelete on &lt;number&gt;&lt;s|m|h|d&gt;</code> — periodic cleanup "
                "(e.g. <code>/autodelete on 9m</code>)\n\n"
                "From my DM (owner only):\n"
                "<code>/autodelete on allgroups</code>\n"
                "<code>/autodelete on &lt;group ID or @username&gt;</code>\n"
                "<code>/autodelete on/off &lt;number&gt;&lt;s|m|h|d&gt; &lt;group ID or @username&gt;</code>"
            )
            return
        if not await is_chat_admin(update, context) and not is_bot_admin_or_owner(user.id):
            await update.message.reply_text("🚫 Group admins or the bot owner only.")
            return
        round_on, interval = await asyncio.to_thread(db.get_autodelete_settings, chat.id)
        await update.message.reply_html(
            f"⚙️ <b>Auto-Delete (this group)</b>\n\n"
            f"Round cleanup: {'🟢 ON' if round_on else '🔴 OFF'}\n"
            f"Interval cleanup: "
            f"{'🟢 every ' + _format_duration_words(interval) if interval else '🔴 OFF'}\n\n"
            f"Usage:\n<code>/autodelete on</code>\n<code>/autodelete off</code>\n"
            f"<code>/autodelete on &lt;number&gt;&lt;s|m|h|d&gt;</code>"
        )
        return

    action = context.args[0].lower()
    if action not in ("on", "off"):
        await update.message.reply_text("Usage: /autodelete on|off [interval] [group]")
        return

    rest = context.args[1:]
    interval_token = None
    group_token = None
    for tok in rest:
        if interval_token is None and _parse_autodelete_interval(tok) is not None:
            interval_token = tok
        elif group_token is None:
            group_token = tok

    interval_seconds = _parse_autodelete_interval(interval_token) if interval_token else None

    if group_token:
        if not is_owner(user.id):
            await update.message.reply_text("🚫 Owner only.")
            return
        if group_token.lower() == "allgroups":
            targets = list(await asyncio.to_thread(db.get_current_groups))
        else:
            resolved = await resolve_group_arg(group_token)
            if not resolved:
                await update.message.reply_html(
                    f"❌ No group found matching <code>{esc(group_token)}</code> that I'm currently in."
                )
                return
            targets = [resolved]
    else:
        if chat.type == ChatType.PRIVATE:
            await update.message.reply_html(
                "Run this inside the group you want to configure, or give me an explicit "
                "group (owner only): <code>/autodelete on allgroups</code> or "
                "<code>/autodelete on &lt;group ID or @username&gt;</code>"
            )
            return
        if not await is_chat_admin(update, context) and not is_bot_admin_or_owner(user.id):
            await update.message.reply_text("🚫 Group admins or the bot owner only.")
            return
        targets = [(chat.id, chat.title)]

    applied = 0
    for target_chat_id, _target_title in targets:
        if action == "off":
            if interval_token:
                await asyncio.to_thread(db.set_autodelete_interval, target_chat_id, 0)
                cancel_autodelete_job(context.job_queue, target_chat_id)
            else:
                await asyncio.to_thread(db.set_autodelete_round_mode, target_chat_id, "off")
        else:
            if interval_token:
                await asyncio.to_thread(db.set_autodelete_interval, target_chat_id, interval_seconds)
                schedule_autodelete_job(context.job_queue, target_chat_id, interval_seconds)
            else:
                await asyncio.to_thread(db.set_autodelete_round_mode, target_chat_id, "on")
        applied += 1

    if interval_token:
        if action == "on":
            summary = (
                f"🟢 Interval Auto-Delete (every {_format_duration_words(interval_seconds)}) "
                f"turned ON for {applied} group(s)."
            )
        else:
            summary = f"🔴 Interval Auto-Delete turned OFF for {applied} group(s)."
    else:
        if action == "on":
            summary = (
                f"🟢 Round-based Auto-Delete turned ON for {applied} group(s) "
                f"(bot messages cleared after each round, winner announcement kept)."
            )
        else:
            summary = f"🔴 Round-based Auto-Delete turned OFF for {applied} group(s)."

    await update.message.reply_text(summary)


@owner_or_admin_only
async def adminmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() not in ("on", "off"):
        current = await asyncio.to_thread(db.get_config, "require_bot_admin_global", "0")
        state = "🟢 ON" if current == "1" else "🔴 OFF"
        await update.message.reply_html(
            f"⚙️ <b>Global Admin Mode</b>\n\n"
            f"Status: <b>{state}</b>\n\n"
            f"When ON, the bot requires admin rights before a game can start in any group "
            f"that doesn't have its own override set via /groupadminmode.\n\n"
            f"Usage:\n<code>/adminmode on</code>\n<code>/adminmode off</code>"
        )
        return

    value = context.args[0].lower()

    if value == "off":
        await asyncio.to_thread(db.set_config, "require_bot_admin_global", "0")
        await update.message.reply_text("🔴 Global Admin Mode is now OFF.")
        return

    await update.message.reply_html(
        "⚙️ Before turning <b>Admin Mode ON</b> globally, how should I notify groups "
        "that don't already have their own admin-mode override?",
        reply_markup=admin_mode_confirm_keyboard("adminmode", "on"),
    )


@owner_or_admin_only
async def groupadminmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2 or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_html(
            "Usage:\n"
            "<code>/groupadminmode on &lt;group ID or @username&gt;</code>\n"
            "<code>/groupadminmode off &lt;group ID or @username&gt;</code>\n\n"
            "Only works for groups I'm currently in. If a group has no override set, it "
            "follows the global /adminmode setting."
        )
        return

    value = context.args[0].lower()
    group_arg = context.args[1]

    resolved = await resolve_group_arg(group_arg)
    if not resolved:
        await update.message.reply_html(
            f"❌ No group found matching <code>{esc(group_arg)}</code> that I'm currently in."
        )
        return

    target_chat_id, target_title = resolved

    if value == "off":
        await asyncio.to_thread(db.set_group_admin_mode_override, target_chat_id, "off")
        await update.message.reply_html(
            f"🔴 Admin Mode override for <b>{esc(target_title)}</b> is now OFF "
            f"(it no longer requires me to be admin there)."
        )
        return

    await update.message.reply_html(
        f"⚙️ Before turning <b>Admin Mode ON</b> for <b>{esc(target_title)}</b>, "
        f"how should I notify that group?",
        reply_markup=admin_mode_confirm_keyboard("groupadminmode", "on", str(target_chat_id)),
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
async def shufflewords_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MEDIUM_WORDS, WORDS, SCRAMBLE_WORDS, CUSTOM_WORDS_CACHE, REMOVED_WORDS_CACHE

    importlib.reload(mw)
    CUSTOM_WORDS_CACHE = await asyncio.to_thread(db.get_custom_words)
    REMOVED_WORDS_CACHE = await asyncio.to_thread(db.get_removed_words)
    MEDIUM_WORDS = _build_medium_words()
    WORDS = _merge_all_words()
    SCRAMBLE_WORDS = _build_scramble_words()

    used_global = {w.lower() for w in await asyncio.to_thread(db.get_all_used_words_global)}

    unused = [w for w in WORDS if w.lower() not in used_global]
    used = [w for w in WORDS if w.lower() in used_global]
    random.shuffle(unused)
    random.shuffle(used)
    WORDS = unused + used

    scramble_unused = [w for w in SCRAMBLE_WORDS if w.lower() not in used_global]
    scramble_used = [w for w in SCRAMBLE_WORDS if w.lower() in used_global]
    random.shuffle(scramble_unused)
    random.shuffle(scramble_used)
    SCRAMBLE_WORDS = scramble_unused + scramble_used

    _invalidate_difficulty_cache()

    await update.message.reply_html(
        f"🔀 <b>Word pool reshuffled.</b>\n\n"
        f"📦 Total words: {len(WORDS)}\n"
        f"🆕 Not yet used anywhere: {len(unused)}\n"
        f"♻️ Already used somewhere: {len(used)}\n\n"
        f"<i>Unused words are now prioritized at the front of the pool.</i>"
    )


@owner_only
async def reloadwords_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MEDIUM_WORDS, WORDS, CUSTOM_WORDS_CACHE, REMOVED_WORDS_CACHE

    importlib.reload(mw)
    CUSTOM_WORDS_CACHE = await asyncio.to_thread(db.get_custom_words)
    REMOVED_WORDS_CACHE = await asyncio.to_thread(db.get_removed_words)
    MEDIUM_WORDS = _build_medium_words()
    WORDS = _merge_all_words()
    _invalidate_difficulty_cache()

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


def _allcommands_text() -> str:
    return f"""
📖 <b>All Bot Commands</b>

<b>👥 Public Commands</b>
/start - open the main menu (reply to someone's message with /start to see that player's profile)
/help - how to play
/leaderboard - this chat's leaderboard (with a Global switch)
/chattop - top players in this chat
/globaltop - top players across all chats
/topplayers - top 15 players, this chat or global
/topgroups - the 10 most active groups
/ranks - the full rank/level ladder and what balance unlocks each one
/claimdailyreward - claim today's daily reward
/buypremium - premium doubles your 💵 rewards in Word Game and Word Scramble (paid with Telegram Stars; used in a group it moves you to my DM, and the invoice has a ➡️ Premium features button)
/premiumstatus - see whether premium is active and how much time is left
/buycash - premium only: buy 💵 cash with Telegram Stars; it increases your balance, level and rank (pack sizes and prices are shown live; used in a group it moves you to my DM)
/boostrank - premium only: same as /buycash - buy 💵 cash to boost your rank
/boostlevel - premium only: same as /buycash - buy 💵 cash to boost your level
/transfer &lt;@username|id&gt; &lt;amount&gt; - premium only: send 💵 from your Word Scramble balance to another player (or reply to their message with /transfer &lt;amount&gt;)
/request &lt;@username|id&gt; &lt;amount&gt; - premium only: ask another player for 💵; they get Yes/No buttons (or reply to their message with /request &lt;amount&gt;)
/settournament &lt;fee&gt; &lt;difficulty&gt; &lt;rounds&gt;rounds - host a Word Scramble tournament in a group (e.g. /settournament 2000 Medium 3rounds); everyone pays the entry fee from their Word Scramble balance and the winner takes the pot
/tournament - quick-start a tournament instantly: same as /settournament 5 easy 20rounds
/join - join the tournament lobby that's open in this group
/searchcommand &lt;text&gt; - search the command list by name or description (only shows commands you're allowed to see)
/fullhelp - a complete, paged guide to the bot: games, tournaments, premium, coins and more

<b>🎮 Group Commands</b>
/wordgame - start a round (admins only once a round is live)
/hint - see the current leader's clues so far (numbered, clickable)
/stopvote - vote to stop a stuck round (3 votes needed)

<b>💬 Private Message Commands</b>
/settings - toggle auto-lead
/autolead - same as /settings

<b>🛡️ Group Admin Commands</b>
/stopgame - group admins (or the owner) can stop a round instantly
/autodelete on|off [interval] - group admins (or the owner) can enable cleanup of the bot's own messages here; add a number+s/m/h/d (e.g. 9m) for periodic cleanup instead of round-based
/deleterequestprompt off|on [&lt;1-9&gt;s|m] - group admins (or the owner) can control whether resolved /request prompts are deleted here and after how long (default on, 30 seconds; no args shows the current setting)
/groupadminmode on|off &lt;group ID or @username&gt; - owner/Bot Admins only, works from my DM: require (or not) that I'm an admin here before games can start, overriding the global /adminmode default
/gamedifficulty easy|easy3-6|medium|hard|off [group] - owner only: control which word lengths get picked; add a group to target just that group

<b>👑 Owner Commands</b>
/stats - bot-wide statistics
/botgroups (alias: /botsgroups) - total/current/removed groups, tappable, with per-group Word Game and Scramble activity
/groups [query] - list/search every group the bot has seen
/users [query] - list/search every registered user
/banuser &lt;ID&gt; - ban a user from the bot
/unbanuser &lt;ID&gt; - unban a user
/bangroup &lt;ID&gt; - ban a group (bot leaves immediately)
/unbangroup &lt;ID&gt; - unban a group
/tournamentban &lt;username|ID&gt; - ban a player (or reply to them) from tournaments only; also accepts a group username/ID to ban that group from running tournaments
/tournamentunban &lt;username|ID&gt; - undo a tournament ban (player or group)
/controlgroup automaticmode on|off &lt;group&gt; | slowmode on &lt;time&gt;|off &lt;group&gt; - owner only: control Word Scramble Slow Mode / Automatic Mode in any group (&lt;group&gt; = ID, @username or allgroups)
/starttournament - tournament host, bot owner or Bot Admins: start the lobby early
/stoptournament - tournament host, bot owner or Bot Admins: cancel a lobby (or a paused tournament) and refund everyone
/resumetournament - tournament host, bot owner or Bot Admins: continue a paused or stuck tournament from where it stopped (owner in DM: /resumetournament &lt;group id|@username&gt; or /resumetournament allgroups)
/extendlobby &lt;2-9&gt;m - owner only: add minutes to the open tournament lobby in this group (e.g. /extendlobby 5m)
/setpremium &lt;stars&gt; &lt;duration&gt; - owner only: set the premium price and length (e.g. /setpremium 99 7d; no args shows current)
/giftpremium &lt;username|ID&gt; - Premium members only: gift Premium to another player (you pay the Stars, or reply to their message; used in a group it moves you to my DM). The owner gifts 7 days free
/setdollarpack &lt;1-5&gt; &lt;dollars&gt; &lt;stars&gt; - owner only: edit one /buycash pack (no args lists the current packs)
/editbuycashtext &lt;text&gt; - owner only, my DM: change the message above the /buycash, /boostrank and /boostlevel packs (send "reset" for the default; no args shows the current text)
/seepremiumplayers - owner only: every current premium player with time remaining
/starbalance - owner only: the bot's current Telegram Stars balance
/withdraw - owner only: Stars balance + how to withdraw it via @BotFather
/messageapplicant &lt;username|Allapplicants&gt; &lt;message&gt; - lives in the ADS bot (@UFFAdsBot), not this one: DM one advertiser or every advertiser, as the ads bot
/broadcast users|groups &lt;message&gt; - mass message everyone
/maintenance on|off - lock the bot to owner/admins only
/setreward win|cheat|daily &lt;amount&gt; - tune reward amounts
/reloadwords - reload the word pool from disk
/shufflewords - reshuffle the word pool; unused words are prioritized, used ones pushed to the end
/addwords word1, word2, ... - add new words to the pool (never deletes existing ones)
/removewords word1, word2, ... - hide a word from the pool regardless of which source it came from (built-in category or /addwords)
/forceend [Group ID] - force-end a stuck round
/forceleader &lt;@user or ID&gt; - assign a leader directly
/topup &lt;@user or ID&gt; &lt;+/-amount&gt; - adjust a player's balance
/share allusers +&lt;amount&gt; [message] - add balance to EVERY registered user
/share top&lt;N&gt; +&lt;amount&gt; [message] - add balance to the top N players on the global leaderboard
/addadmin &lt;@user or ID&gt; - appoint a Bot Admin
/removeadmin &lt;@user or ID&gt; - revoke a Bot Admin
/admins - list current Bot Admins
/allcommands - this list
/adminmode on|off - owner/Bot Admins only: require the bot to be a group admin before games can start, everywhere that hasn't set its own /groupadminmode override
/sendgroupmessage &lt;group ID or @username&gt;|allgroups &lt;message&gt; - send a message to one group or every group, as the bot
/prompt on|off - owner/Bot Admins only: toggle the welcome message sent when the bot joins a new group
/editprompt &lt;text&gt; - owner/Bot Admins only: change that welcome message
/claimeddailyreward [YYYY-MM-DD] - who claimed the daily reward
/addadmanager &lt;@username&gt; - assign the Advertisement Manager
/changeadmanager &lt;@username&gt; - replace the Advertisement Manager
/removeadmanager - clear the Advertisement Manager (hides the ad section)
/admanager - show the current Advertisement Manager
/ad on|off - owner only: globally show/hide the advertisement section
/sponsoredad on|off - owner only: show/hide just the Sponsored Ad line
/changesponsoredad &lt;text, link, @username, or 'label | link'&gt; - owner only: replace the Sponsored Ad content (or 'clear' to reset)
/showgamemedal on|off - owner/Bot Admins only: toggle the 🥇🥈🥉 medal prefix on Word Game names (global, no args shows current status)

<i>Note: banuser/unbanuser/bangroup/unbangroup/broadcast/maintenance/addadmanager/changeadmanager/removeadmanager/admanager/adminmode/prompt/editprompt/groupadminmode can also be used by Bot Admins. /ad on|off, /botgroups, /addwords, /removewords, /shufflewords, and /gamedifficulty are owner-only. /autodelete's bare on/off form works for group admins too; its explicit-group and allgroups forms are owner-only. This list and /scrambleallcommands are visible to the bot owner, Bot Admins, and each group's own Telegram creator. The owner is alerted in DM whenever someone buys Premium, gifts Premium, buys 💵 cash or boosts their rank/level. Word Scramble has its own command list - see /scrambleallcommands (includes /scramblestopallgroups). The 🎁 Daily Reward always credits the Word Game balance, and every player's Balance/Level/Rank on /start combines their Word Game + Word Scramble balances automatically.</i>
"""


async def allcommands_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin_or_owner(update.effective_user.id) and not await is_chat_creator(update, context):
        await update.message.reply_text(
            "🚫 This command is for the bot owner or this group's creator."
        )
        return
    text = _allcommands_text()
    await reply_html_in_parts(update.message, text)


@owner_only
async def invitereward_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        current = await get_invite_reward_amount()
        await update.message.reply_text(f"{DOLLAR} Current Invite & Earn reward: {DOLLAR}{current}")
        return

    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Usage: /invitereward <amount> - a plain integer.")
        return

    if amount < 0:
        await update.message.reply_text("❌ Amount must be zero or positive.")
        return

    await asyncio.to_thread(db.set_config, "invite_reward_amount", str(amount))
    await update.message.reply_text(f"✅ Invite & Earn reward set to {DOLLAR}{amount}.")


async def claim_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == ChatType.PRIVATE:
        await update.message.reply_text(
            "📍 Run /claim inside the group you want to claim the Invite & Earn reward for."
        )
        return

    existing_count = await asyncio.to_thread(db.count_invite_rewards_for_chat, chat.id)
    if existing_count > 0:
        await update.message.reply_text(
            "ℹ️ This group is already being tracked for Invite & Earn (or has already been "
            "paid), so /claim doesn't apply here."
        )
        return

    stint_info = await asyncio.to_thread(db.get_group_current_stint, chat.id)
    if not stint_info or not stint_info[1]:
        await update.message.reply_text(
            "⚠️ I couldn't find how long I've been in this group yet - try again shortly."
        )
        return

    title, stint_started_at, _left_at = stint_info
    try:
        started_dt = datetime.fromisoformat(stint_started_at)
    except (TypeError, ValueError):
        started_dt = datetime.now()

    remaining_seconds = INVITE_REWARD_SECONDS - (datetime.now() - started_dt).total_seconds()
    if remaining_seconds > 0:
        await update.message.reply_text(
            f"⏳ Not quite yet - I need to be in this group for a full "
            f"{INVITE_REWARD_HOURS} hours before /claim works here. "
            f"About {_format_duration_words(int(remaining_seconds))} left."
        )
        return

    try:
        member_count = await context.bot.get_chat_member_count(chat.id)
    except Exception:
        await update.message.reply_text("⚠️ Couldn't check the member count right now - try again in a moment.")
        return

    if member_count < INVITE_REWARD_MIN_MEMBERS:
        await update.message.reply_text(
            f"👥 This group needs at least {INVITE_REWARD_MIN_MEMBERS} members to qualify "
            f"(currently {member_count})."
        )
        return

    amount = await get_invite_reward_amount()
    now_iso = datetime.now().isoformat()

    new_id = await asyncio.to_thread(
        db.create_rewarded_invite_direct,
        chat.id,
        chat.title or title or str(chat.id),
        user.id,
        user.first_name,
        member_count,
        stint_started_at,
        amount,
        now_iso,
    )

    if not new_id:
        await update.message.reply_text(
            "ℹ️ Someone already claimed the Invite & Earn reward for this group."
        )
        return

    new_balance = await asyncio.to_thread(db.scramble_adjust_global_earned, user.id, user.first_name, amount)
    await send_invite_claim_message(
        context, user.id, user.first_name, chat.title or title or str(chat.id), amount, new_balance
    )
    await update.message.reply_text("✅ Reward claimed - check your DM with me!")


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    player = await asyncio.to_thread(db.get_user, user.id)
    if not player:
        await asyncio.to_thread(db.add_user, user.id, user.first_name)
        player = await asyncio.to_thread(db.get_user, user.id)

    await update.message.reply_html(f"""
👤 <b>Your Profile</b>

🆔 User ID: <code>{player[0]}</code>

👤 Name: {esc_name(player[1])}

💵 Balance: {player[2]}
⭐ Level: {player[3]}
🏅 Rank: {player[4]}
""")


async def inviteandearn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = await build_invite_earn_text()
    keyboard = await build_invite_earn_keyboard(context, user.id)
    await update.message.reply_html(text, reply_markup=keyboard)


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


# =======================================================================
# WORD SCRAMBLE TOURNAMENT (/settournament, /tournament)
# =======================================================================

TOURNAMENT_LOBBY_SECONDS = 120
TOURNAMENT_ROUND_SECONDS = 60
TOURNAMENT_POINTS_PER_WIN = 5
TOURNAMENT_MIN_FEE = 5
TOURNAMENT_QUICK_FEE = 5
TOURNAMENT_QUICK_DIFFICULTY = "easy"
TOURNAMENT_QUICK_ROUNDS = 20
TOURNAMENT_MAX_FEE = 100000
TOURNAMENT_MIN_ROUNDS = 1
TOURNAMENT_MAX_ROUNDS = 20
TOURNAMENT_DIFFICULTIES = ("easy", "easy3", "easy4", "easy5", "easy6", "medium", "hard", "off")
TOURNAMENT_START_RETRY_SECONDS = 8
TOURNAMENT_START_MAX_ATTEMPTS = 30
# A round/idle gap that overran by no more than this at startup is caught up as
# before; beyond it the tournament is PAUSED (stakes kept) instead of burning a round.
TOURNAMENT_RESUME_GRACE_SECONDS = 90
# A paused tournament that nobody resumes is refunded and finalized after this long.
TOURNAMENT_PAUSED_MAX_SECONDS = 24 * 3600
TOURNAMENT_PAUSE_CHECK_SECONDS = 600
TOURNAMENT_PAUSE_CHECK_JOB_NAME = "tournament_pause_check"
TOURNAMENT_PAUSED_BLOCK_TEXT = (
    "🏟️ A tournament is paused in this group because of a technical issue - entry fees are safe. "
    "The host or an admin can send /resumetournament to continue it (or /stoptournament to cancel and refund)."
)
TOURNAMENT_USAGE = (
    "Usage: /settournament <fee> <difficulty> <rounds>rounds\n"
    "Example: /settournament 2000 Medium 3rounds\n"
    "Too lazy to type it all? /tournament starts one instantly "
    f"({TOURNAMENT_QUICK_FEE} fee, {TOURNAMENT_QUICK_DIFFICULTY}, {TOURNAMENT_QUICK_ROUNDS} rounds).\n\n"
    f"Fee: {TOURNAMENT_MIN_FEE:,}-{TOURNAMENT_MAX_FEE:,} • Rounds: {TOURNAMENT_MIN_ROUNDS}-{TOURNAMENT_MAX_ROUNDS}\n"
    "Difficulty: easy, easy3-easy6, medium, hard, off"
)


def _tournament_lobby_job_name(tid):
    return f"tournament_lobby_{tid}"


def _tournament_round_job_name(tid):
    return f"tournament_round_{tid}"


def _tournament_advance_job_name(tid):
    return f"tournament_advance_{tid}"


def _cancel_tournament_jobs(job_queue, tid, lobby=True, round_=True, advance=True):
    if job_queue is None:
        return
    names = []
    if lobby:
        names.append(_tournament_lobby_job_name(tid))
    if round_:
        names.append(_tournament_round_job_name(tid))
    if advance:
        names.append(_tournament_advance_job_name(tid))
    for name in names:
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()


def _schedule_tournament_job(job_queue, callback, name, seconds, chat_id, data):
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    job_queue.run_once(callback, seconds, chat_id=chat_id, name=name, data=data)


def tournament_random_word(chat_id, difficulty):
    """Same machinery as scramble_random_word, but the difficulty is passed in
    explicitly - the group's own difficulty setting is never read or changed.
    Shares the chat's used-words tracking so words don't repeat."""
    if not SCRAMBLE_WORDS:
        return None
    pool = _get_difficulty_pool("scramble", SCRAMBLE_WORDS, difficulty)
    if not pool:
        return None

    used = db.scramble_get_used_words(chat_id)
    choices = [w for w in pool if w not in used]
    if not choices:
        db.scramble_reset_used_words(chat_id)
        choices = list(pool)

    word = random.choice(choices)
    db.scramble_mark_word_used(chat_id, word)
    return word


def tournament_prize(fee, player_count) -> int:
    """Advertised (and actually paid) prize: every entry fee plus one bonus
    entry fee from the bot. Must match the pot formula in db.tournament_settle."""
    return fee * (player_count + 1)


def _tournament_lobby_remaining(t) -> float:
    """Seconds until the lobby closes (base 120s + any /extendlobby time)."""
    try:
        elapsed = (datetime.now() - datetime.fromisoformat(t["created_at"])).total_seconds()
    except (TypeError, ValueError):
        elapsed = 0
    return TOURNAMENT_LOBBY_SECONDS + (t.get("lobby_extra_seconds") or 0) - elapsed


def tournament_lobby_text(t, players) -> str:
    minutes = max(1, math.ceil(_tournament_lobby_remaining(t) / 60))
    unit = "minute" if minutes == 1 else "minutes"
    lines = [
        "SCRAMBLE TOURNAMENT 🏟️",
        "",
        f"Prize: {DOLLAR}{tournament_prize(t['fee'], len(players))}",
        f"Mode: {esc((t['difficulty'] or 'off').capitalize())}",
        f"Rounds: {t['total_rounds']}",
        f"Entry fee: {DOLLAR}{t['fee']}",
        "",
        "Send /join to enter.",
        f"⏳ Lobby closes in {minutes} {unit}.",
    ]
    return "\n".join(lines)


def tournament_standings_text(rows) -> str:
    """rows: iterable of (name, score) - sorted here, highest first."""
    ordered = sorted(rows, key=lambda r: -r[1])
    lines = ["Tournament 🏟️", ""]
    lines += [f"{esc_name(name)} 💎 {score}" for name, score in ordered]
    return "\n".join(lines)


async def _tournament_post_lobby(context, tid):
    t = await asyncio.to_thread(db.tournament_get, tid)
    if not t or t["status"] != "lobby":
        return
    players = await asyncio.to_thread(db.tournament_get_players, tid)
    text = tournament_lobby_text(t, players)

    if t.get("lobby_message_id"):
        try:
            await context.bot.edit_message_text(
                chat_id=t["chat_id"], message_id=t["lobby_message_id"], text=text, parse_mode="HTML"
            )
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            # message gone / not editable - repost below
        except Exception:
            pass  # repost below

    sent = await safe_send(context.bot, t["chat_id"], text=text, parse_mode="HTML")
    if sent:
        await asyncio.to_thread(db.tournament_set_lobby_message, tid, sent.message_id)


async def _tournament_announce_join(context, chat_id, user):
    """'<Name> joined ✅' with the name as a tappable (blue) mention."""
    await safe_send(
        context.bot, chat_id,
        text=f'<a href="tg://user?id={user.id}">{esc_name(user.first_name)}</a> joined ✅',
        parse_mode="HTML",
    )


def _parse_bbb_args(args):
    """Returns (fee, difficulty, rounds, error). Rounds token may be '3rounds',
    '3 rounds' (two args) or just '3'."""
    if len(args) < 3 or not re.fullmatch(r"\d+", args[0]):
        return None, None, None, TOURNAMENT_USAGE
    fee = int(args[0])
    difficulty = args[1].lower()
    m = re.fullmatch(r"(\d+)(rounds?)?", "".join(args[2:]).lower())
    if not m:
        return None, None, None, TOURNAMENT_USAGE
    rounds = int(m.group(1))

    if difficulty not in TOURNAMENT_DIFFICULTIES:
        return None, None, None, "❌ Difficulty must be one of: easy, easy3, easy4, easy5, easy6, medium, hard, off."
    if not (TOURNAMENT_MIN_FEE <= fee <= TOURNAMENT_MAX_FEE):
        return None, None, None, f"❌ The entry fee must be between {TOURNAMENT_MIN_FEE:,} and {TOURNAMENT_MAX_FEE:,}."
    if not (TOURNAMENT_MIN_ROUNDS <= rounds <= TOURNAMENT_MAX_ROUNDS):
        return None, None, None, f"❌ Rounds must be between {TOURNAMENT_MIN_ROUNDS} and {TOURNAMENT_MAX_ROUNDS}."
    return fee, difficulty, rounds, None


async def settournament_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("🏟️ Tournaments can only be started inside a group.")
        return

    fee, difficulty, rounds, error = _parse_bbb_args(context.args)
    if error:
        await update.message.reply_text(error)
        return

    await _open_tournament_lobby(update, context, fee, difficulty, rounds)


async def tournament_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lazy-typer shortcut: identical to /settournament 5 easy 20rounds."""
    await _open_tournament_lobby(
        update, context,
        TOURNAMENT_QUICK_FEE, TOURNAMENT_QUICK_DIFFICULTY, TOURNAMENT_QUICK_ROUNDS,
    )


async def _open_tournament_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE, fee, difficulty, rounds):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("🏟️ Tournaments can only be started inside a group.")
        return

    if await asyncio.to_thread(db.is_tournament_banned, "group", chat.id):
        await update.message.reply_text("🚫 This group isn't allowed to run tournaments.")
        return
    if await asyncio.to_thread(db.is_tournament_banned, "user", user.id):
        await update.message.reply_text("🚫 You're banned from tournaments.")
        return

    if await asyncio.to_thread(db.get_config, "scramble_maintenance_mode", "0") == "1":
        await update.message.reply_text("🛠️ Word Scramble is currently under maintenance. Please try again later.")
        return

    _open_t = await asyncio.to_thread(db.tournament_get_open_for_chat, chat.id)
    if _open_t:
        if _open_t["status"] == "paused":
            await update.message.reply_text(TOURNAMENT_PAUSED_BLOCK_TEXT)
        else:
            await update.message.reply_text("🏟️ A tournament is already running (or open for joining) in this group.")
        return
    if await asyncio.to_thread(db.scramble_get_round, chat.id):
        await update.message.reply_text(
            "🎮 A Word Scramble round is in progress here - wait for it to end before starting a tournament."
        )
        return

    status, tid, _balance = await asyncio.to_thread(
        db.tournament_create, chat.id, user.id, user.first_name, fee, difficulty, rounds
    )
    if status == "insufficient":
        await update.message.reply_text(
            f"❌ Your Word Scramble balance is below the entry fee ({DOLLAR}{fee})."
        )
        return
    if status == "exists":
        await update.message.reply_text("🏟️ A tournament is already running (or open for joining) in this group.")
        return

    await _tournament_post_lobby(context, tid)
    await _tournament_announce_join(context, chat.id, user)
    _schedule_tournament_job(
        context.job_queue, tournament_lobby_close_callback,
        _tournament_lobby_job_name(tid), TOURNAMENT_LOBBY_SECONDS, chat.id, {"tid": tid},
    )


async def join_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("🏟️ /join only works inside a group with an open tournament.")
        return

    t = await asyncio.to_thread(db.tournament_get_open_for_chat, chat.id)
    if not t or t["status"] != "lobby":
        await update.message.reply_text("🏟️ There's no tournament open for joining in this group right now.")
        return

    if await asyncio.to_thread(db.is_tournament_banned, "user", user.id):
        await update.message.reply_text("🚫 You're banned from tournaments.")
        return

    status, _balance = await asyncio.to_thread(db.tournament_join, t["id"], user.id, user.first_name)
    if status == "closed":
        await update.message.reply_text("🏟️ The lobby has just closed - too late to join.")
    elif status == "already":
        await update.message.reply_text("✅ You've already joined this tournament.")
    elif status == "insufficient":
        await update.message.reply_text(
            f"❌ Your Word Scramble balance is below the entry fee ({DOLLAR}{t['fee']})."
        )
    else:
        await safe_react(context.bot, chat.id, update.message.message_id, "👍")
        await _tournament_announce_join(context, chat.id, user)
        await _tournament_post_lobby(context, t["id"])  # edits the first message: prize goes up by the new fee


async def _tournament_may_manage(user_id, t) -> bool:
    return user_id == t["host_id"] or await asyncio.to_thread(is_bot_admin_or_owner, user_id)


async def starttournament_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    t = await asyncio.to_thread(db.tournament_get_open_for_chat, chat.id)
    if not t or t["status"] != "lobby":
        await update.message.reply_text("🏟️ There's no tournament lobby open in this group.")
        return
    if not await _tournament_may_manage(update.effective_user.id, t):
        await update.message.reply_text("❌ Only the tournament host, the bot owner, or Bot Admins can start it early.")
        return
    await _tournament_start(context, t["id"])


@owner_only
async def extendlobby_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    usage = "Usage: /extendlobby <2-9>m  (minutes, e.g. /extendlobby 5m) - run it in the group whose lobby is open."

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("🏟️ Run /extendlobby inside the group whose tournament lobby is open.")
        return

    m = re.fullmatch(r"([2-9])m?", (context.args[0].lower() if context.args else ""))
    if len(context.args) != 1 or not m:
        await update.message.reply_text(usage)
        return
    minutes = int(m.group(1))

    t = await asyncio.to_thread(db.tournament_get_open_for_chat, chat.id)
    if not t or t["status"] != "lobby":
        await update.message.reply_text("🏟️ There's no tournament lobby open in this group.")
        return

    extra = await asyncio.to_thread(db.tournament_extend_lobby, t["id"], minutes * 60)
    if extra is None:
        await update.message.reply_text("🏟️ That lobby has just closed - too late to extend it.")
        return

    t["lobby_extra_seconds"] = extra
    _schedule_tournament_job(
        context.job_queue, tournament_lobby_close_callback,
        _tournament_lobby_job_name(t["id"]), max(_tournament_lobby_remaining(t), 1), chat.id, {"tid": t["id"]},
    )
    await _tournament_post_lobby(context, t["id"])
    await update.message.reply_text(f"⏳ Lobby extended by {minutes} minute(s).")


async def stoptournament_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    t = await asyncio.to_thread(db.tournament_get_open_for_chat, chat.id)
    if not t or t["status"] == "active":
        text = "🏟️ There's no tournament lobby to cancel here (a tournament that has already started can't be cancelled)."
        if t:
            text += " If it has stopped responding, try /resumetournament."
        await update.message.reply_text(text)
        return
    if not await _tournament_may_manage(update.effective_user.id, t):
        await update.message.reply_text("❌ Only the tournament host, the bot owner, or Bot Admins can cancel it.")
        return
    if t["status"] == "paused":
        # Paused tournaments can be cancelled: everyone is refunded in full.
        await _tournament_abort_and_announce(
            context, t["id"], chat.id, "Tournament cancelled."
        )
        return
    await _tournament_abort_and_announce(
        context, t["id"], chat.id, "Tournament cancelled.", delete=True
    )


async def _tournament_abort_and_announce(context, tid, chat_id, headline, delete=False,
                                         dm_players=False, group_title=None):
    """Refund everyone still staked, finalize (or delete) the tournament, and
    tell the chat. Idempotent - returns None if there was nothing open."""
    res = await asyncio.to_thread(db.tournament_abort, tid, delete)
    _cancel_tournament_jobs(context.job_queue, tid)
    if not res:
        return None

    n = len(res["refunds"])
    text = f"❌ {headline}\n\nAll {n} player(s) were refunded their {DOLLAR}{res['fee']} entry fee."
    try:
        await safe_send(context.bot, chat_id, text=text)
    except Exception as e:
        logger.info(f"tournament abort notice to {chat_id} not delivered: {e}")

    if dm_players:
        where = f" in {group_title}" if group_title else ""
        for uid, _name, fee, _bal in res["refunds"]:
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"↩️ The tournament{where} was cancelled - your {DOLLAR}{fee} entry fee has been refunded.",
                )
            except Exception:
                pass
            await asyncio.sleep(0.05)
    return res


async def tournament_lobby_close_callback(context: ContextTypes.DEFAULT_TYPE):
    tid = (context.job.data or {}).get("tid")
    if tid is not None:
        await _tournament_start(context, tid)


async def _tournament_start(context, tid):
    """Shared by the lobby-close job and /starttournament."""
    t = await asyncio.to_thread(db.tournament_get, tid)
    if not t or t["status"] != "lobby":
        return
    chat_id = t["chat_id"]
    _cancel_tournament_jobs(context.job_queue, tid, round_=False, advance=False)

    players = await asyncio.to_thread(db.tournament_get_players, tid)
    if len(players) < 2:
        res = await asyncio.to_thread(db.tournament_abort, tid, True)
        if res:
            await safe_send(
                context.bot, chat_id,
                text=f"😔 No one joined the tournament, so it's been cancelled. "
                     f"{DOLLAR}{t['fee']} was refunded to the host.",
            )
        return

    if not await asyncio.to_thread(db.tournament_activate, tid):
        return  # someone else already started it

    await safe_send(
        context.bot, chat_id,
        text=(
            f"🏟️ <b>Tournament starting!</b>\n\n"
            f"Players: {len(players)}\n"
            f"Rounds: {t['total_rounds']}\n"
            f"Difficulty: {esc((t['difficulty'] or 'off').capitalize())}\n"
            f"Prize: {DOLLAR}{tournament_prize(t['fee'], len(players))}\n\n"
            f"First correct answer each round scores 💎{TOURNAMENT_POINTS_PER_WIN}. Get ready!"
        ),
        parse_mode="HTML",
    )
    await _tournament_start_round(context, tid)


async def _tournament_start_round(context, tid, attempt=1):
    t = await asyncio.to_thread(db.tournament_get, tid)
    if not t or t["status"] != "active" or t["current_word"]:
        return
    chat_id = t["chat_id"]

    if t["current_round"] >= t["total_rounds"]:
        await _tournament_finish(context, tid)
        return

    word = await asyncio.to_thread(tournament_random_word, chat_id, t["difficulty"])
    if not word:
        await _tournament_abort_and_announce(
            context, tid, chat_id, "Tournament cancelled - the Word Scramble word list is empty."
        )
        return

    scrambled = scramble_letters(word)
    round_no = await asyncio.to_thread(db.tournament_set_round, tid, word, scrambled)
    if round_no is None:
        return

    try:
        sent = await safe_send(
            context.bot, chat_id,
            text=(
                f"🏟️ <b>Round {round_no}/{t['total_rounds']}</b>\n\n"
                f"Unscramble this word:\n\n"
                f"<b>{esc(scrambled)}</b>\n\n"
                f"First correct answer in {TOURNAMENT_ROUND_SECONDS} seconds scores 💎{TOURNAMENT_POINTS_PER_WIN}."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"tournament {tid}: sending round {round_no} failed: {e}")
        sent = None

    if sent is None:
        # The group never saw the word: roll the round back and retry shortly.
        await asyncio.to_thread(db.tournament_unset_round, tid, round_no)
        if attempt >= TOURNAMENT_START_MAX_ATTEMPTS:
            logger.error(f"tournament {tid}: giving up after {attempt} failed round sends - pausing (stakes kept)")
            await _tournament_pause_and_notify(context, tid)
            return
        logger.warning(f"tournament {tid}: round {round_no} not delivered; retrying in {TOURNAMENT_START_RETRY_SECONDS}s")
        _schedule_tournament_job(
            context.job_queue, tournament_advance_callback, _tournament_advance_job_name(tid),
            TOURNAMENT_START_RETRY_SECONDS, chat_id, {"tid": tid, "attempt": attempt + 1},
        )
        return

    _schedule_tournament_job(
        context.job_queue, tournament_round_timeout_callback, _tournament_round_job_name(tid),
        TOURNAMENT_ROUND_SECONDS, chat_id, {"tid": tid},
    )


def _tournament_rearm_advance(context, tid, chat_id, seconds, attempt=1):
    """Queue (or re-queue) the job that moves a tournament forward. Safe to
    call any number of times - the advance callback re-checks the real state."""
    try:
        _schedule_tournament_job(
            context.job_queue, tournament_advance_callback, _tournament_advance_job_name(tid),
            seconds, chat_id, {"tid": tid, "attempt": attempt},
        )
    except Exception:
        logger.exception(f"tournament {tid}: could not re-arm advance job")


async def tournament_advance_callback(context: ContextTypes.DEFAULT_TYPE):
    """Moves an active tournament forward when no round is live: starts the
    next round, or finishes it. Used for send retries and restart catch-up."""
    data = context.job.data or {}
    tid, attempt = data.get("tid"), data.get("attempt", 1)
    if tid is None:
        return
    try:
        t = await asyncio.to_thread(db.tournament_get, tid)
        if not t or t["status"] != "active" or t["current_word"]:
            return
        if t["current_round"] >= t["total_rounds"]:
            await _tournament_finish(context, tid)
        else:
            await _tournament_start_round(context, tid, attempt)
    except Exception:
        # A DB/Telegram hiccup used to leave the tournament stuck between
        # rounds forever. Try again shortly (the watchdog is the backstop).
        logger.exception(f"tournament {tid}: advance step failed; retrying")
        _tournament_rearm_advance(context, tid, getattr(context.job, "chat_id", None), 8, attempt + 1)


async def _tournament_after_round(context, tid, round_no, total_rounds):
    if round_no >= total_rounds:
        await _tournament_finish(context, tid)
    else:
        await _tournament_start_round(context, tid)  # no pause between rounds


async def tournament_round_timeout_callback(context: ContextTypes.DEFAULT_TYPE):
    tid = (context.job.data or {}).get("tid")
    if tid is None:
        return
    t = await asyncio.to_thread(db.tournament_get, tid)
    if not t:
        return

    claim = await asyncio.to_thread(db.tournament_claim_round, tid)
    if not claim:
        return  # a correct guess (or a stop) already closed this round

    try:
        players = await asyncio.to_thread(db.tournament_get_players, tid)
        await safe_send(
            context.bot, t["chat_id"],
            text=(
                f"⏰ Time's up! Nobody got it - the word was <b>{esc(claim['word'])}</b>.\n\n"
                + tournament_standings_text([(p[1], p[2]) for p in players])
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception(f"tournament {tid}: time-up announcement failed; continuing anyway")
    try:
        await _tournament_after_round(context, tid, claim["round_no"], claim["total_rounds"])
    except Exception:
        logger.exception(f"tournament {tid}: moving to the next round failed; re-arming")
        _tournament_rearm_advance(context, tid, t["chat_id"], 3)


async def tournament_handle_guess(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if this message was consumed by an active tournament round.
    Non-tournament chats, non-joined players, and messages between rounds
    return False and fall through untouched to the normal Word Scramble path."""
    message = update.message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not message.text or not user:
        return False

    t = await asyncio.to_thread(db.tournament_get_open_for_chat, chat.id)
    if not t or t["status"] != "active" or not t["current_word"]:
        return False

    if not await asyncio.to_thread(db.tournament_is_staked_player, t["id"], user.id):
        return False

    # Messages redelivered after an outage were typed BEFORE this round (or its
    # resumed restart) began - they must not steal the word or score points.
    if _tournament_message_is_stale(message, t):
        return True

    guess_clean = re.sub(r"[^\w\s]", "", message.text.lower()).strip()
    if not guess_clean:
        return True

    word_lower = t["current_word"].lower()
    if guess_clean != word_lower:
        if is_close_guess(guess_clean, word_lower):
            await safe_react(context.bot, chat.id, message.message_id, "👀")
        return True

    claim = await asyncio.to_thread(
        db.tournament_claim_round, t["id"], user.id, TOURNAMENT_POINTS_PER_WIN
    )
    if not claim:
        return True  # someone else won this round a moment earlier

    _cancel_tournament_jobs(context.job_queue, t["id"], lobby=False, advance=False)
    await safe_react(context.bot, chat.id, message.message_id, "🎉")

    try:
        players = await asyncio.to_thread(db.tournament_get_players, t["id"])
        await safe_send(
            context.bot, chat.id,
            text=(
                f"🎉 <b>{esc_name(user.first_name)}</b> got it - <b>{esc(claim['word'])}</b>! "
                f"+💎{TOURNAMENT_POINTS_PER_WIN}\n\n"
                + tournament_standings_text([(p[1], p[2]) for p in players])
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception(f"tournament {t['id']}: winner announcement failed; continuing anyway")
    try:
        await _tournament_after_round(context, t["id"], claim["round_no"], claim["total_rounds"])
    except Exception:
        logger.exception(f"tournament {t['id']}: moving to the next round failed; re-arming")
        _tournament_rearm_advance(context, t["id"], chat.id, 3)
    return True


# =======================================================================
# WATCHDOG - the safety net behind Automatic Mode and Tournaments.
# Every chain above already retries on failure; this job guarantees that even
# if a timer was dropped, a deploy/restart happened mid-gap, or something
# unforeseen went wrong, a stalled chat gets moving again within ~30 seconds.
# =======================================================================

SCRAMBLE_WATCHDOG_INTERVAL = 20
SCRAMBLE_WATCHDOG_JOB_NAME = "scramble_watchdog"
SCRAMBLE_WATCHDOG_IDLE_SECONDS = 30
TOURNAMENT_WATCHDOG_IDLE_SECONDS = 25


async def tournament_watchdog_round_timeout_callback(context: ContextTypes.DEFAULT_TYPE):
    """Re-armed round timer. Only fires the real timeout if the SAME round is
    still live, so it can never cut a newer round short."""
    data = context.job.data or {}
    t = await asyncio.to_thread(db.tournament_get, data.get("tid"))
    if not t or t["status"] != "active" or not t["current_word"]:
        return
    if t["current_round"] != data.get("round_no"):
        return
    await tournament_round_timeout_callback(context)


async def _scramble_watchdog_scan(context: ContextTypes.DEFAULT_TYPE):
    jq = context.job_queue
    if jq is None:
        return
    now_mono = time.monotonic()
    now_dt = datetime.now()

    # 1) Word Scramble rounds whose timeout timer went missing.
    rounds = await asyncio.to_thread(db.get_all_active_scramble_rounds_with_time)
    active_chats = set()
    for chat_id, started_at in rounds:
        active_chats.add(chat_id)
        try:
            age = (now_dt - datetime.fromisoformat(started_at)).total_seconds()
        except (TypeError, ValueError):
            age = SCRAMBLE_ROUND_SECONDS + 60
        if age > SCRAMBLE_ROUND_SECONDS + 15 and not jq.get_jobs_by_name(_scramble_timeout_job_name(chat_id)):
            logger.warning(f"watchdog: round in {chat_id} is {age:.0f}s old with no timer; expiring it")
            jq.run_once(
                scramble_timeout_callback, 1, chat_id=chat_id,
                name=_scramble_timeout_job_name(chat_id),
            )

    # 2) Automatic Mode chats that have no live round and nothing queued.
    auto_chats = await asyncio.to_thread(db.scramble_get_auto_enabled_chats)
    for chat_id in auto_chats:
        if chat_id in active_chats:
            continue
        idle = now_mono - _scramble_last_activity.get(chat_id, _PROCESS_BOOT_MONOTONIC)
        if idle < SCRAMBLE_WATCHDOG_IDLE_SECONDS:
            continue
        if _scramble_start_lock(chat_id).locked():
            continue
        if jq.get_jobs_by_name(_scramble_send_retry_job_name(chat_id)) or jq.get_jobs_by_name(_slowmode_next_job_name(chat_id)):
            continue
        slow = await asyncio.to_thread(db.get_scramble_slowmode, chat_id)
        if slow and slow[1]:
            continue
        if await asyncio.to_thread(db.tournament_get_open_for_chat, chat_id):
            continue  # a tournament owns this chat right now; auto resumes after it

        try:
            chat = await context.bot.get_chat(chat_id)
        except Forbidden:
            logger.warning(f"watchdog: bot can't access {chat_id}; turning Automatic Mode off there")
            await asyncio.to_thread(db.scramble_set_auto_enabled, chat_id, False)
            continue
        except BadRequest as e:
            if "not found" in str(e).lower():
                logger.warning(f"watchdog: chat {chat_id} not found; turning Automatic Mode off there")
                await asyncio.to_thread(db.scramble_set_auto_enabled, chat_id, False)
            continue
        except Exception:
            continue

        logger.warning(f"watchdog: Automatic Mode in {chat_id} was idle {idle:.0f}s with no round; restarting it")
        try:
            result = await start_scramble_round(context, chat)
        except Forbidden:
            await asyncio.to_thread(db.scramble_set_auto_enabled, chat_id, False)
            continue
        except Exception:
            logger.exception(f"watchdog: could not restart round in {chat_id}")
            _scramble_touch(chat_id)
            continue
        if result == "send_failed":
            schedule_scramble_send_retry(jq, chat_id, "auto")
        elif result in (False, None, "maintenance"):
            _scramble_touch(chat_id)
        await asyncio.sleep(0.3)

    # 3) Tournaments stuck mid-flight.
    for t in await asyncio.to_thread(db.tournament_get_all_open):
        if t["status"] != "active":
            continue
        tid, chat_id = t["id"], t["chat_id"]
        round_job = jq.get_jobs_by_name(_tournament_round_job_name(tid))
        advance_job = jq.get_jobs_by_name(_tournament_advance_job_name(tid))

        if t["current_word"]:
            if round_job:
                continue
            try:
                age = (now_dt - datetime.fromisoformat(t["round_started_at"])).total_seconds()
            except (TypeError, ValueError):
                age = TOURNAMENT_ROUND_SECONDS + 1
            if age < 10:
                continue
            logger.warning(f"watchdog: tournament {tid} round has no timer; re-arming")
            _schedule_tournament_job(
                jq, tournament_watchdog_round_timeout_callback, _tournament_round_job_name(tid),
                max(TOURNAMENT_ROUND_SECONDS - age, 1), chat_id,
                {"tid": tid, "round_no": t["current_round"]},
            )
        else:
            if round_job or advance_job:
                continue
            try:
                idle = (now_dt - datetime.fromisoformat(t["idle_since"])).total_seconds()
            except (TypeError, ValueError):
                continue
            if idle < TOURNAMENT_WATCHDOG_IDLE_SECONDS:
                continue
            logger.warning(f"watchdog: tournament {tid} idle {idle:.0f}s between rounds; advancing it")
            _tournament_rearm_advance(context, tid, chat_id, 1)


async def scramble_watchdog_callback(context: ContextTypes.DEFAULT_TYPE):
    try:
        await _scramble_watchdog_scan(context)
    except Exception:
        logger.exception("scramble watchdog scan failed (will run again)")


def schedule_scramble_watchdog(job_queue):
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(SCRAMBLE_WATCHDOG_JOB_NAME):
        job.schedule_removal()
    job_queue.run_repeating(
        scramble_watchdog_callback,
        interval=SCRAMBLE_WATCHDOG_INTERVAL,
        first=25,
        name=SCRAMBLE_WATCHDOG_JOB_NAME,
    )


async def scramble_group_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Layered in front of scramble_group_text (group=1): an active tournament
    gets first look at the message; everything else falls through unchanged."""
    if await tournament_handle_guess(update, context):
        return
    await scramble_group_text(update, context)


async def _tournament_finish(context, tid):
    _cancel_tournament_jobs(context.job_queue, tid)
    res = await asyncio.to_thread(db.tournament_settle, tid)
    if not res:
        return  # already settled

    chat_id = res["chat_id"]
    standings = tournament_standings_text([(name, score) for _uid, name, score in res["standings"]])
    lines = ["🏆 <b>TOURNAMENT OVER</b> 🏆", "", standings, ""]

    if res["case"] == "single":
        uid, name, payout, balance = res["winners"][0]
        lines.append(f"👑 Winner: <b>{esc_name(name)}</b> takes the whole prize - {DOLLAR}{payout}!")
        lines.append(f"New balance: {DOLLAR}{balance}")
    elif res["case"] == "tie":
        lines.append("🤝 It's a tie! The prize is split:")
        for uid, name, payout, balance in res["winners"]:
            lines.append(f"• <b>{esc_name(name)}</b> - {DOLLAR}{payout} (new balance {DOLLAR}{balance})")
    else:
        lines.append(
            f"😴 Nobody answered a single word, so there's no winner. "
            f"Every player was refunded their {DOLLAR}{res['fee']} entry fee."
        )

    await safe_send(context.bot, chat_id, text="\n".join(lines), parse_mode="HTML")


def _tournament_message_is_stale(message, t) -> bool:
    """True if the Telegram message was sent before the live round's
    round_started_at. Telegram dates are UTC-aware; the DB stores naive LOCAL
    datetime.now() strings, so both are normalised to aware UTC first."""
    try:
        # Telegram's message.date only has whole-second resolution, so compare
        # against the round start floored to the second (a legit fast guess in
        # the same second as the round start must never be treated as stale).
        started = datetime.fromisoformat(t["round_started_at"]).replace(microsecond=0).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return False
    sent = getattr(message, "date", None)
    if sent is None:
        return False
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    return sent < started


def _tournament_resume_position(t):
    """(round_number_to_continue_from, total_rounds, finishing)."""
    total = t["total_rounds"]
    if t["current_word"]:
        return t["current_round"], total, False
    if t["current_round"] >= total:
        return total, total, True
    return t["current_round"] + 1, total, False


def _tournament_paused_job_name(tid):
    return f"tournament_pausednotice_{tid}"


def _group_title_sync(chat_id):
    try:
        d = db.get_group_full_details(chat_id)
        return d[1] if d and d[1] else None
    except Exception:
        return None


async def _tournament_send_paused_notices(context, t):
    """ONE notice in the group + best-effort DMs to the host and the owner."""
    rn, total, finishing = _tournament_resume_position(t)
    where = f"Round {rn}/{total}" if not finishing else "the final results"
    try:
        await safe_send(
            context.bot, t["chat_id"],
            text=(
                "⏸️ <b>Tournament paused</b> because of a technical issue.\n\n"
                f"All entry fees are safe. The host or an admin can send /resumetournament "
                f"to continue from {where}."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.info(f"tournament {t['id']}: paused notice to group not delivered: {e}")

    title = await asyncio.to_thread(_group_title_sync, t["chat_id"])
    in_group = f" in {title}" if title else ""
    dm = (
        f"⏸️ The tournament{in_group} is paused because of a technical issue. "
        f"Entry fees are safe. Send /resumetournament in that group to continue from {where}."
    )
    for uid in {t["host_id"], OWNER_ID}:
        try:
            await context.bot.send_message(chat_id=uid, text=dm)
        except Exception:
            pass


async def _tournament_pause_and_notify(context, tid):
    """active -> paused (stakes, word, round and scores untouched) + notices.
    Returns True if this call paused it."""
    paused = await asyncio.to_thread(db.tournament_pause, tid)
    _cancel_tournament_jobs(context.job_queue, tid)
    if not paused:
        return False
    t = await asyncio.to_thread(db.tournament_get, tid)
    if t:
        await _tournament_send_paused_notices(context, t)
    return True


async def tournament_paused_notice_callback(context: ContextTypes.DEFAULT_TYPE):
    tid = (context.job.data or {}).get("tid")
    if tid is None:
        return
    t = await asyncio.to_thread(db.tournament_get, tid)
    if t and t["status"] == "paused":
        await _tournament_send_paused_notices(context, t)


async def tournament_pause_check_callback(context: ContextTypes.DEFAULT_TYPE):
    """Safety net: a tournament paused for longer than TOURNAMENT_PAUSED_MAX_SECONDS
    is refunded in full and finalized, so money is never held forever."""
    try:
        open_ts = await asyncio.to_thread(db.tournament_get_all_open)
    except Exception as e:
        logger.error(f"tournament pause check: couldn't list tournaments: {e}")
        return
    now = datetime.now()
    for t in open_ts:
        if t["status"] != "paused" or not t.get("paused_at"):
            continue
        try:
            age = (now - datetime.fromisoformat(t["paused_at"])).total_seconds()
        except (TypeError, ValueError):
            continue
        if age < TOURNAMENT_PAUSED_MAX_SECONDS:
            continue
        try:
            await _tournament_abort_and_announce(
                context, t["id"], t["chat_id"],
                f"Tournament cancelled - it stayed paused for over "
                f"{_format_duration_words(TOURNAMENT_PAUSED_MAX_SECONDS)}.",
                dm_players=True, group_title=await asyncio.to_thread(_group_title_sync, t["chat_id"]),
            )
        except Exception as e:
            logger.error(f"tournament {t['id']}: auto-refund of stale paused tournament failed: {e}")


def schedule_tournament_pause_check(job_queue):
    """Repeating, restart-safe (re-registered on every boot)."""
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(TOURNAMENT_PAUSE_CHECK_JOB_NAME):
        job.schedule_removal()
    job_queue.run_repeating(
        tournament_pause_check_callback,
        interval=TOURNAMENT_PAUSE_CHECK_SECONDS,
        first=5,
        name=TOURNAMENT_PAUSE_CHECK_JOB_NAME,
    )


def restore_stale_tournaments(job_queue):
    """On startup, re-arm every open tournament: reschedule the remaining lobby
    time or round timer, or fire the catch-up almost immediately if it only
    just elapsed. If a live round (or the gap between rounds) overran by MORE
    than TOURNAMENT_RESUME_GRACE_SECONDS - i.e. the bot was down/unreachable -
    the tournament is PAUSED instead (word, round, scores and stakes kept) and
    one notice is posted; /resumetournament continues it. Already-paused
    tournaments are left alone (the pause-check job refunds them if abandoned)."""
    if job_queue is None:
        return
    now = datetime.now()

    for t in db.tournament_get_all_open():
        tid, chat_id = t["id"], t["chat_id"]

        if t["status"] == "paused":
            continue

        if t["status"] == "lobby":
            try:
                remaining = _tournament_lobby_remaining(t)
            except (TypeError, ValueError):
                remaining = 0
            _schedule_tournament_job(
                job_queue, tournament_lobby_close_callback, _tournament_lobby_job_name(tid),
                max(remaining, 1), chat_id, {"tid": tid},
            )
        elif t["current_word"]:
            try:
                remaining = TOURNAMENT_ROUND_SECONDS - (now - datetime.fromisoformat(t["round_started_at"])).total_seconds()
            except (TypeError, ValueError):
                remaining = 0
            if -remaining > TOURNAMENT_RESUME_GRACE_SECONDS:
                if db.tournament_pause(tid):
                    _schedule_tournament_job(
                        job_queue, tournament_paused_notice_callback, _tournament_paused_job_name(tid),
                        2, chat_id, {"tid": tid},
                    )
                continue
            _schedule_tournament_job(
                job_queue, tournament_round_timeout_callback, _tournament_round_job_name(tid),
                max(remaining, 1), chat_id, {"tid": tid},
            )
        else:
            try:
                idle = (now - datetime.fromisoformat(t["idle_since"])).total_seconds()
            except (TypeError, ValueError):
                idle = 0  # unknown (older row) - keep the old catch-up behaviour
            if idle > TOURNAMENT_RESUME_GRACE_SECONDS:
                if db.tournament_pause(tid):
                    _schedule_tournament_job(
                        job_queue, tournament_paused_notice_callback, _tournament_paused_job_name(tid),
                        2, chat_id, {"tid": tid},
                    )
                continue
            _schedule_tournament_job(
                job_queue, tournament_advance_callback, _tournament_advance_job_name(tid),
                1, chat_id, {"tid": tid, "attempt": 1},
            )


async def _tournament_repause(context, tid):
    """Put a tournament back to 'paused' after a failed resume - silently, so
    it can always be resumed again."""
    await asyncio.to_thread(db.tournament_pause, tid)
    _cancel_tournament_jobs(context.job_queue, tid)


async def _tournament_resume_one(context, t):
    """Tries to resume one open tournament. Returns (code, data):
      'resumed' -> data = tournament dict     'healthy' -> data = tournament dict
      'none'    -> data = text                'failed'  -> data = text
    The state change is one atomic DB call (db.tournament_resume)."""
    tid, chat_id, jq = t["id"], t["chat_id"], context.job_queue

    if t["status"] == "lobby":
        return "none", "The tournament is still in its lobby - there's nothing to resume yet."

    if t["status"] == "active":
        job_name = _tournament_round_job_name(tid) if t["current_word"] else _tournament_advance_job_name(tid)
        if jq is not None and jq.get_jobs_by_name(job_name):
            return "healthy", t  # a timer is armed: it's running normally

    now = datetime.now()
    stale_round_cutoff = (now - timedelta(seconds=TOURNAMENT_ROUND_SECONDS + TOURNAMENT_RESUME_GRACE_SECONDS)).isoformat()
    idle_cutoff = (now - timedelta(seconds=TOURNAMENT_RESUME_GRACE_SECONDS)).isoformat()
    state = await asyncio.to_thread(db.tournament_resume, tid, stale_round_cutoff, idle_cutoff)
    if not state:
        fresh = await asyncio.to_thread(db.tournament_get, tid)
        if not fresh or fresh["status"] not in ("active", "paused"):
            return "none", "That tournament has already finished - there's nothing to resume."
        return "healthy", fresh

    _cancel_tournament_jobs(jq, tid, lobby=False)
    rn, total, finishing = _tournament_resume_position(state)
    players = await asyncio.to_thread(db.tournament_get_players, tid)
    where = f"Round {rn}/{total}" if not finishing else "the final results"
    try:
        sent = await safe_send(
            context.bot, chat_id,
            text=(
                f"▶️ <b>Tournament resumed</b> - continuing from {where}\n\n"
                + tournament_standings_text([(p[1], p[2]) for p in players])
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"tournament {tid}: resume status post failed: {e}")
        sent = None
    if sent is None:
        await _tournament_repause(context, tid)
        return "failed", "I couldn't post in that group."

    try:
        if state["current_word"]:
            scrambled = state["current_scrambled"] or scramble_letters(state["current_word"])
            try:
                sent = await safe_send(
                    context.bot, chat_id,
                    text=(
                        f"🏟️ <b>Round {state['current_round']}/{total} (resumed)</b>\n\n"
                        f"Unscramble this word:\n\n"
                        f"<b>{esc(scrambled)}</b>\n\n"
                        f"First correct answer in {TOURNAMENT_ROUND_SECONDS} seconds scores 💎{TOURNAMENT_POINTS_PER_WIN}."
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"tournament {tid}: resumed word send failed: {e}")
                sent = None
            if sent is None:
                await _tournament_repause(context, tid)
                return "failed", "I couldn't re-send the round's word."
            _schedule_tournament_job(
                jq, tournament_round_timeout_callback, _tournament_round_job_name(tid),
                TOURNAMENT_ROUND_SECONDS, chat_id, {"tid": tid},
            )
        elif state["current_round"] >= total:
            await _tournament_finish(context, tid)
        else:
            await _tournament_start_round(context, tid)
    except Exception as e:
        logger.error(f"tournament {tid}: resume failed: {e}")
        await _tournament_repause(context, tid)
        return "failed", "something went wrong while continuing it."
    return "resumed", state


def _tournament_running_text(t) -> str:
    rn, total, finishing = _tournament_resume_position(t)
    if finishing:
        return "▶️ The tournament is already running - it's wrapping up."
    return f"▶️ The tournament is already running - Round {rn}/{total}."


async def resumetournament_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    args = context.args or []

    # ---- DM: owner only -------------------------------------------------
    if chat.type == ChatType.PRIVATE:
        if not is_owner(user.id):
            await update.message.reply_text(
                "🏟️ Run /resumetournament inside the group whose tournament you want to continue."
            )
            return
        if not args:
            await update.message.reply_text(
                "Usage: /resumetournament <group id|@username>  or  /resumetournament allgroups\n"
                "(or run /resumetournament inside the group itself)"
            )
            return

        if args[0].lower() == "allgroups":
            targets = []
            for t in await asyncio.to_thread(db.tournament_get_all_open):
                if t["status"] != "lobby":
                    targets.append(t)
            lines, resumed, failed, healthy = [], 0, 0, 0
            for t in targets:
                title = await asyncio.to_thread(_group_title_sync, t["chat_id"]) or str(t["chat_id"])
                try:
                    code, data = await _tournament_resume_one(context, t)
                except Exception as e:
                    logger.error(f"resume allgroups: tournament {t['id']} failed: {e}")
                    code, data = "failed", "unexpected error"
                if code == "resumed":
                    resumed += 1
                    lines.append(f"✅ {title}: resumed")
                elif code in ("healthy", "none"):
                    healthy += 1
                else:
                    failed += 1
                    lines.append(f"❌ {title}: {data}")
                await asyncio.sleep(0.05)
            if not targets or (not resumed and not failed):
                await update.message.reply_text("🏟️ No paused or stuck tournaments right now.")
                return
            summary = f"🏟️ Resume summary: {resumed} resumed, {failed} failed, {healthy} already running.\n\n" + "\n".join(lines)
            await update.message.reply_text(summary[:4000])
            return

        resolved = await resolve_group_arg(args[0])
        if not resolved:
            await update.message.reply_text("❌ I couldn't find a group I'm currently in that matches that.")
            return
        chat_id, title = resolved
        t = await asyncio.to_thread(db.tournament_get_open_for_chat, chat_id)
        if not t:
            await update.message.reply_text(f"🏟️ There's no paused or running tournament in {title}.")
            return
        code, data = await _tournament_resume_one(context, t)
        if code == "resumed":
            await update.message.reply_text(f"✅ Resumed the tournament in {title}.")
        elif code == "healthy":
            await update.message.reply_text(f"{title}: " + _tournament_running_text(data).replace("▶️ ", ""))
        elif code == "none":
            await update.message.reply_text(f"{title}: {data}")
        else:
            await update.message.reply_text(f"❌ {title}: couldn't resume - {data} It's still paused; try again shortly.")
        return

    # ---- inside a group -------------------------------------------------
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    t = await asyncio.to_thread(db.tournament_get_open_for_chat, chat.id)
    if not t:
        await update.message.reply_text(
            "🏟️ There's no paused or running tournament in this group to resume (or it already finished)."
        )
        return
    if not await _tournament_may_manage(user.id, t):
        await update.message.reply_text(
            "❌ Only the tournament host, the bot owner, or Bot Admins can resume it."
        )
        return

    code, data = await _tournament_resume_one(context, t)
    if code == "healthy":
        await update.message.reply_text(_tournament_running_text(data))
    elif code == "none":
        await update.message.reply_text(f"🏟️ {data}")
    elif code == "failed":
        await update.message.reply_text(
            f"⚠️ Couldn't resume the tournament: {data} It's still paused - try /resumetournament again in a moment."
        )
    # 'resumed': the group has just received the status post and the word.


# =======================================================================
# PREMIUM (Telegram Stars) + Stars management
# =======================================================================

DEFAULT_PREMIUM_PRICE_STARS = 99
DEFAULT_PREMIUM_DURATION_SECONDS = 7 * 86400
PREMIUM_PAYLOAD_PREFIX = "premium"
PREMIUM_REMINDER_WINDOW_SECONDS = 24 * 3600
PREMIUM_REMINDER_JOB_NAME = "premium_renewal_reminder"
PREMIUM_PAGE_SIZE = 20


def get_premium_settings():
    """Live (price_in_stars, duration_seconds) - always read fresh from
    config, never hardcoded. Defaults: 99 stars / 7 days when unset."""
    raw_price = db.get_config("premium_price_stars", str(DEFAULT_PREMIUM_PRICE_STARS))
    raw_dur = db.get_config("premium_duration_seconds", str(DEFAULT_PREMIUM_DURATION_SECONDS))
    try:
        price = int(raw_price)
        if price < 1:
            price = DEFAULT_PREMIUM_PRICE_STARS
    except (TypeError, ValueError):
        price = DEFAULT_PREMIUM_PRICE_STARS
    try:
        dur = int(raw_dur)
        if dur < 60:
            dur = DEFAULT_PREMIUM_DURATION_SECONDS
    except (TypeError, ValueError):
        dur = DEFAULT_PREMIUM_DURATION_SECONDS
    return price, dur


def is_premium(user_id) -> bool:
    """Owner is always premium (free, permanent); everyone else needs a
    premium_users row whose expires_at is in the future. Sync (DB call) -
    call it via asyncio.to_thread from async handlers."""
    if is_owner(user_id):
        return True
    expiry = db.premium_get_expiry(user_id)
    return bool(expiry and expiry > datetime.now())


PREMIUM_ONLY_TEXT = "⭐ This is a premium feature — use /buypremium to buy premium."


def premium_only(func):
    """Reusable gate for premium-only commands: the owner and any active
    premium user pass through; everyone else gets PREMIUM_ONLY_TEXT.
    Usage:  @premium_only  above any `async def x_command(update, context)`."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user or not await asyncio.to_thread(is_premium, user.id):
            if update.callback_query:
                await update.callback_query.answer(PREMIUM_ONLY_TEXT, show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(PREMIUM_ONLY_TEXT)
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


def _format_remaining(seconds) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return "less than a minute"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "less than a minute"


def _parse_premium_payload(payload):
    """'premium:<user_id>:<duration_seconds>:<stars>' -> (uid, secs, stars) or None."""
    try:
        prefix, uid, secs, stars = str(payload).split(":")
        if prefix != PREMIUM_PAYLOAD_PREFIX:
            return None
        return int(uid), int(secs), int(stars)
    except (ValueError, TypeError):
        return None


def premium_features_text(price, dur) -> str:
    """Everything Premium gives, as the bot's code actually implements it."""
    return (
        f"⭐ <b>Premium features</b>\n\n"
        f"💵 <b>Doubled rewards</b> - every {DOLLAR} you earn from Word Game wins (as the guesser AND as the "
        f"leader) and Word Scramble wins is doubled, and Word Scramble win-streak bonuses are doubled too.\n\n"
        f"🚀 <b>Buy cash</b> - /buycash, /boostrank and /boostlevel let you buy {DOLLAR} with Telegram Stars "
        f"to increase your balance, level and rank.\n\n"
        f"🔁 <b>Send cash</b> - /transfer sends {DOLLAR} from your Word Scramble balance to another player.\n\n"
        f"🙏 <b>Ask for cash</b> - /request asks another player for {DOLLAR} (they get Yes/No buttons).\n\n"
        f"🎁 <b>Gift Premium</b> - /giftpremium lets you give Premium to another player "
        f"(you pay the Stars for their gift).\n\n"
        f"⏰ <b>Renewal reminder</b> - I DM you before your Premium runs out, with a one-tap renew button.\n\n"
        f"📊 <b>Status</b> - /premiumstatus shows whether it's active and how much time is left.\n\n"
        f"➕ <b>Stacks</b> - buying again while active extends your time.\n\n"
        f"ℹ️ Not doubled: purchased cash, the daily reward and tournament prizes.\n\n"
        f"Price: {price} ⭐ for {_format_duration_words(dur)}."
    )


def _bot_deeplink(context, param) -> str:
    uname = _bot_username(context)
    return f"https://t.me/{uname}?start={param}" if uname else ""


async def _redirect_to_dm(update: Update, context: ContextTypes.DEFAULT_TYPE, param, text, button_label):
    """Group -> DM hand-off. The button is a t.me deep link: one tap opens the
    bot's DM and Telegram's Start button continues the command there (the
    /start handler picks `param` up and runs the real flow)."""
    url = _bot_deeplink(context, param)
    if not url:
        await update.message.reply_text(f"{text}\n\nOpen my DM and send the command there.")
        return
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(button_label, url=url)]]),
    )


def _premium_invoice_keyboard(price):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Pay ⭐{price}", pay=True)],  # first button MUST be the Pay button
        [InlineKeyboardButton("➡️ Premium features", callback_data="pf:show")],
    ])


async def send_premium_invoice(context: ContextTypes.DEFAULT_TYPE, user_id):
    """Sends the Stars invoice to the user's DM. Returns None on success or a
    short error string to show the user."""
    price, dur = await asyncio.to_thread(get_premium_settings)
    title = f"Premium - {_format_duration_words(dur)}"[:32]
    description = (
        f"Doubles your {DOLLAR} rewards in Word Game and Word Scramble for "
        f"{_format_duration_words(dur)}. Buying again while active extends it. "
        f"Tap ➡️ Premium features below to see all premium features."
    )
    kwargs = dict(
        chat_id=user_id,
        title=title,
        description=description[:255],
        payload=f"{PREMIUM_PAYLOAD_PREFIX}:{user_id}:{dur}:{price}",
        provider_token="",  # must be empty for Telegram Stars
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=int(price))],  # raw star count, no x100
    )
    try:
        try:
            await context.bot.send_invoice(reply_markup=_premium_invoice_keyboard(price), **kwargs)
        except BadRequest as e:
            # If Telegram ever rejects the extra button, still deliver a payable invoice.
            logger.warning(f"premium invoice with features button rejected ({e}); sending plain invoice")
            await context.bot.send_invoice(**kwargs)
    except Forbidden:
        return "⚠️ Please start a private chat with me first (open my DM and press Start), then try /buypremium again."
    except Exception as e:
        logger.error(f"Failed to send premium invoice to {user_id}: {e}")
        return "⚠️ Couldn't create the invoice right now. Please try again in a moment."
    return None


async def premium_features_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'➡️ Premium features' under the invoice, and its Back button."""
    query = update.callback_query
    user = query.from_user
    data = query.data or ""
    chat_id = query.message.chat_id if query.message else user.id

    if data == "pf:show":
        await query.answer()
        price, dur = await asyncio.to_thread(get_premium_settings)
        invoice_msg_id = query.message.message_id if query.message else 0
        await context.bot.send_message(
            chat_id=chat_id,
            text=premium_features_text(price, dur),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data=f"pf:back:{invoice_msg_id}")
            ]]),
        )
        return

    # Back: drop the features list and the old invoice, then put a fresh
    # invoice in front of the user so they land right where they pay.
    await query.answer()
    try:
        old_invoice_id = int(data.split(":")[2])
    except (IndexError, ValueError):
        old_invoice_id = 0
    try:
        await query.message.delete()
    except Exception:
        pass
    if old_invoice_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=old_invoice_id)
        except Exception:
            pass
    err = await send_premium_invoice(context, user.id)
    if err:
        try:
            await context.bot.send_message(chat_id=chat_id, text=err)
        except Exception:
            pass


async def buypremium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_owner(user.id):
        await update.message.reply_text(
            "👑 You're the bot owner - you always have Premium, free and permanent. Nothing to buy!"
        )
        return

    if update.effective_chat.type != ChatType.PRIVATE:
        await _redirect_to_dm(
            update, context, "buypremium",
            "⭐ Premium is bought in my DM. Tap the button below and press Start to continue there.",
            "⭐ Continue in DM",
        )
        return

    err = await send_premium_invoice(context, user.id)
    if err:
        await update.message.reply_text(err)
        return


async def buypremium_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if is_owner(user.id):
        await query.answer("👑 You always have Premium - nothing to buy!", show_alert=True)
        return
    await query.answer()
    err = await send_premium_invoice(context, user.id)
    if err:
        try:
            await context.bot.send_message(chat_id=user.id, text=err)
        except Exception:
            pass


def _payload_prefix(payload) -> str:
    return str(payload or "").split(":", 1)[0]


async def premium_precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pre-checkout dispatcher: routes by invoice payload prefix ('premium:' or
    'dollar:'). Telegram waits ~10s for the answer, so it must always answer."""
    query = update.pre_checkout_query
    prefix = _payload_prefix(query.invoice_payload)
    try:
        if prefix == PREMIUM_PAYLOAD_PREFIX:
            await _premium_precheckout(query)
        elif prefix == DOLLAR_PAYLOAD_PREFIX:
            await _dollar_precheckout(query)
        elif prefix == GIFT_PAYLOAD_PREFIX:
            await _gift_precheckout(query)
        else:
            await query.answer(ok=False, error_message="This payment isn't recognised. Please try again from the command you used.")
    except Exception as e:
        logger.error(f"pre_checkout dispatch failed: {e}", exc_info=True)
        try:
            await query.answer(ok=False, error_message="Something went wrong. Please try again in a moment.")
        except Exception:
            pass


async def _premium_precheckout(query):
    try:
        parsed = _parse_premium_payload(query.invoice_payload)
        if parsed is None or query.currency != "XTR":
            await query.answer(ok=False, error_message="This payment isn't recognised. Please run /buypremium again.")
            return
        uid, _secs, stars = parsed
        if query.from_user.id != uid:
            await query.answer(ok=False, error_message="This invoice belongs to a different account.")
            return
        if query.total_amount != stars:
            await query.answer(ok=False, error_message="This invoice is out of date. Please run /buypremium again.")
            return
        await query.answer(ok=True)
    except Exception as e:
        logger.error(f"premium pre_checkout failed: {e}", exc_info=True)
        try:
            await query.answer(ok=False, error_message="Something went wrong. Please try again in a moment.")
        except Exception:
            pass


async def premium_successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Successful-payment dispatcher: routes by invoice payload prefix."""
    payload = update.message.successful_payment.invoice_payload
    prefix = _payload_prefix(payload)
    if prefix == PREMIUM_PAYLOAD_PREFIX:
        await _premium_successful_payment(update, context)
    elif prefix == DOLLAR_PAYLOAD_PREFIX:
        await _dollar_successful_payment(update, context)
    elif prefix == GIFT_PAYLOAD_PREFIX:
        await _gift_successful_payment(update, context)
    else:
        logger.warning(f"successful_payment with unrecognised payload {payload!r}")


async def _premium_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    payment = message.successful_payment
    parsed = _parse_premium_payload(payment.invoice_payload)
    if parsed is None:
        logger.warning(f"successful_payment with unrecognised payload {payment.invoice_payload!r}")
        return

    payer_id = message.from_user.id
    _uid, secs, stars = parsed
    charge_id = payment.telegram_payment_charge_id

    try:
        new_expiry = await asyncio.to_thread(
            db.premium_grant_from_payment, charge_id, payer_id, payment.total_amount, secs
        )
    except Exception as e:
        logger.error(f"Failed to record premium payment {charge_id}: {e}", exc_info=True)
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    f"🚨 A premium Stars payment went through but failed to record.\n"
                    f"User: {payer_id}\nStars: {payment.total_amount}\nCharge ID: {charge_id}\nError: {e}"
                ),
            )
        except Exception:
            pass
        await message.reply_text(
            "⚠️ Your payment went through, but I hit an error activating Premium. "
            "The owner has been notified and will sort it out."
        )
        return

    if new_expiry is None:
        return  # duplicate delivery of an already-applied charge

    remaining = (new_expiry - datetime.now()).total_seconds()
    await message.reply_text(
        f"🎉 Premium activated! Your Word Game and Word Scramble {DOLLAR} rewards are now doubled.\n"
        f"⏳ Time remaining: {_format_remaining(remaining)}"
    )
    await notify_owner_purchase(
        context, "⭐ Premium purchased", message.from_user, payment.total_amount,
        [f"Duration: {_format_duration_words(secs)}", f"Premium now has {_format_remaining(remaining)} left"],
    )


async def notify_owner_purchase(context, headline, payer, stars, extra_lines=None):
    """DM the owner about a completed Stars payment. Never raises - an alert
    problem must not affect the buyer's purchase."""
    try:
        uname = f"@{payer.username}" if getattr(payer, "username", None) else "no username"
        lines = [
            f"🔔 <b>{headline}</b>",
            "",
            f'👤 <a href="tg://user?id={payer.id}">{esc_name(payer.first_name)}</a> ({esc(uname)})',
            f"🆔 <code>{payer.id}</code>",
            f"⭐ Stars paid: {stars}",
        ]
        for line in (extra_lines or []):
            lines.append(esc(line))
        await context.bot.send_message(chat_id=OWNER_ID, text="\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logger.warning(f"owner purchase alert failed: {e}")


# =======================================================================
# /buycash (+ /boostrank, /boostlevel) - buy 💵 cash with Telegram Stars (premium only)
# =======================================================================

DOLLAR_PAYLOAD_PREFIX = "dollar"
DOLLAR_PACKS_CONFIG_KEY = "dollar_packs"
DEFAULT_DOLLAR_PACKS = [(100, 10), (1000, 95), (5000, 450), (10000, 850), (100000, 7500)]
DOLLAR_PACK_MAX_COINS = 1_000_000_000


def get_dollar_packs():
    """Live list of (coins, stars) - exactly 5 packs, always read fresh from
    config (never cached, never hardcoded elsewhere). Falls back to the
    defaults if the stored value is missing or malformed. Sync (DB call)."""
    raw = db.get_config(DOLLAR_PACKS_CONFIG_KEY)
    try:
        data = json.loads(raw) if raw else None
        packs = [(int(c), int(st)) for c, st in data]
        if len(packs) == len(DEFAULT_DOLLAR_PACKS) and all(c >= 1 and st >= 1 for c, st in packs):
            return packs
    except (TypeError, ValueError):
        pass
    return list(DEFAULT_DOLLAR_PACKS)


def _dollar_pack_label(coins, stars) -> str:
    return f"{DOLLAR}{coins} for {stars} ⭐"


def _parse_dollar_payload(payload):
    """'dollar:<user_id>:<pack_index>:<coins>:<stars>[:<source>]' -> (uid, idx, coins, stars) or None.
    The optional trailing source letter (c/r/l) only records which command
    the buyer used; older invoices without it are still valid."""
    try:
        parts = str(payload).split(":")
        if len(parts) not in (5, 6):
            return None
        prefix, uid, idx, coins, stars = parts[:5]
        if prefix != DOLLAR_PAYLOAD_PREFIX:
            return None
        uid, idx, coins, stars = int(uid), int(idx), int(coins), int(stars)
        if coins < 1 or stars < 1:
            return None
        return uid, idx, coins, stars
    except (ValueError, TypeError):
        return None


def _dollar_payload_source(payload) -> str:
    parts = str(payload).split(":")
    if len(parts) == 6 and parts[5] in CASH_SOURCES:
        return parts[5]
    return "c"


# c = /buycash, r = /boostrank, l = /boostlevel (all three run the same flow)
CASH_SOURCES = {
    "c": ("buycash", "💵 Cash purchased"),
    "r": ("boostrank", "🚀 Rank boosted"),
    "l": ("boostlevel", "🚀 Level boosted"),
}

BUYCASH_TEXT_CONFIG_KEY = "buycash_text"
DEFAULT_BUYCASH_TEXT = (
    "💵 Buying cash increases 🚀 your balance, level and rank ⚡️, "
    "and all purchased cash is added directly to your balance.\n\n"
    "Choose a pack below 👇"
)
BUYCASH_TEXT_MAX_LEN = 1000


def get_buycash_text() -> str:
    """The text above the packs. The default stays in force until the owner
    sets their own with /editbuycashtext. Sync (DB call)."""
    val = db.get_config(BUYCASH_TEXT_CONFIG_KEY)
    if val and val.strip():
        return val
    return DEFAULT_BUYCASH_TEXT


def _buycash_button_label(coins, stars) -> str:
    return f"Get {DOLLAR}{coins} for {stars} ⭐️"


def _buydollar_keyboard(user_id, packs, src="c"):
    # callback_data carries the buyer's id AND the pack they saw (idx, coins,
    # stars): only they can press it, and a pack edited by the owner after the
    # menu was shown can't silently charge a different price.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_buycash_button_label(c, st), callback_data=f"bd:{user_id}:{i}:{c}:{st}:{src}")]
        for i, (c, st) in enumerate(packs)
    ])


async def _cash_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, src):
    cmd_name = CASH_SOURCES[src][0]
    if update.effective_chat.type != ChatType.PRIVATE:
        await _redirect_to_dm(
            update, context, cmd_name,
            f"{DOLLAR} Cash is bought in my DM. Tap the button below and press Start to continue there.",
            f"{DOLLAR} Continue in DM",
        )
        return
    user = update.effective_user
    packs = await asyncio.to_thread(get_dollar_packs)
    text = await asyncio.to_thread(get_buycash_text)
    await update.message.reply_text(text, reply_markup=_buydollar_keyboard(user.id, packs, src))


@premium_only
async def buycash_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _cash_menu(update, context, "c")


@premium_only
async def boostrank_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _cash_menu(update, context, "r")


@premium_only
async def boostlevel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _cash_menu(update, context, "l")


@owner_only
async def editbuycashtext_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("🔒 Use /editbuycashtext in my DM.")
        return

    raw = update.message.text or ""
    parts = raw.split(None, 1)  # keeps the new text's own line breaks
    if len(parts) < 2 or not parts[1].strip():
        current = await asyncio.to_thread(get_buycash_text)
        await update.message.reply_text(
            f"Current /buycash text:\n\n{current}\n\n"
            f"Usage: /editbuycashtext <text>\n"
            f"Send /editbuycashtext reset to go back to the default text."
        )
        return

    new_text = parts[1].strip()
    if new_text.lower() == "reset":
        await asyncio.to_thread(db.set_config, BUYCASH_TEXT_CONFIG_KEY, "")
        await update.message.reply_text(f"✅ Default text restored:\n\n{DEFAULT_BUYCASH_TEXT}")
        return
    if len(new_text) > BUYCASH_TEXT_MAX_LEN:
        await update.message.reply_text(f"❌ That's too long - keep it under {BUYCASH_TEXT_MAX_LEN} characters.")
        return

    await asyncio.to_thread(db.set_config, BUYCASH_TEXT_CONFIG_KEY, new_text)
    await update.message.reply_text(
        f"✅ Updated. /buycash, /boostrank and /boostlevel now show:\n\n{new_text}"
    )


async def buydollar_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    try:
        parts = query.data.split(":")
        if len(parts) not in (5, 6):
            raise ValueError("bad callback")
        _prefix, owner_id, idx, coins, stars = parts[:5]
        owner_id, idx, coins, stars = int(owner_id), int(idx), int(coins), int(stars)
        src = parts[5] if len(parts) == 6 and parts[5] in CASH_SOURCES else "c"
    except (ValueError, TypeError):
        await query.answer("This menu is out of date. Run /buycash again.", show_alert=True)
        return

    if user.id != owner_id:
        await query.answer("This menu isn't yours - run /buycash for your own.", show_alert=True)
        return

    # Re-check premium at the moment of tapping.
    if not await asyncio.to_thread(is_premium, user.id):
        await query.answer(PREMIUM_ONLY_TEXT, show_alert=True)
        return

    packs = await asyncio.to_thread(get_dollar_packs)
    if not (0 <= idx < len(packs)) or packs[idx] != (coins, stars):
        await query.answer("The packs have changed - please run /buycash again.", show_alert=True)
        return

    try:
        await context.bot.send_invoice(
            chat_id=user.id,
            title=f"{DOLLAR}{coins} cash"[:32],
            description=f"{coins} {DOLLAR} added to your balance. Premium does not double purchased cash.",
            payload=f"{DOLLAR_PAYLOAD_PREFIX}:{user.id}:{idx}:{coins}:{stars}:{src}",
            provider_token="",  # must be empty for Telegram Stars
            currency="XTR",
            prices=[LabeledPrice(label=f"{DOLLAR}{coins}"[:32], amount=int(stars))],  # raw star count, no x100
        )
    except Forbidden:
        await query.answer(
            "⚠️ Please start a private chat with me first (open my DM and press Start), then tap again.",
            show_alert=True,
        )
        return
    except Exception as e:
        logger.error(f"Failed to send dollar invoice to {user.id}: {e}")
        await query.answer("⚠️ Couldn't create the invoice right now. Please try again in a moment.", show_alert=True)
        return

    await query.answer("📩 Invoice sent to your DM.")


async def _dollar_precheckout(query):
    """Validates a 'dollar:' invoice. Deliberately does NOT check premium: an
    invoice that was already issued must always be payable."""
    parsed = _parse_dollar_payload(query.invoice_payload)
    if parsed is None or query.currency != "XTR":
        await query.answer(ok=False, error_message="This payment isn't recognised. Please run /buycash again.")
        return
    uid, _idx, _coins, stars = parsed
    if query.from_user.id != uid:
        await query.answer(ok=False, error_message="This invoice belongs to a different account.")
        return
    if query.total_amount != stars:
        await query.answer(ok=False, error_message="This invoice is out of date. Please run /buycash again.")
        return
    await query.answer(ok=True)


async def _dollar_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    payment = message.successful_payment
    parsed = _parse_dollar_payload(payment.invoice_payload)
    if parsed is None:
        logger.warning(f"successful_payment with unrecognised dollar payload {payment.invoice_payload!r}")
        return

    payer = message.from_user
    _uid, _idx, coins, _stars = parsed
    charge_id = payment.telegram_payment_charge_id

    # Coins are ALWAYS delivered once paid - premium status is intentionally
    # not re-checked here, and the amount is never doubled.
    try:
        new_balance = await asyncio.to_thread(
            db.dollar_purchase_credit, charge_id, payer.id, payer.first_name, coins, payment.total_amount
        )
    except Exception as e:
        logger.error(f"Failed to record dollar purchase {charge_id}: {e}", exc_info=True)
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    f"🚨 A /buycash Stars payment went through but failed to credit.\n"
                    f"User: {payer.id}\nCoins owed: {coins}\nStars: {payment.total_amount}\n"
                    f"Charge ID: {charge_id}\nError: {e}"
                ),
            )
        except Exception:
            pass
        await message.reply_text(
            "⚠️ Your payment went through, but I hit an error adding your coins. "
            "The owner has been notified and will sort it out."
        )
        return

    if new_balance is None:
        return  # duplicate delivery of an already-applied charge

    await message.reply_text(
        f"✅ Payment received! {DOLLAR}{coins} added to your Word Scramble balance.\n"
        f"New balance: {DOLLAR}{new_balance}"
    )

    src = _dollar_payload_source(payment.invoice_payload)
    extra = [f"Bought: {DOLLAR}{coins} via /{CASH_SOURCES[src][0]}", f"Word Scramble balance now: {new_balance}"]
    try:
        combined = await asyncio.to_thread(db.get_combined_balance, payer.id)
        level, rank_name = db.compute_level_rank(combined)
        extra.append(f"Now Level {level} - {rank_name}")
    except Exception:
        pass
    await notify_owner_purchase(context, CASH_SOURCES[src][1], payer, payment.total_amount, extra)


@owner_only
async def setdollarpack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    packs = await asyncio.to_thread(get_dollar_packs)

    def _listing():
        return "\n".join(f"{i}. {_dollar_pack_label(c, st)}" for i, (c, st) in enumerate(packs, start=1))

    usage = "Usage: /setdollarpack <1-5> <dollars> <stars>  (e.g. /setdollarpack 2 1500 130)"
    if not args:
        await update.message.reply_text(f"{DOLLAR} Current /buycash packs:\n\n{_listing()}\n\n{usage}")
        return
    if len(args) != 3:
        await update.message.reply_text(usage)
        return
    try:
        slot = int(args[0])
        coins = int(args[1].replace(",", ""))
        stars = int(args[2].replace(",", ""))
    except ValueError:
        await update.message.reply_text(f"❌ All three values must be whole numbers.\n{usage}")
        return
    if not (1 <= slot <= len(packs)):
        await update.message.reply_text(f"❌ The pack number must be between 1 and {len(packs)}.\n{usage}")
        return
    if not (1 <= coins <= DOLLAR_PACK_MAX_COINS) or stars < 1:
        await update.message.reply_text(f"❌ Dollars and stars must be positive (dollars up to {DOLLAR_PACK_MAX_COINS:,}).")
        return

    packs[slot - 1] = (coins, stars)
    await asyncio.to_thread(db.set_config, DOLLAR_PACKS_CONFIG_KEY, json.dumps(packs))
    await update.message.reply_text(
        f"✅ Pack {slot} is now {_dollar_pack_label(coins, stars)}. Takes effect immediately.\n\n{_listing()}"
    )


# =======================================================================
# /transfer, /request, /deleterequestprompt (Word Scramble global balance)
# =======================================================================

MIN_TRANSFER_AMOUNT = 5
# Stay far below the balance column's 32-bit INTEGER ceiling (~2.1 billion).
MAX_TRANSFER_AMOUNT = 1_000_000_000
TRANSFER_CONFIRM_CONFIG_KEY = "transfer_confirm_threshold"
DEFAULT_TRANSFER_CONFIRM_THRESHOLD = 1000
TRANSFER_CONFIRM_SECONDS = 60

REQUEST_COOLDOWN_SECONDS = 30
REQUEST_MAX_PENDING = 3
REQUEST_EXPIRE_SECONDS = 10 * 60
REQUEST_PROMPT_DELETE_DEFAULT_SECONDS = 30

TRANSFER_USAGE = (
    f"Usage: /transfer <@username|id> <amount>\n"
    f"or reply to someone's message with /transfer <amount>\n"
    f"Minimum {MIN_TRANSFER_AMOUNT}."
)
REQUEST_USAGE = (
    f"Usage: /request <@username|id> <amount>\n"
    f"or reply to someone's message with /request <amount>\n"
    f"Minimum {MIN_TRANSFER_AMOUNT}."
)

_pending_transfers = {}  # token -> confirmation state (in memory: expires in 60s anyway)


def get_transfer_confirm_threshold() -> int:
    """Live threshold (sync - DB call). Amounts at or above it need a Confirm tap."""
    raw = db.get_config(TRANSFER_CONFIRM_CONFIG_KEY, str(DEFAULT_TRANSFER_CONFIRM_THRESHOLD))
    try:
        v = int(raw)
        return v if v >= 1 else DEFAULT_TRANSFER_CONFIRM_THRESHOLD
    except (TypeError, ValueError):
        return DEFAULT_TRANSFER_CONFIRM_THRESHOLD


def _mention(user_id, name) -> str:
    return f'<a href="tg://user?id={user_id}">{esc_name(name)}</a>'


def _parse_money_amount(text):
    """'250' or '1,000' -> int; None if it isn't a whole positive number."""
    text = (text or "").strip()
    if not re.fullmatch(r"\d{1,3}(,\d{3})+|\d+", text):
        return None
    value = int(text.replace(",", ""))
    return value if value > 0 else None


async def _parse_money_command(update: Update, context: ContextTypes.DEFAULT_TYPE, usage: str):
    """Shared parsing for /transfer and /request. Returns (target_id, target_name,
    amount) or None (an error message has already been sent). Rules: amount is a
    whole number >= MIN_TRANSFER_AMOUNT; the target is an existing player (a row
    in users), not the sender, not a bot. Two args = explicit target (a reply is
    ignored); one arg + a reply = the replied-to user (forum-topic 'replies' are
    ignored, exactly like /start does)."""
    message = update.message
    sender = update.effective_user
    args = context.args or []
    reply_msg = message.reply_to_message
    if reply_msg is not None and (reply_msg.from_user is None or getattr(reply_msg, "forum_topic_created", None)):
        reply_msg = None

    if len(args) == 2:
        target_arg, amount_arg = args
    elif len(args) == 1 and reply_msg is not None:
        target_arg, amount_arg = None, args[0]
    else:
        await message.reply_text(usage)
        return None

    amount = _parse_money_amount(amount_arg)
    if amount is None:
        await message.reply_text(f"❌ The amount must be a whole positive number (e.g. 250 or 1,000).\n\n{usage}")
        return None
    if amount < MIN_TRANSFER_AMOUNT:
        await message.reply_text(f"❌ The minimum amount is {DOLLAR}{MIN_TRANSFER_AMOUNT}.")
        return None
    if amount > MAX_TRANSFER_AMOUNT:
        await message.reply_text(f"❌ The maximum amount is {DOLLAR}{MAX_TRANSFER_AMOUNT:,}.")
        return None

    if target_arg is None:
        if reply_msg.from_user.is_bot:
            await message.reply_text("❌ You can't do that with a bot.")
            return None
        row = await asyncio.to_thread(db.get_user_by_id, reply_msg.from_user.id)
        if not row:
            await message.reply_text("❌ That person isn't a player yet - they need to open me and press /start first.")
            return None
    elif re.fullmatch(r"\d+", target_arg):
        row = await asyncio.to_thread(db.get_user_by_id, int(target_arg))
        if not row:
            await message.reply_text("❌ I don't know a player with that ID.")
            return None
    else:
        row = await asyncio.to_thread(db.get_user_by_username, target_arg.lstrip("@"))
        if not row:
            await message.reply_text(f"❌ I don't know a player called {esc(target_arg)}.")
            return None

    target_id, target_name = row[0], row[1] or str(row[0])
    if target_id == sender.id:
        await message.reply_text("❌ You can't do that with yourself.")
        return None
    return target_id, target_name, amount


# ---------------------------- /transfer ---------------------------------

async def _do_transfer(chat_id, sender_id, sender_name, target_id, target_name, amount):
    """Runs the transfer. Returns (ok, text)."""
    try:
        code, a, _b = await asyncio.to_thread(
            db.transfer_execute, sender_id, sender_name, target_id, target_name, amount, chat_id
        )
    except Exception as e:
        logger.error(f"transfer {sender_id}->{target_id} {amount} failed: {e}", exc_info=True)
        return False, "⚠️ Something went wrong and nothing was moved. Please try again."
    if code == "insufficient":
        return False, f"❌ Not enough balance - you have {DOLLAR}{a} and tried to send {DOLLAR}{amount}."
    return True, f"✅ {_mention(sender_id, sender_name)} sent {DOLLAR}{amount} to {_mention(target_id, target_name)}"


@premium_only
async def transfer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = await _parse_money_command(update, context, TRANSFER_USAGE)
    if not parsed:
        return
    target_id, target_name, amount = parsed
    sender = update.effective_user
    chat = update.effective_chat

    threshold = await asyncio.to_thread(get_transfer_confirm_threshold)
    if amount >= threshold:
        token = os.urandom(4).hex()
        _pending_transfers[token] = {
            "sender_id": sender.id, "sender_name": sender.first_name,
            "target_id": target_id, "target_name": target_name,
            "amount": amount, "chat_id": chat.id,
            "deadline": asyncio.get_running_loop().time() + TRANSFER_CONFIRM_SECONDS,
        }
        sent = await update.message.reply_html(
            f"⚠️ Confirm: send {DOLLAR}{amount} to {_mention(target_id, target_name)}?\n"
            f"This can't be undone. Expires in {TRANSFER_CONFIRM_SECONDS} seconds.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Confirm ✅", callback_data=f"tf:y:{token}"),
                InlineKeyboardButton("Cancel ❌", callback_data=f"tf:n:{token}"),
            ]]),
        )
        state = _pending_transfers.get(token)
        if state is not None:
            state["message_id"] = sent.message_id
        if context.job_queue is not None:
            context.job_queue.run_once(
                transfer_confirm_expire_callback, TRANSFER_CONFIRM_SECONDS + 1,
                chat_id=chat.id, name=f"transfer_confirm_{token}", data={"token": token},
            )
        return

    ok, text = await _do_transfer(chat.id, sender.id, sender.first_name, target_id, target_name, amount)
    await update.message.reply_html(text)


async def transfer_confirm_expire_callback(context: ContextTypes.DEFAULT_TYPE):
    token = (context.job.data or {}).get("token")
    state = _pending_transfers.pop(token, None)
    if not state or not state.get("message_id"):
        return
    try:
        await context.bot.edit_message_text(
            chat_id=state["chat_id"], message_id=state["message_id"],
            text="⌛ Transfer confirmation expired - no money was moved.",
        )
    except Exception:
        pass


async def transfer_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    try:
        _p, action, token = query.data.split(":")
    except ValueError:
        await query.answer()
        return

    state = _pending_transfers.get(token)
    if state is None or asyncio.get_running_loop().time() > state["deadline"]:
        _pending_transfers.pop(token, None)
        await query.answer("⌛ This confirmation expired - run /transfer again.", show_alert=True)
        try:
            await query.edit_message_text("⌛ Transfer confirmation expired - no money was moved.")
        except Exception:
            pass
        return
    if user.id != state["sender_id"]:
        await query.answer("Only the person who started this transfer can use these buttons.", show_alert=True)
        return

    # Claim the confirmation BEFORE any await: a double-tap can never run twice.
    _pending_transfers.pop(token, None)
    if context.job_queue is not None:
        for job in context.job_queue.get_jobs_by_name(f"transfer_confirm_{token}"):
            job.schedule_removal()

    if action == "n":
        await query.answer("Cancelled.")
        try:
            await query.edit_message_text("❌ Transfer cancelled - no money was moved.")
        except Exception:
            pass
        return

    # Confirm: premium and balance are re-checked right now.
    if not await asyncio.to_thread(is_premium, user.id):
        await query.answer(PREMIUM_ONLY_TEXT, show_alert=True)
        try:
            await query.edit_message_text("❌ Transfer cancelled - premium is required. No money was moved.")
        except Exception:
            pass
        return

    await query.answer()
    ok, text = await _do_transfer(
        state["chat_id"], state["sender_id"], state["sender_name"],
        state["target_id"], state["target_name"], state["amount"],
    )
    try:
        await query.edit_message_text(text, parse_mode="HTML")
    except Exception:
        await safe_send(context.bot, state["chat_id"], text=text, parse_mode="HTML")


# ---------------------------- /request ----------------------------------

def _request_job_name(rid):
    return f"money_request_expire_{rid}"


def _request_prompt_text(row) -> str:
    return (
        f"{_mention(row['target_id'], row['target_name'])}\n"
        f"{DOLLAR}{row['amount']} — give it to {_mention(row['requester_id'], row['requester_name'])}?"
    )


def _request_outcome_text(row, kind) -> str:
    target = _mention(row["target_id"], row["target_name"])
    requester = _mention(row["requester_id"], row["requester_name"])
    if kind == "paid":
        return f"✅ {target} gave {DOLLAR}{row['amount']} to {requester}"
    if kind == "declined":
        return f"❌ {target} declined"
    return f"⌛ Request expired - {target} didn't answer {requester}'s request for {DOLLAR}{row['amount']}."


def _request_prompt_keyboard(rid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes ✅", callback_data=f"rq:y:{rid}"),
        InlineKeyboardButton("No ❌", callback_data=f"rq:n:{rid}"),
    ]])


async def schedule_message_deletion(job_queue, chat_id, message_id, delay_seconds):
    """Deletes one specific message after `delay_seconds`. The deadline is
    persisted (scheduled_deletions) and re-armed on boot by
    restore_scheduled_deletions, so a restart never leaves it behind."""
    if job_queue is None:
        return
    delete_at = datetime.now() + timedelta(seconds=delay_seconds)
    try:
        await asyncio.to_thread(db.scheduled_deletion_add, chat_id, message_id, delete_at.isoformat())
    except Exception as e:
        logger.error(f"could not persist deletion of {chat_id}/{message_id}: {e}")
    _arm_message_deletion(job_queue, chat_id, message_id, delay_seconds)


def _arm_message_deletion(job_queue, chat_id, message_id, delay_seconds):
    if job_queue is None:
        return
    name = f"scheduled_delete_{chat_id}_{message_id}"
    for job in job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    job_queue.run_once(
        scheduled_delete_callback, max(delay_seconds, 1),
        chat_id=chat_id, name=name, data={"chat_id": chat_id, "message_id": message_id},
    )


async def scheduled_delete_callback(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    chat_id, message_id = data.get("chat_id"), data.get("message_id")
    if chat_id is None or message_id is None:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except (TimedOut, NetworkError):
        _arm_message_deletion(context.job_queue, chat_id, message_id, 60)  # transient: keep the row, retry
        return
    except Exception:
        pass  # already gone / too old / no permission - nothing more to do
    try:
        await asyncio.to_thread(db.scheduled_deletion_remove, chat_id, message_id)
    except Exception as e:
        logger.error(f"could not clear deletion row {chat_id}/{message_id}: {e}")


def restore_scheduled_deletions(job_queue):
    """On boot: re-arm every persisted message deletion (overdue ones fire ~1s
    after start)."""
    if job_queue is None:
        return
    now = datetime.now()
    for chat_id, message_id, delete_at in db.scheduled_deletion_get_all():
        try:
            remaining = (datetime.fromisoformat(delete_at) - now).total_seconds()
        except (TypeError, ValueError):
            remaining = 0
        _arm_message_deletion(job_queue, chat_id, message_id, remaining)


async def _finish_request_prompt(context, row, kind, fallback_message_id=None):
    """Edits the prompt to show the outcome (buttons removed), then schedules its
    deletion according to the group's /deleterequestprompt setting. Prompts
    delivered to a DM (positive chat id) are never deleted."""
    chat_id = row["chat_id"]
    message_id = row["message_id"] or fallback_message_id
    if chat_id is None or message_id is None:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=_request_outcome_text(row, kind), parse_mode="HTML", reply_markup=None,
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.info(f"request {row['id']}: couldn't edit prompt: {e}")
    except Exception as e:
        logger.info(f"request {row['id']}: couldn't edit prompt: {e}")

    if chat_id < 0:
        try:
            delay = await asyncio.to_thread(
                db.request_prompt_delete_get, chat_id, REQUEST_PROMPT_DELETE_DEFAULT_SECONDS
            )
        except Exception:
            delay = REQUEST_PROMPT_DELETE_DEFAULT_SECONDS
        if delay and delay > 0:
            await schedule_message_deletion(context.job_queue, chat_id, message_id, delay)


def _schedule_request_expiry(job_queue, rid, seconds):
    if job_queue is None:
        return
    name = _request_job_name(rid)
    for job in job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    job_queue.run_once(request_expire_callback, max(seconds, 1), name=name, data={"rid": rid})


def _cancel_request_expiry(job_queue, rid):
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(_request_job_name(rid)):
        job.schedule_removal()


async def request_expire_callback(context: ContextTypes.DEFAULT_TYPE):
    rid = (context.job.data or {}).get("rid")
    if rid is None:
        return
    row = await asyncio.to_thread(db.request_expire, rid)
    if not row:
        return  # already paid / declined / expired
    await _finish_request_prompt(context, row, "expired")


def restore_money_requests(job_queue):
    """On boot: re-arm the 10-minute expiry of every pending request (overdue
    ones expire ~1s after start, their prompts get edited then)."""
    if job_queue is None:
        return
    now = datetime.now()
    for row in db.request_get_pending():
        try:
            remaining = REQUEST_EXPIRE_SECONDS - (now - datetime.fromisoformat(row["created_at"])).total_seconds()
        except (TypeError, ValueError):
            remaining = 0
        _schedule_request_expiry(job_queue, row["id"], remaining)


@premium_only
async def request_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = await _parse_money_command(update, context, REQUEST_USAGE)
    if not parsed:
        return
    target_id, target_name, amount = parsed
    requester = update.effective_user
    chat = update.effective_chat
    in_dm = chat.type == ChatType.PRIVATE
    prompt_chat_id = target_id if in_dm else chat.id  # DM: deliver to the target's DM

    status, value, swept = await asyncio.to_thread(
        db.request_create, prompt_chat_id, requester.id, requester.first_name, target_id, target_name,
        amount, REQUEST_COOLDOWN_SECONDS, REQUEST_MAX_PENDING, REQUEST_EXPIRE_SECONDS,
    )
    for old in swept:  # overdue ones that just got expired: update their prompts
        _cancel_request_expiry(context.job_queue, old["id"])
        await _finish_request_prompt(context, old, "expired")

    if status == "cooldown":
        await update.message.reply_text(f"⏳ Slow down - you can send another request in {value} seconds.")
        return
    if status == "pair":
        await update.message.reply_text("❌ You already have a pending request to that person. Wait for their answer (it expires in 10 minutes).")
        return
    if status == "max":
        await update.message.reply_text(f"❌ You already have {REQUEST_MAX_PENDING} pending requests. Wait for some to be answered or expire.")
        return

    rid = value
    row = await asyncio.to_thread(db.request_get, rid)
    try:
        sent = await safe_send(
            context.bot, prompt_chat_id,
            text=_request_prompt_text(row), parse_mode="HTML",
            reply_markup=_request_prompt_keyboard(rid),
        )
    except Forbidden:
        sent = None
        if in_dm:
            await asyncio.to_thread(db.request_expire, rid)
            await update.message.reply_text(
                "⚠️ I couldn't deliver that request - they haven't started a private chat with me, so I can't message them."
            )
            return
    except Exception as e:
        logger.error(f"request {rid}: prompt send failed: {e}")
        sent = None

    if sent is None:
        await asyncio.to_thread(db.request_expire, rid)  # frees the slot; no money moved
        await update.message.reply_text("⚠️ I couldn't send that request right now. Please try again in a moment.")
        return

    await asyncio.to_thread(db.request_set_message, rid, prompt_chat_id, sent.message_id)
    _schedule_request_expiry(context.job_queue, rid, REQUEST_EXPIRE_SECONDS)
    if in_dm:
        await update.message.reply_text("📨 Request delivered to their DM.")


async def request_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    try:
        _p, action, rid = query.data.split(":")
        rid = int(rid)
    except ValueError:
        await query.answer()
        return

    fallback_mid = query.message.message_id if query.message else None

    row = await asyncio.to_thread(db.request_get, rid)
    if not row:
        await query.answer("This request no longer exists.", show_alert=True)
        return
    if user.id != row["target_id"]:
        await query.answer("This request isn't for you.", show_alert=True)
        return

    if action == "y":
        try:
            code, row2, extra = await asyncio.to_thread(db.request_pay, rid, user.id, REQUEST_EXPIRE_SECONDS)
        except Exception as e:
            logger.error(f"request {rid}: payment failed: {e}", exc_info=True)
            await query.answer("⚠️ Something went wrong and nothing was moved. Please try again.", show_alert=True)
            return
        if code == "insufficient":
            await query.answer(
                f"You don't have enough {DOLLAR} - your balance is {DOLLAR}{extra}. "
                f"The request stays open until it expires.",
                show_alert=True,
            )
            return
        if code == "paid":
            _cancel_request_expiry(context.job_queue, rid)
            await query.answer("✅ Sent!")
            await _finish_request_prompt(context, row2, "paid", fallback_mid)
            return
    else:
        code, row2 = await asyncio.to_thread(db.request_decline, rid, user.id, REQUEST_EXPIRE_SECONDS)
        if code == "declined":
            _cancel_request_expiry(context.job_queue, rid)
            await query.answer("Declined.")
            await _finish_request_prompt(context, row2, "declined", fallback_mid)
            return

    if code == "expired":
        _cancel_request_expiry(context.job_queue, rid)
        await query.answer("⌛ This request has expired.", show_alert=True)
        await _finish_request_prompt(context, row2, "expired", fallback_mid)
    elif code == "closed":
        await query.answer("This request was already answered.", show_alert=True)
    else:
        await query.answer("This request isn't for you.", show_alert=True)


# ------------------------ /deleterequestprompt --------------------------

def _request_prompt_delete_status(seconds) -> str:
    return "off (prompts are never deleted)" if not seconds else f"on - deleted {_format_duration_words(seconds)} after the answer"


async def deleterequestprompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        await update.message.reply_text("🗑️ Run /deleterequestprompt inside the group you want to apply it to.")
        return
    if not (is_owner(update.effective_user.id) or await is_chat_admin(update, context)):
        await update.message.reply_text("❌ Only group admins (or the bot owner) can use /deleterequestprompt.")
        return

    usage = (
        "Usage:\n/deleterequestprompt off\n/deleterequestprompt on\n"
        "/deleterequestprompt on <1-9>s  or  on <1-9>m\n(no arguments shows the current setting)"
    )
    args = [a.lower() for a in (context.args or [])]
    if not args:
        current = await asyncio.to_thread(
            db.request_prompt_delete_get, chat.id, REQUEST_PROMPT_DELETE_DEFAULT_SECONDS
        )
        await update.message.reply_text(f"🗑️ Request prompts here: {_request_prompt_delete_status(current)}.\n\n{usage}")
        return

    if args == ["off"]:
        seconds = 0
    elif args == ["on"]:
        seconds = REQUEST_PROMPT_DELETE_DEFAULT_SECONDS
    elif len(args) == 2 and args[0] == "on" and re.fullmatch(r"[1-9][sm]", args[1]):
        seconds = int(args[1][0]) * (60 if args[1][1] == "m" else 1)
    else:
        await update.message.reply_text(usage)
        return

    await asyncio.to_thread(db.request_prompt_delete_set, chat.id, seconds)
    await update.message.reply_text(f"✅ Request prompts here: {_request_prompt_delete_status(seconds)}.")


# =======================================================================
# /searchcommand and /fullhelp
# =======================================================================

SEARCHCOMMAND_MAX_RESULTS = 15
SEARCHCOMMAND_USAGE = "Usage: /searchcommand <text>  (e.g. /searchcommand tournament)"


def _command_entries():
    """Every command line from /allcommands and /scrambleallcommands, read from
    the SAME text those commands send (so it can never go stale). Each entry:
    {name, names, desc, restricted}. `restricted` marks lines from the
    'Group Admin' and 'Owner' sections."""
    entries = []
    for text in (_allcommands_text(), _scrambleallcommands_text()):
        restricted = False
        for raw in text.split("\n"):
            line = raw.strip()
            heading = re.fullmatch(r"<b>(.*)</b>", line)
            if heading:
                title = heading.group(1)
                restricted = ("Owner" in title) or ("Group Admin" in title)
                continue
            if not line.startswith("/"):
                continue
            head, _sep, desc = line.partition(" - ")
            names = [n.lower() for n in re.findall(r"/([A-Za-z0-9_]+)", html_lib.unescape(head))]
            if not names:
                continue
            entries.append({
                "name": names[0],
                "names": names,
                "desc": html_lib.unescape(desc).strip(),
                "restricted": restricted,
            })
    return entries


def search_commands(query: str, include_restricted: bool):
    """Case-insensitive; every word must match the command name(s) or the
    description. Restricted-section lines are only searched when
    include_restricted is True. Deduplicated by command name (first wins).
    Returns the full list of matching entries."""
    words = [w.lower().lstrip("/") for w in (query or "").split()]
    words = [w for w in words if w]
    if not words:
        return []
    results, seen = [], set()
    for e in _command_entries():
        if e["restricted"] and not include_restricted:
            continue
        haystack = (" ".join(e["names"]) + " " + e["desc"]).lower()
        if all(w in haystack for w in words):
            if e["name"] in seen:
                continue
            seen.add(e["name"])
            results.append(e)
    return results


async def searchcommand_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args or []).strip()
    if not query.lstrip("/ "):
        await update.message.reply_text(SEARCHCOMMAND_USAGE)
        return

    # Exactly the people /allcommands and /scrambleallcommands are shown to
    # (owner, Bot Admins, this group's creator) may search the admin/owner sections.
    uid = update.effective_user.id
    include_restricted = bool(
        await asyncio.to_thread(is_bot_admin_or_owner, uid) or await is_chat_creator(update, context)
    )
    results = await asyncio.to_thread(search_commands, query, include_restricted)

    if not results:
        await update.message.reply_html(
            f"🔎 No commands match <b>{esc(query)}</b>. Try another word, or see /allcommands (or /fullhelp)."
        )
        return

    shown = results[:SEARCHCOMMAND_MAX_RESULTS]
    lines = [f"🔎 <b>{len(results)} result(s) for {esc(query)}</b>", ""]
    for e in shown:
        lines.append(f"/{e['name']} — {esc(e['desc'])}" if e["desc"] else f"/{e['name']}")
    if len(results) > len(shown):
        lines.append("")
        lines.append(f"<i>+{len(results) - len(shown)} more - add another word to narrow it down.</i>")
    await reply_html_in_parts(update.message, "\n".join(lines))


# ------------------------------- /fullhelp -------------------------------

FULLHELP_PAGES = (
    ("g", "🎮 Word Game"),
    ("s", "🔤 Word Scramble"),
    ("t", "🏟️ Tournaments"),
    ("p", "⭐ Premium & Coins"),
    ("o", "📚 Other"),
)


def _invite_reward_amount_sync() -> int:
    val = db.get_config("invite_reward_amount", str(DEFAULT_INVITE_REWARD_AMOUNT))
    try:
        return int(val)
    except (TypeError, ValueError):
        return DEFAULT_INVITE_REWARD_AMOUNT


def _fullhelp_game_page() -> str:
    win, cheat = get_reward("win"), get_reward("cheat")
    return (
        f"🎮 <b>Word Game</b>\n\n"
        f"A team game: one player explains a secret word without saying it, everyone else guesses.\n\n"
        f"1️⃣ Someone runs /wordgame in a group (once a round is live, only group admins can restart it).\n"
        f"2️⃣ Tap <b>I'll Explain</b> to become the leader.\n"
        f"3️⃣ The leader taps <b>See Word</b> (a random word) or <b>Write Word</b> (send me your own word in DM).\n"
        f"4️⃣ Give clues in the group. Saying the word yourself costs the leader {DOLLAR}1.\n"
        f"5️⃣ Anyone can type the exact word to guess it. Close guesses get a 👀.\n\n"
        f"💰 <b>Rewards</b>\n"
        f"A correct guess pays both the guesser and the leader {DOLLAR}{win} each.\n"
        f"Guessing before the leader has opened the word counts as cheating: {DOLLAR}{cheat}.\n\n"
        f"🧰 <b>Helpful commands</b>\n"
        f"/hint - the leader's clues so far (or just type <code>hint</code>)\n"
        f"/stopvote - 3 votes stop a stuck round\n"
        f"/stopgame - group admins can stop a round instantly\n"
        f"/autolead (or /settings) - in my DM, choose whether winning a round makes you the next leader\n\n"
        f"⏰ If the leader doesn't open the word in time, the round ends and I ask who wants to explain next.\n"
        f"See /help for the short version."
    )


def _fullhelp_scramble_page() -> str:
    reward = get_scramble_reward()
    mins = SCRAMBLE_ROUND_SECONDS // 60
    streak_lines = "\n".join(
        f"{n} in a row: +{DOLLAR}{bonus}" for n, bonus in sorted(SCRAMBLE_STREAK_MILESTONES.items())
    )
    return (
        f"🔤 <b>Word Scramble</b>\n\n"
        f"1️⃣ Run /wordscramble (groups and my DM).\n"
        f"2️⃣ I show a scrambled word - just type your guess as a normal message.\n"
        f"3️⃣ The first correct answer within {mins} minute{'s' if mins != 1 else ''} wins {DOLLAR}{reward}.\n"
        f"4️⃣ Close guess: 👀. Exactly right: 🎉.\n\n"
        f"🔥 <b>Win streaks</b> - win back-to-back rounds in the same chat for a bonus:\n{streak_lines}\n"
        f"Your streak resets when someone else wins a round.\n\n"
        f"⚙️ <b>Group settings</b> (group admins)\n"
        f"/scrambleautomaticmode on|off - a new round starts right after each one ends and keeps going non-stop. "
        f"It only switches itself off after 3 rounds in a row nobody answers, and can't be turned on while Slow Mode is on.\n"
        f"/slowmode on &lt;time&gt;|off - the next round starts exactly &lt;time&gt; after the last one ends "
        f"(e.g. /slowmode on 2m). It turns Automatic Mode off.\n"
        f"/scramblestopgame - stop the active round.\n\n"
        f"📚 Words don't repeat until the whole pool has been used in that chat. Only one round runs per chat at a time.\n\n"
        f"🏆 <b>Rankings</b>\n"
        f"/scrambleleaderboard · /scrambletopplayers · /scrambletopgroups\n"
        f"See /scramblehelp for the short version."
    )


def _fullhelp_tournament_page() -> str:
    quick = f"{TOURNAMENT_QUICK_FEE} {TOURNAMENT_QUICK_DIFFICULTY} {TOURNAMENT_QUICK_ROUNDS}rounds"
    return (
        f"🏟️ <b>Tournaments</b>\n\n"
        f"A Word Scramble contest inside a group. Everyone pays an entry fee from their Word Scramble balance "
        f"and the best score takes the prize.\n\n"
        f"🚀 <b>Starting</b>\n"
        f"/settournament &lt;fee&gt; &lt;difficulty&gt; &lt;rounds&gt;rounds - e.g. /settournament 2000 Medium 3rounds\n"
        f"/tournament - instant one: {quick}\n"
        f"Fee {TOURNAMENT_MIN_FEE:,}-{TOURNAMENT_MAX_FEE:,}, rounds {TOURNAMENT_MIN_ROUNDS}-{TOURNAMENT_MAX_ROUNDS}. "
        f"Difficulty: easy, easy3-easy6, medium, hard, off.\n"
        f"The host pays the fee and joins automatically.\n\n"
        f"🚪 <b>Lobby</b>\n"
        f"Players send /join. The lobby stays open for {TOURNAMENT_LOBBY_SECONDS} seconds, then the tournament starts on its own. "
        f"The host (or a bot admin) can send /starttournament to start early. With fewer than 2 players it is cancelled and refunded.\n\n"
        f"🎯 <b>Playing</b>\n"
        f"Each round has {TOURNAMENT_ROUND_SECONDS} seconds. The first correct answer scores 💎{TOURNAMENT_POINTS_PER_WIN}. "
        f"Other Word Scramble rounds, Slow Mode and Automatic Mode wait in that group until the tournament ends.\n\n"
        f"🏆 <b>Prize</b>\n"
        f"Every entry fee plus one bonus entry fee from me: fee × (players + 1). The top score wins it all; a tie splits it; "
        f"if nobody scores, everyone is refunded.\n\n"
        f"🛠️ <b>If something goes wrong</b>\n"
        f"/stoptournament - the host or a bot admin can cancel a lobby (or a paused tournament) and refund everyone.\n"
        f"If I lose connection, the tournament pauses and entry fees stay safe. The host or a bot admin sends "
        f"/resumetournament to continue from the same round. A tournament left paused for over "
        f"{_format_duration_words(TOURNAMENT_PAUSED_MAX_SECONDS)} is refunded automatically."
    )


def _fullhelp_premium_page() -> str:
    price, dur = get_premium_settings()
    packs = get_dollar_packs()
    pack_lines = "\n".join(_buycash_button_label(c, st) for c, st in packs)
    threshold = get_transfer_confirm_threshold()
    return (
        f"⭐ <b>Premium & Coins</b>\n\n"
        f"⭐ <b>Premium</b>\n"
        f"/buypremium - {price} ⭐ for {_format_duration_words(dur)} (paid with Telegram Stars; buying again while active extends it). "
        f"Used in a group, it moves you to my DM to pay. Tap <b>➡️ Premium features</b> on the invoice to see everything Premium gives.\n"
        f"/premiumstatus - see if it's active and how much time is left.\n"
        f"It doubles the {DOLLAR} you earn from Word Game and Word Scramble wins (streak bonuses too). "
        f"It does not double purchased cash, the daily reward or tournament prizes.\n\n"
        f"🎁 <b>Gifting Premium</b> (Premium members)\n"
        f"/giftpremium &lt;username|ID&gt; - gift Premium to another player (or reply to their message). "
        f"You pay the Stars; they get the Premium, added on top of any time they already have.\n\n"
        f"{DOLLAR} <b>Buying cash</b> (Premium)\n"
        f"/buycash - buying cash increases your balance, level and rank, and the cash goes straight to your balance. "
        f"Pick a pack and I'll send the Stars invoice to your DM.\n"
        f"/boostrank and /boostlevel - do exactly the same as /buycash.\n"
        f"Used in a group, all three move you to my DM.\n"
        f"{pack_lines}\n\n"
        f"🔁 <b>Sending coins</b> (Premium, Word Scramble balance)\n"
        f"/transfer &lt;@username|id&gt; &lt;amount&gt; - or reply to their message with /transfer &lt;amount&gt;. "
        f"Minimum {DOLLAR}{MIN_TRANSFER_AMOUNT}; amounts of {DOLLAR}{threshold} or more ask you to confirm first "
        f"({TRANSFER_CONFIRM_SECONDS} seconds).\n"
        f"/request &lt;@username|id&gt; &lt;amount&gt; - they get Yes/No buttons; nothing moves unless they press Yes. "
        f"Requests expire after {REQUEST_EXPIRE_SECONDS // 60} minutes; you can have {REQUEST_MAX_PENDING} open at a time, "
        f"one per person, with a {REQUEST_COOLDOWN_SECONDS}-second pause between requests.\n"
        f"/deleterequestprompt off|on [&lt;1-9&gt;s|m] - group admins choose whether answered request prompts are deleted "
        f"(default: on, after {REQUEST_PROMPT_DELETE_DEFAULT_SECONDS} seconds).\n"
        f"The other person must already be a player (they've pressed /start)."
    )


def _fullhelp_other_page() -> str:
    daily = db.get_config("reward_daily", db.DAILY_REWARD)
    invite = _invite_reward_amount_sync()
    return (
        f"📚 <b>Everything else</b>\n\n"
        f"{DOLLAR} <b>Invite & Earn</b> - /inviteandearn\n"
        f"Add me to a group with {INVITE_REWARD_MIN_MEMBERS}+ members and keep me there for {INVITE_REWARD_HOURS} hours "
        f"to earn {DOLLAR}{invite} in your Word Scramble balance (once per group, credited to whoever's link did the inviting). "
        f"If I was added without your link, run /claim in that group after {INVITE_REWARD_HOURS} hours.\n\n"
        f"🎁 <b>Daily reward</b> - /claimdailyreward\n"
        f"Message me privately to claim {DOLLAR}{daily} once every 24 hours. It goes to your Word Game balance.\n\n"
        f"🏆 <b>Leaderboards</b>\n"
        f"/leaderboard, /chattop, /globaltop, /topplayers, /topgroups - Word Game and combined rankings\n"
        f"/scrambleleaderboard, /scrambletopplayers, /scrambletopgroups - Word Scramble rankings\n\n"
        f"👤 <b>Profile</b>\n"
        f"/profile - your balance, level and rank. Reply to someone's message with /start to see their profile. "
        f"Balance, level and rank combine your Word Game and Word Scramble earnings.\n"
        f"/ranks - the full rank ladder and what each level needs.\n\n"
        f"🔎 <b>Finding commands</b>\n"
        f"/searchcommand &lt;text&gt; - search commands by name or description.\n"
        f"/help and /scramblehelp - the short how-to-play guides.\n"
        f"/fullhelp - this guide."
    )


_FULLHELP_BUILDERS = {
    "g": _fullhelp_game_page,
    "s": _fullhelp_scramble_page,
    "t": _fullhelp_tournament_page,
    "p": _fullhelp_premium_page,
    "o": _fullhelp_other_page,
}


def build_fullhelp_page(page: str) -> str:
    """Sync (reads live config) - call via asyncio.to_thread."""
    return _FULLHELP_BUILDERS.get(page, _fullhelp_game_page)()


def _fullhelp_keyboard(user_id, current):
    def btn(key, label):
        shown = f"• {label}" if key == current else label
        return InlineKeyboardButton(shown, callback_data=f"fh:{user_id}:{key}")
    labels = dict(FULLHELP_PAGES)
    return InlineKeyboardMarkup([
        [btn("g", labels["g"]), btn("s", labels["s"])],
        [btn("t", labels["t"]), btn("p", labels["p"])],
        [btn("o", labels["o"])],
    ])


async def fullhelp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = await asyncio.to_thread(build_fullhelp_page, "g")
    await update.message.reply_html(
        text,
        reply_markup=_fullhelp_keyboard(user.id, "g"),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def fullhelp_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        _p, owner_id, page = query.data.split(":")
        owner_id = int(owner_id)
    except ValueError:
        await query.answer()
        return
    if query.from_user.id != owner_id:
        await query.answer("Run /fullhelp yourself to flip through the pages.", show_alert=True)
        return
    text = await asyncio.to_thread(build_fullhelp_page, page)
    await query.answer()
    try:
        await query.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=_fullhelp_keyboard(owner_id, page),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except BadRequest:
        pass  # same page tapped again - nothing to change


async def premiumstatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_owner(user.id):
        await update.message.reply_text("👑 You're the bot owner - Premium is always on for you, free and permanent.")
        return

    expiry = await asyncio.to_thread(db.premium_get_expiry, user.id)
    now = datetime.now()
    if expiry and expiry > now:
        await update.message.reply_text(
            f"⭐ Premium is active - your {DOLLAR} rewards are doubled.\n"
            f"⏳ Time remaining: {_format_remaining((expiry - now).total_seconds())}\n\n"
            f"Use /buypremium to extend it."
        )
        return

    price, dur = await asyncio.to_thread(get_premium_settings)
    await update.message.reply_text(
        f"You don't have Premium right now.\n\n"
        f"⭐ /buypremium - {price} ⭐ for {_format_duration_words(dur)} of doubled {DOLLAR} rewards "
        f"in Word Game and Word Scramble."
    )


@owner_only
async def setpremium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        price, dur = await asyncio.to_thread(get_premium_settings)
        await update.message.reply_text(
            f"⭐ Current Premium: {price} ⭐ for {_format_duration_words(dur)}.\n\n"
            f"Usage: /setpremium <stars> <duration>  (e.g. /setpremium 99 7d)\n"
            f"Duration units: m, h, d."
        )
        return

    if len(context.args) != 2:
        await update.message.reply_text("Usage: /setpremium <stars> <duration>  (e.g. /setpremium 99 7d)")
        return

    try:
        stars = int(context.args[0])
    except ValueError:
        stars = 0
    if stars < 1:
        await update.message.reply_text("❌ Stars must be a whole number of at least 1.")
        return

    seconds = _parse_duration(context.args[1])
    if seconds is None:
        await update.message.reply_text("❌ Invalid duration. Use e.g. 30m, 12h, 7d.")
        return

    await asyncio.to_thread(db.set_config, "premium_price_stars", str(stars))
    await asyncio.to_thread(db.set_config, "premium_duration_seconds", str(seconds))
    await update.message.reply_text(
        f"✅ Premium is now {stars} ⭐ for {_format_duration_words(seconds)}. "
        f"Existing subscribers keep the time they already paid for."
    )


def build_premium_players_page(page: int):
    total = db.premium_count_active()
    offset = page * PREMIUM_PAGE_SIZE
    rows = db.premium_list_active(offset, PREMIUM_PAGE_SIZE) if total else []

    total_pages = max(1, (total + PREMIUM_PAGE_SIZE - 1) // PREMIUM_PAGE_SIZE)
    lines = [
        f"⭐ <b>Premium Players</b> ({total} active) — Page {page + 1}/{total_pages}\n",
        f"👑 <b>Bot owner</b> (<code>{OWNER_ID}</code>) — permanent, free Premium "
        f"<i>(not stored in the list; not counted above)</i>\n",
    ]
    now = datetime.now()
    if not rows:
        lines.append("No paid premium players right now." if page == 0 else "No more entries.")
    for i, (uid, name, expires_at) in enumerate(rows):
        try:
            left = _format_remaining((datetime.fromisoformat(expires_at) - now).total_seconds())
        except (TypeError, ValueError):
            left = "unknown"
        lines.append(
            f"{offset + i + 1}. {truncate_name(name or 'Unknown')} — "
            f"ID: <code>{uid}</code> — ⏳ {left}"
        )

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"spp:{page - 1}"))
    if offset + PREMIUM_PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"spp:{page + 1}"))
    return "\n".join(lines), (InlineKeyboardMarkup([nav_row]) if nav_row else None)


@owner_only
async def seepremiumplayers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, keyboard = await asyncio.to_thread(build_premium_players_page, 0)
    await update.message.reply_html(text, reply_markup=keyboard)


async def premium_players_page_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_owner(query.from_user.id):
        await query.answer("🚫 Owner only.", show_alert=True)
        return
    try:
        page = max(0, int(query.data.split(":", 1)[1]))
    except (ValueError, IndexError):
        await query.answer()
        return
    text, keyboard = await asyncio.to_thread(build_premium_players_page, page)
    await query.answer()
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    except BadRequest:
        pass  # message unchanged


async def get_star_balance(bot):
    """The bot's current Telegram Stars balance via the real Bot API method
    getMyStarBalance. Returns (stars, nanostars). Shared by /starbalance and
    /withdraw."""
    fn = getattr(bot, "get_my_star_balance", None)
    if fn is not None:
        result = await fn()
        return int(result.amount), int(getattr(result, "nanostar_amount", 0) or 0)
    api = getattr(bot, "do_api_request", None)
    if api is None:
        raise RuntimeError("This python-telegram-bot version can't call getMyStarBalance - please upgrade it.")
    data = await api("getMyStarBalance")
    return int(data.get("amount", 0)), int(data.get("nanostar_amount", 0) or 0)


def _format_star_amount(stars, nano):
    return f"{stars}" + (f" (+{nano} nanostars)" if nano else "")


@owner_only
async def starbalance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        stars, nano = await get_star_balance(context.bot)
    except Exception as e:
        logger.error(f"getMyStarBalance failed: {e}")
        await update.message.reply_text(f"⚠️ Couldn't read the Stars balance right now: {e}")
        return
    await update.message.reply_text(f"⭐ Bot Stars balance: {_format_star_amount(stars, nano)}")


@owner_only
async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        stars, nano = await get_star_balance(context.bot)
        balance_line = f"⭐ Current balance: <b>{_format_star_amount(stars, nano)}</b>"
    except Exception as e:
        logger.error(f"getMyStarBalance failed in /withdraw: {e}")
        balance_line = "⚠️ Couldn't read the current balance right now."

    await update.message.reply_html(
        f"💸 <b>Withdraw Stars</b>\n\n"
        f"{balance_line}\n\n"
        f"Telegram doesn't expose Stars withdrawal through the Bot API, so I can't do it for you - "
        f"it has to be done by you in @BotFather:\n\n"
        f"1. Open @BotFather → /mybots → choose this bot.\n"
        f"2. Go to Bot Settings → the Balance / Stars option.\n"
        f"3. Tap Withdraw and confirm (Telegram asks for your own two-step verification).\n\n"
        f"<i>Telegram sets its own minimum withdrawal amount and holding period before Stars can be "
        f"withdrawn - BotFather shows the current rules, and they can change.</i>"
    )


GIFT_PREMIUM_SECONDS = 7 * 86400
GIFT_PAYLOAD_PREFIX = "gift"


def _parse_gift_payload(payload):
    """'gift:<payer_id>:<target_id>:<duration_seconds>:<stars>' -> tuple or None."""
    try:
        prefix, payer, target, secs, stars = str(payload).split(":")
        if prefix != GIFT_PAYLOAD_PREFIX:
            return None
        return int(payer), int(target), int(secs), int(stars)
    except (ValueError, TypeError):
        return None


async def send_gift_invoice(context: ContextTypes.DEFAULT_TYPE, payer_id, target_id, target_name):
    """Stars invoice (sent to the gifter's DM) for gifting Premium to target_id.
    Returns None on success or a short error string."""
    price, dur = await asyncio.to_thread(get_premium_settings)
    title = f"Gift Premium - {_format_duration_words(dur)}"[:32]
    description = (
        f"Gift {target_name} {_format_duration_words(dur)} of Premium: doubles their {DOLLAR} rewards in "
        f"Word Game and Word Scramble. You pay the Stars; they get the Premium."
    )
    try:
        await context.bot.send_invoice(
            chat_id=payer_id,
            title=title,
            description=description[:255],
            payload=f"{GIFT_PAYLOAD_PREFIX}:{payer_id}:{target_id}:{dur}:{price}",
            provider_token="",  # must be empty for Telegram Stars
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=int(price))],  # raw star count, no x100
        )
    except Forbidden:
        return "⚠️ Please start a private chat with me first (open my DM and press Start), then try /giftpremium again."
    except Exception as e:
        logger.error(f"Failed to send gift invoice to {payer_id}: {e}")
        return "⚠️ Couldn't create the invoice right now. Please try again in a moment."
    return None


async def _gift_precheckout(query):
    parsed = _parse_gift_payload(query.invoice_payload)
    if parsed is None or query.currency != "XTR":
        await query.answer(ok=False, error_message="This payment isn't recognised. Please run /giftpremium again.")
        return
    payer_id, _target, _secs, stars = parsed
    if query.from_user.id != payer_id:
        await query.answer(ok=False, error_message="This invoice belongs to a different account.")
        return
    if query.total_amount != stars:
        await query.answer(ok=False, error_message="This invoice is out of date. Please run /giftpremium again.")
        return
    await query.answer(ok=True)


async def _gift_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    payment = message.successful_payment
    parsed = _parse_gift_payload(payment.invoice_payload)
    if parsed is None:
        logger.warning(f"successful_payment with unrecognised gift payload {payment.invoice_payload!r}")
        return

    payer = message.from_user
    _payer_id, target_id, secs, _stars = parsed
    charge_id = payment.telegram_payment_charge_id

    try:
        new_expiry = await asyncio.to_thread(
            db.premium_grant_from_payment, charge_id, target_id, payment.total_amount, secs
        )
    except Exception as e:
        logger.error(f"Failed to record gift payment {charge_id}: {e}", exc_info=True)
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    f"🚨 A Premium GIFT Stars payment went through but failed to record.\n"
                    f"Payer: {payer.id}\nRecipient: {target_id}\nStars: {payment.total_amount}\n"
                    f"Charge ID: {charge_id}\nError: {e}"
                ),
            )
        except Exception:
            pass
        await message.reply_text(
            "⚠️ Your payment went through, but I hit an error delivering the gift. "
            "The owner has been notified and will sort it out."
        )
        return

    if new_expiry is None:
        return  # duplicate delivery of an already-applied charge

    remaining = _format_remaining((new_expiry - datetime.now()).total_seconds())
    target_row = await asyncio.to_thread(db.get_user_by_id, target_id)
    target_name = (target_row[1] if target_row else None) or str(target_id)

    await message.reply_html(
        f"🎁 Gift sent! <a href=\"tg://user?id={target_id}\">{esc_name(target_name)}</a> "
        f"(<code>{target_id}</code>) now has Premium.\n"
        f"⏳ Their Premium has {remaining} remaining."
    )
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                f"🎁 {payer.first_name or 'Someone'} gifted you {_format_duration_words(secs)} of Premium! "
                f"Your {DOLLAR} rewards in Word Game and Word Scramble are now doubled.\n"
                f"⏳ Time remaining: {remaining}"
            ),
        )
    except Exception:
        pass  # they may not have started the bot yet - the gift is still applied

    await notify_owner_purchase(
        context, "🎁 Premium gifted", payer, payment.total_amount,
        [f"Gifted to: {target_name} ({target_id})", f"Duration: {_format_duration_words(secs)}"],
    )


async def giftpremium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    owner_gifting = is_owner(user.id)

    # Only Premium members (and the owner) can gift.
    if not owner_gifting and not await asyncio.to_thread(is_premium, user.id):
        await update.message.reply_text(
            "⭐ Only Premium members can gift Premium to others - use /buypremium to get Premium first."
        )
        return

    usage = (
        "Usage: /giftpremium <username|ID>  (or reply to the person's message)\n"
        + (
            "Gives them 7 days of Premium, free (owner)."
            if owner_gifting
            else "You pay the Stars; they get the Premium (added on top of any time they already have)."
        )
    )
    reply_msg = update.message.reply_to_message

    if context.args:
        arg = context.args[0]
        if re.fullmatch(r"\d+", arg):
            target_id, target_name = int(arg), arg
            row = await asyncio.to_thread(db.get_user_by_id, target_id)
            if row:
                target_name = row[1] or arg
        else:
            row = await asyncio.to_thread(db.get_user_by_username, arg.lstrip("@"))
            if not row:
                await update.message.reply_text(f"❌ No player found matching {arg}.")
                return
            target_id, target_name = row[0], row[1] or arg
    elif (
        reply_msg is not None
        and reply_msg.from_user is not None
        and not getattr(reply_msg, "forum_topic_created", None)
    ):
        target_id = reply_msg.from_user.id
        target_name = reply_msg.from_user.first_name or str(target_id)
        if reply_msg.from_user.is_bot:
            await update.message.reply_text("❌ Bots can't have Premium.")
            return
    else:
        await update.message.reply_text(usage)
        return

    if is_owner(target_id):
        await update.message.reply_text("👑 The bot owner always has Premium - nothing to gift!")
        return

    if owner_gifting:
        # Unchanged owner behaviour: free 7-day gift.
        new_expiry = await asyncio.to_thread(db.premium_grant_gift, target_id, GIFT_PREMIUM_SECONDS)
        remaining = _format_remaining((new_expiry - datetime.now()).total_seconds())

        await update.message.reply_html(
            f"🎁 Gifted 7 days of Premium to "
            f'<a href="tg://user?id={target_id}">{esc_name(target_name)}</a> (<code>{target_id}</code>).\n'
            f"⏳ Their Premium now has {remaining} remaining."
        )

        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    f"🎁 You've been gifted 7 days of Premium! Your {DOLLAR} rewards in Word Game and "
                    f"Word Scramble are now doubled.\n⏳ Time remaining: {remaining}"
                ),
            )
        except Exception:
            pass  # they may not have started the bot yet - the gift is still applied
        return

    # Paying premium member.
    if target_id == user.id:
        await update.message.reply_text("ℹ️ That's you! Use /buypremium to extend your own Premium.")
        return

    if update.effective_chat.type != ChatType.PRIVATE:
        await _redirect_to_dm(
            update, context, f"gift_{target_id}",
            f"🎁 Gifts are paid for in my DM. Tap the button below and press Start to gift Premium to {target_name}.",
            "🎁 Continue in DM",
        )
        return

    err = await send_gift_invoice(context, user.id, target_id, target_name)
    if err:
        await update.message.reply_text(err)


async def premium_renewal_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        rows = await asyncio.to_thread(db.premium_get_expiring_unprompted, PREMIUM_REMINDER_WINDOW_SECONDS)
    except Exception as e:
        logger.error(f"premium reminder scan failed: {e}")
        return
    if not rows:
        return

    price, dur = await asyncio.to_thread(get_premium_settings)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⭐ Renew ({price} ⭐ / {_format_duration_words(dur)})", callback_data="buypremium")
    ]])

    for uid, expires_at in rows:
        try:
            # At-most-once per expiry window: flip the flag first, so a failed
            # send (e.g. user blocked the bot) never causes hourly repeats.
            if not await asyncio.to_thread(db.premium_mark_prompted, uid):
                continue
            left = _format_remaining((datetime.fromisoformat(expires_at) - datetime.now()).total_seconds())
            await context.bot.send_message(
                chat_id=uid,
                text=f"⭐ Your Premium expires in {left} - renew to keep your doubled {DOLLAR} rewards?",
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.info(f"premium reminder to {uid} not delivered: {e}")
        await asyncio.sleep(0.05)


def schedule_premium_reminder_job(job_queue):
    """Restart-safe: all state lives in the database, so re-registering the
    repeating job on every boot is all that's needed."""
    if job_queue is None:
        return
    if job_queue.get_jobs_by_name(PREMIUM_REMINDER_JOB_NAME):
        return
    job_queue.run_repeating(
        premium_renewal_reminder_job,
        interval=3600,
        first=60,
        name=PREMIUM_REMINDER_JOB_NAME,
    )


def main():
    print("GAME BOT: main() started", flush=True)

    db.create_tables()
    print("GAME BOT: database tables created", flush=True)

    # One-time migration: anything still sitting in the old file-based
    # OWNER_ADDED storage (from before /addwords was moved to the database)
    # gets copied into custom_words now, so it isn't lost. Safe to run every
    # startup - add_custom_words() uses ON CONFLICT DO NOTHING.
    global CUSTOM_WORDS_CACHE, REMOVED_WORDS_CACHE
    legacy_file_words = _read_owner_added_words()
    if legacy_file_words:
        db.add_custom_words(legacy_file_words)
        print(f"GAME BOT: migrated {len(legacy_file_words)} legacy file-based word(s) into the database", flush=True)

    CUSTOM_WORDS_CACHE = db.get_custom_words()
    REMOVED_WORDS_CACHE = db.get_removed_words()
    _rebuild_word_pools()
    print(
        f"GAME BOT: word pools built ({len(CUSTOM_WORDS_CACHE)} custom, "
        f"{len(REMOVED_WORDS_CACHE)} removed, {len(WORDS)} total in Word Game pool)",
        flush=True,
    )

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

    # NOTE: python-telegram-bot's TelegramObject blocks setting attributes
    # directly on a Bot *instance* (app.bot.send_message = ... raises
    # AttributeError - "can't be set"). Patching the *class* instead is not
    # blocked, since that goes through Python's normal type.__setattr__, not
    # TelegramObject's instance-level guard. The `self is app.bot` check
    # keeps this scoped to only this Application's bot even though the class
    # itself is shared process-wide with @UFFAdsBot's own Bot instance.
    try:
        _bot_class = type(app.bot)
        if not getattr(_bot_class, "_autodelete_send_patched", False):
            _original_bot_send_message = _bot_class.send_message

            async def _tracked_send_message(self, *args, **kwargs):
                result = await _original_bot_send_message(self, *args, **kwargs)
                try:
                    if self is app.bot:
                        cid = kwargs.get("chat_id")
                        if cid is None and args:
                            cid = args[0]
                        if cid is not None:
                            cid = int(cid)
                            if cid < 0 and await asyncio.to_thread(db.is_autodelete_tracking_active, cid):
                                _autodelete_tracker[cid].append(result.message_id)
                except Exception:
                    pass
                return result

            _bot_class.send_message = _tracked_send_message
            _bot_class._autodelete_send_patched = True
    except Exception:
        logger.exception(
            "GAME BOT: could not attach auto-delete message tracking - "
            "/autodelete will not delete anything until this is fixed"
        )

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
    app.add_handler(CommandHandler("ranks", ranks_command))
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
    app.add_handler(CommandHandler("controlgroup", controlgroup_command))
    app.add_handler(CommandHandler("tournamentban", tournamentban_command))
    app.add_handler(CommandHandler("tournamentunban", tournamentunban_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("sendgroupmessage", sendgroupmessage_command))
    app.add_handler(CommandHandler("maintenance", maintenance_command))
    app.add_handler(CommandHandler("adminmode", adminmode_command))
    app.add_handler(CommandHandler("autodelete", autodelete_command))
    app.add_handler(CommandHandler("groupadminmode", groupadminmode_command))
    app.add_handler(CommandHandler("setreward", setreward_command))
    app.add_handler(CommandHandler("reloadwords", reloadwords_command))
    app.add_handler(CommandHandler("shufflewords", shufflewords_command))
    app.add_handler(CommandHandler("gamedifficulty", gamedifficulty_command))
    app.add_handler(CommandHandler("addwords", addwords_command))
    app.add_handler(CommandHandler("removewords", removewords_command))
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
    app.add_handler(CommandHandler("showlevel", showlevel_command))
    app.add_handler(CommandHandler("showmedal", showmedal_command))
    app.add_handler(CommandHandler("showgamemedal", showgamemedal_command))
    app.add_handler(CommandHandler("sponsoredad", sponsoredad_command))
    app.add_handler(CommandHandler("changesponsoredad", changesponsoredad_command))

    # --- Invite & Earn -------------------------------------------------
    app.add_handler(CommandHandler("invitereward", invitereward_command))
    app.add_handler(CommandHandler("claim", claim_command))
    app.add_handler(CommandHandler("settournament", settournament_command))
    app.add_handler(CommandHandler("tournament", tournament_command))
    app.add_handler(CommandHandler("join", join_command))
    app.add_handler(CommandHandler("starttournament", starttournament_command))
    app.add_handler(CommandHandler("stoptournament", stoptournament_command))
    app.add_handler(CommandHandler("resumetournament", resumetournament_command))
    app.add_handler(CommandHandler("extendlobby", extendlobby_command))
    app.add_handler(CommandHandler("giftpremium", giftpremium_command))
    app.add_handler(CommandHandler("buypremium", buypremium_command))
    app.add_handler(CommandHandler("premiumstatus", premiumstatus_command))
    app.add_handler(CommandHandler("buycash", buycash_command))
    app.add_handler(CommandHandler("boostrank", boostrank_command))
    app.add_handler(CommandHandler("boostlevel", boostlevel_command))
    app.add_handler(CommandHandler("editbuycashtext", editbuycashtext_command))
    app.add_handler(CommandHandler("setdollarpack", setdollarpack_command))
    app.add_handler(CommandHandler("transfer", transfer_command))
    app.add_handler(CommandHandler("request", request_command))
    app.add_handler(CommandHandler("deleterequestprompt", deleterequestprompt_command))
    app.add_handler(CommandHandler("searchcommand", searchcommand_command))
    app.add_handler(CommandHandler("fullhelp", fullhelp_command))
    app.add_handler(CommandHandler("setpremium", setpremium_command))
    app.add_handler(CommandHandler("seepremiumplayers", seepremiumplayers_command))
    app.add_handler(CommandHandler("starbalance", starbalance_command))
    app.add_handler(CommandHandler("withdraw", withdraw_command))
    app.add_handler(PreCheckoutQueryHandler(premium_precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, premium_successful_payment_callback))
    # Specific callback patterns must be registered BEFORE the catch-all button handler.
    app.add_handler(CallbackQueryHandler(buypremium_button, pattern=r"^buypremium$"))
    app.add_handler(CallbackQueryHandler(premium_features_button, pattern=r"^pf:(show|back:\d+)$"))
    app.add_handler(CallbackQueryHandler(buydollar_button, pattern=r"^bd:\d+:\d+:\d+:\d+(:[crl])?$"))
    app.add_handler(CallbackQueryHandler(transfer_button, pattern=r"^tf:[yn]:[0-9a-f]+$"))
    app.add_handler(CallbackQueryHandler(request_button, pattern=r"^rq:[yn]:\d+$"))
    app.add_handler(CallbackQueryHandler(fullhelp_button, pattern=r"^fh:\d+:[gstpo]$"))
    app.add_handler(CallbackQueryHandler(premium_players_page_button, pattern=r"^spp:\d+$"))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("inviteandearn", inviteandearn_command))

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
        scramble_group_router,
    ), group=1)

    if app.job_queue is not None:
        # A timer that fires even 1-2 seconds late (event loop briefly busy)
        # is DROPPED by APScheduler under its default 1-second grace window.
        # A dropped timeout/next-round timer is exactly what makes a chain
        # go quiet, so late jobs must still run.
        try:
            app.job_queue.scheduler._job_defaults["misfire_grace_time"] = None
            app.job_queue.scheduler._job_defaults["coalesce"] = True
        except Exception:
            logger.warning("GAME BOT: could not relax scheduler misfire grace time")

        restore_stale_scramble_rounds(app.job_queue)
        restore_stale_slowmode_waits(app.job_queue)
        restore_stale_word_games(app.job_queue)
        restore_autodelete_jobs(app.job_queue)
        restore_scheduled_deletions(app.job_queue)
        restore_money_requests(app.job_queue)
        restore_stale_invite_rewards(app.job_queue)
        schedule_premium_reminder_job(app.job_queue)
        restore_stale_tournaments(app.job_queue)
        schedule_tournament_pause_check(app.job_queue)
        schedule_scramble_watchdog(app.job_queue)

        if db.get_config("scramble_mode_enabled", "0") == "1":
            try:
                interval = int(db.get_config("scramble_mode_interval_seconds", "3600"))
            except ValueError:
                interval = 3600
            schedule_scramblemode_job(app.job_queue, interval)

    return app


if __name__ == "__main__":
    pass