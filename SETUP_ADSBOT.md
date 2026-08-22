# Running @UFFAdsBot

@UFFAdsBot (`ads_main.py` + `ads_database.py`) is a separate bot process
from @Uff_GameBot - deploy it as its own process (its own `python3
ads_main.py`), pointed at the SAME `DATABASE_URL` as @Uff_GameBot. That
shared `DATABASE_URL` is what makes "shared owner/admin system" and
"shared ad manager" work: both bots read the same `bot_admins` and
`bot_config` tables live, on every call - add a Bot Admin in
@Uff_GameBot and @UFFAdsBot recognizes them immediately, no restart,
no sync step.

## Setup

1. Create a second bot with @BotFather (a different token from
   @Uff_GameBot's).
2. Add to your `.env` (see `.env.example`): `ADS_BOT_TOKEN`,
   `OWNER_USERNAME` (optional, powers the Contact Support button).
   Reuses `OWNER_ID` and `DATABASE_URL` from the same `.env` file
   @Uff_GameBot uses.
3. `pip install -r requirements.txt` (same one - already covers both bots).
4. `python3 ads_main.py`

`ads_database.create_tables()` runs automatically on startup and is
safe to run alongside @Uff_GameBot's own `create_tables()` - both use
`CREATE TABLE IF NOT EXISTS`, so whichever bot starts first "wins" and
the second is a no-op for the tables they share.

## What's implemented in this pass

- `/start` welcome menu (Submit Advertisement / Advertisement Rates / Help)
- Full submission conversation (business name → link → type → duration →
  ad text → notes → preview → submit), with Edit (restarts the form) and
  Cancel
- Anti-spam: blocks a second submission while one is still pending
- Owner/admin gets the new application with Approve / Reject / Contact
  Advertiser buttons
- Approve → advertiser is prompted for payment; Reject → owner types a
  reason, advertiser is notified with it
- Advertiser uploads payment proof (photo or document) in DM → forwarded
  to the owner with Confirm/Reject buttons → advertiser notified either way
- `/setrates` (owner-only), `/applications` (paginated, 20/page),
  `/open <id>`, `/approved`, `/rejected`, `/payments`, `/stats`,
  `/searchads`, `/broadcast <audience> <message>`
- Bot Admins (shared with @Uff_GameBot) can view/approve/reject
  applications, view payments, and broadcast - but NOT set rates
  (owner-only, per spec)
- Every application/payment record persists in PostgreSQL

Tested via a full simulated run (submit → approve → payment proof →
confirm, plus the reject path, anti-spam block, permission checks,
pagination, and search) against an in-memory fake of the database layer,
since psycopg2 isn't installable in this sandbox - same caveat as
`SETUP_POSTGRES.md`: the actual SQL has been carefully reviewed by hand
but never run against a live Postgres server. Test on staging first.

## Deliberately NOT in this pass (still ahead per the running plan)

- Making approved+paid ads actually **appear** inside @Uff_GameBot's
  `/leaderboard` / `/topplayers` / `/topgroups` - that's the automatic
  scheduler + rotation + the tables @Uff_GameBot needs to read from,
  which is the remainder of item 4 (shared-DB integration)
- Payment Management System (`/paymentmethods`, `/addpayment`,
  per-gateway config like `/setbank` `/setpaystack`, etc - item 5)
- Subscription & Billing System (coupons, promotional offers, invoices,
  receipts, `/myads`, `/revenue`, renewal reminders, exports - item 6)
- Advertiser history is queryable in the data layer
  (`get_advertiser_history()`) but there's no `/myads`-style command
  exposing it yet - that's part of item 6
