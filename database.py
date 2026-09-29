"""
Shared PostgreSQL data-access layer for @Uff_GameBot (and @UFFAdsBot -
see the "shared tables" section at the bottom of create_tables()).
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
    connect_timeout=10,
    # NOTE: statement_timeout is intentionally NOT set here via
    # options="-c statement_timeout=...". Neon's pooled (pgbouncer)
    # endpoint rejects arbitrary startup parameters like that with
    # "unsupported startup parameter in options" and refuses the
    # connection outright. It's set per-session instead, right after
    # each connection is handed out - see connect() below.
    # Without these, a connection that goes stale mid-idle (e.g. Neon
    # free-tier auto-suspending the compute under it, or a network blip
    # on Render) has NO client-side timeout at all - psycopg2 sits
    # waiting on the dead socket until the OS's default TCP keepalive
    # finally gives up, which on most Linux hosts is ~2 hours. That is
    # the exact "freezes for hours then recovers on its own" symptom.
    # These settings make the OS detect a dead peer and kill the socket
    # within ~45 seconds instead, so a bad connection surfaces as a
    # quick, catchable error instead of an open-ended hang.
    keepalives=1,
    keepalives_idle=15,
    keepalives_interval=10,
    keepalives_count=3,
)


class PooledConnection:
    """Wraps a connection checked out of the pool so existing code that
    calls conn.close() releases it back to the pool instead of closing
    the underlying socket. rollback() is called automatically on close
    if nothing was committed, so a connection left mid-transaction by an
    exception never gets handed back to the pool in a broken state."""

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
            try:
                self._conn.rollback()
            except Exception:
                pass
            self._pool.putconn(self._conn)
            self._closed = True


def connect():
    raw = _pool.getconn()
    try:
        # Set per-session instead of via options="-c statement_timeout=..."
        # at connection time - Neon's pooled endpoint rejects that as an
        # unsupported startup parameter. This SET is a cheap, harmless
        # no-op round trip if it's ever redundant, and it's what actually
        # caps a runaway query at 15s instead of letting it hang.
        raw.cursor().execute("SET statement_timeout = 15000")
    except Exception:
        # If even this fails, don't block the caller on it - the
        # keepalive settings on the pool still bound how long a truly
        # dead connection can hang.
        pass
    return PooledConnection(raw, _pool)


def create_tables():
    conn = connect()
    try:
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

        # One message id per line of `log`, newline-joined in the same order, so
        # each numbered hint can link to the exact message it came from.
        cursor.execute("ALTER TABLE explain_log ADD COLUMN IF NOT EXISTS message_ids TEXT DEFAULT ''")

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

        cursor.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS left_at TEXT")
        cursor.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS username TEXT")
        cursor.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS invite_link TEXT")
        cursor.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS added_by_id BIGINT")
        cursor.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS added_by_name TEXT")
        cursor.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS removed_by_id BIGINT")
        cursor.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS removed_by_name TEXT")
        cursor.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS admin_mode_override TEXT")
        cursor.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS autodelete_round_mode TEXT")
        cursor.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS autodelete_interval_seconds INTEGER DEFAULT 0")
        cursor.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS game_difficulty_override TEXT")

        # Feature: Invite & Earn. Separate from first_seen (set once, ever) -
        # this reflects when the bot's CURRENT continuous stint in the group
        # began, and is refreshed by register_group() on every add/re-add.
        cursor.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS current_stint_started_at TEXT")

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

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS scramble_slowmode (
            chat_id BIGINT PRIMARY KEY,
            interval_seconds INTEGER,
            enabled INTEGER DEFAULT 0
        )
        """)

        cursor.execute("ALTER TABLE scramble_slowmode ADD COLUMN IF NOT EXISTS next_round_due_at TEXT")

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS scramble_auto_mode (
            chat_id BIGINT PRIMARY KEY,
            manual_streak INTEGER DEFAULT 0,
            auto_enabled INTEGER DEFAULT 0,
            miss_streak INTEGER DEFAULT 0
        )
        """)

        # Words added at runtime via /addwords. Backed by Postgres (not the
        # medium_words_raw.py file) because Render wipes any local file
        # changes on every redeploy/restart - this table is what actually
        # survives.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS custom_words (
            word TEXT PRIMARY KEY,
            added_at TEXT
        )
        """)

        # Words hidden via /removewords, regardless of which source (a
        # built-in medium_words_raw.py category, or custom_words above) they
        # originally came from. Subtracted from the merged pool as the final
        # step wherever it's built.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS removed_words (
            word TEXT PRIMARY KEY,
            removed_at TEXT
        )
        """)

        # Feature: Invite & Earn. chat_id is deliberately NOT unique/primary -
        # the same chat_id can accumulate multiple rows over time (e.g. one
        # 'failed' attempt followed by a later 'pending' or 'rewarded' one).
        # The only permanent gate is a chat_id that already has a 'rewarded'
        # row - see create_pending_invite_reward()/create_rewarded_invite_direct()
        # below, which both enforce that gate atomically in a single INSERT.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS invite_rewards (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            group_title TEXT,
            inviter_id BIGINT NOT NULL,
            inviter_name TEXT,
            member_count_at_add INTEGER,
            added_at TEXT,
            status TEXT DEFAULT 'pending',
            amount_paid INTEGER,
            rewarded_at TEXT
        )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_invite_rewards_chat ON invite_rewards (chat_id, status)")

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tournament_bans (
            id BIGSERIAL PRIMARY KEY,
            target_type TEXT,
            target_id BIGINT NOT NULL,
            banned_by BIGINT,
            banned_at TEXT
        )
        """)
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tournament_bans_target "
            "ON tournament_bans (target_type, target_id)"
        )

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tournaments (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            host_id BIGINT NOT NULL,
            fee INTEGER NOT NULL,
            difficulty TEXT,
            total_rounds INTEGER NOT NULL,
            current_round INTEGER DEFAULT 0,
            status TEXT DEFAULT 'lobby',
            current_word TEXT,
            current_scrambled TEXT,
            round_started_at TEXT,
            created_at TEXT
        )
        """)
        cursor.execute("ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS lobby_message_id BIGINT")
        cursor.execute("ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS lobby_extra_seconds INTEGER DEFAULT 0")
        # Pause support: when a tournament was paused, and when the last round
        # ended (so a restart can tell "idle between rounds" from "just now").
        cursor.execute("ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS paused_at TEXT")
        cursor.execute("ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS idle_since TEXT")
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tournament_players (
            id BIGSERIAL PRIMARY KEY,
            tournament_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            name TEXT,
            score INTEGER DEFAULT 0,
            status TEXT DEFAULT 'staked'
        )
        """)
        # Hard guarantees behind the app-level checks: one open tournament per
        # chat, and one row per player per tournament.
        # 'paused' counts as open too. The index is only dropped when the
        # live one doesn't already mention 'paused' (so this is a no-op on
        # every boot after the first), and drop + create happen in the same
        # transaction, so there is never a moment without the guarantee.
        cursor.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'idx_tournaments_one_open_per_chat'"
        )
        _idx_row = cursor.fetchone()
        if _idx_row and "paused" not in (_idx_row[0] or ""):
            cursor.execute("DROP INDEX IF EXISTS idx_tournaments_one_open_per_chat")
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tournaments_one_open_per_chat "
            "ON tournaments (chat_id) WHERE status IN ('lobby', 'active', 'paused')"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tournament_players_unique "
            "ON tournament_players (tournament_id, user_id)"
        )

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS premium_users (
            user_id BIGINT PRIMARY KEY,
            expires_at TEXT,
            last_renewal_prompt_at TEXT
        )
        """)
        # One row per Stars charge, so a redelivered successful_payment update
        # can never grant premium twice.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS premium_payments (
            charge_id TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            stars INTEGER,
            duration_seconds INTEGER,
            paid_at TEXT
        )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_invite_rewards_inviter ON invite_rewards (inviter_id, status)")

        # One row per Stars charge for /buydollar, so a redelivered
        # successful_payment can never credit coins twice.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS dollar_purchases (
            charge_id TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            coins BIGINT,
            stars INTEGER,
            paid_at TEXT
        )
        """)

        # ---- Stage 3: transfers / requests --------------------------------
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS balance_transfers (
            id BIGSERIAL PRIMARY KEY,
            kind TEXT,
            from_id BIGINT,
            to_id BIGINT,
            amount BIGINT,
            chat_id BIGINT,
            created_at TEXT
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS money_requests (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            message_id BIGINT,
            requester_id BIGINT NOT NULL,
            requester_name TEXT,
            target_id BIGINT NOT NULL,
            target_name TEXT,
            amount BIGINT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            resolved_at TEXT
        )
        """)
        # Hard guarantee behind the app-level check: one pending request per
        # requester -> target pair.
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_money_requests_pending_pair "
            "ON money_requests (requester_id, target_id) WHERE status = 'pending'"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_money_requests_requester "
            "ON money_requests (requester_id, status)"
        )
        # Per-group /deleterequestprompt setting (same shape as scramble_slowmode:
        # one row per chat, upserted). No row = default (ON, 30s). 0 = off.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS request_prompt_settings (
            chat_id BIGINT PRIMARY KEY,
            delete_seconds INTEGER
        )
        """)
        # Individually scheduled message deletions that must survive a restart.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_deletions (
            chat_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL,
            delete_at TEXT,
            PRIMARY KEY (chat_id, message_id)
        )
        """)

        conn.commit()
    finally:
        conn.close()


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
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(g.coins, 0), COALESCE(s.earned, 0)
            FROM (SELECT %s::bigint AS uid) x
            LEFT JOIN global_scores g ON g.user_id = x.uid
            LEFT JOIN scramble_global_stats s ON s.user_id = x.uid
        """, (user_id,))
        row = cursor.fetchone()
        if not row:
            return 0
        return (row[0] or 0) + (row[1] or 0)
    finally:
        conn.close()


def add_user(user_id, name, username=None):
    conn = connect()
    try:
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
    finally:
        conn.close()


def get_user(user_id):
    conn = connect()
    try:
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
        if row is None:
            return None
        uid, name, wg_coins, ws_earned, last_claim, auto_lead = row
        balance = (wg_coins or 0) + (ws_earned or 0)
        level, rank = compute_level_rank(balance)
        return (uid, name, balance, level, rank, last_claim, auto_lead)
    finally:
        conn.close()


def get_global_coins(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT coins FROM global_scores WHERE user_id=%s", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def award_global_coins(user_id, name, amount):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO global_scores (user_id, name, coins)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                coins = global_scores.coins + EXCLUDED.coins,
                name = EXCLUDED.name
        """, (user_id, name, amount))
        conn.commit()
    finally:
        conn.close()
    if amount > 0:
        increment_stat("total_rewards_given", amount)


def claim_daily_reward(user_id, name, username=None):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT last_daily_claim FROM users WHERE id=%s", (user_id,))
        row = cursor.fetchone()
        last_claim = row[0] if row else None
        now = datetime.now()
        if last_claim:
            last_claim_dt = datetime.fromisoformat(last_claim)
            if now - last_claim_dt < timedelta(hours=24):
                remaining = timedelta(hours=24) - (now - last_claim_dt)
                return False, remaining
        cursor.execute(
            "UPDATE users SET last_daily_claim=%s WHERE id=%s",
            (now.isoformat(), user_id)
        )
        conn.commit()
    finally:
        conn.close()

    amount = int(get_config("reward_daily", DAILY_REWARD))
    award_global_coins(user_id, name, amount)
    log_daily_claim(user_id, name, username, amount)
    return True, get_combined_balance(user_id)


def log_daily_claim(user_id, name, username, amount):
    conn = connect()
    try:
        cursor = conn.cursor()
        now = datetime.now()
        cursor.execute("""
            INSERT INTO daily_reward_log (user_id, name, username, amount, claim_date, claimed_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (user_id, name, username, amount, now.strftime("%Y-%m-%d"), now.isoformat()))
        conn.commit()
    finally:
        conn.close()


def get_daily_claims(date_str):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, name, username, amount, claimed_at
            FROM daily_reward_log
            WHERE claim_date=%s
            ORDER BY claimed_at ASC
        """, (date_str,))
        return cursor.fetchall()
    finally:
        conn.close()


def get_daily_totals(date_str):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*), COALESCE(SUM(amount), 0)
            FROM daily_reward_log
            WHERE claim_date=%s
        """, (date_str,))
        row = cursor.fetchone()
        return row if row else (0, 0)
    finally:
        conn.close()


def get_leaderboard():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.name, COALESCE(g.coins, 0) AS coins
            FROM users u
            LEFT JOIN global_scores g ON g.user_id = u.id
            ORDER BY coins DESC, u.level DESC
            LIMIT 10
        """)
        return cursor.fetchall()
    finally:
        conn.close()


def increment_user_game_stats(user_id, played=0, won=0):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE users
            SET games_played = COALESCE(games_played, 0) + %s,
                games_won = COALESCE(games_won, 0) + %s
            WHERE id=%s
        """, (played, won, user_id))
        conn.commit()
    finally:
        conn.close()


def get_full_player_profile(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.id, u.name, u.username, COALESCE(g.coins, 0),
                   COALESCE(u.games_played, 0), COALESCE(u.games_won, 0)
            FROM users u
            LEFT JOIN global_scores g ON g.user_id = u.id
            WHERE u.id=%s
        """, (user_id,))
        return cursor.fetchone()
    finally:
        conn.close()


def get_player_position(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, coins
            FROM users
            ORDER BY coins DESC, id ASC
        """)
        players = cursor.fetchall()
    finally:
        conn.close()

    position = None
    coins = 0
    for index, player in enumerate(players, start=1):
        if player[0] == user_id:
            position = index
            coins = player[2]
            break
    return position, coins


def get_auto_lead(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT auto_lead FROM users WHERE id=%s", (user_id,))
        row = cursor.fetchone()
        if row is None:
            return True
        return bool(row[0])
    finally:
        conn.close()


def set_auto_lead(user_id, enabled: bool):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET auto_lead=%s WHERE id=%s",
            (1 if enabled else 0, user_id)
        )
        conn.commit()
    finally:
        conn.close()


def toggle_auto_lead(user_id):
    current = get_auto_lead(user_id)
    set_auto_lead(user_id, not current)
    return not current


def set_last_chat(user_id, chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET last_chat_id=%s WHERE id=%s", (chat_id, user_id))
        conn.commit()
    finally:
        conn.close()


def get_last_chat(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT last_chat_id FROM users WHERE id=%s", (user_id,))
        row = cursor.fetchone()
        return row[0] if row and row[0] else None
    finally:
        conn.close()


def credit_chat_score(user_id, chat_id, name, amount):
    conn = connect()
    try:
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
    finally:
        conn.close()


def start_game(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO word_games (chat_id, leader_id, leader_name, secret_word, game_active, explaining_started, leader_message_id)
            VALUES (%s, NULL, NULL, NULL, 1, 0, NULL)
            ON CONFLICT (chat_id) DO UPDATE SET
                leader_id=NULL, leader_name=NULL, secret_word=NULL, game_active=1, explaining_started=0, leader_message_id=NULL
        """, (chat_id,))
        conn.commit()
    finally:
        conn.close()


def set_leader(chat_id, leader_id, leader_name, word):
    conn = connect()
    try:
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
    finally:
        conn.close()


def get_game(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT chat_id, leader_id, leader_name, secret_word, game_active
            FROM word_games WHERE chat_id=%s
        """, (chat_id,))
        return cursor.fetchone()
    finally:
        conn.close()


def set_word(chat_id, word):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE word_games SET secret_word=%s WHERE chat_id=%s", (word, chat_id))
        conn.commit()
    finally:
        conn.close()


def clear_leader(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE word_games
            SET leader_id=NULL, leader_name=NULL, secret_word=NULL, explaining_started=0, leader_message_id=NULL
            WHERE chat_id=%s
        """, (chat_id,))
        conn.commit()
    finally:
        conn.close()


def claim_word_game_win(chat_id):
    """Atomically claims the currently active Word Game round for chat_id -
    the Word Game equivalent of scramble_claim_round()'s DELETE...RETURNING
    pattern (see that function below). Modeled directly on it: only the
    FIRST concurrent caller gets the round's pre-claim data back; any other
    caller arriving after it's already been claimed gets None and knows it
    lost the race.

    Word Game can't just DELETE the row like Word Scramble does (word_games
    is a persistent per-chat record other logic reads, not a disposable
    round row), so instead this clears the round's fields the same way
    clear_leader() does, but atomically and only when a round is actually
    active - and it returns the PRE-clear values (via the `old` subquery
    below), including explaining_started, so the caller can tell win from
    cheat-guess using the value that was true at the moment of the claim,
    not a stale earlier read.

    Returns (leader_id, leader_name, secret_word, explaining_started) or
    None if there was no active round to claim.
    """
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE word_games AS w
            SET leader_id=NULL, leader_name=NULL, secret_word=NULL, explaining_started=0, leader_message_id=NULL
            FROM (
                SELECT chat_id, leader_id, leader_name, secret_word, explaining_started
                FROM word_games
                WHERE chat_id=%s AND leader_id IS NOT NULL
                FOR UPDATE
            ) AS old
            WHERE w.chat_id = old.chat_id
            RETURNING old.leader_id, old.leader_name, old.secret_word, old.explaining_started
        """, (chat_id,))
        row = cursor.fetchone()
        conn.commit()
        return row
    finally:
        conn.close()


def end_game(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE word_games
            SET leader_id=NULL, leader_name=NULL, secret_word=NULL, game_active=0, explaining_started=0, leader_message_id=NULL
            WHERE chat_id=%s
        """, (chat_id,))
        conn.commit()
    finally:
        conn.close()


def set_leader_message_id(chat_id, message_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE word_games SET leader_message_id=%s WHERE chat_id=%s", (message_id, chat_id))
        conn.commit()
    finally:
        conn.close()


def get_leader_message_id(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT leader_message_id FROM word_games WHERE chat_id=%s", (chat_id,))
        row = cursor.fetchone()
        return row[0] if row and row[0] else None
    finally:
        conn.close()


def set_pending_input(user_id, chat_id, action):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO pending_input (user_id, chat_id, action)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET chat_id=EXCLUDED.chat_id, action=EXCLUDED.action
        """, (user_id, chat_id, action))
        conn.commit()
    finally:
        conn.close()


def get_pending_input(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, action FROM pending_input WHERE user_id=%s", (user_id,))
        return cursor.fetchone()
    finally:
        conn.close()


def clear_pending_input(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pending_input WHERE user_id=%s", (user_id,))
        conn.commit()
    finally:
        conn.close()


def award_coins(user_id, chat_id, name, amount):
    conn = connect()
    try:
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
    finally:
        conn.close()
    if amount > 0:
        increment_stat("total_rewards_given", amount)


def get_chat_leaderboard(chat_id, limit=10):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT name, coins FROM chat_scores WHERE chat_id=%s ORDER BY coins DESC LIMIT %s
        """, (chat_id, limit))
        return cursor.fetchall()
    finally:
        conn.close()


def get_global_leaderboard(limit=10):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name, coins FROM global_scores ORDER BY coins DESC LIMIT %s", (limit,))
        return cursor.fetchall()
    finally:
        conn.close()


def get_chat_leaderboard_with_ids(chat_id, limit=10):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, name, coins FROM chat_scores WHERE chat_id=%s ORDER BY coins DESC LIMIT %s
        """, (chat_id, limit))
        return cursor.fetchall()
    finally:
        conn.close()


def get_global_leaderboard_with_ids(limit=10):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, name, coins FROM global_scores ORDER BY coins DESC LIMIT %s", (limit,))
        return cursor.fetchall()
    finally:
        conn.close()


def get_chat_position(chat_id, user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, name, coins FROM chat_scores WHERE chat_id=%s ORDER BY coins DESC, user_id ASC
        """, (chat_id,))
        rows = cursor.fetchall()
    finally:
        conn.close()
    for index, row in enumerate(rows, start=1):
        if row[0] == user_id:
            return index, row[1], row[2]
    return None


def get_global_position(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, name, coins FROM global_scores ORDER BY coins DESC, user_id ASC")
        rows = cursor.fetchall()
    finally:
        conn.close()
    for index, row in enumerate(rows, start=1):
        if row[0] == user_id:
            return index, row[1], row[2]
    return None


def get_used_words(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT word FROM used_words WHERE chat_id=%s", (chat_id,))
        rows = cursor.fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def mark_word_used(chat_id, word):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO used_words (chat_id, word) VALUES (%s, %s) ON CONFLICT (chat_id, word) DO NOTHING",
            (chat_id, word)
        )
        conn.commit()
    finally:
        conn.close()


def reset_used_words(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM used_words WHERE chat_id=%s", (chat_id,))
        conn.commit()
    finally:
        conn.close()


def set_explaining_started(chat_id, started: bool):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE word_games SET explaining_started=%s WHERE chat_id=%s", (1 if started else 0, chat_id))
        conn.commit()
    finally:
        conn.close()


def is_explaining_started(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT explaining_started FROM word_games WHERE chat_id=%s", (chat_id,))
        row = cursor.fetchone()
        return bool(row[0]) if row else False
    finally:
        conn.close()


def start_explain_log(chat_id, leader_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO explain_log (chat_id, leader_id, log, message_ids)
            VALUES (%s, %s, '', '')
            ON CONFLICT (chat_id) DO UPDATE SET leader_id=EXCLUDED.leader_id, log='', message_ids=''
        """, (chat_id, leader_id))
        conn.commit()
    finally:
        conn.close()


def append_explain_log(chat_id, leader_id, text, message_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT leader_id, log, COALESCE(message_ids, '') FROM explain_log WHERE chat_id=%s",
            (chat_id,)
        )
        row = cursor.fetchone()

        # A hint message can itself contain line breaks, and the log is split on
        # "\n" when it's displayed. Record the message id once per line of `text`
        # so message_ids always has exactly as many lines as log does.
        new_ids = [str(message_id)] * len(text.split("\n"))

        if row is None or row[0] != leader_id:
            cursor.execute("""
                INSERT INTO explain_log (chat_id, leader_id, log, message_ids)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (chat_id) DO UPDATE SET
                    leader_id=EXCLUDED.leader_id, log=EXCLUDED.log, message_ids=EXCLUDED.message_ids
            """, (chat_id, leader_id, text, "\n".join(new_ids)))
        else:
            existing_log, existing_ids_raw = row[1], row[2]
            new_log = (existing_log + "\n" + text) if existing_log else text

            # A round that was already under way when message_ids was introduced
            # has log lines but no ids for them. Pad with blanks (rendered as
            # unlinked numbers) so the ids for NEW hints land on the right lines.
            existing_line_count = len(existing_log.split("\n")) if existing_log else 0
            existing_ids = existing_ids_raw.split("\n") if existing_ids_raw else []
            existing_ids = (existing_ids + [""] * existing_line_count)[:existing_line_count]

            cursor.execute(
                "UPDATE explain_log SET log=%s, message_ids=%s WHERE chat_id=%s",
                (new_log, "\n".join(existing_ids + new_ids), chat_id)
            )
        conn.commit()
    finally:
        conn.close()


def get_explain_log(chat_id, leader_id):
    """Returns (log, message_ids) - two newline-joined strings that line up
    line-for-line. Both are "" when there's no log for this leader."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT leader_id, log, COALESCE(message_ids, '') FROM explain_log WHERE chat_id=%s",
            (chat_id,)
        )
        row = cursor.fetchone()
        if row is None or row[0] != leader_id:
            return "", ""
        return row[1] or "", row[2]
    finally:
        conn.close()


def register_group(chat_id, title, username=None, invite_link=None, added_by_id=None, added_by_name=None):
    conn = connect()
    try:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        # current_stint_started_at is set to now() on EVERY call (both the
        # initial insert and the ON CONFLICT re-add path), so it always
        # reflects when the bot's CURRENT continuous membership began - unlike
        # first_seen, which is only ever set once, on the very first insert.
        cursor.execute("""
            INSERT INTO groups (chat_id, title, first_seen, last_active, username, invite_link, added_by_id, added_by_name, current_stint_started_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (chat_id) DO UPDATE SET
                title=EXCLUDED.title,
                last_active=EXCLUDED.last_active,
                left_at=NULL,
                username=COALESCE(EXCLUDED.username, groups.username),
                invite_link=COALESCE(EXCLUDED.invite_link, groups.invite_link),
                added_by_id=COALESCE(EXCLUDED.added_by_id, groups.added_by_id),
                added_by_name=COALESCE(EXCLUDED.added_by_name, groups.added_by_name),
                current_stint_started_at=EXCLUDED.current_stint_started_at
        """, (chat_id, title, now, now, username, invite_link, added_by_id, added_by_name, now))
        conn.commit()
    finally:
        conn.close()


def mark_group_left(chat_id, removed_by_id=None, removed_by_name=None):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE groups
            SET left_at=%s,
                removed_by_id=COALESCE(%s, removed_by_id),
                removed_by_name=COALESCE(%s, removed_by_name)
            WHERE chat_id=%s
        """, (datetime.now().isoformat(), removed_by_id, removed_by_name, chat_id))
        conn.commit()
    finally:
        conn.close()


def get_bot_groups_stats():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM groups")
        total = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM groups WHERE left_at IS NULL")
        current = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM groups WHERE left_at IS NOT NULL")
        removed = cursor.fetchone()[0]
        return {"total": total, "current": current, "removed": removed}
    finally:
        conn.close()


def get_botgroups_count():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM groups")
        return cursor.fetchone()[0]
    finally:
        conn.close()


def get_botgroups_page(offset, limit, sort="games"):
    conn = connect()
    try:
        cursor = conn.cursor()
        if sort == "time":
            order_clause = "g.first_seen ASC"
        else:
            order_clause = (
                "(g.left_at IS NULL) DESC, "
                "(g.games_played + COALESCE(s.rounds_played, 0)) DESC, g.last_active DESC"
            )
        cursor.execute(f"""
            SELECT g.chat_id, g.title, g.games_played, g.left_at, COALESCE(s.rounds_played, 0)
            FROM groups g
            LEFT JOIN scramble_group_stats s ON s.chat_id = g.chat_id
            ORDER BY {order_clause}
            LIMIT %s OFFSET %s
        """, (limit, offset))
        return cursor.fetchall()
    finally:
        conn.close()


def get_group_full_details(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT g.chat_id, g.title, g.member_count, g.games_played, g.first_seen, g.left_at,
                   COALESCE(s.rounds_played, 0), g.username, g.invite_link,
                   g.added_by_id, g.added_by_name, g.removed_by_id, g.removed_by_name
            FROM groups g
            LEFT JOIN scramble_group_stats s ON s.chat_id = g.chat_id
            WHERE g.chat_id=%s
        """, (chat_id,))
        return cursor.fetchone()
    finally:
        conn.close()


def update_group_member_count(chat_id, count):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE groups SET member_count=%s WHERE chat_id=%s", (count, chat_id))
        conn.commit()
    finally:
        conn.close()


def increment_group_games(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE groups SET games_played = games_played + 1, last_active=%s WHERE chat_id=%s",
            (datetime.now().isoformat(), chat_id)
        )
        conn.commit()
    finally:
        conn.close()


def get_users_count():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        return cursor.fetchone()[0]
    finally:
        conn.close()


def get_users_page(offset, limit):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.id, u.name, u.username, COALESCE(g.coins, 0), u.last_active
            FROM users u
            LEFT JOIN global_scores g ON g.user_id = u.id
            ORDER BY COALESCE(g.coins, 0) DESC, u.last_active DESC
            LIMIT %s OFFSET %s
        """, (limit, offset))
        return cursor.fetchall()
    finally:
        conn.close()


def get_groups_count():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM groups")
        return cursor.fetchone()[0]
    finally:
        conn.close()


def get_groups_page(offset, limit, sort="games"):
    conn = connect()
    try:
        cursor = conn.cursor()
        if sort == "time":
            order_clause = "g.first_seen ASC"
        else:
            order_clause = "(g.games_played + COALESCE(s.rounds_played, 0)) DESC, g.last_active DESC"
        cursor.execute(f"""
            SELECT g.chat_id, g.title, g.member_count, g.games_played, g.last_active
            FROM groups g
            LEFT JOIN scramble_group_stats s ON s.chat_id = g.chat_id
            ORDER BY {order_clause}
            LIMIT %s OFFSET %s
        """, (limit, offset))
        return cursor.fetchall()
    finally:
        conn.close()


def get_all_groups():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT chat_id, title, member_count, games_played FROM groups ORDER BY first_seen ASC
        """)
        return cursor.fetchall()
    finally:
        conn.close()


def search_groups(query):
    conn = connect()
    try:
        cursor = conn.cursor()
        query = query.strip()
        if re.fullmatch(r"-?\d+", query):
            cursor.execute("""
                SELECT chat_id, title, member_count, games_played FROM groups WHERE chat_id=%s
            """, (int(query),))
        else:
            cursor.execute("""
                SELECT chat_id, title, member_count, games_played FROM groups WHERE title ILIKE %s
                ORDER BY first_seen ASC
            """, (f"%{query}%",))
        return cursor.fetchall()
    finally:
        conn.close()


def get_group_count():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM groups")
        return cursor.fetchone()[0]
    finally:
        conn.close()


def get_top_groups(limit=10):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT title, games_played FROM groups ORDER BY games_played DESC LIMIT %s", (limit,))
        return cursor.fetchall()
    finally:
        conn.close()


def get_top_groups_full(limit=10):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, title, games_played FROM groups ORDER BY games_played DESC LIMIT %s", (limit,))
        return cursor.fetchall()
    finally:
        conn.close()


def get_group_details(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT chat_id, title, member_count, games_played, first_seen FROM groups WHERE chat_id=%s
        """, (chat_id,))
        return cursor.fetchone()
    finally:
        conn.close()


def get_user_count():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        return cursor.fetchone()[0]
    finally:
        conn.close()


def get_all_users(limit=None):
    conn = connect()
    try:
        cursor = conn.cursor()
        sql = """
            SELECT u.id, u.name, u.username, COALESCE(g.coins, 0)
            FROM users u LEFT JOIN global_scores g ON g.user_id = u.id ORDER BY u.id ASC
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        cursor.execute(sql)
        return cursor.fetchall()
    finally:
        conn.close()


def search_users(query):
    conn = connect()
    try:
        cursor = conn.cursor()
        query = query.strip().lstrip("@")
        if re.fullmatch(r"\d+", query):
            cursor.execute("""
                SELECT u.id, u.name, u.username, COALESCE(g.coins, 0)
                FROM users u LEFT JOIN global_scores g ON g.user_id = u.id WHERE u.id=%s
            """, (int(query),))
        else:
            cursor.execute("""
                SELECT u.id, u.name, u.username, COALESCE(g.coins, 0)
                FROM users u LEFT JOIN global_scores g ON g.user_id = u.id
                WHERE u.name ILIKE %s OR u.username ILIKE %s ORDER BY u.id ASC
            """, (f"%{query}%", f"%{query}%"))
        return cursor.fetchall()
    finally:
        conn.close()


def get_user_by_username(username):
    conn = connect()
    try:
        cursor = conn.cursor()
        username = username.strip().lstrip("@")
        cursor.execute("""
            SELECT u.id, u.name, u.username, COALESCE(g.coins, 0)
            FROM users u LEFT JOIN global_scores g ON g.user_id = u.id
            WHERE LOWER(u.username) = LOWER(%s)
        """, (username,))
        return cursor.fetchone()
    finally:
        conn.close()


def get_user_by_id(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.id, u.name, u.username, COALESCE(g.coins, 0)
            FROM users u LEFT JOIN global_scores g ON g.user_id = u.id WHERE u.id = %s
        """, (user_id,))
        return cursor.fetchone()
    finally:
        conn.close()


def ban_user(user_id, banned_by):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO banned_users (user_id, banned_at, banned_by)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET banned_at=EXCLUDED.banned_at, banned_by=EXCLUDED.banned_by
        """, (user_id, datetime.now().isoformat(), banned_by))
        conn.commit()
    finally:
        conn.close()


def unban_user(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM banned_users WHERE user_id=%s", (user_id,))
        conn.commit()
    finally:
        conn.close()


def is_user_banned(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM banned_users WHERE user_id=%s", (user_id,))
        return cursor.fetchone() is not None
    finally:
        conn.close()


def ban_group(chat_id, banned_by):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO banned_groups (chat_id, banned_at, banned_by)
            VALUES (%s, %s, %s)
            ON CONFLICT (chat_id) DO UPDATE SET banned_at=EXCLUDED.banned_at, banned_by=EXCLUDED.banned_by
        """, (chat_id, datetime.now().isoformat(), banned_by))
        conn.commit()
    finally:
        conn.close()


def unban_group(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM banned_groups WHERE chat_id=%s", (chat_id,))
        conn.commit()
    finally:
        conn.close()


def is_group_banned(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM banned_groups WHERE chat_id=%s", (chat_id,))
        return cursor.fetchone() is not None
    finally:
        conn.close()


def tournament_ban(target_type, target_id, banned_by):
    """target_type is 'user' or 'group'. Idempotent (re-banning refreshes the row)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO tournament_bans (target_type, target_id, banned_by, banned_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (target_type, target_id) DO UPDATE SET
                banned_by=EXCLUDED.banned_by, banned_at=EXCLUDED.banned_at
        """, (target_type, target_id, banned_by, datetime.now().isoformat()))
        conn.commit()
    finally:
        conn.close()


def tournament_unban(target_type, target_id):
    """Returns True if a ban row was actually removed."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM tournament_bans WHERE target_type=%s AND target_id=%s",
            (target_type, target_id),
        )
        removed = cursor.rowcount > 0
        conn.commit()
        return removed
    finally:
        conn.close()


def is_tournament_banned(target_type, target_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM tournament_bans WHERE target_type=%s AND target_id=%s",
            (target_type, target_id),
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()


# =======================================================================
# WORD SCRAMBLE TOURNAMENT
# Every money movement is done in the SAME transaction as the status change
# that guards it (WHERE status='staked'), so a crash or double-fire can never
# double-pay, double-refund, or take a stake without recording it.
# =======================================================================

_TOURNAMENT_COLS = (
    "id, chat_id, host_id, fee, difficulty, total_rounds, current_round, status, "
    "current_word, current_scrambled, round_started_at, created_at, lobby_message_id, "
    "lobby_extra_seconds, paused_at, idle_since"
)
_TOURNAMENT_KEYS = [c.strip() for c in _TOURNAMENT_COLS.split(",")]


def _tournament_row(row):
    return dict(zip(_TOURNAMENT_KEYS, row)) if row else None


def _tx_deduct(cursor, user_id, name, amount):
    """Atomic conditional deduct inside an open transaction: balance >= amount
    is checked and subtracted in ONE statement. Returns new balance or None."""
    cursor.execute("""
        UPDATE scramble_global_stats
        SET earned = earned - %s, name = %s
        WHERE user_id = %s AND earned >= %s
        RETURNING earned
    """, (amount, name, user_id, amount))
    row = cursor.fetchone()
    return row[0] if row else None


def _tx_credit(cursor, user_id, name, amount):
    """Additive credit inside an open transaction - identical SQL semantics to
    scramble_adjust_global_earned, but sharing the caller's transaction."""
    cursor.execute("""
        INSERT INTO scramble_global_stats (user_id, name, wins, earned)
        VALUES (%s, %s, 0, GREATEST(0, %s))
        ON CONFLICT (user_id) DO UPDATE SET
            earned = GREATEST(0, scramble_global_stats.earned + %s),
            name = EXCLUDED.name
        RETURNING earned
    """, (user_id, name, amount, amount))
    return cursor.fetchone()[0]


def scramble_conditional_deduct(user_id, name, amount):
    """Atomically deducts `amount` from the Word Scramble global balance only
    if the balance covers it. Returns the new balance, or None if insufficient
    (never deducts partially, never goes negative)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        new_balance = _tx_deduct(cursor, user_id, name, amount)
        conn.commit()
        return new_balance
    finally:
        conn.close()


def tournament_create(chat_id, host_id, host_name, fee, difficulty, total_rounds):
    """Returns ('ok', tournament_id, host_new_balance) | ('exists', None, None)
    | ('insufficient', None, None). Deduct + tournament row + host player row
    are one transaction."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            new_balance = _tx_deduct(cursor, host_id, host_name, fee)
            if new_balance is None:
                conn.rollback()
                return "insufficient", None, None

            cursor.execute("""
                INSERT INTO tournaments (chat_id, host_id, fee, difficulty, total_rounds,
                                         current_round, status, created_at)
                SELECT %s, %s, %s, %s, %s, 0, 'lobby', %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM tournaments WHERE chat_id=%s AND status IN ('lobby', 'active', 'paused')
                )
                RETURNING id
            """, (chat_id, host_id, fee, difficulty, total_rounds, datetime.now().isoformat(), chat_id))
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return "exists", None, None
            tid = row[0]

            cursor.execute("""
                INSERT INTO tournament_players (tournament_id, user_id, name, score, status)
                VALUES (%s, %s, %s, 0, 'staked')
            """, (tid, host_id, host_name))
            conn.commit()
            return "ok", tid, new_balance
        except Exception as e:
            conn.rollback()
            if getattr(e, "pgcode", None) == "23505":  # racing create hit the unique index
                return "exists", None, None
            raise
    finally:
        conn.close()


def tournament_join(tournament_id, user_id, name):
    """Returns ('ok', new_balance) | ('closed', None) | ('already', None)
    | ('insufficient', None)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT status, fee FROM tournaments WHERE id=%s FOR UPDATE", (tournament_id,))
            row = cursor.fetchone()
            if not row or row[0] != "lobby":
                conn.rollback()
                return "closed", None
            fee = row[1]

            cursor.execute(
                "SELECT 1 FROM tournament_players WHERE tournament_id=%s AND user_id=%s",
                (tournament_id, user_id),
            )
            if cursor.fetchone():
                conn.rollback()
                return "already", None

            new_balance = _tx_deduct(cursor, user_id, name, fee)
            if new_balance is None:
                conn.rollback()
                return "insufficient", None

            cursor.execute("""
                INSERT INTO tournament_players (tournament_id, user_id, name, score, status)
                VALUES (%s, %s, %s, 0, 'staked')
            """, (tournament_id, user_id, name))
            conn.commit()
            return "ok", new_balance
        except Exception as e:
            conn.rollback()
            if getattr(e, "pgcode", None) == "23505":
                return "already", None
            raise
    finally:
        conn.close()


def tournament_get(tournament_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT {_TOURNAMENT_COLS} FROM tournaments WHERE id=%s", (tournament_id,))
        return _tournament_row(cursor.fetchone())
    finally:
        conn.close()


def tournament_get_open_for_chat(chat_id):
    """The chat's lobby/active/paused tournament (at most one), or None."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {_TOURNAMENT_COLS} FROM tournaments "
            f"WHERE chat_id=%s AND status IN ('lobby', 'active', 'paused')",
            (chat_id,),
        )
        return _tournament_row(cursor.fetchone())
    finally:
        conn.close()


def tournament_get_all_open():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {_TOURNAMENT_COLS} FROM tournaments WHERE status IN ('lobby', 'active', 'paused')"
        )
        return [_tournament_row(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def tournament_get_players(tournament_id):
    """Join order. Rows: (user_id, name, score, status)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, name, score, status FROM tournament_players
            WHERE tournament_id=%s ORDER BY id ASC
        """, (tournament_id,))
        return cursor.fetchall()
    finally:
        conn.close()


def tournament_is_staked_player(tournament_id, user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 1 FROM tournament_players
            WHERE tournament_id=%s AND user_id=%s AND status='staked'
        """, (tournament_id, user_id))
        return cursor.fetchone() is not None
    finally:
        conn.close()


def tournament_set_lobby_message(tournament_id, message_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE tournaments SET lobby_message_id=%s WHERE id=%s", (message_id, tournament_id))
        conn.commit()
    finally:
        conn.close()


def tournament_extend_lobby(tournament_id, seconds):
    """Adds `seconds` to a LOBBY's closing time (persisted, so a restart keeps
    it). Returns the new total extra seconds, or None if it isn't a lobby."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tournaments
            SET lobby_extra_seconds = COALESCE(lobby_extra_seconds, 0) + %s
            WHERE id=%s AND status='lobby'
            RETURNING lobby_extra_seconds
        """, (seconds, tournament_id))
        row = cursor.fetchone()
        conn.commit()
        return row[0] if row else None
    finally:
        conn.close()


def tournament_activate(tournament_id):
    """lobby -> active, exactly once (so /bbbstart and the 120s job can never
    both start it). current_round counts rounds STARTED, so it begins at 0
    and becomes 1 when round 1's word is set."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tournaments SET status='active', current_round=0, idle_since=%s
            WHERE id=%s AND status='lobby' RETURNING id
        """, (datetime.now().isoformat(), tournament_id))
        ok = cursor.fetchone() is not None
        conn.commit()
        return ok
    finally:
        conn.close()


def tournament_set_round(tournament_id, word, scrambled):
    """Starts the next round. Returns the new round number, or None if the
    tournament isn't active or a round is already live."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tournaments
            SET current_round = current_round + 1, current_word=%s, current_scrambled=%s,
                round_started_at=%s
            WHERE id=%s AND status='active' AND current_word IS NULL
            RETURNING current_round
        """, (word, scrambled, datetime.now().isoformat(), tournament_id))
        row = cursor.fetchone()
        conn.commit()
        return row[0] if row else None
    finally:
        conn.close()


def tournament_unset_round(tournament_id, round_number):
    """Rolls back a round whose word message never reached the group."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tournaments
            SET current_word=NULL, current_scrambled=NULL, round_started_at=NULL,
                current_round = current_round - 1, idle_since=%s
            WHERE id=%s AND status='active' AND current_round=%s AND current_word IS NOT NULL
        """, (datetime.now().isoformat(), tournament_id, round_number))
        conn.commit()
    finally:
        conn.close()


def tournament_claim_round(tournament_id, scorer_id=None, points=0):
    """Atomically closes the live round - only ONE caller (a correct guess or
    the timeout job) can ever win this. If scorer_id is given, that player's
    score is bumped in the same transaction (and only if they're a staked
    player; otherwise nothing changes and None is returned).
    Returns {word, round_no, total_rounds, score} or None."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            WITH old AS (
                SELECT id, current_word FROM tournaments
                WHERE id=%s AND status='active' AND current_word IS NOT NULL
                FOR UPDATE
            )
            UPDATE tournaments t
            SET current_word=NULL, current_scrambled=NULL, round_started_at=NULL,
                idle_since=%s
            FROM old WHERE t.id = old.id
            RETURNING old.current_word, t.current_round, t.total_rounds
        """, (tournament_id, datetime.now().isoformat()))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return None
        result = {"word": row[0], "round_no": row[1], "total_rounds": row[2], "score": None}

        if scorer_id is not None:
            cursor.execute("""
                UPDATE tournament_players SET score = score + %s
                WHERE tournament_id=%s AND user_id=%s AND status='staked'
                RETURNING score
            """, (points, tournament_id, scorer_id))
            srow = cursor.fetchone()
            if not srow:
                conn.rollback()
                return None
            result["score"] = srow[0]

        conn.commit()
        return result
    finally:
        conn.close()


def tournament_settle(tournament_id):
    """Ends an ACTIVE tournament and pays out / refunds, all in one transaction.
    Idempotent: a second call finds status='finished' and returns None.
    Returns {case: 'single'|'tie'|'refund', fee, pot, chat_id,
             standings: [(uid, name, score)] (score desc),
             winners: [(uid, name, payout, new_balance)],
             refunds: [(uid, name, fee, new_balance)]} or None."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT status, fee, chat_id FROM tournaments WHERE id=%s FOR UPDATE",
                (tournament_id,),
            )
            trow = cursor.fetchone()
            if not trow or trow[0] != "active":
                conn.rollback()
                return None
            _status, fee, chat_id = trow

            cursor.execute("""
                SELECT user_id, name, score FROM tournament_players
                WHERE tournament_id=%s AND status='staked' ORDER BY id ASC
            """, (tournament_id,))
            players = cursor.fetchall()
            if not players:
                cursor.execute("UPDATE tournaments SET status='finished', current_word=NULL WHERE id=%s", (tournament_id,))
                conn.commit()
                return {"case": "refund", "fee": fee, "pot": 0, "chat_id": chat_id,
                        "standings": [], "winners": [], "refunds": []}

            # Advertised prize = every entry fee PLUS one bonus entry fee added
            # by the bot (fee x (players + 1)); the winner(s) are paid exactly
            # what the lobby announced.
            pot = fee * (len(players) + 1)
            top = max(p[2] for p in players)
            standings = sorted(((p[0], p[1], p[2]) for p in players), key=lambda x: -x[2])
            winners, refunds = [], []

            def _mark(uid, new_status):
                cursor.execute("""
                    UPDATE tournament_players SET status=%s
                    WHERE tournament_id=%s AND user_id=%s AND status='staked'
                """, (new_status, tournament_id, uid))
                return cursor.rowcount == 1

            if top <= 0:
                case = "refund"
                for uid, name, _score in players:
                    if _mark(uid, "refunded"):
                        refunds.append((uid, name, fee, _tx_credit(cursor, uid, name, fee)))
            else:
                top_players = [p for p in players if p[2] == top]  # already in join order
                case = "single" if len(top_players) == 1 else "tie"
                share, remainder = divmod(pot, len(top_players))
                for i, (uid, name, _score) in enumerate(top_players):
                    payout = share + (remainder if i == 0 else 0)  # remainder -> earliest joiner
                    if _mark(uid, "won"):
                        winners.append((uid, name, payout, _tx_credit(cursor, uid, name, payout)))
                for uid, name, score in players:
                    if score != top:
                        _mark(uid, "lost")

            cursor.execute(
                "UPDATE tournaments SET status='finished', current_word=NULL, current_scrambled=NULL "
                "WHERE id=%s",
                (tournament_id,),
            )
            conn.commit()
            return {"case": case, "fee": fee, "pot": pot, "chat_id": chat_id,
                    "standings": standings, "winners": winners, "refunds": refunds}
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def tournament_abort(tournament_id, delete=False):
    """Refunds every still-'staked' player and finalizes a lobby/active/paused
    tournament (status 'finished', or the rows deleted when delete=True).
    One transaction, guarded per player by WHERE status='staked'.
    Returns {chat_id, fee, refunds: [(uid, name, fee, new_balance)]} or None
    if there was nothing open to abort."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT status, fee, chat_id FROM tournaments WHERE id=%s FOR UPDATE",
                (tournament_id,),
            )
            trow = cursor.fetchone()
            if not trow or trow[0] not in ("lobby", "active", "paused"):
                conn.rollback()
                return None
            _status, fee, chat_id = trow

            cursor.execute("""
                SELECT user_id, name FROM tournament_players
                WHERE tournament_id=%s AND status='staked' ORDER BY id ASC
            """, (tournament_id,))
            refunds = []
            for uid, name in cursor.fetchall():
                cursor.execute("""
                    UPDATE tournament_players SET status='refunded'
                    WHERE tournament_id=%s AND user_id=%s AND status='staked'
                """, (tournament_id, uid))
                if cursor.rowcount == 1:
                    refunds.append((uid, name, fee, _tx_credit(cursor, uid, name, fee)))

            if delete:
                cursor.execute("DELETE FROM tournament_players WHERE tournament_id=%s", (tournament_id,))
                cursor.execute("DELETE FROM tournaments WHERE id=%s", (tournament_id,))
            else:
                cursor.execute(
                    "UPDATE tournaments SET status='finished', current_word=NULL, current_scrambled=NULL "
                    "WHERE id=%s",
                    (tournament_id,),
                )
            conn.commit()
            return {"chat_id": chat_id, "fee": fee, "refunds": refunds}
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def tournament_pause(tournament_id):
    """active -> paused, exactly once. Keeps the word, round, scores and every
    stake untouched. Returns True if THIS call paused it."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tournaments SET status='paused', paused_at=%s
            WHERE id=%s AND status='active' RETURNING id
        """, (datetime.now().isoformat(), tournament_id))
        ok = cursor.fetchone() is not None
        conn.commit()
        return ok
    finally:
        conn.close()


def tournament_resume(tournament_id, stale_round_cutoff, idle_cutoff):
    """Atomically resumes a tournament. Allowed when it is:
      - 'paused', or
      - 'active' but STUCK: a live round that started before stale_round_cutoff
        (its deadline + grace has passed), or no live round and idle since
        before idle_cutoff (or never recorded).
    Sets status='active', clears paused_at and, if a round is live, restarts
    its clock (round_started_at = now) WITHOUT touching the word, the round
    number or any score. The row is locked, so two people running the command
    at once cannot both resume it (the second sees a healthy, freshly-started
    round and gets None).
    Returns the tournament dict plus 'prev_status', or None."""
    now = datetime.now().isoformat()
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                WITH old AS (
                    SELECT id, status AS prev_status FROM tournaments
                    WHERE id=%s AND (
                        status='paused'
                        OR (status='active' AND (
                            (current_word IS NOT NULL AND
                                (round_started_at IS NULL
                                 OR round_started_at::timestamp < %s::timestamp))
                            OR (current_word IS NULL AND
                                (idle_since IS NULL
                                 OR idle_since::timestamp < %s::timestamp))
                        ))
                    )
                    FOR UPDATE
                )
                UPDATE tournaments t
                SET status='active', paused_at=NULL,
                    round_started_at = CASE WHEN t.current_word IS NOT NULL
                                            THEN %s ELSE t.round_started_at END,
                    idle_since = CASE WHEN t.current_word IS NULL
                                      THEN %s ELSE t.idle_since END
                FROM old WHERE t.id = old.id
                RETURNING old.prev_status
            """, (tournament_id, stale_round_cutoff, idle_cutoff, now, now))
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return None
            prev_status = row[0]
            cursor.execute(f"SELECT {_TOURNAMENT_COLS} FROM tournaments WHERE id=%s", (tournament_id,))
            t = _tournament_row(cursor.fetchone())
            conn.commit()
            t["prev_status"] = prev_status
            return t
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


# =======================================================================
# /transfer, /request  (Word Scramble global balance only)
# Every balance movement is ONE transaction together with the status change /
# audit row that guards it. Balance rows are locked in ascending user_id order
# so two opposite transfers can never deadlock each other.
# =======================================================================

_REQUEST_COLS = (
    "id, chat_id, message_id, requester_id, requester_name, target_id, target_name, "
    "amount, status, created_at, resolved_at"
)
_REQUEST_KEYS = [c.strip() for c in _REQUEST_COLS.split(",")]


def _request_row(row):
    return dict(zip(_REQUEST_KEYS, row)) if row else None


def _tx_lock_balances(cursor, user_ids):
    """Locks the existing balance rows for these users in a fixed order."""
    cursor.execute(
        "SELECT user_id FROM scramble_global_stats WHERE user_id = ANY(%s) "
        "ORDER BY user_id FOR UPDATE",
        (sorted(set(user_ids)),),
    )
    cursor.fetchall()


def _tx_audit(cursor, kind, from_id, to_id, amount, chat_id):
    cursor.execute("""
        INSERT INTO balance_transfers (kind, from_id, to_id, amount, chat_id, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (kind, from_id, to_id, amount, chat_id, datetime.now().isoformat()))


def transfer_execute(from_id, from_name, to_id, to_name, amount, chat_id):
    """Moves `amount` from one Word Scramble global balance to another in ONE
    transaction: conditional deduct (never negative) + credit + audit row.
    Returns ('ok', sender_new_balance, recipient_new_balance) or
    ('insufficient', sender_current_balance, None)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            _tx_lock_balances(cursor, [from_id, to_id])
            new_from = _tx_deduct(cursor, from_id, from_name, amount)
            if new_from is None:
                cursor.execute("SELECT earned FROM scramble_global_stats WHERE user_id=%s", (from_id,))
                row = cursor.fetchone()
                conn.rollback()
                return "insufficient", (row[0] if row else 0), None
            new_to = _tx_credit(cursor, to_id, to_name, amount)
            _tx_audit(cursor, "transfer", from_id, to_id, amount, chat_id)
            conn.commit()
            return "ok", new_from, new_to
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def request_create(chat_id, requester_id, requester_name, target_id, target_name, amount,
                   cooldown_seconds, max_pending, expire_seconds):
    """Creates a pending money request, enforcing the anti-harassment limits
    inside one transaction (serialised per requester with an advisory lock).
    Returns (status, value, swept) where status is:
      'ok'       value = new request id
      'cooldown' value = seconds left
      'pair'     value = None   (already one pending request to that person)
      'max'      value = None   (already at the pending-request limit)
    `swept` = rows that were overdue-pending and got marked 'expired' on the
    way (so the caller can update their prompts)."""
    now = datetime.now()
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (requester_id,))

            stale_cutoff = (now - timedelta(seconds=expire_seconds)).isoformat()
            cursor.execute(f"""
                UPDATE money_requests SET status='expired', resolved_at=%s
                WHERE requester_id=%s AND status='pending' AND created_at < %s
                RETURNING {_REQUEST_COLS}
            """, (now.isoformat(), requester_id, stale_cutoff))
            swept = [_request_row(r) for r in cursor.fetchall()]

            cursor.execute("SELECT MAX(created_at) FROM money_requests WHERE requester_id=%s", (requester_id,))
            last = cursor.fetchone()[0]
            if last:
                try:
                    since = (now - datetime.fromisoformat(last)).total_seconds()
                    if since < cooldown_seconds:
                        conn.commit()  # keep the sweep
                        return "cooldown", int(cooldown_seconds - since) + 1, swept
                except (TypeError, ValueError):
                    pass

            cursor.execute("""
                SELECT 1 FROM money_requests
                WHERE requester_id=%s AND target_id=%s AND status='pending'
            """, (requester_id, target_id))
            if cursor.fetchone():
                conn.commit()
                return "pair", None, swept

            cursor.execute(
                "SELECT COUNT(*) FROM money_requests WHERE requester_id=%s AND status='pending'",
                (requester_id,),
            )
            if cursor.fetchone()[0] >= max_pending:
                conn.commit()
                return "max", None, swept

            cursor.execute("""
                INSERT INTO money_requests (chat_id, requester_id, requester_name, target_id,
                                            target_name, amount, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s)
                RETURNING id
            """, (chat_id, requester_id, requester_name, target_id, target_name, amount, now.isoformat()))
            rid = cursor.fetchone()[0]
            conn.commit()
            return "ok", rid, swept
        except Exception as e:
            conn.rollback()
            if getattr(e, "pgcode", None) == "23505":  # racing duplicate hit the unique index
                return "pair", None, []
            raise
    finally:
        conn.close()


def request_set_message(request_id, chat_id, message_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE money_requests SET chat_id=%s, message_id=%s WHERE id=%s",
            (chat_id, message_id, request_id),
        )
        conn.commit()
    finally:
        conn.close()


def request_get(request_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT {_REQUEST_COLS} FROM money_requests WHERE id=%s", (request_id,))
        return _request_row(cursor.fetchone())
    finally:
        conn.close()


def request_get_pending():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT {_REQUEST_COLS} FROM money_requests WHERE status='pending'")
        return [_request_row(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def request_expire(request_id):
    """pending -> expired, exactly once. Returns the row, or None if it was
    already resolved (paid/declined/expired). No money moves."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(f"""
            UPDATE money_requests SET status='expired', resolved_at=%s
            WHERE id=%s AND status='pending'
            RETURNING {_REQUEST_COLS}
        """, (datetime.now().isoformat(), request_id))
        row = cursor.fetchone()
        conn.commit()
        return _request_row(row)
    finally:
        conn.close()


def _request_lock(cursor, request_id):
    cursor.execute(f"SELECT {_REQUEST_COLS} FROM money_requests WHERE id=%s FOR UPDATE", (request_id,))
    return _request_row(cursor.fetchone())


def _request_is_overdue(row, expire_seconds):
    try:
        return (datetime.now() - datetime.fromisoformat(row["created_at"])).total_seconds() >= expire_seconds
    except (TypeError, ValueError):
        return False


def request_pay(request_id, actor_id, expire_seconds):
    """The target pressed Yes. ONE transaction: lock the request row, make sure
    it is still 'pending' (and not overdue), conditionally deduct from the
    target, credit the requester, mark 'paid', write the audit row.
    Returns (code, row, extra):
      ('paid', row, (target_balance, requester_balance))
      ('insufficient', row, target_balance)   - stays pending
      ('expired', row, None)                  - marked expired now, no money moved
      ('closed', row, None)                   - already paid/declined/expired
      ('notyours', row, None) | ('gone', None, None)"""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            row = _request_lock(cursor, request_id)
            if not row:
                conn.rollback()
                return "gone", None, None
            if row["target_id"] != actor_id:
                conn.rollback()
                return "notyours", row, None
            if row["status"] != "pending":
                conn.rollback()
                return "closed", row, None
            if _request_is_overdue(row, expire_seconds):
                cursor.execute(
                    "UPDATE money_requests SET status='expired', resolved_at=%s "
                    "WHERE id=%s AND status='pending'",
                    (datetime.now().isoformat(), request_id),
                )
                conn.commit()
                row["status"] = "expired"
                return "expired", row, None

            _tx_lock_balances(cursor, [row["target_id"], row["requester_id"]])
            target_bal = _tx_deduct(cursor, row["target_id"], row["target_name"], row["amount"])
            if target_bal is None:
                cursor.execute("SELECT earned FROM scramble_global_stats WHERE user_id=%s", (row["target_id"],))
                brow = cursor.fetchone()
                conn.rollback()
                return "insufficient", row, (brow[0] if brow else 0)
            req_bal = _tx_credit(cursor, row["requester_id"], row["requester_name"], row["amount"])
            cursor.execute(
                "UPDATE money_requests SET status='paid', resolved_at=%s "
                "WHERE id=%s AND status='pending'",
                (datetime.now().isoformat(), request_id),
            )
            if cursor.rowcount != 1:  # cannot happen under the row lock - belt and braces
                conn.rollback()
                return "closed", row, None
            _tx_audit(cursor, "request", row["target_id"], row["requester_id"], row["amount"], row["chat_id"])
            conn.commit()
            row["status"] = "paid"
            return "paid", row, (target_bal, req_bal)
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def request_decline(request_id, actor_id, expire_seconds):
    """The target pressed No. Returns (code, row): 'declined' | 'expired'
    (was overdue, marked expired instead) | 'closed' | 'notyours' | 'gone'."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            row = _request_lock(cursor, request_id)
            if not row:
                conn.rollback()
                return "gone", None
            if row["target_id"] != actor_id:
                conn.rollback()
                return "notyours", row
            if row["status"] != "pending":
                conn.rollback()
                return "closed", row
            new_status = "expired" if _request_is_overdue(row, expire_seconds) else "declined"
            cursor.execute(
                "UPDATE money_requests SET status=%s, resolved_at=%s WHERE id=%s AND status='pending'",
                (new_status, datetime.now().isoformat(), request_id),
            )
            conn.commit()
            row["status"] = new_status
            return new_status, row
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


# ---- /deleterequestprompt setting + persisted single-message deletions ----

def request_prompt_delete_get(chat_id, default_seconds):
    """Seconds after which a resolved request prompt is deleted in this group
    (0 = never). No row = default_seconds."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT delete_seconds FROM request_prompt_settings WHERE chat_id=%s", (chat_id,))
        row = cursor.fetchone()
        return default_seconds if not row or row[0] is None else row[0]
    finally:
        conn.close()


def request_prompt_delete_set(chat_id, seconds):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO request_prompt_settings (chat_id, delete_seconds) VALUES (%s, %s)
            ON CONFLICT (chat_id) DO UPDATE SET delete_seconds=EXCLUDED.delete_seconds
        """, (chat_id, seconds))
        conn.commit()
    finally:
        conn.close()


def scheduled_deletion_add(chat_id, message_id, delete_at_iso):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO scheduled_deletions (chat_id, message_id, delete_at) VALUES (%s, %s, %s)
            ON CONFLICT (chat_id, message_id) DO UPDATE SET delete_at=EXCLUDED.delete_at
        """, (chat_id, message_id, delete_at_iso))
        conn.commit()
    finally:
        conn.close()


def scheduled_deletion_remove(chat_id, message_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM scheduled_deletions WHERE chat_id=%s AND message_id=%s", (chat_id, message_id)
        )
        conn.commit()
    finally:
        conn.close()


def scheduled_deletion_get_all():
    """Rows: (chat_id, message_id, delete_at)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, message_id, delete_at FROM scheduled_deletions")
        return cursor.fetchall()
    finally:
        conn.close()


def dollar_purchase_credit(charge_id, user_id, name, coins, stars):
    """Idempotent /buydollar delivery. ONE transaction: record the Stars charge
    (ON CONFLICT DO NOTHING) and credit `coins` to the buyer's Word Scramble
    global balance. Returns the new balance, or None if this charge was already
    applied (redelivered payment - nothing is credited the second time)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO dollar_purchases (charge_id, user_id, coins, stars, paid_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (charge_id) DO NOTHING
            """, (charge_id, user_id, coins, stars, datetime.now().isoformat()))
            if cursor.rowcount == 0:
                conn.rollback()
                return None
            new_balance = _tx_credit(cursor, user_id, name, coins)
            conn.commit()
            return new_balance
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def premium_get_expiry(user_id):
    """Returns the premium expiry as a datetime, or None if there's no row."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT expires_at FROM premium_users WHERE user_id=%s", (user_id,))
        row = cursor.fetchone()
        if not row or not row[0]:
            return None
        try:
            return datetime.fromisoformat(row[0])
        except (TypeError, ValueError):
            return None
    finally:
        conn.close()


def premium_grant_from_payment(charge_id, user_id, stars, duration_seconds):
    """Idempotent: records the charge and extends premium in ONE transaction.
    Extends from the current expiry if still active, else from now.
    Returns the new expiry (datetime), or None if this charge was already applied."""
    conn = connect()
    try:
        cursor = conn.cursor()
        now = datetime.now()
        cursor.execute("""
            INSERT INTO premium_payments (charge_id, user_id, stars, duration_seconds, paid_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (charge_id) DO NOTHING
        """, (charge_id, user_id, stars, duration_seconds, now.isoformat()))
        if cursor.rowcount == 0:
            conn.rollback()
            return None

        cursor.execute("SELECT expires_at FROM premium_users WHERE user_id=%s FOR UPDATE", (user_id,))
        row = cursor.fetchone()
        base = now
        if row and row[0]:
            try:
                current = datetime.fromisoformat(row[0])
                if current > now:
                    base = current
            except (TypeError, ValueError):
                pass
        new_expiry = base + timedelta(seconds=duration_seconds)

        cursor.execute("""
            INSERT INTO premium_users (user_id, expires_at, last_renewal_prompt_at)
            VALUES (%s, %s, NULL)
            ON CONFLICT (user_id) DO UPDATE SET
                expires_at=EXCLUDED.expires_at, last_renewal_prompt_at=NULL
        """, (user_id, new_expiry.isoformat()))
        conn.commit()
        return new_expiry
    finally:
        conn.close()


def premium_grant_gift(user_id, duration_seconds):
    """Owner gift: extends premium from the current expiry if still active,
    else from now. No payment record. Returns the new expiry (datetime)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        now = datetime.now()
        cursor.execute("SELECT expires_at FROM premium_users WHERE user_id=%s FOR UPDATE", (user_id,))
        row = cursor.fetchone()
        base = now
        if row and row[0]:
            try:
                current = datetime.fromisoformat(row[0])
                if current > now:
                    base = current
            except (TypeError, ValueError):
                pass
        new_expiry = base + timedelta(seconds=duration_seconds)
        cursor.execute("""
            INSERT INTO premium_users (user_id, expires_at, last_renewal_prompt_at)
            VALUES (%s, %s, NULL)
            ON CONFLICT (user_id) DO UPDATE SET
                expires_at=EXCLUDED.expires_at, last_renewal_prompt_at=NULL
        """, (user_id, new_expiry.isoformat()))
        conn.commit()
        return new_expiry
    finally:
        conn.close()


def premium_count_active():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM premium_users WHERE expires_at::timestamp > %s::timestamp",
            (datetime.now().isoformat(),),
        )
        return cursor.fetchone()[0]
    finally:
        conn.close()


def premium_list_active(offset, limit):
    """Live (non-expired) premium users, soonest-to-expire first.
    Rows: (user_id, name, expires_at_iso)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.user_id, u.name, p.expires_at
            FROM premium_users p LEFT JOIN users u ON u.id = p.user_id
            WHERE p.expires_at::timestamp > %s::timestamp
            ORDER BY p.expires_at::timestamp ASC
            OFFSET %s LIMIT %s
        """, (datetime.now().isoformat(), offset, limit))
        return cursor.fetchall()
    finally:
        conn.close()


def premium_get_expiring_unprompted(within_seconds):
    """Active premium users expiring within the window who haven't been
    prompted for this expiry window yet. Rows: (user_id, expires_at_iso)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        now = datetime.now()
        cursor.execute("""
            SELECT user_id, expires_at FROM premium_users
            WHERE expires_at::timestamp > %s::timestamp
              AND expires_at::timestamp <= %s::timestamp
              AND last_renewal_prompt_at IS NULL
        """, (now.isoformat(), (now + timedelta(seconds=within_seconds)).isoformat()))
        return cursor.fetchall()
    finally:
        conn.close()


def premium_mark_prompted(user_id):
    """Returns True only for the call that actually flipped it (at-most-once)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE premium_users SET last_renewal_prompt_at=%s "
            "WHERE user_id=%s AND last_renewal_prompt_at IS NULL",
            (datetime.now().isoformat(), user_id),
        )
        changed = cursor.rowcount > 0
        conn.commit()
        return changed
    finally:
        conn.close()


def increment_stat(key, amount=1):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO bot_stats (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = bot_stats.value + EXCLUDED.value
        """, (key, amount))
        conn.commit()
    finally:
        conn.close()


def get_stat(key):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM bot_stats WHERE key=%s", (key,))
        row = cursor.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def set_config(key, value):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO bot_config (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
        """, (key, str(value)))
        conn.commit()
    finally:
        conn.close()


def get_config(key, default=None):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM bot_config WHERE key=%s", (key,))
        row = cursor.fetchone()
        return row[0] if row else default
    finally:
        conn.close()


def get_custom_words():
    """All words ever added via /addwords, persisted in Postgres so they
    survive Render's redeploy/restart wipe of local files."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT word FROM custom_words")
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


def add_custom_words(words):
    """Persists new /addwords words. Safe to call repeatedly - ON CONFLICT
    DO NOTHING means re-adding an existing word is a no-op, never an error."""
    if not words:
        return
    conn = connect()
    try:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.executemany(
            "INSERT INTO custom_words (word, added_at) VALUES (%s, %s) ON CONFLICT (word) DO NOTHING",
            [(w, now) for w in words]
        )
        conn.commit()
    finally:
        conn.close()


def get_removed_words():
    """All words ever hidden via /removewords, regardless of which source
    (a medium_words_raw.py category or a custom_words entry) they came
    from - subtracted from the merged pool wherever it's built."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT word FROM removed_words")
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


def add_removed_words(words):
    """Persists new /removewords entries. Safe to call repeatedly - ON
    CONFLICT DO NOTHING means re-removing an already-removed word is a
    no-op, never an error."""
    if not words:
        return
    conn = connect()
    try:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.executemany(
            "INSERT INTO removed_words (word, removed_at) VALUES (%s, %s) ON CONFLICT (word) DO NOTHING",
            [(w, now) for w in words]
        )
        conn.commit()
    finally:
        conn.close()


def add_stop_vote(chat_id, user_id, name):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO stop_votes (chat_id, user_id, name) VALUES (%s, %s, %s) "
            "ON CONFLICT (chat_id, user_id) DO NOTHING",
            (chat_id, user_id, name)
        )
        added = cursor.rowcount > 0
        conn.commit()
        return added
    finally:
        conn.close()


def get_stop_vote_count(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM stop_votes WHERE chat_id=%s", (chat_id,))
        return cursor.fetchone()[0]
    finally:
        conn.close()


def clear_stop_votes(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM stop_votes WHERE chat_id=%s", (chat_id,))
        conn.commit()
    finally:
        conn.close()


def get_stop_vote_names(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM stop_votes WHERE chat_id=%s ORDER BY seq ASC", (chat_id,))
        rows = cursor.fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def log_topup(target_id, target_name, amount, owner_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO topup_log (target_id, target_name, amount, owner_id, timestamp)
            VALUES (%s, %s, %s, %s, %s)
        """, (target_id, target_name, amount, owner_id, datetime.now().isoformat()))
        conn.commit()
    finally:
        conn.close()


def get_active_games_count():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM word_games WHERE leader_id IS NOT NULL")
        return cursor.fetchone()[0]
    finally:
        conn.close()


def add_bot_admin(user_id, name, username, added_by):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO bot_admins (user_id, name, username, added_by, added_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                name=EXCLUDED.name, username=EXCLUDED.username,
                added_by=EXCLUDED.added_by, added_at=EXCLUDED.added_at
        """, (user_id, name, username, added_by, datetime.now().isoformat()))
        conn.commit()
    finally:
        conn.close()


def remove_bot_admin(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bot_admins WHERE user_id=%s", (user_id,))
        removed = cursor.rowcount > 0
        conn.commit()
        return removed
    finally:
        conn.close()


def is_bot_admin(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM bot_admins WHERE user_id=%s", (user_id,))
        return cursor.fetchone() is not None
    finally:
        conn.close()


def get_bot_admins():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, name, username, added_at FROM bot_admins ORDER BY added_at ASC")
        return cursor.fetchall()
    finally:
        conn.close()


def get_active_ads_for_rotation():
    conn = connect()
    try:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute("""
            SELECT id, business_name, ad_link, ad_text
            FROM ad_subscriptions
            WHERE status IN ('active', 'expiring_soon') AND end_at > %s
            ORDER BY id ASC
        """, (now,))
        return cursor.fetchall()
    finally:
        conn.close()


def record_ad_impression(subscription_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE ad_subscriptions SET total_views = total_views + 1 WHERE id=%s", (subscription_id,))
        conn.commit()
    finally:
        conn.close()


def record_ad_click(subscription_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE ad_subscriptions SET total_clicks = total_clicks + 1 WHERE id=%s", (subscription_id,))
        conn.commit()
    finally:
        conn.close()


def get_ad_business_name(subscription_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT business_name, ad_link FROM ad_subscriptions WHERE id=%s", (subscription_id,))
        return cursor.fetchone()
    finally:
        conn.close()


def scramble_start_round(chat_id, word, scrambled):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO scramble_games (chat_id, word, scrambled, started_at, round_active)
            VALUES (%s, %s, %s, %s, 1)
            ON CONFLICT (chat_id) DO UPDATE SET
                word=EXCLUDED.word, scrambled=EXCLUDED.scrambled,
                started_at=EXCLUDED.started_at, round_active=1
        """, (chat_id, word, scrambled, datetime.now().isoformat()))
        conn.commit()
    finally:
        conn.close()


def scramble_get_round(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT chat_id, word, scrambled, started_at, round_active
            FROM scramble_games WHERE chat_id=%s AND round_active=1
        """, (chat_id,))
        return cursor.fetchone()
    finally:
        conn.close()


def scramble_end_round(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scramble_games WHERE chat_id=%s", (chat_id,))
        conn.commit()
    finally:
        conn.close()


def scramble_claim_round(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM scramble_games WHERE chat_id=%s AND round_active=1 RETURNING word",
            (chat_id,)
        )
        row = cursor.fetchone()
        conn.commit()
        return row[0] if row else None
    finally:
        conn.close()


def get_all_active_scramble_chats():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM scramble_games WHERE round_active=1")
        rows = cursor.fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def get_all_active_scramble_rounds_with_time():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, started_at FROM scramble_games WHERE round_active=1")
        return cursor.fetchall()
    finally:
        conn.close()


def scramble_get_used_words(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT word FROM scramble_used_words WHERE chat_id=%s", (chat_id,))
        rows = cursor.fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def scramble_mark_word_used(chat_id, word):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO scramble_used_words (chat_id, word) VALUES (%s, %s) ON CONFLICT (chat_id, word) DO NOTHING",
            (chat_id, word)
        )
        conn.commit()
    finally:
        conn.close()


def scramble_reset_used_words(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scramble_used_words WHERE chat_id=%s", (chat_id,))
        conn.commit()
    finally:
        conn.close()


def scramble_record_win(user_id, chat_id, name, reward_amount):
    conn = connect()
    try:
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
        return new_streak
    finally:
        conn.close()


def scramble_reset_all_streaks_in_chat(chat_id, except_user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE scramble_chat_stats SET current_streak=0 WHERE chat_id=%s AND user_id != %s",
            (chat_id, except_user_id)
        )
        conn.commit()
    finally:
        conn.close()


def scramble_add_bonus(user_id, chat_id, name, bonus_amount):
    conn = connect()
    try:
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
    finally:
        conn.close()


def scramble_get_chat_leaderboard(chat_id, limit=10):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT name, wins, earned, current_streak
            FROM scramble_chat_stats WHERE chat_id=%s ORDER BY earned DESC LIMIT %s
        """, (chat_id, limit))
        return cursor.fetchall()
    finally:
        conn.close()


def scramble_get_global_leaderboard(limit=10):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT name, wins, earned FROM scramble_global_stats ORDER BY earned DESC LIMIT %s
        """, (limit,))
        return cursor.fetchall()
    finally:
        conn.close()


def scramble_get_chat_position(chat_id, user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, name, wins, earned, current_streak
            FROM scramble_chat_stats WHERE chat_id=%s ORDER BY earned DESC, user_id ASC
        """, (chat_id,))
        rows = cursor.fetchall()
    finally:
        conn.close()
    for index, row in enumerate(rows, start=1):
        if row[0] == user_id:
            return index, row[1], row[2], row[3], row[4]
    return None


def scramble_get_global_position(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, name, wins, earned FROM scramble_global_stats ORDER BY earned DESC, user_id ASC
        """)
        rows = cursor.fetchall()
    finally:
        conn.close()
    for index, row in enumerate(rows, start=1):
        if row[0] == user_id:
            return index, row[1], row[2], row[3]
    return None


def scramble_register_group(chat_id, title):
    conn = connect()
    try:
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
    finally:
        conn.close()


def scramble_get_top_groups(limit=10):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT title, rounds_played FROM scramble_group_stats ORDER BY rounds_played DESC LIMIT %s
        """, (limit,))
        return cursor.fetchall()
    finally:
        conn.close()


def scramble_get_stats():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*), COALESCE(SUM(wins), 0), COALESCE(SUM(earned), 0) FROM scramble_global_stats")
        total_players, total_wins, total_rewards = cursor.fetchone()
        cursor.execute("SELECT COALESCE(SUM(rounds_played), 0) FROM scramble_group_stats")
        total_group_rounds = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM scramble_games")
        active_rounds = cursor.fetchone()[0]
        return {
            "total_players": total_players,
            "total_wins": total_wins,
            "total_rewards": total_rewards,
            "total_group_rounds": total_group_rounds,
            "active_rounds": active_rounds,
        }
    finally:
        conn.close()


def scramble_adjust_global_earned(user_id, name, amount):
    conn = connect()
    try:
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
        return new_balance
    finally:
        conn.close()


def get_group_by_username(username):
    """Resolve a stored group by its @username. Returns (chat_id, title) or None."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id, title FROM groups WHERE LOWER(username) = LOWER(%s)",
            (str(username).lstrip("@"),),
        )
        return cursor.fetchone()
    finally:
        conn.close()


def scramble_adjust_chat_earned(user_id, chat_id, name, amount):
    """Adjust a player's Word Scramble balance inside ONE specific group
    (scramble_chat_stats) AND their global balance (scramble_global_stats) -
    exactly as if they had earned/lost that amount playing in that group.

    Returns (new_chat_balance, new_global_balance).
    Balances are floored at 0, same as scramble_adjust_global_earned()."""
    conn = connect()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO scramble_chat_stats
                (user_id, chat_id, name, wins, earned, current_streak, best_streak)
            VALUES (%s, %s, %s, 0, GREATEST(0, %s), 0, 0)
            ON CONFLICT (user_id, chat_id) DO UPDATE SET
                earned = GREATEST(0, scramble_chat_stats.earned + %s),
                name = EXCLUDED.name
            RETURNING earned
        """, (user_id, chat_id, name, amount, amount))
        new_chat_balance = cursor.fetchone()[0]

        cursor.execute("""
            INSERT INTO scramble_global_stats (user_id, name, wins, earned)
            VALUES (%s, %s, 0, GREATEST(0, %s))
            ON CONFLICT (user_id) DO UPDATE SET
                earned = GREATEST(0, scramble_global_stats.earned + %s),
                name = EXCLUDED.name
            RETURNING earned
        """, (user_id, name, amount, amount))
        new_global_balance = cursor.fetchone()[0]

        conn.commit()
        return new_chat_balance, new_global_balance
    finally:
        conn.close()


def set_scramble_slowmode(chat_id, interval_seconds, enabled: bool):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO scramble_slowmode (chat_id, interval_seconds, enabled)
            VALUES (%s, %s, %s)
            ON CONFLICT (chat_id) DO UPDATE SET
                interval_seconds=EXCLUDED.interval_seconds, enabled=EXCLUDED.enabled
        """, (chat_id, interval_seconds, 1 if enabled else 0))
        conn.commit()
    finally:
        conn.close()


def get_scramble_slowmode(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT interval_seconds, enabled FROM scramble_slowmode WHERE chat_id=%s", (chat_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return row[0], bool(row[1])
    finally:
        conn.close()


def get_all_active_slowmode_chats():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, interval_seconds FROM scramble_slowmode WHERE enabled=1")
        return cursor.fetchall()
    finally:
        conn.close()


def scramble_set_next_round_due(chat_id, due_iso):
    """Record when the next slowmode round is due, so a restart during the
    gap between rounds knows how much longer to wait (or that it's already
    overdue) instead of leaving the chat waiting forever."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE scramble_slowmode SET next_round_due_at=%s WHERE chat_id=%s",
            (due_iso, chat_id),
        )
        conn.commit()
    finally:
        conn.close()


def scramble_clear_next_round_due(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE scramble_slowmode SET next_round_due_at=NULL WHERE chat_id=%s",
            (chat_id,),
        )
        conn.commit()
    finally:
        conn.close()


def get_pending_slowmode_waits():
    """Chats where slowmode is on and we're between rounds, waiting for the
    next one to start - used to resume that wait after a restart."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT chat_id, interval_seconds, next_round_due_at
            FROM scramble_slowmode
            WHERE enabled=1 AND next_round_due_at IS NOT NULL
        """)
        return cursor.fetchall()
    finally:
        conn.close()


def get_all_active_word_game_chats():
    """Chats with a /wordgame round in progress (someone volunteered to
    explain, whether or not they've started) - used to clean up and reopen
    the floor for a new leader after a restart."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM word_games WHERE leader_id IS NOT NULL")
        return [r[0] for r in cursor.fetchall()]
    finally:
        conn.close()


def get_combined_global_leaderboard(limit=10):
    """Global leaderboard combining regular game coins (global_scores) with
    Word Scramble earnings (scramble_global_stats) per player."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(g.name, s.name) AS name,
                   COALESCE(g.coins, 0) + COALESCE(s.earned, 0) AS total
            FROM global_scores g
            FULL OUTER JOIN scramble_global_stats s ON g.user_id = s.user_id
            ORDER BY total DESC
            LIMIT %s
        """, (limit,))
        return cursor.fetchall()
    finally:
        conn.close()


def get_combined_global_position(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(g.user_id, s.user_id) AS user_id,
                   COALESCE(g.name, s.name) AS name,
                   COALESCE(g.coins, 0) + COALESCE(s.earned, 0) AS total
            FROM global_scores g
            FULL OUTER JOIN scramble_global_stats s ON g.user_id = s.user_id
            ORDER BY total DESC, user_id ASC
        """)
        rows = cursor.fetchall()
    finally:
        conn.close()
    for index, row in enumerate(rows, start=1):
        if row[0] == user_id:
            return index, row[1], row[2]
    return None


def get_group_admin_mode_override(chat_id):
    """Returns 'on', 'off', or None (meaning: follow the global default)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT admin_mode_override FROM groups WHERE chat_id=%s", (chat_id,))
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_group_admin_mode_override(chat_id, value):
    """value must be 'on' or 'off'."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE groups SET admin_mode_override=%s WHERE chat_id=%s", (value, chat_id))
        conn.commit()
    finally:
        conn.close()


def get_effective_admin_mode(chat_id) -> bool:
    """True if the bot should require admin rights to start a game in this
    chat - the group's own override if it has one, else the global default."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT admin_mode_override FROM groups WHERE chat_id=%s", (chat_id,))
        row = cursor.fetchone()
        override = row[0] if row else None
        if override in ("on", "off"):
            return override == "on"
        cursor.execute("SELECT value FROM bot_config WHERE key=%s", ("require_bot_admin_global",))
        cfg_row = cursor.fetchone()
        return bool(cfg_row) and cfg_row[0] == "1"
    finally:
        conn.close()


def get_autodelete_settings(chat_id):
    """Returns (round_mode_on: bool, interval_seconds: int)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT autodelete_round_mode, autodelete_interval_seconds FROM groups WHERE chat_id=%s",
            (chat_id,),
        )
        row = cursor.fetchone()
        if not row:
            return False, 0
        return (row[0] == "on"), (row[1] or 0)
    finally:
        conn.close()


def set_autodelete_round_mode(chat_id, value):
    """value must be 'on' or 'off'."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE groups SET autodelete_round_mode=%s WHERE chat_id=%s", (value, chat_id))
        conn.commit()
    finally:
        conn.close()


def set_autodelete_interval(chat_id, seconds):
    """seconds=0 disables interval cleanup for this group."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE groups SET autodelete_interval_seconds=%s WHERE chat_id=%s", (seconds, chat_id)
        )
        conn.commit()
    finally:
        conn.close()


def is_autodelete_tracking_active(chat_id) -> bool:
    """True if this group needs its bot messages tracked for deletion -
    either round-based cleanup or interval cleanup is on."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT autodelete_round_mode, autodelete_interval_seconds FROM groups WHERE chat_id=%s",
            (chat_id,),
        )
        row = cursor.fetchone()
        if not row:
            return False
        return row[0] == "on" or (row[1] or 0) > 0
    finally:
        conn.close()


def get_all_autodelete_interval_groups():
    """Returns [(chat_id, interval_seconds), ...] for currently-active groups
    with interval cleanup on. Used to restore JobQueue jobs on restart."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id, autodelete_interval_seconds FROM groups "
            "WHERE autodelete_interval_seconds > 0 AND left_at IS NULL"
        )
        return cursor.fetchall()
    finally:
        conn.close()


def get_group_game_difficulty_override(chat_id):
    """Returns the override label ('easy'/'easy3'/.../'medium'/'hard'/'off') or None."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT game_difficulty_override FROM groups WHERE chat_id=%s", (chat_id,))
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_group_game_difficulty_override(chat_id, value):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE groups SET game_difficulty_override=%s WHERE chat_id=%s", (value, chat_id))
        conn.commit()
    finally:
        conn.close()


def get_effective_game_difficulty(chat_id) -> str:
    """The group's own override if set, else the global default. 'off' if neither is set."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT game_difficulty_override FROM groups WHERE chat_id=%s", (chat_id,))
        row = cursor.fetchone()
        override = row[0] if row else None
        if override:
            return override
        cursor.execute("SELECT value FROM bot_config WHERE key=%s", ("game_difficulty_global",))
        cfg_row = cursor.fetchone()
        return cfg_row[0] if cfg_row else "off"
    finally:
        conn.close()


def get_all_used_words_global():
    """Every word marked used in ANY chat (word-game used_words table), deduped."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT word FROM used_words")
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


def get_current_groups():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, title FROM groups WHERE left_at IS NULL")
        return cursor.fetchall()
    finally:
        conn.close()


def scramble_get_auto_mode(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT manual_streak, auto_enabled, miss_streak FROM scramble_auto_mode WHERE chat_id=%s",
            (chat_id,)
        )
        row = cursor.fetchone()
        if not row:
            return (0, False, 0)
        return (row[0], bool(row[1]), row[2])
    finally:
        conn.close()


def scramble_increment_manual_streak(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO scramble_auto_mode (chat_id, manual_streak, auto_enabled, miss_streak)
            VALUES (%s, 1, 0, 0)
            ON CONFLICT (chat_id) DO UPDATE SET manual_streak = scramble_auto_mode.manual_streak + 1
            RETURNING manual_streak
        """, (chat_id,))
        val = cursor.fetchone()[0]
        conn.commit()
        return val
    finally:
        conn.close()


def scramble_reset_manual_streak(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO scramble_auto_mode (chat_id, manual_streak, auto_enabled, miss_streak)
            VALUES (%s, 0, 0, 0)
            ON CONFLICT (chat_id) DO UPDATE SET manual_streak = 0
        """, (chat_id,))
        conn.commit()
    finally:
        conn.close()


def scramble_set_auto_enabled(chat_id, enabled: bool):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO scramble_auto_mode (chat_id, manual_streak, auto_enabled, miss_streak)
            VALUES (%s, 0, %s, 0)
            ON CONFLICT (chat_id) DO UPDATE SET auto_enabled=%s, manual_streak=0, miss_streak=0
        """, (chat_id, 1 if enabled else 0, 1 if enabled else 0))
        conn.commit()
    finally:
        conn.close()


def scramble_record_round_result(chat_id, won: bool):
    conn = connect()
    try:
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
        if not row:
            return None
        return bool(row[0]), row[1]
    finally:
        conn.close()


def scramble_increment_played(user_id, chat_id, name):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO scramble_chat_stats (user_id, chat_id, name, wins, earned, current_streak, best_streak, played)
            VALUES (%s, %s, %s, 0, 0, 0, 0, 1)
            ON CONFLICT (user_id, chat_id) DO UPDATE SET
                played = scramble_chat_stats.played + 1,
                name = EXCLUDED.name
        """, (user_id, chat_id, name))
        conn.commit()
    finally:
        conn.close()


def get_scramble_participants_count():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM scramble_chat_stats WHERE played > 0 OR wins > 0")
        return cursor.fetchone()[0]
    finally:
        conn.close()


def get_scramble_participants_page(offset, limit):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, chat_id, name, played, wins, earned
            FROM scramble_chat_stats
            WHERE played > 0 OR wins > 0
            ORDER BY played DESC, earned DESC
            LIMIT %s OFFSET %s
        """, (limit, offset))
        return cursor.fetchall()
    finally:
        conn.close()


def get_scramble_participant_detail(user_id, chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name, played, wins, earned FROM scramble_chat_stats WHERE user_id=%s AND chat_id=%s",
            (user_id, chat_id)
        )
        return cursor.fetchone()
    finally:
        conn.close()


def scramble_get_global_earned(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT earned FROM scramble_global_stats WHERE user_id=%s", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()

# --- Invite & Earn feature ---------------------------------------------
# Add the bot to a 200+ member group and keep it there 72 hours for a
# configurable Word Scramble reward. See main.py's on_bot_membership_change,
# start(), /claim, /invitereward and the "Invite & Earn" menu for the flow
# these functions support.

def has_rewarded_invite(chat_id) -> bool:
    """True if this chat_id already has a 'rewarded' row - the one permanent
    gate that blocks it from ever earning the invite reward again."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM invite_rewards WHERE chat_id=%s AND status='rewarded' LIMIT 1",
            (chat_id,)
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()


def create_pending_invite_reward(chat_id, group_title, inviter_id, inviter_name, member_count_at_add):
    """Creates a new 'pending' invite_rewards row for a fresh, qualifying add -
    UNLESS this chat_id already has a 'rewarded' row (permanent gate) or
    already has another still-'pending' row (duplicate-fire guard). Both
    checks and the insert happen in one atomic statement. Returns the new
    row's id, or None if nothing was inserted (either gate blocked it)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute("""
            INSERT INTO invite_rewards (chat_id, group_title, inviter_id, inviter_name, member_count_at_add, added_at, status)
            SELECT %s, %s, %s, %s, %s, %s, 'pending'
            WHERE NOT EXISTS (
                SELECT 1 FROM invite_rewards WHERE chat_id=%s AND status IN ('rewarded', 'pending')
            )
            RETURNING id
        """, (chat_id, group_title, inviter_id, inviter_name, member_count_at_add, now, chat_id))
        row = cursor.fetchone()
        conn.commit()
        return row[0] if row else None
    finally:
        conn.close()


def fail_pending_invite_reward_for_chat(chat_id):
    """Immediately marks any still-pending invite_rewards row for this
    chat_id as 'failed' (used when the bot is removed before the 72-hour
    check ever fires). Returns the row's id (so its scheduled job can be
    cancelled), or None if there was nothing pending."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE invite_rewards SET status='failed'
            WHERE chat_id=%s AND status='pending'
            RETURNING id
        """, (chat_id,))
        row = cursor.fetchone()
        conn.commit()
        return row[0] if row else None
    finally:
        conn.close()


def get_invite_reward(row_id):
    """Returns (id, chat_id, group_title, inviter_id, inviter_name,
    member_count_at_add, added_at, status, amount_paid, rewarded_at) or None."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, chat_id, group_title, inviter_id, inviter_name,
                   member_count_at_add, added_at, status, amount_paid, rewarded_at
            FROM invite_rewards WHERE id=%s
        """, (row_id,))
        return cursor.fetchone()
    finally:
        conn.close()


def mark_invite_reward_failed_by_id(row_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE invite_rewards SET status='failed' WHERE id=%s AND status='pending'",
            (row_id,)
        )
        conn.commit()
    finally:
        conn.close()


def mark_invite_reward_rewarded_by_id(row_id, amount_paid):
    conn = connect()
    try:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute("""
            UPDATE invite_rewards
            SET status='rewarded', amount_paid=%s, rewarded_at=%s
            WHERE id=%s AND status='pending'
        """, (amount_paid, now, row_id))
        conn.commit()
    finally:
        conn.close()


def update_invite_reward_inviter(row_id, inviter_id, inviter_name):
    """Referral attribution override (point 5) - only ever touches a row
    that's still 'pending', so it can never reattribute an already-resolved
    (rewarded/failed) row."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE invite_rewards SET inviter_id=%s, inviter_name=%s
            WHERE id=%s AND status='pending'
        """, (inviter_id, inviter_name, row_id))
        conn.commit()
    finally:
        conn.close()


def get_recent_pending_invite_reward_for_chat(chat_id, max_age_seconds=180):
    """Most recently-created still-pending invite_rewards row for this
    chat_id, but only if it was created within the last max_age_seconds -
    so a stray /start invite_<id> relay can never reattribute an old,
    unrelated pending row. Returns the row id, or None."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, added_at FROM invite_rewards
            WHERE chat_id=%s AND status='pending'
            ORDER BY id DESC LIMIT 1
        """, (chat_id,))
        row = cursor.fetchone()
        if not row:
            return None
        row_id, added_at = row
        try:
            added_dt = datetime.fromisoformat(added_at)
        except (TypeError, ValueError):
            return None
        if (datetime.now() - added_dt).total_seconds() > max_age_seconds:
            return None
        return row_id
    finally:
        conn.close()


def get_all_pending_invite_reward_ids_with_time():
    """(id, chat_id, added_at) for every still-'pending' row - used on
    startup to reschedule (or immediately run) each one's 72-hour payout
    check, mirroring restore_stale_scramble_rounds/_slowmode_waits/_word_games."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, chat_id, added_at FROM invite_rewards WHERE status='pending'")
        return cursor.fetchall()
    finally:
        conn.close()


def get_user_invite_rewards(user_id):
    """Returns (claimed, pending) for the 'My Invites' view, where claimed is
    a list of (group_title, amount_paid) and pending is a list of
    (group_title,) - both newest first."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT group_title, amount_paid FROM invite_rewards
            WHERE inviter_id=%s AND status='rewarded'
            ORDER BY rewarded_at DESC
        """, (user_id,))
        claimed = cursor.fetchall()
        cursor.execute("""
            SELECT group_title FROM invite_rewards
            WHERE inviter_id=%s AND status='pending'
            ORDER BY added_at DESC
        """, (user_id,))
        pending = cursor.fetchall()
        return claimed, pending
    finally:
        conn.close()


def is_group_currently_present(chat_id) -> bool:
    """True if the bot is currently (still) in this group, i.e. groups.left_at
    IS NULL for it. False if it's never been seen at all, or has left."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT left_at FROM groups WHERE chat_id=%s", (chat_id,))
        row = cursor.fetchone()
        return bool(row) and row[0] is None
    finally:
        conn.close()


def get_group_current_stint(chat_id):
    """Returns (title, current_stint_started_at, left_at) for /claim's
    72-hour-since-current-stint check, or None if the group has never been seen."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT title, current_stint_started_at, left_at FROM groups WHERE chat_id=%s",
            (chat_id,)
        )
        return cursor.fetchone()
    finally:
        conn.close()


def count_invite_rewards_for_chat(chat_id) -> int:
    """/claim is only for a chat_id with ZERO invite_rewards rows at all -
    any row (pending OR rewarded OR failed) means it's already been handled
    automatically at some point, or already paid."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM invite_rewards WHERE chat_id=%s", (chat_id,))
        return cursor.fetchone()[0]
    finally:
        conn.close()


def create_rewarded_invite_direct(chat_id, group_title, user_id, user_name, member_count, added_at_iso, amount, rewarded_at_iso):
    """/claim's manual-fallback payout: creates a row that's ALREADY
    'rewarded' directly, but only if this chat_id truly has zero rows at
    all - checked and inserted atomically in one statement so two players
    racing to run /claim in the same group can't both succeed. Returns the
    new row's id, or None if another row already existed (already handled
    automatically, already paid, or another /claim beat this one to it)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO invite_rewards
                (chat_id, group_title, inviter_id, inviter_name, member_count_at_add,
                 added_at, status, amount_paid, rewarded_at)
            SELECT %s, %s, %s, %s, %s, %s, 'rewarded', %s, %s
            WHERE NOT EXISTS (
                SELECT 1 FROM invite_rewards WHERE chat_id=%s
            )
            RETURNING id
        """, (chat_id, group_title, user_id, user_name, member_count,
              added_at_iso, amount, rewarded_at_iso, chat_id))
        row = cursor.fetchone()
        conn.commit()
        return row[0] if row else None
    finally:
        conn.close()