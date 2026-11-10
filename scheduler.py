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
    ChatAdminRequired, UserNotParticipant, ChannelInvalid
)

from database import (
    get_active_schedules, save_schedule, deactivate_schedule,
    get_group_multiplier, set_group_multiplier, mark_group_failed, remove_group,
    get_last_message_id, set_last_message_id, clear_last_message_id,
    get_slowmode, set_slowmode, get_group_custom_message
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


async def _send_with_retry(client, chat_id: int, message: str, multiplier: float, user_id: int = 0, delete_prev: bool = False):
    """Send to one group. If delete_prev=True, deletes last sent message first (for interval/schedule jobs)."""
    if not client:
        logger.error(f"No Pyrogram client available for {chat_id}")
        return False

    # Delete previous interval/schedule message if requested
    if delete_prev:
        prev_msg_id = get_last_message_id(user_id, chat_id)
        if prev_msg_id:
            try:
                await client.delete_messages(chat_id, [prev_msg_id])
            except Exception:
                pass
            clear_last_message_id(user_id, chat_id)

    try:
        sent = await client.send_message(chat_id, message)
        if delete_prev:
            set_last_message_id(user_id, chat_id, sent.id)
        logger.info(f"✅ Sent to {chat_id} (msg_id={sent.id})")
        _error_counts[chat_id] = 0
        if multiplier > 1.0:
            set_group_multiplier(chat_id, max(1.0, round(multiplier * 0.9, 2)))
        return True

    except FloodWait as e:
        wait = e.value + 5
        logger.warning(f"⏳ FloodWait {wait}s for {chat_id}")
        # Backoff badha do — har FloodWait pe 1.5x
        current = _group_backoff.get(chat_id, 0)
        _group_backoff[chat_id] = min(int(max(wait, current) * 1.5), 3600)
        logger.warning(f"🔺 Group {chat_id} backoff set to {_group_backoff[chat_id]}s")
        await asyncio.sleep(wait)
        try:
            sent = await client.send_message(chat_id, message)
            _error_counts[chat_id] = 0
            return True
        except Exception as e2:
            logger.error(f"Retry failed {chat_id}: {e2}")
            _increment_error(chat_id)
            return False

    except SlowmodeWait as e:
        logger.warning(f"⏳ SlowmodeWait {e.value}s for {chat_id} — skipping")
        set_slowmode(chat_id, e.value)
        # Backoff bhi update karo
        _group_backoff[chat_id] = max(_group_backoff.get(chat_id, 0), e.value)
        return False

    except PeerFlood:
        logger.error("🚨 PeerFlood! Account spam ban. Pausing 5 minutes.")
        await asyncio.sleep(300)
        _increment_error(chat_id)
        return False

    except (UserBannedInChannel, ChatWriteForbidden, PeerIdInvalid,
            ChannelPrivate, ChatAdminRequired, UserNotParticipant,
            ChannelInvalid) as e:
        logger.error(f"❌ Permanent error {chat_id}: {type(e).__name__} — removing from DB")
        remove_group(chat_id)
        _error_counts[chat_id] = 0
        return False

    except Exception as e:
        err_str = str(e)
        if "CHANNEL_INVALID" in err_str or "CHANNEL_PRIVATE" in err_str:
            logger.error(f"❌ Permanent error (str match) {chat_id}: {e} — removing from DB")
            remove_group(chat_id)
            _error_counts[chat_id] = 0
            return False
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
        slowmode = get_slowmode(chat_id)

        if i > 0:
            # 1-3s random gap between groups
            gap = random.randint(1, 3)
            # agar slowmode hai toh usse bhi add karo
            wait = gap + (slowmode if slowmode > 0 else 0)
            logger.info(f"Group {chat_id} — waiting {wait}s (gap={gap}s, slowmode={slowmode}s)")
            await asyncio.sleep(wait)

        await _send_with_retry(client, chat_id, message, multiplier, user_id=user_id or 0, delete_prev=False)


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
_pinned_groups: dict = {}  # user_id → set of chat_ids being spammed via spamgroup
_group_backoff: dict = {}  # chat_id → current backoff seconds

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
    logger.info(f"[spam] {job_id} started — {len(chat_ids)} groups, auto slowmode")

    now = asyncio.get_event_loop().time
    next_send = {chat_id: now() for chat_id in chat_ids}

    while True:
        if job_id not in _spread_tasks:
            break

        client = get_client(user_id)
        if not client:
            logger.error(f"[spam] No client for user {user_id} — stopping job")
            break

        # Daily Limit check removed

        sent_any = False
        for chat_id in list(chat_ids):
            if job_id not in _spread_tasks:
                break
            if now() < next_send.get(chat_id, 0):
                continue

            logger.info(f"[spam] Attempting send to {chat_id}")
            slowmode = get_slowmode(chat_id)
            multiplier = get_group_multiplier(chat_id)            custom_msg = get_group_custom_message(user_id, chat_id)
            varied_msg = _vary_message(custom_msg if custom_msg else message)

            sent = False
            try:
                sent = await _send_with_retry(client, chat_id, varied_msg, multiplier, user_id=user_id, delete_prev=False)
                if sent:
                    _increment_daily(user_id)
                    sent_any = True
                elif _error_counts.get(chat_id, 0) == 0 and not sent:
                    # Permanently failed — remove from active list
                    if chat_id in chat_ids:
                        chat_ids.remove(chat_id)
                        logger.info(f"[spam] {chat_id} removed from active list (permanent fail)")
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"[spam] Error {chat_id}: {e}")

            # Re-fetch slowmode in case SlowmodeWait just updated it
            slowmode = get_slowmode(chat_id)
            backoff = _group_backoff.get(chat_id, 0)
            jitter = random.randint(1, 3)
            wait = max(slowmode, backoff) + jitter
            # Slowly reduce backoff over time (decay)
            if backoff > 0:
                _group_backoff[chat_id] = max(0, int(backoff * 0.8))
                logger.info(f"📉 Group {chat_id} backoff decayed to {_group_backoff[chat_id]}s")
            next_send[chat_id] = now() + wait
            logger.info(f"[spam] {chat_id} next in {wait}s (slowmode={slowmode}s, backoff={backoff}s)")

            try:
                await asyncio.sleep(random.randint(1, 3))
            except asyncio.CancelledError:
                return

        if not sent_any:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break

    logger.info(f"[spam] {job_id} stopped")
    _spread_tasks.pop(job_id, None)


def start_spread_spam(chat_ids: list, message: str, gap_seconds: int,
                      user_id: int, pinned: bool = False) -> str:
    client = get_client(user_id)
    if not client:
        logger.error(f"[spreadspam] No client for user {user_id} — cannot start")
        return None
    if pinned:
        _pinned_groups.setdefault(user_id, set()).update(chat_ids)
    job_id = f"spread_{user_id}_{uuid.uuid4()}"
    task = asyncio.create_task(
        spread_spam_loop(job_id, chat_ids, message, gap_seconds, user_id)
    )
    _spread_tasks[job_id] = task
    logger.info(f"[spreadspam] Started {job_id} pinned={pinned}")
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
    to_stop = [jid for jid in list(_spread_tasks) if f"_{user_id}_" in jid]
    for jid in to_stop:
        stop_spread_spam(jid)
    _pinned_groups.pop(user_id, None)
    return len(to_stop)


def get_pinned_groups(user_id: int) -> set:
    return _pinned_groups.get(user_id, set())


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
