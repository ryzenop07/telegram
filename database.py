# database.py
import sqlite3
from logger import logger

import os

DB_NAME = os.environ.get("DB_PATH", "announcer.db")


def init_db():
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER,
                title TEXT,
                interval_multiplier REAL DEFAULT 1.0,
                is_failed INTEGER DEFAULT 0,
                added_by INTEGER DEFAULT 0,
                UNIQUE(chat_id, added_by)
            )
        ''')
        # Migrate old schema — add missing columns if not exist
        for col, definition in [
            ("interval_multiplier", "REAL DEFAULT 1.0"),
            ("is_failed", "INTEGER DEFAULT 0"),
            ("added_by", "INTEGER DEFAULT 0"),
        ]:
            try:
                c.execute(f"ALTER TABLE groups ADD COLUMN {col} {definition}")
            except Exception:
                pass
        c.execute("UPDATE groups SET interval_multiplier=1.0 WHERE interval_multiplier IS NULL")
        c.execute("UPDATE groups SET is_failed=0 WHERE is_failed IS NULL")
        c.execute('''
            CREATE TABLE IF NOT EXISTS schedules (
                job_id TEXT PRIMARY KEY,
                chat_id INTEGER,
                message TEXT,
                cron_expression TEXT,
                user_id INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1
            )
        ''')
        try:
            c.execute("ALTER TABLE schedules ADD COLUMN user_id INTEGER DEFAULT 0")
        except Exception:
            pass

        c.execute('''
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER,
                phone_number TEXT DEFAULT '',
                session_name TEXT DEFAULT '',
                session_string TEXT DEFAULT ''
            )
        ''')
        for col, definition in [
            ("phone_number", "TEXT DEFAULT ''"),
            ("session_name", "TEXT DEFAULT ''"),
            ("session_string", "TEXT DEFAULT ''"),
        ]:
            try:
                c.execute(f"ALTER TABLE allowed_users ADD COLUMN {col} {definition}")
            except Exception:
                pass

        # Track last sent message per (user, group) for delete-before-resend
        c.execute('''
            CREATE TABLE IF NOT EXISTS last_messages (
                user_id INTEGER,
                chat_id INTEGER,
                message_id INTEGER,
                PRIMARY KEY (user_id, chat_id)
            )
        ''')

        c.execute('''
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT UNIQUE COLLATE NOCASE,
                reply TEXT NOT NULL,
                is_active INTEGER DEFAULT 1
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS group_custom_messages (
                user_id INTEGER,
                chat_id INTEGER,
                message TEXT,
                PRIMARY KEY (user_id, chat_id)
            )
        ''')
        conn.commit()
    except Exception as e:
        logger.error(f"init_db error: {e}")
    finally:
        if conn:
            conn.close()


def add_allowed_user(user_id, added_by):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO allowed_users (user_id, added_by) VALUES (?,?)", (user_id, added_by))
        conn.commit()
        return c.rowcount > 0
    except Exception as e:
        logger.error(f"add_allowed_user error: {e}")
        return False
    finally:
        if conn:
            conn.close()


def remove_allowed_user(user_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("DELETE FROM allowed_users WHERE user_id=?", (user_id,))
        conn.commit()
        return c.rowcount > 0
    except Exception as e:
        logger.error(f"remove_allowed_user error: {e}")
        return False
    finally:
        if conn:
            conn.close()


def get_allowed_users():
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT user_id, added_by FROM allowed_users")
        return c.fetchall()
    except Exception as e:
        logger.error(f"get_allowed_users error: {e}")
        return []
    finally:
        if conn:
            conn.close()


def is_user_allowed(user_id, owner_id):
    if user_id == owner_id:
        return True
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT 1 FROM allowed_users WHERE user_id=?", (user_id,))
        return c.fetchone() is not None
    except Exception as e:
        logger.error(f"is_user_allowed error: {e}")
        return False
    finally:
        if conn:
            conn.close()


def update_user_session(user_id, phone="", session_name="", session_string=""):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute(
            "UPDATE allowed_users SET phone_number=?, session_name=?, session_string=? WHERE user_id=?",
            (phone, session_name, session_string, user_id)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"update_user_session error: {e}")
        return False
    finally:
        if conn:
            conn.close()


def get_user_session(user_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT phone_number, session_name, session_string FROM allowed_users WHERE user_id=?", (user_id,))
        return c.fetchone()
    except Exception as e:
        logger.error(f"get_user_session error: {e}")
        return None
    finally:
        if conn:
            conn.close()


def get_all_user_sessions():
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT user_id, phone_number, session_name, session_string FROM allowed_users WHERE session_string != '' OR session_name != ''")
        return c.fetchall()
    except Exception as e:
        logger.error(f"get_all_user_sessions error: {e}")
        return []
    finally:
        if conn:
            conn.close()


def clear_user_session(user_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE allowed_users SET phone_number='', session_name='', session_string='' WHERE user_id=?", (user_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"clear_user_session error: {e}")
        return False
    finally:
        if conn:
            conn.close()


def add_group(chat_id, title="", added_by=0):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO groups (chat_id, title, added_by) VALUES (?, ?, ?)", (chat_id, title, added_by))
        inserted = c.rowcount > 0
        if not inserted:
            c.execute("UPDATE groups SET title=? WHERE chat_id=? AND added_by=?", (title, chat_id, added_by))
        conn.commit()
        return inserted
    except Exception as e:
        logger.error(f"add_group error: {e}")
        return False
    finally:
        if conn:
            conn.close()


def remove_group(chat_id, added_by=None):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        if added_by is not None:
            c.execute("DELETE FROM groups WHERE chat_id=? AND added_by=?", (chat_id, added_by))
        else:
            c.execute("DELETE FROM groups WHERE chat_id=?", (chat_id,))
        c.execute("DELETE FROM schedules WHERE chat_id=?", (chat_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"remove_group error: {e}")
        return False
    finally:
        if conn:
            conn.close()


def get_all_groups(added_by=None):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        if added_by:
            # Owner gets all groups, others get only their own
            from config import OWNER_ID
            if added_by == OWNER_ID:
                c.execute("SELECT DISTINCT chat_id FROM groups WHERE is_failed=0")
            else:
                c.execute("SELECT chat_id FROM groups WHERE is_failed=0 AND added_by=?", (added_by,))
        else:
            c.execute("SELECT DISTINCT chat_id FROM groups WHERE is_failed=0")
        return [row[0] for row in c.fetchall()]
    except Exception as e:
        logger.error(f"get_all_groups error: {e}")
        return []
    finally:
        if conn:
            conn.close()


def get_all_groups_with_info(added_by=None):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        if added_by:
            from config import OWNER_ID
            if added_by == OWNER_ID:
                c.execute("SELECT chat_id, title, interval_multiplier, is_failed FROM groups GROUP BY chat_id")
            else:
                c.execute("SELECT chat_id, title, interval_multiplier, is_failed FROM groups WHERE added_by=?", (added_by,))
        else:
            c.execute("SELECT chat_id, title, interval_multiplier, is_failed FROM groups")
        return c.fetchall()
    except Exception as e:
        logger.error(f"get_all_groups_with_info error: {e}")
        return []
    finally:
        if conn:
            conn.close()


def get_failed_groups(added_by=None):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        if added_by is not None:
            c.execute("SELECT chat_id, title FROM groups WHERE is_failed=1 AND added_by=?", (added_by,))
        else:
            c.execute("SELECT chat_id, title FROM groups WHERE is_failed=1")
        return c.fetchall()
    except Exception as e:
        logger.error(f"get_failed_groups error: {e}")
        return []
    finally:
        if conn:
            conn.close()


def mark_group_failed(chat_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE groups SET is_failed=1 WHERE chat_id=?", (chat_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"mark_group_failed error: {e}")
        return False
    finally:
        if conn:
            conn.close()


def clear_group_failed(chat_id, added_by=None):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        if added_by is not None:
            c.execute("UPDATE groups SET is_failed=0, interval_multiplier=1.0 WHERE chat_id=? AND added_by=?", (chat_id, added_by))
        else:
            c.execute("UPDATE groups SET is_failed=0, interval_multiplier=1.0 WHERE chat_id=?", (chat_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"clear_group_failed error: {e}")
        return False
    finally:
        if conn:
            conn.close()


def get_group_multiplier(chat_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT interval_multiplier FROM groups WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
        return row[0] if row else 1.0
    except Exception as e:
        logger.error(f"get_group_multiplier error: {e}")
        return 1.0
    finally:
        if conn:
            conn.close()


def set_group_multiplier(chat_id, multiplier):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE groups SET interval_multiplier=? WHERE chat_id=?", (multiplier, chat_id))
        conn.commit()
    except Exception as e:
        logger.error(f"set_group_multiplier error: {e}")
    finally:
        if conn:
            conn.close()


def get_active_schedules():
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT job_id, chat_id, message, cron_expression, user_id FROM schedules WHERE is_active=1")
        return c.fetchall()
    except Exception as e:
        logger.error(f"get_active_schedules error: {e}")
        return []
    finally:
        if conn:
            conn.close()


def save_schedule(job_id, chat_id, message, cron_expression, user_id=0):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO schedules (job_id, chat_id, message, cron_expression, user_id, is_active) VALUES (?,?,?,?,?,1)",
            (job_id, chat_id, message, cron_expression, user_id)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"save_schedule error: {e}")
        return False
    finally:
        if conn:
            conn.close()


# ── Keyword functions ─────────────────────────────────────────────────────────
def add_keyword(keyword, reply):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO keywords (keyword, reply, is_active) VALUES (?,?,1)", (keyword.lower().strip(), reply))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"add_keyword error: {e}")
        return False
    finally:
        if conn: conn.close()


def remove_keyword(keyword):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("DELETE FROM keywords WHERE keyword=?", (keyword.lower().strip(),))
        conn.commit()
        return c.rowcount > 0
    except Exception as e:
        logger.error(f"remove_keyword error: {e}")
        return False
    finally:
        if conn: conn.close()


def get_all_keywords(page=0, per_page=20):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT keyword, reply, is_active FROM keywords ORDER BY keyword LIMIT ? OFFSET ?", (per_page, page * per_page))
        return c.fetchall()
    except Exception as e:
        logger.error(f"get_all_keywords error: {e}")
        return []
    finally:
        if conn: conn.close()


def get_keyword_count():
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM keywords WHERE is_active=1")
        return c.fetchone()[0]
    except Exception as e:
        return 0
    finally:
        if conn: conn.close()


def find_reply(text):
    """Find best matching keyword reply for given text."""
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        text_lower = text.lower().strip()
        # Exact match first
        c.execute("SELECT reply FROM keywords WHERE is_active=1 AND keyword=?", (text_lower,))
        row = c.fetchone()
        if row:
            return row[0]
        # Partial match — keyword appears anywhere in message
        c.execute("SELECT keyword, reply FROM keywords WHERE is_active=1")
        for keyword, reply in c.fetchall():
            if keyword in text_lower:
                return reply
        return None
    except Exception as e:
        logger.error(f"find_reply error: {e}")
        return None
    finally:
        if conn: conn.close()



def get_slowmode(chat_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT slowmode_delay FROM groups WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
        return row[0] if row and row[0] else 0
    except Exception as e:
        logger.error(f"get_slowmode error: {e}")
        return 0
    finally:
        if conn: conn.close()


def set_slowmode(chat_id, delay):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE groups SET slowmode_delay=? WHERE chat_id=?", (delay, chat_id))
        conn.commit()
    except Exception as e:
        logger.error(f"set_slowmode error: {e}")
    finally:
        if conn: conn.close()

def deactivate_schedule(job_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE schedules SET is_active=0 WHERE job_id=?", (job_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"deactivate_schedule error: {e}")
        return False
    finally:
        if conn:
            conn.close()


# ── Last Message Tracking (delete-before-resend) ──────────────────────────────

def get_last_message_id(user_id: int, chat_id: int):
    """Return the last sent message_id for this (user, chat), or None."""
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute(
            "SELECT message_id FROM last_messages WHERE user_id=? AND chat_id=?",
            (user_id, chat_id)
        )
        row = c.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"get_last_message_id error: {e}")
        return None
    finally:
        if conn:
            conn.close()


def set_last_message_id(user_id: int, chat_id: int, message_id: int):
    """Save/update the last sent message_id for this (user, chat)."""
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO last_messages (user_id, chat_id, message_id) VALUES (?,?,?)",
            (user_id, chat_id, message_id)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"set_last_message_id error: {e}")
    finally:
        if conn:
            conn.close()


def clear_last_message_id(user_id: int, chat_id: int):
    """Remove the tracked message_id (e.g., after confirmed deletion)."""
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute(
            "DELETE FROM last_messages WHERE user_id=? AND chat_id=?",
            (user_id, chat_id)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"clear_last_message_id error: {e}")
    finally:
        if conn:
            conn.close()


# ── Group Custom Messages ─────────────────────────────────────────────────────

def set_group_custom_message(user_id: int, chat_id: int, message: str):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO group_custom_messages (user_id, chat_id, message) VALUES (?,?,?)",
            (user_id, chat_id, message)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"set_group_custom_message error: {e}")
        return False
    finally:
        if conn: conn.close()


def get_group_custom_message(user_id: int, chat_id: int):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute(
            "SELECT message FROM group_custom_messages WHERE user_id=? AND chat_id=?",
            (user_id, chat_id)
        )
        row = c.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"get_group_custom_message error: {e}")
        return None
    finally:
        if conn: conn.close()


def clear_group_custom_message(user_id: int, chat_id: int):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute(
            "DELETE FROM group_custom_messages WHERE user_id=? AND chat_id=?",
            (user_id, chat_id)
        )
        conn.commit()
        return c.rowcount > 0
    except Exception as e:
        logger.error(f"clear_group_custom_message error: {e}")
        return False
    finally:
        if conn: conn.close()
