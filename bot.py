# bot.py
import asyncio
import re

from pyrogram import Client, filters
from pyrogram.types import Message, Chat
from pyrogram.errors import UserAlreadyParticipant, InviteHashExpired
from pyrogram.enums import ChatType

from logger import logger
from database import (
    init_db, add_group, remove_group,
    get_all_groups, get_all_groups_with_info,
    get_failed_groups, clear_group_failed,
    add_allowed_user, remove_allowed_user, get_allowed_users, is_user_allowed
)
from scheduler import (
    start_scheduler, stop_scheduler,
    schedule_message_job, stop_job,
    send_interval_message, set_client
)
import config

# ── Clients ────────────────────────────────────────────────────────────────────
user_app = Client(
    "user_session",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    phone_number=config.PHONE_NUMBER
)

bot_app = Client(
    "bot_session",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN
)

# ── Filters ────────────────────────────────────────────────────────────────────
def allowed_only(_, __, message: Message):
    return message.from_user and is_user_allowed(message.from_user.id, config.OWNER_ID)

def owner_only(_, __, message: Message):
    return message.from_user and message.from_user.id == config.OWNER_ID

allowed_filter = filters.create(allowed_only)
owner_filter = filters.create(owner_only)

# ── Helpers ────────────────────────────────────────────────────────────────────
def parse_cron(time_str):
    if re.match(r"^\d{2}:\d{2}$", time_str):
        hour, minute = map(int, time_str.split(':'))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return f"{minute} {hour} * * *"
    return None

async def join_and_add(target: str):
    try:
        try:
            await user_app.join_chat(target)
        except UserAlreadyParticipant:
            pass
        except InviteHashExpired:
            return None, "Invite link expired."
        chat = await user_app.get_chat(target)
        add_group(chat.id, chat.title or "")
        return chat, None
    except Exception as e:
        return None, str(e)

# ── /help ──────────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("help") & filters.private & allowed_filter)
async def help_cmd(client, message: Message):
    await message.reply(
        "📋 **Bot Commands**\n\n"
        "👥 **Groups**\n"
        "`/addgroup <link/@username/id>` — Group add + auto join\n"
        "`/removegroup <group_id>` — Group remove karo\n"
        "`/listgroups` — Saare groups status ke saath dekho\n"
        "`/syncgroups` — User ke saare joined groups auto fetch karo\n"
        "`/autojoin` — DB ke saare groups mein rejoin karo\n\n"
        "📨 **Messaging**\n"
        "`/interval <seconds> <message>` — Har X seconds pe message\n"
        "`/schedule <HH:MM> <message>` — Daily ek time pe message (UTC)\n"
        "`/stop <job_id>` — Job band karo\n"
        "`/listjobs` — Saare active jobs dekho\n\n"
        "⚠️ **Spam / Fail**\n"
        "`/faillist` — Failed groups ki list\n"
        "`/clearfail <group_id>` — Ek group fail list se hatao\n"
        "`/clearfail all` — Saare failed groups clear karo\n\n"
        "👤 **Users** _(sirf owner)_\n"
        "`/adduser <user_id>` — Kisi ko access do\n"
        "`/removeuser <user_id>` — Access hatao\n"
        "`/listusers` — Allowed users dekho\n\n"
        "💡 **Examples**\n"
        "`/addgroup @mygroup`\n"
        "`/interval 300 Hello everyone!`\n"
        "`/schedule 14:30 Aaj meeting hai!`\n"
        "`/syncgroups` — Saare groups ek baar mein add karo"
    )

# ── /adduser ───────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("adduser") & filters.private & owner_filter)
async def adduser_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /adduser <user_id>")
        return
    try:
        uid = int(parts[1].strip())
        if uid == config.OWNER_ID:
            await message.reply("Owner ko add karne ki zaroorat nahi.")
            return
        if add_allowed_user(uid, message.from_user.id):
            await message.reply(f"✅ User `{uid}` ko access de diya.")
        else:
            await message.reply(f"ℹ️ User `{uid}` already allowed hai.")
    except ValueError:
        await message.reply("Numeric user ID chahiye.")

# ── /removeuser ────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("removeuser") & filters.private & owner_filter)
async def removeuser_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /removeuser <user_id>")
        return
    try:
        uid = int(parts[1].strip())
        if remove_allowed_user(uid):
            await message.reply(f"✅ User `{uid}` ka access hata diya.")
        else:
            await message.reply(f"❌ User `{uid}` list mein nahi tha.")
    except ValueError:
        await message.reply("Numeric user ID chahiye.")

# ── /listusers ─────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("listusers") & filters.private & owner_filter)
async def listusers_cmd(client, message: Message):
    rows = get_allowed_users()
    if not rows:
        await message.reply("Koi allowed user nahi hai. Sirf owner access kar sakta hai.")
        return
    lines = [f"• `{uid}` (added by `{added_by}`)" for uid, added_by in rows]
    await message.reply(f"✅ Allowed Users ({len(rows)}):\n\n" + "\n".join(lines))

# ── /addgroup ──────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("addgroup") & filters.private & allowed_filter)
async def addgroup_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /addgroup <link or @username or group_id>")
        return
    chat, err = await join_and_add(parts[1].strip())
    if err:
        await message.reply(f"❌ Error: `{err}`")
    else:
        await message.reply(f"✅ Added: **{chat.title}** (`{chat.id}`)")

# ── /removegroup ───────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("removegroup") & filters.private & allowed_filter)
async def removegroup_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /removegroup <group_id>")
        return
    try:
        chat_id = int(parts[1].strip())
        if remove_group(chat_id):
            await message.reply(f"✅ Removed group `{chat_id}`")
        else:
            await message.reply("❌ Failed to remove.")
    except ValueError:
        await message.reply("Numeric group ID chahiye.")

# ── /listgroups ────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("listgroups") & filters.private & allowed_filter)
async def listgroups_cmd(client, message: Message):
    rows = get_all_groups_with_info()
    if not rows:
        await message.reply("Koi group registered nahi hai.")
        return
    active = [(c, t, m, f) for c, t, m, f in rows if not f]
    failed = [(c, t, m, f) for c, t, m, f in rows if f]
    lines = []
    for chat_id, title, mult, _ in active:
        lines.append(f"🟢 **{title or 'Unknown'}** (`{chat_id}`)" + (f" ⚡x{mult}" if mult > 1.0 else ""))
    for chat_id, title, mult, _ in failed:
        lines.append(f"🔴 **{title or 'Unknown'}** (`{chat_id}`)")
    await message.reply(
        f"📋 Groups: {len(active)} active, {len(failed)} failed\n\n" + "\n".join(lines)
    )

# ── /syncgroups ────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("syncgroups") & filters.private & allowed_filter)
async def syncgroups_cmd(client, message: Message):
    """Auto fetch all groups/channels user_app is already member of."""
    await message.reply("🔄 Syncing... saare joined groups fetch ho rahe hain.")
    added, skipped = 0, 0
    try:
        async for dialog in user_app.get_dialogs():
            chat = dialog.chat
            if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
                if add_group(chat.id, chat.title or ""):
                    added += 1
                else:
                    skipped += 1
        await message.reply(
            f"✅ Sync complete!\n"
            f"• Naye add: {added}\n"
            f"• Already the: {skipped}"
        )
    except Exception as e:
        await message.reply(f"❌ Sync error: `{e}`")

# ── /autojoin ──────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("autojoin") & filters.private & allowed_filter)
async def autojoin_cmd(client, message: Message):
    rows = get_all_groups_with_info()
    if not rows:
        await message.reply("Koi group DB mein nahi hai. Pehle /syncgroups ya /addgroup karo.")
        return
    ok, fail = [], []
    for chat_id, title, _, _ in rows:
        try:
            try:
                await user_app.join_chat(chat_id)
            except UserAlreadyParticipant:
                pass
            ok.append(f"✅ {title or chat_id}")
        except Exception as e:
            fail.append(f"❌ {title or chat_id}: {e}")
    result = "\n".join(ok + fail) or "Nothing to do."
    await message.reply(f"Auto-join result:\n{result}")

# ── /schedule ──────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("schedule") & filters.private & allowed_filter)
async def schedule_cmd(client, message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("Usage: /schedule HH:MM Your message")
        return
    cron = parse_cron(parts[1])
    if not cron:
        await message.reply("Invalid time. Use HH:MM (UTC)\nExample: /schedule 14:30 Hello!")
        return
    groups = get_all_groups()
    if not groups:
        await message.reply("Pehle /addgroup ya /syncgroups karo.")
        return
    job_id = schedule_message_job(groups, parts[2], cron)
    if job_id:
        await message.reply(
            f"✅ Scheduled at `{parts[1]}` UTC daily\n"
            f"Groups: {len(groups)}\n"
            f"Job ID: `{job_id}`"
        )
    else:
        await message.reply("❌ Schedule failed.")

# ── /interval ──────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("interval") & filters.private & allowed_filter)
async def interval_cmd(client, message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("Usage: /interval <seconds> Your message\nMinimum: 60 seconds")
        return
    try:
        seconds = int(parts[1])
        if seconds < 60:
            await message.reply("⚠️ Minimum 60 seconds — spam ban se bachne ke liye.")
            return
        groups = get_all_groups()
        if not groups:
            await message.reply("Pehle /addgroup ya /syncgroups karo.")
            return
        job_id = send_interval_message(groups, parts[2], seconds)
        if job_id:
            await message.reply(
                f"✅ Interval every {seconds}s\n"
                f"Groups: {len(groups)}\n"
                f"Job ID: `{job_id}`"
            )
        else:
            await message.reply("❌ Interval schedule failed.")
    except ValueError:
        await message.reply("Seconds numeric hona chahiye.")

# ── /stop ──────────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("stop") & filters.private & allowed_filter)
async def stop_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /stop <job_id>")
        return
    if stop_job(parts[1]):
        await message.reply(f"✅ Stopped job `{parts[1]}`")
    else:
        await message.reply("❌ Job not found. /listjobs se dekho.")

# ── /listjobs ──────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("listjobs") & filters.private & allowed_filter)
async def listjobs_cmd(client, message: Message):
    from scheduler import scheduler
    jobs = scheduler.get_jobs()
    if not jobs:
        await message.reply("Koi active job nahi hai.")
        return
    lines = []
    for job in jobs:
        lines.append(f"• `{job.id}`\n  Next: {job.next_run_time}")
    await message.reply(f"Active Jobs ({len(jobs)}):\n\n" + "\n".join(lines))

# ── /faillist ──────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("faillist") & filters.private & allowed_filter)
async def faillist_cmd(client, message: Message):
    failed = get_failed_groups()
    if not failed:
        await message.reply("✅ Koi failed group nahi hai.")
        return
    lines = [f"🔴 **{title or 'Unknown'}** (`{chat_id}`)" for chat_id, title in failed]
    await message.reply(f"Failed Groups ({len(failed)}):\n\n" + "\n".join(lines))

# ── /clearfail ─────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("clearfail") & filters.private & allowed_filter)
async def clearfail_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /clearfail <group_id>  OR  /clearfail all")
        return
    arg = parts[1].strip()
    if arg == "all":
        failed = get_failed_groups()
        for chat_id, _ in failed:
            clear_group_failed(chat_id)
        await message.reply(f"✅ {len(failed)} group(s) fail list se hata diye.")
    else:
        try:
            chat_id = int(arg)
            clear_group_failed(chat_id)
            await message.reply(f"✅ Group `{chat_id}` fail list se hata diya.")
        except ValueError:
            await message.reply("Numeric group ID ya 'all' do.")

# ── Unauthorized ───────────────────────────────────────────────────────────────
@bot_app.on_message(filters.private & ~allowed_filter)
async def unauthorized(client, message: Message):
    pass  # Silently ignore

# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    init_db()
    start_scheduler()

    await user_app.start()
    set_client(user_app)
    logger.info("User client started.")

    await bot_app.start()
    logger.info("Bot client started.")
    logger.info("Bot running. Ctrl+C to stop.")

    try:
        await asyncio.Future()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stop_scheduler()
        await user_app.stop()
        await bot_app.stop()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
