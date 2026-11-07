"""
scramblesession.py - /endscramblesession: ends the Word Scramble session of THIS group for good.

Use it in the group (reply to the scramble message if you like - the reply is optional, the command always acts on
this group's session). It:
  * closes the running round (nobody is charged or paid),
  * switches Slow Mode and Automatic Mode OFF for this group and cancels their pending timers, so nothing is sent again
    by itself - not after a restart either, because the switches are stored in the database,
  * tells the group how to start again: members /wordscramble, admins /slowmode on ... or /scrambleautomaticmode on.
Who can use it: the bot owner, bot admins and the group's admins.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)
D = None


async def endscramblesession_command(update, context):
    chat, user, message = update.effective_chat, update.effective_user, update.message
    if chat.type == "private":
        await message.reply_text("Use /endscramblesession inside the group whose Word Scramble you want to end.")
        return
    allowed = D.is_owner(user.id) or await asyncio.to_thread(D.is_bot_admin_or_owner, user.id) or await D.is_chat_admin(update, context)
    if not allowed:
        await message.reply_text("🚫 Only group admins (or the bot owner) can end the Word Scramble session.")
        return
    chat_id = update.effective_chat.id
    jq = context.job_queue
    # 1) the running round: the same atomic claim the timeout uses - a winner and this command can never both count
    round_word = None
    try:
        round_word = await asyncio.to_thread(D.db.scramble_claim_round, chat_id)
    except Exception:
        logger.exception("endscramblesession: could not claim the round")
    try:
        await asyncio.to_thread(D.db.scramble_end_round, chat_id)       # also removes a half-finished leftover row
    except Exception:
        logger.exception("endscramblesession: could not delete the round")
    # 2) switch the timed modes off in the database (this also bumps the Slow Mode generation, so any timer that is
    #    still alive - in this process - is ignored when it fires)
    slow_on, interval = False, 600
    try:
        row = await asyncio.to_thread(D.db.get_scramble_slowmode, chat_id)
        if row:
            interval, slow_on = row[0], row[1]
        await asyncio.to_thread(D.db.set_scramble_slowmode, chat_id, interval, False)
        await asyncio.to_thread(D.db.scramble_clear_next_round_due, chat_id)
    except Exception:
        logger.exception("endscramblesession: could not switch Slow Mode off")
    auto_on = False
    try:
        auto_on = (await asyncio.to_thread(D.db.scramble_get_auto_mode, chat_id))[1]
        await asyncio.to_thread(D.db.scramble_set_auto_enabled, chat_id, False)
    except Exception:
        logger.exception("endscramblesession: could not switch Automatic Mode off")
    # 3) every pending timer of this group
    if jq is not None:
        for name in (D.scramble_timeout_job_name(chat_id), D.slowmode_next_job_name(chat_id),
                     D.scramble_send_retry_job_name(chat_id)):
            for job in jq.get_jobs_by_name(name):
                job.schedule_removal()
    parts = ["🛑 The Word Scramble session was ended" + (" (the running word is closed)" if round_word else "") +
             f" by {D.esc_name(user.first_name or 'an admin')}."]
    parts.append("Slow Mode and Automatic Mode are OFF here, so no new round will start by itself.")
    parts.append("\n👥 <b>Members</b>: /wordscramble starts a normal round (or /wordgame).")
    admin = ["/slowmode on " + D.compact_duration(interval) + " - turn Slow Mode back on",
             "/scrambleautomaticmode on - turn Automatic Mode back on"]
    parts.append("\n👑 <b>Admins</b> (these stay OFF until you switch them on):\n" + "\n".join(admin))
    await D.safe_send(context.bot, chat_id, text="\n".join(parts), parse_mode="HTML")


def install(app, deps):
    global D
    D = deps
    from telegram.ext import CommandHandler
    app.add_handler(CommandHandler("endscramblesession", endscramblesession_command))
