# Telegram Announcement Bot

This bot allows you to manage announcements and interval messages to Telegram groups.

## Features
- Add/Remove groups for announcements.
- Schedule daily announcements at a specific time.
- Send messages at a fixed interval (e.g., every 30 seconds).
- Store groups and schedules in an SQLite database.
- Reload schedules on bot restart.

## Setup

1.  **Prerequisites:**
    *   Python 3.7+
    *   A Telegram Bot Token from [BotFather](https://t.me/botfather).
    *   Your Telegram API ID and API Hash from [my.telegram.org](https://my.telegram.org/).

2.  **Clone the repository (or create files):**
    ```bash
    # If using git:
    # git clone <your-repo-url>
    # cd telegram_announcer
    ```

3.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Configure:**
    *   Create a file named `config.py` in the same directory as `bot.py`.
    *   Add your API credentials and bot token:
        ```python
        # config.py
        API_ID = "YOUR_API_ID"
        API_HASH = "YOUR_API_HASH"
        BOT_TOKEN = "YOUR_BOT_TOKEN"
        ```
    *   **IMPORTANT:** Add `config.py` to your `.gitignore` file if you are using Git.

5.  **Run the Bot:**
    ```bash
    python bot.py
    ```

## Usage

Send commands to the bot in a **private chat** with it.

*   **/addgroup**: Use this command in a private chat with the bot. The bot will then need to be added to the target group(s) for it to be able to send messages there.
*   **/removegroup**: Use this command in a private chat with the bot to remove a group.
*   **/listgroups**: Lists all registered groups.
*   **/schedule HH:MM Message**: Schedules a daily announcement.
    *   Example: `/schedule 15:00 Important meeting tomorrow!`
    *   The bot will send the `Message` daily at `HH:MM` UTC.
*   **/stop JOB_ID**: Stops a scheduled announcement. You will see the `JOB_ID` when you schedule a message.
*   **/interval SECONDS Message**: Schedules a recurring message.
    *   Example: `/interval 30 Send this every 30 seconds.`
    *   **NOTE:** The actual sending logic for interval messages needs to be implemented within the `send_interval_message_callback` function in `scheduler.py`. The current implementation is a placeholder.

## Running on Termux / Without a VPS

You can run this script on Termux or a regular Linux/Windows machine.
- **Termux:**
    1.  Install Python: `pkg install python`
    2.  Install dependencies: `pip install -r requirements.txt`
    3.  Run the bot: `python bot.py`
    4.  To keep it running in the background, consider using `tmux` or `screen`.
        *   Install `tmux`: `pkg install tmux`
        *   Start `tmux`: `tmux`
        *   Run the bot: `python bot.py`
        *   Detach from `tmux` (to keep it running): `Ctrl+B` then `D`. Reattach with `tmux attach`.
- **Linux/Windows:**
    1.  Run `python bot.py` in your terminal.
    2.  For continuous running without a VPS, you would typically use `screen` or `tmux` on Linux, or a process manager like `pm2` (for Node.js, but there are Python equivalents) or run it as a service.

## Rate Limits & Best Practices

- Telegram has strict rate limits. Sending too many messages too quickly can lead to temporary bans or flood waits.
- The `/interval` command can easily hit rate limits. Use it cautiously and with longer intervals.
- The `apscheduler` library helps manage schedules, but you are responsible for not exceeding Telegram's limits.
- Error handling is included, but robust production use might require more advanced techniques.
