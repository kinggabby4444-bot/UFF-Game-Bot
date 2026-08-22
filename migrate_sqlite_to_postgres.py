"""
One-time migration: copies every row from the OLD SQLite database
(data/uff_game.db) into the NEW PostgreSQL database, so cutting over to
Postgres doesn't lose any existing players, balances, groups, or history.

Usage:
    python3 migrate_sqlite_to_postgres.py [path-to-sqlite-file]

Requires DATABASE_URL to be set (same as the bots need) pointing at the
already-created Postgres database - run database.create_tables() first
(or just start either bot once) so the target tables exist.

Safe to re-run: every insert uses ON CONFLICT DO NOTHING, so running
this twice never duplicates or overwrites rows. It only fills in rows
that don't exist yet in Postgres - it will NOT overwrite anything
already there, so run it BEFORE putting real traffic on Postgres.
"""

import sys
import sqlite3

import database as pg  # the new Postgres-backed database.py


TABLES_IN_DEPENDENCY_ORDER = [
    # (table name, column list) - order matters where there's no FK
    # constraint issue, but none of these tables actually have foreign
    # keys to each other, so plain declaration order is fine.
    ("users", ["id", "name", "username", "level", "rank", "last_daily_claim",
               "auto_lead", "games_played", "games_won", "last_active"]),
    ("word_games", ["chat_id", "leader_id", "leader_name", "secret_word",
                     "game_active", "explaining_started"]),
    ("pending_input", ["user_id", "chat_id", "action"]),
    ("chat_scores", ["user_id", "chat_id", "name", "coins"]),
    ("global_scores", ["user_id", "name", "coins"]),
    ("used_words", ["chat_id", "word"]),
    ("explain_log", ["chat_id", "leader_id", "log"]),
    ("groups", ["chat_id", "title", "member_count", "first_seen", "games_played", "last_active"]),
    ("banned_users", ["user_id", "banned_at", "banned_by"]),
    ("banned_groups", ["chat_id", "banned_at", "banned_by"]),
    ("bot_config", ["key", "value"]),
    ("bot_stats", ["key", "value"]),
    ("stop_votes", ["chat_id", "user_id", "name"]),
    ("topup_log", ["target_id", "target_name", "amount", "owner_id", "timestamp"]),
    ("bot_admins", ["user_id", "name", "username", "added_by", "added_at"]),
    ("daily_reward_log", ["user_id", "name", "username", "amount", "claim_date", "claimed_at"]),
]

# Conflict target per table (matches the PRIMARY KEY in create_tables()).
# topup_log and daily_reward_log have a surrogate `id` primary key that
# isn't in the source column list, so there's nothing meaningful to
# conflict on - those two just always insert (safe: re-running would
# duplicate log rows, so only run this script once against a fresh
# Postgres database, as documented above).
CONFLICT_TARGETS = {
    "users": "id",
    "word_games": "chat_id",
    "pending_input": "user_id",
    "chat_scores": "user_id, chat_id",
    "global_scores": "user_id",
    "used_words": "chat_id, word",
    "explain_log": "chat_id",
    "groups": "chat_id",
    "banned_users": "user_id",
    "banned_groups": "chat_id",
    "bot_config": "key",
    "bot_stats": "key",
    "stop_votes": "chat_id, user_id",
    "bot_admins": "user_id",
}


def migrate(sqlite_path: str):
    sq_conn = sqlite3.connect(sqlite_path)
    sq_cursor = sq_conn.cursor()

    pg_conn = pg.connect()
    pg_cursor = pg_conn.cursor()

    total_rows = 0

    for table, columns in TABLES_IN_DEPENDENCY_ORDER:
        try:
            sq_cursor.execute(f"SELECT {', '.join(columns)} FROM {table}")
        except sqlite3.OperationalError as e:
            print(f"  skip {table}: {e} (table doesn't exist in the source DB - fine, nothing to migrate)")
            continue

        rows = sq_cursor.fetchall()
        if not rows:
            print(f"  {table}: 0 rows")
            continue

        placeholders = ", ".join(["%s"] * len(columns))
        col_list = ", ".join(columns)
        conflict = CONFLICT_TARGETS.get(table)
        conflict_clause = f"ON CONFLICT ({conflict}) DO NOTHING" if conflict else ""

        sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) {conflict_clause}"

        inserted = 0
        for row in rows:
            pg_cursor.execute(sql, row)
            inserted += pg_cursor.rowcount

        pg_conn.commit()
        print(f"  {table}: {inserted}/{len(rows)} rows inserted (rest already existed)")
        total_rows += inserted

    pg_conn.close()
    sq_conn.close()

    print(f"\nDone. {total_rows} rows migrated into PostgreSQL.")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "data/uff_game.db"
    print(f"Migrating {path} -> PostgreSQL ({pg.DATABASE_URL.split('@')[-1] if '@' in pg.DATABASE_URL else '(local)'})...\n")
    migrate(path)
