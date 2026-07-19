# scheduler.py
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.redis import RedisJobStore # Example if using Redis for persistence
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore # Or use SQLAlchemy
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime
import pytz # For timezone awareness
import uuid # To generate unique job IDs

from pyrogram import Client
from database import get_active_schedules, save_schedule, delete_schedule, deactivate_schedule
from logger import logger
import config # Assuming config.py exists

# IMPORTANT: For persistence between restarts, APScheduler needs a job store.
# SQLAlchemyJobStore is a good option if you're already using SQLite.
# For a robust solution, consider Redis or a dedicated database for job stores.
# For simplicity in this example, we'll manage schedules in our own SQLite DB
# and reload them manually, but APScheduler can handle persistence directly.

scheduler = AsyncIOScheduler()
# Example with SQLAlchemyJobStore (requires SQLAlchemy to be installed and configured)
# jobstores = {
#     'default': SQLAlchemyJobStore(url='sqlite:///jobs.db')
# }
# executors = {
#     'default': ThreadPoolExecutor(20)
# }
# scheduler = AsyncIOScheduler(jobstores=jobstores, executors=executors)


def schedule_message_job(chat_id: int, message: str, cron_expression: str):
    """Schedules a recurring message."""
    job_id = str(uuid.uuid4()) # Generate a unique ID for the job
    try:
        cron_trigger = CronTrigger.from_crontab(cron_expression, timezone=pytz.timezone('UTC'))
        job = scheduler.add_job(
            send_scheduled_message,
            trigger=cron_trigger,
            args=[chat_id, message],
            id=job_id,
            replace_existing=True # If a job with this ID exists, replace it
        )
        save_schedule(job_id, chat_id, message, cron_expression) # Save to our DB
        logger.info(f"Scheduled message for chat {chat_id} with job ID {job_id} using cron '{cron_expression}'")
        return job_id
    except Exception as e:
        logger.error(f"Error scheduling message for chat {chat_id}: {e}")
        return None

def send_scheduled_message(chat_id: int, message: str):
    """The function that will be executed by APScheduler."""
    # This function needs access to the Pyrogram Client
    # It's best to pass the client or have a way to access it globally/passed down.
    # For simplicity here, assume client is accessible or passed.
    # In a real app, you'd likely have a global client instance or a way to get it.
    # Example: client.send_message(chat_id, message)
    logger.info(f"Executing scheduled message for chat {chat_id}: '{message[:50]}...'")
    # Placeholder for actual message sending
    # You'll need to pass the client object here or have it accessible.
    # For instance, if bot.py manages the client instance:
    # global bot_client
    # if bot_client:
    #     try:
    #         bot_client.send_message(chat_id, message)
    #         logger.info(f"Successfully sent scheduled message to {chat_id}")
    #     except Exception as e:
    #         logger.error(f"Failed to send scheduled message to {chat_id}: {e}")
    # else:
    #     logger.error("Pyrogram client not available to send scheduled message.")


def send_interval_message(chat_id: int, message: str, interval_seconds: int = 30):
    """Sends a message repeatedly at a fixed interval."""
    # This is for the "every 30 seconds" requirement.
    # It's not a typical cron job, but a recurring interval.
    # For this, we can use scheduler.add_job with interval trigger.
    try:
        # Create a unique job ID for this interval task
        job_id = f"interval_{chat_id}_{uuid.uuid4()}"
        job = scheduler.add_job(
            send_interval_message_callback,
            trigger='interval',
            seconds=interval_seconds,
            args=[chat_id, message, interval_seconds], # Pass interval_seconds for recursion
            id=job_id,
            replace_existing=True,
            misfire_grace_time=60 # Allow a grace period for missed runs
        )
        logger.info(f"Scheduled interval message for chat {chat_id} every {interval_seconds}s with job ID {job_id}")
        return job_id
    except Exception as e:
        logger.error(f"Error scheduling interval message for chat {chat_id}: {e}")
        return None

def send_interval_message_callback(chat_id: int, message: str, interval_seconds: int):
    """Callback function for interval messages."""
    logger.info(f"Executing interval message for chat {chat_id}: '{message[:50]}...'")
    # Placeholder for actual message sending using Pyrogram client
    # Similar to send_scheduled_message, the client needs to be accessible.
    # Example: client.send_message(chat_id, message)
    pass # Replace with actual sending logic

def stop_job(job_id: str):
    """Stops a scheduled job."""
    try:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            deactivate_schedule(job_id) # Mark as inactive in our DB
            logger.info(f"Stopped and deactivated job {job_id}.")
            return True
        else:
            logger.warning(f"Job {job_id} not found or already stopped.")
            return False
    except Exception as e:
        logger.error(f"Error stopping job {job_id}: {e}")
        return False

def reload_schedules():
    """Reloads all active schedules from the database when the bot restarts."""
    logger.info("Reloading schedules from database...")
    active_schedules = get_active_schedules()
    for job_id, chat_id, message, cron_expression in active_schedules:
        # We need to re-schedule based on our stored cron expression.
        # For interval jobs, you'd need to store type (cron/interval) and parameters.
        # This example assumes only cron jobs for simplicity of reload.
        # If you have interval jobs, you'll need to adapt database schema and this logic.
        try:
            # Reconstruct the job ID for APScheduler if it was stored separately
            # If job_id from DB is the actual APScheduler job_id, use it.
            # Otherwise, regenerate and associate.
            # For simplicity, we assume job_id from DB is the one to use.

            # Check if job already exists (e.g., if this is not the first reload)
            if scheduler.get_job(job_id):
                logger.warning(f"Job {job_id} already exists in scheduler. Skipping reload.")
                continue

            # This needs to be adapted if you store interval jobs.
            # For now, assume cron:
            cron_trigger = CronTrigger.from_crontab(cron_expression, timezone=pytz.timezone('UTC'))
            scheduler.add_job(
                send_scheduled_message, # Make sure this function can send messages
                trigger=cron_trigger,
                args=[chat_id, message],
                id=job_id, # Use the stored job ID
                replace_existing=True
            )
            logger.info(f"Reloaded schedule for job {job_id} (Chat: {chat_id}, Cron: '{cron_expression}')")
        except Exception as e:
            logger.error(f"Failed to reload schedule for job {job_id}: {e}")

def start_scheduler():
    """Starts the APScheduler."""
    # Optional: Configure job stores and executors for persistence
    # For simplicity, using default in-memory scheduler here.
    # For production, configure persistence!
    # scheduler.configure(jobstores=jobstores, executors=executors)

    # Load existing schedules from DB on startup
    reload_schedules()

    if not scheduler.running:
        scheduler.start()
        logger.info("APScheduler started.")
    else:
        logger.info("APScheduler is already running.")

def stop_scheduler():
    """Stops the APScheduler."""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("APScheduler stopped.")
    else:
        logger.info("APScheduler is not running.")

