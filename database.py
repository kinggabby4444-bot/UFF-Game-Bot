"""
Shared PostgreSQL data-access layer for @Uff_GameBot (and @UFFAdsBot -
see the "shared tables" section at the bottom of create_tables()).
"""

import os
import re
import secrets
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta

import psycopg2
import psycopg2.pool
from dotenv import load_dotenv

load_dotenv()
DAILY_REWARD = 10
DAILY_REWARD_PREMIUM = 100   # unchanged

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


POOL_WAIT_SECONDS = float(os.environ.get("GAME_DB_POOL_WAIT", "20"))


def connect():
    """Check a connection out of the pool.

    Two failure modes used to surface here as exceptions that silently
    killed background chains (Automatic Mode, tournaments):
      * the pool was momentarily exhausted - ThreadedConnectionPool raises
        immediately instead of waiting, so we now wait for a free slot;
      * the pooled connection had gone stale (server/pooler closed it while
        idle) - it is now detected on checkout, thrown away, and replaced.
    """
    deadline = time.monotonic() + POOL_WAIT_SECONDS
    stale_replaced = 0
    while True:
        try:
            raw = _pool.getconn()
        except psycopg2.pool.PoolError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
            continue
        except psycopg2.Error:
            # Could not open a brand-new connection right now.
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)
            continue

        try:
            if raw.closed:
                raise psycopg2.InterfaceError("pooled connection is closed")
            # Set per-session instead of via options="-c statement_timeout=..."
            # at connection time - Neon's pooled endpoint rejects that as an
            # unsupported startup parameter. This also doubles as a cheap
            # liveness check for the connection we were just handed.
            raw.cursor().execute("SET statement_timeout = 15000")
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            # Dead/stale connection: discard it and take another.
            try:
                _pool.putconn(raw, close=True)
            except Exception:
                pass
            stale_replaced += 1
            if stale_replaced > 5 or time.monotonic() >= deadline:
                raise
            continue
        except Exception:
            # Anything else: don't block the caller on it (as before), but
            # clear any aborted-transaction state first.
            try:
                raw.rollback()
            except Exception:
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

        # /botusers: who has started the bot in DM, and who blocked/removed it.
        # The one-time backfill treats every already-registered user as having
        # started the bot (their real start time is unknown, so last_active is
        # used); it only runs the first time these columns are created.
        cursor.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'users' "
            "AND column_name = 'bot_started_at'"
        )
        _had_bot_started_col = cursor.fetchone() is not None
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_started_at TEXT")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_blocked_at TEXT")
        if not _had_bot_started_col:
            cursor.execute(
                "UPDATE users SET bot_started_at = COALESCE(last_active, %s) WHERE bot_started_at IS NULL",
                (datetime.now().isoformat(),),
            )

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
        # Which balance the entry fee was taken from ('scramble' or 'global'),
        # so a refund goes back to the same place. Old rows default to scramble.
        cursor.execute("ALTER TABLE tournament_players ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'scramble'")

        # ---- Elimination game (separate from tournaments) ------------------
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS eliminations (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            host_id BIGINT NOT NULL,
            fee BIGINT NOT NULL,
            difficulty TEXT,
            start_countdown INTEGER NOT NULL,
            current_countdown INTEGER NOT NULL,
            status TEXT DEFAULT 'lobby',
            cycle_no INTEGER DEFAULT 1,
            current_player_id BIGINT,
            current_word TEXT,
            current_scrambled TEXT,
            turn_started_at TEXT,
            turn_seconds INTEGER,
            created_at TEXT,
            lobby_message_id BIGINT,
            idle_since TEXT
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS elimination_players (
            id BIGSERIAL PRIMARY KEY,
            elimination_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            name TEXT,
            status TEXT DEFAULT 'staked',
            source TEXT DEFAULT 'scramble',
            play_pos INTEGER,
            last_cycle INTEGER DEFAULT 0,
            out_order INTEGER
        )
        """)
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_eliminations_one_open_per_chat "
            "ON eliminations (chat_id) WHERE status IN ('lobby', 'active')"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_elimination_players_unique "
            "ON elimination_players (elimination_id, user_id)"
        )

        # ---- Hack game (number-cracking) ------------------------------------
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS hacks (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            host_id BIGINT NOT NULL,
            fee BIGINT NOT NULL,
            digits INTEGER NOT NULL,
            guesses_each INTEGER NOT NULL,
            status TEXT DEFAULT 'lobby',
            secret TEXT,
            created_at TEXT,
            started_at TEXT,
            lobby_message_id BIGINT,
            last_activity TEXT,
            winner_id BIGINT
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS hack_players (
            id BIGSERIAL PRIMARY KEY,
            hack_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            name TEXT,
            status TEXT DEFAULT 'staked',
            source TEXT DEFAULT 'scramble',
            guesses_left INTEGER DEFAULT 0,
            guess_count INTEGER DEFAULT 0,
            no_guess_notified BOOLEAN DEFAULT FALSE
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS hack_last_secret (
            chat_id BIGINT PRIMARY KEY,
            secret TEXT
        )
        """)
        cursor.execute("ALTER TABLE hacks ADD COLUMN IF NOT EXISTS current_player_id BIGINT")
        cursor.execute("ALTER TABLE hacks ADD COLUMN IF NOT EXISTS turn_started_at TEXT")
        cursor.execute("ALTER TABLE hacks ADD COLUMN IF NOT EXISTS turn_seconds INTEGER")
        cursor.execute("ALTER TABLE hacks ADD COLUMN IF NOT EXISTS last_pos INTEGER DEFAULT 0")
        cursor.execute("ALTER TABLE hacks ADD COLUMN IF NOT EXISTS mode TEXT DEFAULT 'turn'")
        cursor.execute("ALTER TABLE hack_players ADD COLUMN IF NOT EXISTS play_pos INTEGER")
        cursor.execute("ALTER TABLE hack_players ADD COLUMN IF NOT EXISTS skips INTEGER DEFAULT 0")
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hacks_one_open_per_chat "
            "ON hacks (chat_id) WHERE status IN ('lobby', 'active')"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hack_players_unique "
            "ON hack_players (hack_id, user_id)"
        )

        # ---- Jackpot / Lucky Number (additive tables only) ------------------
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS jackpot_settings (
            chat_id BIGINT PRIMARY KEY,
            fee BIGINT NOT NULL,
            reward BIGINT NOT NULL,
            updated_at TEXT
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS jackpot_games (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            user_name TEXT,
            fee BIGINT NOT NULL,
            reward BIGINT NOT NULL,
            range_max INTEGER NOT NULL,
            secret INTEGER NOT NULL,
            source TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            guess INTEGER,
            message_id BIGINT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            settled_at TEXT
        )
        """)
        # One row per player per UTC day, across ALL groups. The PRIMARY KEY is
        # what makes "one attempt per day" race-proof: it is written in the same
        # transaction as the fee debit.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS jackpot_daily (
            user_id BIGINT NOT NULL,
            play_date TEXT NOT NULL,
            game_id BIGINT,
            PRIMARY KEY (user_id, play_date)
        )
        """)
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_jackpot_one_active_per_user "
            "ON jackpot_games (user_id) WHERE status = 'active'"
        )

        # ---- /support (Telegram Stars) -------------------------------------
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS support_payments (
            charge_id TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            name TEXT,
            stars INTEGER NOT NULL,
            paid_at TEXT
        )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_support_payments_user ON support_payments (user_id)")

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
    """Word Game + Word Scramble money (each counted only when positive) -
    exactly what fee checks and the combined leaderboard use."""
    return get_spendable_balance(user_id)


def get_spendable_balance(user_id):
    """What a player can actually spend on fees/transfers/requests: Word
    Scramble money + Word Game money (each counted only when positive). This is
    the exact figure _tx_deduct_combined checks, so a pre-check here can never
    disagree with the real debit."""
    conn = connect()
    try:
        cursor = conn.cursor()
        scramble, game = _tx_spendable(cursor, user_id)
        return scramble + game
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


def claim_daily_reward(user_id, name, username=None, premium=False):
    """Claim and credit the daily reward in ONE PostgreSQL transaction.

    The user row is locked before checking the 24-hour cooldown. The claim
    timestamp, Word Game credit, and audit-log row commit together, so a crash
    cannot consume a claim without paying it (or pay it without recording it).
    Concurrent /daily requests for the same user serialize on the users row.
    """
    conn = connect()
    try:
        cursor = conn.cursor()
        now = datetime.now()
        now_text = now.isoformat()

        # Normally /start has already created this row. Ensure it exists so a
        # missing users row cannot accidentally allow unlimited unrecorded
        # claims. ON CONFLICT does not overwrite an existing profile.
        cursor.execute(
            "INSERT INTO users (id, name, username, last_active) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (user_id, name, username, now_text),
        )
        cursor.execute(
            "SELECT last_daily_claim FROM users WHERE id=%s FOR UPDATE",
            (user_id,),
        )
        row = cursor.fetchone()
        last_claim = row[0] if row else None
        if last_claim:
            last_claim_dt = datetime.fromisoformat(last_claim)
            elapsed = now - last_claim_dt
            if elapsed < timedelta(hours=24):
                remaining = timedelta(hours=24) - elapsed
                conn.rollback()
                return False, remaining

        amount = int(
            get_config("reward_daily_premium", DAILY_REWARD_PREMIUM)
            if premium else get_config("reward_daily", DAILY_REWARD)
        )
        if amount < 0:
            raise ValueError("Daily reward amount cannot be negative")

        cursor.execute(
            "UPDATE users SET last_daily_claim=%s WHERE id=%s",
            (now_text, user_id),
        )
        # Daily reward belongs to the Word Game/global balance, as before.
        _tx_credit_global(cursor, user_id, name, amount)

        # Preserve the existing "earned in the last group" chat leaderboard
        # behavior, but keep it inside this same transaction as the global
        # credit. A DM claim is attributed to the user's last known group.
        reward_chat_id = _tx_chat_for_credit(cursor, user_id, None)
        if reward_chat_id is not None:
            cursor.execute("""
                INSERT INTO chat_scores (user_id, chat_id, name, coins)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, chat_id) DO UPDATE SET
                    coins = chat_scores.coins + EXCLUDED.coins,
                    name = EXCLUDED.name
            """, (user_id, reward_chat_id, name, amount))

        cursor.execute("""
            INSERT INTO bot_stats (key, value) VALUES ('total_rewards_given', %s)
            ON CONFLICT (key) DO UPDATE SET value = bot_stats.value + EXCLUDED.value
        """, (amount,))
        cursor.execute("""
            INSERT INTO daily_reward_log (user_id, name, username, amount, claim_date, claimed_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            user_id, name, username, amount,
            now.strftime("%Y-%m-%d"), now_text,
        ))
        balance = _tx_combined_total(cursor, user_id)
        conn.commit()
        return True, balance
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


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


# NOTE (egress): the *_position() helpers below used to SELECT the whole
# leaderboard table and scan it in Python to find one user's rank. The rank
# is now computed inside Postgres (ROW_NUMBER over the SAME ORDER BY, so ranks
# and tie-breaks are identical) and only that one row comes back.
def get_chat_position(chat_id, user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT rnk, name, coins FROM (
                SELECT user_id, name, coins,
                       ROW_NUMBER() OVER (ORDER BY coins DESC, user_id ASC) AS rnk
                FROM chat_scores WHERE chat_id=%s
            ) t WHERE user_id=%s
        """, (chat_id, user_id))
        row = cursor.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return int(row[0]), row[1], row[2]


def get_global_position(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT rnk, name, coins FROM (
                SELECT user_id, name, coins,
                       ROW_NUMBER() OVER (ORDER BY coins DESC, user_id ASC) AS rnk
                FROM global_scores
            ) t WHERE user_id=%s
        """, (user_id,))
        row = cursor.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return int(row[0]), row[1], row[2]


# ---------------------------------------------------------------------------
# Used-word history (Word Game: used_words, Word Scramble / Tournament /
# Elimination: scramble_used_words).
#
# Every word pick used to download the chat's ENTIRE used-word history
# (SELECT word ... WHERE chat_id=...) and filter it in Python. The history
# only ever changes through mark / reset below, so it is now kept in a
# bounded in-memory cache and the full download happens only on a cache
# miss (first pick after a restart, after eviction, after the TTL, or after
# any DB error). The database stays the single source of truth:
#   * every mark/reset writes to the DB FIRST; the cache is only touched if
#     that write succeeded, and is DROPPED (never trusted) if it failed or
#     its outcome is uncertain;
#   * a cache miss always reloads the exact set from the DB;
#   * entries expire after a TTL so a manual edit of the table in the
#     database (or a second bot instance during a redeploy overlap) can
#     never leave a chat with a permanently stale history.
# ---------------------------------------------------------------------------

_USED_WORDS_TABLES = {"wg": "used_words", "sc": "scramble_used_words"}
_USED_WORDS_CACHE_MAX_CHATS = 250          # per table; least-recently-used evicted
_USED_WORDS_CACHE_TTL_SECONDS = 6 * 3600   # forced reload from the DB after this

_used_words_cache = {kind: OrderedDict() for kind in _USED_WORDS_TABLES}
_used_words_cache_mu = threading.Lock()    # guards the OrderedDicts only (never held during I/O)
# Striped per-chat locks: bounded memory no matter how many chats exist.
# Reentrant, so word pickers can hold one across get -> choose -> mark.
_used_words_locks = tuple(threading.RLock() for _ in range(64))
_used_words_stats = {"hits": 0, "db_loads": 0}


def used_words_lock(kind, chat_id):
    """Lock that serialises pick/mark/reset for one chat's used-word history.
    kind: "wg" (Word Game) or "sc" (Scramble/Tournament/Elimination).
    Callers hold it across get_used_words -> choose -> mark_word_used so two
    simultaneous picks in the same chat can never choose the same word."""
    return _used_words_locks[hash((kind, chat_id)) % len(_used_words_locks)]


def _uw_cache_get(kind, chat_id):
    with _used_words_cache_mu:
        od = _used_words_cache[kind]
        entry = od.get(chat_id)
        if entry is None:
            return None
        loaded_at, words = entry
        if time.monotonic() - loaded_at > _USED_WORDS_CACHE_TTL_SECONDS:
            del od[chat_id]
            return None
        od.move_to_end(chat_id)
        return words


def _uw_cache_put(kind, chat_id, words):
    with _used_words_cache_mu:
        od = _used_words_cache[kind]
        od[chat_id] = (time.monotonic(), words)
        od.move_to_end(chat_id)
        while len(od) > _USED_WORDS_CACHE_MAX_CHATS:
            od.popitem(last=False)


def _uw_cache_drop(kind, chat_id):
    with _used_words_cache_mu:
        _used_words_cache[kind].pop(chat_id, None)


def invalidate_used_words_cache(chat_id=None):
    """Forget cached used-word history (one chat, or everything when chat_id
    is None). Safe to call at any time - the next pick just reloads from the
    database. Use after any out-of-band change to the used-word tables."""
    with _used_words_cache_mu:
        for od in _used_words_cache.values():
            if chat_id is None:
                od.clear()
            else:
                od.pop(chat_id, None)


def _uw_stat(name):
    with _used_words_cache_mu:
        _used_words_stats[name] += 1


def used_words_cache_stats():
    """In-process counters (reset on restart): cache hits vs. full DB loads,
    plus how many chats are cached. For verifying the cache is working."""
    with _used_words_cache_mu:
        return {
            "hits": _used_words_stats["hits"],
            "db_loads": _used_words_stats["db_loads"],
            "cached_chats": {k: len(v) for k, v in _used_words_cache.items()},
        }


def _used_words_get(kind, chat_id):
    table = _USED_WORDS_TABLES[kind]
    with used_words_lock(kind, chat_id):
        words = _uw_cache_get(kind, chat_id)
        if words is not None:
            _uw_stat("hits")
            return set(words)   # copy: callers can never mutate the cache
        conn = connect()
        try:
            cursor = conn.cursor()
            cursor.execute(f"SELECT word FROM {table} WHERE chat_id=%s", (chat_id,))
            loaded = {sys.intern(row[0]) for row in cursor.fetchall()}
        finally:
            conn.close()
        _uw_stat("db_loads")
        _uw_cache_put(kind, chat_id, loaded)
        return set(loaded)


def _used_words_mark(kind, chat_id, word):
    table = _USED_WORDS_TABLES[kind]
    with used_words_lock(kind, chat_id):
        try:
            conn = connect()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    f"INSERT INTO {table} (chat_id, word) VALUES (%s, %s) ON CONFLICT (chat_id, word) DO NOTHING",
                    (chat_id, word)
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            # The write may or may not have reached the DB (e.g. commit
            # raised after the server applied it) - don't trust the cache.
            _uw_cache_drop(kind, chat_id)
            raise
        cached = _uw_cache_get(kind, chat_id)
        if cached is not None:
            cached.add(sys.intern(word))


def _used_words_reset(kind, chat_id):
    table = _USED_WORDS_TABLES[kind]
    with used_words_lock(kind, chat_id):
        # Drop first: if anything below fails the cache is already gone.
        _uw_cache_drop(kind, chat_id)
        conn = connect()
        try:
            cursor = conn.cursor()
            cursor.execute(f"DELETE FROM {table} WHERE chat_id=%s", (chat_id,))
            conn.commit()
        finally:
            conn.close()
        # Dropped (not "set to empty") so the next pick re-reads the DB's
        # truth - a 0-row query, negligible egress.
        _uw_cache_drop(kind, chat_id)


def get_used_words(chat_id):
    return _used_words_get("wg", chat_id)


def mark_word_used(chat_id, word):
    _used_words_mark("wg", chat_id, word)


def reset_used_words(chat_id):
    _used_words_reset("wg", chat_id)


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
        # current_stint_started_at marks when the bot's CURRENT continuous
        # membership began. It is set on the first insert, and again only when
        # the bot is genuinely (re)added - i.e. the row was marked as left, or
        # never had a start time. register_group() is also called for EVERY
        # message the bot sees in a group; resetting the start time then is
        # what kept the /claim and Invite & Earn countdown stuck at full length.
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
                current_stint_started_at=CASE
                    WHEN groups.left_at IS NOT NULL OR groups.current_stint_started_at IS NULL
                    THEN EXCLUDED.current_stint_started_at
                    ELSE groups.current_stint_started_at
                END
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
        cursor.execute("SELECT COUNT(*) FROM groups WHERE chat_id < 0")
        total = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM groups WHERE chat_id < 0 AND left_at IS NULL")
        current = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM groups WHERE chat_id < 0 AND left_at IS NOT NULL")
        removed = cursor.fetchone()[0]
        return {"total": total, "current": current, "removed": removed}
    finally:
        conn.close()


def get_botgroups_count():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM groups WHERE chat_id < 0")
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
            WHERE g.chat_id < 0
            ORDER BY {order_clause}
            LIMIT %s OFFSET %s
        """, (limit, offset))
        return cursor.fetchall()
    finally:
        conn.close()


# --- /botusers: users who started the bot in DM (and who blocked it) ---------

def mark_user_bot_started(user_id):
    """Record that this user has the bot open in DM. Cheap no-op when already recorded."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET bot_started_at = COALESCE(bot_started_at, %s), bot_blocked_at = NULL "
            "WHERE id=%s AND (bot_started_at IS NULL OR bot_blocked_at IS NOT NULL)",
            (datetime.now().isoformat(), user_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_user_bot_blocked(user_id):
    """Record that this user blocked / removed the bot in DM."""
    conn = connect()
    try:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute(
            "UPDATE users SET bot_started_at = COALESCE(bot_started_at, %s), bot_blocked_at = %s WHERE id=%s",
            (now, now, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_bot_users_stats():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users WHERE bot_started_at IS NOT NULL")
        total = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE bot_started_at IS NOT NULL AND bot_blocked_at IS NULL")
        current = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE bot_started_at IS NOT NULL AND bot_blocked_at IS NOT NULL")
        removed = cursor.fetchone()[0]
        return {"total": total, "current": current, "removed": removed}
    finally:
        conn.close()


def get_botusers_count():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users WHERE bot_started_at IS NOT NULL")
        return cursor.fetchone()[0]
    finally:
        conn.close()


def get_botusers_page(offset, limit, sort="games"):
    conn = connect()
    try:
        cursor = conn.cursor()
        if sort == "time":
            order_clause = "u.bot_started_at ASC"
        else:
            order_clause = (
                "(u.bot_blocked_at IS NULL) DESC, "
                "(COALESCE(u.games_played, 0) + COALESCE(sc.played, 0)) DESC, u.last_active DESC"
            )
        cursor.execute(f"""
            SELECT u.id, u.name, COALESCE(u.games_played, 0), u.bot_blocked_at, COALESCE(sc.played, 0)
            FROM users u
            LEFT JOIN (
                SELECT user_id, SUM(played) AS played FROM scramble_chat_stats GROUP BY user_id
            ) sc ON sc.user_id = u.id
            WHERE u.bot_started_at IS NOT NULL
            ORDER BY {order_clause}
            LIMIT %s OFFSET %s
        """, (limit, offset))
        return cursor.fetchall()
    finally:
        conn.close()


def get_bot_user_full_details(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.id, u.name, u.username, COALESCE(u.games_played, 0), u.bot_started_at,
                   u.bot_blocked_at, COALESCE(sc.played, 0), u.last_active, COALESCE(g.coins, 0)
            FROM users u
            LEFT JOIN (
                SELECT user_id, SUM(played) AS played FROM scramble_chat_stats GROUP BY user_id
            ) sc ON sc.user_id = u.id
            LEFT JOIN global_scores g ON g.user_id = u.id
            WHERE u.id=%s AND u.bot_started_at IS NOT NULL
        """, (user_id,))
        return cursor.fetchone()
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


def _tx_deduct_global(cursor, user_id, name, amount):
    """Same as _tx_deduct but against the Word Game (global_scores) balance."""
    cursor.execute("""
        UPDATE global_scores
        SET coins = coins - %s, name = %s
        WHERE user_id = %s AND coins >= %s
        RETURNING coins
    """, (amount, name, user_id, amount))
    row = cursor.fetchone()
    return row[0] if row else None


def _tx_credit_global(cursor, user_id, name, amount):
    cursor.execute("""
        INSERT INTO global_scores (user_id, name, coins)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            coins = global_scores.coins + EXCLUDED.coins,
            name = EXCLUDED.name
        RETURNING coins
    """, (user_id, name, amount))
    return cursor.fetchone()[0]


def _tx_spendable(cursor, user_id, lock=False):
    """(scramble, word_game) spendable amounts for one player, each clamped at 0
    (a Word Game balance can dip below zero from cheat penalties; that never
    counts as spendable money). lock=True takes row locks - always scramble
    row first, then word-game row, so concurrent transactions can't deadlock."""
    suffix = " FOR UPDATE" if lock else ""
    cursor.execute("SELECT earned FROM scramble_global_stats WHERE user_id=%s" + suffix, (user_id,))
    row = cursor.fetchone()
    scramble = max((row[0] or 0), 0) if row else 0
    cursor.execute("SELECT coins FROM global_scores WHERE user_id=%s" + suffix, (user_id,))
    row = cursor.fetchone()
    game = max((row[0] or 0), 0) if row else 0
    return scramble, game


def _tx_deduct_combined(cursor, user_id, name, amount):
    """THE one fee/debit helper for every paid action (Tournament, Elimination,
    Hack joins, /transfer, /request, Jackpot...). Spends from the COMBINED
    Word Scramble + Word Game balance: Word Scramble money first, then the
    remainder from Word Game money (e.g. fee 1000 with 300 Scramble and 900
    Word Game -> 300 + 700, leaving 200 Word Game).
    All-or-nothing: if the combined balance can't cover `amount`, NOTHING is
    changed. Must run inside the caller's open transaction.
    Returns (combined_balance_after, source) or (None, None) when insufficient.
    `source` records the split so a refund restores exactly what was taken:
    'scramble' | 'global' | 'split:<scramble>:<word game>'."""
    if amount < 0:
        raise ValueError("amount must not be negative")
    scramble, game = _tx_spendable(cursor, user_id, lock=True)
    if scramble + game < amount:
        return None, None
    take_scramble = min(scramble, amount)
    take_game = amount - take_scramble
    if take_scramble:
        cursor.execute(
            "UPDATE scramble_global_stats SET earned = earned - %s, name = %s WHERE user_id = %s",
            (take_scramble, name, user_id),
        )
    if take_game:
        cursor.execute(
            "UPDATE global_scores SET coins = coins - %s, name = %s WHERE user_id = %s",
            (take_game, name, user_id),
        )
    if take_game == 0:
        source = "scramble"
    elif take_scramble == 0:
        source = "global"
    else:
        source = f"split:{take_scramble}:{take_game}"
    return scramble + game - amount, source


def _tx_deduct_any(cursor, user_id, name, amount):
    """Kept under its old name for the existing join paths: now a combined
    (split-capable) deduction - see _tx_deduct_combined."""
    return _tx_deduct_combined(cursor, user_id, name, amount)


def _tx_combined_total(cursor, user_id):
    scramble, game = _tx_spendable(cursor, user_id)
    return scramble + game


def _tx_credit_source(cursor, user_id, name, amount, source):
    """Refund helper: puts money back exactly where it was taken from. Handles
    the legacy single-source values ('scramble'/'global') and split sources.
    Returns the player's combined balance afterwards."""
    src = source or "scramble"
    if src.startswith("split:"):
        try:
            _, part_scramble, part_game = src.split(":")
            part_scramble, part_game = int(part_scramble), int(part_game)
        except ValueError:
            part_scramble, part_game = amount, 0   # unreadable marker: refund to Scramble
        if part_scramble:
            _tx_credit(cursor, user_id, name, part_scramble)
        if part_game:
            _tx_credit_global(cursor, user_id, name, part_game)
    elif src == "global":
        _tx_credit_global(cursor, user_id, name, amount)
    else:
        _tx_credit(cursor, user_id, name, amount)
    return _tx_combined_total(cursor, user_id)


def _tx_chat_for_credit(cursor, user_id, chat_id):
    """The chat a received amount is recorded under: the group it happened in;
    for a DM (positive chat id) the player's last group chat, or None."""
    if chat_id is not None and chat_id < 0:
        return chat_id
    cursor.execute("SELECT last_chat_id FROM users WHERE id=%s", (user_id,))
    row = cursor.fetchone()
    last = row[0] if row and row[0] else None
    return last if last is not None and last < 0 else None


def _tx_credit_chat_scramble(cursor, user_id, chat_id, name, amount):
    """Registers winnings for ONE group's 'in this chat' Word Scramble board
    (scramble_chat_stats) only - the global balance is credited separately by
    the caller, so nothing is double counted."""
    cursor.execute("""
        INSERT INTO scramble_chat_stats
            (user_id, chat_id, name, wins, earned, current_streak, best_streak)
        VALUES (%s, %s, %s, 0, GREATEST(0, %s), 0, 0)
        ON CONFLICT (user_id, chat_id) DO UPDATE SET
            earned = GREATEST(0, scramble_chat_stats.earned + %s),
            name = EXCLUDED.name
    """, (user_id, chat_id, name, amount, amount))


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
    """Returns ('ok', tournament_id, None) | ('exists', None, None).
    The lobby opens EMPTY: the host is not joined and is not charged - they
    join with /join like everyone else."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            new_balance = None

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

            new_balance, source = _tx_deduct_any(cursor, user_id, name, fee)
            if new_balance is None:
                conn.rollback()
                return "insufficient", None

            cursor.execute("""
                INSERT INTO tournament_players (tournament_id, user_id, name, score, status, source)
                VALUES (%s, %s, %s, 0, 'staked', %s)
            """, (tournament_id, user_id, name, source))
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
                SELECT user_id, name, score, COALESCE(source, 'scramble') FROM tournament_players
                WHERE tournament_id=%s AND status='staked' ORDER BY id ASC
            """, (tournament_id,))
            players = cursor.fetchall()
            _sources = {p[0]: p[3] for p in players}
            players = [(p[0], p[1], p[2]) for p in players]
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
                        refunds.append((uid, name, fee, _tx_credit_source(cursor, uid, name, fee, _sources.get(uid))))
            else:
                top_players = [p for p in players if p[2] == top]  # already in join order
                case = "single" if len(top_players) == 1 else "tie"
                share, remainder = divmod(pot, len(top_players))
                for i, (uid, name, _score) in enumerate(top_players):
                    payout = share + (remainder if i == 0 else 0)  # remainder -> earliest joiner
                    if _mark(uid, "won"):
                        winners.append((uid, name, payout, _tx_credit(cursor, uid, name, payout)))
                        # Winnings also count for THIS group's "in this chat" board.
                        _tx_credit_chat_scramble(cursor, uid, chat_id, name, payout)
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
                SELECT user_id, name, COALESCE(source, 'scramble') FROM tournament_players
                WHERE tournament_id=%s AND status='staked' ORDER BY id ASC
            """, (tournament_id,))
            refunds = []
            for uid, name, source in cursor.fetchall():
                cursor.execute("""
                    UPDATE tournament_players SET status='refunded'
                    WHERE tournament_id=%s AND user_id=%s AND status='staked'
                """, (tournament_id, uid))
                if cursor.rowcount == 1:
                    refunds.append((uid, name, fee, _tx_credit_source(cursor, uid, name, fee, source)))

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
    """Locks the existing balance rows (Word Scramble, then Word Game) for
    these users in a fixed order, so concurrent transfers/requests between the
    same players can never deadlock or interleave."""
    ids = sorted(set(user_ids))
    cursor.execute(
        "SELECT user_id FROM scramble_global_stats WHERE user_id = ANY(%s) "
        "ORDER BY user_id FOR UPDATE",
        (ids,),
    )
    cursor.fetchall()
    cursor.execute(
        "SELECT user_id FROM global_scores WHERE user_id = ANY(%s) "
        "ORDER BY user_id FOR UPDATE",
        (ids,),
    )
    cursor.fetchall()


def _tx_audit(cursor, kind, from_id, to_id, amount, chat_id):
    cursor.execute("""
        INSERT INTO balance_transfers (kind, from_id, to_id, amount, chat_id, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (kind, from_id, to_id, amount, chat_id, datetime.now().isoformat()))


def transfer_execute(from_id, from_name, to_id, to_name, amount, chat_id):
    """Moves `amount` between players in ONE transaction: all-or-nothing
    combined debit of the sender (Word Scramble first, then Word Game), credit
    of the recipient's Word Scramble balance AND of the selected chat's
    "in this chat" record (so their previous balance in that chat + the
    transfer is what that chat shows), plus an audit row.
    Returns ('ok', sender_combined_after, recipient_combined_after) or
    ('insufficient', sender_combined_balance, None)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            _tx_lock_balances(cursor, [from_id, to_id])
            new_from, _src = _tx_deduct_combined(cursor, from_id, from_name, amount)
            if new_from is None:
                have = _tx_combined_total(cursor, from_id)
                conn.rollback()
                return "insufficient", have, None
            _tx_credit(cursor, to_id, to_name, amount)
            credit_chat = _tx_chat_for_credit(cursor, to_id, chat_id)
            if credit_chat is not None:
                _tx_credit_chat_scramble(cursor, to_id, credit_chat, to_name, amount)
            new_to = _tx_combined_total(cursor, to_id)
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
            target_bal, _src = _tx_deduct_combined(cursor, row["target_id"], row["target_name"], row["amount"])
            if target_bal is None:
                have = _tx_combined_total(cursor, row["target_id"])
                conn.rollback()
                return "insufficient", row, have
            _tx_credit(cursor, row["requester_id"], row["requester_name"], row["amount"])
            credit_chat = _tx_chat_for_credit(cursor, row["requester_id"], row["chat_id"])
            if credit_chat is not None:
                _tx_credit_chat_scramble(cursor, row["requester_id"], credit_chat, row["requester_name"], row["amount"])
            req_bal = _tx_combined_total(cursor, row["requester_id"])
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


# Rewards can be overridden at runtime (bot_config keys written by
# /setreward and /scramblesetreward). Changing the code defaults alone would
# not affect a database that already holds such a stored value, so this
# runs ONCE (guarded by a marker row) to bring any stored value in line with
# the new rules. Anything the owner sets afterwards is never touched again.
_REWARD_DEFAULTS_V1 = (("reward_scramble_win", 1), ("reward_daily", 10))
_REWARD_DEFAULTS_V1_MARKER = "migration_reward_defaults_v1"


def apply_reward_defaults_v1():
    """Returns the list of (key, old, new) values that were changed."""
    changed = []
    if get_config(_REWARD_DEFAULTS_V1_MARKER) is not None:
        return changed
    for key, new_value in _REWARD_DEFAULTS_V1:
        old = get_config(key)
        if old is not None and str(old) != str(new_value):
            set_config(key, new_value)
            changed.append((key, old, new_value))
    set_config(_REWARD_DEFAULTS_V1_MARKER, "1")
    return changed


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
    return _used_words_get("sc", chat_id)


def scramble_mark_word_used(chat_id, word):
    _used_words_mark("sc", chat_id, word)


def scramble_reset_used_words(chat_id):
    _used_words_reset("sc", chat_id)


def scramble_record_win(user_id, chat_id, name, reward_amount):
    """Record a Word Scramble win. Win streaks were removed: current_streak /
    best_streak are no longer read or changed (existing values are left as-is)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO scramble_chat_stats (user_id, chat_id, name, wins, earned, current_streak, best_streak)
            VALUES (%s, %s, %s, 1, %s, 0, 0)
            ON CONFLICT (user_id, chat_id) DO UPDATE SET
                name = EXCLUDED.name,
                wins = scramble_chat_stats.wins + 1,
                earned = scramble_chat_stats.earned + %s
        """, (user_id, chat_id, name, reward_amount, reward_amount))

        cursor.execute("""
            INSERT INTO scramble_global_stats (user_id, name, wins, earned)
            VALUES (%s, %s, 1, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                name = EXCLUDED.name,
                wins = scramble_global_stats.wins + 1,
                earned = scramble_global_stats.earned + %s
        """, (user_id, name, reward_amount, reward_amount))
        conn.commit()
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
            SELECT rnk, name, wins, earned, current_streak FROM (
                SELECT user_id, name, wins, earned, current_streak,
                       ROW_NUMBER() OVER (ORDER BY earned DESC, user_id ASC) AS rnk
                FROM scramble_chat_stats WHERE chat_id=%s
            ) t WHERE user_id=%s
        """, (chat_id, user_id))
        row = cursor.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return int(row[0]), row[1], row[2], row[3], row[4]


def scramble_get_global_position(user_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT rnk, name, wins, earned FROM (
                SELECT user_id, name, wins, earned,
                       ROW_NUMBER() OVER (ORDER BY earned DESC, user_id ASC) AS rnk
                FROM scramble_global_stats
            ) t WHERE user_id=%s
        """, (user_id,))
        row = cursor.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return int(row[0]), row[1], row[2], row[3]


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


# THE combined-balance definition, used by fee checks (_tx_spendable), by
# get_combined_balance, and by the leaderboard/rank queries below:
#   Word Scramble money + Word Game money, each counted only when positive.
_COMBINED_TOTAL_SQL = "GREATEST(COALESCE(g.coins, 0), 0) + GREATEST(COALESCE(s.earned, 0), 0)"


def get_combined_global_leaderboard(limit=10):
    """Global leaderboard by COMBINED balance: Word Game money (global_scores)
    + Word Scramble money (scramble_global_stats) per player - the same
    definition the fee checks use. Ties are broken by user id (same as ranks)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT COALESCE(g.name, s.name) AS name,
                   {_COMBINED_TOTAL_SQL} AS total
            FROM global_scores g
            FULL OUTER JOIN scramble_global_stats s ON g.user_id = s.user_id
            ORDER BY {_COMBINED_TOTAL_SQL} DESC, COALESCE(g.user_id, s.user_id) ASC
            LIMIT %s
        """, (limit,))
        return cursor.fetchall()
    finally:
        conn.close()


def get_combined_global_position(user_id):
    # Called for the top-3 medal on every correct answer, so it must not
    # download the whole player table: rank is computed in Postgres with the
    # same ordering (total DESC, user_id ASC) and one row is returned.
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT rnk, name, total FROM (
                SELECT COALESCE(g.user_id, s.user_id) AS user_id,
                       COALESCE(g.name, s.name) AS name,
                       {_COMBINED_TOTAL_SQL} AS total,
                       ROW_NUMBER() OVER (
                           ORDER BY {_COMBINED_TOTAL_SQL} DESC,
                                    COALESCE(g.user_id, s.user_id) ASC
                       ) AS rnk
                FROM global_scores g
                FULL OUTER JOIN scramble_global_stats s ON g.user_id = s.user_id
            ) t WHERE user_id=%s
        """, (user_id,))
        row = cursor.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return int(row[0]), row[1], row[2]


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


def clear_all_group_game_difficulty_overrides() -> int:
    """Used by a global /gamedifficulty so the new setting reaches EVERY group.
    Returns how many groups had their own override cleared."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE groups SET game_difficulty_override=NULL WHERE game_difficulty_override IS NOT NULL")
        cleared = cursor.rowcount
        conn.commit()
        return cleared
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


def scramble_get_auto_enabled_chats():
    """Chat ids that currently have Automatic Mode switched on."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM scramble_auto_mode WHERE auto_enabled=1")
        return [r[0] for r in cursor.fetchall()]
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

# =======================================================================
# ELIMINATION (word-scramble elimination game)
# Same safety model as the tournament: every money movement happens in the
# same transaction as the status change that guards it.
# =======================================================================

_ELIM_COLS = (
    "id, chat_id, host_id, fee, difficulty, start_countdown, current_countdown, status, "
    "cycle_no, current_player_id, current_word, current_scrambled, turn_started_at, "
    "turn_seconds, created_at, lobby_message_id, idle_since"
)
_ELIM_KEYS = [c.strip() for c in _ELIM_COLS.split(",")]


def _elim_row(row):
    return dict(zip(_ELIM_KEYS, row)) if row else None


def elim_create(chat_id, host_id, fee, difficulty, countdown):
    """Returns ('ok', elimination_id) | ('exists', None). The lobby opens empty."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO eliminations (chat_id, host_id, fee, difficulty, start_countdown,
                                          current_countdown, status, cycle_no, created_at)
                SELECT %s, %s, %s, %s, %s, %s, 'lobby', 1, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM eliminations WHERE chat_id=%s AND status IN ('lobby', 'active')
                )
                RETURNING id
            """, (chat_id, host_id, fee, difficulty, countdown, countdown,
                  datetime.now().isoformat(), chat_id))
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return "exists", None
            conn.commit()
            return "ok", row[0]
        except Exception as e:
            conn.rollback()
            if getattr(e, "pgcode", None) == "23505":
                return "exists", None
            raise
    finally:
        conn.close()


def elim_get(elimination_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT {_ELIM_COLS} FROM eliminations WHERE id=%s", (elimination_id,))
        return _elim_row(cursor.fetchone())
    finally:
        conn.close()


def elim_get_open_for_chat(chat_id):
    """The chat's lobby/active elimination (at most one), or None."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {_ELIM_COLS} FROM eliminations WHERE chat_id=%s AND status IN ('lobby', 'active')",
            (chat_id,),
        )
        return _elim_row(cursor.fetchone())
    finally:
        conn.close()


def elim_get_all_open():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT {_ELIM_COLS} FROM eliminations WHERE status IN ('lobby', 'active')")
        return [_elim_row(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def elim_get_players(elimination_id):
    """Join order. Rows: (user_id, name, status, play_pos, last_cycle)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, name, status, play_pos, last_cycle FROM elimination_players
            WHERE elimination_id=%s ORDER BY id ASC
        """, (elimination_id,))
        return cursor.fetchall()
    finally:
        conn.close()


def elim_set_lobby_message(elimination_id, message_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE eliminations SET lobby_message_id=%s WHERE id=%s", (message_id, elimination_id))
        conn.commit()
    finally:
        conn.close()


def elim_join(elimination_id, user_id, name):
    """Returns ('ok', source, player_count) | ('closed', None, None)
    | ('already', None, None) | ('insufficient', None, None). The fee comes
    from whichever balance covers it (Scramble first, then Word Game)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT status, fee FROM eliminations WHERE id=%s FOR UPDATE", (elimination_id,))
            row = cursor.fetchone()
            if not row or row[0] != "lobby":
                conn.rollback()
                return "closed", None, None
            fee = row[1]

            cursor.execute(
                "SELECT 1 FROM elimination_players WHERE elimination_id=%s AND user_id=%s",
                (elimination_id, user_id),
            )
            if cursor.fetchone():
                conn.rollback()
                return "already", None, None

            new_balance, source = _tx_deduct_any(cursor, user_id, name, fee)
            if new_balance is None:
                conn.rollback()
                return "insufficient", None, None

            cursor.execute("""
                INSERT INTO elimination_players (elimination_id, user_id, name, status, source, last_cycle)
                VALUES (%s, %s, %s, 'staked', %s, 0)
            """, (elimination_id, user_id, name, source))
            cursor.execute(
                "SELECT COUNT(*) FROM elimination_players WHERE elimination_id=%s", (elimination_id,)
            )
            count = cursor.fetchone()[0]
            conn.commit()
            return "ok", source, count
        except Exception as e:
            conn.rollback()
            if getattr(e, "pgcode", None) == "23505":
                return "already", None, None
            raise
    finally:
        conn.close()


def elim_activate(elimination_id, ordered_user_ids):
    """lobby -> active, exactly once. `ordered_user_ids` is the (already
    shuffled) play order, stored as play_pos. Needs 2+ staked players."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT status FROM eliminations WHERE id=%s FOR UPDATE", (elimination_id,))
            row = cursor.fetchone()
            if not row or row[0] != "lobby":
                conn.rollback()
                return False
            cursor.execute(
                "SELECT COUNT(*) FROM elimination_players WHERE elimination_id=%s AND status='staked'",
                (elimination_id,),
            )
            if cursor.fetchone()[0] < 2:
                conn.rollback()
                return False
            for pos, uid in enumerate(ordered_user_ids, start=1):
                cursor.execute("""
                    UPDATE elimination_players SET play_pos=%s, last_cycle=0
                    WHERE elimination_id=%s AND user_id=%s AND status='staked'
                """, (pos, elimination_id, uid))
            cursor.execute("""
                UPDATE eliminations
                SET status='active', cycle_no=1, current_countdown=start_countdown,
                    current_word=NULL, current_scrambled=NULL, current_player_id=NULL,
                    idle_since=%s
                WHERE id=%s
            """, (datetime.now().isoformat(), elimination_id))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def elim_next_turn(elimination_id):
    """Decides who plays next. Every alive player gets one turn per cycle (in
    the fixed shuffled order) BEFORE the countdown drops.
    Returns one of:
      {'kind': 'none'}  - not an active game
      {'kind': 'busy'}  - a turn is already live
      {'kind': 'over'}  - 1 (or 0) players left
      {'kind': 'turn', 'uid', 'name', 'countdown', 'cycle_no',
       'cycle_changed': bool, 'prev_countdown': int|None}"""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT status, cycle_no, current_countdown, current_word
                FROM eliminations WHERE id=%s FOR UPDATE
            """, (elimination_id,))
            row = cursor.fetchone()
            if not row or row[0] != "active":
                conn.rollback()
                return {"kind": "none"}
            _status, cycle_no, countdown, current_word = row
            if current_word is not None:
                conn.rollback()
                return {"kind": "busy"}

            cursor.execute("""
                SELECT user_id, name, last_cycle FROM elimination_players
                WHERE elimination_id=%s AND status='staked' ORDER BY play_pos ASC, id ASC
            """, (elimination_id,))
            alive = cursor.fetchall()
            if len(alive) <= 1:
                conn.rollback()
                return {"kind": "over"}

            nxt = next((a for a in alive if (a[2] or 0) < cycle_no), None)
            cycle_changed, prev = False, None
            if nxt is None:
                prev = countdown
                countdown = max(2, (prev + 1) // 2)  # halves round UP, floor of 2s
                cycle_no += 1
                cycle_changed = True
                cursor.execute(
                    "UPDATE eliminations SET current_countdown=%s, cycle_no=%s WHERE id=%s",
                    (countdown, cycle_no, elimination_id),
                )
                nxt = alive[0]
            conn.commit()
            return {"kind": "turn", "uid": nxt[0], "name": nxt[1], "countdown": countdown,
                    "cycle_no": cycle_no, "cycle_changed": cycle_changed, "prev_countdown": prev}
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def elim_set_turn(elimination_id, user_id, word, scrambled, seconds):
    """Starts the live turn. Returns the turn_started_at string, or None if the
    game isn't active / a turn is already live."""
    now = datetime.now().isoformat()
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE eliminations
                SET current_player_id=%s, current_word=%s, current_scrambled=%s,
                    turn_started_at=%s, turn_seconds=%s
                WHERE id=%s AND status='active' AND current_word IS NULL
                RETURNING cycle_no
            """, (user_id, word, scrambled, now, seconds, elimination_id))
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return None
            cursor.execute("""
                UPDATE elimination_players SET last_cycle=%s
                WHERE elimination_id=%s AND user_id=%s
            """, (row[0], elimination_id, user_id))
            conn.commit()
            return now
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def elim_unset_turn(elimination_id, user_id):
    """Rolls a turn back (e.g. the word message could not be delivered) so the
    player is not skipped for this cycle."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE eliminations
            SET current_word=NULL, current_scrambled=NULL, current_player_id=NULL,
                idle_since=%s
            WHERE id=%s AND status='active'
        """, (datetime.now().isoformat(), elimination_id))
        cursor.execute("""
            UPDATE elimination_players SET last_cycle=GREATEST(0, COALESCE(last_cycle, 0) - 1)
            WHERE elimination_id=%s AND user_id=%s
        """, (elimination_id, user_id))
        conn.commit()
    finally:
        conn.close()


def elim_resolve_turn(elimination_id, correct, expected_uid=None, expected_started=None):
    """Atomically closes the live turn - exactly one caller (correct guess OR
    the timeout) can ever win this. A wrong/timed-out player becomes 'out'.
    Returns {'word', 'uid', 'name', 'alive': [(uid, name)], 'cycle_no'} or None
    if there was no live turn (or it was already closed / belongs to someone
    else / is a newer turn than the timer that fired)."""
    now = datetime.now().isoformat()
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT status, current_player_id, current_word, cycle_no, turn_started_at
                FROM eliminations WHERE id=%s FOR UPDATE
            """, (elimination_id,))
            row = cursor.fetchone()
            if not row or row[0] != "active" or row[2] is None:
                conn.rollback()
                return None
            _status, uid, word, cycle_no, started = row
            if expected_uid is not None and uid != expected_uid:
                conn.rollback()
                return None
            if expected_started is not None and started != expected_started:
                conn.rollback()
                return None

            cursor.execute("""
                UPDATE eliminations
                SET current_word=NULL, current_scrambled=NULL, current_player_id=NULL, idle_since=%s
                WHERE id=%s
            """, (now, elimination_id))
            cursor.execute(
                "SELECT name FROM elimination_players WHERE elimination_id=%s AND user_id=%s",
                (elimination_id, uid),
            )
            nrow = cursor.fetchone()
            name = nrow[0] if nrow else str(uid)
            if not correct:
                cursor.execute(
                    "SELECT COALESCE(MAX(out_order), 0) + 1 FROM elimination_players WHERE elimination_id=%s",
                    (elimination_id,),
                )
                order = cursor.fetchone()[0]
                cursor.execute("""
                    UPDATE elimination_players SET status='out', out_order=%s
                    WHERE elimination_id=%s AND user_id=%s AND status='staked'
                """, (order, elimination_id, uid))
            cursor.execute("""
                SELECT user_id, name FROM elimination_players
                WHERE elimination_id=%s AND status='staked' ORDER BY play_pos ASC, id ASC
            """, (elimination_id,))
            alive = cursor.fetchall()
            conn.commit()
            return {"word": word, "uid": uid, "name": name, "alive": alive, "cycle_no": cycle_no}
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def elim_restart_turn_clock(elimination_id):
    """Gives the live turn a fresh full countdown WITHOUT changing the word or
    the player (used after a restart / by /resumeelimination). Returns the new
    turn_started_at or None if there is no live turn."""
    now = datetime.now().isoformat()
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE eliminations SET turn_started_at=%s
            WHERE id=%s AND status='active' AND current_word IS NOT NULL
            RETURNING id
        """, (now, elimination_id))
        ok = cursor.fetchone() is not None
        conn.commit()
        return now if ok else None
    finally:
        conn.close()


def elim_mark_idle(elimination_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE eliminations SET idle_since=%s WHERE id=%s AND status='active'",
            (datetime.now().isoformat(), elimination_id),
        )
        conn.commit()
    finally:
        conn.close()


def elim_settle(elimination_id):
    """Ends an ACTIVE game with <=1 player left and pays the whole prize pool
    (fee x (players + 1): the bonus is the bot's, not a player's money) to the
    winner. If nobody is left alive, the LAST player eliminated wins.
    Idempotent. Returns {chat_id, fee, pot, winner: (uid, name), balance} or None."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT status, fee, chat_id FROM eliminations WHERE id=%s FOR UPDATE", (elimination_id,)
            )
            row = cursor.fetchone()
            if not row or row[0] != "active":
                conn.rollback()
                return None
            _status, fee, chat_id = row

            cursor.execute("""
                SELECT user_id, name FROM elimination_players
                WHERE elimination_id=%s AND status='staked' ORDER BY play_pos ASC, id ASC
            """, (elimination_id,))
            alive = cursor.fetchall()
            if len(alive) >= 2:
                conn.rollback()
                return None

            cursor.execute("""
                SELECT COUNT(*) FROM elimination_players
                WHERE elimination_id=%s AND status IN ('staked', 'out')
            """, (elimination_id,))
            total = cursor.fetchone()[0]
            pot = fee * (total + 1)

            if alive:
                winner = alive[0]
            else:
                cursor.execute("""
                    SELECT user_id, name FROM elimination_players
                    WHERE elimination_id=%s AND status='out'
                    ORDER BY out_order DESC NULLS LAST LIMIT 1
                """, (elimination_id,))
                winner = cursor.fetchone()
                if not winner:
                    cursor.execute("UPDATE eliminations SET status='finished' WHERE id=%s", (elimination_id,))
                    conn.commit()
                    return None

            uid, name = winner
            cursor.execute("""
                UPDATE elimination_players SET status='won'
                WHERE elimination_id=%s AND user_id=%s
            """, (elimination_id, uid))
            balance = _tx_credit(cursor, uid, name, pot)
            _tx_credit_chat_scramble(cursor, uid, chat_id, name, pot)
            cursor.execute("""
                UPDATE eliminations
                SET status='finished', current_word=NULL, current_scrambled=NULL, current_player_id=NULL
                WHERE id=%s
            """, (elimination_id,))
            conn.commit()
            return {"chat_id": chat_id, "fee": fee, "pot": pot, "winner": (uid, name), "balance": balance}
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def elim_cancel(elimination_id):
    """Cancels a lobby/active game and refunds EVERY player's join fee (even
    those already eliminated) to the balance it came from. The first-player
    bonus was never real money, so it simply disappears. Idempotent.
    Returns {chat_id, fee, refunds: [(uid, name, fee, new_balance)]} or None."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT status, fee, chat_id FROM eliminations WHERE id=%s FOR UPDATE", (elimination_id,)
            )
            row = cursor.fetchone()
            if not row or row[0] not in ("lobby", "active"):
                conn.rollback()
                return None
            _status, fee, chat_id = row

            cursor.execute("""
                SELECT user_id, name, COALESCE(source, 'scramble') FROM elimination_players
                WHERE elimination_id=%s AND status IN ('staked', 'out') ORDER BY id ASC
            """, (elimination_id,))
            refunds = []
            for uid, name, source in cursor.fetchall():
                cursor.execute("""
                    UPDATE elimination_players SET status='refunded'
                    WHERE elimination_id=%s AND user_id=%s AND status IN ('staked', 'out')
                """, (elimination_id, uid))
                if cursor.rowcount == 1:
                    refunds.append((uid, name, fee, _tx_credit_source(cursor, uid, name, fee, source)))

            cursor.execute("""
                UPDATE eliminations
                SET status='cancelled', current_word=NULL, current_scrambled=NULL, current_player_id=NULL
                WHERE id=%s
            """, (elimination_id,))
            conn.commit()
            return {"chat_id": chat_id, "fee": fee, "refunds": refunds}
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


# =======================================================================
# HACK (number-cracking game)
# Same safety model as the elimination: every money movement happens in the
# same transaction as the status change that guards it.
# =======================================================================

_HACK_COLS = (
    "id, chat_id, host_id, fee, digits, guesses_each, status, secret, created_at, "
    "started_at, lobby_message_id, last_activity, winner_id, current_player_id, "
    "turn_started_at, turn_seconds, last_pos, mode"
)
_HACK_KEYS = [c.strip() for c in _HACK_COLS.split(",")]


def _hack_row(row):
    return dict(zip(_HACK_KEYS, row)) if row else None


def hack_create(chat_id, host_id, fee, digits, guesses_each, mode="turn"):
    """Returns ('ok', hack_id) | ('exists', None). The lobby opens empty.
    mode 'turn' = players guess one after the other; 'free' = Hack X, everybody
    guesses at the same time."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO hacks (chat_id, host_id, fee, digits, guesses_each, status, created_at, last_activity, mode)
                SELECT %s, %s, %s, %s, %s, 'lobby', %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM hacks WHERE chat_id=%s AND status IN ('lobby', 'active')
                )
                RETURNING id
            """, (chat_id, host_id, fee, digits, guesses_each,
                  datetime.now().isoformat(), datetime.now().isoformat(),
                  "free" if mode == "free" else "turn", chat_id))
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return "exists", None
            conn.commit()
            return "ok", row[0]
        except Exception as e:
            conn.rollback()
            if getattr(e, "pgcode", None) == "23505":
                return "exists", None
            raise
    finally:
        conn.close()


def hack_get(hack_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT {_HACK_COLS} FROM hacks WHERE id=%s", (hack_id,))
        return _hack_row(cursor.fetchone())
    finally:
        conn.close()


def hack_get_open_for_chat(chat_id):
    """The chat's lobby/active hack (at most one), or None."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {_HACK_COLS} FROM hacks WHERE chat_id=%s AND status IN ('lobby', 'active')",
            (chat_id,),
        )
        return _hack_row(cursor.fetchone())
    finally:
        conn.close()


def hack_get_all_open():
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT {_HACK_COLS} FROM hacks WHERE status IN ('lobby', 'active')")
        return [_hack_row(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def hack_get_players(hack_id):
    """Join order. Rows: (user_id, name, status, guesses_left, guess_count, play_pos, skips)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, name, status, guesses_left, guess_count, play_pos, skips FROM hack_players
            WHERE hack_id=%s ORDER BY id ASC
        """, (hack_id,))
        return cursor.fetchall()
    finally:
        conn.close()


def hack_set_lobby_message(hack_id, message_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE hacks SET lobby_message_id=%s WHERE id=%s", (message_id, hack_id))
        conn.commit()
    finally:
        conn.close()


def hack_get_last_secret(chat_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT secret FROM hack_last_secret WHERE chat_id=%s", (chat_id,))
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def hack_join(hack_id, user_id, name):
    """Returns ('ok', source, player_count) | ('closed', None, None)
    | ('already', None, None) | ('insufficient', None, None). The fee comes
    from whichever balance covers it (Scramble first, then Word Game)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT status, fee, guesses_each FROM hacks WHERE id=%s FOR UPDATE", (hack_id,))
            row = cursor.fetchone()
            if not row or row[0] != "lobby":
                conn.rollback()
                return "closed", None, None
            fee, guesses_each = row[1], row[2]

            cursor.execute(
                "SELECT 1 FROM hack_players WHERE hack_id=%s AND user_id=%s", (hack_id, user_id)
            )
            if cursor.fetchone():
                conn.rollback()
                return "already", None, None

            new_balance, source = _tx_deduct_any(cursor, user_id, name, fee)
            if new_balance is None:
                conn.rollback()
                return "insufficient", None, None

            cursor.execute("""
                INSERT INTO hack_players (hack_id, user_id, name, status, source, guesses_left, guess_count)
                VALUES (%s, %s, %s, 'staked', %s, %s, 0)
            """, (hack_id, user_id, name, source, guesses_each))
            cursor.execute("SELECT COUNT(*) FROM hack_players WHERE hack_id=%s", (hack_id,))
            count = cursor.fetchone()[0]
            conn.commit()
            return "ok", source, count
        except Exception as e:
            conn.rollback()
            if getattr(e, "pgcode", None) == "23505":
                return "already", None, None
            raise
    finally:
        conn.close()


def hack_activate(hack_id, secret, ordered_user_ids):
    """lobby -> active, exactly once, storing the hidden number and the
    (already shuffled) turn order as play_pos. Needs 2+ staked players. Also
    remembers the number as this chat's previous one."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT status, chat_id FROM hacks WHERE id=%s FOR UPDATE", (hack_id,))
            row = cursor.fetchone()
            if not row or row[0] != "lobby":
                conn.rollback()
                return False
            chat_id = row[1]
            cursor.execute(
                "SELECT COUNT(*) FROM hack_players WHERE hack_id=%s AND status='staked'", (hack_id,)
            )
            if cursor.fetchone()[0] < 2:
                conn.rollback()
                return False
            for pos, uid in enumerate(ordered_user_ids, start=1):
                cursor.execute("""
                    UPDATE hack_players SET play_pos=%s, skips=0
                    WHERE hack_id=%s AND user_id=%s AND status='staked'
                """, (pos, hack_id, uid))
            now = datetime.now().isoformat()
            cursor.execute("""
                UPDATE hacks SET status='active', secret=%s, started_at=%s, last_activity=%s,
                    current_player_id=NULL, turn_started_at=NULL, last_pos=0
                WHERE id=%s
            """, (secret, now, now, hack_id))
            cursor.execute("""
                INSERT INTO hack_last_secret (chat_id, secret) VALUES (%s, %s)
                ON CONFLICT (chat_id) DO UPDATE SET secret = EXCLUDED.secret
            """, (chat_id, secret))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def _hack_tx_finish_over(cursor, hack_id, fee, secret, chat_id):
    """Inside an open transaction: nobody can guess any more -> the game
    ends and the BOT KEEPS THE POOL (nothing is paid or refunded)."""
    cursor.execute(
        "SELECT COUNT(*) FROM hack_players WHERE hack_id=%s AND status IN ('staked', 'out')", (hack_id,)
    )
    pot = fee * cursor.fetchone()[0]
    cursor.execute(
        "UPDATE hack_players SET status='lost' WHERE hack_id=%s AND status='staked'", (hack_id,)
    )
    cursor.execute(
        "UPDATE hacks SET status='finished', current_player_id=NULL, turn_started_at=NULL WHERE id=%s",
        (hack_id,),
    )
    return {"kind": "over", "pot": pot, "secret": secret, "chat_id": chat_id, "fee": fee}


def hack_next_turn(hack_id, seconds):
    """Decides whose turn it is: the shuffled order, one player after the
    other, skipping eliminated players and players with no guesses left.
    Returns one of:
      {'kind': 'none'}  - not an active game
      {'kind': 'busy'}  - a turn is already live
      {'kind': 'over', 'pot', 'secret', ...} - nobody can guess any more
      {'kind': 'turn', 'uid', 'name', 'left', 'started', 'seconds'}"""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT status, current_player_id, last_pos, fee, secret, chat_id
                FROM hacks WHERE id=%s FOR UPDATE
            """, (hack_id,))
            row = cursor.fetchone()
            if not row or row[0] != "active":
                conn.rollback()
                return {"kind": "none"}
            _st, current, last_pos, fee, secret, chat_id = row
            if current is not None:
                conn.rollback()
                return {"kind": "busy"}

            cursor.execute("""
                SELECT user_id, name, play_pos, guesses_left FROM hack_players
                WHERE hack_id=%s AND status='staked' AND COALESCE(guesses_left, 0) > 0
                ORDER BY play_pos ASC, id ASC
            """, (hack_id,))
            cands = cursor.fetchall()
            if not cands:
                res = _hack_tx_finish_over(cursor, hack_id, fee, secret, chat_id)
                conn.commit()
                return res

            last_pos = last_pos or 0
            nxt = next((c for c in cands if (c[2] or 0) > last_pos), cands[0])
            now = datetime.now().isoformat()
            cursor.execute("""
                UPDATE hacks SET current_player_id=%s, turn_started_at=%s, turn_seconds=%s,
                    last_pos=%s, last_activity=%s
                WHERE id=%s
            """, (nxt[0], now, seconds, nxt[2] or 0, now, hack_id))
            conn.commit()
            return {"kind": "turn", "uid": nxt[0], "name": nxt[1], "left": nxt[3],
                    "started": now, "seconds": seconds}
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def hack_turn_timeout(hack_id, expected_uid, expected_started, max_skips):
    """The live turn ran out of time. Exactly one caller (this or a valid
    guess) can ever close a turn. The player gets a skip; at `max_skips`
    skips they are eliminated (their fee stays in the pool).
    Returns {'uid','name','skips','eliminated','over', ...} or None if the
    turn was already closed / is a newer turn than the timer that fired."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT status, current_player_id, turn_started_at, fee, secret, chat_id
                FROM hacks WHERE id=%s FOR UPDATE
            """, (hack_id,))
            row = cursor.fetchone()
            if not row or row[0] != "active" or row[1] is None:
                conn.rollback()
                return None
            _st, uid, started, fee, secret, chat_id = row
            if expected_uid is not None and uid != expected_uid:
                conn.rollback()
                return None
            if expected_started is not None and started != expected_started:
                conn.rollback()
                return None

            cursor.execute("""
                UPDATE hacks SET current_player_id=NULL, turn_started_at=NULL, last_activity=%s WHERE id=%s
            """, (datetime.now().isoformat(), hack_id))
            cursor.execute(
                "SELECT name, COALESCE(skips, 0) FROM hack_players WHERE hack_id=%s AND user_id=%s FOR UPDATE",
                (hack_id, uid),
            )
            prow = cursor.fetchone()
            name, skips = (prow[0], prow[1]) if prow else (str(uid), 0)
            skips += 1
            eliminated = skips >= max_skips
            cursor.execute("""
                UPDATE hack_players SET skips=%s, status=CASE WHEN %s THEN 'out' ELSE status END
                WHERE hack_id=%s AND user_id=%s AND status='staked'
            """, (skips, eliminated, hack_id, uid))
            res = {"kind": "skip", "uid": uid, "name": name, "skips": skips,
                   "eliminated": eliminated, "over": False, "chat_id": chat_id}
            if eliminated:
                cursor.execute("""
                    SELECT COUNT(*) FROM hack_players
                    WHERE hack_id=%s AND status='staked' AND COALESCE(guesses_left, 0) > 0
                """, (hack_id,))
                if cursor.fetchone()[0] == 0:
                    res.update(_hack_tx_finish_over(cursor, hack_id, fee, secret, chat_id))
                    res["kind"] = "skip"
                    res["over"] = True
            conn.commit()
            return res
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def hack_restart_turn_clock(hack_id):
    """Gives the live turn a fresh full countdown (same player). Used after
    a restart / by /resumehack. Returns the new turn_started_at or None."""
    now = datetime.now().isoformat()
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE hacks SET turn_started_at=%s, last_activity=%s
            WHERE id=%s AND status='active' AND current_player_id IS NOT NULL
            RETURNING id
        """, (now, now, hack_id))
        ok = cursor.fetchone() is not None
        conn.commit()
        return now if ok else None
    finally:
        conn.close()


def hack_guess(hack_id, user_id, guess):
    """One atomic guess, only on the player's own turn. The hack row is
    locked for the whole call, so two quick guesses can never both pass at 1
    guess left or both win. A valid guess ends the player's turn.
    Returns a dict whose 'kind' is one of:
      'ignore'  - no active hack / not a (still active) player (stay silent)
      'nomore'  - player has 0 guesses left ('first': True the first time only)
      'notturn' - it is somebody else's turn (normal Hack only)
      'invalid' - malformed guess (no guess used, turn continues)
      'guess'   - valid, not solved
      'win'     - solved: pot paid to the winner (idempotent: status guard)
      'over'    - that was the last guess anybody had: the bot keeps the pot"""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT status, secret, digits, fee, chat_id, current_player_id, mode
                FROM hacks WHERE id=%s FOR UPDATE
            """, (hack_id,))
            row = cursor.fetchone()
            if not row or row[0] != "active" or not row[1]:
                conn.rollback()
                return {"kind": "ignore"}
            _status, secret, digits, fee, chat_id, current, mode = row

            cursor.execute("""
                SELECT name, status, guesses_left, guess_count, no_guess_notified
                FROM hack_players WHERE hack_id=%s AND user_id=%s FOR UPDATE
            """, (hack_id, user_id))
            p = cursor.fetchone()
            if not p or p[1] != "staked":
                conn.rollback()
                return {"kind": "ignore"}
            name, _pst, left, count, notified = p
            left, count = left or 0, count or 0

            if left <= 0:
                first = not notified
                if first:
                    cursor.execute(
                        "UPDATE hack_players SET no_guess_notified=TRUE WHERE hack_id=%s AND user_id=%s",
                        (hack_id, user_id),
                    )
                conn.commit()
                return {"kind": "nomore", "first": first}

            if mode != "free" and current != user_id:  # Hack X: anybody may guess at any time
                conn.rollback()
                return {"kind": "notturn"}

            if (len(guess) != digits or not guess.isascii() or not guess.isdigit()
                    or "0" in guess or len(set(guess)) != digits):
                conn.rollback()
                return {"kind": "invalid", "digits": digits}

            left -= 1
            count += 1
            hacks = sum(1 for a, b in zip(guess, secret) if a == b)
            glitches = sum(1 for i, ch in enumerate(guess) if ch != secret[i] and ch in secret)
            cursor.execute(
                "UPDATE hack_players SET guesses_left=%s, guess_count=%s WHERE hack_id=%s AND user_id=%s",
                (left, count, hack_id, user_id),
            )
            # the valid guess ends this player's turn
            cursor.execute("""
                UPDATE hacks SET last_activity=%s, current_player_id=NULL, turn_started_at=NULL WHERE id=%s
            """, (datetime.now().isoformat(), hack_id))
            base = {"uid": user_id, "name": name, "guess": guess, "hacks": hacks, "glitches": glitches,
                    "left": left, "count": count, "chat_id": chat_id, "secret": secret, "fee": fee}

            cursor.execute(
                "SELECT COUNT(*) FROM hack_players WHERE hack_id=%s AND status IN ('staked', 'out')", (hack_id,)
            )
            pot = fee * cursor.fetchone()[0]

            if hacks == digits:
                balance = _tx_credit(cursor, user_id, name, pot)
                _tx_credit_chat_scramble(cursor, user_id, chat_id, name, pot)
                cursor.execute(
                    "UPDATE hack_players SET status='won' WHERE hack_id=%s AND user_id=%s", (hack_id, user_id)
                )
                cursor.execute(
                    "UPDATE hack_players SET status='lost' WHERE hack_id=%s AND status='staked'", (hack_id,)
                )
                cursor.execute(
                    "UPDATE hacks SET status='finished', winner_id=%s WHERE id=%s", (user_id, hack_id)
                )
                conn.commit()
                base.update({"kind": "win", "pot": pot, "balance": balance})
                return base

            cursor.execute("""
                SELECT COUNT(*) FROM hack_players
                WHERE hack_id=%s AND status='staked' AND COALESCE(guesses_left, 0) > 0
            """, (hack_id,))
            if cursor.fetchone()[0] == 0:
                over = _hack_tx_finish_over(cursor, hack_id, fee, secret, chat_id)
                conn.commit()
                base.update({"kind": "over", "pot": over["pot"]})
                return base

            conn.commit()
            base["kind"] = "guess"
            return base
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def hack_cancel(hack_id, keep_host_fee=False):
    """Cancels a lobby/active hack and refunds every staked player's fee to
    the balance it came from. With keep_host_fee=True the host's own fee (if
    the host joined as a player) is NOT refunded - the bot keeps it.
    Idempotent. Returns {chat_id, fee, host_id, refunds: [(uid, name, fee, balance)],
    kept: [(uid, name, fee)]} or None."""
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT status, fee, chat_id, host_id FROM hacks WHERE id=%s FOR UPDATE", (hack_id,)
            )
            row = cursor.fetchone()
            if not row or row[0] not in ("lobby", "active"):
                conn.rollback()
                return None
            _status, fee, chat_id, host_id = row

            cursor.execute("""
                SELECT user_id, name, COALESCE(source, 'scramble') FROM hack_players
                WHERE hack_id=%s AND status IN ('staked', 'out') ORDER BY id ASC
            """, (hack_id,))
            refunds, kept = [], []
            for uid, name, source in cursor.fetchall():
                if keep_host_fee and uid == host_id:
                    cursor.execute("""
                        UPDATE hack_players SET status='kept'
                        WHERE hack_id=%s AND user_id=%s AND status IN ('staked', 'out')
                    """, (hack_id, uid))
                    if cursor.rowcount == 1:
                        kept.append((uid, name, fee))
                    continue
                cursor.execute("""
                    UPDATE hack_players SET status='refunded'
                    WHERE hack_id=%s AND user_id=%s AND status IN ('staked', 'out')
                """, (hack_id, uid))
                if cursor.rowcount == 1:
                    refunds.append((uid, name, fee, _tx_credit_source(cursor, uid, name, fee, source)))

            cursor.execute("UPDATE hacks SET status='cancelled' WHERE id=%s", (hack_id,))
            conn.commit()
            return {"chat_id": chat_id, "fee": fee, "host_id": host_id, "refunds": refunds, "kept": kept}
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


# =======================================================================
# /support (Telegram Stars)
# =======================================================================

def support_record(charge_id, user_id, name, stars):
    """Stores one support payment. Returns False if this charge was already
    recorded (a redelivered successful_payment), True otherwise."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO support_payments (charge_id, user_id, name, stars, paid_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (charge_id) DO NOTHING
            RETURNING charge_id
        """, (charge_id, user_id, name, stars, datetime.now().isoformat()))
        inserted = cursor.fetchone() is not None
        conn.commit()
        return inserted
    finally:
        conn.close()


def support_get_user(user_id):
    """(total_stars, contributions) for one user."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COALESCE(SUM(stars), 0), COUNT(*) FROM support_payments WHERE user_id=%s", (user_id,)
        )
        row = cursor.fetchone()
        return (row[0] or 0, row[1] or 0)
    finally:
        conn.close()


def support_leaderboard(limit=10):
    """[(user_id, latest_name, total_stars, contributions)], biggest first."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id,
                   (ARRAY_AGG(name ORDER BY paid_at DESC))[1] AS name,
                   SUM(stars) AS total, COUNT(*) AS n
            FROM support_payments
            GROUP BY user_id
            ORDER BY total DESC, MIN(paid_at) ASC
            LIMIT %s
        """, (limit,))
        return cursor.fetchall()
    finally:
        conn.close()


def support_stats(latest=5):
    """{'total_stars', 'supporters', 'contributions', 'latest': [(user_id, name, stars, paid_at)]}"""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COALESCE(SUM(stars), 0), COUNT(DISTINCT user_id), COUNT(*) FROM support_payments"
        )
        total, supporters, contributions = cursor.fetchone()
        cursor.execute("""
            SELECT user_id, name, stars, paid_at FROM support_payments
            ORDER BY paid_at DESC LIMIT %s
        """, (latest,))
        return {"total_stars": total or 0, "supporters": supporters or 0,
                "contributions": contributions or 0, "latest": cursor.fetchall()}
    finally:
        conn.close()

# ===========================================================================
# JACKPOT / LUCKY NUMBER
# ===========================================================================
# Defaults and limits live here so the DB layer and the bot agree.
JACKPOT_DEFAULT_FEE = 1000
JACKPOT_REWARD_MULTIPLIER = 10          # default prize = fee x 10
JACKPOT_SMALL_RANGE_MAX = 20            # fee below the threshold: 1-20
JACKPOT_BIG_RANGE_MAX = 9               # fee at/above the threshold: 1-9
JACKPOT_BIG_FEE_THRESHOLD = 1000


def jackpot_range_max(fee):
    """Highest lucky number for this fee (the lowest is always 1)."""
    return JACKPOT_BIG_RANGE_MAX if fee >= JACKPOT_BIG_FEE_THRESHOLD else JACKPOT_SMALL_RANGE_MAX


def _jackpot_random_number(range_max):
    """Unpredictable pick from 1..range_max (OS entropy, no seed/cycle)."""
    return secrets.randbelow(range_max) + 1


def jackpot_get_settings(chat_id):
    """(fee, reward) for a chat; defaults when the chat never configured it."""
    conn = connect()
    try:
        cursor = conn.cursor()
        return _jackpot_settings_tx(cursor, chat_id)
    finally:
        conn.close()


def _jackpot_settings_tx(cursor, chat_id):
    cursor.execute("SELECT fee, reward FROM jackpot_settings WHERE chat_id=%s", (chat_id,))
    row = cursor.fetchone()
    if row:
        return int(row[0]), int(row[1])
    return JACKPOT_DEFAULT_FEE, JACKPOT_DEFAULT_FEE * JACKPOT_REWARD_MULTIPLIER


def jackpot_set_settings(chat_id, fee, reward, now_str):
    """Persist the chat's fee and prize (kept until changed again)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO jackpot_settings (chat_id, fee, reward, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (chat_id) DO UPDATE SET
                fee = EXCLUDED.fee, reward = EXCLUDED.reward, updated_at = EXCLUDED.updated_at
        """, (chat_id, fee, reward, now_str))
        conn.commit()
    finally:
        conn.close()


def jackpot_start(chat_id, user_id, name, play_date, now_str, expires_str):
    """Join a Jackpot: ONE transaction that
         1. claims today's attempt for this player (PRIMARY KEY on user+day),
         2. refuses if they already have an active game,
         3. debits the fee from the combined balance (all-or-nothing),
         4. creates the game with a fresh secret number.
    Anything other than success rolls the WHOLE transaction back, so a failed
    join (not enough money, already played...) never consumes the attempt and
    never charges. Two simultaneous starts by one player cannot both succeed:
    the second blocks on the PRIMARY KEY row, then sees it already exists.
    Returns (status, info): 'ok' -> game dict | 'already_played' | 'active' |
    'insufficient' -> {'have': combined balance, 'fee': fee}."""
    conn = connect()
    try:
        cursor = conn.cursor()
        fee, reward = _jackpot_settings_tx(cursor, chat_id)

        cursor.execute("""
            INSERT INTO jackpot_daily (user_id, play_date) VALUES (%s, %s)
            ON CONFLICT (user_id, play_date) DO NOTHING
        """, (user_id, play_date))
        if cursor.rowcount == 0:
            conn.rollback()
            return "already_played", None

        cursor.execute("SELECT id FROM jackpot_games WHERE user_id=%s AND status='active'", (user_id,))
        if cursor.fetchone():
            conn.rollback()
            return "active", None

        balance_after, source = _tx_deduct_combined(cursor, user_id, name, fee)
        if balance_after is None:
            have = _tx_combined_total(cursor, user_id)
            conn.rollback()
            return "insufficient", {"have": have, "fee": fee}

        range_max = jackpot_range_max(fee)
        secret = _jackpot_random_number(range_max)
        cursor.execute("""
            INSERT INTO jackpot_games
                (chat_id, user_id, user_name, fee, reward, range_max, secret, source,
                 status, created_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s)
            RETURNING id
        """, (chat_id, user_id, name, fee, reward, range_max, secret, source, now_str, expires_str))
        game_id = cursor.fetchone()[0]
        cursor.execute("UPDATE jackpot_daily SET game_id=%s WHERE user_id=%s AND play_date=%s",
                       (game_id, user_id, play_date))
        conn.commit()
        return "ok", {
            "id": game_id, "chat_id": chat_id, "user_id": user_id, "user_name": name,
            "fee": fee, "reward": reward, "range_max": range_max, "secret": secret,
            "expires_at": expires_str, "balance_after": balance_after,
        }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def jackpot_attach_message(game_id, message_id):
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE jackpot_games SET message_id=%s WHERE id=%s", (message_id, game_id))
        conn.commit()
    finally:
        conn.close()


def jackpot_settle(game_id, guess, now_str):
    """Settle ONE game exactly once. guess=None means the time ran out.
    The row is locked and only an 'active' game can be settled, so a guess
    racing the timeout, a double tap, or a restart replay can never pay twice
    or settle twice. A guess that arrives after the deadline is ignored (the
    timeout settles it). Returns None when there was nothing to settle, else
    {'outcome': 'won'|'lost'|'timeout', ...} including the real new balance."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT chat_id, user_id, user_name, fee, reward, range_max, secret, status,
                   expires_at, message_id
            FROM jackpot_games WHERE id=%s FOR UPDATE
        """, (game_id,))
        row = cursor.fetchone()
        if not row or row[7] != "active":
            conn.rollback()
            return None
        (chat_id, user_id, name, fee, reward, range_max, secret, _status, expires_at, message_id) = row

        if guess is not None:
            if now_str > expires_at or not (1 <= guess <= range_max):
                conn.rollback()
                return None
            outcome = "won" if guess == secret else "lost"
        else:
            outcome = "timeout"

        if outcome == "won":
            _tx_credit(cursor, user_id, name, reward)
            if chat_id < 0:
                _tx_credit_chat_scramble(cursor, user_id, chat_id, name, reward)
        cursor.execute(
            "UPDATE jackpot_games SET status=%s, guess=%s, settled_at=%s WHERE id=%s",
            (outcome, guess, now_str, game_id),
        )
        balance = _tx_combined_total(cursor, user_id)
        conn.commit()
        return {
            "outcome": outcome, "id": game_id, "chat_id": chat_id, "user_id": user_id,
            "user_name": name, "fee": fee, "reward": reward, "secret": secret,
            "guess": guess, "balance": balance, "message_id": message_id,
        }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def jackpot_get_active():
    """Every unsettled game (used once at startup to restore timers)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, chat_id, user_id, user_name, fee, reward, range_max, message_id, expires_at
            FROM jackpot_games WHERE status='active'
        """)
        keys = ("id", "chat_id", "user_id", "user_name", "fee", "reward", "range_max",
                "message_id", "expires_at")
        return [dict(zip(keys, r)) for r in cursor.fetchall()]
    finally:
        conn.close()