# bot.py
import asyncio
import re

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import UserAlreadyParticipant, InviteHashExpired

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

# ── Handlers ───────────────────────────────────────────────────────────────────
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


@bot_app.on_message(filters.command("listusers") & filters.private & owner_filter)
async def listusers_cmd(client, message: Message):
    users = get_allowed_users()
    if not users:
        await message.reply("Koi allowed user nahi hai (sirf owner).")
        return
    await message.reply("Allowed Users:\n" + "\n".join(f"• `{uid}`" for uid in users))


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


@bot_app.on_message(filters.command("listgroups") & filters.private & allowed_filter)
async def listgroups_cmd(client, message: Message):
    rows = get_all_groups_with_info()
    if not rows:
        await message.reply("Koi group registered nahi hai.")
        return
    lines = []
    for chat_id, title, mult, is_failed in rows:
        status = "🔴 FAILED" if is_failed else "🟢"
        lines.append(f"{status} **{title or 'Unknown'}** (`{chat_id}`) mult={mult}")
    await message.reply("Groups:\n\n" + "\n".join(lines))


@bot_app.on_message(filters.command("autojoin") & filters.private & allowed_filter)
async def autojoin_cmd(client, message: Message):
    rows = get_all_groups_with_info()
    if not rows:
        await message.reply("Koi group DB mein nahi hai.")
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
    await message.reply("Auto-join result:\n" + "\n".join(ok + fail) or "Nothing to do.")


@bot_app.on_message(filters.command("schedule") & filters.private & allowed_filter)
async def schedule_cmd(client, message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("Usage: /schedule HH:MM Your message")
        return
    cron = parse_cron(parts[1])
    if not cron:
        await message.reply("Invalid time. Use HH:MM (UTC)")
        return
    groups = get_all_groups()
    if not groups:
        await message.reply("Pehle /addgroup karo.")
        return
    job_id = schedule_message_job(groups, parts[2], cron)
    if job_id:
        await message.reply(f"✅ Scheduled at `{parts[1]}` UTC daily → {len(groups)} group(s)\nJob ID: `{job_id}`")
    else:
        await message.reply("❌ Schedule failed.")


@bot_app.on_message(filters.command("interval") & filters.private & allowed_filter)
async def interval_cmd(client, message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("Usage: /interval <seconds> Your message")
        return
    try:
        seconds = int(parts[1])
        if seconds < 15:
            await message.reply("Minimum 15 seconds.")
            return
        groups = get_all_groups()
        if not groups:
            await message.reply("Pehle /addgroup karo.")
            return
        job_id = send_interval_message(groups, parts[2], seconds)
        if job_id:
            await message.reply(f"✅ Interval every {seconds}s → {len(groups)} group(s)\nJob ID: `{job_id}`")
        else:
            await message.reply("❌ Interval schedule failed.")
    except ValueError:
        await message.reply("Seconds numeric hona chahiye.")


@bot_app.on_message(filters.command("stop") & filters.private & allowed_filter)
async def stop_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /stop <job_id>")
        return
    if stop_job(parts[1]):
        await message.reply(f"✅ Stopped job `{parts[1]}`")
    else:
        await message.reply("❌ Job not found.")


@bot_app.on_message(filters.command("faillist") & filters.private & allowed_filter)
async def faillist_cmd(client, message: Message):
    failed = get_failed_groups()
    if not failed:
        await message.reply("✅ Koi failed group nahi hai.")
        return
    lines = [f"❌ **{title or 'Unknown'}** (`{chat_id}`)" for chat_id, title in failed]
    await message.reply("Failed Groups:\n\n" + "\n".join(lines))


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
