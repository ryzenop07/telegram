# scheduler.py
import asyncio
import uuid

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pyrogram.errors import (
    FloodWait, UserBannedInChannel, ChatWriteForbidden,
    SlowmodeWait, PeerIdInvalid, ChannelPrivate
)

from database import (
    get_active_schedules, save_schedule, deactivate_schedule,
    get_group_multiplier, set_group_multiplier, mark_group_failed
)
from logger import logger

scheduler = AsyncIOScheduler()
pyrogram_client = None

# Per-group consecutive error counter  {chat_id: count}
_error_counts: dict = {}
MAX_ERRORS_BEFORE_FAIL = 3


def set_client(client):
    global pyrogram_client
    pyrogram_client = client


async def send_to_all_groups(chat_ids: list, message: str, base_interval: int = 0):
    """
    Send message to each group.
    - FloodWait / SlowModeWait  → wait then retry, increase that group's multiplier
    - Banned / Forbidden / Invalid → mark group as failed after MAX_ERRORS_BEFORE_FAIL
    - base_interval > 0 means this is an interval job; apply per-group multiplier delay
    """
    if not pyrogram_client:
        logger.error("Pyrogram client not set.")
        return

    for chat_id in chat_ids:
        multiplier = get_group_multiplier(chat_id)
        effective_delay = base_interval * multiplier if base_interval > 0 else 0

        if effective_delay > 0:
            await asyncio.sleep(effective_delay)

        try:
            await pyrogram_client.send_message(chat_id, message)
            logger.info(f"Sent to {chat_id}")
            # Reset error count on success
            _error_counts[chat_id] = 0
            # Gradually reduce multiplier back toward 1.0 on success
            if multiplier > 1.0:
                new_mult = max(1.0, round(multiplier * 0.9, 2))
                set_group_multiplier(chat_id, new_mult)

        except FloodWait as e:
            wait = e.value + 5
            logger.warning(f"FloodWait {wait}s for {chat_id}. Waiting...")
            await asyncio.sleep(wait)
            # Increase multiplier for this group
            new_mult = round(multiplier * 1.5, 2)
            set_group_multiplier(chat_id, new_mult)
            logger.info(f"Group {chat_id} multiplier → {new_mult}")
            # Retry once after wait
            try:
                await pyrogram_client.send_message(chat_id, message)
                logger.info(f"Retry sent to {chat_id}")
                _error_counts[chat_id] = 0
            except Exception as retry_err:
                logger.error(f"Retry failed for {chat_id}: {retry_err}")
                _increment_error(chat_id)

        except SlowmodeWait as e:
            wait = e.value + 2
            logger.warning(f"SlowModeWait {wait}s for {chat_id}")
            new_mult = round(multiplier * 1.3, 2)
            set_group_multiplier(chat_id, new_mult)
            await asyncio.sleep(wait)
            try:
                await pyrogram_client.send_message(chat_id, message)
                _error_counts[chat_id] = 0
            except Exception as retry_err:
                logger.error(f"SlowMode retry failed for {chat_id}: {retry_err}")
                _increment_error(chat_id)

        except (UserBannedInChannel, ChatWriteForbidden, PeerIdInvalid, ChannelPrivate) as e:
            logger.error(f"Permanent error for {chat_id}: {e}")
            _increment_error(chat_id)

        except Exception as e:
            logger.error(f"Unexpected error for {chat_id}: {e}")
            _increment_error(chat_id)


def _increment_error(chat_id):
    _error_counts[chat_id] = _error_counts.get(chat_id, 0) + 1
    count = _error_counts[chat_id]
    logger.warning(f"Group {chat_id} error count: {count}/{MAX_ERRORS_BEFORE_FAIL}")
    if count >= MAX_ERRORS_BEFORE_FAIL:
        mark_group_failed(chat_id)
        logger.error(f"Group {chat_id} marked as FAILED and added to fail list.")
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
        logger.info(f"Scheduled job {job_id} to {len(chat_ids)} groups")
        return job_id
    except Exception as e:
        logger.error(f"schedule_message_job error: {e}")
        return None


def send_interval_message(chat_ids: list, message: str, interval_seconds: int = 30):
    job_id = f"interval_{uuid.uuid4()}"
    try:
        scheduler.add_job(
            send_to_all_groups,
            trigger='interval',
            seconds=interval_seconds,
            args=[chat_ids, message, interval_seconds],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=60
        )
        logger.info(f"Interval job {job_id} every {interval_seconds}s to {len(chat_ids)} groups")
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
            logger.error(f"reload_schedules error for {job_id}: {e}")


def start_scheduler():
    reload_schedules()
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started.")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped.")
