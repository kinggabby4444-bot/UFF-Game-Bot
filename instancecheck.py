"""
instancecheck.py - finds out whether a SECOND copy of the bot (another Render service, another host, an old
deployment) is running with the same bot token. Owner-only, read-only apart from one small log row per start.

Why: two copies share one Telegram token but each keeps its own timers and (if its DATABASE_URL differs) its own
database. Telegram sends the players' messages to whichever copy registered the webhook last, so you would see
old balances coming back, wins that are "not recorded", and a Slow Mode game that is sent by one copy but never sees
the answers (which go to the other copy).

  /dbwho   shows this copy (service, instance, commit, database), who holds the Telegram webhook right now, and the
           last starts with the balance totals each start saw.
  Automatic: every 5 minutes this copy checks the webhook. If it points somewhere else than this service's own
           address, the owner gets ONE private message naming the other address. Nothing is changed automatically.
"""
import asyncio
import logging
import os
import socket
import time
from datetime import datetime
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

CHECK_EVERY = 300
WEBHOOK_PATH = "/webhook/game"            # same as GAME_WEBHOOK_PATH in server.py
_STARTED = time.time()
_ALERTED = {"url": None}
D = None


def _expected_url():
    base = os.environ.get("RENDER_EXTERNAL_URL")
    return (base.rstrip("/") + WEBHOOK_PATH) if base else None


def _db_target():
    """host:port/dbname of DATABASE_URL - never the user or the password."""
    try:
        u = urlparse(os.environ.get("DATABASE_URL", ""))
        return f"{u.hostname}:{u.port or 5432}{u.path}"
    except Exception:
        return "?"


def _who():
    return {
        "service": os.environ.get("RENDER_SERVICE_NAME", "?"),
        "instance": os.environ.get("RENDER_INSTANCE_ID", socket.gethostname()),
        "commit": (os.environ.get("RENDER_GIT_COMMIT") or "?")[:7],
        "ext_url": os.environ.get("RENDER_EXTERNAL_URL", "?"),
    }


def _db_facts():
    """Facts about the database this copy is really connected to + the balance totals it sees."""
    conn = D.db.connect()
    out = {}
    try:
        cur = conn.cursor()
        for key, sql in (("db", "SELECT current_database()"),
                         ("server", "SELECT split_part(version(), ' ', 2)"),
                         ("pg_start", "SELECT pg_postmaster_start_time()::text"),
                         ("sys_id", "SELECT system_identifier::text FROM pg_control_system()"),
                         ("now", "SELECT now()::text")):
            try:
                cur.execute(sql)
                out[key] = cur.fetchone()[0]
            except Exception:
                conn.rollback()
                out[key] = "n/a"
        cur.execute("SELECT COUNT(*), COALESCE(SUM(coins), 0) FROM global_scores")
        out["players"], out["wg_sum"] = cur.fetchone()
        cur.execute("SELECT COALESCE(SUM(earned), 0) FROM scramble_global_stats")
        out["sc_sum"] = cur.fetchone()[0]
        conn.rollback()
        return out
    finally:
        conn.close()


def record_boot():
    f, w = _db_facts(), _who()
    conn = D.db.connect()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO instance_boots (booted_at, service, instance, commit_id, ext_url, db_target, db_name, sys_id, "
                    "players, wg_sum, sc_sum) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (datetime.now().isoformat(), w["service"], w["instance"], w["commit"], w["ext_url"], _db_target(),
                     f["db"], f["sys_id"], f["players"], f["wg_sum"], f["sc_sum"]))
        conn.commit()
    finally:
        conn.close()


def recent_boots(limit=8):
    conn = D.db.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT booted_at, service, instance, commit_id, db_target, sys_id, players, wg_sum, sc_sum "
                    "FROM instance_boots ORDER BY id DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
        conn.rollback()
        return rows
    finally:
        conn.close()


async def _webhook_url(bot):
    info = await bot.get_webhook_info()
    return info.url or "", info


async def webhook_job(context):
    expected = _expected_url()
    if not expected:
        return
    try:
        url, _info = await _webhook_url(context.bot)
    except Exception:
        logger.exception("instancecheck: could not read the webhook")
        return
    if url and url != expected:
        if _ALERTED["url"] != url:
            _ALERTED["url"] = url
            host = urlparse(url).hostname or url
            await D.safe_send(
                context.bot, D.owner_id, parse_mode="HTML",
                text=("⚠️ <b>Another copy of the bot holds the Telegram webhook.</b>\n\n"
                      f"This service ({D.esc(_who()['service'])}) expects:\n<code>{D.esc(expected)}</code>\n"
                      f"Telegram currently sends the players' messages to:\n<code>{D.esc(url)}</code>\n\n"
                      f"That means the service at <b>{D.esc(host)}</b> is also running with this bot's token. "
                      "It has its own timers (and maybe its own database), which explains old balances coming back and a "
                      "Slow Mode game that is sent but never sees answers. Delete or suspend that service on Render "
                      "(or wherever it runs), then deploy this one again.")),
    elif _ALERTED["url"] and url == expected:
        _ALERTED["url"] = None
        await D.safe_send(context.bot, D.owner_id, text="✅ The Telegram webhook points to this service again.")


async def boot_job(context):
    try:
        await asyncio.to_thread(record_boot)
    except Exception:
        logger.exception("instancecheck: could not record this start")


def _n(v):
    return D.fmt_coins(v if v is not None else 0)


async def dbwho_command(update, context):
    if not D.is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 This command is for the bot owner.")
        return
    w = _who()
    expected = _expected_url()
    try:
        url, info = await _webhook_url(context.bot)
        if expected is None:
            hook = f"{D.esc(url)}  (RENDER_EXTERNAL_URL is not set here, cannot compare)"
        elif url == expected:
            hook = f"✅ {D.esc(url)}"
        else:
            hook = f"⚠️ <b>DIFFERENT</b>: {D.esc(url)}\n   this service expects {D.esc(expected)}"
        hook += f"\n   waiting updates: {info.pending_update_count}"
        if info.last_error_message:
            hook += f"\n   last error: {D.esc(info.last_error_message)}"
    except Exception as e:
        hook = f"could not read it ({D.esc(str(e))})"
    try:
        f = await asyncio.to_thread(_db_facts)
        dbt = (f"{D.esc(_db_target())}\n   database {D.esc(f['db'])}, Postgres {D.esc(f['server'])}, system id {D.esc(f['sys_id'])}\n"
               f"   started {D.esc(f['pg_start'])[:19]}, now {D.esc(f['now'])[:19]}\n"
               f"   {f['players']:,} players, Word Game total {D.DOLLAR}{_n(f['wg_sum'])}, Word Scramble total {D.DOLLAR}{_n(f['sc_sum'])}")
    except Exception as e:
        dbt = f"could not read it ({D.esc(str(e))})"
    up = int(time.time() - _STARTED)
    lines = ["🧭 <b>Which copy am I?</b>", "",
             f"Service: {D.esc(w['service'])}   Instance: {D.esc(w['instance'])}",
             f"Commit: {D.esc(w['commit'])}   Running for {up // 3600}h {up % 3600 // 60}m",
             f"Address: {D.esc(w['ext_url'])}", "",
             "<b>Telegram webhook</b>", hook, "",
             "<b>Database this copy uses</b>", dbt, ""]
    try:
        boots = await asyncio.to_thread(recent_boots)
    except Exception:
        boots = []
    lines.append("<b>Recent starts</b> (newest first; totals each start saw)")
    for at, svc, inst, commit, target, sysid, players, wg, sc in boots:
        lines.append(f"{D.esc((at or '')[:16].replace('T', ' '))} · {D.esc(svc or '?')} {D.esc((inst or '?')[:8])} · {D.esc(commit or '?')} · "
                     f"{D.esc((target or '?').split('/')[0][-22:])} · {players:,} players · {D.DOLLAR}{_n((wg or 0) + (sc or 0))}")
    if not boots:
        lines.append("(none recorded yet)")
    lines += ["", "<i>If a start shows a different database, or totals that fell between two starts, something replaced "
                  "or restored the data. If the webhook is DIFFERENT, a second service is answering your players.</i>"]
    await D.reply_html_in_parts(update.message, "\n".join(lines))


def install(app, deps):
    global D
    D = deps
    from telegram.ext import CommandHandler
    app.add_handler(CommandHandler("dbwho", dbwho_command))
    if app.job_queue is not None:
        app.job_queue.run_once(boot_job, 12, name="ic_boot")
        app.job_queue.run_repeating(webhook_job, interval=CHECK_EVERY, first=90, name="ic_webhook")
