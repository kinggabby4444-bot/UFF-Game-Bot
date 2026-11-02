"""
poolfix.py - two small speed-ups that need NO edits to main.py / database.py.

1) KEEP CONNECTIONS WARM.
   psycopg2's pool, as database.py creates it (minconn=1), keeps only ONE idle
   connection and CLOSES every other connection as soon as it is handed back.
   So whenever two or more database calls overlap, the bot opens a brand-new
   connection (~0.5 s: TCP + TLS + login over a 70 ms link) and throws it away
   a moment later. Your DIAG log showed this: "NEW connections opened: 2-5" per
   minute. This module makes the pool keep all its connections open.
   Turn off:  set the Render env var  DB_POOL_MIN=1
   Or set it to a number (e.g. DB_POOL_MIN=5) to keep only that many warm.

2) FEWER ROUND TRIPS FOR READ-ONLY QUERIES.
   A normal call is BEGIN + query + ROLLBACK = 3 network round trips (3 x 70 ms).
   For a fixed list of functions that only read (single SELECTs), the connection
   is switched to autocommit for that one call, so it costs 1 round trip.
   Functions that write, lock rows (FOR UPDATE) or need several statements to be
   atomic are NOT on the list and behave exactly as before.
   Turn off:  set the Render env var  DB_READ_AUTOCOMMIT=0

Import this BEFORE main/database (server.py does), then call install(database).
"""
import logging
import os
import sys

import psycopg2
import psycopg2.extensions
import psycopg2.pool

log = logging.getLogger("poolfix")

# ---------------------------------------------------------------------------
# 1) warm pool
# ---------------------------------------------------------------------------
if not getattr(psycopg2.pool.ThreadedConnectionPool, "_poolfix", False):
    _orig_init = psycopg2.pool.ThreadedConnectionPool.__init__

    def _init(self, minconn, maxconn, *a, **k):
        wanted = os.environ.get("DB_POOL_MIN")
        if maxconn >= 2:                      # leave 0/1-sized pools (e.g. the ads bot) alone
            try:
                target = maxconn if wanted is None else int(wanted)
            except ValueError:
                target = maxconn
            minconn = max(minconn, min(maxconn, target))
        _orig_init(self, minconn, maxconn, *a, **k)
        log.warning("POOLFIX pool created: minconn=%s maxconn=%s (connections kept open)", minconn, maxconn)

    psycopg2.pool.ThreadedConnectionPool.__init__ = _init
    psycopg2.pool.ThreadedConnectionPool._poolfix = True


# ---------------------------------------------------------------------------
# 2) autocommit for read-only functions
# ---------------------------------------------------------------------------
# Every name here is a database.py function that ONLY reads (SELECTs), with no
# row locks and nothing that must be atomic with another statement.
READ_ONLY = frozenset({
    "is_user_banned", "is_group_banned", "is_tournament_banned", "is_bot_admin",
    "get_game", "is_explaining_started", "get_explain_log", "get_auto_lead",
    "get_pending_input", "get_config",
    "get_user", "get_user_by_id", "get_user_by_username", "premium_get_expiry",
    "tournament_get", "tournament_get_open_for_chat", "tournament_get_all_open",
    "tournament_get_players", "tournament_is_staked_player",
    "elim_get", "elim_get_open_for_chat", "elim_get_all_open", "elim_get_players",
    "hack_get", "hack_get_open_for_chat", "hack_get_all_open", "hack_get_players",
    "scramble_get_round", "get_overdue_scramble_rounds", "scramble_get_idle_auto_chats",
    "get_scramble_slowmode", "scramble_get_auto_mode",
    "get_effective_game_difficulty", "get_effective_admin_mode",
    "is_autodelete_tracking_active", "get_autodelete_settings",
    "_used_words_get", "get_active_ads_for_rotation",
    "get_combined_top_user_ids", "get_combined_global_position",
})


class _AutoCommitConn:
    """Wraps the pooled connection for ONE read-only call; restores normal mode on close()."""

    def __init__(self, pooled, inner):
        self._p = pooled
        self._i = inner
        self._done = False

    def cursor(self, *a, **k):
        return self._p.cursor(*a, **k)

    def commit(self):
        return self._p.commit()

    def rollback(self):
        return self._p.rollback()

    def close(self):
        if self._done:
            return
        self._done = True
        try:
            self._i.autocommit = False
        except Exception:
            # Could not put it back into normal mode: never hand it to anyone else.
            try:
                self._i.close()
            except Exception:
                pass
        self._p.close()

    def __getattr__(self, name):
        return getattr(self._p, name)


def install(database):
    if os.environ.get("DB_READ_AUTOCOMMIT", "1") == "0":
        log.warning("POOLFIX read-only autocommit is OFF (DB_READ_AUTOCOMMIT=0)")
        return
    orig = database.connect
    if getattr(orig, "_poolfix", False):
        return
    idle = psycopg2.extensions.TRANSACTION_STATUS_IDLE

    def connect():
        # Find the database.py function that asked for the connection (other wrappers may sit in between).
        f = sys._getframe(1)
        while f is not None and f.f_globals.get("__name__") != database.__name__:
            f = f.f_back
        fn = f.f_code.co_name if f is not None else ""
        pooled = orig()
        if fn not in READ_ONLY:
            return pooled
        inner = getattr(pooled, "_conn", None)
        if inner is None:
            return pooled
        try:
            if inner.get_transaction_status() != idle:
                inner.rollback()              # e.g. the idle-connection ping left a transaction open
            inner.autocommit = True
        except Exception:
            return pooled                     # anything odd: behave exactly as before
        return _AutoCommitConn(pooled, inner)

    connect._poolfix = True
    database.connect = connect
    log.warning("POOLFIX read-only autocommit active for %d functions", len(READ_ONLY))