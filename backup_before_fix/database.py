"""
Shared PostgreSQL data-access layer for @Uff_GameBot (and @UFFAdsBot -
see the "shared tables" section at the bottom of create_tables()).

Behavioural note: every timestamp in this file is stored as TEXT holding
an ISO-8601 string (datetime.now().isoformat()), exactly like the SQLite
version this replaces - never a native TIMESTAMP column. That's
deliberate: it keeps every `datetime.fromisoformat(...)` call elsewhere
in the codebase (in this file and in main.py) working unchanged, since
psycopg2 returns TEXT columns as plain Python str, same as sqlite3 did.

Connection handling: a small pooled-connection wrapper (see
PooledConnection below) means every function below keeps the exact same
connect() / cursor() / commit() / close() pattern the SQLite version
used - close() returns the connection to the pool instead of actually
closing the socket, so nothing else in this file had to change shape.
"""

import os
import re
from datetime import datetime, timedelta

import psycopg2
import psycopg2.pool
from dotenv import load_dotenv

load_dotenv()
DAILY_REWARD = 50

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Set it to a PostgreSQL connection "
        "string, e.g. postgresql://user:password@host:5432/uff_shared"
    )

_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=int(os.environ.get("GAME_DB_POOL_MAX", "10")),
    dsn=DATABASE_URL,
)


class PooledConnection:
    """Wraps a connection checked out of the pool so existing code that
    calls conn.close() releases it back to the pool instead of closing
    the underlying socket."""

    def __init__(self, raw_conn, pool):
        self._conn = raw_conn
        self._pool = pool
        self._closed = False

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if not self._closed:
            self._pool.putconn(self._conn)
            self._closed = True


def connect():
    raw = _pool.getconn()
    return PooledConnection(raw, _pool)


def create_tables():
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id BIGINT PRIMARY KEY,
        name TEXT,
        username TEXT,
        level INTEGER DEFAULT 1,
        rank TEXT DEFAULT '🌱 Beginner',
        last_daily_claim TEXT,
        auto_lead INTEGER DEFAULT 1,
        games_played INTEGER DEFAULT 0,
        games_won INTEGER DEFAULT 0,
        last_active TEXT
    )
    """)

    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_chat_id BIGINT")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS word_games (
        chat_id BIGINT PRIMARY KEY,
        leader_id BIGINT,
        leader_name TEXT,
        secret_word TEXT,
        game_active INTEGER DEFAULT 0,
        explaining_started INTEGER DEFAULT 0
    )
    """)

    cursor.execute("ALTER TABLE word_games ADD COLUMN IF NOT EXISTS leader_message_id BIGINT")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS pending_input (
        user_id BIGINT PRIMARY KEY,
        chat_id BIGINT,
        action TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chat_scores (
        user_id BIGINT,
        chat_id BIGINT,
        name TEXT,
        coins INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, chat_id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS global_scores (
        user_id BIGINT PRIMARY KEY,
        name TEXT,
        coins INTEGER DEFAULT 0
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS used_words (
        chat_id BIGINT,
        word TEXT,
        PRIMARY KEY (chat_id, word)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS explain_log (
        chat_id BIGINT PRIMARY KEY,
        leader_id BIGINT,
        log TEXT DEFAULT ''
    )
    """)

    # Every group the bot has ever seen activity in.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS groups (
        chat_id BIGINT PRIMARY KEY,
        title TEXT,
        member_count INTEGER DEFAULT 0,
        first_seen TEXT,
        games_played INTEGER DEFAULT 0,
        last_active TEXT
    )
    """)

    # Migration: tracks when the bot was removed from a group (kicked,
    # left, or banned-and-left). NULL means the bot is currently in the
    # group. Powers /botgroups' current-vs-removed breakdown. Safe/
    # no-op if the column already exists.
    cursor.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS left_at TEXT")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS banned_users (
        user_id BIGINT PRIMARY KEY,
        banned_at TEXT,
        banned_by BIGINT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS banned_groups (
        chat_id BIGINT PRIMARY KEY,
        banned_at TEXT,
        banned_by BIGINT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bot_config (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bot_stats (
        key TEXT PRIMARY KEY,
        value BIGINT DEFAULT 0
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS stop_votes (
        chat_id BIGINT,
        user_id BIGINT,
        name TEXT,
        seq BIGSERIAL,
        PRIMARY KEY (chat_id, user_id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS topup_log (
        id BIGSERIAL PRIMARY KEY,
        target_id BIGINT,
        target_name TEXT,
        amount INTEGER,
        owner_id BIGINT,
        timestamp TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bot_admins (
        user_id BIGINT PRIMARY KEY,
        name TEXT,
        username TEXT,
        added_by BIGINT,
        added_at TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS daily_reward_log (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT,
        name TEXT,
        username TEXT,
        amount INTEGER,
        claim_date TEXT,
        claimed_at TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ad_subscriptions (
        id BIGSERIAL PRIMARY KEY,
        application_id BIGINT,
        applicant_id BIGINT NOT NULL,
        business_name TEXT,
        ad_link TEXT,
        ad_text TEXT,
        ad_type TEXT,
        duration_days INTEGER,
        start_at TEXT,
        end_at TEXT,
        status TEXT DEFAULT 'scheduled',
        created_at TEXT,
        total_views INTEGER DEFAULT 0,
        total_clicks INTEGER DEFAULT 0,
        reminder_7d_sent INTEGER DEFAULT 0,
        reminder_3d_sent INTEGER DEFAULT 0,
        reminder_24h_sent INTEGER DEFAULT 0,
        reminder_expiry_sent INTEGER DEFAULT 0,
        renewed_from BIGINT
    )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_active ON users (last_active DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users (username)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_global_scores_coins ON global_scores (coins DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_scores_chat_coins ON chat_scores (chat_id, coins DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_groups_games ON groups (games_played DESC, last_active DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_daily_reward_log_date ON daily_reward_log (claim_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ad_subscriptions_status ON ad_subscriptions (status, end_at)")

    # ===================================================================
    # WORD SCRAMBLE - separate game, isolated from every Word Game table
    # above.
    # ===================================================================

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scramble_games (
        chat_id BIGINT PRIMARY KEY,
        word TEXT,
        scrambled TEXT,
        started_at TEXT,
        round_active INTEGER DEFAULT 0
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scramble_used_words (
        chat_id BIGINT,
        word TEXT,
        PRIMARY KEY (chat_id, word)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scramble_chat_stats (
        user_id BIGINT,
        chat_id BIGINT,
        name TEXT,
        wins INTEGER DEFAULT 0,
        earned INTEGER DEFAULT 0,
        current_streak INTEGER DEFAULT 0,
        best_streak INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, chat_id)
    )
    """)

    cursor.execute("ALTER TABLE scramble_chat_stats ADD COLUMN IF NOT EXISTS played INTEGER DEFAULT 0")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scramble_global_stats (
        user_id BIGINT PRIMARY KEY,
        name TEXT,
        wins INTEGER DEFAULT 0,
        earned INTEGER DEFAULT 0
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scramble_group_stats (
        chat_id BIGINT PRIMARY KEY,
        title TEXT,
        rounds_played INTEGER DEFAULT 0,
        last_active TEXT
    )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_scramble_chat_stats_chat ON scramble_chat_stats (chat_id, earned DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_scramble_global_stats_earned ON scramble_global_stats (earned DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_scramble_group_stats_rounds ON scramble_group_stats (rounds_played DESC)")

    # Per-chat /slowmode setting - a repeating auto-start interval for
    # Word Scramble in one specific group. Separate from the global
    # /scramblemode (below), which covers every group at once.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scramble_slowmode (
        chat_id BIGINT PRIMARY KEY,
        interval_seconds INTEGER,
        enabled INTEGER DEFAULT 0
    )
    """)

    # Per-chat Automatic Mode state (Feature: 5 consecutive /wordscramble
    # starts). manual_streak counts consecutive rounds started via the
    # literal /wordscramble command; auto_enabled is whether Automatic
    # Mode is currently on; miss_streak counts consecutive
    # auto-started rounds nobody answered (3 turns it back off).
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scramble_auto_mode (
        chat_id BIGINT PRIMARY KEY,
        manual_streak INTEGER DEFAULT 0,
        auto_enabled INTEGER DEFAULT 0,
        miss_streak INTEGER DEFAULT 0
    )
    """)

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Combined-balance ranking system (Word Game + Word Scramble balances
# added together). Used only for display (get_user() / get_combined_
# balance()) - never written back into any balance column, so nothing
# is double-counted and no existing balance table is touched.
# ---------------------------------------------------------------------

RANKS = [
    (0, "🌱 Beginner", 1),
    (5000, "🦅 Elite", 2),
    (15000, "🔥 Veteran", 3),
    (35000, "💎 Expert", 4),
    (75000, "⚡ Ace", 5),
    (150000, "🚀 Pro", 6),
    (300000, "🌟 Master", 7),
    (600000, "👑 Legend", 8),
    (1200000, "🌌 Immortal", 9),
    (2500000, "🔱 Supreme", 10),
]


def compute_level_rank(balance):
    level, rank = RANKS[0][2], RANKS[0][1]
    for threshold, rank_name, lvl in RANKS:
        if balance >= threshold:
            level, rank = lvl, rank_name
        else:
            break
    return level, rank


def get_combined_balance(user_id):
    """Word Game global balance + Word Scramble global balance, added
    together for display only. Never written anywhere."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COALESCE(g.coins, 0), COALESCE(s.earned, 0)
        FROM (SELECT %s::bigint AS uid) x
        LEFT JOIN global_scores g ON g.user_id = x.uid
        LEFT JOIN scramble_global_stats s ON s.user_id = x.uid
    """, (user_id,))

    row = cursor.fetchone()
    conn.close()

    if not row:
        return 0

    return (row[0] or 0) + (row[1] or 0)


# ---------------------------------------------------------------------
# Existing profile / daily reward functions (unchanged behaviour)
# ---------------------------------------------------------------------

def add_user(user_id, name, username=None):
    conn = connect()
    cursor = conn.cursor()

    now = datetime.now().isoformat()

    cursor.execute(
        "INSERT INTO users (id, name, username, last_active) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (user_id, name, username, now)
    )

    cursor.execute(
        "UPDATE users SET name=%s, username=%s, last_active=%s WHERE id=%s",
        (name, username, now, user_id)
    )

    conn.commit()
    conn.close()


def get_user(user_id):
    """Returns (id, name, combined_balance, level, rank, last_daily_claim,
    auto_lead). combined_balance/level/rank are computed live from the
    Word Game global balance + the Word Scramble global balance added
    together - see compute_level_rank()/RANKS above. The users.level and
    users.rank columns are left in the schema untouched but are no
    longer what's shown to players."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT u.id, u.name, COALESCE(g.coins, 0), COALESCE(s.earned, 0),
               u.last_daily_claim, u.auto_lead
        FROM users u
        LEFT JOIN global_scores g ON g.user_id = u.id
        LEFT JOIN scramble_global_stats s ON s.user_id = u.id
        WHERE u.id=%s
    """, (user_id,))

    row = cursor.fetchone()
    conn.close()

    if row is None:
        return None

    uid, name, wg_coins, ws_earned, last_claim, auto_lead = row
    balance = (wg_coins or 0) + (ws_earned or 0)
    level, rank = compute_level_rank(balance)

    return (uid, name, balance, level, rank, last_claim, auto_lead)


def get_global_coins(user_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("SELECT coins FROM global_scores WHERE user_id=%s", (user_id,))
    row = cursor.fetchone()
    conn.close()

    return row[0] if row else 0


def award_global_coins(user_id, name, amount):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO global_scores (user_id, name, coins)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            coins = global_scores.coins + EXCLUDED.coins,
            name = EXCLUDED.name
    """, (user_id, name, amount))

    conn.commit()
    conn.close()

    if amount > 0:
        increment_stat("total_rewards_given", amount)


def claim_daily_reward(user_id, name, username=None):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT last_daily_claim FROM users WHERE id=%s",
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
        "UPDATE users SET last_daily_claim=%s WHERE id=%s",
        (now.isoformat(), user_id)
    )

    conn.commit()
    conn.close()

    amount = int(get_config("reward_daily", DAILY_REWARD))
    award_global_coins(user_id, name, amount)
    log_daily_claim(user_id, name, username, amount)

    # Daily Reward is only ever written into the Word Game's
    # global_scores table above (never into Word Scramble's tables too -
    # that would double-count it). The balance handed back to the
    # caller for display is the COMBINED balance (Word Game + Word
    # Scramble), so /claimdailyreward and the "🎁 Daily Reward" button
    # show the same total the /start profile shows.
    return True, get_combined_balance(user_id)


# ---------------------------------------------------------------------
# Persistent daily-reward claim log - powers /claimeddailyreward
# ---------------------------------------------------------------------

def log_daily_claim(user_id, name, username, amount):
    conn = connect()
    cursor = conn.cursor()

    now = datetime.now()

    cursor.execute("""
        INSERT INTO daily_reward_log (user_id, name, username, amount, claim_date, claimed_at)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (user_id, name, username, amount, now.strftime("%Y-%m-%d"), now.isoformat()))

    conn.commit()
    conn.close()


def get_daily_claims(date_str):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT user_id, name, username, amount, claimed_at
        FROM daily_reward_log
        WHERE claim_date=%s
        ORDER BY claimed_at ASC
    """, (date_str,))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_daily_totals(date_str):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*), COALESCE(SUM(amount), 0)
        FROM daily_reward_log
        WHERE claim_date=%s
    """, (date_str,))

    row = cursor.fetchone()
    conn.close()

    return row if row else (0, 0)


def get_leaderboard():
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT u.name, COALESCE(g.coins, 0) AS coins
        FROM users u
        LEFT JOIN global_scores g ON g.user_id = u.id
        ORDER BY coins DESC, u.level DESC
        LIMIT 10
    """)

    leaderboard = cursor.fetchall()
    conn.close()

    return leaderboard


def increment_user_game_stats(user_id, played=0, won=0):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE users
        SET games_played = COALESCE(games_played, 0) + %s,
            games_won = COALESCE(games_won, 0) + %s
        WHERE id=%s
    """, (played, won, user_id))

    conn.commit()
    conn.close()


def get_full_player_profile(user_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT u.id, u.name, u.username, COALESCE(g.coins, 0),
               COALESCE(u.games_played, 0), COALESCE(u.games_won, 0)
        FROM users u
        LEFT JOIN global_scores g ON g.user_id = u.id
        WHERE u.id=%s
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
    conn.close()

    position = None
    coins = 0

    for index, player in enumerate(players, start=1):
        if player[0] == user_id:
            position = index
            coins = player[2]
            break

    return position, coins


# ---------------------------------------------------------------------
# Auto-lead setting (per user, set from the bot's private chat)
# ---------------------------------------------------------------------

def get_auto_lead(user_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("SELECT auto_lead FROM users WHERE id=%s", (user_id,))
    row = cursor.fetchone()
    conn.close()

    if row is None:
        return True

    return bool(row[0])


def set_auto_lead(user_id, enabled: bool):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE users SET auto_lead=%s WHERE id=%s",
        (1 if enabled else 0, user_id)
    )

    conn.commit()
    conn.close()


def toggle_auto_lead(user_id):
    current = get_auto_lead(user_id)
    set_auto_lead(user_id, not current)
    return not current


# ---------------------------------------------------------------------
# Last active chat
# ---------------------------------------------------------------------

def set_last_chat(user_id, chat_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET last_chat_id=%s WHERE id=%s", (chat_id, user_id))
    conn.commit()
    conn.close()


def get_last_chat(user_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT last_chat_id FROM users WHERE id=%s", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def credit_chat_score(user_id, chat_id, name, amount):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO chat_scores (user_id, chat_id, name, coins)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id, chat_id) DO UPDATE SET
            coins = chat_scores.coins + EXCLUDED.coins,
            name = EXCLUDED.name
    """, (user_id, chat_id, name, amount))
    cursor.execute("UPDATE users SET last_chat_id=%s WHERE id=%s", (chat_id, user_id))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Word game state (per chat)
# ---------------------------------------------------------------------

def start_game(chat_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO word_games (chat_id, leader_id, leader_name, secret_word, game_active, explaining_started, leader_message_id)
        VALUES (%s, NULL, NULL, NULL, 1, 0, NULL)
        ON CONFLICT (chat_id) DO UPDATE SET
            leader_id=NULL, leader_name=NULL, secret_word=NULL, game_active=1, explaining_started=0, leader_message_id=NULL
    """, (chat_id,))

    conn.commit()
    conn.close()


def set_leader(chat_id, leader_id, leader_name, word):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO word_games (chat_id, leader_id, leader_name, secret_word, game_active, explaining_started, leader_message_id)
        VALUES (%s, %s, %s, %s, 1, 0, NULL)
        ON CONFLICT (chat_id) DO UPDATE SET
            leader_id=EXCLUDED.leader_id,
            leader_name=EXCLUDED.leader_name,
            secret_word=EXCLUDED.secret_word,
            game_active=1,
            explaining_started=0,
            leader_message_id=NULL
    """, (chat_id, leader_id, leader_name, word))

    conn.commit()
    conn.close()


def get_game(chat_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT chat_id, leader_id, leader_name, secret_word, game_active
        FROM word_games WHERE chat_id=%s
    """, (chat_id,))

    row = cursor.fetchone()
    conn.close()

    return row


def set_word(chat_id, word):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE word_games SET secret_word=%s WHERE chat_id=%s",
        (word, chat_id)
    )

    conn.commit()
    conn.close()


def clear_leader(chat_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE word_games
        SET leader_id=NULL, leader_name=NULL, secret_word=NULL, explaining_started=0, leader_message_id=NULL
        WHERE chat_id=%s
    """, (chat_id,))

    conn.commit()
    conn.close()


def end_game(chat_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE word_games
        SET leader_id=NULL, leader_name=NULL, secret_word=NULL, game_active=0, explaining_started=0, leader_message_id=NULL
        WHERE chat_id=%s
    """, (chat_id,))

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Leader's initial round-announcement message (for clickable /hint links)
# ---------------------------------------------------------------------

def set_leader_message_id(chat_id, message_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("UPDATE word_games SET leader_message_id=%s WHERE chat_id=%s", (message_id, chat_id))
    conn.commit()
    conn.close()


def get_leader_message_id(chat_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT leader_message_id FROM word_games WHERE chat_id=%s", (chat_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row and row[0] else None


# ---------------------------------------------------------------------
# Pending input (used for the "Write Word" DM flow)
# ---------------------------------------------------------------------

def set_pending_input(user_id, chat_id, action):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO pending_input (user_id, chat_id, action)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET chat_id=EXCLUDED.chat_id, action=EXCLUDED.action
    """, (user_id, chat_id, action))

    conn.commit()
    conn.close()


def get_pending_input(user_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT chat_id, action FROM pending_input WHERE user_id=%s",
        (user_id,)
    )

    row = cursor.fetchone()
    conn.close()

    return row


def clear_pending_input(user_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM pending_input WHERE user_id=%s", (user_id,))

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Coins: dual-write to per-chat and global tables
# ---------------------------------------------------------------------

def award_coins(user_id, chat_id, name, amount):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO chat_scores (user_id, chat_id, name, coins)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id, chat_id) DO UPDATE SET
            coins = chat_scores.coins + EXCLUDED.coins,
            name = EXCLUDED.name
    """, (user_id, chat_id, name, amount))

    cursor.execute("""
        INSERT INTO global_scores (user_id, name, coins)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            coins = global_scores.coins + EXCLUDED.coins,
            name = EXCLUDED.name
    """, (user_id, name, amount))

    cursor.execute("UPDATE users SET last_chat_id=%s WHERE id=%s", (chat_id, user_id))

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
        WHERE chat_id=%s
        ORDER BY coins DESC
        LIMIT %s
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
        LIMIT %s
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_chat_leaderboard_with_ids(chat_id, limit=10):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT user_id, name, coins
        FROM chat_scores
        WHERE chat_id=%s
        ORDER BY coins DESC
        LIMIT %s
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
        LIMIT %s
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_chat_position(chat_id, user_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT user_id, name, coins
        FROM chat_scores
        WHERE chat_id=%s
        ORDER BY coins DESC, user_id ASC
    """, (chat_id,))

    rows = cursor.fetchall()
    conn.close()

    for index, row in enumerate(rows, start=1):
        if row[0] == user_id:
            return index, row[1], row[2]

    return None


def get_global_position(user_id):
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

    cursor.execute("SELECT word FROM used_words WHERE chat_id=%s", (chat_id,))
    rows = cursor.fetchall()
    conn.close()

    return {row[0] for row in rows}


def mark_word_used(chat_id, word):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "INSERT INTO used_words (chat_id, word) VALUES (%s, %s) "
        "ON CONFLICT (chat_id, word) DO NOTHING",
        (chat_id, word)
    )

    conn.commit()
    conn.close()


def reset_used_words(chat_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM used_words WHERE chat_id=%s", (chat_id,))

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# "Explaining started?" flag - powers the ran-out-of-time auto-timeout
# ---------------------------------------------------------------------

def set_explaining_started(chat_id, started: bool):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE word_games SET explaining_started=%s WHERE chat_id=%s",
        (1 if started else 0, chat_id)
    )

    conn.commit()
    conn.close()


def is_explaining_started(chat_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT explaining_started FROM word_games WHERE chat_id=%s",
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
        VALUES (%s, %s, '')
        ON CONFLICT (chat_id) DO UPDATE SET leader_id=EXCLUDED.leader_id, log=''
    """, (chat_id, leader_id))

    conn.commit()
    conn.close()


def append_explain_log(chat_id, leader_id, text):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT leader_id, log FROM explain_log WHERE chat_id=%s", (chat_id,)
    )
    row = cursor.fetchone()

    if row is None or row[0] != leader_id:
        cursor.execute("""
            INSERT INTO explain_log (chat_id, leader_id, log)
            VALUES (%s, %s, %s)
            ON CONFLICT (chat_id) DO UPDATE SET leader_id=EXCLUDED.leader_id, log=EXCLUDED.log
        """, (chat_id, leader_id, text))
    else:
        new_log = (row[1] + "\n" + text) if row[1] else text
        cursor.execute(
            "UPDATE explain_log SET log=%s WHERE chat_id=%s",
            (new_log, chat_id)
        )

    conn.commit()
    conn.close()


def get_explain_log(chat_id, leader_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT leader_id, log FROM explain_log WHERE chat_id=%s", (chat_id,)
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
    """Registers/refreshes a group. If the bot is rejoining a group it
    had previously left, left_at is cleared - the group is "current"
    again. Never touches games_played/first_seen on rejoin."""
    conn = connect()
    cursor = conn.cursor()

    now = datetime.now().isoformat()

    cursor.execute("""
        INSERT INTO groups (chat_id, title, first_seen, last_active)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (chat_id) DO UPDATE SET
            title=EXCLUDED.title, last_active=EXCLUDED.last_active, left_at=NULL
    """, (chat_id, title, now, now))

    conn.commit()
    conn.close()


def mark_group_left(chat_id):
    """Records that the bot is no longer in this group (kicked, left,
    or removed via ban) - powers /botgroups' current-vs-removed split.
    The group row itself (and all its stats) is kept, not deleted."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("UPDATE groups SET left_at=%s WHERE chat_id=%s", (datetime.now().isoformat(), chat_id))
    conn.commit()
    conn.close()


def get_bot_groups_stats():
    """(total ever, currently in, removed from) - powers /botgroups'
    summary header."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM groups")
    total = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM groups WHERE left_at IS NULL")
    current = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM groups WHERE left_at IS NOT NULL")
    removed = cursor.fetchone()[0]
    conn.close()
    return {"total": total, "current": current, "removed": removed}


def get_botgroups_count():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM groups")
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_botgroups_page(offset, limit):
    """(chat_id, title, word_game_games_played, left_at, scramble_rounds_played)
    per row, currently-in groups first, most active first - powers the
    /botgroups paginated table."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT g.chat_id, g.title, g.games_played, g.left_at, COALESCE(s.rounds_played, 0)
        FROM groups g
        LEFT JOIN scramble_group_stats s ON s.chat_id = g.chat_id
        ORDER BY (g.left_at IS NULL) DESC, g.games_played DESC, g.last_active DESC
        LIMIT %s OFFSET %s
    """, (limit, offset))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_group_full_details(chat_id):
    """(chat_id, title, member_count, word_game_games_played, first_seen,
    left_at, scramble_rounds_played) for the /botgroups detail view."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT g.chat_id, g.title, g.member_count, g.games_played, g.first_seen, g.left_at,
               COALESCE(s.rounds_played, 0)
        FROM groups g
        LEFT JOIN scramble_group_stats s ON s.chat_id = g.chat_id
        WHERE g.chat_id=%s
    """, (chat_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def update_group_member_count(chat_id, count):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE groups SET member_count=%s WHERE chat_id=%s",
        (count, chat_id)
    )

    conn.commit()
    conn.close()


def increment_group_games(chat_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE groups SET games_played = games_played + 1, last_active=%s WHERE chat_id=%s",
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
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT u.id, u.name, u.username, COALESCE(g.coins, 0), u.last_active
        FROM users u
        LEFT JOIN global_scores g ON g.user_id = u.id
        ORDER BY COALESCE(g.coins, 0) DESC, u.last_active DESC
        LIMIT %s OFFSET %s
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
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT chat_id, title, member_count, games_played, last_active
        FROM groups
        ORDER BY games_played DESC, last_active DESC
        LIMIT %s OFFSET %s
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
            FROM groups WHERE chat_id=%s
        """, (int(query),))
    else:
        cursor.execute("""
            SELECT chat_id, title, member_count, games_played
            FROM groups WHERE title ILIKE %s
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
        LIMIT %s
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_top_groups_full(limit=10):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT chat_id, title, games_played
        FROM groups
        ORDER BY games_played DESC
        LIMIT %s
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_group_details(chat_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT chat_id, title, member_count, games_played, first_seen
        FROM groups
        WHERE chat_id=%s
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
            WHERE u.id=%s
        """, (int(query),))
    else:
        cursor.execute("""
            SELECT u.id, u.name, u.username, COALESCE(g.coins, 0)
            FROM users u
            LEFT JOIN global_scores g ON g.user_id = u.id
            WHERE u.name ILIKE %s OR u.username ILIKE %s
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
        WHERE LOWER(u.username) = LOWER(%s)
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
        WHERE u.id = %s
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
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET banned_at=EXCLUDED.banned_at, banned_by=EXCLUDED.banned_by
    """, (user_id, datetime.now().isoformat(), banned_by))

    conn.commit()
    conn.close()


def unban_user(user_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM banned_users WHERE user_id=%s", (user_id,))
    conn.commit()
    conn.close()


def is_user_banned(user_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM banned_users WHERE user_id=%s", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def ban_group(chat_id, banned_by):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO banned_groups (chat_id, banned_at, banned_by)
        VALUES (%s, %s, %s)
        ON CONFLICT (chat_id) DO UPDATE SET banned_at=EXCLUDED.banned_at, banned_by=EXCLUDED.banned_by
    """, (chat_id, datetime.now().isoformat(), banned_by))

    conn.commit()
    conn.close()


def unban_group(chat_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM banned_groups WHERE chat_id=%s", (chat_id,))
    conn.commit()
    conn.close()


def is_group_banned(chat_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM banned_groups WHERE chat_id=%s", (chat_id,))
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
        VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = bot_stats.value + EXCLUDED.value
    """, (key, amount))

    conn.commit()
    conn.close()


def get_stat(key):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM bot_stats WHERE key=%s", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0


# ---------------------------------------------------------------------
# Owner admin panel: tunable config (rewards, maintenance mode, ads)
# ---------------------------------------------------------------------

def set_config(key, value):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO bot_config (key, value)
        VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
    """, (key, str(value)))

    conn.commit()
    conn.close()


def get_config(key, default=None):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM bot_config WHERE key=%s", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default


# ---------------------------------------------------------------------
# /stopvote ballots
# ---------------------------------------------------------------------

def add_stop_vote(chat_id, user_id, name):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute(
        "INSERT INTO stop_votes (chat_id, user_id, name) VALUES (%s, %s, %s) "
        "ON CONFLICT (chat_id, user_id) DO NOTHING",
        (chat_id, user_id, name)
    )
    added = cursor.rowcount > 0

    conn.commit()
    conn.close()

    return added


def get_stop_vote_count(chat_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM stop_votes WHERE chat_id=%s", (chat_id,))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def clear_stop_votes(chat_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM stop_votes WHERE chat_id=%s", (chat_id,))
    conn.commit()
    conn.close()


def get_stop_vote_names(chat_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM stop_votes WHERE chat_id=%s ORDER BY seq ASC",
        (chat_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------
# /topup audit log
# ---------------------------------------------------------------------

def log_topup(target_id, target_name, amount, owner_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO topup_log (target_id, target_name, amount, owner_id, timestamp)
        VALUES (%s, %s, %s, %s, %s)
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


# ---------------------------------------------------------------------
# Bot Admins
# ---------------------------------------------------------------------

def add_bot_admin(user_id, name, username, added_by):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO bot_admins (user_id, name, username, added_by, added_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            name=EXCLUDED.name,
            username=EXCLUDED.username,
            added_by=EXCLUDED.added_by,
            added_at=EXCLUDED.added_at
    """, (user_id, name, username, added_by, datetime.now().isoformat()))

    conn.commit()
    conn.close()


def remove_bot_admin(user_id):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM bot_admins WHERE user_id=%s", (user_id,))
    removed = cursor.rowcount > 0

    conn.commit()
    conn.close()

    return removed


def is_bot_admin(user_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM bot_admins WHERE user_id=%s", (user_id,))
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


# ---------------------------------------------------------------------
# Advertisement rotation
# ---------------------------------------------------------------------

def get_active_ads_for_rotation():
    conn = connect()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    cursor.execute("""
        SELECT id, business_name, ad_link, ad_text
        FROM ad_subscriptions
        WHERE status IN ('active', 'expiring_soon') AND end_at > %s
        ORDER BY id ASC
    """, (now,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def record_ad_impression(subscription_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("UPDATE ad_subscriptions SET total_views = total_views + 1 WHERE id=%s", (subscription_id,))
    conn.commit()
    conn.close()


def record_ad_click(subscription_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("UPDATE ad_subscriptions SET total_clicks = total_clicks + 1 WHERE id=%s", (subscription_id,))
    conn.commit()
    conn.close()


def get_ad_business_name(subscription_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT business_name, ad_link FROM ad_subscriptions WHERE id=%s", (subscription_id,))
    row = cursor.fetchone()
    conn.close()
    return row


# =======================================================================
# WORD SCRAMBLE
# =======================================================================

def scramble_start_round(chat_id, word, scrambled):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO scramble_games (chat_id, word, scrambled, started_at, round_active)
        VALUES (%s, %s, %s, %s, 1)
        ON CONFLICT (chat_id) DO UPDATE SET
            word=EXCLUDED.word, scrambled=EXCLUDED.scrambled,
            started_at=EXCLUDED.started_at, round_active=1
    """, (chat_id, word, scrambled, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def scramble_get_round(chat_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT chat_id, word, scrambled, started_at, round_active
        FROM scramble_games WHERE chat_id=%s AND round_active=1
    """, (chat_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def scramble_end_round(chat_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM scramble_games WHERE chat_id=%s", (chat_id,))
    conn.commit()
    conn.close()


def scramble_claim_round(chat_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM scramble_games WHERE chat_id=%s AND round_active=1 RETURNING word",
        (chat_id,)
    )
    row = cursor.fetchone()
    conn.commit()
    conn.close()
    return row[0] if row else None


def get_all_active_scramble_chats():
    """[chat_id, ...] for every chat with a Word Scramble round
    currently marked active - powers /scramblestopallgroups."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM scramble_games WHERE round_active=1")
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_all_active_scramble_rounds_with_time():
    """[(chat_id, started_at), ...] for every chat with a Word Scramble
    round currently marked active - used at startup to restore/expire
    rounds whose in-memory timeout timer was lost on restart, so a
    round can never stay stuck forever in the database."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, started_at FROM scramble_games WHERE round_active=1")
    rows = cursor.fetchall()
    conn.close()
    return rows


def scramble_get_used_words(chat_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT word FROM scramble_used_words WHERE chat_id=%s", (chat_id,))
    rows = cursor.fetchall()
    conn.close()
    return {row[0] for row in rows}


def scramble_mark_word_used(chat_id, word):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO scramble_used_words (chat_id, word) VALUES (%s, %s) "
        "ON CONFLICT (chat_id, word) DO NOTHING",
        (chat_id, word)
    )
    conn.commit()
    conn.close()


def scramble_reset_used_words(chat_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM scramble_used_words WHERE chat_id=%s", (chat_id,))
    conn.commit()
    conn.close()


def scramble_record_win(user_id, chat_id, name, reward_amount):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO scramble_chat_stats (user_id, chat_id, name, wins, earned, current_streak, best_streak)
        VALUES (%s, %s, %s, 1, %s, 1, 1)
        ON CONFLICT (user_id, chat_id) DO UPDATE SET
            name = EXCLUDED.name,
            wins = scramble_chat_stats.wins + 1,
            earned = scramble_chat_stats.earned + %s,
            current_streak = scramble_chat_stats.current_streak + 1,
            best_streak = GREATEST(scramble_chat_stats.best_streak, scramble_chat_stats.current_streak + 1)
        RETURNING current_streak
    """, (user_id, chat_id, name, reward_amount, reward_amount))
    new_streak = cursor.fetchone()[0]

    cursor.execute("""
        INSERT INTO scramble_global_stats (user_id, name, wins, earned)
        VALUES (%s, %s, 1, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            name = EXCLUDED.name,
            wins = scramble_global_stats.wins + 1,
            earned = scramble_global_stats.earned + %s
    """, (user_id, name, reward_amount, reward_amount))

    conn.commit()
    conn.close()
    return new_streak


def scramble_reset_all_streaks_in_chat(chat_id, except_user_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE scramble_chat_stats SET current_streak=0 WHERE chat_id=%s AND user_id != %s",
        (chat_id, except_user_id)
    )
    conn.commit()
    conn.close()


def scramble_add_bonus(user_id, chat_id, name, bonus_amount):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE scramble_chat_stats SET earned = earned + %s WHERE user_id=%s AND chat_id=%s",
        (bonus_amount, user_id, chat_id)
    )
    cursor.execute(
        "UPDATE scramble_global_stats SET earned = earned + %s WHERE user_id=%s",
        (bonus_amount, user_id)
    )
    conn.commit()
    conn.close()


def scramble_get_chat_leaderboard(chat_id, limit=10):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT name, wins, earned, current_streak
        FROM scramble_chat_stats WHERE chat_id=%s
        ORDER BY earned DESC LIMIT %s
    """, (chat_id, limit))
    rows = cursor.fetchall()
    conn.close()
    return rows


def scramble_get_global_leaderboard(limit=10):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT name, wins, earned
        FROM scramble_global_stats ORDER BY earned DESC LIMIT %s
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def scramble_get_chat_position(chat_id, user_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, name, wins, earned, current_streak
        FROM scramble_chat_stats WHERE chat_id=%s ORDER BY earned DESC, user_id ASC
    """, (chat_id,))
    rows = cursor.fetchall()
    conn.close()
    for index, row in enumerate(rows, start=1):
        if row[0] == user_id:
            return index, row[1], row[2], row[3], row[4]
    return None


def scramble_get_global_position(user_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, name, wins, earned
        FROM scramble_global_stats ORDER BY earned DESC, user_id ASC
    """)
    rows = cursor.fetchall()
    conn.close()
    for index, row in enumerate(rows, start=1):
        if row[0] == user_id:
            return index, row[1], row[2], row[3]
    return None


def scramble_register_group(chat_id, title):
    conn = connect()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    cursor.execute("""
        INSERT INTO scramble_group_stats (chat_id, title, rounds_played, last_active)
        VALUES (%s, %s, 1, %s)
        ON CONFLICT (chat_id) DO UPDATE SET
            title=EXCLUDED.title,
            rounds_played = scramble_group_stats.rounds_played + 1,
            last_active=EXCLUDED.last_active
    """, (chat_id, title, now))
    conn.commit()
    conn.close()


def scramble_get_top_groups(limit=10):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT title, rounds_played FROM scramble_group_stats
        ORDER BY rounds_played DESC LIMIT %s
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def scramble_get_stats():
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*), COALESCE(SUM(wins), 0), COALESCE(SUM(earned), 0) FROM scramble_global_stats")
    total_players, total_wins, total_rewards = cursor.fetchone()

    cursor.execute("SELECT COALESCE(SUM(rounds_played), 0) FROM scramble_group_stats")
    total_group_rounds = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM scramble_games")
    active_rounds = cursor.fetchone()[0]

    conn.close()
    return {
        "total_players": total_players,
        "total_wins": total_wins,
        "total_rewards": total_rewards,
        "total_group_rounds": total_group_rounds,
        "active_rounds": active_rounds,
    }


# ---------------------------------------------------------------------
# Word Scramble: /scrambletopup - adjusts ONLY the scramble_global_stats
# balance, never chat_scores/global_scores (the Word Game's wallet).
# ---------------------------------------------------------------------

def scramble_adjust_global_earned(user_id, name, amount):
    """Adds (or removes, if amount is negative) from a player's global
    Word Scramble 'earned' total only. Never lets the balance go below
    0. Returns the new balance."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO scramble_global_stats (user_id, name, wins, earned)
        VALUES (%s, %s, 0, GREATEST(0, %s))
        ON CONFLICT (user_id) DO UPDATE SET
            earned = GREATEST(0, scramble_global_stats.earned + %s),
            name = EXCLUDED.name
        RETURNING earned
    """, (user_id, name, amount, amount))
    new_balance = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return new_balance


# ---------------------------------------------------------------------
# Word Scramble: /slowmode (per-chat repeating auto-start)
# ---------------------------------------------------------------------

def set_scramble_slowmode(chat_id, interval_seconds, enabled: bool):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO scramble_slowmode (chat_id, interval_seconds, enabled)
        VALUES (%s, %s, %s)
        ON CONFLICT (chat_id) DO UPDATE SET
            interval_seconds=EXCLUDED.interval_seconds, enabled=EXCLUDED.enabled
    """, (chat_id, interval_seconds, 1 if enabled else 0))
    conn.commit()
    conn.close()


def get_scramble_slowmode(chat_id):
    """(interval_seconds, enabled) or None if never configured."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT interval_seconds, enabled FROM scramble_slowmode WHERE chat_id=%s", (chat_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return row[0], bool(row[1])


def get_all_active_slowmode_chats():
    """[(chat_id, interval_seconds), ...] for every chat with slowmode
    currently enabled - used to re-schedule jobs on bot restart."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, interval_seconds FROM scramble_slowmode WHERE enabled=1")
    rows = cursor.fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------
# Word Scramble: /scramblemode (global, every currently-in group)
# ---------------------------------------------------------------------

def get_current_groups():
    """[(chat_id, title), ...] for every group the bot is currently in
    (left_at IS NULL) - used by /scramblemode's global auto-start tick."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, title FROM groups WHERE left_at IS NULL")
    rows = cursor.fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------
# Word Scramble: Automatic Mode (5 consecutive /wordscramble starts)
# ---------------------------------------------------------------------

def scramble_get_auto_mode(chat_id):
    """(manual_streak, auto_enabled, miss_streak) - defaults to
    (0, False, 0) for a chat with no row yet."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT manual_streak, auto_enabled, miss_streak FROM scramble_auto_mode WHERE chat_id=%s",
        (chat_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return (0, False, 0)
    return (row[0], bool(row[1]), row[2])


def scramble_increment_manual_streak(chat_id):
    """Bumps the consecutive-/wordscramble-starts counter and returns
    the new value."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO scramble_auto_mode (chat_id, manual_streak, auto_enabled, miss_streak)
        VALUES (%s, 1, 0, 0)
        ON CONFLICT (chat_id) DO UPDATE SET manual_streak = scramble_auto_mode.manual_streak + 1
        RETURNING manual_streak
    """, (chat_id,))
    val = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return val


def scramble_reset_manual_streak(chat_id):
    """Called whenever a round starts through anything OTHER than the
    literal /wordscramble command, so an interruption breaks the
    'consecutive' count."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO scramble_auto_mode (chat_id, manual_streak, auto_enabled, miss_streak)
        VALUES (%s, 0, 0, 0)
        ON CONFLICT (chat_id) DO UPDATE SET manual_streak = 0
    """, (chat_id,))
    conn.commit()
    conn.close()


def scramble_set_auto_enabled(chat_id, enabled: bool):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO scramble_auto_mode (chat_id, manual_streak, auto_enabled, miss_streak)
        VALUES (%s, 0, %s, 0)
        ON CONFLICT (chat_id) DO UPDATE SET auto_enabled=%s, manual_streak=0, miss_streak=0
    """, (chat_id, 1 if enabled else 0, 1 if enabled else 0))
    conn.commit()
    conn.close()


def scramble_record_round_result(chat_id, won: bool):
    """Called when an Automatic-Mode round ends. Resets miss_streak to
    0 on a win, or increments it on a miss (timeout with no winner).
    Returns (auto_enabled, miss_streak) after the update, or None if
    this chat has no auto_mode row at all (shouldn't normally happen -
    caller should have already checked scramble_get_auto_mode)."""
    conn = connect()
    cursor = conn.cursor()
    if won:
        cursor.execute(
            "UPDATE scramble_auto_mode SET miss_streak=0 WHERE chat_id=%s RETURNING auto_enabled, miss_streak",
            (chat_id,)
        )
    else:
        cursor.execute(
            "UPDATE scramble_auto_mode SET miss_streak=miss_streak+1 WHERE chat_id=%s RETURNING auto_enabled, miss_streak",
            (chat_id,)
        )
    row = cursor.fetchone()
    conn.commit()
    conn.close()
    if not row:
        return None
    return bool(row[0]), row[1]


# ---------------------------------------------------------------------
# Word Scramble: /scrambleusers - anyone who used the command or
# guessed a word, per (user, chat) they were active in.
# ---------------------------------------------------------------------

def scramble_increment_played(user_id, chat_id, name):
    """Bumps this user's 'played' counter (interaction count - command
    use or a guess attempt, right or wrong) for this specific chat."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO scramble_chat_stats (user_id, chat_id, name, wins, earned, current_streak, best_streak, played)
        VALUES (%s, %s, %s, 0, 0, 0, 0, 1)
        ON CONFLICT (user_id, chat_id) DO UPDATE SET
            played = scramble_chat_stats.played + 1,
            name = EXCLUDED.name
    """, (user_id, chat_id, name))
    conn.commit()
    conn.close()


def get_scramble_participants_count():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM scramble_chat_stats WHERE played > 0 OR wins > 0")
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_scramble_participants_page(offset, limit):
    """[(user_id, chat_id, name, played, wins, earned), ...] - one row
    per user-per-chat they've been active in, most active first."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, chat_id, name, played, wins, earned
        FROM scramble_chat_stats
        WHERE played > 0 OR wins > 0
        ORDER BY played DESC, earned DESC
        LIMIT %s OFFSET %s
    """, (limit, offset))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_scramble_participant_detail(user_id, chat_id):
    """(name, played, wins, earned) for one user in one chat, or None."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name, played, wins, earned FROM scramble_chat_stats WHERE user_id=%s AND chat_id=%s",
        (user_id, chat_id)
    )
    row = cursor.fetchone()
    conn.close()
    return row


def scramble_get_global_earned(user_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT earned FROM scramble_global_stats WHERE user_id=%s", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0