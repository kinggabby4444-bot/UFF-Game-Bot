"""
PostgreSQL data-access layer for @UFFAdsBot.

This is a separate bot/process/deployment from @Uff_GameBot, so it ships
with its own copy of the connection-pool plumbing - but it points at the
SAME DATABASE_URL, and reuses the SAME bot_admins/bot_config tables
@Uff_GameBot already created, which is what makes "shared owner/admin
system" and "shared ad manager" work: whichever bot writes to those
tables, both read the current value live, every time, with no caching
and no restart required.

New tables this bot owns: ad_applications, ad_payments, ad_rates,
ad_subscriptions, payment_methods, ad_coupons, ad_offers, ad_invoices,
ad_audit_log. @Uff_GameBot does not write to any of these - it's
UFFAdsBot's job to manage them (per the "Bot Responsibilities" split in
the spec). @Uff_GameBot's own database.py has a matching (defensive,
CREATE TABLE IF NOT EXISTS) copy of the ad_subscriptions table
definition and read-only query functions against it, so whichever bot
happens to start first creates it and the other just reads live from
the same rows - that's what makes an ad approved/scheduled/paused in
UFFAdsBot show up (or disappear) inside @Uff_GameBot immediately, no
restart, no sync step.
"""

import os
from datetime import datetime, timedelta

import psycopg2
import psycopg2.pool

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Set it to the SAME PostgreSQL connection "
        "string @Uff_GameBot uses - that's what makes the two bots share data."
    )

_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=int(os.environ.get("ADS_DB_POOL_MAX", "5")),
    dsn=DATABASE_URL,
)


class PooledConnection:
    """Same wrapper as @Uff_GameBot's database.py - conn.close() returns
    the connection to the pool instead of closing the socket."""

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


# Default advertisement durations offered everywhere in this bot. These
# are only used to SEED the table the very first time it's created -
# once seeded, /setrate and /deleterate fully control what durations
# exist. Owners are not limited to this list.
AD_DURATIONS = [1, 3, 7, 14, 30]

_DEFAULT_RATES = {1: 500, 3: 1200, 7: 2500, 14: 5000, 30: 9000}


def create_tables():
    conn = connect()
    cursor = conn.cursor()

    # --- Tables shared with @Uff_GameBot - CREATE IF NOT EXISTS so this
    # is a no-op if @Uff_GameBot already created them (it's just as safe
    # if this bot happens to start first against a brand new database).
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
    CREATE TABLE IF NOT EXISTS bot_config (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    # --- Tables this bot owns ---

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ad_applications (
        id BIGSERIAL PRIMARY KEY,
        applicant_id BIGINT NOT NULL,
        applicant_name TEXT,
        applicant_username TEXT,
        business_name TEXT,
        ad_link TEXT,
        ad_type TEXT,
        duration_days INTEGER,
        ad_text TEXT,
        notes TEXT,
        status TEXT DEFAULT 'pending',
        reject_reason TEXT,
        submitted_at TEXT,
        decided_at TEXT,
        decided_by BIGINT,
        coupon_code TEXT,
        final_price INTEGER
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ad_payments (
        id BIGSERIAL PRIMARY KEY,
        application_id BIGINT REFERENCES ad_applications(id),
        file_id TEXT,
        file_kind TEXT,
        submitted_at TEXT,
        status TEXT DEFAULT 'pending',
        confirmed_at TEXT,
        confirmed_by BIGINT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ad_rates (
        duration_days INTEGER PRIMARY KEY,
        price INTEGER NOT NULL
    )
    """)

    # Migration: adds currency support to ad_rates without touching any
    # existing rows - safe/no-op if the column is already there. Every
    # existing row (which was implicitly NGN) gets tagged 'NGN'
    # automatically via the DEFAULT.
    cursor.execute("ALTER TABLE ad_rates ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'NGN'")

    # An approved + paid advertisement's scheduled/active/expired
    # lifecycle - this IS the "subscription" in the billing sense too
    # (one table serves both the scheduler/rotation needs and the
    # subscription-management needs, since they're the same entity).
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ad_subscriptions (
        id BIGSERIAL PRIMARY KEY,
        application_id BIGINT REFERENCES ad_applications(id),
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

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS payment_methods (
        name TEXT PRIMARY KEY,
        enabled INTEGER DEFAULT 1,
        config TEXT DEFAULT '{}',
        created_at TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ad_coupons (
        code TEXT PRIMARY KEY,
        discount_type TEXT NOT NULL,
        discount_value INTEGER NOT NULL,
        expires_at TEXT,
        max_uses INTEGER,
        uses_count INTEGER DEFAULT 0,
        created_at TEXT,
        created_by BIGINT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ad_offers (
        id BIGSERIAL PRIMARY KEY,
        description TEXT NOT NULL,
        start_at TEXT,
        end_at TEXT,
        created_at TEXT,
        created_by BIGINT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ad_invoices (
        id BIGSERIAL PRIMARY KEY,
        application_id BIGINT REFERENCES ad_applications(id),
        subscription_id BIGINT REFERENCES ad_subscriptions(id),
        business_name TEXT,
        advertiser_name TEXT,
        advertiser_username TEXT,
        advertiser_id BIGINT,
        duration_days INTEGER,
        amount_paid INTEGER,
        payment_method TEXT,
        payment_date TEXT,
        ad_start TEXT,
        ad_end TEXT
    )
    """)

    # Migration: adds currency to invoices, matching ad_rates - safe/
    # no-op if already present. Existing rows (implicitly NGN) get
    # tagged 'NGN' automatically via the DEFAULT.
    cursor.execute("ALTER TABLE ad_invoices ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'NGN'")

    # Every owner/admin action that changes something - approvals,
    # rejections, payment decisions, pause/resume/cancel/extend,
    # coupon/rate/payment-method changes.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ad_audit_log (
        id BIGSERIAL PRIMARY KEY,
        actor_id BIGINT,
        action TEXT,
        details TEXT,
        timestamp TEXT
    )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ad_subscriptions_status ON ad_subscriptions (status, end_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ad_subscriptions_applicant ON ad_subscriptions (applicant_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ad_invoices_applicant ON ad_invoices (advertiser_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ad_invoices_date ON ad_invoices (payment_date)")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ad_applications_status ON ad_applications (status, submitted_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ad_applications_applicant ON ad_applications (applicant_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ad_payments_application ON ad_payments (application_id)")

    conn.commit()

    # Seed default rates for any duration that doesn't have one yet.
    for days, price in _DEFAULT_RATES.items():
        cursor.execute(
            "INSERT INTO ad_rates (duration_days, price) VALUES (%s, %s) "
            "ON CONFLICT (duration_days) DO NOTHING",
            (days, price)
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Shared: Bot Admins / Bot Config (same tables @Uff_GameBot uses)
# ---------------------------------------------------------------------

def is_bot_admin(user_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM bot_admins WHERE user_id=%s", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def get_config(key, default=None):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM bot_config WHERE key=%s", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default


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


# ---------------------------------------------------------------------
# Advertisement rates
# ---------------------------------------------------------------------

def get_rates():
    """[(duration_days, price), ...] ordered shortest to longest. Kept
    2-tuple shaped for backward compatibility with every existing caller
    (duration_keyboard, rates_text, /setrates) - use
    get_rates_with_currency() when you also need the currency."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT duration_days, price FROM ad_rates ORDER BY duration_days ASC")
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_rates_with_currency():
    """[(duration_days, price, currency), ...] ordered shortest to
    longest - powers /rates and the live duration/price picker."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT duration_days, price, currency FROM ad_rates ORDER BY duration_days ASC")
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_rate(duration_days):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT price FROM ad_rates WHERE duration_days=%s", (duration_days,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def get_rate_with_currency(duration_days):
    """(price, currency) for one duration, or None if no rate is
    configured for it - lets the payment flow show the amount due in
    whatever currency /setrate actually saved for that duration,
    instead of assuming NGN."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT price, currency FROM ad_rates WHERE duration_days=%s", (duration_days,))
    row = cursor.fetchone()
    conn.close()
    return row


def set_rate(duration_days, price, currency="NGN"):
    """Creates a new rate, or updates the existing one for that
    duration (price AND currency) if it already exists - matches the
    "/setrate <days> <amount> [currency]" behaviour: re-running it for a
    duration that already has a rate replaces that rate rather than
    creating a duplicate. Any duration is allowed, not just a fixed
    list. currency defaults to NGN so older callers that only pass
    (duration_days, price) keep working unchanged."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO ad_rates (duration_days, price, currency)
        VALUES (%s, %s, %s)
        ON CONFLICT (duration_days) DO UPDATE SET
            price=EXCLUDED.price,
            currency=EXCLUDED.currency
    """, (duration_days, price, currency.upper() if currency else "NGN"))
    conn.commit()
    conn.close()


def delete_rate(duration_days):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM ad_rates WHERE duration_days=%s", (duration_days,))
    removed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return removed


# ---------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------

def create_application(applicant_id, applicant_name, applicant_username,
                        business_name, ad_link, ad_type, duration_days,
                        ad_text, notes):
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO ad_applications
            (applicant_id, applicant_name, applicant_username, business_name,
             ad_link, ad_type, duration_days, ad_text, notes, status, submitted_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
        RETURNING id
    """, (applicant_id, applicant_name, applicant_username, business_name,
          ad_link, ad_type, duration_days, ad_text, notes,
          datetime.now().isoformat()))

    application_id = cursor.fetchone()[0]

    conn.commit()
    conn.close()

    return application_id


def get_application(application_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, applicant_id, applicant_name, applicant_username, business_name,
               ad_link, ad_type, duration_days, ad_text, notes, status,
               reject_reason, submitted_at, decided_at, decided_by, coupon_code, final_price
        FROM ad_applications WHERE id=%s
    """, (application_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def set_application_coupon(application_id, coupon_code, final_price):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE ad_applications SET coupon_code=%s, final_price=%s WHERE id=%s",
        (coupon_code, final_price, application_id)
    )
    conn.commit()
    conn.close()


def get_latest_approved_application(applicant_id):
    """Most recent application still sitting at status='approved' (i.e.
    awaiting payment) for this user - used by the coupon-code handler."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, applicant_id, applicant_name, applicant_username, business_name,
               ad_link, ad_type, duration_days, ad_text, notes, status,
               reject_reason, submitted_at, decided_at, decided_by, coupon_code, final_price
        FROM ad_applications
        WHERE applicant_id=%s AND status='approved'
        ORDER BY submitted_at DESC LIMIT 1
    """, (applicant_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def has_pending_application(applicant_id):
    """Anti-spam guard: True if this user already has an application
    still awaiting a decision (blocks a second simultaneous submission)."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM ad_applications WHERE applicant_id=%s AND status='pending'",
        (applicant_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return row is not None


def set_application_status(application_id, status, decided_by=None, reject_reason=None):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE ad_applications
        SET status=%s, decided_at=%s, decided_by=%s, reject_reason=%s
        WHERE id=%s
    """, (status, datetime.now().isoformat(), decided_by, reject_reason, application_id))
    conn.commit()
    conn.close()


def get_pending_application_ids():
    """Every application currently at status='pending', unpaginated -
    powers /approveall and /rejectall, which need to process the whole
    set in one go rather than a 20-at-a-time page."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM ad_applications WHERE status='pending'")
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_applications_by_status(status, offset=0, limit=20):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, applicant_id, applicant_name, applicant_username, business_name,
               ad_type, duration_days, status, submitted_at
        FROM ad_applications
        WHERE status=%s
        ORDER BY submitted_at DESC
        LIMIT %s OFFSET %s
    """, (status, limit, offset))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_applications():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, applicant_id, applicant_name, applicant_username, business_name,
               ad_link, ad_type, duration_days, status, submitted_at
        FROM ad_applications ORDER BY submitted_at DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows


def count_applications_by_status(status):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM ad_applications WHERE status=%s", (status,))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def search_applications(query):
    """Search by username, Telegram ID, business name, link, or
    application ID - whichever matches."""
    conn = connect()
    cursor = conn.cursor()

    query = query.strip().lstrip("@")

    if query.isdigit():
        cursor.execute("""
            SELECT id, applicant_id, applicant_name, applicant_username, business_name,
                   ad_type, duration_days, status, submitted_at
            FROM ad_applications
            WHERE id=%s OR applicant_id=%s
            ORDER BY submitted_at DESC
        """, (int(query), int(query)))
    else:
        like = f"%{query}%"
        cursor.execute("""
            SELECT id, applicant_id, applicant_name, applicant_username, business_name,
                   ad_type, duration_days, status, submitted_at
            FROM ad_applications
            WHERE applicant_username ILIKE %s OR business_name ILIKE %s OR ad_link ILIKE %s
            ORDER BY submitted_at DESC
        """, (like, like, like))

    rows = cursor.fetchall()
    conn.close()
    return rows


def get_advertiser_history(applicant_id):
    """(total_submitted, approved, rejected, last_submitted_at) for one advertiser."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            COUNT(*),
            COUNT(*) FILTER (WHERE status IN ('approved', 'payment_confirmed')),
            COUNT(*) FILTER (WHERE status='rejected'),
            MAX(submitted_at)
        FROM ad_applications
        WHERE applicant_id=%s
    """, (applicant_id,))
    row = cursor.fetchone()
    conn.close()
    return row if row else (0, 0, 0, None)


def get_all_advertiser_ids(status=None):
    """Distinct applicant_ids, optionally filtered by their most recent
    application's status - powers /broadcast's audience targeting."""
    conn = connect()
    cursor = conn.cursor()
    if status:
        cursor.execute(
            "SELECT DISTINCT applicant_id FROM ad_applications WHERE status=%s",
            (status,)
        )
    else:
        cursor.execute("SELECT DISTINCT applicant_id FROM ad_applications")
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------

def create_payment(application_id, file_id, file_kind):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO ad_payments (application_id, file_id, file_kind, submitted_at, status)
        VALUES (%s, %s, %s, %s, 'pending')
        RETURNING id
    """, (application_id, file_id, file_kind, datetime.now().isoformat()))
    payment_id = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return payment_id


def get_payment(payment_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, application_id, file_id, file_kind, submitted_at, status, confirmed_at, confirmed_by
        FROM ad_payments WHERE id=%s
    """, (payment_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def set_payment_status(payment_id, status, confirmed_by=None):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE ad_payments
        SET status=%s, confirmed_at=%s, confirmed_by=%s
        WHERE id=%s
    """, (status, datetime.now().isoformat(), confirmed_by, payment_id))
    conn.commit()
    conn.close()


def get_pending_payments(offset=0, limit=20):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.id, p.application_id, p.submitted_at, a.business_name, a.applicant_name
        FROM ad_payments p
        JOIN ad_applications a ON a.id = p.application_id
        WHERE p.status='pending'
        ORDER BY p.submitted_at ASC
        LIMIT %s OFFSET %s
    """, (limit, offset))
    rows = cursor.fetchall()
    conn.close()
    return rows


def count_pending_payments():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM ad_payments WHERE status='pending'")
    count = cursor.fetchone()[0]
    conn.close()
    return count


# ---------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------

def get_stats():
    """Everything /stats needs, in one round trip."""
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM ad_applications")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM ad_applications WHERE status='approved' OR status='payment_confirmed'")
    approved = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM ad_applications WHERE status='rejected'")
    rejected = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM ad_applications WHERE status='pending'")
    pending = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM ad_payments WHERE status='pending'")
    payments_waiting = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COALESCE(SUM(r.price), 0)
        FROM ad_applications a
        JOIN ad_rates r ON r.duration_days = a.duration_days
        WHERE a.status='payment_confirmed'
    """)
    revenue = cursor.fetchone()[0]

    cursor.execute("SELECT COALESCE(SUM(amount_paid), 0) FROM ad_invoices")
    revenue = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*) FROM ad_applications
        WHERE submitted_at::date = CURRENT_DATE
    """)
    today = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM ad_subscriptions WHERE status='active'")
    active_ads = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM ad_subscriptions WHERE status='scheduled'")
    scheduled_ads = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM ad_subscriptions WHERE status='expired'")
    expired_ads = cursor.fetchone()[0]

    cursor.execute("SELECT COALESCE(SUM(total_clicks), 0), COALESCE(SUM(total_views), 0) FROM ad_subscriptions")
    total_clicks, total_views = cursor.fetchone()

    conn.close()

    return {
        "total": total,
        "approved": approved,
        "rejected": rejected,
        "pending": pending,
        "payments_waiting": payments_waiting,
        "revenue": revenue,
        "today": today,
        "active_ads": active_ads,
        "scheduled_ads": scheduled_ads,
        "expired_ads": expired_ads,
        "total_clicks": total_clicks,
        "total_views": total_views,
    }


# ---------------------------------------------------------------------
# Audit log - every owner/admin action that changes something
# ---------------------------------------------------------------------

def audit(actor_id, action, details=""):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO ad_audit_log (actor_id, action, details, timestamp)
        VALUES (%s, %s, %s, %s)
    """, (actor_id, action, details, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_audit_log(limit=50):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT actor_id, action, details, timestamp
        FROM ad_audit_log ORDER BY timestamp DESC LIMIT %s
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------
# Subscriptions (scheduling, rotation, pause/resume/cancel/extend)
# ---------------------------------------------------------------------

def create_subscription(application_id, applicant_id, business_name, ad_link,
                         ad_text, ad_type, duration_days, start_at, end_at,
                         status="scheduled", renewed_from=None):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO ad_subscriptions
            (application_id, applicant_id, business_name, ad_link, ad_text, ad_type,
             duration_days, start_at, end_at, status, created_at, renewed_from)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (application_id, applicant_id, business_name, ad_link, ad_text, ad_type,
          duration_days, start_at, end_at, status, datetime.now().isoformat(), renewed_from))
    subscription_id = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return subscription_id


def get_subscription(subscription_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, application_id, applicant_id, business_name, ad_link, ad_text, ad_type,
               duration_days, start_at, end_at, status, created_at, total_views, total_clicks,
               reminder_7d_sent, reminder_3d_sent, reminder_24h_sent, reminder_expiry_sent, renewed_from
        FROM ad_subscriptions WHERE id=%s
    """, (subscription_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def get_all_subscriptions(offset=0, limit=20):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, applicant_id, business_name, duration_days, start_at, end_at, status
        FROM ad_subscriptions ORDER BY created_at DESC LIMIT %s OFFSET %s
    """, (limit, offset))
    rows = cursor.fetchall()
    conn.close()
    return rows


def count_subscriptions():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM ad_subscriptions")
    count = cursor.fetchone()[0]
    conn.close()
    return count


def set_subscription_status(subscription_id, status):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("UPDATE ad_subscriptions SET status=%s WHERE id=%s", (status, subscription_id))
    conn.commit()
    conn.close()


def extend_subscription(subscription_id, extra_days):
    """Pushes end_at forward by extra_days and returns the new end_at."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT end_at FROM ad_subscriptions WHERE id=%s", (subscription_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    new_end = (datetime.fromisoformat(row[0]) + timedelta(days=extra_days)).isoformat()
    cursor.execute("UPDATE ad_subscriptions SET end_at=%s WHERE id=%s", (new_end, subscription_id))
    conn.commit()
    conn.close()
    return new_end


def get_active_ads_for_rotation():
    """Ads eligible to actually be shown right now - status 'active' or
    'expiring_soon' (both still live) and not yet past end_at. Used by
    @Uff_GameBot's matching query - kept here too so UFFAdsBot's own
    admin views (e.g. a live preview) can use the identical definition
    of "currently showing"."""
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


def get_subscriptions_needing_activation():
    conn = connect()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    cursor.execute("""
        SELECT id FROM ad_subscriptions WHERE status='scheduled' AND start_at <= %s
    """, (now,))
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_subscriptions_needing_expiry():
    conn = connect()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    cursor.execute("""
        SELECT id FROM ad_subscriptions
        WHERE status IN ('active', 'expiring_soon') AND end_at <= %s
    """, (now,))
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_subscriptions_expiring_within(hours):
    """For marking 'expiring_soon' and for reminder jobs."""
    conn = connect()
    cursor = conn.cursor()
    now = datetime.now()
    cutoff = (now + timedelta(hours=hours)).isoformat()
    cursor.execute("""
        SELECT id, applicant_id, business_name, end_at,
               reminder_7d_sent, reminder_3d_sent, reminder_24h_sent, reminder_expiry_sent
        FROM ad_subscriptions
        WHERE status IN ('active', 'expiring_soon') AND end_at <= %s
    """, (cutoff,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def mark_reminder_sent(subscription_id, which):
    """which is one of '7d', '3d', '24h', 'expiry'."""
    column = {"7d": "reminder_7d_sent", "3d": "reminder_3d_sent",
              "24h": "reminder_24h_sent", "expiry": "reminder_expiry_sent"}[which]
    conn = connect()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE ad_subscriptions SET {column}=1 WHERE id=%s", (subscription_id,))
    conn.commit()
    conn.close()


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


def get_advertiser_subscriptions(applicant_id):
    """Powers /myads - every subscription this advertiser has ever had."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, business_name, duration_days, start_at, end_at, status, total_views, total_clicks
        FROM ad_subscriptions WHERE applicant_id=%s ORDER BY created_at DESC
    """, (applicant_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------
# Payment methods
# ---------------------------------------------------------------------

def get_payment_methods():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT name, enabled, config FROM payment_methods ORDER BY created_at ASC")
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_enabled_payment_methods():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT name, config FROM payment_methods WHERE enabled=1 ORDER BY created_at ASC")
    rows = cursor.fetchall()
    conn.close()
    return rows


def add_payment_method(name):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO payment_methods (name, enabled, config, created_at)
        VALUES (%s, 1, '{}', %s)
        ON CONFLICT (name) DO NOTHING
    """, (name, datetime.now().isoformat()))
    added = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return added


def remove_payment_method(name):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM payment_methods WHERE name=%s", (name,))
    removed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return removed


def set_payment_method_enabled(name, enabled: bool):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("UPDATE payment_methods SET enabled=%s WHERE name=%s", (1 if enabled else 0, name))
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def set_payment_method_config(name, config_json: str):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO payment_methods (name, enabled, config, created_at)
        VALUES (%s, 1, %s, %s)
        ON CONFLICT (name) DO UPDATE SET config=EXCLUDED.config
    """, (name, config_json, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def rename_payment_method(old_name, new_name):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE payment_methods SET name=%s WHERE name=%s",
        (new_name, old_name)
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def get_payment_method_config(name):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT config FROM payment_methods WHERE name=%s", (name,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------
# Coupons
# ---------------------------------------------------------------------

def create_coupon(code, discount_type, discount_value, expires_at, max_uses, created_by):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO ad_coupons (code, discount_type, discount_value, expires_at, max_uses, uses_count, created_at, created_by)
        VALUES (%s, %s, %s, %s, %s, 0, %s, %s)
        ON CONFLICT (code) DO UPDATE SET
            discount_type=EXCLUDED.discount_type,
            discount_value=EXCLUDED.discount_value,
            expires_at=EXCLUDED.expires_at,
            max_uses=EXCLUDED.max_uses
    """, (code, discount_type, discount_value, expires_at, max_uses, datetime.now().isoformat(), created_by))
    conn.commit()
    conn.close()


def get_coupon(code):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT code, discount_type, discount_value, expires_at, max_uses, uses_count
        FROM ad_coupons WHERE code=%s
    """, (code,))
    row = cursor.fetchone()
    conn.close()
    return row


def get_all_coupons():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT code, discount_type, discount_value, expires_at, max_uses, uses_count
        FROM ad_coupons ORDER BY created_at DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows


def delete_coupon(code):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM ad_coupons WHERE code=%s", (code,))
    removed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return removed


def redeem_coupon(code):
    """Atomically bumps uses_count - call only once the coupon has
    actually been applied to a confirmed payment, to avoid burning a use
    on an abandoned checkout."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("UPDATE ad_coupons SET uses_count = uses_count + 1 WHERE code=%s", (code,))
    conn.commit()
    conn.close()


def is_coupon_valid(code):
    """(valid: bool, reason_if_not: str)"""
    row = get_coupon(code)
    if not row:
        return False, "Coupon code not found."
    _, _, _, expires_at, max_uses, uses_count = row
    if expires_at:
        try:
            if datetime.fromisoformat(expires_at) < datetime.now():
                return False, "This coupon has expired."
        except ValueError:
            pass
    if max_uses is not None and uses_count >= max_uses:
        return False, "This coupon has reached its maximum number of uses."
    return True, ""


def apply_coupon_to_price(code, price):
    """Returns the discounted price, or the original price if the
    coupon is invalid (callers should check is_coupon_valid() first for
    a proper error message - this is a safe fallback)."""
    row = get_coupon(code)
    if not row:
        return price
    _, discount_type, discount_value, *_ = row
    if discount_type == "percent":
        return max(0, price - (price * discount_value // 100))
    return max(0, price - discount_value)


# ---------------------------------------------------------------------
# Promotional offers
# ---------------------------------------------------------------------

def create_offer(description, start_at, end_at, created_by):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO ad_offers (description, start_at, end_at, created_at, created_by)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
    """, (description, start_at, end_at, datetime.now().isoformat(), created_by))
    offer_id = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return offer_id


def get_active_offers():
    conn = connect()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    cursor.execute("""
        SELECT id, description, start_at, end_at
        FROM ad_offers WHERE start_at <= %s AND end_at >= %s
        ORDER BY start_at ASC
    """, (now, now))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_offers():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id, description, start_at, end_at FROM ad_offers ORDER BY start_at DESC")
    rows = cursor.fetchall()
    conn.close()
    return rows


def delete_offer(offer_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM ad_offers WHERE id=%s", (offer_id,))
    removed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return removed


# ---------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------

def create_invoice(application_id, subscription_id, business_name, advertiser_name,
                    advertiser_username, advertiser_id, duration_days, amount_paid,
                    payment_method, ad_start, ad_end, currency="NGN"):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO ad_invoices
            (application_id, subscription_id, business_name, advertiser_name, advertiser_username,
             advertiser_id, duration_days, amount_paid, payment_method, payment_date, ad_start, ad_end, currency)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (application_id, subscription_id, business_name, advertiser_name, advertiser_username,
          advertiser_id, duration_days, amount_paid, payment_method, datetime.now().isoformat(),
          ad_start, ad_end, currency))
    invoice_id = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return invoice_id


def get_invoice(invoice_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, application_id, subscription_id, business_name, advertiser_name, advertiser_username,
               advertiser_id, duration_days, amount_paid, payment_method, payment_date, ad_start, ad_end, currency
        FROM ad_invoices WHERE id=%s
    """, (invoice_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def get_advertiser_invoices(advertiser_id):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, business_name, duration_days, amount_paid, payment_method, payment_date, currency
        FROM ad_invoices WHERE advertiser_id=%s ORDER BY payment_date DESC
    """, (advertiser_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_invoices():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, business_name, advertiser_name, duration_days, amount_paid, payment_method, payment_date, currency
        FROM ad_invoices ORDER BY payment_date DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_revenue_report(period: str):
    """period is one of 'today', 'week', 'month', 'year'."""
    conn = connect()
    cursor = conn.cursor()

    days_map = {"today": 1, "week": 7, "month": 30, "year": 365}
    days = days_map.get(period, 1)

    if period == "today":
        date_clause = "payment_date::date = CURRENT_DATE"
        created_clause = "created_at::date = CURRENT_DATE"
    else:
        date_clause = f"payment_date::timestamp >= NOW() - INTERVAL '{days} days'"
        created_clause = f"created_at::timestamp >= NOW() - INTERVAL '{days} days'"

    cursor.execute(f"""
        SELECT COALESCE(SUM(amount_paid), 0), COUNT(*), COALESCE(AVG(amount_paid), 0)
        FROM ad_invoices WHERE {date_clause}
    """)
    revenue, sold, avg_sale = cursor.fetchone()

    cursor.execute("SELECT COUNT(*) FROM ad_subscriptions WHERE status='active'")
    active = cursor.fetchone()[0]

    cursor.execute(f"""
        SELECT COUNT(*) FROM ad_subscriptions WHERE renewed_from IS NOT NULL AND {created_clause}
    """)
    renewals = cursor.fetchone()[0]

    # "New" advertiser = their earliest-ever invoice falls within this period.
    cursor.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT advertiser_id, MIN(payment_date) AS first_payment
            FROM ad_invoices
            GROUP BY advertiser_id
        ) first_payments
        WHERE first_payment::timestamp >= NOW() - INTERVAL '{days} days'
    """ if period != "today" else """
        SELECT COUNT(*) FROM (
            SELECT advertiser_id, MIN(payment_date) AS first_payment
            FROM ad_invoices
            GROUP BY advertiser_id
        ) first_payments
        WHERE first_payment::date = CURRENT_DATE
    """)
    new_advertisers = cursor.fetchone()[0]

    conn.close()

    return {
        "revenue": revenue,
        "sold": sold,
        "avg_sale": round(avg_sale) if avg_sale else 0,
        "active": active,
        "renewals": renewals,
        "new_advertisers": new_advertisers,
    }