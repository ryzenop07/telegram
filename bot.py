# bot.py
import asyncio
import re

from pyrogram import Client, filters, raw
import pyrogram.handlers
from pyrogram.types import Message, Chat
from pyrogram.errors import (
    UserAlreadyParticipant, InviteHashExpired, InviteHashInvalid,
    SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired,
    PasswordHashInvalid, ChatWriteForbidden, ChatAdminRequired,
    ChannelPrivate, PeerIdInvalid, FloodWait, InviteRequestSent
)
from pyrogram.enums import ChatType, ParseMode

from logger import logger, patch_pyrogram_session

# Patch Pyrogram to silence MESSAGE_IDS_EMPTY retry spam (library bug, harmless)
try:
    patch_pyrogram_session()
except Exception:
    pass
from database import (
    init_db, add_group, remove_group,
    get_all_groups, get_all_groups_with_info,
    get_failed_groups, clear_group_failed,
    add_allowed_user, remove_allowed_user, get_allowed_users, is_user_allowed,
    update_user_session, get_user_session, get_all_user_sessions, clear_user_session,
    add_keyword, remove_keyword, get_all_keywords, get_keyword_count, find_reply,
    get_slowmode, set_slowmode,
    get_group_multiplier, set_group_multiplier
)
from scheduler import (
    start_scheduler, stop_scheduler,
    schedule_message_job, stop_job,
    send_interval_message, set_client,
    register_client, unregister_client, scheduler,
    start_spread_spam, stop_spread_spam,
    stop_all_spread_spam, get_spread_jobs, get_pinned_groups
)
import config

# ── Init DB before any DB calls ───────────────────────────────────────────────
init_db()

# ── Multi-Client Storage ───────────────────────────────────────────────────────
# user_id -> Client
user_clients = {}
# user_id -> {"phone": ..., "client": ..., "phone_code_hash": ..., "step": ...}
login_states = {}

# Owner session — env var first (Railway), then DB, else phone login
_owner_sstr = config.OWNER_SESSION or None
if not _owner_sstr:
    _owner_session = get_user_session(config.OWNER_ID)
    _owner_sstr = _owner_session[2] if _owner_session and _owner_session[2] else None

if _owner_sstr:
    user_app = Client(
        f"user_{config.OWNER_ID}",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        session_string=_owner_sstr,
        parse_mode=ParseMode.MARKDOWN
    )
else:
    user_app = Client(
        "user_session",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        phone_number=config.PHONE_NUMBER,
        parse_mode=ParseMode.MARKDOWN
    )

bot_app = Client(
    "bot_session",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    parse_mode=ParseMode.MARKDOWN
)

def get_user_client(uid: int):
    """Return Pyrogram user client for given Telegram user ID."""
    if uid in user_clients:
        return user_clients[uid]
    if uid == config.OWNER_ID:
        return user_app
    return None


async def auto_join_db_groups(uclient, uid: int, notify_chat_id: int = None):
    """
    Silently join all groups stored in DB for this user.
    Called automatically after session login and on startup.
    """
    rows = get_all_groups_with_info(uid)
    if not rows:
        return
    ok, fail = 0, 0
    for chat_id, title, _, _ in rows:
        try:
            await uclient.join_chat(chat_id)
            ok += 1
            logger.info(f"Auto-joined {title or chat_id} for user {uid}")
        except UserAlreadyParticipant:
            ok += 1  # Already in — count as success
        except AttributeError:
            # Pyrogram bug: 'ChatInviteJoinResultOk' has no 'chats' attribute
            # This means user is already a member — treat as success
            ok += 1
        except Exception as e:
            err = str(e)
            if any(x in err for x in ["CHANNEL_INVALID", "CHAT_ID_INVALID", "CHANNEL_PRIVATE", "GetMessages"]):
                ok += 1  # Already member or inaccessible — not a real failure
            else:
                fail += 1
                logger.warning(f"Auto-join failed {title or chat_id} for user {uid}: {e}")
    logger.info(f"Auto-join done for user {uid}: {ok} already/joined, {fail} real failures")
    if notify_chat_id:
        try:
            await bot_app.send_message(
                notify_chat_id,
                f"🤖 **Auto-join complete!**\n"
                f"✅ Joined: {ok} groups\n"
                f"❌ Failed: {fail} groups\n\n"
                f"Sabhi DB groups mein automatically rejoin ho gaya!"
            )
        except Exception:
            pass

# ── Filters ────────────────────────────────────────────────────────────────────
def allowed_only(_, __, message: Message):
    return message.from_user and is_user_allowed(message.from_user.id, config.OWNER_ID)

def owner_only(_, __, message: Message):
    return message.from_user and message.from_user.id == config.OWNER_ID

allowed_filter = filters.create(allowed_only)
owner_filter = filters.create(owner_only)

# ── Helpers ────────────────────────────────────────────────────────────────────
def parse_cron(time_str):
    if re.match(r"^\d{2}:\d{2}$", time_str):
        hour, minute = map(int, time_str.split(':'))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return f"{minute} {hour} * * *"
    return None

async def join_and_add(target: str, added_by: int = 0):
    uclient = get_user_client(added_by)
    if not uclient:
        return None, "Aapka account session connected nahi hai. Pehle /login karein."
    try:
        # Normalize t.me links → pyrogram format
        target = target.strip()
        if "t.me/+" in target or "t.me/joinchat" in target:
            pass
        elif "t.me/" in target:
            target = "@" + target.split("t.me/")[-1].strip("/")
        try:
            await uclient.join_chat(target)
        except UserAlreadyParticipant:
            pass
        except InviteHashExpired:
            return None, "Invite link expired."
        chat = await uclient.get_chat(target)
        add_group(chat.id, chat.title or "", added_by)
        # Fetch and save slowmode delay
        try:
            slowmode = getattr(chat, 'slowmode_delay', 0) or 0
            if slowmode > 0:
                set_slowmode(chat.id, slowmode)
                logger.info(f"Group {chat.id} slowmode={slowmode}s saved")
        except Exception:
            pass
        return chat, None
    except Exception as e:
        return None, str(e)

# ── /help ──────────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("help") & filters.private & allowed_filter)
async def help_cmd(client, message: Message):
    is_owner = message.from_user.id == config.OWNER_ID
    owner_section = (
        "👤 **Users** _(sirf owner)_\n"
        "`/adduser [user_id]` — Kisi ko access do\n"
        "`/removeuser [user_id]` — Access hatao\n"
        "`/listusers` — Allowed users dekho\n"
        "`/getsession [phone]` — Kisi bhi number ka session bot se generate karo\n\n"
    ) if is_owner else ""

    await message.reply(
        "📋 **Bot Commands**\n\n"

        "🔐 **Account & Session**\n"
        "`/login [phone]` — Apna Telegram account connect karo\n"
        "`/otp [code]` — OTP enter karo\n"
        "`/password [pwd]` — 2FA password (agar ho)\n"
        "`/session [string]` — Session string se connect karo\n"
        "`/myaccount` — Connected account status dekho\n"
        "`/logout` — Account disconnect karo\n\n"

        "👥 **Groups**\n"
        "`/addgroup [link/@username/id]` — Group add + auto join\n"
        "`/removegroup [group_id]` — Group remove karo\n"
        "`/listgroups` — Registered groups dekho\n"
        "`/syncgroups` — Joined groups DB mein sync karo\n"
        "`/autojoin` — DB ke saare groups mein rejoin karo\n\n"

        "📡 **Channel Scan**\n"
        "`/scanchannel [@username/id]` — Channel se group links nikal ke auto-join karo\n"
        "`/stopscan` — Scan band karo\n\n"

        "📨 **Spam**\n"
        "`/spam [message]` — Saare groups mein spam (failed groups skip honge)\n"
        "`/spamgroup [id] [seconds] [msg]` — Specific group mein fixed interval pe spam\n"
        "`/stopspam` — Spam band karo\n\n"

        "⚠️ **Failed Groups**\n"
        "`/faillist` — Failed groups ki list\n"
        "`/clearfail [group_id]` — Ek group fail list se hatao\n"
        "`/clearfail all` — Saare failed groups clear karo\n"
        "_(Failed group mein spam tabhi hoga jab /clearfail karo)_\n\n"

        + owner_section +

        "💡 **Examples**\n"
        "`/getsession +91XXXXXXXXXX` — Session generate karo\n"
        "`/syncgroups` — Groups sync karo\n"
        "`/spam Aaj sale hai!` — Spam shuru\n"
        "`/spamgroup -100123 10 Hello!` — Specific group mein 10s pe spam"
    )

# ── /getsession ──────────────────────────────────────────────────────────────
_getsession_states: dict = {}  # owner_id -> {phone, client, hash, step}

@bot_app.on_message(filters.command("getsession") & filters.private & owner_filter)
async def getsession_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: `/getsession +91XXXXXXXXXX`")
        return
    phone = parts[1].strip().replace(" ", "")
    status_msg = await message.reply(f"🔄 `{phone}` pe OTP bheja ja raha hai...")
    try:
        temp = Client(f"gs_{phone.replace('+','')}", api_id=config.API_ID, api_hash=config.API_HASH, in_memory=True)
        await temp.connect()
        sent = await temp.send_code(phone)
        _getsession_states[message.from_user.id] = {
            "phone": phone, "client": temp,
            "hash": sent.phone_code_hash, "step": "otp"
        }
        await status_msg.edit_text(
            f"📩 OTP bhej diya `{phone}` pe!\n"
            f"Code bhejo: `/gsotp 12345`"
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: `{e}`")


@bot_app.on_message(filters.command("gsotp") & filters.private & owner_filter)
async def gsotp_cmd(client, message: Message):
    uid = message.from_user.id
    state = _getsession_states.get(uid)
    if not state or state["step"] != "otp":
        await message.reply("❌ Pehle `/getsession [phone]` karo.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: `/gsotp [code]`")
        return
    code = parts[1].strip().replace(" ", "").replace("-", "")
    temp = state["client"]
    status_msg = await message.reply("🔄 Verifying...")
    try:
        await temp.sign_in(phone_number=state["phone"], phone_code_hash=state["hash"], phone_code=code)
    except SessionPasswordNeeded:
        state["step"] = "pwd"
        await status_msg.edit_text("🔒 2FA password chahiye: `/gspwd your_password`")
        return
    except PhoneCodeInvalid:
        await status_msg.edit_text("❌ Galat OTP. Dobara try karo.")
        return
    except PhoneCodeExpired:
        _getsession_states.pop(uid, None)
        await status_msg.edit_text("❌ OTP expire. `/getsession` dobara karo.")
        return
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: `{e}`")
        return
    sstr = await temp.export_session_string()
    me = await temp.get_me()
    await temp.disconnect()
    _getsession_states.pop(uid, None)
    await status_msg.edit_text(
        f"✅ **Session Ready!**\n"
        f"👤 {me.first_name} (`{me.id}`)\n\n"
        f"🔑 **String Session:**\n`{sstr}`\n\n"
        f"Is string ko `/session` command mein use karo."
    )


@bot_app.on_message(filters.command("gspwd") & filters.private & owner_filter)
async def gspwd_cmd(client, message: Message):
    uid = message.from_user.id
    state = _getsession_states.get(uid)
    if not state or state["step"] != "pwd":
        await message.reply("❌ Pehle `/getsession` karo.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: `/gspwd [password]`")
        return
    temp = state["client"]
    status_msg = await message.reply("🔄 Verifying 2FA...")
    try:
        await temp.check_password(parts[1].strip())
        sstr = await temp.export_session_string()
        me = await temp.get_me()
        await temp.disconnect()
        _getsession_states.pop(uid, None)
        await status_msg.edit_text(
            f"✅ **Session Ready!**\n"
            f"👤 {me.first_name} (`{me.id}`)\n\n"
            f"🔑 **String Session:**\n`{sstr}`\n\n"
            f"Is string ko `/session` command mein use karo."
        )
    except PasswordHashInvalid:
        await status_msg.edit_text("❌ Galat password. Dobara: `/gspwd [pwd]`")
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: `{e}`")


# ── /adduser ───────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("adduser") & filters.private & owner_filter)
async def adduser_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /adduser [user_id]")
        return
    try:
        uid = int(parts[1].strip())
        if uid == config.OWNER_ID:
            await message.reply("Owner ko add karne ki zaroorat nahi.")
            return
        if add_allowed_user(uid, message.from_user.id):
            await message.reply(f"✅ User `{uid}` ko access de diya.\nUser ko bole ki bot ko `/login [phone]` karein taaki unke groups sync ho sakein.")
        else:
            await message.reply(f"ℹ️ User `{uid}` already allowed hai.")
    except ValueError:
        await message.reply("Numeric user ID chahiye.")

# ── /removeuser ────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("removeuser") & filters.private & owner_filter)
async def removeuser_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /removeuser [user_id]")
        return
    try:
        uid = int(parts[1].strip())
        uclient = user_clients.pop(uid, None)
        if uclient:
            try:
                await uclient.stop()
            except Exception:
                pass
        unregister_client(uid)
        clear_user_session(uid)
        if remove_allowed_user(uid):
            await message.reply(f"✅ User `{uid}` ka access hata diya.")
        else:
            await message.reply(f"❌ User `{uid}` list mein nahi tha.")
    except ValueError:
        await message.reply("Numeric user ID chahiye.")

# ── /listusers ─────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("listusers") & filters.private & owner_filter)
async def listusers_cmd(client, message: Message):
    rows = get_allowed_users()
    if not rows:
        await message.reply("Koi allowed user nahi hai. Sirf owner access kar sakta hai.")
        return
    lines = []
    for uid, added_by in rows:
        status = "🟢 Connected" if uid in user_clients else "⚪ No Session"
        lines.append(f"• `{uid}` ({status}) — added by `{added_by}`")
    await message.reply(f"✅ Allowed Users ({len(rows)}):\n\n" + "\n".join(lines))

# ── /login ─────────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("login") & filters.private & allowed_filter)
async def login_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: `/login +91XXXXXXXXXX`\nExample: `/login +919876543210`")
        return
    phone = parts[1].strip().replace(" ", "")
    uid = message.from_user.id

    curr_client = get_user_client(uid)
    if curr_client and curr_client.is_connected:
        try:
            me = await curr_client.get_me()
            await message.reply(
                f"ℹ️ Aap already logged in hain as **{me.first_name}** (`{me.id}`).\n"
                f"Naya login karne ke liye pehle `/logout` karein."
            )
            return
        except Exception:
            pass

    status_msg = await message.reply(f"🔄 `{phone}` pe OTP bheja ja raha hai...")
    try:
        if uid in login_states and login_states[uid].get("client"):
            try:
                await login_states[uid]["client"].disconnect()
            except Exception:
                pass
        temp_client = Client(
            f"user_{uid}",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            in_memory=True,
            parse_mode=ParseMode.MARKDOWN
        )
        await temp_client.connect()
        sent_code = await temp_client.send_code(phone)
        login_states[uid] = {
            "phone": phone,
            "client": temp_client,
            "phone_code_hash": sent_code.phone_code_hash,
            "step": "OTP"
        }
        await status_msg.edit_text(
            f"📩 **Telegram ne `{phone}` pe OTP bhej diya hai!**\n\n"
            f"Kripya code bhejein:\n"
            f"👉 `/otp 12345`"
        )
    except Exception as e:
        logger.error(f"Login error for {uid} ({phone}): {e}")
        await status_msg.edit_text(f"❌ OTP bhejte waqt error aaya: `{e}`")

# ── /otp ───────────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("otp") & filters.private & allowed_filter)
async def otp_cmd(client, message: Message):
    uid = message.from_user.id
    if uid not in login_states or login_states[uid].get("step") != "OTP":
        await message.reply("❌ Pehle `/login [phone]` karein.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: `/otp [code_from_telegram]`")
        return
    code = parts[1].strip().replace(" ", "").replace("-", "")
    state = login_states[uid]
    temp_client = state["client"]
    phone = state["phone"]
    phone_code_hash = state["phone_code_hash"]

    status_msg = await message.reply("🔄 Verifying OTP...")
    try:
        await temp_client.sign_in(
            phone_number=phone,
            phone_code_hash=phone_code_hash,
            phone_code=code
        )
    except SessionPasswordNeeded:
        state["step"] = "PASSWORD"
        await status_msg.edit_text(
            "🔒 **2-Step Verification Password Required!**\n\n"
            "Apna password bhejein:\n"
            "👉 `/password your_password`"
        )
        return
    except PhoneCodeInvalid:
        await status_msg.edit_text("❌ Galat OTP code hai. Dobara check karke `/otp [code]` karein.")
        return
    except PhoneCodeExpired:
        if uid in login_states:
            try:
                await login_states[uid]["client"].disconnect()
            except Exception:
                pass
            login_states.pop(uid, None)
        await status_msg.edit_text("❌ OTP expire ho gaya. Kripya `/login [phone]` dobara karein.")
        return
    except Exception as e:
        await status_msg.edit_text(f"❌ Verification failed: `{e}`")
        return

    try:
        me = await temp_client.get_me()
        session_str = await temp_client.export_session_string()
        user_clients[uid] = temp_client
        set_client(temp_client, uid)
        register_client(uid, temp_client)
        update_user_session(uid, phone=phone, session_string=session_str)
        login_states.pop(uid, None)
        await status_msg.edit_text(
            f"✅ **Account Connected Successfully!**\n\n"
            f"👤 Name: **{me.first_name}**\n"
            f"🆔 User ID: `{me.id}`\n"
            f"📞 Phone: `{phone}`\n\n"
            f"🎉 Ab aap `/syncgroups` chala ke sirf apne groups sync kar sakte hain!"
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Session save error: `{e}`")

# ── /password ──────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("password") & filters.private & allowed_filter)
async def password_cmd(client, message: Message):
    uid = message.from_user.id
    if uid not in login_states or login_states[uid].get("step") != "PASSWORD":
        await message.reply("❌ Abhi password ki zaroorat nahi hai.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: `/password [your_password]`")
        return
    pwd = parts[1].strip()
    state = login_states[uid]
    temp_client = state["client"]
    phone = state["phone"]

    status_msg = await message.reply("🔄 Verifying 2FA password...")
    try:
        await temp_client.check_password(pwd)
        me = await temp_client.get_me()
        session_str = await temp_client.export_session_string()
        user_clients[uid] = temp_client
        set_client(temp_client, uid)
        register_client(uid, temp_client)
        update_user_session(uid, phone=phone, session_string=session_str)
        login_states.pop(uid, None)
        await status_msg.edit_text(
            f"✅ **Account Connected Successfully!**\n\n"
            f"👤 Name: **{me.first_name}**\n"
            f"🆔 User ID: `{me.id}`\n\n"
            f"🎉 Ab aap `/syncgroups` chala ke sirf apne groups sync kar sakte hain!"
        )
    except PasswordHashInvalid:
        await status_msg.edit_text("❌ Galat 2FA password hai. Dobara try karein: `/password [your_password]`")
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: `{e}`")

# ── /session ───────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("session") & filters.private & allowed_filter)
async def session_cmd(client, message: Message):
    uid = message.from_user.id
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: `/session [pyrogram_session_string]`")
        return
    sstr = parts[1].strip()
    status_msg = await message.reply("🔄 Connecting session string...")
    try:
        uclient = Client(
            f"user_{uid}",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            session_string=sstr,
            parse_mode=ParseMode.MARKDOWN
        )
        await uclient.start()
        me = await uclient.get_me()
        user_clients[uid] = uclient
        set_client(uclient, uid)
        register_client(uid, uclient)
        update_user_session(uid, phone=me.phone_number or "", session_string=sstr)
        await status_msg.edit_text(
            f"✅ **Connected via String Session!**\n\n"
            f"👤 Name: **{me.first_name}**\n"
            f"🆔 Telegram ID: `{me.id}`\n\n"
            f"⏳ DB ke sabhi groups mein auto-join ho raha hai..."
        )
        # Auto-join all DB groups for this user
        await auto_join_db_groups(uclient, uid, notify_chat_id=message.chat.id)
    except Exception as e:
        logger.error(f"String session error for {uid}: {e}")
        await status_msg.edit_text(f"❌ Invalid session string: `{e}`")

# ── /myaccount ─────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("myaccount") & filters.private & allowed_filter)
async def myaccount_cmd(client, message: Message):
    uid = message.from_user.id
    uclient = get_user_client(uid)
    groups = get_all_groups(uid)
    if uclient:
        try:
            me = await uclient.get_me()
            await message.reply(
                f"👤 **Account Info**\n\n"
                f"• Name: **{me.first_name}**\n"
                f"• Telegram ID: `{me.id}`\n"
                f"• Phone: `{me.phone_number or 'N/A'}`\n"
                f"• Synced Groups: **{len(groups)}**\n"
                f"• Session Status: 🟢 Connected"
            )
            return
        except Exception:
            pass
    await message.reply(
        f"👤 **Account Info**\n\n"
        f"• Telegram ID: `{uid}`\n"
        f"• Session Status: 🔴 Not Connected\n\n"
        f"Apne groups sync karne ke liye pehle account connect karein:\n"
        f"👉 `/login [phone]`\n"
        f"ya `/session [string]`"
    )

# ── /logout ────────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("logout") & filters.private & allowed_filter)
async def logout_cmd(client, message: Message):
    uid = message.from_user.id
    if uid == config.OWNER_ID:
        await message.reply("Owner session disconnect nahi kiya ja sakta.")
        return
    uclient = user_clients.pop(uid, None)
    if uclient:
        try:
            await uclient.stop()
        except Exception:
            pass
    unregister_client(uid)
    clear_user_session(uid)
    await message.reply("✅ Aapka Telegram session disconnect ho gaya hai.")

# ── /addgroup ──────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("addgroup") & filters.private & allowed_filter)
async def addgroup_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /addgroup [link or @username or group_id]")
        return
    chat, err = await join_and_add(parts[1].strip(), message.from_user.id)
    if err:
        await message.reply(f"❌ Error: `{err}`")
    else:
        await message.reply(f"✅ Added: **{chat.title}** (`{chat.id}`)")

# ── /removegroup ───────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("removegroup") & filters.private & allowed_filter)
async def removegroup_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /removegroup [group_id]")
        return
    try:
        chat_id = int(parts[1].strip())
        if remove_group(chat_id, message.from_user.id):
            await message.reply(f"✅ Removed group `{chat_id}`")
        else:
            await message.reply("❌ Failed to remove. Group list mein nahi mila.")
    except ValueError:
        await message.reply("Numeric group ID chahiye.")

# ── /listgroups ────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("listgroups") & filters.private & allowed_filter)
async def listgroups_cmd(client, message: Message):
    rows = get_all_groups_with_info(message.from_user.id)
    if not rows:
        await message.reply("Koi group registered nahi hai. /syncgroups ya /addgroup karein.")
        return
    active = [(c, t, m, f) for c, t, m, f in rows if not f]
    failed = [(c, t, m, f) for c, t, m, f in rows if f]
    lines = []
    for chat_id, title, mult, _ in active:
        lines.append(f"🟢 **{title or 'Unknown'}** (`{chat_id}`)" + (f" ⚡x{mult}" if mult > 1.0 else ""))
    for chat_id, title, mult, _ in failed:
        lines.append(f"🔴 **{title or 'Unknown'}** (`{chat_id}`)")
    await message.reply(
        f"📋 Groups: {len(active)} active, {len(failed)} failed\n\n" + "\n".join(lines)
    )

# ── /syncgroups ────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("syncgroups") & filters.private & allowed_filter)
async def syncgroups_cmd(client, message: Message):
    uid = message.from_user.id
    uclient = get_user_client(uid)
    if not uclient:
        await message.reply(
            "⚠️ **Aapka Telegram account session connect nahi hai!**\n\n"
            "Telegram bot token kisi user ke private groups ko read nahi kar sakta.\n"
            "Apne groups sync karne ke liye pehle apna Telegram account connect karein:\n"
            "👉 `/login [phone_number]` (Example: `/login +91XXXXXXXXXX`)\n"
            "ya\n"
            "👉 `/session [pyrogram_string_session]`\n\n"
            "Login hone ke baad `/syncgroups` dobara run karein."
        )
        return

    status_msg = await message.reply("🔄 Syncing... aapke account ke saare joined groups fetch ho rahe hain.")
    added, skipped = 0, 0
    offset_date, offset_id, offset_peer = 0, 0, raw.types.InputPeerEmpty()
    while True:
        try:
            r = await uclient.invoke(
                raw.functions.messages.GetDialogs(
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_peer=offset_peer,
                    limit=100,
                    hash=0,
                    exclude_pinned=False,
                    folder_id=None,
                )
            )
        except Exception as e:
            logger.warning(f"[syncgroups] GetDialogs error: {e}")
            break
        if not hasattr(r, 'dialogs') or not r.dialogs:
            break
        chats = {c.id: c for c in r.chats}
        for d in r.dialogs:
            try:
                peer = d.peer
                if isinstance(peer, (raw.types.PeerChat, raw.types.PeerChannel)):
                    cid = getattr(peer, 'chat_id', None) or getattr(peer, 'channel_id', None)
                    chat_obj = chats.get(cid)
                    if not chat_obj:
                        continue
                    # skip channels (broadcast), only groups/supergroups
                    is_group = isinstance(chat_obj, raw.types.Chat)
                    is_supergroup = isinstance(chat_obj, raw.types.Channel) and getattr(chat_obj, 'megagroup', False)
                    if not (is_group or is_supergroup):
                        continue
                    full_id = -cid if is_group else int(f"-100{cid}")
                    title = getattr(chat_obj, 'title', '') or ''
                    slowmode = getattr(chat_obj, 'slowmode_delay', 0) or 0
                    if add_group(full_id, title, uid):
                        added += 1
                        if slowmode > 0:
                            set_slowmode(full_id, slowmode)
                    else:
                        skipped += 1
            except Exception:
                pass
        if len(r.dialogs) < 100:
            break
        last = r.dialogs[-1]
        last_peer = last.peer
        offset_id = last.top_message
        last_msg = next((m for m in r.messages if m.id == last.top_message), None)
        offset_date = getattr(last_msg, 'date', 0) or 0
        cid = getattr(last_peer, 'chat_id', None) or getattr(last_peer, 'channel_id', None) or getattr(last_peer, 'user_id', None)
        offset_peer = await uclient.resolve_peer(cid) if cid else raw.types.InputPeerEmpty()
    await status_msg.edit_text(
        f"✅ **Sync complete!**\n"
        f"• Naye groups add hue: {added}\n"
        f"• Already DB mein the: {skipped}\n"
        f"• Total active: {len(get_all_groups(uid))}"
    )

# ── /autojoin ──────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("autojoin") & filters.private & allowed_filter)
async def autojoin_cmd(client, message: Message):
    uid = message.from_user.id
    uclient = get_user_client(uid)
    if not uclient:
        await message.reply("⚠️ Pehle apna account connect karein via `/login [phone]`")
        return
    rows = get_all_groups_with_info(uid)
    if not rows:
        await message.reply("Koi group DB mein nahi hai. Pehle /syncgroups ya /addgroup karo.")
        return
    ok, fail = [], []
    for chat_id, title, _, _ in rows:
        try:
            try:
                await uclient.join_chat(chat_id)
            except UserAlreadyParticipant:
                pass
            ok.append(f"✅ {title or chat_id}")
        except Exception as e:
            fail.append(f"❌ {title or chat_id}: {e}")
    result = "\n".join(ok + fail) or "Nothing to do."
    await message.reply(f"Auto-join result:\n{result}")

# ── /scanchannel ──────────────────────────────────────────────────────────────────
_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/(?:joinchat/|\+)([a-zA-Z0-9_-]+)"   # private invite
    r"|(?:https?://)?t\.me/([a-zA-Z0-9_]{4,})"                  # t.me/username
    r"|@([a-zA-Z0-9_]{4,})"                                      # @username
)

_active_scans: dict = {}  # uid → True/False

PUBLIC_JOIN_INTERVAL  = 8    # 8s gap between public joins (prevents FloodWait buildup)
PRIVATE_JOIN_INTERVAL = 20   # 20s gap for private invite links
MAX_FLOODWAIT_SEC     = 60   # Cap: agar Telegram >60s wait bolde toh link skip karo


async def _try_join_group(
    uclient, link: str, uid: int,
    notify_chat_id: int, idx: int, total: int,
    counters: dict, chan_title: str
) -> bool:
    """
    Attempt to join one group link.
    - Public links (@username): pre-checks type BEFORE joining — channels skipped instantly
    - Private links (t.me/+HASH): must join first, then check type
    Returns True to continue scanning, False to stop.
    """
    if not _active_scans.get(uid):
        return False  # Scan cancelled

    is_private = link.startswith("https://t.me/+")

    # ── Pre-check for public links: no need to join a channel ──
    if not is_private:
        try:
            pre = await uclient.get_chat(link)
            pre_type = getattr(pre, "type", None)
            if pre_type not in (ChatType.GROUP, ChatType.SUPERGROUP):
                counters["skipped_channel"] += 1
                logger.info(f"[scanchannel] Pre-check: skipped channel {getattr(pre,'title',link)}")
                return True
        except Exception:
            pass  # Couldn't pre-check — try joining anyway

    try:
        chat = await uclient.join_chat(link)
        ctype = getattr(chat, "type", None)

        # ── Not a group — leave immediately (mainly for private links) ──
        if ctype not in (ChatType.GROUP, ChatType.SUPERGROUP):
            try:
                await uclient.leave_chat(chat.id)
            except Exception:
                pass
            counters["skipped_channel"] += 1
            return True

        # ── Check messaging permission ──
        perms = getattr(chat, "permissions", None)
        can_msg = perms is None or getattr(perms, "can_send_messages", True)
        if not can_msg:
            try:
                await uclient.leave_chat(chat.id)
            except Exception:
                pass
            counters["skipped_no_msg"] += 1
            logger.info(f"[scanchannel] Left (no msg perm): {chat.title}")
            return True

        # ── Valid group — add to DB + notify ──
        add_group(chat.id, chat.title or "", uid)
        counters["joined"] += 1
        logger.info(f"[scanchannel] ✅ Joined: {chat.title} ({chat.id})")
        try:
            await bot_app.send_message(
                notify_chat_id,
                f"✅ **Joined:** **{chat.title}**\n"
                f"🆔 `{chat.id}` — `{idx}/{total}`"
            )
        except Exception:
            pass
        return True

    except InviteRequestSent:
        # Group requires admin approval — caller collects this link for Pass 3
        counters["skipped_approval"] += 1
        logger.info(f"[scanchannel] Request sent (approval needed): {link}")
        return "REQUEST"  # Signal to caller: move to pass 3

    except UserAlreadyParticipant:
        try:
            chat = await uclient.get_chat(link)
            ctype = getattr(chat, "type", None)
            if ctype in (ChatType.GROUP, ChatType.SUPERGROUP):
                perms = getattr(chat, "permissions", None)
                can_msg = perms is None or getattr(perms, "can_send_messages", True)
                if can_msg:
                    add_group(chat.id, chat.title or "", uid)
                    counters["already_in"] += 1
                else:
                    counters["skipped_no_msg"] += 1
            else:
                counters["skipped_channel"] += 1
        except Exception:
            counters["already_in"] += 1
        return True

    except FloodWait as e:
        wait_sec = e.value
        logger.warning(f"[scanchannel] FloodWait {wait_sec}s for {link}")

        if wait_sec > MAX_FLOODWAIT_SEC:
            # Too long to wait — skip this link, don't block the whole scan
            counters["skipped_invalid"] += 1
            logger.warning(f"[scanchannel] FloodWait {wait_sec}s > cap {MAX_FLOODWAIT_SEC}s, skipping")
            try:
                await bot_app.send_message(
                    notify_chat_id,
                    f"⚠️ **FloodWait {wait_sec}s** — cap exceeded, link skip kiya\n"
                    f"Progress: `{idx}/{total}` | Joined: `{counters['joined']}`"
                )
            except Exception:
                pass
            # Still wait the cap amount before continuing
            await asyncio.sleep(MAX_FLOODWAIT_SEC)
            return True

        # Acceptable wait — wait + retry once
        try:
            await bot_app.send_message(
                notify_chat_id,
                f"⚠️ **FloodWait `{wait_sec}s`** — ruk raha hoon...\n"
                f"Progress: `{idx}/{total}` | Joined: `{counters['joined']}`"
            )
        except Exception:
            pass
        await asyncio.sleep(wait_sec + 5)
        # Retry once after wait
        try:
            chat = await uclient.join_chat(link)
            ctype = getattr(chat, "type", None)
            if ctype in (ChatType.GROUP, ChatType.SUPERGROUP):
                perms = getattr(chat, "permissions", None)
                can_msg = perms is None or getattr(perms, "can_send_messages", True)
                if can_msg:
                    add_group(chat.id, chat.title or "", uid)
                    counters["joined"] += 1
                    try:
                        await bot_app.send_message(
                            notify_chat_id,
                            f"✅ **Joined (after wait):** **{chat.title}**\n"
                            f"🆔 `{chat.id}`"
                        )
                    except Exception:
                        pass
                else:
                    try:
                        await uclient.leave_chat(chat.id)
                    except Exception:
                        pass
                    counters["skipped_no_msg"] += 1
            else:
                try:
                    await uclient.leave_chat(chat.id)
                except Exception:
                    pass
                counters["skipped_channel"] += 1
        except (InviteRequestSent, UserAlreadyParticipant):
            counters["already_in"] += 1
        except Exception:
            counters["skipped_invalid"] += 1
        return True

    except (InviteHashExpired, InviteHashInvalid, ChannelPrivate, PeerIdInvalid):
        counters["skipped_invalid"] += 1
        return True

    except Exception as e:
        counters["skipped_invalid"] += 1
        logger.warning(f"[scanchannel] Error {link}: {e}")
        return True


async def _do_scan(uclient, uid: int, channel_target: str, notify_chat_id: int):
    """
    3-pass scan:
    Pass 1 — Public links       (newest first, no gap, FloodWait handles throttle)
    Pass 2 — Private direct     (newest first, 20s gap, direct join only)
    Pass 3 — Private request    (links that need approval, 30s gap, attempt at end)
    """
    counters = {
        "joined": 0, "already_in": 0,
        "skipped_channel": 0, "skipped_no_msg": 0,
        "skipped_approval": 0, "skipped_invalid": 0
    }
    public_links   = []   # @username / t.me/username
    private_links  = []   # t.me/+HASH (direct join)
    request_links  = []   # collected during pass 2 (InviteRequestSent)
    processed      = set()

    try:
        chan = await uclient.get_chat(channel_target)
        chan_title = getattr(chan, "title", channel_target)

        status_msg = await bot_app.send_message(
            notify_chat_id,
            f"🔍 **Scanning channel:** `{chan_title}`\n"
            f"⏳ Messages fetch ho rahi hain..."
        )

        # ── Collect & categorize links (newest messages FIRST via get_chat_history) ──
        msg_count = 0
        async for msg in uclient.get_chat_history(chan.id):
            msg_count += 1
            text = (msg.text or msg.caption or "")
            for m in _LINK_RE.finditer(text):
                inv_hash = m.group(1)
                tme_user = m.group(2)
                at_user  = m.group(3)

                if inv_hash:
                    lnk = f"https://t.me/+{inv_hash}"
                    if lnk not in processed:
                        processed.add(lnk)
                        private_links.append(lnk)
                elif tme_user:
                    lnk = f"@{tme_user}"
                    if lnk not in processed:
                        processed.add(lnk)
                        public_links.append(lnk)
                elif at_user:
                    lnk = f"@{at_user}"
                    if lnk not in processed:
                        processed.add(lnk)
                        public_links.append(lnk)

        total = len(public_links) + len(private_links)

        await status_msg.edit_text(
            f"🔍 **Channel:** `{chan_title}`\n"
            f"📨 Messages: `{msg_count}` | Links: `{total}`\n"
            f"🔓 Public: `{len(public_links)}` │ 🔒 Private: `{len(private_links)}`\n"
            f"📅 Order: Latest links pehle\n\n"
            f"🚀 **Pass 1:** Public groups join ho rahe hain..."
        )

        # ═══════════════════════════════════════════════════════════════════
        # PASS 1: Public links — 8s gap + FloodWait auto-cap
        # ═══════════════════════════════════════════════════════════════════
        for i, link in enumerate(public_links, 1):
            if not _active_scans.get(uid):
                break
            await _try_join_group(
                uclient, link, uid, notify_chat_id, i, total, counters, chan_title
            )
            # 8s gap to prevent FloodWait buildup
            if i < len(public_links) and _active_scans.get(uid):
                await asyncio.sleep(PUBLIC_JOIN_INTERVAL)
            if i % 10 == 0:
                try:
                    await status_msg.edit_text(
                        f"🚀 **Pass 1 — Public:** `{i}/{len(public_links)}`\n"
                        f"✅ Joined: `{counters['joined']}` │ 🔄 Already: `{counters['already_in']}`\n"
                        f"📺 Channels: `{counters['skipped_channel']}` │ 📵 No msg: `{counters['skipped_no_msg']}`\n"
                        f"❌ Invalid: `{counters['skipped_invalid']}`"
                    )
                except Exception:
                    pass

        # ═══════════════════════════════════════════════════════════════════
        # PASS 2: Private direct links — 20s gap, collect request links for pass 3
        # ═══════════════════════════════════════════════════════════════════
        if private_links and _active_scans.get(uid):
            try:
                await status_msg.edit_text(
                    f"✅ **Pass 1 done** — `{counters['joined']}` joined\n"
                    f"🚀 **Pass 2:** Private direct links `{len(private_links)}`\n"
                    f"⏳ `{PRIVATE_JOIN_INTERVAL}s` gap | Request links aakhir mein"
                )
            except Exception:
                pass

            for i, link in enumerate(private_links, 1):
                if not _active_scans.get(uid):
                    break
                global_idx = len(public_links) + i
                result = await _try_join_group(
                    uclient, link, uid, notify_chat_id, global_idx, total, counters, chan_title
                )
                # If returned "REQUEST", collect for pass 3
                if result == "REQUEST":
                    request_links.append(link)

                if i < len(private_links) and _active_scans.get(uid):
                    await asyncio.sleep(PRIVATE_JOIN_INTERVAL)

                if i % 5 == 0:
                    try:
                        await status_msg.edit_text(
                            f"🚀 **Pass 2 — Private:** `{i}/{len(private_links)}`\n"
                            f"✅ Joined: `{counters['joined']}` │ 🔄 Already: `{counters['already_in']}`\n"
                            f"🔐 Request links (pass 3): `{len(request_links)}`\n"
                            f"📵 No msg: `{counters['skipped_no_msg']}` │ ❌ Invalid: `{counters['skipped_invalid']}`"
                        )
                    except Exception:
                        pass

        # ═══════════════════════════════════════════════════════════════════
        # PASS 3: Private request links — 30s gap, attempt again at end
        # ═══════════════════════════════════════════════════════════════════
        if request_links and _active_scans.get(uid):
            try:
                await status_msg.edit_text(
                    f"✅ **Pass 2 done**\n"
                    f"🚀 **Pass 3:** Request-only links `{len(request_links)}`\n"
                    f"⏳ `30s` gap | Request bheja tha — retry kar raha hoon"
                )
            except Exception:
                pass

            for i, link in enumerate(request_links, 1):
                if not _active_scans.get(uid):
                    break
                global_idx = len(public_links) + len(private_links) + i
                # Try again — some groups auto-approve or admin may have approved by now
                await _try_join_group(
                    uclient, link, uid, notify_chat_id, global_idx, total + len(request_links), counters, chan_title
                )
                if i < len(request_links) and _active_scans.get(uid):
                    await asyncio.sleep(30)

        # ── Final report ──
        total_db = len(get_all_groups(uid))
        await status_msg.edit_text(
            f"✅ **Scan Complete!** — `{chan_title}`\n\n"
            f"🔓 Public: `{len(public_links)}` │ 🔒 Private direct: `{len(private_links)}` │ 🔐 Request: `{len(request_links)}`\n\n"
            f"✅ Joined: `{counters['joined']}`\n"
            f"🔄 Already member: `{counters['already_in']}`\n"
            f"📺 Channels skip: `{counters['skipped_channel']}`\n"
            f"📵 No msg perm: `{counters['skipped_no_msg']}`\n"
            f"🔐 Approval needed: `{counters['skipped_approval']}`\n"
            f"❌ Invalid/expired: `{counters['skipped_invalid']}`\n\n"
            f"📦 DB mein total groups: `{total_db}`\n\n"
            f"Ab `/interval` ya `/spamgroup` se spam shuru karo!"
        )

    except Exception as e:
        logger.error(f"[scanchannel] Fatal error for {uid}: {e}")
        try:
            await bot_app.send_message(notify_chat_id, f"❌ Scan error: `{e}`")
        except Exception:
            pass
    finally:
        _active_scans[uid] = False


@bot_app.on_message(filters.command("scanchannel") & filters.private & allowed_filter)
async def scanchannel_cmd(client, message: Message):
    uid = message.from_user.id
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply(
            "**Usage:** `/scanchannel [@username ya channel_id]`\n\n"
            "**Bot kya karega:**\n"
            "• Channel ke saare msgs se links extract karega\n"
            "• 🔓 Public groups pehle join karega (`15s` gap)\n"
            "• 🔒 Private invite links baad mein (`25s` gap)\n"
            "• Channels → skip\n"
            "• Approval-required groups → skip\n"
            "• Messaging not allowed → join karke turant leave\n"
            "• Har successful join pe real-time notification\n"
            "• Sab DB mein save hote rahenge\n\n"
            "🛑 Scan rokne ke liye: `/stopscan`"
        )
        return

    if _active_scans.get(uid):
        await message.reply("⚠️ Ek scan already chal raha hai. Pehle `/stopscan` karo.")
        return

    uclient = get_user_client(uid)
    if not uclient:
        await message.reply("⚠️ Pehle apna account connect karein via `/session`")
        return

    channel_target = parts[1].strip()
    _active_scans[uid] = True

    await message.reply(
        f"🚀 **Scan shuru!**\n"
        f"Channel: `{channel_target}`\n\n"
        f"🔓 Public links → `{PUBLIC_JOIN_INTERVAL}s` gap\n"
        f"🔒 Private links → `{PRIVATE_JOIN_INTERVAL}s` gap\n\n"
        f"Har join pe notification aayegi. 🛑 `/stopscan` se rokein."
    )
    asyncio.create_task(_do_scan(uclient, uid, channel_target, message.chat.id))


@bot_app.on_message(filters.command("stopscan") & filters.private & allowed_filter)
async def stopscan_cmd(client, message: Message):
    uid = message.from_user.id
    if _active_scans.get(uid):
        _active_scans[uid] = False
        await message.reply("🛑 Scan stop signal bheja gaya. Thodi der mein band ho jayega.")
    else:
        await message.reply("ℹ️ Koi active scan nahi chal raha.")



# ── /schedule ──────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("schedule") & filters.private & allowed_filter)
async def schedule_cmd(client, message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("Usage: /schedule HH:MM Your message")
        return
    cron = parse_cron(parts[1])
    if not cron:
        await message.reply("Invalid time. Use HH:MM (UTC)\nExample: /schedule 14:30 Hello!")
        return
    groups = get_all_groups(message.from_user.id)
    if not groups:
        await message.reply("Pehle /addgroup ya /syncgroups karo.")
        return
    job_id = schedule_message_job(groups, parts[2], cron, user_id=message.from_user.id)
    if job_id:
        await message.reply(
            f"✅ Scheduled at `{parts[1]}` UTC daily\n"
            f"Groups: {len(groups)}\n"
            f"Job ID: `{job_id}`"
        )
    else:
        await message.reply("❌ Schedule failed.")

# ── /interval ──────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("interval") & filters.private & allowed_filter)
async def interval_cmd(client, message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("Usage: /interval [seconds] Your message\nMinimum: 60 seconds")
        return
    try:
        seconds = int(parts[1])
        if seconds < 60:
            await message.reply("⚠️ Minimum 60 seconds — spam ban se bachne ke liye.")
            return
        groups = get_all_groups(message.from_user.id)
        if not groups:
            await message.reply("Pehle /addgroup ya /syncgroups karo.")
            return
        job_id = send_interval_message(groups, parts[2], seconds, user_id=message.from_user.id)
        if job_id:
            await message.reply(
                f"✅ Interval every {seconds}s\n"
                f"Groups: {len(groups)}\n"
                f"Job ID: `{job_id}`"
            )
        else:
            await message.reply("❌ Interval schedule failed.")
    except ValueError:
        await message.reply("Seconds numeric hona chahiye.")

# ── /spamgroup ─────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("spamgroup") & filters.private & allowed_filter)
async def spamgroup_cmd(client, message: Message):
    uid = message.from_user.id
    parts = message.text.split(maxsplit=3)
    if len(parts) < 4:
        await message.reply(
            "**Usage:** `/spamgroup [group_id/@username] [seconds] [message]`\n"
            "**Example:** `/spamgroup -1001234567890 10 Hello!`"
        )
        return
    group_target = parts[1].strip()
    try:
        seconds = int(parts[2])
    except ValueError:
        await message.reply("❌ Seconds numeric hona chahiye.")
        return
    if seconds < 5:
        await message.reply("⚠️ Minimum 5 seconds.")
        return
    msg_text = parts[3]
    uclient = get_user_client(uid)
    if not uclient:
        await message.reply("⚠️ Pehle `/session` se account connect karo.")
        return
    status_msg = await message.reply("🔄 Group verify ho raha hai...")
    try:
        if group_target.lstrip("-").isdigit():
            chat_id = int(group_target)
            chat = await uclient.get_chat(chat_id)
        else:
            chat = await uclient.get_chat(group_target)
            chat_id = chat.id
        title = getattr(chat, "title", str(chat_id))
    except Exception as e:
        await status_msg.edit_text(f"❌ Group resolve nahi hua: `{e}`")
        return
    existing = [jid for jid in get_spread_jobs(uid) if str(chat_id) in jid]
    for jid in existing:
        stop_spread_spam(jid)
    job_id = start_spread_spam([chat_id], msg_text, seconds, uid, pinned=True)
    if job_id:
        await status_msg.edit_text(
            f"✅ **Group Spam shuru!**\n"
            f"📢 **{title}** (`{chat_id}`)\n"
            f"⏱ Interval: `{seconds}s`\n"
            f"📝 `{msg_text[:80]}`\n\n"
            f"🛑 Band karne ke liye: `/stopspam`"
        )
    else:
        await status_msg.edit_text("❌ Client not found. Bot restart karo.")

# ── /spam ──────────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("spam") & filters.private & allowed_filter)
async def spam_cmd(client, message: Message):
    uid = message.from_user.id
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: `/spam [message]`\nExample: `/spam Aaj sale hai!`")
        return
    uclient = get_user_client(uid)
    if not uclient:
        await message.reply("⚠️ Pehle `/session` se account connect karo.")
        return
    groups = get_all_groups(uid)
    if not groups:
        await message.reply("⚠️ Koi group nahi. Pehle `/syncgroups` karo.")
        return
    # Pinned groups skip karo — unka apna spamgroup chal raha hai
    pinned = get_pinned_groups(uid)
    groups = [g for g in groups if g not in pinned]
    if not groups:
        await message.reply("⚠️ Saare groups already `/spamgroup` se chal rahe hain.")
        return
    stop_all_spread_spam(uid)
    job_id = start_spread_spam(groups, parts[1], 0, uid)
    if not job_id:
        await message.reply("❌ Client not found. Bot restart karo aur dobara `/session` lagao.")
        return
    await message.reply(
        f"✅ **Spam shuru!**\n"
        f"📊 Groups: `{len(groups)}`\n"
        f"📝 Message: `{parts[1][:80]}`\n"
        f"⏱ Har group apne slowmode ke hisab se automatically spam hoga\n\n"
        f"🛑 Band karne ke liye: `/stopspam`"
    )


@bot_app.on_message(filters.command("stopspam") & filters.private & allowed_filter)
async def stopspam_cmd(client, message: Message):
    uid = message.from_user.id
    count = stop_all_spread_spam(uid)
    if count:
        await message.reply(f"🛑 Spam band — `{count}` job(s) stop ho gayi.")
    else:
        await message.reply("ℹ️ Koi active spam nahi chal raha.")

# ── /stop ──────────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("stop") & filters.private & allowed_filter)
async def stop_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /stop [job_id]")
        return
    jid = parts[1].strip()
    uid = message.from_user.id
    if uid != config.OWNER_ID and f"_{uid}_" not in jid:
        await message.reply("❌ Yeh job aapki nahi hai.")
        return
    if stop_job(jid):
        await message.reply(f"✅ Stopped job `{jid}`")
    else:
        await message.reply("❌ Job not found. /listjobs se dekho.")

# ── /listjobs ──────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("listjobs") & filters.private & allowed_filter)
async def listjobs_cmd(client, message: Message):
    jobs = scheduler.get_jobs()
    uid = message.from_user.id
    is_owner = (uid == config.OWNER_ID)
    user_jobs = []
    for job in jobs:
        if is_owner or f"_{uid}_" in job.id:
            user_jobs.append(job)
    if not user_jobs:
        await message.reply("Koi active job nahi hai.")
        return
    lines = []
    for job in user_jobs:
        lines.append(f"• `{job.id}`\n  Next: {job.next_run_time}")
    await message.reply(f"Active Jobs ({len(user_jobs)}):\n\n" + "\n".join(lines))

# ── /faillist ──────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("faillist") & filters.private & allowed_filter)
async def faillist_cmd(client, message: Message):
    failed = get_failed_groups(message.from_user.id)
    if not failed:
        await message.reply("✅ Koi failed group nahi hai.")
        return
    lines = [f"🔴 **{title or 'Unknown'}** (`{chat_id}`)" for chat_id, title in failed]
    await message.reply(f"Failed Groups ({len(failed)}):\n\n" + "\n".join(lines))

# ── /clearfail ─────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("clearfail") & filters.private & allowed_filter)
async def clearfail_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /clearfail [group_id]  OR  /clearfail all")
        return
    arg = parts[1].strip()
    uid = message.from_user.id
    if arg == "all":
        failed = get_failed_groups(uid)
        for chat_id, _ in failed:
            clear_group_failed(chat_id, uid)
        await message.reply(f"✅ {len(failed)} group(s) fail list se hata diye.")
    else:
        try:
            chat_id = int(arg)
            clear_group_failed(chat_id, uid)
            await message.reply(f"✅ Group `{chat_id}` fail list se hata diya.")
        except ValueError:
            await message.reply("Numeric group ID ya 'all' do.")

# ── Keywords ───────────────────────────────────────────────────────────────────
@bot_app.on_message(filters.command("addkeyword") & filters.private & allowed_filter)
async def addkeyword_cmd(client, message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("Usage: /addkeyword <keyword> <reply>\nExample: /addkeyword hello Hi there!")
        return
    if add_keyword(parts[1], parts[2]):
        await message.reply(f"✅ Keyword `{parts[1]}` add ho gaya.")
    else:
        await message.reply("❌ Failed.")


@bot_app.on_message(filters.command("removekeyword") & filters.private & allowed_filter)
async def removekeyword_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /removekeyword <keyword>")
        return
    if remove_keyword(parts[1]):
        await message.reply(f"✅ Keyword `{parts[1]}` remove ho gaya.")
    else:
        await message.reply(f"❌ Keyword `{parts[1]}` nahi mila.")


@bot_app.on_message(filters.command("listkeywords") & filters.private & allowed_filter)
async def listkeywords_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    page = int(parts[1]) - 1 if len(parts) > 1 and parts[1].isdigit() else 0
    total = get_keyword_count()
    rows = get_all_keywords(page=page, per_page=20)
    if not rows:
        await message.reply("Koi keyword nahi hai.")
        return
    lines = [f"`{kw}` → {rp[:30]}..." if len(rp) > 30 else f"`{kw}` → {rp}" for kw, rp, _ in rows]
    await message.reply(f"Keywords (Page {page+1}) — Total: {total}\n\n" + "\n".join(lines) + f"\n\nNext: /listkeywords {page+2}")


@bot_app.on_message(filters.private & ~allowed_filter)
async def unauthorized(client, message: Message):
    pass  # Silently ignore

# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    start_scheduler()

    # Start Owner user client
    await user_app.start()
    user_clients[config.OWNER_ID] = user_app
    set_client(user_app, config.OWNER_ID)
    register_client(config.OWNER_ID, user_app)
    # Save owner session string to DB for Railway restarts
    try:
        if not _owner_sstr:
            sstr = await user_app.export_session_string()
            update_user_session(config.OWNER_ID, phone=config.PHONE_NUMBER, session_string=sstr)
            logger.info("Owner session string saved to DB.")
    except Exception:
        pass
    logger.info("Owner user client started.")

    # Load saved user sessions
    for uid, phone, sname, sstr in get_all_user_sessions():
        try:
            if sstr:
                uclient = Client(
                    f"user_{uid}",
                    api_id=config.API_ID,
                    api_hash=config.API_HASH,
                    session_string=sstr,
                    parse_mode=ParseMode.MARKDOWN
                )
                await uclient.start()
                user_clients[uid] = uclient
                set_client(uclient, uid)
                register_client(uid, uclient)
                logger.info(f"Loaded session string for user {uid}")
                # Auto-join all DB groups silently on startup (in background)
                import asyncio
                asyncio.create_task(auto_join_db_groups(uclient, uid))
            elif sname:
                uclient = Client(
                    sname,
                    api_id=config.API_ID,
                    api_hash=config.API_HASH,
                    parse_mode=ParseMode.MARKDOWN
                )
                await uclient.start()
                user_clients[uid] = uclient
                set_client(uclient, uid)
                register_client(uid, uclient)
                logger.info(f"Loaded session file for user {uid}")
                # Auto-join all DB groups silently on startup (in background)
                import asyncio
                asyncio.create_task(auto_join_db_groups(uclient, uid))
        except Exception as e:
            logger.error(f"Failed to start session for user {uid}: {e}")

    await bot_app.start()
    logger.info("Bot client started.")
    logger.info("Bot running. Ctrl+C to stop.")

    # Attach warn handler to all user clients
    for _uid, _uc in list(user_clients.items()):
        try:
            _uc.add_handler(
                pyrogram.handlers.MessageHandler(
                    _handle_warn_for_client,
                    filters.group & filters.incoming
                )
            )
        except Exception:
            pass

    try:
        await asyncio.Future()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stop_scheduler()
        for uc in list(user_clients.values()):
            try:
                await uc.stop()
            except Exception:
                pass
        await bot_app.stop()
        logger.info("Shutdown complete.")


# ── Protectron warn auto-backoff ─────────────────────────────────────────────
WARN_KEYWORDS = ["warn", "warned", "warning", "⚠️", "🚨"]
_me_cache: dict = {}

def _is_warn_message(text: str, username: str, user_id: int) -> bool:
    if not text:
        return False
    tl = text.lower()
    has_warn = any(w in tl for w in WARN_KEYWORDS)
    has_mention = (f"@{username.lower()}" in tl if username else False) or (str(user_id) in text)
    return has_warn and has_mention

async def _handle_warn_for_client(uclient, message):
    try:
        uid = uclient.me.id if uclient.me else None
        if uid is None:
            if id(uclient) in _me_cache:
                me = _me_cache[id(uclient)]
            else:
                me = await uclient.get_me()
                _me_cache[id(uclient)] = me
        else:
            me = uclient.me
        text = message.text or message.caption or ""
        if not _is_warn_message(text, me.username or "", me.id):
            return
        chat_id = message.chat.id
        old = get_group_multiplier(chat_id)
        new_mult = round(old * 2, 2)
        set_group_multiplier(chat_id, new_mult)
        logger.info(f"[warn-detect] {chat_id} multiplier {old} → {new_mult} (Protectron warn)")
    except Exception as e:
        logger.warning(f"[warn-detect] error: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
