# bot.py
import asyncio
import sys
import re

from pyrogram import Client, filters
from pyrogram.types import Message

from logger import logger
from database import init_db, add_group, remove_group, get_all_groups
from scheduler import start_scheduler, stop_scheduler, schedule_message_job, stop_job, send_interval_message, set_client
import config

bot_app = None
user_app = None


def parse_cron(time_str):
    if re.match(r"^\d{2}:\d{2}$", time_str):
        hour, minute = map(int, time_str.split(':'))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return f"{minute} {hour} * * *"
    return None


async def main():
    global bot_app, user_app
    init_db()
    start_scheduler()

    # User client — teri ID se message bhejega
    user_app = Client(
        "user_session",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        phone_number=config.PHONE_NUMBER
    )
    await user_app.start()
    set_client(user_app)
    logger.info("User client started.")

    # Bot client — commands accept karega
    bot_app = Client(
        "bot_session",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN
    )
    await bot_app.start()
    logger.info("Bot client started.")

    @bot_app.on_message(filters.command("addgroup") & filters.private)
    async def addgroup_cmd(client, message: Message):
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply("Usage: /addgroup <link or @username or group_id>")
            return
        target = parts[1].strip()
        try:
            try:
                await user_app.join_chat(target)
            except Exception as e:
                logger.info(f"Join attempt: {e}")
            chat = await user_app.get_chat(target)
            if add_group(chat.id):
                await message.reply(f"✅ Added: **{chat.title}** (`{chat.id}`)")
            else:
                await message.reply("❌ Failed to add to database.")
        except Exception as e:
            await message.reply(f"❌ Error: `{e}`")

    @bot_app.on_message(filters.command("removegroup") & filters.private)
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
            await message.reply("Please provide a numeric group ID.")

    @bot_app.on_message(filters.command("listgroups") & filters.private)
    async def listgroups_cmd(client, message: Message):
        groups = get_all_groups()
        if not groups:
            await message.reply("No groups registered.")
            return
        text = "Registered Groups:\n\n"
        for gid in groups:
            try:
                chat = await user_app.get_chat(gid)
                text += f"- **{chat.title}** (`{gid}`)\n"
            except Exception:
                text += f"- Unknown (`{gid}`)\n"
        await message.reply(text)

    @bot_app.on_message(filters.command("schedule") & filters.private)
    async def schedule_cmd(client, message: Message):
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.reply("Usage: /schedule HH:MM Your message here")
            return
        cron = parse_cron(parts[1])
        if not cron:
            await message.reply("Invalid time. Use HH:MM format (e.g. 14:30)")
            return
        groups = get_all_groups()
        if not groups:
            await message.reply("No groups registered. Use /addgroup first.")
            return
        job_id = schedule_message_job(groups, parts[2], cron)
        if job_id:
            await message.reply(f"✅ Scheduled at `{parts[1]}` UTC daily to {len(groups)} group(s).\nJob ID: `{job_id}`")
        else:
            await message.reply("❌ Failed to schedule.")

    @bot_app.on_message(filters.command("interval") & filters.private)
    async def interval_cmd(client, message: Message):
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.reply("Usage: /interval <seconds> Your message here")
            return
        try:
            seconds = int(parts[1])
            if seconds < 15:
                await message.reply("Minimum 15 seconds.")
                return
            groups = get_all_groups()
            if not groups:
                await message.reply("No groups registered. Use /addgroup first.")
                return
            job_id = send_interval_message(groups, parts[2], seconds)
            if job_id:
                await message.reply(f"✅ Interval every {seconds}s to {len(groups)} group(s).\nJob ID: `{job_id}`")
            else:
                await message.reply("❌ Failed to schedule.")
        except ValueError:
            await message.reply("Invalid seconds value.")

    @bot_app.on_message(filters.command("stop") & filters.private)
    async def stop_cmd(client, message: Message):
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply("Usage: /stop <job_id>")
            return
        if stop_job(parts[1]):
            await message.reply(f"✅ Stopped job `{parts[1]}`")
        else:
            await message.reply("❌ Job not found.")

    logger.info("Bot is running. Press Ctrl+C to stop.")
    await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    finally:
        stop_scheduler()
        logger.info("Bot shutdown complete.")
