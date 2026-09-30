# scheduler.py
import asyncio
import uuid

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pyrogram.errors import (
    FloodWait, UserBannedInChannel, ChatWriteForbidden,
    SlowmodeWait, PeerIdInvalid, ChannelPrivate, PeerFlood,
    ChatAdminRequired, UserNotParticipant
)

from database import (
    get_active_schedules, save_schedule, deactivate_schedule,
    get_group_multiplier, set_group_multiplier, mark_group_failed
)
from logger import logger

scheduler = AsyncIOScheduler()
pyrogram_client = None

_error_counts: dict = {}
MAX_ERRORS_BEFORE_FAIL = 3
# Telegram safe limit: 1 msg per group, 1s gap between groups
INTER_GROUP_DELAY = 2  # seconds between each group message


def set_client(client):
    global pyrogram_client
    pyrogram_client = client


async def _send_with_retry(chat_id: int, message: str, multiplier: float):
    """Send to one group, handle all exceptions. Returns True on success."""
    try:
        await pyrogram_client.send_message(chat_id, message)
        logger.info(f"✅ Sent to {chat_id}")
        _error_counts[chat_id] = 0
        if multiplier > 1.0:
            set_group_multiplier(chat_id, max(1.0, round(multiplier * 0.9, 2)))
        return True

    except FloodWait as e:
        wait = e.value + 5
        logger.warning(f"⏳ FloodWait {wait}s for {chat_id}")
        await asyncio.sleep(wait)
        new_mult = round(multiplier * 1.5, 2)
        set_group_multiplier(chat_id, new_mult)
        try:
            await pyrogram_client.send_message(chat_id, message)
            _error_counts[chat_id] = 0
            return True
        except Exception as e2:
            logger.error(f"Retry failed {chat_id}: {e2}")
            _increment_error(chat_id)
            return False

    except SlowmodeWait as e:
        wait = e.value + 2
        logger.warning(f"⏳ SlowmodeWait {wait}s for {chat_id}")
        set_group_multiplier(chat_id, round(multiplier * 1.3, 2))
        await asyncio.sleep(wait)
        try:
            await pyrogram_client.send_message(chat_id, message)
            _error_counts[chat_id] = 0
            return True
        except Exception as e2:
            logger.error(f"SlowMode retry failed {chat_id}: {e2}")
            _increment_error(chat_id)
            return False

    except PeerFlood:
        # Account spam ban — stop sending to ALL groups temporarily
        logger.error("🚨 PeerFlood! Account spam ban. Pausing 5 minutes.")
        await asyncio.sleep(300)
        _increment_error(chat_id)
        return False

    except (UserBannedInChannel, ChatWriteForbidden, PeerIdInvalid,
            ChannelPrivate, ChatAdminRequired, UserNotParticipant) as e:
        logger.error(f"❌ Permanent error {chat_id}: {type(e).__name__}")
        _increment_error(chat_id)
        return False

    except Exception as e:
        logger.error(f"❌ Unexpected error {chat_id}: {e}")
        _increment_error(chat_id)
        return False


async def send_to_all_groups(chat_ids: list, message: str, base_interval: int = 0):
    if not pyrogram_client:
        logger.error("Pyrogram client not set.")
        return

    for i, chat_id in enumerate(chat_ids):
        multiplier = get_group_multiplier(chat_id)

        # Inter-group delay to avoid spam detection
        if i > 0:
            await asyncio.sleep(INTER_GROUP_DELAY)

        # Extra delay if this group had previous issues
        if base_interval > 0 and multiplier > 1.0:
            extra = base_interval * (multiplier - 1.0)
            await asyncio.sleep(min(extra, 60))  # cap at 60s extra

        await _send_with_retry(chat_id, message, multiplier)


def _increment_error(chat_id):
    _error_counts[chat_id] = _error_counts.get(chat_id, 0) + 1
    count = _error_counts[chat_id]
    logger.warning(f"Group {chat_id} errors: {count}/{MAX_ERRORS_BEFORE_FAIL}")
    if count >= MAX_ERRORS_BEFORE_FAIL:
        mark_group_failed(chat_id)
        logger.error(f"🔴 Group {chat_id} → FAILED list")
        _error_counts[chat_id] = 0


def schedule_message_job(chat_ids: list, message: str, cron_expression: str):
    job_id = str(uuid.uuid4())
    try:
        trigger = CronTrigger.from_crontab(cron_expression, timezone=pytz.utc)
        scheduler.add_job(
            send_to_all_groups,
            trigger=trigger,
            args=[chat_ids, message],
            id=job_id,
            replace_existing=True
        )
        for chat_id in chat_ids:
            save_schedule(f"{job_id}_{chat_id}", chat_id, message, cron_expression)
        logger.info(f"Scheduled job {job_id} → {len(chat_ids)} groups")
        return job_id
    except Exception as e:
        logger.error(f"schedule_message_job error: {e}")
        return None


def send_interval_message(chat_ids: list, message: str, interval_seconds: int = 60):
    job_id = f"interval_{uuid.uuid4()}"
    try:
        scheduler.add_job(
            send_to_all_groups,
            trigger='interval',
            seconds=interval_seconds,
            args=[chat_ids, message, interval_seconds],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=120
        )
        logger.info(f"Interval job {job_id} every {interval_seconds}s → {len(chat_ids)} groups")
        return job_id
    except Exception as e:
        logger.error(f"send_interval_message error: {e}")
        return None


def stop_job(job_id: str):
    try:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            deactivate_schedule(job_id)
            logger.info(f"Stopped job {job_id}")
            return True
        return False
    except Exception as e:
        logger.error(f"stop_job error: {e}")
        return False


def reload_schedules():
    for job_id, chat_id, message, cron_expression in get_active_schedules():
        try:
            if scheduler.get_job(job_id):
                continue
            trigger = CronTrigger.from_crontab(cron_expression, timezone=pytz.utc)
            scheduler.add_job(
                send_to_all_groups,
                trigger=trigger,
                args=[[chat_id], message],
                id=job_id,
                replace_existing=True
            )
            logger.info(f"Reloaded job {job_id} for chat {chat_id}")
        except Exception as e:
            logger.error(f"reload_schedules error {job_id}: {e}")


def start_scheduler():
    reload_schedules()
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started.")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped.")
