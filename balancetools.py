"""
balancetools.py - owner-only balance diagnostics and an UNDO for /repairbalances (Stage 7).

Put this file next to main.py. main.py loads it from main() inside a try/except.

  /balancetrace            overall picture: every repair run, totals, and how many players still show 'chat > global'
  /balancetrace <user id>  (or reply to the player's message) every balance row of that player and what each repair run
                           changed for them. Read-only.
  /undorepair              lists the repair runs that can be undone
  /undorepair <run id>     shows what it would restore; nothing changes until you tap the button. The old 'in this chat'
                           figures saved by /repairbalances are put back - but only where the row still holds the value the
                           repair left (a row that changed since is skipped and counted, never overwritten).

None of this touches a global balance (what /bal shows). /repairbalances never did either - it only lowered 'in this chat' rows.
"""
import asyncio
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

CONFIRM_SECONDS = 120
LOCK_KEY = 8800123                 # the same advisory lock /repairbalances uses: the two can never run at the same time
_TABLES = {"scramble_chat_stats": "earned", "chat_scores": "coins"}   # whitelist: backup table name -> chat column
_LABEL = {"scramble_chat_stats": "Word Scramble", "chat_scores": "Word Game"}
_PENDING = {}                      # token -> (run_id, deadline)
D = None


# ---------------------------------------------------------------- database (all in this file)
def _runs(cursor):
    cursor.execute("""
        SELECT r.id, r.applied_by, r.created_at, r.users_changed, r.rows_changed, r.total_removed,
               u.created_at, u.rows_restored, u.rows_skipped
        FROM balance_repair_runs r LEFT JOIN balance_repair_undo u ON u.run_id = r.id ORDER BY r.id
    """)
    return cursor.fetchall()


def overview():
    conn = D.db.connect()
    try:
        cur = conn.cursor()
        runs = _runs(cur)
        cur.execute("SELECT COUNT(*), COALESCE(SUM(coins), 0) FROM global_scores")
        wg_n, wg_sum = cur.fetchone()
        cur.execute("SELECT COUNT(*), COALESCE(SUM(earned), 0) FROM scramble_global_stats")
        sc_n, sc_sum = cur.fetchone()
        cur.execute("SELECT COALESCE(SUM(coins), 0) FROM chat_scores")
        wg_chat = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(earned), 0) FROM scramble_chat_stats")
        sc_chat = cur.fetchone()[0]
        conn.rollback()
        return {"runs": runs, "wg_n": wg_n, "wg_sum": wg_sum, "sc_n": sc_n, "sc_sum": sc_sum, "wg_chat": wg_chat, "sc_chat": sc_chat}
    finally:
        conn.close()


def user_trace(user_id):
    conn = D.db.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT coins FROM global_scores WHERE user_id=%s", (user_id,))
        r = cur.fetchone(); wg = r[0] if r else None
        cur.execute("SELECT earned FROM scramble_global_stats WHERE user_id=%s", (user_id,))
        r = cur.fetchone(); sc = r[0] if r else None
        cur.execute("SELECT chat_id, coins FROM chat_scores WHERE user_id=%s ORDER BY coins DESC LIMIT 12", (user_id,))
        wg_rows = cur.fetchall()
        cur.execute("SELECT chat_id, earned FROM scramble_chat_stats WHERE user_id=%s ORDER BY earned DESC LIMIT 12", (user_id,))
        sc_rows = cur.fetchall()
        cur.execute("SELECT run_id, tbl, chat_id, old_value, new_value FROM balance_repair_backup WHERE user_id=%s ORDER BY run_id, id", (user_id,))
        backup = cur.fetchall()
        conn.rollback()
        return {"wg": wg, "sc": sc, "wg_rows": wg_rows, "sc_rows": sc_rows, "backup": backup}
    finally:
        conn.close()


def _plan(cursor, run_id, lock):
    """(restorable, skipped, total_to_restore) for one run, looking at the rows as they are NOW."""
    cursor.execute("SELECT tbl, user_id, chat_id, old_value, new_value FROM balance_repair_backup WHERE run_id=%s ORDER BY id", (run_id,))
    todo, skipped, amount = [], 0, 0
    for tbl, uid, cid, old, new in cursor.fetchall():
        col = _TABLES.get(tbl)
        if col is None:
            skipped += 1
            continue
        cursor.execute(f"SELECT {col} FROM {tbl} WHERE user_id=%s AND chat_id=%s" + (" FOR UPDATE" if lock else ""), (uid, cid))
        row = cursor.fetchone()
        if row is not None and row[0] == new:
            todo.append((tbl, col, uid, cid, old, new))
            amount += old - new
        else:
            skipped += 1
    return todo, skipped, amount


def undo_preview(run_id):
    conn = D.db.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, created_at, rows_changed, total_removed FROM balance_repair_runs WHERE id=%s", (run_id,))
        run = cur.fetchone()
        if not run:
            conn.rollback()
            return {"status": "missing"}
        cur.execute("SELECT 1 FROM balance_repair_undo WHERE run_id=%s", (run_id,))
        if cur.fetchone():
            conn.rollback()
            return {"status": "done", "run": run}
        todo, skipped, amount = _plan(cur, run_id, False)
        conn.rollback()
        return {"status": "ok", "run": run, "rows": len(todo), "skipped": skipped, "amount": amount}
    finally:
        conn.close()


def undo_apply(run_id, applied_by):
    """ONE transaction. Marks the run as undone first (primary key = it can only ever happen once), then puts the saved
    values back where the row still holds the value the repair left."""
    conn = D.db.connect()
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            cur.execute("SELECT 1 FROM balance_repair_runs WHERE id=%s", (run_id,))
            if not cur.fetchone():
                conn.rollback()
                return {"status": "missing"}
            cur.execute("INSERT INTO balance_repair_undo (run_id, applied_by, created_at, rows_restored, rows_skipped) "
                        "VALUES (%s, %s, %s, 0, 0) ON CONFLICT DO NOTHING", (run_id, applied_by, datetime.now().isoformat()))
            if cur.rowcount != 1:
                conn.rollback()
                return {"status": "done"}
            todo, skipped, amount = _plan(cur, run_id, True)
            for tbl, col, uid, cid, old, new in todo:
                cur.execute(f"UPDATE {tbl} SET {col}=%s WHERE user_id=%s AND chat_id=%s AND {col}=%s", (old, uid, cid, new))
            cur.execute("UPDATE balance_repair_undo SET rows_restored=%s, rows_skipped=%s WHERE run_id=%s", (len(todo), skipped, run_id))
            conn.commit()
            return {"status": "ok", "rows": len(todo), "skipped": skipped, "amount": amount}
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------- commands
def _n(v):
    return D.fmt_coins(v if v is not None else 0)


def _date(s):
    return (s or "?")[:16].replace("T", " ")


async def _owner_only(update):
    if not D.is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 This command is for the bot owner.")
        return False
    return True


async def trace_command(update, context):
    if not await _owner_only(update):
        return
    msg = update.message
    target = None
    if context.args and context.args[0].lstrip("-").isdigit():
        target = int(context.args[0])
    elif msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user.id
    if target is None:
        o = await asyncio.to_thread(overview)
        lines = ["🔎 <b>Balance trace</b>", "",
                 f"Word Game global balances: {o['wg_n']:,} players, total {D.DOLLAR}{_n(o['wg_sum'])}",
                 f"Word Scramble global balances: {o['sc_n']:,} players, total {D.DOLLAR}{_n(o['sc_sum'])}",
                 f"'In this chat' rows (all groups): Word Game {D.DOLLAR}{_n(o['wg_chat'])}, Word Scramble {D.DOLLAR}{_n(o['sc_chat'])}", ""]
        if not o["runs"]:
            lines.append("✅ /repairbalances has <b>never been applied</b>, so no repair changed anything.")
        for rid, by, at, users, rows, removed, u_at, u_rows, u_skip in o["runs"]:
            state = f" - ↩️ undone {_date(u_at)} ({u_rows} restored, {u_skip} skipped)" if u_at else ""
            lines.append(f"Repair #{rid} ({_date(at)}): {rows} row(s), {users} player(s), {D.DOLLAR}{_n(removed)} lowered{state}")
        lines += ["", "<i>/balancetrace &lt;user id&gt; shows one player. /undorepair &lt;run id&gt; undoes a repair. "
                      "Global balances (what /bal shows) are never changed by a repair or an undo.</i>"]
        await D.reply_html_in_parts(msg, "\n".join(lines))
        return
    t = await asyncio.to_thread(user_trace, target)
    total = await asyncio.to_thread(D.db.get_combined_balance, target)
    lines = [f"🔎 <b>Balance trace</b> - <code>{target}</code>", "",
             f"/bal shows: {D.DOLLAR}{_n(total)}",
             f"Word Scramble global: {D.DOLLAR}{_n(t['sc'])}   Word Game global: {D.DOLLAR}{_n(t['wg'])}", ""]
    for label, rows in (("Word Scramble", t["sc_rows"]), ("Word Game", t["wg_rows"])):
        lines.append(f"<b>{label} - 'in this chat' rows</b>")
        lines += [f"<code>{c}</code>: {D.DOLLAR}{_n(v)}" for c, v in rows] or ["(none)"]
        lines.append("")
    lines.append("<b>Changed by /repairbalances</b>")
    lines += [f"#{r} {_LABEL.get(tb, tb)} <code>{c}</code>: {D.DOLLAR}{_n(o_)} → {D.DOLLAR}{_n(n_)}" for r, tb, c, o_, n_ in t["backup"]] or ["(nothing)"]
    await D.reply_html_in_parts(msg, "\n".join(lines))


async def undo_command(update, context):
    if not await _owner_only(update):
        return
    msg = update.message
    arg = (context.args or [""])[0]
    if not arg.isdigit():
        o = await asyncio.to_thread(overview)
        open_runs = [r for r in o["runs"] if not r[6]]
        if not open_runs:
            await msg.reply_text("Nothing to undo: no repair run exists that is still applied. (See /balancetrace.)")
            return
        lines = ["↩️ <b>Repair runs you can undo</b>", ""]
        lines += [f"#{r[0]} ({_date(r[2])}): {r[4]} row(s), {D.DOLLAR}{_n(r[5])} lowered" for r in open_runs]
        lines += ["", "Use /undorepair &lt;run id&gt;, e.g. /undorepair " + str(open_runs[-1][0])]
        await msg.reply_html("\n".join(lines))
        return
    run_id = int(arg)
    pv = await asyncio.to_thread(undo_preview, run_id)
    if pv["status"] == "missing":
        await msg.reply_text(f"❌ There is no repair run #{run_id}. See /balancetrace.")
        return
    if pv["status"] == "done":
        await msg.reply_text(f"Repair #{run_id} was already undone.")
        return
    if pv["rows"] == 0:
        await msg.reply_text(f"Nothing can be restored for repair #{run_id}: all {pv['skipped']} row(s) changed since the repair.")
        return
    now = asyncio.get_running_loop().time()
    for k in [k for k, (_r, d) in _PENDING.items() if d < now]:
        _PENDING.pop(k, None)
    token = os.urandom(4).hex()
    _PENDING[token] = (run_id, now + CONFIRM_SECONDS)
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Restore", callback_data=f"ur:y:{token}"),
                                InlineKeyboardButton("❌ Cancel", callback_data=f"ur:n:{token}")]])
    await msg.reply_html(
        f"↩️ <b>Undo repair #{run_id}</b>\n\n"
        f"{pv['rows']} 'in this chat' row(s) will go back to the value they had before the repair "
        f"(+{D.DOLLAR}{_n(pv['amount'])} in total).\n"
        f"{pv['skipped']} row(s) changed since the repair and will be left alone.\n"
        f"Global balances are not touched. Tap within {CONFIRM_SECONDS} seconds.", reply_markup=kb)


async def undo_button(update, context):
    query = update.callback_query
    try:
        _p, action, token = query.data.split(":")
    except ValueError:
        await query.answer()
        return
    if not D.is_owner(query.from_user.id):
        await query.answer("Only the bot owner can use these buttons.", show_alert=True)
        return
    item = _PENDING.pop(token, None)                  # claimed before any await: a double tap can never run twice
    if item is None or asyncio.get_running_loop().time() > item[1]:
        await query.answer("⌛ Expired - run /undorepair again.", show_alert=True)
        try:
            await query.edit_message_text("⌛ Expired - nothing was changed.")
        except Exception:
            pass
        return
    if action == "n":
        await query.answer("Cancelled.")
        await query.edit_message_text("❌ Cancelled - nothing was changed.")
        return
    await query.answer("Working...")
    try:
        res = await asyncio.to_thread(undo_apply, item[0], query.from_user.id)
    except Exception:
        logger.exception("undo repair failed")
        text = "❌ The undo failed and NOTHING was changed. Check the logs."
    else:
        if res["status"] == "ok":
            text = (f"✅ Repair #{item[0]} undone: {res['rows']} row(s) restored (+{D.DOLLAR}{_n(res['amount'])}), "
                    f"{res['skipped']} left alone because they changed since.")
        elif res["status"] == "done":
            text = f"Repair #{item[0]} was already undone."
        else:
            text = "❌ That repair run does not exist."
    try:
        await query.edit_message_text(text)
    except Exception:
        pass


def install(app, deps):
    """Called once from main.py BEFORE the generic button handler, so the Restore button is recognised."""
    global D
    D = deps
    from telegram.ext import CallbackQueryHandler, CommandHandler
    app.add_handler(CommandHandler("balancetrace", trace_command))
    app.add_handler(CommandHandler("undorepair", undo_command))
    app.add_handler(CallbackQueryHandler(undo_button, pattern=r"^ur:[yn]:[0-9a-f]+$"))
