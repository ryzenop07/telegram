# bot.py
import asyncio
import sys
import re
from datetime import datetime

from pyrogram import Client, filters
from pyrogram.errors import FloodWait, UserNotParticipant, ChatAdminRequired
from pyrogram.types import Message

from logger import logger
from database import init_db, add_group, remove_group, get_all_groups
from scheduler import (
    start_scheduler,
    stop_scheduler,
    schedule_message_job,
    stop_job,
    send_interval_message # Import this for interval messages
)
import config

# --- Global Client Instance ---
# It's often easier to manage a single client instance.
app = None

# --- Helper to parse cron expressions ---
def parse_cron_expression(time_str: str, message_text: str) -> str | None:
    """Parses HH:MM to a cron expression string."""
    try:
        # Basic validation for HH:MM format
        if re.match(r"^\d{2}:\d{2}$", time_str):
            hour, minute = map(int, time_str.split(':'))
            if 0 <= hour < 24 and 0 <= minute < 60:
                # Cron expression: minute hour * * * (runs daily at specified hour:minute)
                # Assuming UTC timezone for APScheduler. Adjust if needed.
                return f"{minute} {hour} * * *"
        return None
    except ValueError:
        return None

# --- Pyrogram Message Handlers ---
async def handle_addgroup(client: Client, message: Message):
    if message.chat.type not in ["group", "supergroup"]:
        await message.reply_text("This command can only be used in a group or supergroup.")
        return

    chat_id = message.chat.id
    if add_group(chat_id):
        await message.reply_text(f"✅ Group '{message.chat.title}' ({chat_id}) added to announcement list.")
        # Optionally, start an interval message here if needed for all groups
        # Or wait for a specific /schedule command
    else:
        await message.reply_text("❌ Failed to add group. Please check logs.")

async def handle_removegroup(client: Client, message: Message):
    if message.chat.type not in ["group", "supergroup"]:
        await message.reply_text("This command can only be used in a group or supergroup.")
        return

    chat_id = message.chat.id
    if remove_group(chat_id):
        await message.reply_text(f"✅ Group '{message.chat.title}' ({chat_id}) removed from announcement list.")
        # You might want to stop any scheduled jobs associated with this group here.
        # This would require mapping group_id to job_id and using stop_job.
    else:
        await message.reply_text("❌ Failed to remove group. Please check logs.")

async def handle_listgroups(client: Client, message: Message):
    groups = get_all_groups()
    if not groups:
        await message.reply_text("No groups are currently registered.")
        return

    group_list_text = "Registered Groups:\n\n"
    for group_id in groups:
        try:
            chat = await client.get_chat(group_id)
            group_list_text += f"- **{chat.title}** (`{group_id}`)\n"
        except Exception:
            group_list_text += f"- Unknown Group (`{group_id}`)\n"

    await message.reply_text(group_list_text)

async def handle_schedule(client: Client, message: Message):
    """Schedules a daily announcement."""
    if message.chat.type not in ["group", "supergroup"]:
        await message.reply_text("This command can only be used in a group or supergroup.")
        return

    chat_id = message.chat.id
    # Expected format: /schedule HH:MM Message text
    # We need to split the command carefully.
    parts = message.text.split(maxsplit=2) # Split into at most 3 parts: command, time, message

    if len(parts) < 3:
        await message.reply_text("Usage: `/schedule HH:MM Your announcement message here.`")
        return

    time_str = parts[1]
    announcement_message = parts[2]

    cron_expression = parse_cron_expression(time_str, announcement_message)

    if not cron_expression:
        await message.reply_text("Invalid time format. Please use HH:MM (e.g., `14:30`).")
        return

    # Ensure the group is registered before scheduling
    if chat_id not in get_all_groups():
        await message.reply_text("This group is not registered. Please use `/addgroup` first.")
        return

    job_id = schedule_message_job(chat_id, announcement_message, cron_expression)

    if job_id:
        await message.reply_text(f"✅ Announcement scheduled for `{time_str}` daily in this group.\nJob ID: `{job_id}`")
    else:
        await message.reply_text("❌ Failed to schedule announcement. Please check logs.")

async def handle_stop_schedule(client: Client, message: Message):
    """Stops a scheduled announcement using its job ID."""
    # Expected format: /stop JOB_ID
    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        await message.reply_text("Usage: `/stop JOB_ID`")
        return

    job_id_to_stop = parts[1]

    if stop_job(job_id_to_stop):
        await message.reply_text(f"✅ Scheduled announcement with Job ID `{job_id_to_stop}` has been stopped.")
    else:
        await message.reply_text("❌ Failed to stop announcement. Job ID not found or already stopped. Please check logs.")

async def handle_interval_message(client: Client, message: Message):
    """
    Handles the /interval command to send messages every X seconds.
    This is a simplified implementation. A more robust version would
    manage these interval jobs more explicitly and have a way to stop them.
    """
    if message.chat.type not in ["group", "supergroup"]:
        await message.reply_text("This command can only be used in a group or supergroup.")
        return

    chat_id = message.chat.id
    # Expected format: /interval <seconds> Message
    parts = message.text.split(maxsplit=2)

    if len(parts) < 3:
        await message.reply_text("Usage: `/interval <seconds> Your message here.`")
        return

    try:
        interval_seconds = int(parts[1])
        interval_message = parts[2]

        if interval_seconds <= 0:
            await message.reply_text("Interval must be a positive number of seconds.")
            return

        # Limit the interval to prevent spamming and hitting rate limits easily
        # You might want to make this configurable or enforce stricter limits.
        if interval_seconds < 15: # Example: Minimum 15 seconds
            await message.reply_text("Minimum interval is 15 seconds to prevent abuse.")
            return

        # Ensure the group is registered
        if chat_id not in get_all_groups():
            await message.reply_text("This group is not registered. Please use `/addgroup` first.")
            return

        # Note: The send_interval_message function in scheduler.py is a placeholder.
        # You MUST implement the actual sending logic within that function or a callback it uses.
        # This function needs access to the Pyrogram client.
        job_id = send_interval_message(chat_id, interval_message, interval_seconds)

        if job_id:
            await message.reply_text(f"✅ Interval messages scheduled for every {interval_seconds} seconds in this group.\nJob ID: `{job_id}`\nNote: The message sending logic needs to be implemented in `scheduler.py`.")
        else:
            await message.reply_text("❌ Failed to schedule interval message. Please check logs.")

    except ValueError:
        await message.reply_text("Invalid interval. Please provide a number for seconds.")
    except Exception as e:
        logger.error(f"Error handling /interval command: {e}")
        await message.reply_text("An error occurred while scheduling the interval message.")


async def main():
    global app
    # Initialize Database
    init_db()

    # Start APScheduler
    start_scheduler()

    # Initialize Pyrogram Client
    try:
        app = Client(
            "announcement_bot",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            bot_token=config.BOT_TOKEN
        )
        await app.start()
        logger.info("Pyrogram client started.")
    except Exception as e:
        logger.critical(f"Failed to start Pyrogram client: {e}")
        sys.exit(1)

    # --- Command Filters ---
    bot_username = (await app.get_me()).username
    command_prefix = "/" # Or use @command_prefix for inline bot commands if you were using inline mode

    # Add commands
    @app.on_message(filters.command("addgroup", prefixes=command_prefix) & filters.private)
    async def addgroup_command(client, message):
        await handle_addgroup(client, message)

    @app.on_message(filters.command("removegroup", prefixes=command_prefix) & filters.private)
    async def removegroup_command(client, message):
        await handle_removegroup(client, message)

    @app.on_message(filters.command("listgroups", prefixes=command_prefix) & filters.private)
    async def listgroups_command(client, message):
        await handle_listgroups(client, message)

    @app.on_message(filters.command("schedule", prefixes=command_prefix) & filters.private)
    async def schedule_command(client, message):
        await handle_schedule(client, message)

    @app.on_message(filters.command("stop", prefixes=command_prefix) & filters.private)
    async def stop_command(client, message):
        await handle_stop_schedule(client, message)

    @app.on_message(filters.command("interval", prefixes=command_prefix) & filters.private)
    async def interval_command(client, message):
        await handle_interval_message(client, message)

    # --- Forwarding to groups ---
    # To make the bot add groups, you need to send the /addgroup command
    # from a PRIVATE CHAT with the bot.
    # The bot itself needs to be added to the groups it will manage.
    # When the bot is added to a group, it receives messages with chat.id referring to that group.
    # However, it cannot *itself* execute commands like /addgroup within a group it's in.
    # The current implementation requires admin intervention to add/remove groups via PM.

    # If you want to be able to execute commands *from* a group (e.g., /addgroup in a group),
    # the `filters.private` needs to be removed, and you'd check `message.chat.type`
    # to ensure it's a group/supergroup command.
    # For simplicity, this example uses private chat commands for management.

    logger.info("Bot is running. Press Ctrl+C to stop.")
    await asyncio.Future()  # Run forever

    # --- Cleanup ---
    await app.stop()
    stop_scheduler()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.critical(f"An unexpected error occurred: {e}")
    finally:
        # Ensure cleanup happens even on unexpected errors
        if app and app.is_connected:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(app.stop())
        stop_scheduler()
        logger.info("Bot shutdown complete.")

