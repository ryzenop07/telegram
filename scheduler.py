# scheduler.py
import asyncio
import random
import uuid
from datetime import datetime, timezone, timedelta

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
    get_group_multiplier, set_group_multiplier, mark_group_failed,
)
from logger import logger

scheduler = AsyncIOScheduler()
_default_client = None
_user_clients: dict = {}  # user_id -> Client

_error_counts: dict = {}
MAX_ERRORS_BEFORE_FAIL = 3
INTER_GROUP_DELAY = 2  # seconds between each group message


def register_client(user_id: int, client):
    _user_clients[user_id] = client
    logger.info(f"Scheduler registered client for user: {user_id}")


def unregister_client(user_id: int):
    _user_clients.pop(user_id, None)
    logger.info(f"Scheduler unregistered client for user: {user_id}")


def set_client(client, user_id: int = None):
    global _default_client
    if user_id:
        _user_clients[user_id] = client
    if _default_client is None or not user_id:
        _default_client = client


def get_client(user_id: int = None):
    if user_id and user_id in _user_clients:
        return _user_clients[user_id]
    return _default_client


async def _send_with_retry(client, chat_id: int, message: str, multiplier: float, user_id: int = 0):
    """Send to one group using the appropriate client, handle all exceptions."""
    if not client:
        logger.error(f"No Pyrogram client available for {chat_id}")
        return False

    try:
        sent = await client.send_message(chat_id, message)
        logger.info(f"✅ Sent to {chat_id} (msg_id={sent.id})")
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
            sent = await client.send_message(chat_id, message)
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
            sent = await client.send_message(chat_id, message)
            _error_counts[chat_id] = 0
            return True
        except Exception as e2:
            logger.error(f"SlowMode retry failed {chat_id}: {e2}")
            _increment_error(chat_id)
            return False

    except PeerFlood:
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


async def send_to_all_groups(chat_ids: list, message: str, base_interval: int = 0, user_id: int = None):
    client = get_client(user_id)
    if not client:
        logger.error(f"Pyrogram client not found for user {user_id}")
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

        await _send_with_retry(client, chat_id, message, multiplier, user_id=user_id or 0)


def _increment_error(chat_id):
    _error_counts[chat_id] = _error_counts.get(chat_id, 0) + 1
    count = _error_counts[chat_id]
    logger.warning(f"Group {chat_id} errors: {count}/{MAX_ERRORS_BEFORE_FAIL}")
    if count >= MAX_ERRORS_BEFORE_FAIL:
        mark_group_failed(chat_id)
        logger.error(f"🔴 Group {chat_id} → FAILED list")
        _error_counts[chat_id] = 0


def schedule_message_job(chat_ids: list, message: str, cron_expression: str, user_id: int = None):
    job_id = f"job_{user_id or 0}_{uuid.uuid4()}"
    try:
        trigger = CronTrigger.from_crontab(cron_expression, timezone=pytz.utc)
        scheduler.add_job(
            send_to_all_groups,
            trigger=trigger,
            args=[chat_ids, message, 0, user_id],
            id=job_id,
            replace_existing=True
        )
        for chat_id in chat_ids:
            save_schedule(f"{job_id}_{chat_id}", chat_id, message, cron_expression, user_id or 0)
        logger.info(f"Scheduled job {job_id} (user {user_id}) → {len(chat_ids)} groups")
        return job_id
    except Exception as e:
        logger.error(f"schedule_message_job error: {e}")
        return None


def send_interval_message(chat_ids: list, message: str, interval_seconds: int = 60, user_id: int = None):
    job_id = f"interval_{user_id or 0}_{uuid.uuid4()}"
    try:
        scheduler.add_job(
            send_to_all_groups,
            trigger='interval',
            seconds=interval_seconds,
            args=[chat_ids, message, interval_seconds, user_id],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=120
        )
        logger.info(f"Interval job {job_id} (user {user_id}) every {interval_seconds}s → {len(chat_ids)} groups")
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


# ── Spread Spam (rolling one-by-one) ──────────────────────────────────────────
_spread_tasks: dict = {}  # job_id → asyncio.Task

# ── Anti-Ban Rule 1: Message Variation ──────────────────────────────────────
# Invisible Unicode chars — visually identical, but technically unique messages
_INVIS_CHARS = ["\u200b", "\u200c", "\u200d", "\u2060", "\u2063", "\ufeff"]

def _vary_message(msg: str) -> str:
    """
    Insert 1–3 random invisible Unicode chars at random positions.
    Message looks identical to humans but is technically unique to Telegram.
    Prevents 'duplicate message' detection.
    """
    chars = list(msg)
    n = random.randint(1, 3)
    for _ in range(n):
        pos = random.randint(0, len(chars))
        chars.insert(pos, random.choice(_INVIS_CHARS))
    return "".join(chars)


# ── Anti-Ban Rule 2: Night Pause (2AM–6AM IST) ──────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))

def _is_night_pause() -> tuple[bool, int]:
    """
    Returns (True, seconds_to_wait) if current IST time is 2AM–6AM.
    Returns (False, 0) if it's safe to send.
    """
    now = datetime.now(IST)
    hour = now.hour
    if 2 <= hour < 6:
        # Calculate seconds until 6AM IST
        six_am = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now >= six_am:  # past 6AM (shouldn't happen but safety)
            return False, 0
        wait = int((six_am - now).total_seconds())
        return True, wait
    return False, 0


# ── Anti-Ban Rule 3: Daily Limit (150 groups/day per user) ───────────────────
_daily_counters: dict = {}  # user_id → {"date": date, "count": int}
DAILY_SOFT_LIMIT = 100   # warn at this count
DAILY_HARD_LIMIT = 150   # pause for rest of day at this count

def _check_daily_limit(user_id: int) -> tuple[str, int]:
    """
    Returns ("ok", count), ("warn", count) or ("stop", seconds_till_midnight).
    """
    today = datetime.now(IST).date()
    entry = _daily_counters.get(user_id)
    if not entry or entry["date"] != today:
        _daily_counters[user_id] = {"date": today, "count": 0}
        entry = _daily_counters[user_id]

    count = entry["count"]
    if count >= DAILY_HARD_LIMIT:
        # Wait till midnight IST
        now = datetime.now(IST)
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return "stop", int((midnight - now).total_seconds())
    if count >= DAILY_SOFT_LIMIT:
        return "warn", count
    return "ok", count


def _increment_daily(user_id: int):
    today = datetime.now(IST).date()
    entry = _daily_counters.setdefault(user_id, {"date": today, "count": 0})
    if entry["date"] != today:
        entry["date"] = today
        entry["count"] = 0
    entry["count"] += 1


async def spread_spam_loop(job_id: str, chat_ids: list, message: str,
                           gap_seconds: int, user_id: int):
    """
    Rolling spam loop with full anti-ban protection:
    - Rule 1: Auto-vary message (invisible chars) — unique per send
    - Rule 2: Night pause 2AM–6AM IST automatically
    - Rule 3: Daily limit 150 groups/day
    - Rule 4: Randomized delay (80%–130% of gap)
    """
    client = get_client(user_id)
    if not client:
        logger.error(f"[spreadspam] No client for user {user_id}")
        return

    idx = 0
    total = len(chat_ids)
    logger.info(f"[spreadspam] {job_id} started — {total} groups, ~{gap_seconds}s gap (randomized)")

    while True:
        if job_id not in _spread_tasks:
            break

        # ── Rule 2: Night Pause (2AM–6AM IST) ──
        is_night, wait_night = _is_night_pause()
        if is_night:
            logger.info(f"[spreadspam] Night pause — resuming in {wait_night//60} mins (6AM IST)")
            try:
                await asyncio.sleep(wait_night + 60)  # +60s buffer
            except asyncio.CancelledError:
                break
            continue

        # ── Rule 3: Daily Limit ──
        limit_status, limit_val = _check_daily_limit(user_id)
        if limit_status == "stop":
            logger.warning(f"[spreadspam] Daily limit {DAILY_HARD_LIMIT} reached. Pausing till midnight.")
            try:
                await asyncio.sleep(limit_val + 120)
            except asyncio.CancelledError:
                break
            continue
        if limit_status == "warn":
            logger.warning(f"[spreadspam] Daily count {limit_val}/{DAILY_HARD_LIMIT} — approaching limit")

        chat_id = chat_ids[idx % total]
        multiplier = get_group_multiplier(chat_id)

        # ── Rule 1: Vary message to avoid duplicate detection ──
        varied_msg = _vary_message(message)

        try:
            await _send_with_retry(client, chat_id, varied_msg, multiplier, user_id=user_id)
            _increment_daily(user_id)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"[spreadspam] Error sending to {chat_id}: {e}")

        idx += 1

        # ── Rule 4: Slight Randomized delay (+0 to +2 seconds) ──
        # Taki ek time pe fixed na lage (e.g. 5 sec -> 5, 6, ya 7)
        actual_wait = gap_seconds + random.randint(0, 2)
        logger.info(f"[spreadspam] Waiting {actual_wait}s — group {idx % total + 1}/{total} | daily: {_daily_counters.get(user_id, {}).get('count', 0)}/{DAILY_HARD_LIMIT}")

        try:
            await asyncio.sleep(actual_wait)
        except asyncio.CancelledError:
            break

    logger.info(f"[spreadspam] {job_id} stopped")
    _spread_tasks.pop(job_id, None)


def start_spread_spam(chat_ids: list, message: str, gap_seconds: int,
                      user_id: int) -> str:
    """Start a rolling spread spam job. Returns job_id."""
    job_id = f"spread_{user_id}_{uuid.uuid4()}"
    task = asyncio.create_task(
        spread_spam_loop(job_id, chat_ids, message, gap_seconds, user_id)
    )
    _spread_tasks[job_id] = task
    logger.info(f"[spreadspam] Started {job_id}")
    return job_id


def stop_spread_spam(job_id: str) -> bool:
    """Cancel a running spread spam job."""
    task = _spread_tasks.pop(job_id, None)
    if task:
        task.cancel()
        logger.info(f"[spreadspam] Cancelled {job_id}")
        return True
    return False


def stop_all_spread_spam(user_id: int) -> int:
    """Stop all spread spam jobs for a user. Returns count stopped."""
    to_stop = [jid for jid in list(_spread_tasks) if f"_{user_id}_" in jid]
    for jid in to_stop:
        stop_spread_spam(jid)
    return len(to_stop)


def get_spread_jobs(user_id: int = None) -> list:
    """Return list of active spread job IDs, optionally filtered by user."""
    if user_id:
        return [jid for jid in _spread_tasks if f"_{user_id}_" in jid]
    return list(_spread_tasks.keys())


def reload_schedules():
    for row in get_active_schedules():
        job_id = row[0]
        chat_id = row[1]
        message = row[2]
        cron_expression = row[3]
        user_id = row[4] if len(row) > 4 else 0
        try:
            if scheduler.get_job(job_id):
                continue
            trigger = CronTrigger.from_crontab(cron_expression, timezone=pytz.utc)
            scheduler.add_job(
                send_to_all_groups,
                trigger=trigger,
                args=[[chat_id], message, 0, user_id],
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
