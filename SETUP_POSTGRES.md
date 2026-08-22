# Moving @Uff_GameBot to PostgreSQL

This is Phase 1 of the shared-database work: `database.py` now talks to
PostgreSQL instead of a local SQLite file, and `BOT_TOKEN`/`OWNER_ID`
come from environment variables instead of being hardcoded. Nothing
about the bot's actual behavior changed - every function in
`database.py` kept its exact name, arguments, and return shape.

**Important - I could not test this against a real PostgreSQL server.**
My sandbox has no network access and no local Postgres install, so
everything below was written and reviewed carefully by hand (every
function's SQL was checked line-by-line against the original SQLite
version, and function signatures were verified to match 1:1
programmatically), but it has not been run against a live database.
Please run through the steps below on a staging database before
pointing this at production.

## 1. Get a PostgreSQL database

Any of these work - you just need a connection string:
- A managed service (Railway, Render, Supabase, AWS RDS, DigitalOcean, etc.)
- Self-hosted: `sudo apt install postgresql` then `createdb uff_shared`

## 2. Install dependencies

```bash
pip install -r requirements.txt
```

## 3. Configure

```bash
cp .env.example .env
# edit .env with your real BOT_TOKEN, OWNER_ID, and DATABASE_URL
```

## 4. Create the schema

The bot creates its own tables on startup (`db.create_tables()` runs
inside `main()`), so you generally don't need to do anything manual -
just start the bot once:

```bash
python3 main.py
```

If you'd rather review/run the schema by hand first, every
`CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` statement
is in `create_tables()` at the top of `database.py` - safe to run
against a fresh database with a Postgres client if you want to inspect
it before the bot ever connects.

## 5. If you have existing data in the old SQLite file

If `data/uff_game.db` already has real players/groups/balances in it
(i.e. this bot has been running in production), migrate that data over
**before** switching production traffic to Postgres:

```bash
# 1. Start the bot once against Postgres (or run db.create_tables()
#    directly) so the target tables exist.
# 2. Then run:
python3 migrate_sqlite_to_postgres.py data/uff_game.db
```

This copies every row across using `ON CONFLICT DO NOTHING`, so it's
safe to re-run - it will never duplicate or overwrite data. Read the
docstring at the top of that script for the exact guarantees.

## 6. Test before going live

Things worth checking against your staging database before cutover:
- `/start`, playing a full round of the word game, `/leaderboard`,
  `/topplayers`, `/topgroups`, `/claimdailyreward`
- Owner commands: `/users`, `/groups`, `/stats`, `/addadmin`
- Restart the bot process and confirm everything (balances, admins,
  bot config like maintenance mode / ad manager) survived the restart

## What's next

This is just the SQLite→PostgreSQL swap for the existing bot. The rest
of the shared-database plan (item 4 in the running spec) - building
@UFFAdsBot against these same tables, background jobs for ad
scheduling/expiry, and the new ads/payments/subscriptions tables - is
still ahead and will build on top of this foundation.
