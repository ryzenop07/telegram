# database.py
import sqlite3
from logger import logger

DB_NAME = "announcer.db"


def init_db():
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                interval_multiplier REAL DEFAULT 1.0,
                is_failed INTEGER DEFAULT 0
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS schedules (
                job_id TEXT PRIMARY KEY,
                chat_id INTEGER,
                message TEXT,
                cron_expression TEXT,
                is_active INTEGER DEFAULT 1
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER
            )
        ''')
        conn.commit()
        logger.info("Database initialized.")
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
        c.execute("SELECT user_id FROM allowed_users")
        return {row[0] for row in c.fetchall()}
    except Exception as e:
        logger.error(f"get_allowed_users error: {e}")
        return set()
    finally:
        if conn:
            conn.close()


def is_user_allowed(user_id, owner_id):
    if user_id == owner_id:
        return True
    return user_id in get_allowed_users()


    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute(
            "INSERT OR IGNORE INTO groups (chat_id, title) VALUES (?, ?)",
            (chat_id, title)
        )
        # update title if already exists
        c.execute("UPDATE groups SET title=? WHERE chat_id=?", (title, chat_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"add_group error: {e}")
        return False
    finally:
        if conn:
            conn.close()


def add_group(chat_id, title=""):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO groups (chat_id, title) VALUES (?, ?)", (chat_id, title))
        c.execute("UPDATE groups SET title=? WHERE chat_id=?", (title, chat_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"add_group error: {e}")
        return False
    finally:
        if conn:
            conn.close()


def remove_group(chat_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
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


def get_all_groups():
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT chat_id FROM groups WHERE is_failed=0")
        return [row[0] for row in c.fetchall()]
    except Exception as e:
        logger.error(f"get_all_groups error: {e}")
        return []
    finally:
        if conn:
            conn.close()


def get_all_groups_with_info():
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT chat_id, title, interval_multiplier, is_failed FROM groups")
        return c.fetchall()
    except Exception as e:
        logger.error(f"get_all_groups_with_info error: {e}")
        return []
    finally:
        if conn:
            conn.close()


def get_failed_groups():
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
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


def clear_group_failed(chat_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
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
        c.execute("SELECT job_id, chat_id, message, cron_expression FROM schedules WHERE is_active=1")
        return c.fetchall()
    except Exception as e:
        logger.error(f"get_active_schedules error: {e}")
        return []
    finally:
        if conn:
            conn.close()


def save_schedule(job_id, chat_id, message, cron_expression):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO schedules (job_id, chat_id, message, cron_expression, is_active) VALUES (?,?,?,?,1)",
            (job_id, chat_id, message, cron_expression)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"save_schedule error: {e}")
        return False
    finally:
        if conn:
            conn.close()


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
