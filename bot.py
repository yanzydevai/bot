#!/usr/bin/env python3
import os
import sqlite3
import logging
import random
import string
import time
import threading
import asyncio
import shutil
import glob
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple, List
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.error import TimedOut, NetworkError
from telegram.request import HTTPXRequest

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8715900599:AAHq33TaNL1zZ8ODdV5CCO9nT8KyaqqWClI")
ADMIN_ID = 7293981502
ADMIN_URL = "t.me/yanzysaja"
GRUP_LINK = "https://t.me/+XXXXXXXXXX"
FALLBACK_PHOTO = "yanzy.jpg"
USER_PHOTO = "yanzy1.jpg"

PAKET = {
    "member":   {"nama": "MEMBER",   "harga": 50000},
    "reseller": {"nama": "RESELLER", "harga": 70000},
    "pt":       {"nama": "PT",       "harga": 90000},
    "tk":       {"nama": "TK",       "harga": 140000},
    "owner":    {"nama": "OWNER",    "harga": 190000}
}

PAKET_ORDER = ["member", "reseller", "pt", "tk", "owner"]
USERS_PER_PAGE = 6

bot_start_time = time.time()

admin_live_task: asyncio.Task = None
admin_live_msg = {"chat_id": None, "message_id": None, "has_photo": False}
admin_panel_msg_ids: list = []

active_conversations: set = set()
review_requested: set = set()
awaiting_review_message: set = set()
group_promo_pending: dict = {}
group_promo_cooldown: dict = {}
awaiting_join_link: set = set()

PROMO_COOLDOWN_SECONDS = 86400

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def init_db():
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        status TEXT DEFAULT 'pending',
        paket TEXT,
        password TEXT,
        tanggal_daftar TEXT,
        banned INTEGER DEFAULT 0
    )''')

    try:
        c.execute('ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0')
    except Exception:
        pass

    c.execute('''CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        paket TEXT,
        harga INTEGER,
        bukti_path TEXT,
        status TEXT DEFAULT 'pending',
        tanggal TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS temp_paket (
        user_id INTEGER PRIMARY KEY,
        paket TEXT,
        harga INTEGER,
        is_upgrade INTEGER DEFAULT 0,
        paket_lama TEXT,
        timestamp REAL
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS groups (
        chat_id INTEGER PRIMARY KEY,
        chat_title TEXT,
        tanggal_join TEXT
    )''')

    try:
        c.execute('ALTER TABLE temp_paket ADD COLUMN is_upgrade INTEGER DEFAULT 0')
    except Exception:
        pass
    try:
        c.execute('ALTER TABLE temp_paket ADD COLUMN paket_lama TEXT')
    except Exception:
        pass

    conn.commit()
    conn.close()
    print("✅ Database terhubung")


def generate_password(length: int = 8) -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))


def save_user(user_id: int, username: str, full_name: str):
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
    exists = c.fetchone()
    if not exists:
        c.execute(
            'INSERT INTO users (user_id, username, full_name, tanggal_daftar) VALUES (?, ?, ?, ?)',
            (user_id, username, full_name, datetime.now().isoformat())
        )
    else:
        c.execute(
            'UPDATE users SET username = ?, full_name = ? WHERE user_id = ?',
            (username, full_name, user_id)
        )
    conn.commit()
    conn.close()


def save_group(chat_id: int, chat_title: str):
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute('SELECT chat_id FROM groups WHERE chat_id = ?', (chat_id,))
    if not c.fetchone():
        c.execute(
            'INSERT INTO groups (chat_id, chat_title, tanggal_join) VALUES (?, ?, ?)',
            (chat_id, chat_title, datetime.now().isoformat())
        )
    else:
        c.execute('UPDATE groups SET chat_title = ? WHERE chat_id = ?', (chat_title, chat_id))
    conn.commit()
    conn.close()


def get_all_group_ids() -> List[int]:
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute('SELECT chat_id FROM groups')
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_all_groups_paged(page: int = 0, per_page: int = 5) -> Tuple[List[Dict], int]:
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM groups')
    total = c.fetchone()[0]
    offset = page * per_page
    c.execute('SELECT chat_id, chat_title FROM groups ORDER BY rowid DESC LIMIT ? OFFSET ?', (per_page, offset))
    rows = c.fetchall()
    conn.close()
    return [{"chat_id": r[0], "chat_title": r[1] or "Grup"} for r in rows], total


def remove_group(chat_id: int):
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute('DELETE FROM groups WHERE chat_id = ?', (chat_id,))
    conn.commit()
    conn.close()


def get_group_name(chat_id: int) -> str:
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute('SELECT chat_title FROM groups WHERE chat_id = ?', (chat_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else str(chat_id)


def get_user_status(user_id: int) -> Dict:
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute('SELECT status, paket, password, banned FROM users WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"status": row[0], "paket": row[1], "password": row[2], "banned": row[3] or 0}
    return {"status": None, "paket": None, "password": None, "banned": 0}


def update_user_status(user_id: int, status: str, paket: str = None, password: str = None):
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    if paket and password:
        c.execute('UPDATE users SET status = ?, paket = ?, password = ? WHERE user_id = ?',
                  (status, paket, password, user_id))
    elif paket:
        c.execute('UPDATE users SET status = ?, paket = ? WHERE user_id = ?',
                  (status, paket, user_id))
    else:
        c.execute('UPDATE users SET status = ? WHERE user_id = ?', (status, user_id))
    conn.commit()
    conn.close()


def ban_user(user_id: int):
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute('UPDATE users SET banned = 1 WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()


def unban_user(user_id: int):
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute('UPDATE users SET banned = 0 WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()


def get_user_by_identifier(identifier: str) -> Optional[Dict]:
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    identifier = identifier.strip()
    if identifier.startswith('@'):
        username = identifier[1:]
        c.execute(
            'SELECT user_id, username, full_name, status, paket, tanggal_daftar, banned FROM users WHERE username = ?',
            (username,)
        )
    elif identifier.isdigit():
        c.execute(
            'SELECT user_id, username, full_name, status, paket, tanggal_daftar, banned FROM users WHERE user_id = ?',
            (int(identifier),)
        )
    else:
        c.execute(
            'SELECT user_id, username, full_name, status, paket, tanggal_daftar, banned FROM users WHERE username = ?',
            (identifier,)
        )
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "user_id": row[0], "username": row[1] or "-", "full_name": row[2] or "-",
            "status": row[3], "paket": row[4], "tanggal_daftar": row[5], "banned": row[6] or 0
        }
    return None


def get_all_users(page: int = 0, per_page: int = USERS_PER_PAGE) -> Tuple[List[Dict], int]:
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM users')
    total = c.fetchone()[0]
    offset = page * per_page
    c.execute(
        'SELECT user_id, username, full_name, status, paket FROM users ORDER BY rowid DESC LIMIT ? OFFSET ?',
        (per_page, offset)
    )
    rows = c.fetchall()
    conn.close()
    users = []
    for row in rows:
        users.append({
            "user_id": row[0], "username": row[1] or "-",
            "full_name": row[2] or "-", "status": row[3], "paket": row[4]
        })
    return users, total


def save_temp_paket(user_id: int, paket_nama: str, paket_harga: int,
                    is_upgrade: bool = False, paket_lama: str = None):
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute(
        'INSERT OR REPLACE INTO temp_paket (user_id, paket, harga, is_upgrade, paket_lama, timestamp) VALUES (?, ?, ?, ?, ?, ?)',
        (user_id, paket_nama, paket_harga, 1 if is_upgrade else 0, paket_lama, time.time())
    )
    conn.commit()
    conn.close()


def get_temp_paket(user_id: int) -> Optional[Dict]:
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute('SELECT paket, harga, is_upgrade, paket_lama FROM temp_paket WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"paket": row[0], "harga": row[1], "is_upgrade": row[2], "paket_lama": row[3]}
    return None


def clear_temp_paket(user_id: int):
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute('DELETE FROM temp_paket WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()


def save_transaction(user_id: int, paket: str, harga: int, bukti_path: str) -> int:
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute(
        'INSERT INTO transactions (user_id, paket, harga, bukti_path, tanggal) VALUES (?, ?, ?, ?, ?)',
        (user_id, paket, harga, bukti_path, datetime.now().isoformat())
    )
    trans_id = c.lastrowid
    conn.commit()
    conn.close()
    return trans_id


def get_transaction_by_id(trans_id: int) -> Optional[Dict]:
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute(
        'SELECT id, user_id, paket, harga, bukti_path, status, tanggal FROM transactions WHERE id = ?',
        (trans_id,)
    )
    row = c.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "user_id": row[1], "paket": row[2], "harga": row[3],
                "bukti_path": row[4], "status": row[5], "tanggal": row[6]}
    return None


def update_transaction_status(trans_id: int, status: str):
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute('UPDATE transactions SET status = ? WHERE id = ?', (status, trans_id))
    conn.commit()
    conn.close()


def get_all_user_ids() -> list:
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute('SELECT user_id FROM users')
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]


def get_stats() -> Dict:
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM users')
    total_users = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM users WHERE status = "active" AND banned = 0')
    active_users = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM users WHERE banned = 1')
    banned_users = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM transactions WHERE status = "pending"')
    pending_transactions = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM transactions WHERE status = "approved"')
    total_transactions = c.fetchone()[0]
    c.execute('SELECT SUM(harga) FROM transactions WHERE status = "approved"')
    total_revenue = c.fetchone()[0] or 0
    paket_counts = {}
    for kode in PAKET_ORDER:
        nama = PAKET[kode]['nama']
        c.execute('SELECT COUNT(*) FROM users WHERE LOWER(paket) = ? AND status = "active"', (kode,))
        paket_counts[nama] = c.fetchone()[0]
    conn.close()
    return {
        "total_users": total_users,
        "active_users": active_users,
        "banned_users": banned_users,
        "pending_transactions": pending_transactions,
        "total_transactions": total_transactions,
        "total_revenue": total_revenue,
        "paket_counts": paket_counts
    }


def file_exists(filepath: str) -> bool:
    return os.path.exists(filepath)


def get_owner_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("UPDATE INFO", callback_data="broadcast_mode"),
         InlineKeyboardButton("LIST USER", callback_data="list_user_0")],
        [InlineKeyboardButton("REFRESH", callback_data="owner_stats")]
    ])


def build_admin_msg() -> str:
    stats = get_stats()
    utc_now = datetime.now(timezone.utc)
    wib  = (utc_now + timedelta(hours=7)).strftime('%H:%M')
    wita = (utc_now + timedelta(hours=8)).strftime('%H:%M')
    wit  = (utc_now + timedelta(hours=9)).strftime('%H:%M')
    elapsed = int(time.time() - bot_start_time)
    up_hari = elapsed // 86400
    up_jam  = (elapsed % 86400) // 3600
    up_mnt  = (elapsed % 3600) // 60
    up_dtk  = elapsed % 60
    return f"""╔═══ 🛡️ PANEL ADMIN ═══╗

👥 User     : {stats['total_users']}   ✅ Aktif : {stats['active_users']}
🔔 Nunggu   : {stats['pending_transactions']}   🚫 Banned : {stats['banned_users']}
──────────────────────────────
💳 Transaksi : {stats['total_transactions']}
💰 Pendapatan : Rp{stats['total_revenue']:,}
──────────────────────────────
📆 KALENDER : {(utc_now + timedelta(hours=7)).strftime('%d')} - {(utc_now + timedelta(hours=7)).strftime('%m')} - {(utc_now + timedelta(hours=7)).strftime('%Y')}
🕐 WIB {wib} | WITA {wita} | WIT {wit}
⌛ WAKTU AKTIF : {up_hari}d - {up_jam}h - {up_mnt}m - {up_dtk}s
╚══════════════════════════╝"""


async def live_uptime_task(bot):
    global admin_live_msg
    await asyncio.sleep(5)
    while True:
        try:
            chat_id = admin_live_msg["chat_id"]
            message_id = admin_live_msg["message_id"]
            has_photo = admin_live_msg["has_photo"]
            if not chat_id or not message_id:
                break
            msg = build_admin_msg()
            if has_photo:
                await bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=message_id,
                    caption=msg,
                    reply_markup=get_owner_keyboard()
                )
            else:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=msg,
                    reply_markup=get_owner_keyboard()
                )
        except Exception:
            pass
        await asyncio.sleep(5)


def start_live_uptime(bot, chat_id: int, message_id: int, has_photo: bool):
    global admin_live_task, admin_live_msg
    admin_live_msg["chat_id"] = chat_id
    admin_live_msg["message_id"] = message_id
    admin_live_msg["has_photo"] = has_photo
    if admin_live_task and not admin_live_task.done():
        admin_live_task.cancel()
    admin_live_task = asyncio.create_task(live_uptime_task(bot))


def stop_live_uptime():
    global admin_live_task, admin_live_msg
    if admin_live_task and not admin_live_task.done():
        admin_live_task.cancel()
    admin_live_msg["chat_id"] = None
    admin_live_msg["message_id"] = None


async def delete_all_admin_panels(bot, chat_id: int):
    global admin_panel_msg_ids
    stop_live_uptime()
    for msg_id in admin_panel_msg_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
    admin_panel_msg_ids = []


async def send_admin_panel(bot, chat_id: int):
    global admin_panel_msg_ids
    stop_live_uptime()
    msg = build_admin_msg()
    try:
        with open("yanzy.jpg", "rb") as photo:
            new_msg = await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=msg,
                reply_markup=get_owner_keyboard()
            )
        start_live_uptime(bot, chat_id, new_msg.message_id, has_photo=True)
    except Exception:
        new_msg = await bot.send_message(
            chat_id=chat_id,
            text=msg,
            reply_markup=get_owner_keyboard()
        )
        start_live_uptime(bot, chat_id, new_msg.message_id, has_photo=False)
    admin_panel_msg_ids.append(new_msg.message_id)


async def handle_start_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or "-"
    chat = update.effective_chat

    is_admin = (user_id == ADMIN_ID)
    if not is_admin:
        try:
            admins = await context.bot.get_chat_administrators(chat.id)
            admin_ids = {a.user.id for a in admins}
            if user_id in admin_ids:
                is_admin = True
        except Exception:
            pass

    if not is_admin:
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    group_promo_pending[user_id] = {
        'chat_id': chat.id,
        'chat_title': chat.title or "Grup",
        'waiting': False
    }

    msg = (
        "╔═════════════╗\n"
        "    📢 PROMOSI GRUP\n"
        "╚═════════════╝\n\n"
        "⚠️ Pesan akan dikirim ke semua anggota via hidetag grup.\n\n"
        f"Hai, @{username} - {user_id}\n\n"
        "\"✅ MULAI\" untuk memasukkan pesan promosi.\n"
        "\"❌ BATAL\" untuk membatalkan promosi"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ BATAL", callback_data=f"grp_batal_{user_id}"),
        InlineKeyboardButton("✅ MULAI", callback_data=f"grp_mulai_{user_id}")
    ]])

    await context.bot.send_message(chat.id, msg, reply_markup=keyboard)


async def handle_group_promo_mulai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    target_user_id = int(parts[2])
    invoker_id = query.from_user.id

    if invoker_id != target_user_id:
        await query.answer("❌ Hanya pengirim perintah yang bisa mengkonfirmasi!", show_alert=True)
        return

    if target_user_id not in group_promo_pending:
        await query.answer("❌ Sesi promosi tidak ditemukan. Ketik /start lagi.", show_alert=True)
        return

    group_promo_pending[target_user_id]['waiting'] = True

    try:
        await query.message.delete()
    except Exception:
        pass

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ BATAL", callback_data=f"grp_batal_{target_user_id}")
    ]])

    sent = await context.bot.send_message(
        chat_id=query.message.chat.id,
        text="📝 Kirim pesan promosi Anda (text/foto/video):",
        reply_markup=keyboard
    )
    group_promo_pending[target_user_id]['prompt_msg_id'] = sent.message_id


async def handle_group_promo_batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    target_user_id = int(parts[2])
    invoker_id = query.from_user.id

    if invoker_id != target_user_id:
        await query.answer("❌ Bukan sesi kamu!", show_alert=True)
        return

    group_promo_pending.pop(target_user_id, None)
    chat_id = query.message.chat.id
    try:
        await query.message.delete()
    except Exception:
        pass


def _build_tags(user_ids: list) -> str:
    return "".join(f'<a href="tg://user?id={uid}">\u200b</a>' for uid in user_ids)


async def _kirim_hidetag_text(context, chat_id: int, pesan: str, user_ids: list) -> int:
    BATCH = 50
    total_tagged = 0
    import html as _html
    safe_pesan = _html.escape(pesan)
    for i in range(0, len(user_ids), BATCH):
        batch = user_ids[i:i + BATCH]
        tags = "".join(f'<a href="tg://user?id={uid}">\u200b</a>' for uid in batch)
        try:
            if i == 0:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{safe_pesan}{tags}",
                    parse_mode="HTML"
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=tags,
                    parse_mode="HTML"
                )
            total_tagged += len(batch)
        except Exception:
            pass
        await asyncio.sleep(0.3)
    return total_tagged


async def _kirim_hidetag_photo(context, chat_id: int, file_id: str, caption: str, user_ids: list) -> int:
    BATCH = 50
    total_tagged = 0
    for i in range(0, len(user_ids), BATCH):
        batch = user_ids[i:i + BATCH]
        tags = _build_tags(batch)
        try:
            if i == 0:
                cap = f"{caption}\n{tags}" if caption else tags
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=file_id,
                    caption=cap,
                    parse_mode="HTML"
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=tags,
                    parse_mode="HTML"
                )
            total_tagged += len(batch)
        except Exception:
            pass
        await asyncio.sleep(0.3)
    return total_tagged


async def _kirim_hidetag_video(context, chat_id: int, file_id: str, caption: str, user_ids: list) -> int:
    BATCH = 50
    total_tagged = 0
    for i in range(0, len(user_ids), BATCH):
        batch = user_ids[i:i + BATCH]
        tags = _build_tags(batch)
        try:
            if i == 0:
                cap = f"{caption}\n{tags}" if caption else tags
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=file_id,
                    caption=cap,
                    parse_mode="HTML"
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=tags,
                    parse_mode="HTML"
                )
            total_tagged += len(batch)
        except Exception:
            pass
        await asyncio.sleep(0.3)
    return total_tagged


async def _kirim_promo_selesai(status_msg, context, chat_id: int, tagged: int):
    try:
        await status_msg.delete()
    except Exception:
        pass


async def handle_group_member_tracker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.is_bot:
        return
    save_user(user.id, user.username or "-", f"{user.first_name or ''} {user.last_name or ''}".strip())
    chat = update.effective_chat
    if chat:
        save_group(chat.id, chat.title or "")


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat and update.effective_chat.type in ('group', 'supergroup'):
        await handle_start_group(update, context)
        return

    user = update.effective_user
    user_id = user.id
    username = user.username or "-"
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()

    query = update.callback_query
    if query:
        await query.answer()
        async def reply_text(text, **kwargs):
            await context.bot.send_message(user_id, text, **kwargs)
        async def reply_video(file, **kwargs):
            await context.bot.send_video(user_id, file, **kwargs)
        async def reply_photo(file, **kwargs):
            await context.bot.send_photo(user_id, file, **kwargs)
    else:
        async def reply_text(text, **kwargs):
            await update.message.reply_text(text, **kwargs)
        async def reply_video(file, **kwargs):
            await update.message.reply_video(file, **kwargs)
        async def reply_photo(file, **kwargs):
            await update.message.reply_photo(file, **kwargs)

    save_user(user_id, username, full_name)

    if user_id == ADMIN_ID:
        msg = build_admin_msg()
        if file_exists(FALLBACK_PHOTO):
            with open(FALLBACK_PHOTO, 'rb') as f:
                sent = await context.bot.send_photo(user_id, f, caption=msg, reply_markup=get_owner_keyboard())
            start_live_uptime(context.bot, user_id, sent.message_id, has_photo=True)
        else:
            sent = await context.bot.send_message(user_id, msg, reply_markup=get_owner_keyboard())
            start_live_uptime(context.bot, user_id, sent.message_id, has_photo=False)
        return

    user_status = get_user_status(user_id)

    if user_status['status'] == 'active' and not user_status['banned']:
        msg = f"""✅ Selamat datang kembali, {full_name}!

👤 Username: @{username}
🆔 ID: {user_id}
🏷️ Paket: {user_status['paket']}

Silakan klik tombol di bawah:"""

        paket_lower = (user_status['paket'] or '').lower()

        if paket_lower == 'owner':
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("DATA AKUN + APK", callback_data="data_akun_dan_apk"),
                 InlineKeyboardButton("ADMIN", url=ADMIN_URL)],
                [InlineKeyboardButton("STATUS", callback_data="cek_status")]
            ])
        else:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("DATA AKUN + APK", callback_data="data_akun_dan_apk"),
                 InlineKeyboardButton("ADMIN", url=ADMIN_URL)],
                [InlineKeyboardButton("STATUS", callback_data="cek_status"),
                 InlineKeyboardButton("UPGRADE PAKET", callback_data="upgrade_paket")]
            ])

        if file_exists("acctrx.mp4"):
            with open("acctrx.mp4", 'rb') as f:
                await reply_video(f, caption=msg, reply_markup=keyboard)
        elif file_exists(USER_PHOTO):
            with open(USER_PHOTO, 'rb') as f:
                await reply_photo(f, caption=msg, reply_markup=keyboard)
        else:
            await reply_text(msg, reply_markup=keyboard)
    else:
        msg = f"""🎉 Selamat datang, {full_name}! 🎉

👤 Username: @{username}
🆔 ID: {user_id}

Silakan pilih menu di bawah:

ORDER : UNTUK BELI APK PERMANEN
ADMIN : UNTUK BELI PER HARI/BULAN/TAHUN"""

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("ORDER", callback_data="order_apk_bug"),
             InlineKeyboardButton("ADMIN", url=ADMIN_URL)]
        ])

        if file_exists("menu.mp4"):
            with open("menu.mp4", 'rb') as f:
                await reply_video(f, caption=msg, reply_markup=keyboard)
        elif file_exists(USER_PHOTO):
            with open(USER_PHOTO, 'rb') as f:
                await reply_photo(f, caption=msg, reply_markup=keyboard)
        else:
            await reply_text(msg, reply_markup=keyboard)


async def handle_order_apk_bug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    username = query.from_user.username or "-"
    user_status = get_user_status(user_id)

    if user_status['status'] == 'active' and not user_status['banned']:
        msg = f"""✅ Anda sudah memiliki paket aktif!

👤 @{username}
🏷️ Paket: {user_status['paket']}

Silakan klik tombol di bawah:"""
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("DATA AKUN + APK", callback_data="data_akun_dan_apk")
        ]])
        if file_exists("acctrx.mp4"):
            with open("acctrx.mp4", 'rb') as f:
                await query.message.reply_video(f, caption=msg, reply_markup=keyboard)
        elif file_exists(USER_PHOTO):
            with open(USER_PHOTO, 'rb') as f:
                await query.message.reply_photo(f, caption=msg, reply_markup=keyboard)
        else:
            await query.message.reply_text(msg, reply_markup=keyboard)
        return

    msg = f"""🔰 PILIH APK 🔰

👤 @{username}
🆔 {user_id}

Silakan pilih APK yang ingin kamu order:"""

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("OTAX", callback_data="pilih_apk_otax"),
         InlineKeyboardButton("MANTA", callback_data="pilih_apk_mantax")]
    ])

    if file_exists("yanzy1.jpg"):
        with open("yanzy1.jpg", 'rb') as f:
            await query.message.reply_photo(f, caption=msg, reply_markup=keyboard)
    else:
        await query.message.reply_text(msg, reply_markup=keyboard)


async def handle_pilih_apk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    username = query.from_user.username or "-"
    apk_nama = "OTAX" if query.data == "pilih_apk_otax" else "MANTA"

    msg = f"""🔰 DAFTAR HARGA PAKET {apk_nama} 🔰

👤 @{username}
🆔 {user_id}

Silakan pilih paket di bawah ini:"""

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("MEMBER - Rp50.000", callback_data="pilih_member")],
        [InlineKeyboardButton("RESELLER - Rp70.000", callback_data="pilih_reseller")],
        [InlineKeyboardButton("PT - Rp90.000", callback_data="pilih_pt")],
        [InlineKeyboardButton("TK - Rp140.000", callback_data="pilih_tk")],
        [InlineKeyboardButton("OWNER - Rp190.000", callback_data="pilih_owner")]
    ])

    if file_exists("menu_paket.mp4"):
        with open("menu_paket.mp4", 'rb') as f:
            await query.message.reply_video(f, caption=msg, reply_markup=keyboard)
    elif file_exists(USER_PHOTO):
        with open(USER_PHOTO, 'rb') as f:
            await query.message.reply_photo(f, caption=msg, reply_markup=keyboard)
    else:
        await query.message.reply_text(msg, reply_markup=keyboard)


async def handle_pilih_paket(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, paket_kode: str):
    query = update.callback_query
    await query.answer()

    user_status = get_user_status(user_id)

    if user_status['status'] == 'active' and not user_status['banned']:
        await query.answer("❌ Anda sudah memiliki paket aktif!", show_alert=True)
        return

    paket = PAKET[paket_kode]
    save_temp_paket(user_id, paket['nama'], paket['harga'])

    msg = f"""💳 PEMBAYARAN {paket['nama']}

────────────────────────────────
📱 Paket: {paket['nama']}
💰 Harga: Rp{paket['harga']:,}
🆔 User ID: {user_id}
────────────────────────────────

📌 Cara Pembayaran:
1️⃣ Scan QRIS di atas
2️⃣ Lakukan transfer sesuai nominal
3️⃣ Upload bukti transfer disini"""

    if file_exists("qris.jpg"):
        with open("qris.jpg", 'rb') as f:
            await query.message.reply_photo(f, caption=msg)
    else:
        await query.message.reply_text("⚠️ QRIS sedang tidak tersedia, hubungi admin.\n\n" + msg)


async def handle_upgrade_paket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    username = query.from_user.username or "-"
    user_status = get_user_status(user_id)

    if user_status['banned']:
        await query.answer()
        await query.message.reply_text("🚫 Akun kamu telah dinonaktifkan.\nKlik /start untuk order kembali.")
        return

    if user_status['status'] != 'active':
        await query.answer("❌ Anda belum memiliki paket aktif!", show_alert=True)
        return

    paket_sekarang = (user_status['paket'] or '').lower()

    if paket_sekarang == 'owner':
        await query.answer("✅ Anda sudah di paket tertinggi!", show_alert=True)
        return

    harga_sekarang = PAKET.get(paket_sekarang, {}).get('harga', 0)
    idx_sekarang = PAKET_ORDER.index(paket_sekarang) if paket_sekarang in PAKET_ORDER else 0

    msg = f"""UPGRADE PAKET

👤 @{username}
🆔 {user_id}
🏷️ Paket Saat Ini: {user_status['paket']} (Rp{harga_sekarang:,})

Pilih paket upgrade:"""

    buttons = []
    for kode in PAKET_ORDER[idx_sekarang + 1:]:
        p = PAKET[kode]
        selisih = p['harga'] - harga_sekarang
        buttons.append([InlineKeyboardButton(
            f"{p['nama']} +Rp{selisih:,}", callback_data=f"upgrade_{kode}"
        )])

    keyboard = InlineKeyboardMarkup(buttons)
    if file_exists(USER_PHOTO):
        with open(USER_PHOTO, 'rb') as f:
            await query.message.reply_photo(f, caption=msg, reply_markup=keyboard)
    else:
        await query.message.reply_text(msg, reply_markup=keyboard)


async def handle_pilih_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE, paket_kode: str):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    user_status = get_user_status(user_id)

    if user_status['banned']:
        await query.answer()
        await query.message.reply_text("🚫 Akun kamu telah dinonaktifkan.\nKlik /start untuk order kembali.")
        return

    paket_sekarang = (user_status['paket'] or '').lower()
    harga_sekarang = PAKET.get(paket_sekarang, {}).get('harga', 0)
    paket_baru = PAKET[paket_kode]
    selisih = paket_baru['harga'] - harga_sekarang

    save_temp_paket(user_id, paket_baru['nama'], selisih, is_upgrade=True, paket_lama=user_status['paket'])

    msg = f"""💳 UPGRADE KE {paket_baru['nama']}

📱 {user_status['paket']} → {paket_baru['nama']}
💸 Bayar: Rp{selisih:,}
🆔 ID: {user_id}

1️⃣ Scan QRIS
2️⃣ Transfer Rp{selisih:,}
3️⃣ Upload bukti disini"""

    if file_exists("qris.jpg"):
        with open("qris.jpg", 'rb') as f:
            await query.message.reply_photo(f, caption=msg)
    else:
        await query.message.reply_text("⚠️ QRIS sedang tidak tersedia, hubungi admin.\n\n" + msg)


async def handle_bukti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or "-"
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()

    save_user(user_id, username, full_name)

    temp = get_temp_paket(user_id)

    if not temp:
        await update.message.reply_text("❌ Silakan pilih paket terlebih dahulu.")
        return

    paket_nama = temp['paket']
    paket_harga = temp['harga']
    is_upgrade = temp.get('is_upgrade', 0)
    paket_lama = temp.get('paket_lama', '')

    photo = update.message.photo[-1]
    file_id = photo.file_id
    file = await context.bot.get_file(file_id)

    if not os.path.exists('bukti'):
        os.makedirs('bukti')

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    bukti_path = f"bukti/{user_id}_{timestamp}.jpg"
    await file.download_to_drive(bukti_path)

    trans_id = save_transaction(user_id, paket_nama, paket_harga, bukti_path)

    if is_upgrade:
        await update.message.reply_text(f"""✅ Bukti upgrade terkirim!
🆔 ID Transaksi: {trans_id}

⏳ Menunggu konfirmasi admin...""")
        msg = f"""🔄 PERMINTAAN UPGRADE PAKET

👤 Nama: {full_name}
👤 Username: @{username}
🆔 User ID: {user_id}
📱 Upgrade: {paket_lama} → {paket_nama}
💰 Dibayar: Rp{paket_harga:,}
🆔 Transaksi: {trans_id}"""
    else:
        await update.message.reply_text(f"""✅ Bukti pembayaran terkirim!
🆔 ID Transaksi: {trans_id}

⏳ Menunggu konfirmasi admin...""")
        msg = f"""🆕 PERMINTAAN KONFIRMASI PEMBAYARAN

👤 Nama: {full_name}
👤 Username: @{username}
🆔 User ID: {user_id}
📱 Paket: {paket_nama}
💰 Harga: Rp{paket_harga:,}
🆔 Transaksi: {trans_id}"""

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ TOLAK", callback_data=f"tolak_{trans_id}"),
        InlineKeyboardButton("✅ TERIMA", callback_data=f"terima_{trans_id}")
    ]])

    with open(bukti_path, 'rb') as f:
        await context.bot.send_photo(ADMIN_ID, f, caption=msg, reply_markup=keyboard)

    clear_temp_paket(user_id)


async def handle_data_akun_dan_apk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    user_id = query.from_user.id
    user_status = get_user_status(user_id)

    if user_status['banned']:
        await query.answer()
        await query.message.reply_text("🚫 Akun kamu telah dinonaktifkan.\nKlik /start untuk order kembali.")
        return

    if user_status['status'] != 'active':
        await query.answer("❌ Anda belum memiliki akses. Silakan beli paket terlebih dahulu!", show_alert=True)
        return

    msg = f"""DATA AKUN + APK

👤 Username : {user_id}
🔑 Password: {user_status['password']}

🏷️ Paket: {user_status['paket']}

📌 Simpan data akun Anda dengan aman."""

    if file_exists("dataakun.mp4"):
        with open("dataakun.mp4", 'rb') as f:
            await query.message.reply_video(f, caption=msg)
    elif file_exists(USER_PHOTO):
        with open(USER_PHOTO, 'rb') as f:
            await query.message.reply_photo(f, caption=msg)
    else:
        await query.message.reply_text(msg)

    if file_exists("OTAX x MANTA.apk"):
        with open("OTAX x MANTA.apk", 'rb') as f:
            await query.message.reply_document(f, caption=f"""📱 OTAX x MANTA

✅ APK resmi khusus role {user_status['paket']}.

📌 Cara install:
1️⃣ Download file APK di atas
2️⃣ Buka file manager, temukan file APK
3️⃣ Klik install (izinkan install dari sumber tidak dikenal)
4️⃣ Buka aplikasi dan login dengan data akun kamu

⚠️ Jangan bagikan akun dan APK anda ke orang lain!""")
    else:
        await query.message.reply_text("❌ File APK sedang tidak tersedia. Silakan hubungi admin.")


async def handle_cek_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    user_status = get_user_status(user_id)

    if user_status['banned']:
        await query.answer()
        await query.message.reply_text("🚫 Akun kamu telah dinonaktifkan.\nKlik /start untuk order kembali.")
        return

    if user_status['status'] == 'active':
        msg = f"""✅ STATUS AKUN

📱 Paket: {user_status['paket']}
🔑 Password: {user_status['password']}
🆔 User ID: {user_id}

✅ Akun Anda AKTIF
📌 Akses berlaku PERMANEN"""
    else:
        msg = """⏳ STATUS AKUN

Anda belum memiliki paket aktif.

📌 Ketik /start untuk membeli paket."""

    if file_exists(USER_PHOTO):
        with open(USER_PHOTO, 'rb') as f:
            await query.message.reply_photo(f, caption=msg)
    else:
        await query.message.reply_text(msg)


async def handle_owner_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    old_message_id = query.message.message_id

    msg = build_admin_msg()

    try:
        with open("yanzy.jpg", "rb") as photo:
            new_msg = await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=msg,
                reply_markup=get_owner_keyboard()
            )
    except Exception:
        new_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=msg,
            reply_markup=get_owner_keyboard()
        )

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=old_message_id)
    except Exception:
        pass

    start_live_uptime(context.bot, chat_id, new_msg.message_id, has_photo=True)


async def handle_owner_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.answer("❌ Hanya untuk owner!", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("APK", callback_data="owner_update_apk"),
        InlineKeyboardButton("QRIS", callback_data="owner_update_qris")
    ], [
        InlineKeyboardButton("❌ BATAL", callback_data="batal_to_admin")
    ]])
    msg = "UPDATE\n\nPilih yang ingin diperbarui:"
    await query.message.reply_text(msg, reply_markup=keyboard)


async def handle_owner_update_apk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    context.user_data['owner_mode'] = 'update_apk'
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ BATAL", callback_data="batal_to_admin")
    ]])
    await query.message.reply_text("📱 Kirim file APK terbaru.", reply_markup=keyboard)


async def handle_owner_update_qris(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    context.user_data['owner_mode'] = 'update_qris'
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ BATAL", callback_data="batal_to_admin")
    ]])
    await query.message.reply_text("🖼️ Kirim foto QRIS terbaru.", reply_markup=keyboard)


async def handle_batal_update_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    context.user_data.pop('owner_mode', None)
    chat_id = query.message.chat_id
    try:
        await query.message.delete()
    except Exception:
        pass
    await delete_all_admin_panels(context.bot, chat_id)
    await send_admin_panel(context.bot, chat_id)


async def handle_batal_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    context.user_data.pop('owner_mode', None)
    context.user_data.pop('broadcast_mode', None)
    chat_id = query.message.chat_id
    try:
        await query.message.delete()
    except Exception:
        pass
    await delete_all_admin_panels(context.bot, chat_id)
    await send_admin_panel(context.bot, chat_id)


async def handle_join_grup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.answer("❌ Hanya untuk owner!", show_alert=True)
        return

    me = await context.bot.get_me()
    username = f"@{me.username}" if me.username else "bot ini"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ TUTUP", callback_data="join_grup_batal")
    ]])
    sent = await query.message.reply_text(
        f"🔗 CARA MENAMBAHKAN BOT KE GRUP\n\n"
        f"1. Buka grup yang ingin ditambahkan\n"
        f"2. Tambahkan {username} sebagai anggota\n"
        f"3. Grup akan otomatis terdaftar",
        reply_markup=keyboard
    )
    context.user_data['join_grup_prompt_id'] = sent.message_id


async def handle_join_grup_batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    context.user_data.pop('owner_mode', None)
    context.user_data.pop('join_grup_prompt_id', None)
    try:
        await query.message.delete()
    except Exception:
        pass


async def handle_daftar_grup(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    stop_live_uptime()
    GROUPS_PER_PAGE = 5
    groups, total = get_all_groups_paged(page=page, per_page=GROUPS_PER_PAGE)
    total_pages = max(1, (total + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)

    msg = f"🏘️ DAFTAR GRUP\nTotal: {total} grup"

    keyboard_rows = []
    if total == 0:
        msg = "🏘️ DAFTAR GRUP\n\nBelum ada grup yang terdaftar."
    else:
        for g in groups:
            keyboard_rows.append([
                InlineKeyboardButton(f"❌ {g['chat_title']}", callback_data=f"keluar_grup_{g['chat_id']}")
            ])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("← PREV", callback_data=f"daftar_grup_{page - 1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("NEXT →", callback_data=f"daftar_grup_{page + 1}"))
        if nav:
            keyboard_rows.append(nav)

    keyboard_rows.append([InlineKeyboardButton("← KEMBALI", callback_data="owner_stats")])
    keyboard = InlineKeyboardMarkup(keyboard_rows)

    await query.message.reply_text(msg, reply_markup=keyboard)


async def handle_keluar_grup_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    stop_live_uptime()
    chat_id = int(query.data.split("_", 2)[2])
    nama = get_group_name(chat_id)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ YA, KELUAR", callback_data=f"keluar_execute_{chat_id}"),
        InlineKeyboardButton("❌ BATAL", callback_data="daftar_grup_0")
    ]])
    msg = f"⚠️ Yakin bot keluar dari:\n{nama}?"
    await query.message.reply_text(msg, reply_markup=keyboard)


async def handle_keluar_grup_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    chat_id = int(query.data.split("_", 2)[2])
    nama = get_group_name(chat_id)

    try:
        await context.bot.leave_chat(chat_id)
        remove_group(chat_id)
        msg = f"✅ Bot berhasil keluar dari:\n{nama}"
    except Exception:
        msg = f"❌ Gagal keluar dari grup:\n{nama}"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("← KEMBALI", callback_data="daftar_grup_0")
    ]])
    await query.message.reply_text(msg, reply_markup=keyboard)


async def handle_list_user(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    users, total = get_all_users(page=page)
    total_pages = max(1, (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE)

    if total == 0:
        await query.message.reply_text("👥 Belum ada user yang terdaftar.")
        return

    msg = f"👥 DAFTAR USER\nTotal: {total} user | Halaman {page + 1}/{total_pages}"

    keyboard_rows = []
    for u in users:
        uname = f"@{u['username']}" if u['username'] != '-' else u['full_name']
        label = f"{uname} - {u['user_id']}"
        keyboard_rows.append([InlineKeyboardButton(label, callback_data=f"detail_user_{u['user_id']}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("← PREV", callback_data=f"list_user_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("NEXT →", callback_data=f"list_user_{page + 1}"))
    if nav_buttons:
        keyboard_rows.append(nav_buttons)
    keyboard_rows.append([InlineKeyboardButton("❌ BATAL", callback_data="batal_to_admin")])

    keyboard = InlineKeyboardMarkup(keyboard_rows)
    chat_id = query.message.chat_id
    try:
        await query.message.delete()
    except Exception:
        pass
    await context.bot.send_message(chat_id=chat_id, text=msg, reply_markup=keyboard)


async def handle_detail_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    target_id = int(query.data.split("_")[2])
    user = get_user_by_identifier(str(target_id))
    if not user:
        await query.message.reply_text("❌ User tidak ditemukan.")
        return

    paket_info = user['paket'] if user['paket'] else "Belum aktif"
    tanggal = user['tanggal_daftar'][:10] if user['tanggal_daftar'] else "-"
    status_ban = " (BANNED)" if user['banned'] else ""

    msg = f"""👤 DETAIL USER

Nama     : {user['full_name']}
Username : @{user['username']}
ID       : {user['user_id']}
Paket    : {paket_info}{status_ban}
Daftar   : {tanggal}"""

    if user['banned']:
        aksi_btn = InlineKeyboardButton("🔓 UNBAN USER", callback_data=f"buka_ban_confirm_{user['user_id']}")
    else:
        aksi_btn = InlineKeyboardButton("🔒 BAN USER", callback_data=f"ban_confirm_{user['user_id']}")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("CHAT USER", callback_data=f"pchat_{user['user_id']}"),
        aksi_btn
    ], [
        InlineKeyboardButton("❌ BATAL", callback_data="batal_to_admin")
    ]])

    chat_id = query.message.chat_id
    try:
        await query.message.delete()
    except Exception:
        pass
    await context.bot.send_message(chat_id=chat_id, text=msg, reply_markup=keyboard)


async def handle_owner_cek_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    context.user_data['owner_mode'] = 'cek_user'
    await query.message.reply_text(
        "Kirim ID atau username user yang ingin dicek.\n(Contoh: 123456789 atau @budi123)"
    )


async def handle_owner_ban_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    context.user_data['owner_mode'] = 'ban_user'
    await query.message.reply_text(
        "Kirim ID atau username user yang ingin di-ban.\n(Contoh: 123456789 atau @budi123)"
    )


async def handle_owner_ban_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, target_id: int):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    user = get_user_by_identifier(str(target_id))
    if not user:
        await query.message.reply_text("❌ User tidak ditemukan.")
        return

    paket_info = user['paket'] if user['paket'] else "Belum aktif"
    tanggal = user['tanggal_daftar'][:10] if user['tanggal_daftar'] else "-"

    msg = f"""⚠️ KONFIRMASI BAN USER

Nama     : {user['full_name']}
Username : @{user['username']}
ID       : {user['user_id']}
Paket    : {paket_info}
Daftar   : {tanggal}

🚫 Yakin ingin BAN user ini?
User tidak akan bisa mengakses akun."""

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ BATAL", callback_data="batal_to_admin"),
        InlineKeyboardButton("✅ Ya, Lanjut Ban", callback_data=f"ban_execute_{user['user_id']}")
    ]])
    try:
        if query.message.photo or query.message.video:
            await query.edit_message_caption(caption=msg, reply_markup=keyboard)
        else:
            await query.edit_message_text(msg, reply_markup=keyboard)
    except Exception:
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception:
            pass


async def handle_owner_ban_execute(update: Update, context: ContextTypes.DEFAULT_TYPE, target_id: int):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    user = get_user_by_identifier(str(target_id))
    if not user:
        await query.message.reply_text("❌ User tidak ditemukan.")
        return

    ban_user(target_id)

    review_requested.discard(target_id)
    awaiting_review_message.discard(target_id)
    tinjauan_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Minta Tinjauan", callback_data=f"minta_tinjauan_{target_id}")
    ]])
    try:
        await context.bot.send_message(
            target_id,
            "🚫 Akun kamu telah dinonaktifkan.\nMinta Tinjauan untuk kembali ke akses Anda.",
            reply_markup=tinjauan_kb
        )
    except Exception:
        pass

    user = get_user_by_identifier(str(target_id))
    paket_info = user['paket'] if user and user['paket'] else "Belum aktif"
    tanggal = user['tanggal_daftar'][:10] if user and user['tanggal_daftar'] else "-"
    full_name = user['full_name'] if user else str(target_id)
    username = user['username'] if user else "-"

    result_msg = f"""🚫 USER BERHASIL DI-BAN

Nama     : {full_name}
Username : @{username}
ID       : {target_id}
Paket    : {paket_info} (BANNED)
Daftar   : {tanggal}"""

    new_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("CHAT USER", callback_data=f"pchat_{target_id}"),
        InlineKeyboardButton("🔓 UNBAN USER", callback_data=f"buka_ban_confirm_{target_id}")
    ], [
        InlineKeyboardButton("❌ BATAL", callback_data="batal_to_admin")
    ]])
    try:
        if query.message.photo or query.message.video:
            await query.edit_message_caption(caption=result_msg, reply_markup=new_kb)
        else:
            await query.edit_message_text(result_msg, reply_markup=new_kb)
    except Exception:
        try:
            await query.edit_message_reply_markup(reply_markup=new_kb)
        except Exception:
            pass


async def handle_unban_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        await query.answer("❌ Hanya untuk owner!", show_alert=True)
        return

    target_id = int(query.data.split("_")[1])
    user = get_user_by_identifier(str(target_id))
    if not user:
        await query.message.reply_text("❌ User tidak ditemukan.")
        return

    unban_user(target_id)

    paket_info = user['paket'] if user['paket'] else "Belum aktif"
    tanggal = user['tanggal_daftar'][:10] if user['tanggal_daftar'] else "-"

    result_msg = f"""✅ USER BERHASIL DI-UNBAN

Nama     : {user['full_name']}
Username : @{user['username']}
ID       : {target_id}
Paket    : {paket_info}
Daftar   : {tanggal}"""

    new_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("CHAT USER", callback_data=f"pchat_{target_id}"),
        InlineKeyboardButton("🔒 BAN USER", callback_data=f"ban_confirm_{target_id}")
    ]])
    try:
        if query.message.photo or query.message.video:
            await query.edit_message_caption(caption=result_msg, reply_markup=new_kb)
        else:
            await query.edit_message_text(result_msg, reply_markup=new_kb)
    except Exception:
        try:
            await query.edit_message_reply_markup(reply_markup=new_kb)
        except Exception:
            pass

    try:
        await context.bot.send_message(
            target_id,
            "✅ Akunmu telah diaktifkan kembali.\nBuka Akun untuk menerima ke akses Anda."
        )
    except Exception:
        pass


async def handle_minta_tinjauan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id in review_requested:
        await query.answer("⚠️ Kamu sudah pernah mengirim permintaan tinjauan.", show_alert=True)
        return

    review_requested.add(user_id)
    awaiting_review_message.add(user_id)

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await context.bot.send_message(
        user_id,
        "📋 *Permintaan Tinjauan*\n\n"
        "Silakan kirim pesan permintaan maaf kepada admin.\n"
        "Tulis alasan kamu dan harap bersikap sopan.\n\n"
        "✏️ Ketik pesanmu sekarang:",
        parse_mode="Markdown"
    )


async def handle_buka_ban_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.answer("❌ Hanya untuk owner!", show_alert=True)
        return

    target_id = int(query.data.split("_")[-1])
    user = get_user_by_identifier(str(target_id))

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ BATAL", callback_data="batal_to_admin"),
        InlineKeyboardButton("✅ Ya, Lanjut UNBAN", callback_data=f"buka_ban_execute_{target_id}")
    ]])

    if user:
        paket_info = user['paket'] if user['paket'] else "Belum aktif"
        tanggal = user['tanggal_daftar'][:10] if user['tanggal_daftar'] else "-"

        msg = f"""✅ KONFIRMASI UNBAN USER

Nama     : {user['full_name']}
Username : @{user['username']}
ID       : {user['user_id']}
Paket    : {paket_info}
Daftar   : {tanggal}

🔓 Yakin ingin UNBAN user ini?
User akan bisa mengakses akun kembali."""

        try:
            if query.message.photo or query.message.video:
                await query.edit_message_caption(caption=msg, reply_markup=keyboard)
            else:
                await query.edit_message_text(msg, reply_markup=keyboard)
        except Exception:
            try:
                await query.edit_message_reply_markup(reply_markup=keyboard)
            except Exception:
                pass
    else:
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception:
            pass


async def handle_ban_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    target_id = int(query.data.split("_")[2])
    user = get_user_by_identifier(str(target_id))
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("CHAT USER", callback_data=f"pchat_{target_id}"),
        InlineKeyboardButton("BAN / UNBAN USER", callback_data=f"ban_confirm_{target_id}")
    ]])
    if user:
        paket_info = user['paket'] if user['paket'] else "Belum aktif"
        tanggal = user['tanggal_daftar'][:10] if user['tanggal_daftar'] else "-"
        status_ban = " (BANNED)" if user['banned'] else ""
        msg = f"""👤 DETAIL USER

Nama     : {user['full_name']}
Username : @{user['username']}
ID       : {user['user_id']}
Paket    : {paket_info}{status_ban}
Daftar   : {tanggal}"""
        try:
            if query.message.photo or query.message.video:
                await query.edit_message_caption(caption=msg, reply_markup=keyboard)
            else:
                await query.edit_message_text(msg, reply_markup=keyboard)
        except Exception:
            try:
                await query.edit_message_reply_markup(reply_markup=keyboard)
            except Exception:
                pass
    else:
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception:
            pass


async def handle_unban_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    target_id = int(query.data.split("_")[2])
    user = get_user_by_identifier(str(target_id))
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("CHAT USER", callback_data=f"pchat_{target_id}"),
        InlineKeyboardButton("BAN / UNBAN USER", callback_data=f"buka_ban_confirm_{target_id}")
    ]])
    if user:
        paket_info = user['paket'] if user['paket'] else "Belum aktif"
        tanggal = user['tanggal_daftar'][:10] if user['tanggal_daftar'] else "-"
        status_ban = " (BANNED)" if user['banned'] else ""
        msg = f"""👤 DETAIL USER

Nama     : {user['full_name']}
Username : @{user['username']}
ID       : {user['user_id']}
Paket    : {paket_info}{status_ban}
Daftar   : {tanggal}"""
        try:
            if query.message.photo or query.message.video:
                await query.edit_message_caption(caption=msg, reply_markup=keyboard)
            else:
                await query.edit_message_text(msg, reply_markup=keyboard)
        except Exception:
            try:
                await query.edit_message_reply_markup(reply_markup=keyboard)
            except Exception:
                pass
    else:
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception:
            pass


async def handle_buka_ban_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "buka_ban_batal":
        chat_id = query.message.chat_id
        try:
            await query.message.delete()
        except Exception:
            pass
        await delete_all_admin_panels(context.bot, chat_id)
        await send_admin_panel(context.bot, chat_id)
        return

    if query.from_user.id != ADMIN_ID:
        await query.answer("❌ Hanya untuk owner!", show_alert=True)
        return

    target_id = int(query.data.split("_")[-1])
    user = get_user_by_identifier(str(target_id))
    if not user:
        await query.message.reply_text("❌ User tidak ditemukan.")
        return

    unban_user(target_id)
    review_requested.discard(target_id)
    awaiting_review_message.discard(target_id)

    paket_info = user['paket'] if user['paket'] else "Belum aktif"
    tanggal = user['tanggal_daftar'][:10] if user['tanggal_daftar'] else "-"

    result_msg = f"""✅ USER BERHASIL DI-UNBAN

Nama     : {user['full_name']}
Username : @{user['username']}
ID       : {target_id}
Paket    : {paket_info}
Daftar   : {tanggal}"""

    new_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("CHAT USER", callback_data=f"pchat_{target_id}"),
        InlineKeyboardButton("🔒 BAN USER", callback_data=f"ban_confirm_{target_id}")
    ], [
        InlineKeyboardButton("❌ BATAL", callback_data="batal_to_admin")
    ]])
    try:
        if query.message.photo or query.message.video:
            await query.edit_message_caption(caption=result_msg, reply_markup=new_kb)
        else:
            await query.edit_message_text(result_msg, reply_markup=new_kb)
    except Exception:
        try:
            await query.edit_message_reply_markup(reply_markup=new_kb)
        except Exception:
            pass

    try:
        buka_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Buka Akun", callback_data="start_menu")
        ]])
        await context.bot.send_message(
            target_id,
            "✅ Akunmu telah diaktifkan kembali.\nBuka Akun untuk menerima ke akses Anda.",
            reply_markup=buka_kb
        )
    except Exception:
        pass


async def handle_start_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    user = update.effective_user
    user_id = user.id
    username = user.username or "-"
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()

    user_status = get_user_status(user_id)
    paket_lower = (user_status.get('paket') or '').lower()

    msg = f"""✅ Selamat datang kembali, {full_name}!

👤 Username: @{username}
🆔 ID: {user_id}
🏷️ Paket: {user_status.get('paket', '-')}

Silakan klik tombol di bawah:"""

    if paket_lower == 'owner':
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("DATA AKUN + APK", callback_data="data_akun_dan_apk"),
             InlineKeyboardButton("ADMIN", url=ADMIN_URL)],
            [InlineKeyboardButton("STATUS", callback_data="cek_status")]
        ])
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("DATA AKUN + APK", callback_data="data_akun_dan_apk"),
             InlineKeyboardButton("ADMIN", url=ADMIN_URL)],
            [InlineKeyboardButton("STATUS", callback_data="cek_status"),
             InlineKeyboardButton("UPGRADE PAKET", callback_data="upgrade_paket")]
        ])

    if file_exists("acctrx.mp4"):
        with open("acctrx.mp4", 'rb') as f:
            await context.bot.send_video(user_id, f, caption=msg, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await context.bot.send_message(user_id, msg, reply_markup=keyboard, parse_mode="Markdown")


async def handle_konfirmasi(update: Update, context: ContextTypes.DEFAULT_TYPE, trans_id: int, action: str):
    query = update.callback_query
    await query.answer()

    trans = get_transaction_by_id(trans_id)

    if not trans:
        await query.answer("❌ Transaksi tidak ditemukan!", show_alert=True)
        return

    if action == "terima":
        if trans['status'] == 'approved':
            await query.answer("✅ Transaksi sudah diproses sebelumnya!", show_alert=True)
            return

        password_acak = generate_password()
        update_transaction_status(trans_id, 'approved')
        update_user_status(trans['user_id'], 'active', trans['paket'], password_acak)
        conn_unban = sqlite3.connect('bot_database.db')
        conn_unban.execute('UPDATE users SET banned = 0 WHERE user_id = ?', (trans['user_id'],))
        conn_unban.commit()
        conn_unban.close()

        new_caption = f"""✅ PEMBAYARAN TELAH DIVERIFIKASI ✅

📱 Paket: {trans['paket']}
🆔 Transaksi: {trans_id}
👤 User ID: {trans['user_id']}

⏰ Diverifikasi pada: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"""

        try:
            await query.edit_message_caption(caption=new_caption)
        except Exception:
            pass

        msg = f"""✅ PEMBAYARAN DISETUJUI!

📱 Paket: {trans['paket']}
✅ Akses Anda sudah aktif.

Silakan klik tombol di bawah:"""

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("DATA AKUN + APK", callback_data="data_akun_dan_apk")
        ]])

        if file_exists("acctrx.mp4"):
            with open("acctrx.mp4", 'rb') as f:
                await context.bot.send_video(trans['user_id'], f, caption=msg, reply_markup=keyboard)
        else:
            await context.bot.send_message(trans['user_id'], msg, reply_markup=keyboard)

        await query.answer("✅ Pembayaran berhasil diverifikasi!")

    elif action == "tolak":
        if trans['status'] == 'rejected':
            await query.answer("❌ Transaksi sudah ditolak sebelumnya!", show_alert=True)
            return

        update_transaction_status(trans_id, 'rejected')

        new_caption = f"""❌ PEMBAYARAN DITOLAK ❌

📱 Paket: {trans['paket']}
🆔 Transaksi: {trans_id}
👤 User ID: {trans['user_id']}

⏰ Ditolak pada: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"""

        try:
            await query.edit_message_caption(caption=new_caption)
        except Exception:
            pass

        await context.bot.send_message(
            trans['user_id'],
            "❌ PEMBAYARAN DITOLAK!\n\nSilakan upload ulang bukti pembayaran yang valid."
        )
        await query.answer("❌ Pembayaran ditolak!")


async def handle_broadcast_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.answer("❌ Hanya untuk owner!", show_alert=True)
        return

    stop_live_uptime()
    try:
        await query.message.delete()
    except Exception:
        pass

    context.user_data['broadcast_mode'] = True
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ BATAL", callback_data="batal_to_admin")
    ]])
    sent = await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="UPDATE INFO AKTIF\n\nKirim pesan, foto, atau video yang ingin dikirim ke semua user.",
        reply_markup=keyboard
    )
    context.user_data['broadcast_prompt_msg_id'] = sent.message_id


async def handle_batal_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data.pop('broadcast_mode', None)
    context.user_data.pop('owner_mode', None)
    await update.message.reply_text("❌ Dibatalkan.")


async def handle_batal_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    context.user_data.pop('broadcast_mode', None)
    context.user_data.pop('owner_mode', None)
    chat_id = query.message.chat_id
    try:
        await query.message.delete()
    except Exception:
        pass
    await delete_all_admin_panels(context.bot, chat_id)
    await send_admin_panel(context.bot, chat_id)


async def _kirim_broadcast(context, semua_user: list, kirim_fn) -> Tuple[int, int]:
    berhasil = 0
    gagal = 0
    for uid in semua_user:
        try:
            await kirim_fn(uid)
            berhasil += 1
        except Exception:
            gagal += 1
    return berhasil, gagal


async def handle_private_chat_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        await query.answer("❌ Hanya untuk owner!", show_alert=True)
        return
    context.user_data['owner_mode'] = 'private_cari_user'
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Batal", callback_data="batal_broadcast")
    ]])
    sent = await query.message.reply_text(
        "CHAT USER\n\n"
        "Kirim ID atau username user yang ingin dihubungi:",
        reply_markup=keyboard
    )
    context.user_data['chat_prompt_msg_id'] = sent.message_id


async def handle_pchat_langsung(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        await query.answer("❌ Hanya untuk owner!", show_alert=True)
        return
    target_id = int(query.data.split("_")[1])
    user = get_user_by_identifier(str(target_id))
    uname = f"@{user['username']}" if user and user['username'] != '-' else f"ID {target_id}"
    context.user_data['owner_mode'] = f'private_tulis_{target_id}'
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Batal", callback_data="batal_broadcast")
    ]])
    sent = await query.message.reply_text(
        f"CHAT USER ke {uname}\n\n"
        "Kirim pesan berupa teks, foto, atau video:",
        reply_markup=keyboard
    )
    context.user_data['chat_prompt_msg_id'] = sent.message_id


async def handle_balas_u(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    target_user_id = int(query.data.split("_")[2])
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    context.user_data['owner_mode'] = f'reply_u_{target_user_id}'
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Akhiri Percakapan", callback_data=f"akhiri_{target_user_id}")
    ]])
    await query.message.reply_text(
        "✏️ Kirim pesan balasan:\n\nBisa kirim teks, foto, atau foto+caption.",
        reply_markup=keyboard
    )


async def handle_balas_a(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id not in active_conversations:
        await query.answer("❌ Percakapan sudah tidak aktif.", show_alert=True)
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    context.user_data['reply_admin'] = True
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Akhiri Percakapan", callback_data="akhiri_a")
    ]])
    await query.message.reply_text(
        "✏️ Kirim pesan balasan kamu:",
        reply_markup=keyboard
    )


async def handle_akhiri_percakapan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if data == "akhiri_a":
        user_id = query.from_user.id
        active_conversations.discard(user_id)
        context.user_data.pop('reply_admin', None)
        await query.message.reply_text("🚫 Kamu telah mengakhiri percakapan.")
        user = get_user_by_identifier(str(user_id))
        uname = f"@{user['username']}" if user and user['username'] != '-' else str(user_id)
        await context.bot.send_message(
            ADMIN_ID,
            f"🚫 Percakapan dengan {uname} telah diakhiri oleh user."
        )
    else:
        target_user_id = int(data.split("_")[1])
        active_conversations.discard(target_user_id)
        context.user_data.pop('owner_mode', None)
        user = get_user_by_identifier(str(target_user_id))
        uname = f"@{user['username']}" if user and user['username'] != '-' else str(target_user_id)
        await query.message.reply_text(f"✅ Percakapan dengan {uname} telah diakhiri.")
        try:
            await context.bot.send_message(
                target_user_id,
                "🚫 Percakapan telah diakhiri oleh admin."
            )
        except Exception:
            pass


async def handle_pesan_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type

    if chat_type in ('group', 'supergroup'):
        promo = group_promo_pending.get(user_id)
        if promo and promo.get('waiting') and promo['chat_id'] == update.effective_chat.id:
            group_promo_pending.pop(user_id, None)
            pesan = update.message.text
            group_chat_id = update.effective_chat.id
            prompt_msg_id = promo.get('prompt_msg_id')
            if prompt_msg_id:
                try:
                    await context.bot.delete_message(chat_id=group_chat_id, message_id=prompt_msg_id)
                except Exception:
                    pass
            try:
                await update.message.delete()
            except Exception:
                pass
            semua_user = get_all_user_ids()
            status_msg = await context.bot.send_message(chat_id=group_chat_id, text="⌛ Memulai promosi...")
            tagged = await _kirim_hidetag_text(context, group_chat_id, pesan, semua_user)
            group_promo_cooldown[user_id] = time.time()
            await _kirim_promo_selesai(status_msg, context, group_chat_id, tagged)
        return

    if user_id != ADMIN_ID and user_id in awaiting_review_message:
        awaiting_review_message.discard(user_id)
        pesan = update.message.text
        user = get_user_by_identifier(str(user_id))
        uname = f"@{user['username']}" if user and user['username'] != '-' else str(user_id)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Buka Ban", callback_data=f"buka_ban_confirm_{user_id}")
        ]])
        await context.bot.send_message(
            ADMIN_ID,
            f"📋 *PERMINTAAN TINJAUAN BAN*\n\n"
            f"👤 User: {uname} ({user_id})\n\n"
            f"💬 Pesan:\n{pesan}",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        await update.message.reply_text(
            "✅ Pesan kamu telah dikirim ke admin.\n"
            "Harap tunggu keputusan dari admin."
        )
        return

    if user_id != ADMIN_ID:
        if context.user_data.get('reply_admin') and user_id in active_conversations:
            context.user_data.pop('reply_admin', None)
            pesan = update.message.text
            user = get_user_by_identifier(str(user_id))
            uname = f"@{user['username']}" if user and user['username'] != '-' else str(user_id)
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Balas Pesan", callback_data=f"balas_u_{user_id}"),
                InlineKeyboardButton("Akhiri Percakapan", callback_data=f"akhiri_{user_id}")
            ]])
            await context.bot.send_message(
                ADMIN_ID,
                f"📨 PESAN DARI USER\n\n{uname} - {user_id}\n\n{pesan}",
                reply_markup=keyboard
            )
            await update.message.reply_text("✅ Pesan terkirim ke admin.")
        return

    if context.user_data.get('broadcast_mode'):
        context.user_data.pop('broadcast_mode', None)
        prompt_id = context.user_data.pop('broadcast_prompt_msg_id', None)
        if prompt_id:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=update.effective_chat.id, message_id=prompt_id, reply_markup=None
                )
            except Exception:
                pass
        pesan = update.message.text
        semua_user = get_all_user_ids()
        semua_grup = get_all_group_ids()
        total = len(semua_user)
        if total == 0:
            await update.message.reply_text("❌ Belum ada user yang terdaftar.")
            return
        status_msg = await update.message.reply_text(f"📢 Mengirim broadcast teks ke {total} user + {len(semua_grup)} grup...")
        async def kirim_teks(uid):
            await context.bot.send_message(chat_id=uid, text=f"📢 INFO TERBARU\n\n{pesan}")
        berhasil, gagal = await _kirim_broadcast(context, semua_user, kirim_teks)
        for gid in semua_grup:
            try:
                await context.bot.send_message(chat_id=gid, text=f"📢 INFO TERBARU\n\n{pesan}")
                await asyncio.sleep(0.3)
            except Exception:
                pass
        await status_msg.edit_text(
            f"✅ Info terbaru selesai dibagikan\n"
            f"📨 Total user : {total}\n"
            f"✅ Berhasil   : {berhasil}\n"
            f"❌ Gagal      : {gagal}\n"
            f"🏘️ Grup       : {len(semua_grup)}"
        )
        return

    owner_mode = context.user_data.get('owner_mode')

    if owner_mode == 'join_grup':
        context.user_data.pop('owner_mode', None)
        prompt_id = context.user_data.pop('join_grup_prompt_id', None)
        if prompt_id:
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_id)
            except Exception:
                pass
        me = await context.bot.get_me()
        username = f"@{me.username}" if me.username else "bot ini"
        await update.message.reply_text(
            f"ℹ️ Bot Telegram tidak bisa bergabung otomatis via link invite.\n\n"
            f"Silakan tambahkan {username} ke grup kamu secara manual.\n\n"
            f"Setelah bot ditambahkan ke grup, grup akan otomatis terdaftar."
        )
        return

    if owner_mode == 'cek_user':
        context.user_data.pop('owner_mode', None)
        identifier = update.message.text.strip()
        user = get_user_by_identifier(identifier)
        if not user:
            await update.message.reply_text("❌ User tidak ditemukan.")
            return
        paket_info = user['paket'] if user['paket'] else "Belum aktif"
        tanggal = user['tanggal_daftar'][:10] if user['tanggal_daftar'] else "-"
        status_ban = " (BANNED)" if user['banned'] else ""
        msg = f"""👤 DETAIL USER

Nama     : {user['full_name']}
Username : @{user['username']}
ID       : {user['user_id']}
Paket    : {paket_info}{status_ban}
Daftar   : {tanggal}"""
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("CHAT USER", callback_data=f"pchat_{user['user_id']}"),
            InlineKeyboardButton("🔒 BAN USER", callback_data=f"ban_confirm_{user['user_id']}"),
            InlineKeyboardButton("← KEMBALI", callback_data="owner_stats")
        ]])
        if file_exists(FALLBACK_PHOTO):
            with open(FALLBACK_PHOTO, 'rb') as f:
                await update.message.reply_photo(f, caption=msg, reply_markup=keyboard)
        else:
            await update.message.reply_text(msg, reply_markup=keyboard)
        return

    if owner_mode == 'ban_user':
        context.user_data.pop('owner_mode', None)
        identifier = update.message.text.strip()
        user = get_user_by_identifier(identifier)
        if not user:
            await update.message.reply_text("❌ User tidak ditemukan.")
            return
        paket_info = user['paket'] if user['paket'] else "Belum aktif"
        msg = f"""👤 Nama     : {user['full_name']}
👤 Username : @{user['username']}
🏷️ Paket    : {paket_info}

Yakin ingin ban user ini?"""
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Ya, Ban", callback_data=f"ban_execute_{user['user_id']}"),
            InlineKeyboardButton("❌ Batal", callback_data="owner_stats")
        ]])
        if file_exists(FALLBACK_PHOTO):
            with open(FALLBACK_PHOTO, 'rb') as f:
                await update.message.reply_photo(f, caption=msg, reply_markup=keyboard)
        else:
            await update.message.reply_text(msg, reply_markup=keyboard)
        return

    if owner_mode == 'private_cari_user':
        context.user_data.pop('owner_mode', None)
        prompt_id = context.user_data.pop('chat_prompt_msg_id', None)
        if prompt_id:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=update.effective_chat.id, message_id=prompt_id, reply_markup=None
                )
            except Exception:
                pass
        identifier = update.message.text.strip()
        user = get_user_by_identifier(identifier)
        if not user:
            await update.message.reply_text("❌ User tidak ditemukan.")
            return
        target_id = user['user_id']
        uname = f"@{user['username']}" if user['username'] != '-' else f"ID {target_id}"
        context.user_data['owner_mode'] = f'private_tulis_{target_id}'
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Batal", callback_data="batal_broadcast")
        ]])
        sent = await update.message.reply_text(
            f"✅ User ditemukan: {uname} (ID: {target_id})\n\n"
            "Kirim pesan ke user berupa text, foto, atau video.",
            reply_markup=keyboard
        )
        context.user_data['chat_prompt_msg_id'] = sent.message_id
        return

    if owner_mode and owner_mode.startswith('private_tulis_'):
        target_id = int(owner_mode.split('_')[2])
        context.user_data.pop('owner_mode', None)
        prompt_id = context.user_data.pop('chat_prompt_msg_id', None)
        if prompt_id:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=update.effective_chat.id, message_id=prompt_id, reply_markup=None
                )
            except Exception:
                pass
        pesan = update.message.text
        active_conversations.add(target_id)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Balas Pesan", callback_data="balas_a")
        ]])
        try:
            await context.bot.send_message(
                target_id,
                f"📩 PESAN DARI ADMIN\n\n{pesan}",
                reply_markup=keyboard
            )
            await update.message.reply_text("✅ Pesan terkirim.")
        except Exception:
            active_conversations.discard(target_id)
            await update.message.reply_text("❌ Gagal mengirim pesan ke user.")
        return

    if owner_mode and owner_mode.startswith('reply_u_'):
        target_id = int(owner_mode.split('_')[2])
        context.user_data.pop('owner_mode', None)
        pesan = update.message.text
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Balas Pesan", callback_data="balas_a")
        ]])
        try:
            await context.bot.send_message(
                target_id,
                f"📩 PESAN DARI ADMIN\n\n{pesan}",
                reply_markup=keyboard
            )
            await update.message.reply_text("✅ Pesan terkirim.")
        except Exception:
            await update.message.reply_text("❌ Gagal mengirim pesan ke user.")
        return


async def handle_foto_semua(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type

    if chat_type in ('group', 'supergroup'):
        promo = group_promo_pending.get(user_id)
        if promo and promo.get('waiting') and promo['chat_id'] == update.effective_chat.id:
            group_promo_pending.pop(user_id, None)
            photo = update.message.photo[-1]
            file_id = photo.file_id
            caption = update.message.caption or ""
            group_chat_id = update.effective_chat.id
            prompt_msg_id = promo.get('prompt_msg_id')
            if prompt_msg_id:
                try:
                    await context.bot.delete_message(chat_id=group_chat_id, message_id=prompt_msg_id)
                except Exception:
                    pass
            try:
                await update.message.delete()
            except Exception:
                pass
            semua_user = get_all_user_ids()
            status_msg = await context.bot.send_message(chat_id=group_chat_id, text="⌛ Memulai promosi...")
            tagged = await _kirim_hidetag_photo(context, group_chat_id, file_id, caption, semua_user)
            group_promo_cooldown[user_id] = time.time()
            await _kirim_promo_selesai(status_msg, context, group_chat_id, tagged)
        return

    if user_id != ADMIN_ID:
        await handle_bukti(update, context)
        return

    if context.user_data.get('broadcast_mode'):
        context.user_data.pop('broadcast_mode', None)
        prompt_id = context.user_data.pop('broadcast_prompt_msg_id', None)
        if prompt_id:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=update.effective_chat.id, message_id=prompt_id, reply_markup=None
                )
            except Exception:
                pass
        photo = update.message.photo[-1]
        file_id = photo.file_id
        caption = update.message.caption or ""
        semua_user = get_all_user_ids()
        semua_grup = get_all_group_ids()
        total = len(semua_user)
        if total == 0:
            await update.message.reply_text("❌ Belum ada user yang terdaftar.")
            return
        status_msg = await update.message.reply_text(f"📢 Mengirim broadcast foto ke {total} user + {len(semua_grup)} grup...")
        cap_text = f"📢 INFO TERBARU\n\n{caption}" if caption else "📢 INFO TERBARU"
        async def kirim_foto(uid):
            await context.bot.send_photo(chat_id=uid, photo=file_id, caption=cap_text)
        berhasil, gagal = await _kirim_broadcast(context, semua_user, kirim_foto)
        for gid in semua_grup:
            try:
                await context.bot.send_photo(chat_id=gid, photo=file_id, caption=cap_text)
                await asyncio.sleep(0.3)
            except Exception:
                pass
        await status_msg.edit_text(
            f"✅ Info terbaru selesai dibagikan\n"
            f"📨 Total user : {total}\n"
            f"✅ Berhasil   : {berhasil}\n"
            f"❌ Gagal      : {gagal}\n"
            f"🏘️ Grup       : {len(semua_grup)}"
        )
        return

    owner_mode = context.user_data.get('owner_mode', '')

    if owner_mode and owner_mode.startswith('private_tulis_'):
        target_id = int(owner_mode.split('_')[2])
        context.user_data.pop('owner_mode', None)
        prompt_id = context.user_data.pop('chat_prompt_msg_id', None)
        if prompt_id:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=update.effective_chat.id, message_id=prompt_id, reply_markup=None
                )
            except Exception:
                pass
        photo = update.message.photo[-1]
        file_id = photo.file_id
        caption = update.message.caption or ""
        active_conversations.add(target_id)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Balas Pesan", callback_data="balas_a")
        ]])
        try:
            await context.bot.send_photo(
                target_id,
                photo=file_id,
                caption=f"📩 PESAN DARI ADMIN\n\n{caption}" if caption else "📩 PESAN DARI ADMIN",
                reply_markup=keyboard
            )
            await update.message.reply_text("✅ Pesan terkirim.")
        except Exception:
            active_conversations.discard(target_id)
            await update.message.reply_text("❌ Gagal mengirim pesan ke user.")
        return

    if owner_mode and owner_mode.startswith('reply_u_'):
        target_id = int(owner_mode.split('_')[2])
        context.user_data.pop('owner_mode', None)
        photo = update.message.photo[-1]
        file_id = photo.file_id
        caption = update.message.caption or ""
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Balas Pesan", callback_data="balas_a")
        ]])
        try:
            await context.bot.send_photo(
                target_id,
                photo=file_id,
                caption=f"📩 PESAN DARI ADMIN\n\n{caption}" if caption else "📩 PESAN DARI ADMIN",
                reply_markup=keyboard
            )
            await update.message.reply_text("✅ Pesan terkirim.")
        except Exception:
            await update.message.reply_text("❌ Gagal mengirim pesan ke user.")
        return

    if owner_mode == 'update_qris':
        context.user_data.pop('owner_mode', None)
        photo = update.message.photo[-1]
        file_obj = await context.bot.get_file(photo.file_id)
        await file_obj.download_to_drive("qris.jpg")
        await update.message.reply_text("✅ QRIS diperbarui!")
        return


async def handle_video_semua(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type

    if chat_type in ('group', 'supergroup'):
        promo = group_promo_pending.get(user_id)
        if promo and promo.get('waiting') and promo['chat_id'] == update.effective_chat.id:
            group_promo_pending.pop(user_id, None)
            video = update.message.video
            file_id = video.file_id
            caption = update.message.caption or ""
            group_chat_id = update.effective_chat.id
            prompt_msg_id = promo.get('prompt_msg_id')
            if prompt_msg_id:
                try:
                    await context.bot.delete_message(chat_id=group_chat_id, message_id=prompt_msg_id)
                except Exception:
                    pass
            try:
                await update.message.delete()
            except Exception:
                pass
            semua_user = get_all_user_ids()
            status_msg = await context.bot.send_message(chat_id=group_chat_id, text="⌛ Memulai promosi...")
            tagged = await _kirim_hidetag_video(context, group_chat_id, file_id, caption, semua_user)
            group_promo_cooldown[user_id] = time.time()
            await _kirim_promo_selesai(status_msg, context, group_chat_id, tagged)
        return

    if user_id != ADMIN_ID:
        return

    owner_mode_v = context.user_data.get('owner_mode', '')

    if owner_mode_v and owner_mode_v.startswith('private_tulis_'):
        target_id = int(owner_mode_v.split('_')[2])
        context.user_data.pop('owner_mode', None)
        prompt_id = context.user_data.pop('chat_prompt_msg_id', None)
        if prompt_id:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=update.effective_chat.id, message_id=prompt_id, reply_markup=None
                )
            except Exception:
                pass
        video = update.message.video
        file_id = video.file_id
        caption = update.message.caption or ""
        active_conversations.add(target_id)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Balas Pesan", callback_data="balas_a")
        ]])
        try:
            await context.bot.send_video(
                target_id,
                video=file_id,
                caption=f"📩 PESAN DARI ADMIN\n\n{caption}" if caption else "📩 PESAN DARI ADMIN",
                reply_markup=keyboard
            )
            await update.message.reply_text("✅ Pesan terkirim.")
        except Exception:
            active_conversations.discard(target_id)
            await update.message.reply_text("❌ Gagal mengirim pesan ke user.")
        return

    if owner_mode_v and owner_mode_v.startswith('reply_u_'):
        target_id = int(owner_mode_v.split('_')[2])
        context.user_data.pop('owner_mode', None)
        video = update.message.video
        file_id = video.file_id
        caption = update.message.caption or ""
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Balas Pesan", callback_data="balas_a")
        ]])
        try:
            await context.bot.send_video(
                target_id,
                video=file_id,
                caption=f"📩 PESAN DARI ADMIN\n\n{caption}" if caption else "📩 PESAN DARI ADMIN",
                reply_markup=keyboard
            )
            await update.message.reply_text("✅ Pesan terkirim.")
        except Exception:
            await update.message.reply_text("❌ Gagal mengirim pesan ke user.")
        return

    if context.user_data.get('broadcast_mode'):
        context.user_data.pop('broadcast_mode', None)
        prompt_id = context.user_data.pop('broadcast_prompt_msg_id', None)
        if prompt_id:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=update.effective_chat.id, message_id=prompt_id, reply_markup=None
                )
            except Exception:
                pass
        video = update.message.video
        file_id = video.file_id
        caption = update.message.caption or ""
        semua_user = get_all_user_ids()
        semua_grup = get_all_group_ids()
        total = len(semua_user)
        if total == 0:
            await update.message.reply_text("❌ Belum ada user yang terdaftar.")
            return
        status_msg = await update.message.reply_text(f"📢 Mengirim broadcast video ke {total} user + {len(semua_grup)} grup...")
        cap_text = f"📢 INFO TERBARU\n\n{caption}" if caption else "📢 INFO TERBARU"
        async def kirim_video(uid):
            await context.bot.send_video(chat_id=uid, video=file_id, caption=cap_text)
        berhasil, gagal = await _kirim_broadcast(context, semua_user, kirim_video)
        for gid in semua_grup:
            try:
                await context.bot.send_video(chat_id=gid, video=file_id, caption=cap_text)
                await asyncio.sleep(0.3)
            except Exception:
                pass
        await status_msg.edit_text(
            f"✅ Info terbaru selesai dibagikan\n"
            f"📨 Total user : {total}\n"
            f"✅ Berhasil   : {berhasil}\n"
            f"❌ Gagal      : {gagal}\n"
            f"🏘️ Grup       : {len(semua_grup)}"
        )


async def handle_document_semua(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id != ADMIN_ID:
        return

    if context.user_data.get('owner_mode') == 'update_apk':
        context.user_data.pop('owner_mode', None)
        doc = update.message.document
        file_obj = await context.bot.get_file(doc.file_id)
        await file_obj.download_to_drive("OTAX x MANTA.apk")
        await update.message.reply_text("✅ APK diperbarui!")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'OK - Bot is running!')

    def log_message(self, format, *args):
        pass


BACKUP_DIR = "backups"
BACKUP_INTERVAL_SECONDS = 3600
BACKUP_MAX_KEEP = 5
DB_FILE = "bot_database.db"


def backup_database() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dest = os.path.join(BACKUP_DIR, f"bot_database_{timestamp}.db")
    try:
        src_conn = sqlite3.connect(DB_FILE)
        dst_conn = sqlite3.connect(dest)
        src_conn.backup(dst_conn)
        src_conn.close()
        dst_conn.close()
        files = sorted(glob.glob(os.path.join(BACKUP_DIR, "bot_database_*.db")))
        while len(files) > BACKUP_MAX_KEEP:
            os.remove(files.pop(0))
        print(f"✅ Backup berhasil: {dest}")
        return dest
    except Exception as e:
        print(f"❌ Backup gagal: {e}")
        return ""


def restore_latest_backup() -> bool:
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "bot_database_*.db")))
    if not files:
        return False
    latest = files[-1]
    try:
        shutil.copy2(latest, DB_FILE)
        print(f"✅ Database dipulihkan dari: {latest}")
        return True
    except Exception as e:
        print(f"❌ Restore gagal: {e}")
        return False


async def job_backup_database(context: ContextTypes.DEFAULT_TYPE):
    dest = backup_database()
    if dest:
        try:
            files = sorted(glob.glob(os.path.join(BACKUP_DIR, "bot_database_*.db")))
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"💾 *Backup Otomatis Selesai*\n\n"
                    f"📁 File: `{os.path.basename(dest)}`\n"
                    f"📊 Total backup tersimpan: {len(files)}/{BACKUP_MAX_KEEP}"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass


def start_health_server():
    port = int(os.environ.get('PORT', 8000))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"✅ Health check server berjalan di port {port}")


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning(f"Koneksi timeout/network error, diabaikan: {err}")
        return
    logger.error(f"Error tidak terduga: {err}", exc_info=err)


def main():
    if not os.path.exists(DB_FILE):
        restored = restore_latest_backup()
        if restored:
            print("✅ Database dipulihkan dari backup terakhir.")
        else:
            print("⚠️ Tidak ada backup ditemukan, database baru akan dibuat.")
    init_db()
    backup_database()
    start_health_server()

    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    app = Application.builder().token(BOT_TOKEN).request(request).build()
    app.add_error_handler(handle_error)

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("batal", handle_batal_broadcast))

    app.add_handler(CallbackQueryHandler(handle_owner_stats, pattern="^owner_stats$"))
    app.add_handler(CallbackQueryHandler(handle_owner_update, pattern="^owner_update$"))
    app.add_handler(CallbackQueryHandler(handle_owner_update_apk, pattern="^owner_update_apk$"))
    app.add_handler(CallbackQueryHandler(handle_owner_update_qris, pattern="^owner_update_qris$"))
    app.add_handler(CallbackQueryHandler(handle_batal_update_mode, pattern="^batal_update_mode$"))
    app.add_handler(CallbackQueryHandler(handle_batal_to_admin, pattern="^batal_to_admin$"))
    app.add_handler(CallbackQueryHandler(handle_join_grup, pattern="^join_grup$"))
    app.add_handler(CallbackQueryHandler(handle_join_grup_batal, pattern="^join_grup_batal$"))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_daftar_grup(u, c, int(u.callback_query.data.split("_")[2])),
        pattern="^daftar_grup_"
    ))
    app.add_handler(CallbackQueryHandler(handle_keluar_grup_confirm, pattern="^keluar_grup_"))
    app.add_handler(CallbackQueryHandler(handle_keluar_grup_execute, pattern="^keluar_execute_"))
    app.add_handler(CallbackQueryHandler(handle_detail_user, pattern="^detail_user_"))
    app.add_handler(CallbackQueryHandler(handle_owner_cek_user, pattern="^owner_cek_user$"))
    app.add_handler(CallbackQueryHandler(handle_owner_ban_input, pattern="^owner_ban_input$"))
    app.add_handler(CallbackQueryHandler(handle_broadcast_mode, pattern="^broadcast_mode$"))
    app.add_handler(CallbackQueryHandler(handle_batal_broadcast_callback, pattern="^batal_broadcast$"))
    app.add_handler(CallbackQueryHandler(handle_private_chat_user, pattern="^private_chat_user$"))
    app.add_handler(CallbackQueryHandler(handle_balas_a, pattern="^balas_a$"))
    app.add_handler(CallbackQueryHandler(handle_akhiri_percakapan, pattern="^akhiri_a$"))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_balas_u(u, c),
        pattern="^balas_u_"
    ))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_akhiri_percakapan(u, c),
        pattern="^akhiri_\\d+"
    ))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_list_user(u, c, int(u.callback_query.data.split("_")[2])),
        pattern="^list_user_"
    ))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_pchat_langsung(u, c),
        pattern="^pchat_"
    ))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_owner_ban_confirm(u, c, int(u.callback_query.data.split("_")[2])),
        pattern="^ban_confirm_"
    ))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_owner_ban_execute(u, c, int(u.callback_query.data.split("_")[2])),
        pattern="^ban_execute_"
    ))
    app.add_handler(CallbackQueryHandler(handle_unban_execute, pattern="^unban_"))
    app.add_handler(CallbackQueryHandler(handle_start_menu_callback, pattern="^start_menu$"))
    app.add_handler(CallbackQueryHandler(handle_minta_tinjauan, pattern="^minta_tinjauan_"))
    app.add_handler(CallbackQueryHandler(handle_buka_ban_confirm, pattern="^buka_ban_confirm_"))
    app.add_handler(CallbackQueryHandler(handle_ban_cancel, pattern="^ban_cancel_"))
    app.add_handler(CallbackQueryHandler(handle_unban_cancel, pattern="^unban_cancel_"))
    app.add_handler(CallbackQueryHandler(handle_buka_ban_execute, pattern="^buka_ban_execute_"))
    app.add_handler(CallbackQueryHandler(handle_buka_ban_execute, pattern="^buka_ban_batal$"))

    app.add_handler(CallbackQueryHandler(handle_group_promo_mulai, pattern="^grp_mulai_"))
    app.add_handler(CallbackQueryHandler(handle_group_promo_batal, pattern="^grp_batal_"))

    app.add_handler(CallbackQueryHandler(handle_order_apk_bug, pattern="^order_apk_bug$"))
    app.add_handler(CallbackQueryHandler(handle_pilih_apk, pattern="^pilih_apk_otax$"))
    app.add_handler(CallbackQueryHandler(handle_pilih_apk, pattern="^pilih_apk_mantax$"))
    app.add_handler(CallbackQueryHandler(handle_data_akun_dan_apk, pattern="^data_akun_dan_apk$"))
    app.add_handler(CallbackQueryHandler(handle_cek_status, pattern="^cek_status$"))
    app.add_handler(CallbackQueryHandler(handle_upgrade_paket, pattern="^upgrade_paket$"))

    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_pilih_paket(u, c, u.callback_query.from_user.id, "member"), pattern="^pilih_member$"))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_pilih_paket(u, c, u.callback_query.from_user.id, "reseller"), pattern="^pilih_reseller$"))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_pilih_paket(u, c, u.callback_query.from_user.id, "pt"), pattern="^pilih_pt$"))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_pilih_paket(u, c, u.callback_query.from_user.id, "tk"), pattern="^pilih_tk$"))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_pilih_paket(u, c, u.callback_query.from_user.id, "owner"), pattern="^pilih_owner$"))

    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_pilih_upgrade(u, c, u.callback_query.data.split("_")[1]), pattern="^upgrade_reseller$"))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_pilih_upgrade(u, c, u.callback_query.data.split("_")[1]), pattern="^upgrade_pt$"))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_pilih_upgrade(u, c, u.callback_query.data.split("_")[1]), pattern="^upgrade_tk$"))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_pilih_upgrade(u, c, u.callback_query.data.split("_")[1]), pattern="^upgrade_owner$"))

    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_konfirmasi(u, c, int(u.callback_query.data.split("_")[1]), "terima"), pattern="^terima_"))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: handle_konfirmasi(u, c, int(u.callback_query.data.split("_")[1]), "tolak"), pattern="^tolak_"))

    group_filter = filters.ChatType.GROUPS
    app.add_handler(MessageHandler(group_filter & filters.TEXT, handle_group_member_tracker), group=1)
    app.add_handler(MessageHandler(group_filter & filters.PHOTO, handle_group_member_tracker), group=1)
    app.add_handler(MessageHandler(group_filter & filters.VIDEO, handle_group_member_tracker), group=1)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pesan_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_foto_semua))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video_semua))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document_semua))

    job_queue = app.job_queue
    job_queue.run_repeating(
        job_backup_database,
        interval=BACKUP_INTERVAL_SECONDS,
        first=BACKUP_INTERVAL_SECONDS
    )

    print("✅ BOT PAKET AKTIF!")
    print(f"Bot token: {BOT_TOKEN[:10]}...")
    print(f"Owner ID: {ADMIN_ID}")
    print(f"💾 Backup otomatis setiap {BACKUP_INTERVAL_SECONDS//3600} jam, simpan {BACKUP_MAX_KEEP} backup terakhir.")
    print("Tekan Ctrl+C untuk berhenti.")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
