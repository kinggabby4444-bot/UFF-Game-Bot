"""
patch_boot.py - one-shot patcher for UFF-Game-Bot (edits main.py AND database.py).

Usage (put this file in the same folder as main.py and database.py):
    python patch_boot.py

What it does
  A) Every time the bot starts (= every deploy / restart), BEFORE anything is restored:
       * every running Word Scramble round (Normal Mode) is ended,
       * Slow Mode is turned off in every group,
       * Automatic Mode is turned off in every group,
     all in ONE database transaction. Because the old bot instance (which is still running for a short
     time during a deploy) checks the database before every next round, its chains stop too - so two Slow Modes
     can no longer run in one group.
     Each group that had something running gets ONE short message saying what was stopped.
     Switch it off with the Render environment variable  FORCE_END_ON_BOOT=0  (old behaviour comes back).
  B) Tournament / Elimination / Hack:
       * they are resumed after the restart exactly as before (nothing changes when that works),
       * a tournament that used to be left "paused" waiting for /resumetournament after a restart is now
         resumed automatically,
       * if a tournament, elimination or hack still cannot be continued after 3 tries (about 40 seconds),
         it is cancelled and EVERY player is refunded their join fee (the host too).

Safety: every edit needs its original text to be found EXACTLY once, otherwise NOTHING is changed.
Both files must compile and contain the new functions before either is written. Backups: main.py.bak2 and
database.py.bak2 (your earlier .bak files are not touched). Running it twice is harmless.
"""
import ast
import os
import py_compile
import shutil
import sys
import tempfile

EDITS = {"main.py": [], "database.py": []}


def E(path, name, old, new, count=1, optional=False):
    EDITS[path].append({"name": name, "old": old, "new": new, "count": count, "optional": optional})


# =====================================================================================
# database.py
# =====================================================================================
E("database.py", "force_end_scramble_everywhere",
  "def get_all_active_scramble_chats():",
  r'''def force_end_scramble_everywhere():
    """Used once at every boot. In ONE transaction: end EVERY Word Scramble round, turn Slow Mode off and turn
    Automatic Mode off in every chat. Tournaments, Eliminations and Hacks are NOT touched (their rounds live in
    their own tables). Returns {chat_id: {"word": str|None, "slow_seconds": int|None, "auto": bool}} for every
    chat that had something running (used for the one-line notice)."""
    conn = connect()
    try:
        cursor = conn.cursor()
        out = {}

        def info(chat_id):
            return out.setdefault(chat_id, {"word": None, "slow_seconds": None, "auto": False})

        cursor.execute("DELETE FROM scramble_games RETURNING chat_id, word, round_active")
        for chat_id, word, active in cursor.fetchall():
            if active:
                info(chat_id)["word"] = word
        cursor.execute(
            "UPDATE scramble_slowmode SET enabled=0, next_round_due_at=NULL "
            "WHERE enabled=1 RETURNING chat_id, interval_seconds"
        )
        for chat_id, interval in cursor.fetchall():
            info(chat_id)["slow_seconds"] = interval or 0
        cursor.execute("UPDATE scramble_slowmode SET next_round_due_at=NULL WHERE next_round_due_at IS NOT NULL")
        cursor.execute(
            "UPDATE scramble_auto_mode SET auto_enabled=0, manual_streak=0, miss_streak=0 "
            "WHERE auto_enabled=1 RETURNING chat_id"
        )
        for (chat_id,) in cursor.fetchall():
            info(chat_id)["auto"] = True
        conn.commit()
        return out
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def get_all_active_scramble_chats():''')

# =====================================================================================
# main.py
# =====================================================================================

# ---- A) end Normal / Slow / Automatic mode on every boot ----------------------------------
E("main.py", "boot cleanup + notice functions",
  "def restore_stale_scramble_rounds(job_queue):",
  r'''def force_end_modes_on_boot():
    """At every start (= every deploy / restart), before anything is restored: end all Word Scramble rounds
    and turn Slow Mode and Automatic Mode off in every chat. That also stops the OLD instance's chains
    (it checks the database before every next round), so two Slow Modes can never run in one group.
    Tournaments, Eliminations and Hacks are not touched here (they resume, or are refunded if they can't).
    Set FORCE_END_ON_BOOT=0 in the environment to switch this off. Returns {chat_id: info} for the notices."""
    if os.environ.get("FORCE_END_ON_BOOT", "1") == "0":
        print("GAME BOT: FORCE_END_ON_BOOT=0 - Slow/Automatic Mode are left as they were", flush=True)
        return {}
    try:
        cleaned = db.force_end_scramble_everywhere()
    except Exception as e:
        print(f"GAME BOT: could not end the Word Scramble modes on boot: {e}", flush=True)
        return {}
    print(f"GAME BOT: ended Word Scramble rounds / Slow Mode / Automatic Mode in {len(cleaned)} chat(s)", flush=True)
    return cleaned


def schedule_boot_notices(job_queue, cleaned):
    if job_queue is None or not cleaned:
        return
    job_queue.run_once(boot_notice_callback, 8, name="boot_notice", data=cleaned)


async def boot_notice_callback(context: ContextTypes.DEFAULT_TYPE):
    """One short message in every chat where something was stopped by the restart."""
    cleaned = context.job.data or {}
    sent = 0
    for chat_id, info in cleaned.items():
        word, slow, auto = info.get("word"), info.get("slow_seconds"), info.get("auto")
        if not (word or slow is not None or auto):
            continue
        lines = ["🔄 <b>The bot was just updated and restarted.</b>", ""]
        if word:
            lines.append(f"🛑 The Word Scramble round that was running here was ended. The word was: <b>{esc(word)}</b>")
        if slow is not None:
            lines.append(
                f"🐢 Slow Mode ({_format_duration_words(slow)}) was turned off." if slow
                else "🐢 Slow Mode was turned off."
            )
        if auto:
            lines.append("🤖 Automatic Mode was turned off.")
        lines.append("")
        lines.append(
            "Start a new round with /wordscramble or /playgame. Admins can turn Slow Mode or Automatic Mode "
            "back on with /slowmode on &lt;time&gt; and /scrambleautomaticmode on."
        )
        try:
            await safe_send(context.bot, chat_id, text="\n".join(lines), parse_mode="HTML")
            sent += 1
        except Exception as ex:
            logger.info(f"boot notice to {chat_id} not delivered: {ex}")
        await asyncio.sleep(0.1)
    logger.info(f"boot notices sent to {sent} chat(s)")


def restore_stale_scramble_rounds(job_queue):''')

E("main.py", "boot cleanup is called first",
  "restore_stale_scramble_rounds(app.job_queue)",
  "_boot_cleaned = force_end_modes_on_boot()\n"
  "        schedule_boot_notices(app.job_queue, _boot_cleaned)\n"
  "        restore_stale_scramble_rounds(app.job_queue)")

# ---- B) tournaments: resume by themselves, refund if that is impossible -----------------------
E("main.py", "tournament boot resume or refund",
  "async def tournament_paused_notice_callback(context: ContextTypes.DEFAULT_TYPE):",
  r'''BOOT_RESUME_ATTEMPTS = 3           # tries after a restart before a game that can't continue is refunded
BOOT_RESUME_RETRY_SECONDS = 20


def schedule_boot_tournament_resumes(job_queue):
    """After the normal restore pass: every tournament that is still 'paused' (including the ones the restore
    pass just paused because the downtime was long) is resumed automatically a few seconds later."""
    if job_queue is None:
        return
    try:
        open_ts = db.tournament_get_all_open()
    except Exception:
        logger.exception("could not load open tournaments for the automatic resume")
        return
    for t in open_ts:
        if t["status"] == "paused":
            _schedule_tournament_job(
                job_queue, tournament_paused_notice_callback, _tournament_paused_job_name(t["id"]),
                6, t["chat_id"], {"tid": t["id"], "attempt": 1},
            )


async def tournament_paused_notice_callback(context: ContextTypes.DEFAULT_TYPE):
    """After a restart / deploy: continue the tournament by itself. If it still cannot be continued after
    BOOT_RESUME_ATTEMPTS tries, cancel it and refund every player."""
    data = context.job.data or {}
    tid, attempt = data.get("tid"), data.get("attempt", 1)
    if tid is None:
        return
    t = None
    code = "failed"
    try:
        t = await asyncio.to_thread(db.tournament_get, tid)
        if not t or t["status"] not in ("paused", "active"):
            return
        code, _info = await _tournament_resume_one(context, t)
    except Exception:
        logger.exception(f"tournament {tid}: automatic resume after the restart failed")
        code = "failed"
    if code != "failed":
        return
    chat_id = t["chat_id"] if t else getattr(context.job, "chat_id", None)
    if attempt < BOOT_RESUME_ATTEMPTS:
        _schedule_tournament_job(
            context.job_queue, tournament_paused_notice_callback, _tournament_paused_job_name(tid),
            BOOT_RESUME_RETRY_SECONDS, chat_id, {"tid": tid, "attempt": attempt + 1},
        )
        return
    try:
        await _tournament_abort_and_announce(
            context, tid, chat_id,
            "Tournament cancelled - it couldn't be continued after the bot update.",
            dm_players=True, group_title=await asyncio.to_thread(_group_title_sync, chat_id),
        )
    except Exception:
        logger.exception(f"tournament {tid}: refund after a failed automatic resume failed (the 24h check will retry)")


async def _tournament_paused_notice_callback_old(context: ContextTypes.DEFAULT_TYPE):''')

E("main.py", "boot tournament resume is scheduled",
  "restore_stale_tournaments(app.job_queue)",
  "restore_stale_tournaments(app.job_queue)\n"
  "        schedule_boot_tournament_resumes(app.job_queue)")

# ---- B) eliminations ---------------------------------------------------------------------------
E("main.py", "elimination boot resume or refund",
  "async def elim_restore_callback(context: ContextTypes.DEFAULT_TYPE):",
  r'''async def elim_restore_callback(context: ContextTypes.DEFAULT_TYPE):
    """After a restart / deploy: continue the Elimination exactly where it stopped. If that keeps failing
    (3 tries), cancel it and refund every join fee."""
    data = context.job.data or {}
    eid, attempt = data.get("eid"), data.get("attempt", 1)
    if eid is None:
        return
    chat_id = getattr(context.job, "chat_id", None)
    failed = False
    try:
        e = await asyncio.to_thread(db.elim_get, eid)
        if not e or e["status"] not in ("lobby", "active"):
            return
        chat_id = e["chat_id"]
        code, _info = await _elim_resume_one(
            context, e, force=True,
            header="⚙️ <b>I'm back!</b> The Elimination continues - nobody lost time.",
        )
        failed = (code == "failed")
    except Exception:
        logger.exception(f"elimination {eid}: restore failed")
        failed = True
    if not failed:
        return
    if attempt < BOOT_RESUME_ATTEMPTS:
        context.job_queue.run_once(
            elim_restore_callback, BOOT_RESUME_RETRY_SECONDS, chat_id=chat_id,
            name=_elim_restore_job_name(eid), data={"eid": eid, "attempt": attempt + 1},
        )
        return
    try:
        await _elim_cancel_and_announce(
            context, eid, "Elimination cancelled - it couldn't be continued after the bot update."
        )
    except Exception:
        logger.exception(f"elimination {eid}: refund after a failed restore failed (the watchdog will retry)")


async def _elim_restore_callback_old(context: ContextTypes.DEFAULT_TYPE):''')

# ---- B) hacks ------------------------------------------------------------------------------------
E("main.py", "hack boot resume or refund",
  "async def hack_restore_callback(context: ContextTypes.DEFAULT_TYPE):",
  r'''async def hack_restore_callback(context: ContextTypes.DEFAULT_TYPE):
    """After a restart / deploy: continue the Hack where it stopped. If that keeps failing (3 tries),
    cancel it and refund every player (the host included)."""
    data = context.job.data or {}
    hid, attempt = data.get("hid"), data.get("attempt", 1)
    if hid is None:
        return
    chat_id = getattr(context.job, "chat_id", None)
    failed = False
    try:
        h = await asyncio.to_thread(db.hack_get, hid)
        if not h or h["status"] not in ("lobby", "active"):
            return
        chat_id = h["chat_id"]
        await _hack_resume_one(context, h, force=True)
    except Exception:
        logger.exception(f"hack {hid}: restore failed")
        failed = True
    if not failed:
        return
    if attempt < BOOT_RESUME_ATTEMPTS:
        context.job_queue.run_once(
            hack_restore_callback, BOOT_RESUME_RETRY_SECONDS, chat_id=chat_id,
            name=_hack_restore_job_name(hid), data={"hid": hid, "attempt": attempt + 1},
        )
        return
    try:
        await _hack_cancel_and_announce(
            context, hid, "Hack cancelled - it couldn't be continued after the bot update.", keep_host_fee=False
        )
    except Exception:
        logger.exception(f"hack {hid}: refund after a failed restore failed (the watchdog will retry)")


async def _hack_restore_callback_old(context: ContextTypes.DEFAULT_TYPE):''')

# ---- help (optional) ---------------------------------------------------------------------------
E("main.py", "fullhelp: restart note",
  "Only one round runs per chat at a time.",
  "Only one round runs per chat at a time. When the bot is updated or restarted, running rounds, Slow Mode and "
  "Automatic Mode are ended - just start them again.",
  optional=True)


# =====================================================================================
# engine (same rules as before)
# =====================================================================================
def apply_edits(path, text):
    report, failed = [], False
    for e in EDITS[path]:
        name, old, new, count, optional = e["name"], e["old"], e["new"], e["count"], e["optional"]
        if new in text:
            report.append(f"[skip] {name}: already applied")
            continue
        n = text.count(old)
        ok = (n >= 1) if count == "any" else (n == count)
        if not ok:
            if optional:
                report.append(f"[warn] {name}: wording not found exactly (found {n}) - left as it is (optional)")
                continue
            report.append(f"[FAIL] {name}: expected the original text {count} time(s), found {n}")
            failed = True
            continue
        text = text.replace(old, new)
        report.append(f"[ok]   {name}")
    return text, report, failed


def defined_names(src):
    tree = ast.parse(src)
    return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def main():
    new_text, any_failed = {}, False
    for path in EDITS:
        if not os.path.exists(path):
            print(f"[FAIL] {path} not found - run this next to main.py and database.py. Nothing was changed.")
            sys.exit(1)
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        text, report, failed = apply_edits(path, src)
        print(f"--- {path}")
        print("\n".join(report))
        new_text[path] = text
        any_failed = any_failed or failed
    if any_failed:
        print("\nNothing was changed (at least one required edit did not match). Send me this output.")
        sys.exit(1)

    with tempfile.TemporaryDirectory() as d:
        for path, text in new_text.items():
            tmp = os.path.join(d, path)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                py_compile.compile(tmp, doraise=True)
            except py_compile.PyCompileError as ex:
                print(f"[FAIL] patched {path} does not compile ({ex}). Nothing was changed.")
                sys.exit(1)
    main_names = defined_names(new_text["main.py"])
    need = {"force_end_modes_on_boot", "schedule_boot_notices", "boot_notice_callback",
            "schedule_boot_tournament_resumes", "tournament_paused_notice_callback",
            "elim_restore_callback", "hack_restore_callback"}
    if need - main_names or "force_end_scramble_everywhere" not in defined_names(new_text["database.py"]):
        print(f"[FAIL] sanity check failed (missing: {sorted(need - main_names)}). Nothing was changed.")
        sys.exit(1)

    for path, text in new_text.items():
        shutil.copyfile(path, path + ".bak2")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    print("\nDone. Backups saved as main.py.bak2 and database.py.bak2.")
    print("IMPORTANT: commit and push BOTH main.py and database.py together.")


if __name__ == "__main__":
    main()