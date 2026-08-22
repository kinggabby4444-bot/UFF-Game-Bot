"""
BACKUP / REFERENCE ONLY - this is the previous SQLite-backed
database.py, kept here in case you need to compare against it or roll
back. It is NOT imported by main.py anymore - database.py (in this same
folder) is now the active, PostgreSQL-backed version. See
SETUP_POSTGRES.md for the migration.
"""

import sqlite3
import re
from datetime import datetime, timedelta

DB_NAME = "data/uff_game.db"

DAILY_REWARD = 50


def connect():
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_tables():
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        name TEXT,
        coins INTEGER DEFAULT 100,
        level INTEGER DEFAULT 1,
        rank TEXT DEFAULT '🌱 Beginner',
        last_daily_claim TEXT,
        auto_lead INTEGER DEFAULT 1
    )
    """)

    # One active word-game state per chat.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS word_games (
        chat_id INTEGER PRIMARY KEY,
        leader_id INTEGER,
        leader_name TEXT,
        secret_word TEXT,
        game_active INTEGER DEFAULT 0
    )
    """)

    # Tracks a user who is expected to send a DM (e.g. after pressing
    # "Write Word") and which chat/game that input belongs to.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS pending_input (
        user_id INTEGER PRIMARY KEY,
        chat_id INTEGER,
        action TEXT
    )
    """)

    # Per-chat leaderboard balance.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chat_scores (
        user_id INTEGER,
        chat_id INTEGER,
        name TEXT,
        coins INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, chat_id)
    )
    """)

    # Global leaderboard balance (sum across all chats).
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS global_scores (
        user_id INTEGER PRIMARY KEY,
        name TEXT,
        coins INTEGER DEFAULT 0
    )
    """)

    # Words already used in a chat's current no-repeat cycle. Once every
    # word in the pool has been used for a chat, the cycle resets.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS used_words (
        chat_id INTEGER,
        word TEXT,
        PRIMARY KEY (chat_id, word)
    )
    """)

    # Rolling transcript of what the current leader has typed in the
    # group since they became leader - powers /hint.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS explain_log (
        chat_id INTEGER PRIMARY KEY,
        leader_id INTEGER,
        log TEXT DEFAULT ''
    )
    """)

    # Every group the bot has ever seen activity in.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS groups (
        chat_id INTEGER PRIMARY KEY,
        title TEXT,
        member_count INTEGER DEFAULT 0,
        first_seen TEXT,
        games_played INTEGER DEFAULT 0
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS banned_users (
        user_id INTEGER PRIMARY KEY,
        banned_at TEXT,
        banned_by INTEGER
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS banned_groups (
        chat_id INTEGER PRIMARY KEY,
        banned_at TEXT,
        banned_by INTEGER
    )
    """)

    # Owner-tunable numbers (rewards/penalties) and switches (maintenance).
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bot_config (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    # Simple counters for /stats.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bot_stats (
        key TEXT PRIMARY KEY,
        value INTEGER DEFAULT 0
    )
    """)

    # /stopvote ballots, cleared once a game stops or a new one starts.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS stop_votes (
        chat_id INTEGER,
        user_id INTEGER,
        name TEXT,
        PRIMARY KEY (chat_id, user_id)
    )
    """)

    # Audit trail for owner /topup actions.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS topup_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id INTEGER,
        target_name TEXT,
        amount INTEGER,
        owner_id INTEGER,
        timestamp TEXT
    )
    """)

    # Bot Admins appointed by the owner (persists across restarts).
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bot_admins (
        user_id INTEGER PRIMARY KEY,
        name TEXT,
        username TEXT,
        added_by INTEGER,
        added_at TEXT
    )
    """)

    # Permanent log of every daily-reward claim, so /claimeddailyreward
    # still works after a restart and for any past date.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS daily_reward_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT,
        username TEXT,
        amount INTEGER,
        claim_date TEXT,
        claimed_at TEXT
    )
    """)

    conn.commit()

    # --- Migration: patch columns onto tables that already existed
    # before this column was added (CREATE TABLE IF NOT EXISTS won't
    # alter a table that's already there). Safe to run every startup.
    cursor.execute("PRAGMA table_info(users)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    if "auto_lead" not in existing_columns:
        cursor.execute(
            "ALTER TABLE users ADD COLUMN auto_lead INTEGER DEFAULT 1"
        )
        conn.commit()

    cursor.execute("PRAGMA table_info(word_games)")
    existing_wg_columns = {row[1] for row in cursor.fetchall()}

    if "explaining_started" not in existing_wg_columns:
        cursor.execute(
            "ALTER TABLE word_games ADD COLUMN explaining_started INTEGER DEFAULT 0"
        )
        conn.commit()

    if "username" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN username TEXT")
        conn.commit()

    if "games_played" not in existing_columns:
        cursor.execute(
            "ALTER TABLE users ADD COLUMN games_played INTEGER DEFAULT 0"
        )
        conn.commit()

    if "games_won" not in existing_columns:
        cursor.execute(
            "ALTER TABLE users ADD COLUMN games_won INTEGER DEFAULT 0"
        )
        conn.commit()

    if "last_active" not in existing_columns:
        cursor.execute(
            "ALTER TABLE users ADD COLUMN last_active TEXT"
        )
        conn.commit()

    cursor.execute("PRAGMA table_info(groups)")
    existing_group_columns = {row[1] for row in cursor.fetchall()}

    if "last_active" not in existing_group_columns:
        cursor.execute(
            "ALTER TABLE groups ADD COLUMN last_active TEXT"
        )
        conn.commit()

    conn.close()


# ---------------------------------------------------------------------
# Existing profile / daily reward functions (unchanged behaviour)
# ---------------------------------------------------------------------

def add_user(user_id, name, username=None):
    conn = connect()
    cursor = conn.cursor()

    now = datetime.now().isoformat()

    cursor.execute(
        "INSERT OR IGNORE INTO users (id, name, username, last_active) VALUES (?, ?, ?, ?)",
        (user_id, name, username, now)
    )

    # Keep name/username/last_active fresh on every interaction (people
    # change names, and last_active drives the /users recency tie-break).
    cursor.execute(
        "UPDATE users SET name=?, username=?, last_active=? WHERE id=?",
        (name, username, now, user_id)
    )

    conn.commit()
    conn.close()


def get_user(user_id):
    """Profile row, with `coins` sourced from global_scores so the
    profile always shows the same merged balance as the leaderboards
    (daily reward + all game winnings combined)."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT u.id, u.name, COALESCE(g.coins, 0), u.level, u.rank, u.last_daily_claim, u.auto_lead
        FROM users u
        LEFT JOIN global_scores g ON g.user_id = u.id
        WHERE u.id=?
    """, (user_id,))

    user = cursor.fetchone()

    conn.close()

    return user


def get_global_coins(user_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("SELECT coins FROM global_scores WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    return row[0] if row else 0


def award_global_coins(user_id, name, amount):
    """Add coins that aren't tied to any particular group chat (e.g.
    the daily reward) straight into the merged global balance."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO global_scores (user_id, name, coins)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            coins = coins + excluded.coins,
            name = excluded.name
    """, (user_id, name, amount))

    conn.commit()
    conn.close()

    if amount > 0:
        increment_stat("total_rewards_given", amount)


def claim_daily_reward(user_id, name, username=None):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT last_daily_claim FROM users WHERE id=?",
        (user_id,)
    )

    row = cursor.fetchone()
    last_claim = row[0] if row else None

    now = datetime.now()

    if last_claim:
        last_claim_dt = datetime.fromisoformat(last_claim)

        if now - last_claim_dt < timedelta(hours=24):
            remaining = timedelta(hours=24) - (now - last_claim_dt)

            conn.close()

            return False, remaining

    cursor.execute(
        "UPDATE users SET last_daily_claim=? WHERE id=?",
        (now.isoformat(), user_id)
    )

    conn.commit()
    conn.close()

    amount = int(get_config("reward_daily", DAILY_REWARD))
    award_global_coins(user_id, name, amount)
    log_daily_claim(user_id, name, username, amount)

    return True, get_global_coins(user_id)


# ---------------------------------------------------------------------
# Persistent daily-reward claim log - powers /claimeddailyreward
# ---------------------------------------------------------------------

def log_daily_claim(user_id, name, username, amount):
    conn = connect()
    cursor = conn.cursor()

    now = datetime.now()

    cursor.execute("""
        INSERT INTO daily_reward_log (user_id, name, username, amount, claim_date, claimed_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, name, username, amount, now.strftime("%Y-%m-%d"), now.isoformat()))

    conn.commit()
    conn.close()


def get_daily_claims(date_str):
    """Every claim recorded for a given YYYY-MM-DD date, oldest first."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT user_id, name, username, amount, claimed_at
        FROM daily_reward_log
        WHERE claim_date=?
        ORDER BY claimed_at ASC
    """, (date_str,))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_daily_totals(date_str):
    """(total_players, total_amount_distributed) for a given date."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*), COALESCE(SUM(amount), 0)
        FROM daily_reward_log
        WHERE claim_date=?
    """, (date_str,))

    row = cursor.fetchone()
    conn.close()

    return row if row else (0, 0)


def get_leaderboard():
    """Legacy global profile leaderboard (users.coins) - superseded by
    get_global_leaderboard(); kept only so nothing else breaks if it's
    still referenced somewhere."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT name, coins
        FROM users
        ORDER BY coins DESC, level DESC
        LIMIT 10
    """)

    leaderboard = cursor.fetchall()

    conn.close()

    return leaderboard


def increment_user_game_stats(user_id, played=0, won=0):
    """Bump a user's personal games_played / games_won counters. Used by
    the owner-only player profile view under /topplayers."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE users
        SET games_played = COALESCE(games_played, 0) + ?,
            games_won = COALESCE(games_won, 0) + ?
        WHERE id=?
    """, (played, won, user_id))

    conn.commit()
    conn.close()


def get_full_player_profile(user_id):
    """(id, name, username, balance, games_played, games_won) for the
    owner-only interactive /topplayers detail view."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT u.id, u.name, u.username, COALESCE(g.coins, 0),
               COALESCE(u.games_played, 0), COALESCE(u.games_won, 0)
        FROM users u
        LEFT JOIN global_scores g ON g.user_id = u.id
        WHERE u.id=?
    """, (user_id,))

    row = cursor.fetchone()
    conn.close()

    return row


def get_player_position(user_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, name, coins
        FROM users
        ORDER BY coins DESC, id ASC
    """)

    players = cursor.fetchall()

    position = None
    coins = 0

    for index, player in enumerate(players, start=1):
        if player[0] == user_id:
            position = index
            coins = player[2]
            break

    conn.close()

    return position, coins


# ---------------------------------------------------------------------
# Auto-lead setting (per user, set from the bot's private chat)
# ---------------------------------------------------------------------

def get_auto_lead(user_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("SELECT auto_lead FROM users WHERE id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    if row is None:
        return True

    return bool(row[0])


def set_auto_lead(user_id, enabled: bool):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE users SET auto_lead=? WHERE id=?",
        (1 if enabled else 0, user_id)
    )

    conn.commit()
    conn.close()


def toggle_auto_lead(user_id):
    current = get_auto_lead(user_id)
    set_auto_lead(user_id, not current)
    return not current


# ---------------------------------------------------------------------
# Word game state (per chat)
# ---------------------------------------------------------------------

def start_game(chat_id):
    """Create/reset the game row for a chat with no leader yet."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO word_games (chat_id, leader_id, leader_name, secret_word, game_active, explaining_started)
        VALUES (?, NULL, NULL, NULL, 1, 0)
        ON CONFLICT(chat_id) DO UPDATE SET
            leader_id=NULL, leader_name=NULL, secret_word=NULL, game_active=1, explaining_started=0
    """, (chat_id,))

    conn.commit()
    conn.close()


def set_leader(chat_id, leader_id, leader_name, word):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO word_games (chat_id, leader_id, leader_name, secret_word, game_active, explaining_started)
        VALUES (?, ?, ?, ?, 1, 0)
        ON CONFLICT(chat_id) DO UPDATE SET
            leader_id=excluded.leader_id,
            leader_name=excluded.leader_name,
            secret_word=excluded.secret_word,
            game_active=1,
            explaining_started=0
    """, (chat_id, leader_id, leader_name, word))

    conn.commit()
    conn.close()


def get_game(chat_id):
    """Returns (chat_id, leader_id, leader_name, secret_word, game_active) or None."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT chat_id, leader_id, leader_name, secret_word, game_active
        FROM word_games WHERE chat_id=?
    """, (chat_id,))

    row = cursor.fetchone()
    conn.close()

    return row


def set_word(chat_id, word):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE word_games SET secret_word=? WHERE chat_id=?",
        (word, chat_id)
    )

    conn.commit()
    conn.close()


def clear_leader(chat_id):
    """Round over (drop / reveal) - keep the game 'active' (still open),
    but wipe the leader/word so a new volunteer can be picked."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE word_games
        SET leader_id=NULL, leader_name=NULL, secret_word=NULL, explaining_started=0
        WHERE chat_id=?
    """, (chat_id,))

    conn.commit()
    conn.close()


def end_game(chat_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE word_games
        SET leader_id=NULL, leader_name=NULL, secret_word=NULL, game_active=0, explaining_started=0
        WHERE chat_id=?
    """, (chat_id,))

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Pending input (used for the "Write Word" DM flow)
# ---------------------------------------------------------------------

def set_pending_input(user_id, chat_id, action):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO pending_input (user_id, chat_id, action)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id, action=excluded.action
    """, (user_id, chat_id, action))

    conn.commit()
    conn.close()


def get_pending_input(user_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT chat_id, action FROM pending_input WHERE user_id=?",
        (user_id,)
    )

    row = cursor.fetchone()
    conn.close()

    return row


def clear_pending_input(user_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM pending_input WHERE user_id=?", (user_id,))

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Coins: dual-write to per-chat and global tables
# ---------------------------------------------------------------------

def award_coins(user_id, chat_id, name, amount):
    """Adjust coins (amount may be negative) in both the per-chat and
    global tables in a single transaction, so they never drift apart."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO chat_scores (user_id, chat_id, name, coins)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, chat_id) DO UPDATE SET
            coins = coins + excluded.coins,
            name = excluded.name
    """, (user_id, chat_id, name, amount))

    cursor.execute("""
        INSERT INTO global_scores (user_id, name, coins)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            coins = coins + excluded.coins,
            name = excluded.name
    """, (user_id, name, amount))

    conn.commit()
    conn.close()

    if amount > 0:
        increment_stat("total_rewards_given", amount)


def get_chat_leaderboard(chat_id, limit=10):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT name, coins
        FROM chat_scores
        WHERE chat_id=?
        ORDER BY coins DESC
        LIMIT ?
    """, (chat_id, limit))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_global_leaderboard(limit=10):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT name, coins
        FROM global_scores
        ORDER BY coins DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    return rows


# ---------------------------------------------------------------------
# Owner-only interactive rankings: same boards, but with user_id included
# so each row can be turned into a clickable button.
# ---------------------------------------------------------------------

def get_chat_leaderboard_with_ids(chat_id, limit=10):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT user_id, name, coins
        FROM chat_scores
        WHERE chat_id=?
        ORDER BY coins DESC
        LIMIT ?
    """, (chat_id, limit))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_global_leaderboard_with_ids(limit=10):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT user_id, name, coins
        FROM global_scores
        ORDER BY coins DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_chat_position(chat_id, user_id):
    """(position, name, coins) of user_id within chat_id's leaderboard,
    or None if they have no score there yet."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT user_id, name, coins
        FROM chat_scores
        WHERE chat_id=?
        ORDER BY coins DESC, user_id ASC
    """, (chat_id,))

    rows = cursor.fetchall()
    conn.close()

    for index, row in enumerate(rows, start=1):
        if row[0] == user_id:
            return index, row[1], row[2]

    return None


def get_global_position(user_id):
    """(position, name, coins) of user_id within the global leaderboard,
    or None if they have no score there yet."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT user_id, name, coins
        FROM global_scores
        ORDER BY coins DESC, user_id ASC
    """)

    rows = cursor.fetchall()
    conn.close()

    for index, row in enumerate(rows, start=1):
        if row[0] == user_id:
            return index, row[1], row[2]

    return None


# ---------------------------------------------------------------------
# No-repeat word pool (per chat)
# ---------------------------------------------------------------------

def get_used_words(chat_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("SELECT word FROM used_words WHERE chat_id=?", (chat_id,))
    rows = cursor.fetchall()
    conn.close()

    return {row[0] for row in rows}


def mark_word_used(chat_id, word):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "INSERT OR IGNORE INTO used_words (chat_id, word) VALUES (?, ?)",
        (chat_id, word)
    )

    conn.commit()
    conn.close()


def reset_used_words(chat_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM used_words WHERE chat_id=?", (chat_id,))

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# "Explaining started?" flag - powers the ran-out-of-time auto-timeout
# ---------------------------------------------------------------------

def set_explaining_started(chat_id, started: bool):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE word_games SET explaining_started=? WHERE chat_id=?",
        (1 if started else 0, chat_id)
    )

    conn.commit()
    conn.close()


def is_explaining_started(chat_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT explaining_started FROM word_games WHERE chat_id=?",
        (chat_id,)
    )

    row = cursor.fetchone()
    conn.close()

    return bool(row[0]) if row else False


# ---------------------------------------------------------------------
# Explain log - rolling transcript of the leader's clues, powers /hint
# ---------------------------------------------------------------------

def start_explain_log(chat_id, leader_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO explain_log (chat_id, leader_id, log)
        VALUES (?, ?, '')
        ON CONFLICT(chat_id) DO UPDATE SET leader_id=excluded.leader_id, log=''
    """, (chat_id, leader_id))

    conn.commit()
    conn.close()


def append_explain_log(chat_id, leader_id, text):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT leader_id, log FROM explain_log WHERE chat_id=?", (chat_id,)
    )
    row = cursor.fetchone()

    if row is None or row[0] != leader_id:
        # No log started yet for this leader (edge case) - start fresh.
        cursor.execute("""
            INSERT INTO explain_log (chat_id, leader_id, log)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET leader_id=excluded.leader_id, log=excluded.log
        """, (chat_id, leader_id, text))
    else:
        new_log = (row[1] + "\n" + text) if row[1] else text
        cursor.execute(
            "UPDATE explain_log SET log=? WHERE chat_id=?",
            (new_log, chat_id)
        )

    conn.commit()
    conn.close()


def get_explain_log(chat_id, leader_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT leader_id, log FROM explain_log WHERE chat_id=?", (chat_id,)
    )
    row = cursor.fetchone()
    conn.close()

    if row is None or row[0] != leader_id:
        return ""

    return row[1]

# ---------------------------------------------------------------------
# Owner admin panel: groups registry
# ---------------------------------------------------------------------

def register_group(chat_id, title):
    conn = connect()
    cursor = conn.cursor()

    now = datetime.now().isoformat()

    cursor.execute("""
        INSERT INTO groups (chat_id, title, first_seen, last_active)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, last_active=excluded.last_active
    """, (chat_id, title, now, now))

    conn.commit()
    conn.close()


def update_group_member_count(chat_id, count):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE groups SET member_count=? WHERE chat_id=?",
        (count, chat_id)
    )

    conn.commit()
    conn.close()


def increment_group_games(chat_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE groups SET games_played = games_played + 1, last_active=? WHERE chat_id=?",
        (datetime.now().isoformat(), chat_id)
    )

    conn.commit()
    conn.close()


def get_users_count():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_users_page(offset, limit):
    """Sorted, LIMIT/OFFSET page of users for the owner-only /users list:
    highest balance first, ties broken by whoever interacted with the
    bot most recently. Done entirely in SQL so it stays fast no matter
    how many thousands of users are registered - nothing is ever
    fetched into Python beyond the single page requested."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT u.id, u.name, u.username, COALESCE(g.coins, 0), u.last_active
        FROM users u
        LEFT JOIN global_scores g ON g.user_id = u.id
        ORDER BY COALESCE(g.coins, 0) DESC, u.last_active DESC
        LIMIT ? OFFSET ?
    """, (limit, offset))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_groups_count():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM groups")
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_groups_page(offset, limit):
    """Sorted, LIMIT/OFFSET page of groups for the owner-only /groups
    list: most games played first, ties broken by whichever group was
    active most recently. Same SQL-level pagination as get_users_page()
    for the same reason - stays fast at any scale."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT chat_id, title, member_count, games_played, last_active
        FROM groups
        ORDER BY games_played DESC, last_active DESC
        LIMIT ? OFFSET ?
    """, (limit, offset))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_all_groups():
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT chat_id, title, member_count, games_played
        FROM groups
        ORDER BY first_seen ASC
    """)

    rows = cursor.fetchall()
    conn.close()

    return rows


def search_groups(query):
    conn = connect()
    cursor = conn.cursor()

    query = query.strip()

    if re.fullmatch(r"-?\d+", query):
        cursor.execute("""
            SELECT chat_id, title, member_count, games_played
            FROM groups WHERE chat_id=?
        """, (int(query),))
    else:
        cursor.execute("""
            SELECT chat_id, title, member_count, games_played
            FROM groups WHERE title LIKE ?
            ORDER BY first_seen ASC
        """, (f"%{query}%",))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_group_count():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM groups")
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_top_groups(limit=10):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT title, games_played
        FROM groups
        ORDER BY games_played DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_top_groups_full(limit=10):
    """Same ranking as get_top_groups(), but includes chat_id so the
    owner-only interactive /topgroups view can turn each row into a
    clickable button."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT chat_id, title, games_played
        FROM groups
        ORDER BY games_played DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_group_details(chat_id):
    """(chat_id, title, member_count, games_played, first_seen) for the
    owner-only interactive /topgroups detail view."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT chat_id, title, member_count, games_played, first_seen
        FROM groups
        WHERE chat_id=?
    """, (chat_id,))

    row = cursor.fetchone()
    conn.close()

    return row


# ---------------------------------------------------------------------
# Owner admin panel: users registry / search
# ---------------------------------------------------------------------

def get_user_count():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_all_users(limit=None):
    conn = connect()
    cursor = conn.cursor()

    sql = """
        SELECT u.id, u.name, u.username, COALESCE(g.coins, 0)
        FROM users u
        LEFT JOIN global_scores g ON g.user_id = u.id
        ORDER BY u.id ASC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    cursor.execute(sql)
    rows = cursor.fetchall()
    conn.close()

    return rows


def search_users(query):
    conn = connect()
    cursor = conn.cursor()

    query = query.strip().lstrip("@")

    if re.fullmatch(r"\d+", query):
        cursor.execute("""
            SELECT u.id, u.name, u.username, COALESCE(g.coins, 0)
            FROM users u
            LEFT JOIN global_scores g ON g.user_id = u.id
            WHERE u.id=?
        """, (int(query),))
    else:
        cursor.execute("""
            SELECT u.id, u.name, u.username, COALESCE(g.coins, 0)
            FROM users u
            LEFT JOIN global_scores g ON g.user_id = u.id
            WHERE u.name LIKE ? OR u.username LIKE ?
            ORDER BY u.id ASC
        """, (f"%{query}%", f"%{query}%"))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_user_by_username(username):
    conn = connect()
    cursor = conn.cursor()

    username = username.strip().lstrip("@")

    cursor.execute("""
        SELECT u.id, u.name, u.username, COALESCE(g.coins, 0)
        FROM users u
        LEFT JOIN global_scores g ON g.user_id = u.id
        WHERE u.username = ? COLLATE NOCASE
    """, (username,))

    row = cursor.fetchone()
    conn.close()

    return row


def get_user_by_id(user_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT u.id, u.name, u.username, COALESCE(g.coins, 0)
        FROM users u
        LEFT JOIN global_scores g ON g.user_id = u.id
        WHERE u.id = ?
    """, (user_id,))

    row = cursor.fetchone()
    conn.close()

    return row


# ---------------------------------------------------------------------
# Owner admin panel: bans
# ---------------------------------------------------------------------

def ban_user(user_id, banned_by):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO banned_users (user_id, banned_at, banned_by)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET banned_at=excluded.banned_at, banned_by=excluded.banned_by
    """, (user_id, datetime.now().isoformat(), banned_by))

    conn.commit()
    conn.close()


def unban_user(user_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM banned_users WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def is_user_banned(user_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM banned_users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def ban_group(chat_id, banned_by):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO banned_groups (chat_id, banned_at, banned_by)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET banned_at=excluded.banned_at, banned_by=excluded.banned_by
    """, (chat_id, datetime.now().isoformat(), banned_by))

    conn.commit()
    conn.close()


def unban_group(chat_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM banned_groups WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def is_group_banned(chat_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM banned_groups WHERE chat_id=?", (chat_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None


# ---------------------------------------------------------------------
# Owner admin panel: stats counters
# ---------------------------------------------------------------------

def increment_stat(key, amount=1):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO bot_stats (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = value + excluded.value
    """, (key, amount))

    conn.commit()
    conn.close()


def get_stat(key):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM bot_stats WHERE key=?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0


# ---------------------------------------------------------------------
# Owner admin panel: tunable config (rewards, maintenance mode)
# ---------------------------------------------------------------------

def set_config(key, value):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO bot_config (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, str(value)))

    conn.commit()
    conn.close()


def get_config(key, default=None):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM bot_config WHERE key=?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default


# ---------------------------------------------------------------------
# /stopvote ballots
# ---------------------------------------------------------------------

def add_stop_vote(chat_id, user_id, name):
    """Returns True if this was a new vote, False if they'd already voted."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "INSERT OR IGNORE INTO stop_votes (chat_id, user_id, name) VALUES (?, ?, ?)",
        (chat_id, user_id, name)
    )
    added = cursor.rowcount > 0

    conn.commit()
    conn.close()

    return added


def get_stop_vote_count(chat_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM stop_votes WHERE chat_id=?", (chat_id,))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def clear_stop_votes(chat_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM stop_votes WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# /topup audit log
# ---------------------------------------------------------------------

def log_topup(target_id, target_name, amount, owner_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO topup_log (target_id, target_name, amount, owner_id, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, (target_id, target_name, amount, owner_id, datetime.now().isoformat()))

    conn.commit()
    conn.close()


def get_active_games_count():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM word_games WHERE leader_id IS NOT NULL")
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_stop_vote_names(chat_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM stop_votes WHERE chat_id=? ORDER BY rowid ASC",
        (chat_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------
# Bot Admins (appointed by the owner via /addadmin) - persists across
# restarts. Bot Admins are separate from, and lesser than, the Owner.
# ---------------------------------------------------------------------

def add_bot_admin(user_id, name, username, added_by):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO bot_admins (user_id, name, username, added_by, added_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            name=excluded.name,
            username=excluded.username,
            added_by=excluded.added_by,
            added_at=excluded.added_at
    """, (user_id, name, username, added_by, datetime.now().isoformat()))

    conn.commit()
    conn.close()


def remove_bot_admin(user_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM bot_admins WHERE user_id=?", (user_id,))
    removed = cursor.rowcount > 0

    conn.commit()
    conn.close()

    return removed


def is_bot_admin(user_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM bot_admins WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def get_bot_admins():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, name, username, added_at
        FROM bot_admins
        ORDER BY added_at ASC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows