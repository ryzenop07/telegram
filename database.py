# database.py
import sqlite3
from logger import logger

DB_NAME = "announcer.db"

def init_db():
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        # Store group chat IDs
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER UNIQUE
            )
        ''')
        # Store scheduled messages (simplistic for now)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE,
                chat_id INTEGER,
                message TEXT,
                cron_expression TEXT,
                is_active BOOLEAN DEFAULT TRUE
            )
        ''')
        conn.commit()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
    finally:
        if conn:
            conn.close()

def add_group(chat_id):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO groups (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
        logger.info(f"Group {chat_id} added to database.")
        return True
    except Exception as e:
        logger.error(f"Error adding group {chat_id}: {e}")
        return False
    finally:
        if conn:
            conn.close()

def remove_group(chat_id):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM groups WHERE chat_id = ?", (chat_id,))
        # Also remove any schedules associated with this group
        cursor.execute("DELETE FROM schedules WHERE chat_id = ?", (chat_id,))
        conn.commit()
        logger.info(f"Group {chat_id} removed from database and its schedules.")
        return True
    except Exception as e:
        logger.error(f"Error removing group {chat_id}: {e}")
        return False
    finally:
        if conn:
            conn.close()

def get_all_groups():
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM groups")
        groups = [row[0] for row in cursor.fetchall()]
        return groups
    except Exception as e:
        logger.error(f"Error fetching all groups: {e}")
        return []
    finally:
        if conn:
            conn.close()

def get_active_schedules():
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT job_id, chat_id, message, cron_expression FROM schedules WHERE is_active = TRUE")
        schedules = cursor.fetchall()
        return schedules
    except Exception as e:
        logger.error(f"Error fetching active schedules: {e}")
        return []
    finally:
        if conn:
            conn.close()

def save_schedule(job_id, chat_id, message, cron_expression):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO schedules (job_id, chat_id, message, cron_expression) VALUES (?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET chat_id=excluded.chat_id, message=excluded.message, cron_expression=excluded.cron_expression, is_active=TRUE",
            (job_id, chat_id, message, cron_expression)
        )
        conn.commit()
        logger.info(f"Schedule saved for job_id: {job_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving schedule for job_id {job_id}: {e}")
        return False
    finally:
        if conn:
            conn.close()

def delete_schedule(job_id):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM schedules WHERE job_id = ?", (job_id,))
        conn.commit()
        logger.info(f"Schedule deleted for job_id: {job_id}")
        return True
    except Exception as e:
        logger.error(f"Error deleting schedule for job_id {job_id}: {e}")
        return False
    finally:
        if conn:
            conn.close()

def deactivate_schedule(job_id):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("UPDATE schedules SET is_active = FALSE WHERE job_id = ?", (job_id,))
        conn.commit()
        logger.info(f"Schedule deactivated for job_id: {job_id}")
        return True
    except Exception as e:
        logger.error(f"Error deactivating schedule for job_id {job_id}: {e}")
        return False
    finally:
        if conn:
            conn.close()
