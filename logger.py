# logger.py
import logging


class _SuppressNoise(logging.Filter):
    """Suppress repetitive Pyrogram internal retry spam that is harmless noise."""
    _SUPPRESS = (
        "MESSAGE_IDS_EMPTY",
        "CHAT_WRITE_FORBIDDEN",
        "CHANNEL_INVALID",
        "CHAT_ID_INVALID",
        "SLOWMODE_WAIT_X",
        "Retrying",
        "Waiting for",
        "channels.GetMessages",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(kw in msg for kw in self._SUPPRESS)


def setup_logger():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Suppress noisy retry spam from all Pyrogram loggers
    for name in ["pyrogram.session.session", "pyrogram.dispatcher", "pyrogram.connection.connection"]:
        logging.getLogger(name).addFilter(_SuppressNoise())
    logging.getLogger("pyrogram").addFilter(_SuppressNoise())

    return logging.getLogger(__name__)


def patch_pyrogram_session():
    """
    Patch Pyrogram's Client.get_messages to gracefully handle MessageIdsEmpty
    instead of crashing with AttributeError('NoneType has no attribute users').

    Root cause: When bot/user client receives group updates, some messages have
    empty reply_to IDs. Pyrogram calls get_messages with those empty IDs,
    Telegram returns MESSAGE_IDS_EMPTY (500), Pyrogram retries 9 times, then
    raises — and message._parse crashes because it doesn't catch MessageIdsEmpty.

    Also patches ChatWriteForbidden to not retry (permanent error, not transient).
    """
    try:
        from pyrogram import Client
        from pyrogram.errors import MessageIdsEmpty, ChatWriteForbidden
        from pyrogram.session.session import Session

        # ── Patch 1: get_messages → catch MessageIdsEmpty cleanly ────────────
        _orig_get_messages = Client.get_messages

        async def _patched_get_messages(self, chat_id, message_ids=None,
                                        reply_to_message_ids=None, replies=1):
            try:
                return await _orig_get_messages(
                    self, chat_id,
                    message_ids=message_ids,
                    reply_to_message_ids=reply_to_message_ids,
                    replies=replies
                )
            except (MessageIdsEmpty, AttributeError):
                # Return None for single ID, [] for list — message._parse handles None gracefully
                is_list = isinstance(message_ids, (list, tuple)) or \
                          isinstance(reply_to_message_ids, (list, tuple))
                return [] if is_list else None

        Client.get_messages = _patched_get_messages

        # ── Patch 2: Session.invoke → don't retry on permanent errors ────────
        _orig_invoke = Session.invoke
        from pyrogram.errors import SlowmodeWait

        async def _patched_invoke(self, query, retries=Session.MAX_RETRIES,
                                  timeout=Session.WAIT_TIMEOUT,
                                  sleep_threshold=Session.SLEEP_THRESHOLD):
            try:
                return await _orig_invoke(
                    self, query, retries=retries,
                    timeout=timeout, sleep_threshold=sleep_threshold
                )
            except MessageIdsEmpty:
                raise
            except ChatWriteForbidden:
                raise
            except SlowmodeWait:
                raise  # Don't retry — scheduler.py handles wait + retry

        Session.invoke = _patched_invoke

    except Exception:
        pass  # If patch fails, bot still works — just with noisy logs


logger = setup_logger()
