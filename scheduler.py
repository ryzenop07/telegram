# scheduler.py
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import uuid

from database import get_active_schedules, save_schedule, deactivate_schedule
from logger import logger

scheduler = AsyncIOScheduler()

# Global reference to the Pyrogram user client (set from bot.py after login)
pyrogram_client = None

def set_client(client):
    global pyrogram_client
    pyrogram_client = client


def schedule_message_job(chat_ids: list, message: str, cron_expression: str):
    job_id = str(uuid.uuid4())
    try:
        cron_trigger = CronTrigger.from_crontab(cron_expression, timezone=pytz.timezone('UTC'))
        scheduler.add_job(
            send_to_all_groups,
            trigger=cron_trigger,
            args=[chat_ids, message],
            id=job_id,
            replace_existing=True
        )
        # Save one entry per group so reload works
        for chat_id in chat_ids:
            save_schedule(f"{job_id}_{chat_id}", chat_id, message, cron_expression)
        logger.info(f"Scheduled message to {len(chat_ids)} groups, job ID {job_id}")
        return job_id
    except Exception as e:
        logger.error(f"Error scheduling message: {e}")
        return None


def send_interval_message(chat_ids: list, message: str, interval_seconds: int = 30):
    job_id = f"interval_{uuid.uuid4()}"
    try:
        scheduler.add_job(
            send_to_all_groups,
            trigger='interval',
            seconds=interval_seconds,
            args=[chat_ids, message],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=60
        )
        logger.info(f"Interval message every {interval_seconds}s to {len(chat_ids)} groups, job ID {job_id}")
        return job_id
    except Exception as e:
        logger.error(f"Error scheduling interval message: {e}")
        return None


async def send_to_all_groups(chat_ids: list, message: str):
    if not pyrogram_client:
        logger.error("Pyrogram client not set. Cannot send messages.")
        return
    for chat_id in chat_ids:
        try:
            await pyrogram_client.send_message(chat_id, message)
            logger.info(f"Message sent to {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")


def stop_job(job_id: str):
    try:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            deactivate_schedule(job_id)
            logger.info(f"Stopped job {job_id}.")
            return True
        logger.warning(f"Job {job_id} not found.")
        return False
    except Exception as e:
        logger.error(f"Error stopping job {job_id}: {e}")
        return False


def reload_schedules():
    logger.info("Reloading schedules from database...")
    for job_id, chat_id, message, cron_expression in get_active_schedules():
        try:
            if scheduler.get_job(job_id):
                continue
            cron_trigger = CronTrigger.from_crontab(cron_expression, timezone=pytz.timezone('UTC'))
            scheduler.add_job(
                send_to_all_groups,
                trigger=cron_trigger,
                args=[[chat_id], message],
                id=job_id,
                replace_existing=True
            )
            logger.info(f"Reloaded job {job_id} for chat {chat_id}")
        except Exception as e:
            logger.error(f"Failed to reload job {job_id}: {e}")


def start_scheduler():
    reload_schedules()
    if not scheduler.running:
        scheduler.start()
        logger.info("APScheduler started.")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info("APScheduler stopped.")

