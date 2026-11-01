"""
UzServicePro - optimallashtirilgan versiya

Kerakli o'zgaruvchilar (.env yoki server environment):
    BOT_TOKEN, SMM_API_KEY, SMSMAN_API_KEY
Ixtiyoriy:
    ADMIN_ID, SMSMAN_APP_ID (Telegram uchun application_id, bo'sh bo'lsa avtomatik topiladi)
"""
import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import time
from collections import OrderedDict
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta
from typing import Any, Optional

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
)

# =========================================================
# SOZLAMALAR
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8711883494:AAE1v7k6dNgQ9ejIqnZ4NVLFgOR_QRIEcOA")
SMM_API_KEY = os.getenv("SMM_API_KEY", "52ecbad7bad826f42436e1abe4163ece")
SMSMAN_API_KEY = os.getenv("SMSMAN_API_KEY", "q-TTtGRieW-ReQMY99ieNNePyW2r9F-p")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN environment o'zgaruvchisi o'rnatilmagan!")

ADMIN_ID = int(os.getenv("ADMIN_ID", "8694507735"))
DB_NAME = "uzservicepro.db"

DEFAULT_CARD = "9860176618564593"
PAYNET_CARD = "7777015463375877"
USD_TRANSFER_CARD = "4023060102316865"
VISA_CARD = "4067070000433115"
CARD_OWNER = "Sh Shamuratova"

REQUIRED_CHANNEL = "@UzServiceGlobal"
REQUIRED_CHANNEL_LINK = "https://t.me/UzServiceGlobal"
REQUIRED_CHANNEL_TITLE = "UzService - Global"
SUPPORT_ADMIN = "@UzServiceGlobal_admin"
NEWS_CHANNEL = "@UzServiceGlobal"
CHAT_CHANNEL = "@UzService_chat"
REFERRAL_BONUS = 350.0
BOT_USERNAME = ""
ORDER_LOG_CHAT_ID = -1003745486531
USER_LOG_CHAT_ID = -1003812454235

SMM_API_URL = "https://caspersmm.com/api/v2"
SMSMAN_API_BASE = "https://api.sms-man.com/control/"

# ---- Narx siyosati ----
USD_UZS_RATE = 12900.0        # CBU dan avtomatik yangilanadi (xato bo'lsa shu qiymat qoladi)
USD_BUFFER = 1.03             # kurs tebranishidan himoya (+3%)
MIN_PROFIT_PERCENT = 15.0     # buyurtmada minimal foyda, undan past bo'lsa buyurtma to'xtatiladi
# (tannarx 1000 ta uchun so'mda < chegara) -> ustama foiz
MARGIN_TIERS = [(2000, 1.20), (10000, 0.80), (50000, 0.50), (10**12, 0.35)]
MIN_SERVICE_PRICE = 100       # 1000 ta uchun eng past narx (so'm)

# SMS-Man narxlari qaysi valyutada ekanini /smsman_debug orqali tekshiring.
# USD bo'lsa bo'sh qoldiring; RUB bo'lsa 1 rublning so'mdagi qiymatini yozing (masalan 150).
SMSMAN_UZS_PER_COST_UNIT = float(os.getenv("SMSMAN_UZS_PER_COST_UNIT", "0") or 0)
# Tannarx (so'm) -> ko'paytirgich. Arzon nomerlarga ko'proq, qimmatlariga kam ustama.
NUMBER_MARGIN_TIERS = [(6000, 1.50), (15000, 1.25), (40000, 1.15), (10**12, 1.10)]
NUMBER_MIN_PRICE = 6000       # nomerning eng past narxi (so'm)
RUB_UZS_RATE = 150.0          # CBU dan avtomatik yangilanadi
NUMBER_ROUND = 500            # narx shu songa yuqoriga yaxlitlanadi
NUMBER_TTL = 15 * 60          # SMS kelmasa shu vaqtdan keyin bekor + pul qaytariladi (sekund)
NUMBERS_PER_PAGE = 12
OFFERS_CACHE_TTL = 300   # fonda har 60 soniyada yangilanadi, foydalanuvchi kutmaydi

PROVIDER_CACHE_TTL = 600
SYNC_INTERVAL = 30 * 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("uzservicepro")

WARNING_TEXT = (
    "⚠️ Eslatma\n\n"
    "Taqdim etilgan nomerni Telegram’ning original rasmiy ilovasiga joylashtiring.\n\n"
    "❗ Muhim:\n"
    "SMS kod taqdim qilingandan keyingi barcha harakatlar uchun biz javobgar emasmiz."
)

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

MAIN_MENU_TEXTS = {
    "🛍 Xizmatlar", "📲 Nomer olish", "🛒 Buyurtmalarim", "👥 Pul ishlash", "💳 Hisobim",
    "💰 Hisob To'ldirish", "📞 Murojaat", "☎️ Qo'llab-quvvatlash", "🎛 Admin panel", "/start", "/cancel",
}

# =========================================================
# KATALOG (ixcham). Narx avtomatik hisoblanadi: provider kursi x USD x ustama.
# (nomi, provider_id, provider_rate USD/1000, min, tezlik: s=sekin, t=tezkor)
# =========================================================
CATALOG_SRC = OrderedDict([
    ("tg", ("🔵 Telegram", [
        ("👥 OBUNACHILAR (KAFOLATLI)", [
            ("30 kun", 1111, 0.247, 100, "s"), ("60 kun", 1112, 0.2964, 100, "s"),
            ("90 kun", 1113, 0.3458, 100, "s"), ("365 kun", 1115, 0.3952, 100, "s"),
            ("Lifetime", 1116, 0.4446, 100, "s")]),
        ("⚡ OBUNACHI (ARZON / KAFOLATSIZ)", [
            ("refillsiz", 5578, 0.0165, 100, "s"), ("refillsiz 2", 5580, 0.017, 100, "s"),
            ("real refillsiz", 1110, 0.26, 100, "s")]),
        ("💎 PREMIUM OBUNACHI", [
            ("premium members", 7576, 2.5, 100, "t"), ("premium members 2", 7581, 1.38, 100, "t"),
            ("premium 30 kun", 22452, 10.71, 100, "s")]),
        ("🤖 BOT START", [
            ("bot start refillsiz", 22403, 0.1575, 100, "s"), ("bot start 60 kun", 22408, 0.1995, 100, "s"),
            ("bot start 365 kun", 22409, 0.231, 100, "s")]),
        ("👁 PRASMOTR (KO‘RISH)", [
            ("views 🐢 sekin", 7744, 0.0069, 100, "s"), ("views ⚡ tezkor arzon", 22427, 0.0023, 100, "t"),
            ("views 🚀 ultra tezkor", 7745, 0.0087, 100, "t"), ("views ⚡ tezkor premium", 7746, 0.0088, 100, "t")]),
        ("🔁 AUTO POST VIEWS", [
            ("oxirgi 5 post", 7754, 0.0375, 100, "t"), ("oxirgi 20 post", 7757, 0.1125, 100, "t"),
            ("oxirgi 5 post 2", 22663, 0.0362, 100, "t"), ("oxirgi 20 post 2", 22665, 0.1108, 100, "t")]),
        ("❤️ REAKSIYALAR", [
            ("reaction", 5267, 0.0265, 100, "t"), ("reaction 2", 5269, 0.013, 100, "t"),
            ("reaction 3", 5333, 0.0242, 100, "t")]),
        ("🔁 AUTO REAKSIYA", [
            ("auto reaction 30 kun", 7710, 0.0325, 100, "s"), ("auto reaction 30 kun 2", 7737, 0.0325, 100, "s")]),
        ("📊 SO‘ROVNOMA (POLL)", [
            ("poll votes", 6323, 0.2625, 100, "t"), ("poll votes 2", 6324, 0.275, 100, "t"),
            ("poll votes 3", 7803, 0.3373, 100, "t")]),
        ("🔁 ULASHISH (SHARE)", [
            ("forward", 22720, 0.0506, 100, "t"), ("forward 2", 22721, 0.0484, 100, "t"),
            ("forward 3", 22722, 0.0484, 100, "t")]),
    ])),
    ("ig", ("🟣 Instagram", [
        ("Premium tezkor (HQ + 6 post)", [
            ("30 kun ♻️", 24056, 0.35, 100, "s"), ("60 kun ♻️", 24057, 0.36, 100, "s"),
            ("90 kun ♻️", 24058, 0.38, 100, "s"), ("365 kun ♻️", 24059, 0.39, 100, "s"),
            ("Lifetime", 24060, 0.41, 100, "s")]),
        ("📌 Eng optimal (30/60/90/365 + lifetime)", [
            ("Real Followers | 30 kun ♻️", 24050, 0.3329, 100, "s"), ("Real Followers | 60 kun ♻️", 24051, 0.35, 100, "s"),
            ("Real Followers | 90 kun ♻️", 24052, 0.365, 100, "s"), ("Real Followers | 365 kun ♻️", 24053, 0.38, 100, "s"),
            ("Real Followers | Lifetime ♻️", 24054, 0.4, 100, "s")]),
        ("📌 Arzon + stabil", [("30 kun", 24044, 0.28, 100, "s")]),
        ("💬 KOMMENTLAR", [
            ("Random Positive", 154, 0.155, 10, "t"), ("Random Negative", 155, 0.155, 10, "t"),
            ("Emoji Positive", 156, 0.155, 10, "t")]),
        ("❤️ LIKE (SIFATLI + ARZON)", [("HQ Likes (lifetime)", 184, 0.085, 100, "s")]),
        ("👁 KO‘RISHLAR (REELS / STORY / VIDEO)", [
            ("Video Views", 1552, 0.0011, 100, "t"), ("Video Views 2", 9374, 0.0013, 100, "t")]),
        ("🔁 SAVE / ULASHISH / REACH", [
            ("Reach + Save + Explore", 22902, 0.0627, 100, "t"), ("Reach + Save + Visit", 22903, 0.0669, 100, "t")]),
        ("💎 PREMIUM OBUNACHI", [
            ("HQ Followers", 1562, 0.98, 100, "t"), ("HQ Followers 2", 1563, 1.05, 100, "t"),
            ("HQ Followers 3", 1564, 1.12, 100, "t")]),
    ])),
    ("tt", ("⚫ TikTok", [
        ("🔥 ARZON / REAL", [
            ("Real Followers", 23859, 0.389, 100, "t"), ("Real + Bot mix", 1418, 0.64, 100, "t"),
            ("LQ Accounts (arzon)", 23887, 0.64, 100, "t")]),
        ("🔥 ENG ARZON / ULTRA FAST", [
            ("Views", 2143, 0.003, 100, "t"), ("Views 2", 2144, 0.0035, 100, "t"),
            ("Views 3", 1117, 0.0032, 100, "t"), ("Views 4", 1034, 0.005, 100, "t")]),
        ("💎 KAFOLATLI", [
            ("Kafolatli", 1488, 1.28, 100, "s"), ("Kafolatli 2", 1491, 0.85, 100, "s"),
            ("Low Drop", 23902, 1.11, 100, "s")]),
        ("🔥 ARZON LIKE", [("Likes", 1026, 0.011, 100, "t"), ("Likes 2", 2058, 0.015, 100, "t"),
                           ("Likes 3", 2063, 0.02, 100, "t")]),
        ("⚡ SIFATLI", [("Real Likes", 10184, 0.034, 100, "t"), ("HQ Likes", 1464, 0.0336, 100, "t")]),
        ("🔥 SAVE", [("Save", 1344, 0.0047, 100, "t"), ("Save 2", 1345, 0.0057, 100, "t")]),
        ("🔁 SHARE", [("Share", 23908, 0.0119, 100, "t"), ("HQ Share", 22908, 0.0158, 100, "t")]),
        ("🔴 LIVE", [("Live Views", 950, 0.864, 10, "t"), ("Live Views 2", 951, 1.72, 10, "t")]),
    ])),
    ("fb", ("📘 Facebook", [
        ("🔥 ARZON + HQ (ENG KO‘P SOTILADI)", [
            ("Followers HQ", 158, 0.1252, 100, "t"), ("Followers HQ 2", 159, 0.1378, 100, "t"),
            ("Followers HQ 3", 160, 0.1503, 100, "t"), ("Followers HQ 4", 161, 0.1628, 100, "t"),
            ("Kafolatli", 22740, 0.34, 100, "s"), ("Kafolatli 2", 22741, 0.42, 100, "s")]),
        ("💰 ENG ARZON (BOT / MIX)", [("Bot Followers", 1485, 0.1108, 100, "t"), ("Bot Accounts", 23899, 0.1431, 100, "t")]),
        ("🔥 ENG ARZON REELS / VIDEO", [("Reels Views", 5330, 0.0392, 100, "t")]),
        ("📺 LIVE STREAM", [("Live Views", 24097, 0.2174, 10, "t"), ("Live Views 2", 24098, 0.4347, 10, "t")]),
        ("❤️ LAYKLAR (SIFATLI)", [
            ("Post Likes HQ", 23990, 0.115, 100, "t"), ("Post Likes HQ 2", 23991, 0.12, 100, "t"),
            ("Likes (30 kun refill)", 5212, 0.1625, 100, "s")]),
        ("🔥 ENG ARZON REAKSIYA", [
            ("Like", 24009, 0.074, 100, "t"), ("Love", 24010, 0.074, 100, "t"),
            ("Haha", 24012, 0.074, 100, "t"), ("Angry", 24015, 0.074, 100, "t")]),
        ("🔁 ULASHISH (SHARE)", [("Share (90 kun refill)", 7831, 0.126, 100, "s"), ("Share", 9937, 0.132, 100, "t")]),
    ])),
    ("yt", ("🔴 YouTube", [
        ("💎 KAFOLATLI (30–365 kun)", [
            ("30 kun", 9477, 1.69, 100, "s"), ("60 kun", 9488, 1.95, 100, "s"),
            ("90 kun", 9568, 2.21, 100, "s"), ("365 kun", 9655, 2.73, 100, "s")]),
        ("🔥 ENG ARZON", [("Views", 1130, 0.19, 100, "t"), ("Real Views", 1131, 0.28, 100, "t")]),
        ("🚀 SHORTS / VIDEO", [("Shorts Views", 23978, 0.55, 100, "t")]),
        ("🔥 ARZON", [
            ("Likes", 24063, 0.053, 100, "t"), ("Likes 2", 24064, 0.06, 100, "t"), ("Likes 3", 24001, 0.054, 100, "t"),
            ("15 min", 20608, 0.049, 100, "t"), ("30 min", 20609, 0.099, 100, "t"), ("60 min", 20610, 0.198, 100, "t")]),
        ("💬 COMMENT LIKE", [
            ("Comment Like", 20571, 0.122, 10, "s"), ("30 kun", 20572, 0.142, 10, "s"),
            ("90 kun", 20573, 0.153, 10, "s"), ("Lifetime", 20575, 0.193, 10, "s")]),
    ])),
    ("x", ("𝕏 Twitter (X)", [
        ("🔥 ARZON / ULTRA FAST", [("Tweet Views", 23996, 0.0028, 100, "t")]),
        ("⚡ TEZKOR VARIANT", [("Views", 962, 0.005, 100, "t"), ("Views 2", 965, 0.0043, 100, "t")]),
        ("🔥 ARZON (Refillsiz)", [("Followers", 6848, 1.012, 100, "t")]),
        ("⚡ HQ / SIFATLI", [("HQ Followers", 413, 1.9, 100, "t"), ("Old Accounts", 412, 4.5, 100, "s")]),
        ("🔥 ARZON LIKE", [("Likes", 6934, 1.102, 100, "t")]),
        ("⚡ KAFOLATLI (30 kun)", [("Likes 30 kun", 6931, 2.0, 100, "s"), ("Likes 30 kun 2", 6920, 2.4, 100, "s")]),
        ("🔁 RETWEET (ULASHISH)", [("Retweet", 6963, 0.44, 100, "t"), ("Retweet 2", 6966, 1.67, 100, "t")]),
        ("🎧 SPACE (LIVE OVOZ)", [("15 min listener", 7104, 0.1359, 10, "t"), ("60 min", 7106, 0.5875, 10, "t")]),
    ])),
])


def build_catalog():
    catalog: OrderedDict = OrderedDict()
    lookup: dict[str, dict] = {}
    for pk, (ptitle, cats) in CATALOG_SRC.items():
        cdict = OrderedDict()
        for ci, (ctitle, items) in enumerate(cats):
            services = []
            for name, pid, rate, mn, speed in items:
                code = f"{pk}_{pid}"
                svc = {
                    "code": code, "name": name, "provider_id": pid, "rate": float(rate),
                    "min": int(mn), "max": 0, "speed": speed, "unit": "ta", "ok": True,
                    "platform": ptitle, "category": ctitle,
                }
                services.append(svc)
                lookup[code] = svc
            cdict[f"c{ci}"] = {"title": ctitle, "services": services}
        catalog[pk] = {"title": ptitle, "categories": cdict}
    return catalog, lookup


SERVICE_CATALOG, SERVICE_LOOKUP = build_catalog()

# =========================================================
# HTTP
# =========================================================
_http: Optional[aiohttp.ClientSession] = None


def http() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
    return _http


# =========================================================
# DB
# =========================================================
@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")  # har ulanish uchun alohida; tezlikni 5-10x oshiradi
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _add_col(conn, table: str, col: str, ddl: str):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init_db():
    with sqlite3.connect(DB_NAME) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
    with db_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, full_name TEXT, username TEXT, phone TEXT,
            balance REAL DEFAULT 0, total_deposit REAL DEFAULT 0,
            is_blocked INTEGER DEFAULT 0, is_frozen INTEGER DEFAULT 0,
            referred_by INTEGER, referral_count INTEGER DEFAULT 0, referral_earned REAL DEFAULT 0,
            referral_rewarded INTEGER DEFAULT 0, created_at TEXT, last_active TEXT);
        CREATE TABLE IF NOT EXISTS prices (key TEXT PRIMARY KEY, value REAL DEFAULT 0, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, type TEXT, amount REAL,
            details TEXT, status TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, order_api_id TEXT DEFAULT '',
            category TEXT, item TEXT, qty REAL, amount REAL, provider_cost REAL DEFAULT 0,
            link TEXT, status TEXT, refund_done INTEGER DEFAULT 0, created_at TEXT);
        CREATE TABLE IF NOT EXISTS promo_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, amount REAL DEFAULT 0,
            max_uses INTEGER DEFAULT 0, used_count INTEGER DEFAULT 0, expires_at TEXT,
            active INTEGER DEFAULT 1, created_at TEXT);
        CREATE TABLE IF NOT EXISTS user_promo_uses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, promo_code TEXT, used_at TEXT,
            UNIQUE(user_id, promo_code));
        CREATE TABLE IF NOT EXISTS numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, request_id TEXT,
            country_id TEXT, country TEXT, number TEXT, price REAL, provider_cost REAL DEFAULT 0,
            status TEXT DEFAULT 'waiting', sms_code TEXT DEFAULT '', created_at TEXT);
        """)
        for col, ddl in [("total_deposit", "REAL DEFAULT 0"), ("is_blocked", "INTEGER DEFAULT 0"),
                         ("is_frozen", "INTEGER DEFAULT 0"), ("last_active", "TEXT"), ("referred_by", "INTEGER"),
                         ("referral_count", "INTEGER DEFAULT 0"), ("referral_earned", "REAL DEFAULT 0"),
                         ("referral_rewarded", "INTEGER DEFAULT 0")]:
            _add_col(conn, "users", col, ddl)
        for col, ddl in [("order_api_id", "TEXT DEFAULT ''"), ("category", "TEXT DEFAULT ''"),
                         ("item", "TEXT DEFAULT ''"), ("qty", "REAL DEFAULT 0"), ("amount", "REAL DEFAULT 0"),
                         ("provider_cost", "REAL DEFAULT 0"), ("link", "TEXT DEFAULT ''"),
                         ("status", "TEXT DEFAULT 'pending'"), ("refund_done", "INTEGER DEFAULT 0"),
                         ("created_at", "TEXT")]:
            _add_col(conn, "orders", col, ddl)
        conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_numbers_status ON numbers(status);
        CREATE INDEX IF NOT EXISTS idx_users_active ON users(last_active);
        """)


def now_iso() -> str:
    return datetime.now().isoformat()


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with db_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def save_user(user_id: int, full_name: str, username: str, phone: str):
    with db_conn() as conn:
        conn.execute("""
        INSERT INTO users (user_id, full_name, username, phone, created_at, last_active)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, username=excluded.username,
            phone=excluded.phone, last_active=excluded.last_active
        """, (user_id, full_name, username, phone, now_iso(), now_iso()))


def get_balance(user_id: int) -> float:
    with db_conn() as conn:
        row = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        return float(row["balance"]) if row else 0.0


def get_total_deposit(user_id: int) -> float:
    with db_conn() as conn:
        row = conn.execute("SELECT total_deposit FROM users WHERE user_id=?", (user_id,)).fetchone()
        return float(row["total_deposit"]) if row else 0.0


def update_balance(user_id: int, amount: float):
    with db_conn() as conn:
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))


def try_debit(user_id: int, amount: float) -> bool:
    """Atomik yechish: balans yetarli bo'lsa ayiradi (ikki marta bosish / race condition himoyasi)."""
    with db_conn() as conn:
        cur = conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id=? AND balance >= ?",
            (amount, user_id, amount))
        return cur.rowcount > 0


def add_total_deposit(user_id: int, amount: float):
    with db_conn() as conn:
        conn.execute("UPDATE users SET total_deposit = total_deposit + ? WHERE user_id=?", (amount, user_id))


def set_user_flag(user_id: int, column: str, value: bool):
    assert column in {"is_blocked", "is_frozen"}
    _user_cache.pop(user_id, None)
    with db_conn() as conn:
        conn.execute(f"UPDATE users SET {column}=? WHERE user_id=?", (1 if value else 0, user_id))


def touch_user_activity(user_id: int):
    with db_conn() as conn:
        conn.execute("UPDATE users SET last_active=? WHERE user_id=?", (now_iso(), user_id))


def get_all_user_ids() -> list[int]:
    with db_conn() as conn:
        return [int(r["user_id"]) for r in conn.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()]


def add_transaction(user_id: int, tx_type: str, amount: float, details: str, status: str = "pending") -> int:
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO transactions (user_id, type, amount, details, status, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, tx_type, amount, details, status, now_iso()))
        return cur.lastrowid


def get_stats_summary() -> dict[str, float]:
    with db_conn() as conn:
        dep = conn.execute("SELECT COALESCE(SUM(total_deposit),0) s FROM users").fetchone()["s"]
        o = conn.execute("SELECT COALESCE(SUM(provider_cost),0) c, COALESCE(SUM(amount),0) a FROM orders "
                         "WHERE status != 'canceled'").fetchone()
        n = conn.execute("SELECT COALESCE(SUM(provider_cost),0) c, COALESCE(SUM(price),0) a FROM numbers "
                         "WHERE status IN ('waiting','done')").fetchone()
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    sold = float(o["a"]) + float(n["a"])
    cost = float(o["c"]) + float(n["c"])
    return {"total_users": float(users), "total_deposit": float(dep), "provider_spent": cost,
            "order_sales": sold, "profit": sold - cost}


def get_user_activity_stats(active_days: int = 7) -> dict[str, int]:
    border = (datetime.now() - timedelta(days=active_days)).isoformat()
    with db_conn() as conn:
        total = int(conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] or 0)
        active = int(conn.execute(
            "SELECT COUNT(*) c FROM users WHERE last_active IS NOT NULL AND last_active >= ?",
            (border,)).fetchone()["c"] or 0)
    return {"total_users": total, "active_users": active, "inactive_users": max(0, total - active),
            "active_days": active_days}


# ---- promo ----
def normalize_promo_code(code: str) -> str:
    return (code or "").strip().lower()


def create_promo_code(code: str, amount: float, max_uses: int = 0, expires_at: Optional[str] = None):
    with db_conn() as conn:
        conn.execute("""
        INSERT INTO promo_codes (code, amount, max_uses, used_count, expires_at, active, created_at)
        VALUES (?, ?, ?, 0, ?, 1, ?)
        ON CONFLICT(code) DO UPDATE SET amount=excluded.amount, max_uses=excluded.max_uses,
            expires_at=excluded.expires_at, used_count=0, active=1
        """, (normalize_promo_code(code), amount, max_uses, expires_at, now_iso()))


def delete_promo_code(code: str) -> bool:
    with db_conn() as conn:
        return conn.execute("UPDATE promo_codes SET active=0 WHERE code=?",
                            (normalize_promo_code(code),)).rowcount > 0


def apply_promo_code_for_user(user_id: int, code: str) -> tuple[bool, str, float]:
    normalized = normalize_promo_code(code)
    with db_conn() as conn:
        promo = conn.execute("SELECT * FROM promo_codes WHERE code=?", (normalized,)).fetchone()
        if not promo or not int(promo["active"] or 0):
            return False, "Promo-kod topilmadi yoki faol emas.", 0.0
        if promo["expires_at"] and str(promo["expires_at"]) < now_iso():
            conn.execute("UPDATE promo_codes SET active=0 WHERE code=?", (normalized,))
            return False, "Promo-kod muddati tugagan.", 0.0
        max_uses, used = int(promo["max_uses"] or 0), int(promo["used_count"] or 0)
        if max_uses > 0 and used >= max_uses:
            conn.execute("UPDATE promo_codes SET active=0 WHERE code=?", (normalized,))
            return False, "Promo-kod limiti tugagan.", 0.0
        try:
            conn.execute("INSERT INTO user_promo_uses (user_id, promo_code, used_at) VALUES (?,?,?)",
                         (user_id, normalized, now_iso()))
        except sqlite3.IntegrityError:
            return False, "Siz bu promo-kodni oldin ishlatgansiz.", 0.0
        amount = float(promo["amount"] or 0)
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
        conn.execute("UPDATE promo_codes SET used_count = used_count + 1 WHERE code=?", (normalized,))
        if max_uses > 0 and used + 1 >= max_uses:
            conn.execute("UPDATE promo_codes SET active=0 WHERE code=?", (normalized,))
    add_transaction(user_id, "promo_bonus", amount, f"Promo-kod: {normalized}", "approved")
    return True, "Promo-kod muvaffaqiyatli ishlatildi.", amount


# ---- referral ----
def get_referral_stats(user_id: int) -> Optional[sqlite3.Row]:
    with db_conn() as conn:
        return conn.execute("SELECT referral_count, referral_earned FROM users WHERE user_id=?",
                            (user_id,)).fetchone()


def reward_referral(referrer_id: int, invited_user_id: int):
    if referrer_id == invited_user_id:
        return
    with db_conn() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE user_id=?", (referrer_id,)).fetchone():
            return
        cur = conn.execute(
            "UPDATE users SET referred_by=?, referral_rewarded=1 WHERE user_id=? AND referral_rewarded=0",
            (referrer_id, invited_user_id))
        if cur.rowcount == 0:
            return
        conn.execute(
            "UPDATE users SET balance = balance + ?, referral_count = referral_count + 1, "
            "referral_earned = referral_earned + ? WHERE user_id=?",
            (REFERRAL_BONUS, REFERRAL_BONUS, referrer_id))
    add_transaction(referrer_id, "referral_bonus", REFERRAL_BONUS,
                    f"Taklif bonus: foydalanuvchi {invited_user_id}", "approved")


# ---- narxlar ----
_manual_cache: dict[str, float] = {}


def load_manual_prices():
    with db_conn() as conn:
        for r in conn.execute("SELECT key, value FROM prices WHERE key LIKE 'manual:%'").fetchall():
            _manual_cache[r["key"][7:]] = float(r["value"])


def set_manual_price(code: str, value: float):
    _manual_cache[code] = float(value)
    with db_conn() as conn:
        conn.execute("""INSERT INTO prices (key, value, updated_at) VALUES (?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                     (f"manual:{code}", value, now_iso()))


def get_manual_price(code: str) -> float:
    return _manual_cache.get(code, 0.0)


def auto_price(rate_usd: float) -> int:
    """1000 ta uchun sotuv narxi (so'm): tannarx + pog'onali ustama."""
    cost = rate_usd * USD_UZS_RATE * USD_BUFFER
    margin = next(m for limit, m in MARGIN_TIERS if cost < limit)
    price = max(MIN_SERVICE_PRICE, cost * (1 + margin))
    step = 10 if price < 1000 else 50 if price < 20000 else 100
    return int(math.ceil(price / step) * step)


def get_effective_price(code: str) -> float:
    svc = SERVICE_LOOKUP.get(code)
    if not svc:
        return 0.0
    manual = get_manual_price(code)
    return manual if manual > 0 else float(auto_price(svc["rate"]))


# ---- buyurtmalar ----
def add_order(user_id, category, item, qty, amount, link, status="processing", order_api_id="", provider_cost=0.0) -> int:
    with db_conn() as conn:
        cur = conn.execute("""INSERT INTO orders (user_id, order_api_id, category, item, qty, amount,
            provider_cost, link, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                           (user_id, order_api_id, category, item, qty, amount, provider_cost, link, status, now_iso()))
        return cur.lastrowid


def get_last_orders(user_id: int, limit: int = 30):
    with db_conn() as conn:
        return conn.execute("""SELECT id, order_api_id, status, qty, amount, link, item, created_at
        FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?""", (user_id, limit)).fetchall()


def get_processing_orders(limit: int = 100):
    with db_conn() as conn:
        return conn.execute("""SELECT id, user_id, order_api_id, item, qty, amount, link, status
        FROM orders WHERE order_api_id != '' AND status IN ('pending','approved','processing')
        ORDER BY id ASC LIMIT ?""", (limit,)).fetchall()


def update_order_status(order_id: int, status: str):
    with db_conn() as conn:
        conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))


def refund_order(order_id: int, amount_override: Optional[float] = None, new_status: str = "canceled"):
    """Bir martalik qaytarish. Qaytaradi: (qaytarildimi, summa, user_id, item)."""
    with db_conn() as conn:
        row = conn.execute("SELECT user_id, amount, item FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            return False, 0.0, 0, ""
        uid, item = int(row["user_id"] or 0), str(row["item"] or "")
        cur = conn.execute("UPDATE orders SET status=?, refund_done=1 WHERE id=? AND refund_done=0",
                           (new_status, order_id))
        if cur.rowcount == 0:
            return False, 0.0, uid, item
        amount = float(row["amount"] or 0) if amount_override is None else float(amount_override)
        if amount > 0 and uid > 0:
            conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, uid))
            conn.execute("INSERT INTO transactions (user_id, type, amount, details, status, created_at) "
                         "VALUES (?,?,?,?,?,?)",
                         (uid, "order_refund", amount, f"Qaytarildi. Order ID: {order_id} | {item}",
                          "approved", now_iso()))
        return True, amount, uid, item


# =========================================================
# YORDAMCHI
# =========================================================
def fmt(n) -> str:
    try:
        if n is None:
            return "0"
        if isinstance(n, str):
            cleaned = n.replace(" ", "").replace(",", ".").strip()
            n = float(cleaned) if cleaned else 0.0
        return f"{float(n):,.0f}".replace(",", " ")
    except Exception:
        return str(n)


def parse_num(text: Optional[str]) -> Optional[float]:
    try:
        v = float((text or "").replace(" ", "").replace(",", "."))
    except Exception:
        return None
    return v if v > 0 and math.isfinite(v) else None


def calc_total(price_per_1000: float, qty: float) -> float:
    return float(math.ceil(price_per_1000 / 1000.0 * qty))


def provider_cost_uzs(rate_usd_per_1000: float, qty: float) -> float:
    return float(math.ceil(rate_usd_per_1000 * USD_UZS_RATE * qty / 1000.0))


def sanitize_display_text(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"\s*[/\-]?\s*non[\s\-]?drop\s*", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\(\s*\)", "", text)
    return re.sub(r"\s{2,}", " ", text).strip(" -/")


def speed_text(svc: dict) -> str:
    return "🐢 Sekin (barqaror)" if svc["speed"] == "s" else "⚡ Tezkor"


async def safe_edit_text(message: Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup,
                                link_preview_options=LinkPreviewOptions(is_disabled=True))
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return
        await message.answer(text, reply_markup=reply_markup)


def chat_ok(chat_id) -> bool:
    return chat_id not in (None, "", 0, "0")


async def send_log(chat_id, text: str):
    if chat_ok(chat_id):
        with suppress(Exception):
            await bot.send_message(chat_id, text, link_preview_options=LinkPreviewOptions(is_disabled=True))


# =========================================================
# USD KURS + PROVIDER (SMM)
# =========================================================
async def refresh_usd_rate():
    global USD_UZS_RATE, RUB_UZS_RATE
    with suppress(Exception):
        async with http().get("https://cbu.uz/uz/arkhiv-kursov-valyut/json/RUB/") as r:
            d = await r.json(content_type=None)
        rub = float(str(d[0]["Rate"]).replace(",", "."))
        if 50 < rub < 400:
            RUB_UZS_RATE = rub
    try:
        async with http().get("https://cbu.uz/uz/arkhiv-kursov-valyut/json/USD/") as r:
            data = await r.json(content_type=None)
        rate = float(str(data[0]["Rate"]).replace(",", "."))
        if 8000 < rate < 20000:
            USD_UZS_RATE = rate
            logger.info("USD kursi yangilandi: %s", rate)
    except Exception as e:
        logger.warning("USD kursini olib bo'lmadi, eski qiymat qoladi (%s): %s", USD_UZS_RATE, e)


async def smm_api(payload: dict) -> Any:
    async with http().post(SMM_API_URL, data={"key": SMM_API_KEY, **payload}) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"Provider HTTP {resp.status}: {body[:200]}")
    try:
        data = json.loads(body)
    except Exception as e:
        raise RuntimeError(f"Provider JSON xato: {body[:200]}") from e
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data


_provider_cache: dict[str, dict] = {}
_provider_cache_at = 0.0


def _f(v) -> float:
    try:
        return float(str(v).replace(",", ".").strip())
    except Exception:
        return 0.0


async def get_provider_services(force: bool = False) -> dict[str, dict]:
    global _provider_cache, _provider_cache_at
    if _provider_cache and not force and time.monotonic() - _provider_cache_at < PROVIDER_CACHE_TTL:
        return _provider_cache
    raw = await smm_api({"action": "services"})
    if not isinstance(raw, list):
        raise RuntimeError("Provider services ro'yxati kelmadi")
    _provider_cache = {str(i.get("service", "")).strip(): {
        "rate": _f(i.get("rate")), "min": _f(i.get("min")), "max": _f(i.get("max")), "name": i.get("name", "")}
        for i in raw}
    _provider_cache_at = time.monotonic()
    return _provider_cache


async def sync_with_provider(force: bool = False) -> tuple[int, list[str]]:
    """Provider kurslari, min/max ni yangilaydi. Narxlar avtomatik shundan hisoblanadi."""
    by_id = await get_provider_services(force)
    missing = []
    for code, svc in SERVICE_LOOKUP.items():
        p = by_id.get(str(svc["provider_id"]))
        if not p or p["rate"] <= 0:
            svc["ok"] = False
            missing.append(code)
            continue
        svc["ok"] = True
        svc["rate"] = p["rate"]
        if p["min"] > 0:
            svc["min"] = int(max(p["min"], 1))
        svc["max"] = int(p["max"]) if p["max"] > 0 else 0
    logger.info("Provider sinxron: %d ta xizmat, %d ta topilmadi", len(SERVICE_LOOKUP) - len(missing), len(missing))
    return len(by_id), missing


async def create_provider_order(service_id, link: str, qty: int) -> str:
    result = await smm_api({"action": "add", "service": str(service_id), "link": link.strip(), "quantity": int(qty)})
    order_id = result.get("order") if isinstance(result, dict) else None
    if not order_id:
        raise RuntimeError(f"Provider order ID kelmadi: {result}")
    return str(order_id)


def normalize_provider_status(raw_status: str) -> str:
    s = re.sub(r"\s+", " ", (raw_status or "").strip().lower().replace("_", " ").replace("-", " "))
    if s in {"pending", "waiting", "queued"}:
        return "pending"
    if s in {"completed", "complete", "success", "done"}:
        return "completed"
    if s in {"canceled", "cancelled", "failed", "fail", "error", "refunded"}:
        return "canceled"
    if "complete" in s or "success" in s:
        return "completed"
    if "cancel" in s or "fail" in s or "refund" in s or "error" in s:
        return "canceled"
    return "processing"


# =========================================================
# SMS-MAN (NOMER OLISH) - to'liq qayta yozilgan
# =========================================================
_offers_cache: dict[str, Any] = {"at": 0.0, "items": []}
_app_id_cache: Optional[str] = os.getenv("SMSMAN_APP_ID") or None
_countries_cache: dict[str, str] = {}


async def sm_get(method: str, **params) -> Any:
    q = {"token": SMSMAN_API_KEY, **params}
    try:
        async with http().get(SMSMAN_API_BASE + method, params=q) as r:
            txt = await r.text()
    except Exception as e:
        return {"error_code": "network", "error_msg": str(e)}
    try:
        return json.loads(txt)
    except Exception:
        return {"error_code": "bad_response", "error_msg": txt[:200]}


def sm_error(data: Any) -> Optional[str]:
    if isinstance(data, dict):
        code, msg = data.get("error_code"), data.get("error_msg")
        if code or msg:
            return f"{code or ''} {msg or ''}".strip()
        if data.get("success") is False:
            return str(data)[:200]
    return None


def pretty_sm_error(err: str) -> str:
    low = err.lower()
    if "balance" in low or "money" in low or "funds" in low:
        return "SMS-Man balansida mablag‘ yetarli emas (admin balansni to‘ldirishi kerak)."
    if "no_numbers" in low or "no numbers" in low or "no free" in low:
        return "Hozircha bu davlatda bo‘sh nomer yo‘q. Boshqa davlatni tanlang."
    if "early" in low or "2 min" in low:
        return "Nomerni dastlabki 2 daqiqa ichida bekor qilib bo‘lmaydi. Birozdan keyin urinib ko‘ring."
    if "token" in low:
        return "SMS-Man API kaliti noto‘g‘ri."
    return err


def _items(data: Any):
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                v = dict(v)
                v.setdefault("id", k)
                yield v
            else:
                yield {"id": k, "title": v}
    elif isinstance(data, list):
        for v in data:
            if isinstance(v, dict):
                yield v


async def sm_app_id() -> str:
    """Telegram uchun application_id ni avtomatik topadi."""
    global _app_id_cache
    if _app_id_cache:
        return _app_id_cache
    data = await sm_get("applications")
    for it in _items(data):
        name = str(it.get("name") or it.get("title") or "").strip().lower()
        if name == "telegram":
            _app_id_cache = str(it.get("id"))
            return _app_id_cache
    logger.warning("SMS-Man applications ichida Telegram topilmadi: %s", str(data)[:200])
    return "1"


async def sm_country_names() -> dict[str, str]:
    global _countries_cache
    if _countries_cache:
        return _countries_cache
    data = await sm_get("countries")
    names = {}
    for it in _items(data):
        cid = it.get("id") or it.get("country_id")
        title = it.get("title") or it.get("name")
        if cid is not None and title:
            names[str(cid)] = str(title)
    _countries_cache = names
    return names


_detected_unit = {"uzs": 0.0, "cur": "USD"}


def cost_unit_uzs() -> float:
    return SMSMAN_UZS_PER_COST_UNIT or _detected_unit["uzs"] or USD_UZS_RATE


def detect_cost_unit(costs: list[float]):
    """SMS-Man narxlari USD yoki RUB ekanini medianaga qarab aniqlaydi (Telegram nomeri USD da odatda < 3)."""
    if SMSMAN_UZS_PER_COST_UNIT or not costs:
        return
    median = sorted(costs)[len(costs) // 2]
    if median > 8:
        _detected_unit.update(uzs=RUB_UZS_RATE, cur="RUB")
    else:
        _detected_unit.update(uzs=USD_UZS_RATE, cur="USD")


def number_price_uzs(cost: float) -> int:
    cost_uzs = cost * cost_unit_uzs() * USD_BUFFER
    mult = next(m for limit, m in NUMBER_MARGIN_TIERS if cost_uzs < limit)
    return int(max(NUMBER_MIN_PRICE, math.ceil(cost_uzs * mult / NUMBER_ROUND) * NUMBER_ROUND))


async def get_number_offers(force: bool = False) -> list[dict]:
    """Haqiqiy mavjud davlatlar (country_id, soni, narxi) - jonli SMS-Man ma'lumotlaridan."""
    if not force and _offers_cache["items"] and time.monotonic() - _offers_cache["at"] < OFFERS_CACHE_TTL:
        return _offers_cache["items"]
    app_id = await sm_app_id()
    data = await sm_get("get-prices")
    if sm_error(data):
        logger.warning("SMS-Man get-prices xato: %s", sm_error(data))
        return _offers_cache["items"]
    names = await sm_country_names()
    offers = []
    for it in _items(data):
        cid = it.get("country_id") or it.get("id")
        if cid is None or str(cid) == "0":
            continue
        if "cost" not in it and "numbers" not in it and "count" not in it:
            sub = it.get(str(app_id))
            if isinstance(sub, dict):
                it = {**sub, "country_id": cid}
        count = int(_f(it.get("numbers") if it.get("numbers") is not None else it.get("count")))
        cost = _f(it.get("cost") if it.get("cost") is not None else it.get("price"))
        if count <= 0 or cost <= 0:
            continue
        cid = str(cid)
        offers.append({"country_id": cid, "name": names.get(cid, f"Country {cid}"),
                       "count": count, "cost_usd": cost, "price": 0})
    detect_cost_unit([o["cost_usd"] for o in offers])
    for o in offers:
        o["price"] = number_price_uzs(o["cost_usd"])
    offers.sort(key=lambda x: (x["price"], x["name"].lower()))
    if offers:
        _offers_cache.update(at=time.monotonic(), items=offers)
    return offers


def find_offer(offers: list[dict], country_id: str) -> Optional[dict]:
    return next((o for o in offers if o["country_id"] == str(country_id)), None)


def offers_kb(offers: list[dict], page: int) -> InlineKeyboardMarkup:
    total_pages = max(1, math.ceil(len(offers) / NUMBERS_PER_PAGE))
    page = max(1, min(page, total_pages))
    chunk = offers[(page - 1) * NUMBERS_PER_PAGE: page * NUMBERS_PER_PAGE]
    rows = [[InlineKeyboardButton(text=f"🌍 {o['name']} — {fmt(o['price'])} so‘m",
                                  callback_data=f"nm_o_{o['country_id']}")] for o in chunk]
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"nm_p_{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"nm_p_{page + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔄 Yangilash", callback_data=f"nm_r_{page}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_number_row(num_id: int):
    with db_conn() as conn:
        return conn.execute("SELECT * FROM numbers WHERE id=?", (num_id,)).fetchone()


def get_waiting_numbers():
    with db_conn() as conn:
        return conn.execute("SELECT * FROM numbers WHERE status='waiting' ORDER BY id").fetchall()


def mark_number_done(num_id: int, code: str) -> bool:
    with db_conn() as conn:
        return conn.execute("UPDATE numbers SET status='done', sms_code=? WHERE id=? AND status='waiting'",
                            (code, num_id)).rowcount > 0


def refund_number(num_id: int) -> tuple[bool, float, int]:
    with db_conn() as conn:
        row = conn.execute("SELECT user_id, price FROM numbers WHERE id=?", (num_id,)).fetchone()
        if not row:
            return False, 0.0, 0
        cur = conn.execute("UPDATE numbers SET status='refunded' WHERE id=? AND status='waiting'", (num_id,))
        if cur.rowcount == 0:
            return False, 0.0, int(row["user_id"])
        uid, price = int(row["user_id"]), float(row["price"])
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (price, uid))
        conn.execute("INSERT INTO transactions (user_id, type, amount, details, status, created_at) "
                     "VALUES (?,?,?,?,?,?)",
                     (uid, "sms_number_refund", price, f"Nomer #{num_id} bekor qilindi", "approved", now_iso()))
        return True, price, uid


def parse_sms_code(data: Any) -> Optional[str]:
    if not isinstance(data, dict) or sm_error(data):
        return None
    for key in ("sms_code", "code", "sms"):
        val = data.get(key)
        if isinstance(val, list) and val:
            val = val[0].get("code") if isinstance(val[0], dict) else val[0]
        if val:
            return str(val).strip()
    return None


def number_kb(num_id: int, waiting: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if waiting:
        rows.append([InlineKeyboardButton(text="📩 SMS tekshirish", callback_data=f"nm_s_{num_id}")])
        rows.append([InlineKeyboardButton(text="❌ Bekor qilish (pul qaytadi)", callback_data=f"nm_c_{num_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Davlatlar", callback_data="nm_p_1")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def expire_number(row) -> None:
    """SMS kelmagan nomerni provayderda bekor qiladi va foydalanuvchiga pulni qaytaradi."""
    with suppress(Exception):
        await sm_get("set-status", request_id=row["request_id"], status="reject")
    ok, amount, uid = refund_number(int(row["id"]))
    if ok:
        with suppress(Exception):
            await bot.send_message(
                uid, f"⌛ Nomer ({row['number']}) uchun SMS kelmadi.\n💰 {fmt(amount)} so‘m balansingizga qaytarildi.")


async def numbers_loop():
    """SMS kelishini avtomatik kuzatadi: kod kelsa foydalanuvchiga yuboradi, kelmasa pulni qaytaradi."""
    await asyncio.sleep(10)
    while True:
        try:
            for row in get_waiting_numbers():
                try:
                    created = datetime.fromisoformat(row["created_at"])
                except Exception:
                    created = datetime.now()
                age = (datetime.now() - created).total_seconds()
                data = await sm_get("get-sms", request_id=row["request_id"])
                code = parse_sms_code(data)
                if code:
                    if mark_number_done(int(row["id"]), code):
                        with suppress(Exception):
                            await bot.send_message(
                                int(row["user_id"]),
                                f"✅ SMS kodi keldi\n\n📱 Nomer: {row['number']}\n🔐 Kod: {code}")
                        with suppress(Exception):
                            await sm_get("set-status", request_id=row["request_id"], status="close")
                elif age > NUMBER_TTL:
                    await expire_number(row)
                await asyncio.sleep(0.4)
        except Exception as e:
            logger.exception("numbers_loop xato: %s", e)
        await asyncio.sleep(10)


# =========================================================
# FSM
# =========================================================
class RegisterState(StatesGroup):
    waiting_contact = State()
    waiting_subscription = State()


class DepositState(StatesGroup):
    entering_amount = State()
    entering_proof = State()


class ServiceOrderState(StatesGroup):
    entering_qty = State()
    entering_link = State()
    confirming = State()


class SupportState(StatesGroup):
    waiting_text = State()


class PromoState(StatesGroup):
    waiting_code = State()


class AdminState(StatesGroup):
    waiting_user_block = State()
    waiting_user_unblock = State()
    waiting_user_freeze = State()
    waiting_user_unfreeze = State()
    waiting_balance_add_user = State()
    waiting_balance_add_amount = State()
    waiting_balance_remove_user = State()
    waiting_balance_remove_amount = State()
    waiting_broadcast_user = State()
    waiting_broadcast_text = State()
    waiting_broadcast_all_text = State()
    waiting_new_promo_code = State()
    waiting_new_promo_amount = State()
    waiting_new_promo_days = State()
    waiting_new_promo_max_uses = State()
    waiting_delete_promo_code = State()


# =========================================================
# KLAVIATURALAR
# =========================================================
def main_kb(user_id: Optional[int] = None):
    keyboard = [
        [KeyboardButton(text="🛍 Xizmatlar"), KeyboardButton(text="📲 Nomer olish")],
        [KeyboardButton(text="🛒 Buyurtmalarim"), KeyboardButton(text="👥 Pul ishlash")],
        [KeyboardButton(text="💳 Hisobim"), KeyboardButton(text="💰 Hisob To'ldirish")],
        [KeyboardButton(text="📞 Murojaat"), KeyboardButton(text="☎️ Qo'llab-quvvatlash")],
    ]
    if user_id == ADMIN_ID:
        keyboard.append([KeyboardButton(text="🎛 Admin panel")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def contact_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Kontakt yuborish", request_contact=True)]],
                               resize_keyboard=True, one_time_keyboard=True)


def subscription_prompt_text() -> str:
    return "⚠️ Iltimos, quyidagi kanalga obuna bo‘ling:\n🔥 Keyinchalik botdan foydalana olasiz!"


def subscribe_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"❌ | 🔥 {REQUIRED_CHANNEL_TITLE}", url=REQUIRED_CHANNEL_LINK)],
        [InlineKeyboardButton(text="✅ Obuna bo‘ldim", callback_data="check_sub_required")]])


def account_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Hisob To'ldirish", callback_data="acc_topup")],
        [InlineKeyboardButton(text="🎟 Promo-Bonus", callback_data="acc_promo")]])


def payment_methods_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏦 Bank karta", callback_data="pay_bank"),
         InlineKeyboardButton(text="🅿️ Payme", callback_data="pay_payme")],
        [InlineKeyboardButton(text="🔵 Click", callback_data="pay_click"),
         InlineKeyboardButton(text="🟢 Paynet", callback_data="pay_paynet")],
        [InlineKeyboardButton(text="💳 Visa", callback_data="pay_visa"),
         InlineKeyboardButton(text="☎️ Admin", callback_data="pay_admin")]])


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="admin_users")],
        [InlineKeyboardButton(text="💸 Tranzaksiyalar", callback_data="admin_transactions")],
        [InlineKeyboardButton(text="🎟 Promo kod", callback_data="admin_promos")]])


def admin_users_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛔ Bloklash", callback_data="admin_user_block"),
         InlineKeyboardButton(text="✅ Blokdan chiqarish", callback_data="admin_user_unblock")],
        [InlineKeyboardButton(text="➕ Hisob to‘ldirish", callback_data="admin_balance_add"),
         InlineKeyboardButton(text="➖ Hisobdan yechish", callback_data="admin_balance_remove")],
        [InlineKeyboardButton(text="🧊 Muzlatish", callback_data="admin_user_freeze"),
         InlineKeyboardButton(text="🔥 Muzdan chiqarish", callback_data="admin_user_unfreeze")],
        [InlineKeyboardButton(text="✉️ Foydalanuvchiga xabar", callback_data="admin_msg_one"),
         InlineKeyboardButton(text="📢 Barchaga xabar", callback_data="admin_msg_all")],
        [InlineKeyboardButton(text="📊 Aktiv / Noaktiv", callback_data="admin_user_activity_stats")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_home")]])


def admin_promos_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Yangi promo kod", callback_data="admin_promo_new")],
        [InlineKeyboardButton(text="🗑 Promo kodni o‘chirish", callback_data="admin_promo_delete")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_home")]])


def services_inline_kb():
    keys = list(SERVICE_CATALOG.keys())
    rows = []
    for i in range(0, len(keys), 2):
        rows.append([InlineKeyboardButton(text=SERVICE_CATALOG[k]["title"], callback_data=f"menu_platform_{k}")
                     for k in keys[i:i + 2]])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_platform_categories_kb(pk: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=sanitize_display_text(c["title"]), callback_data=f"menu_cat_{pk}_{ck}")]
            for ck, c in SERVICE_CATALOG[pk]["categories"].items()
            if any(s["ok"] for s in c["services"])]
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_services")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_category_services_kb(pk: str, ck: str) -> InlineKeyboardMarkup:
    rows = []
    for s in SERVICE_CATALOG[pk]["categories"][ck]["services"]:
        if not s["ok"]:
            continue
        icon = "🐢" if s["speed"] == "s" else "⚡"
        rows.append([InlineKeyboardButton(
            text=f"{icon} {sanitize_display_text(s['name'])} - {fmt(get_effective_price(s['code']))} so‘m",
            callback_data=f"srv_{s['code']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"menu_platform_{pk}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_order_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tasdiqlash", callback_data="confirm_order_yes"),
        InlineKeyboardButton(text="❌ Bekor qilish", callback_data="confirm_order_no")]])


def render_orders_page(rows, page: int, per_page: int = 3):
    if not rows:
        return "🛒 Buyurtmalaringiz hozircha yo‘q.", None
    total_pages = max(1, math.ceil(len(rows) / per_page))
    page = max(1, min(page, total_pages))
    chunk = rows[(page - 1) * per_page: page * per_page]
    statuses = {"pending": "🟡 Kutilmoqda", "approved": "🟢 Qabul qilindi", "processing": "🟠 Jarayonda",
                "completed": "✅ Tugallangan", "rejected": "❌ Rad etilgan", "canceled": "❌ Bekor qilingan"}
    out = f"🛒 Buyurtmalarim\n\n📄 Sahifa: {page}/{total_pages}\n\n"
    for r in chunk:
        out += (f"🪪 ID: {r['id']}\n📦 Xizmat: {sanitize_display_text(r['item'])}\n"
                f"📄 Holat: {statuses.get(r['status'], r['status'])}\n🔢 Miqdor: {fmt(r['qty'])}\n"
                f"💰 Summa: {fmt(r['amount'])} so‘m\n🔗 Link: {r['link']}\n🗓 Vaqt: {r['created_at'] or '-'}\n"
                f"——————————————\n\n")
    kb = None
    if total_pages > 1:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️", callback_data=f"orders_page_{max(1, page - 1)}"),
            InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"),
            InlineKeyboardButton(text="➡️", callback_data=f"orders_page_{min(total_pages, page + 1)}")]])
    return out.strip(), kb


# =========================================================
# RUXSAT / OBUNA
# =========================================================
_sub_cache: dict[int, float] = {}


async def is_user_subscribed(user_id: int) -> bool:
    if time.monotonic() - _sub_cache.get(user_id, -1e9) < 600:
        return True
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        status = str(getattr(member.status, "value", member.status))
        ok = status in {"member", "administrator", "creator"} or (
            status == "restricted" and getattr(member, "is_member", True))
    except Exception as e:
        logger.warning("Obuna tekshiruvi xato: %s", e)
        return False
    if ok:
        _sub_cache[user_id] = time.monotonic()
    return ok


def extract_referrer_id(text_value: str, current_user_id: int) -> Optional[int]:
    parts = (text_value or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        return None
    rid = int(parts[1].strip())
    return rid if rid != current_user_id and get_user(rid) else None


def get_referral_link(user_id: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start={user_id}" if BOT_USERNAME else f"/start {user_id}"


async def show_access_requirements(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user or not user["phone"]:
        await state.set_state(RegisterState.waiting_contact)
        await message.answer("Botdan foydalanish uchun avval telefon raqamingizni yuboring.",
                             reply_markup=contact_kb())
        return
    await state.set_state(RegisterState.waiting_subscription)
    await message.answer(subscription_prompt_text(), reply_markup=subscribe_inline_kb())


_user_cache: dict[int, tuple[float, Any]] = {}


def get_user_cached(uid: int):
    hit = _user_cache.get(uid)
    if hit and time.monotonic() - hit[0] < 15:
        return hit[1]
    row = get_user(uid)
    _user_cache[uid] = (time.monotonic(), row)
    return row


class AccessMiddleware(BaseMiddleware):
    """Barcha callback'lar uchun yagona tekshiruv (ro'yxat, obuna, blok)."""
    OPEN = {"check_sub_required", "noop"}

    async def __call__(self, handler, event: CallbackQuery, data):
        uid = event.from_user.id
        if uid == ADMIN_ID or event.data in self.OPEN:
            return await handler(event, data)
        user = get_user_cached(uid)
        if not user or not user["phone"] or not await is_user_subscribed(uid):
            await event.answer("Avval /start bosing, telefon yuboring va kanalga obuna bo‘ling.", show_alert=True)
            return
        if int(user["is_blocked"] or 0):
            await event.answer("⛔ Siz bloklangansiz.", show_alert=True)
            return
        return await handler(event, data)


dp.callback_query.outer_middleware(AccessMiddleware())
ADMIN = F.from_user.id == ADMIN_ID


@dp.callback_query(F.data == "noop")
async def noop(call: CallbackQuery):
    await call.answer()


# =========================================================
# START / MENYU
# =========================================================
@dp.message(Command("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Bekor qilindi.", reply_markup=main_kb(message.from_user.id))


@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    referrer_id = extract_referrer_id(message.text or "", message.from_user.id)
    if referrer_id:
        await state.update_data(referrer_id=referrer_id)
    user = get_user(message.from_user.id)
    if user:
        touch_user_activity(message.from_user.id)
    if user and user["phone"]:
        if await is_user_subscribed(message.from_user.id):
            await state.clear()
            await message.answer("Asosiy menyu 👇", reply_markup=main_kb(message.from_user.id))
            return
        await show_access_requirements(message, state)
        return
    await state.set_state(RegisterState.waiting_contact)
    await message.answer(
        "Assalomu alaykum.\n\nBot ishlashi uchun 2 ta shart bor:\n1) Telefon raqamingizni yuborish\n"
        f"2) {REQUIRED_CHANNEL} kanaliga obuna bo‘lish\n\nAvval telefon raqamingizni yuboring.",
        reply_markup=contact_kb())


async def send_orders_message(message: Message):
    text_out, kb = render_orders_page(get_last_orders(message.from_user.id), 1)
    await message.answer(text_out, reply_markup=kb, link_preview_options=LinkPreviewOptions(is_disabled=True))


async def send_referral_message(message: Message, state: FSMContext):
    stats = get_referral_stats(message.from_user.id)
    cnt = int(stats["referral_count"]) if stats else 0
    earned = float(stats["referral_earned"]) if stats else 0.0
    await message.answer(
        "👥 Pul ishlash - referal tizimi\n\n"
        f"🔗 Sizning referal linkingiz:\n{get_referral_link(message.from_user.id)}\n\n"
        f"💸 Har bir taklif qilgan do‘st uchun: {fmt(REFERRAL_BONUS)} so‘m\n"
        f"👤 Taklif qilinganlar: {cnt} ta\n💰 Referaldan ishlagan pulingiz: {fmt(earned)} so‘m\n\n"
        "Do‘stingiz shu link orqali kirib, telefon raqamini yuborsa va kanalga obuna bo‘lsa, bonus avtomatik qo‘shiladi.")


async def send_balance_message(message: Message):
    uid = message.from_user.id
    await message.answer(
        "💼 Kabinetingizga xush kelibsiz.\n\n📋 Ma'lumotlaringiz\n"
        f"🪪 ID raqam: {uid}\n💳 Hisobingiz: {fmt(get_balance(uid))} so‘m\n"
        f"✅ Kiritgan pullaringiz: {fmt(get_total_deposit(uid))} so‘m",
        reply_markup=account_inline_kb())


async def send_support_message(message: Message):
    await message.answer(f"☎️ Qo'llab-quvvatlash\n\n👨‍💻 Admin: {SUPPORT_ADMIN}\n"
                         f"📰 Yangiliklar: {NEWS_CHANNEL}\n💬 Chat: {CHAT_CHANNEL}")


async def show_numbers_menu(target: Message, page: int = 1, edit: bool = False):
    offers = await get_number_offers()
    if not offers:
        text, kb = ("😔 Hozircha SMS-Man’da bo‘sh Telegram nomerlar topilmadi yoki xizmat vaqtincha ishlamayapti.\n"
                    "Birozdan keyin qayta urinib ko‘ring."), InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔄 Yangilash", callback_data="nm_r_1")]])
    else:
        text, kb = "🛍 Telegram Tayyor Akkauntlar bo‘limi!\n🌍 Kerakli davlatni tanlang: 👇", offers_kb(offers, page)
    if edit:
        await safe_edit_text(target, text, kb)
    else:
        await target.answer(text, reply_markup=kb)


def guarded_user(message: Message):
    user = get_user(message.from_user.id)
    return user


@dp.message(F.text.in_(MAIN_MENU_TEXTS))
async def global_menu_router(message: Message, state: FSMContext):
    if await state.get_state() and message.text != "/cancel":
        await state.clear()
    text = message.text
    if text == "/start":
        return await start_cmd(message, state)
    user = get_user(message.from_user.id)
    if not user or not user["phone"] or not await is_user_subscribed(message.from_user.id):
        return await show_access_requirements(message, state)
    touch_user_activity(message.from_user.id)
    if int(user["is_blocked"] or 0):
        return await message.answer("⛔ Siz bloklangansiz. Admin bilan bog‘laning.")

    if text == "🎛 Admin panel":
        if message.from_user.id != ADMIN_ID:
            return await message.answer("Siz admin emassiz.")
        return await message.answer("🎛 Admin panel", reply_markup=admin_panel_kb())
    if text == "🛍 Xizmatlar":
        return await message.answer("✅ Kerakli bo‘limni tanlang:", reply_markup=services_inline_kb())
    if text == "📲 Nomer olish":
        return await show_numbers_menu(message)
    if text == "🛒 Buyurtmalarim":
        return await send_orders_message(message)
    if text == "👥 Pul ishlash":
        return await send_referral_message(message, state)
    if text == "💳 Hisobim":
        return await send_balance_message(message)
    if text == "💰 Hisob To'ldirish":
        return await message.answer("👇 To‘lov usulini tanlang:", reply_markup=payment_methods_kb())
    if text == "📞 Murojaat":
        await state.set_state(SupportState.waiting_text)
        return await message.answer("📄 Murojaat matnini yozib yuboring.")
    if text == "☎️ Qo'llab-quvvatlash":
        return await send_support_message(message)


@dp.message(Command("balance"))
async def balance_cmd(message: Message, state: FSMContext):
    message.text = "💳 Hisobim"
    await global_menu_router(message, state)


@dp.message(Command("orders"))
async def orders_cmd(message: Message, state: FSMContext):
    message.text = "🛒 Buyurtmalarim"
    await global_menu_router(message, state)


@dp.message(Command("referral"))
async def referral_cmd(message: Message, state: FSMContext):
    message.text = "👥 Pul ishlash"
    await global_menu_router(message, state)


@dp.message(Command("support"))
async def support_cmd(message: Message, state: FSMContext):
    message.text = "☎️ Qo'llab-quvvatlash"
    await global_menu_router(message, state)


# =========================================================
# RO'YXATDAN O'TISH
# =========================================================
async def finish_registration(user, phone: str, state: FSMContext, answer_to: Message):
    save_user(user.id, user.full_name, user.username or "", phone)
    _user_cache.pop(user.id, None)
    username = f"@{user.username}" if user.username else "username_yo'q"
    await send_log(USER_LOG_CHAT_ID, f"🆕 Yangi foydalanuvchi\n\n👤 Ism: {user.full_name}\n🪪 ID: {user.id}\n"
                                    f"🔗 Username: {username}\n📞 Telefon: {phone}")
    data = await state.get_data()
    if data.get("referrer_id"):
        reward_referral(int(data["referrer_id"]), user.id)
    await state.clear()
    await answer_to.answer("✅ Ro'yxatdan o'tdingiz. Bot ishga tushdi.", reply_markup=main_kb(user.id))


@dp.message(RegisterState.waiting_contact, F.contact)
async def register_contact(message: Message, state: FSMContext):
    if message.contact.user_id != message.from_user.id:
        await message.answer("Faqat o‘zingizning telefon raqamingizni yuboring.", reply_markup=contact_kb())
        return
    await state.update_data(phone=message.contact.phone_number)
    if await is_user_subscribed(message.from_user.id):
        await finish_registration(message.from_user, message.contact.phone_number, state, message)
        return
    await state.set_state(RegisterState.waiting_subscription)
    await message.answer("✅ Telefon raqamingiz saqlandi.\n\n" + subscription_prompt_text(),
                         reply_markup=subscribe_inline_kb())


@dp.callback_query(F.data == "check_sub_required")
async def check_sub_required(call: CallbackQuery, state: FSMContext):
    if not await is_user_subscribed(call.from_user.id):
        await call.answer("Avval kanalga obuna bo‘ling.", show_alert=True)
        return
    user = get_user(call.from_user.id)
    if user and user["phone"]:
        await state.clear()
        await call.message.answer("✅ Tekshirildi. Bot ishga tushdi.", reply_markup=main_kb(call.from_user.id))
        await call.answer()
        return
    phone = (await state.get_data()).get("phone")
    if not phone:
        await state.set_state(RegisterState.waiting_contact)
        await call.message.answer("Avval telefon raqamingizni yuboring.", reply_markup=contact_kb())
        await call.answer()
        return
    await finish_registration(call.from_user, phone, state, call.message)
    await call.answer("Tasdiqlandi")


@dp.message(RegisterState.waiting_contact)
async def need_contact(message: Message):
    await message.answer("Telefon raqamingizni tugma orqali yuboring.", reply_markup=contact_kb())


@dp.message(RegisterState.waiting_subscription)
async def waiting_subscription_message(message: Message):
    await message.answer(subscription_prompt_text(), reply_markup=subscribe_inline_kb())


# =========================================================
# TO'LOVLAR
# =========================================================
PAY_METHODS = {"pay_bank": "🏦 Bank karta", "pay_payme": "🅿️ Payme", "pay_click": "🔵 Click",
               "pay_paynet": "🟢 Paynet", "pay_visa": "💳 Visa", "pay_admin": "☎️ Admin"}


def get_payment_card(code: str) -> str:
    return {"pay_paynet": PAYNET_CARD, "pay_usd": USD_TRANSFER_CARD, "pay_visa": VISA_CARD}.get(code, DEFAULT_CARD)


@dp.callback_query(F.data == "acc_topup")
async def account_topup_callback(call: CallbackQuery):
    await call.message.answer("👇 To‘lov usulini tanlang:", reply_markup=payment_methods_kb())
    await call.answer()


@dp.callback_query(F.data.startswith("pay_"))
async def payment_method_handler(call: CallbackQuery, state: FSMContext):
    if call.data == "pay_admin":
        await call.message.answer(f"Admin: {SUPPORT_ADMIN}")
        await call.answer()
        return
    await state.clear()
    await state.set_state(DepositState.entering_amount)
    await state.update_data(payment_method=PAY_METHODS.get(call.data, "To'lov usuli"), payment_code=call.data)
    await call.message.answer(f"{PAY_METHODS.get(call.data, 'To‘lov usuli')}\n\n"
                              "To‘ldirish summasini kiriting.\nMasalan: 50000")
    await call.answer()


@dp.message(DepositState.entering_amount)
async def deposit_amount(message: Message, state: FSMContext):
    amount = parse_num(message.text)
    if not amount:
        await message.answer("Iltimos, to‘g‘ri summa kiriting.")
        return
    data = await state.get_data()
    card = get_payment_card(data.get("payment_code", "pay_bank"))
    await state.update_data(amount=amount, card_number=card)
    await state.set_state(DepositState.entering_proof)
    await message.answer(f"💰 Summa: {fmt(amount)} so‘m\n💳 Usul: {data.get('payment_method')}\n"
                         f"🏦 Karta: {card}\n👤 Egasi: {CARD_OWNER}\n\nTo‘lov qilgach chek yuboring.")


@dp.message(DepositState.entering_proof, F.content_type.in_({ContentType.TEXT, ContentType.PHOTO, ContentType.DOCUMENT}))
async def deposit_proof(message: Message, state: FSMContext):
    data = await state.get_data()
    amount = float(data["amount"])
    method = data.get("payment_method", "To'lov usuli")
    card = data.get("card_number", DEFAULT_CARD)
    tx_id = add_transaction(message.from_user.id, "deposit", amount, f"Usul: {method} | Karta: {card}", "pending")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"approve_deposit_{tx_id}"),
        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"reject_deposit_{tx_id}")]])
    caption = (f"📥 Yangi hisob to‘ldirish\n\n👤 {message.from_user.full_name}\n🪪 ID: {message.from_user.id}\n"
               f"🔗 @{message.from_user.username or 'username_yoq'}\n💳 Usul: {method}\n🏦 Karta: {card}\n"
               f"💰 Summa: {fmt(amount)} so‘m\n🧾 TX ID: {tx_id}")
    try:
        if message.photo:
            await bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=caption, reply_markup=kb)
        elif message.document:
            await bot.send_document(ADMIN_ID, message.document.file_id, caption=caption, reply_markup=kb)
        else:
            await bot.send_message(ADMIN_ID, caption + f"\n\n📄 Chek:\n{message.text or 'Matn yo‘q'}", reply_markup=kb)
        await state.clear()
        await message.answer("✅ Chekingiz adminga yuborildi.", reply_markup=main_kb(message.from_user.id))
    except Exception as e:
        logger.exception("Deposit proof send error: %s", e)
        await message.answer("❌ Chekni yuborishda xatolik bo‘ldi.")


async def _finish_deposit_msg(call: CallbackQuery, suffix: str):
    with suppress(TelegramBadRequest):
        if call.message.caption:
            await call.message.edit_caption(caption=call.message.caption + suffix)
        else:
            await call.message.edit_text((call.message.text or "") + suffix)


@dp.callback_query(F.data.startswith("approve_deposit_"), ADMIN)
async def approve_deposit(call: CallbackQuery):
    tx_id = int(call.data.split("_")[2])
    with db_conn() as conn:
        cur = conn.execute("UPDATE transactions SET status='approved' WHERE id=? AND status='pending' "
                           "AND type='deposit'", (tx_id,))
        if cur.rowcount == 0:
            await call.answer("Bu to‘lov allaqachon ko‘rib chiqilgan.", show_alert=True)
            return
        tx = conn.execute("SELECT user_id, amount FROM transactions WHERE id=?", (tx_id,)).fetchone()
        conn.execute("UPDATE users SET balance = balance + ?, total_deposit = total_deposit + ? WHERE user_id=?",
                     (tx["amount"], tx["amount"], tx["user_id"]))
    with suppress(Exception):
        await bot.send_message(tx["user_id"], f"✅ Hisob to‘ldirish tasdiqlandi.\n💰 +{fmt(tx['amount'])} so‘m tushirildi.")
    await _finish_deposit_msg(call, "\n\n✅ TASDIQLANDI")
    await call.answer("Tasdiqlandi")


@dp.callback_query(F.data.startswith("reject_deposit_"), ADMIN)
async def reject_deposit(call: CallbackQuery):
    tx_id = int(call.data.split("_")[2])
    with db_conn() as conn:
        cur = conn.execute("UPDATE transactions SET status='rejected' WHERE id=? AND status='pending'", (tx_id,))
        if cur.rowcount == 0:
            await call.answer("Bu to‘lov allaqachon ko‘rib chiqilgan.", show_alert=True)
            return
        uid = conn.execute("SELECT user_id FROM transactions WHERE id=?", (tx_id,)).fetchone()["user_id"]
    with suppress(Exception):
        await bot.send_message(uid, "❌ Hisob to‘ldirish rad etildi.\nIltimos, chekni tekshirib qayta yuboring.")
    await _finish_deposit_msg(call, "\n\n❌ RAD ETILDI")
    await call.answer("Rad etildi")


# =========================================================
# MUROJAAT / PROMO
# =========================================================
@dp.message(SupportState.waiting_text)
async def support_text_received(message: Message, state: FSMContext):
    try:
        await bot.send_message(ADMIN_ID, f"📞 Yangi murojaat\n\n👤 {message.from_user.full_name}\n"
                                         f"🪪 ID: {message.from_user.id}\n🔗 @{message.from_user.username or 'username_yoq'}\n\n"
                                         f"📄 Matn:\n{message.text or '(matnsiz xabar)'}")
        await state.clear()
        await message.answer("✅ Murojaatingiz yuborildi.", reply_markup=main_kb(message.from_user.id))
    except Exception as e:
        logger.exception("Support send error: %s", e)
        await message.answer("❌ Murojaat yuborishda xatolik bo‘ldi.")


@dp.callback_query(F.data == "acc_promo")
async def account_promo_callback(call: CallbackQuery, state: FSMContext):
    await state.set_state(PromoState.waiting_code)
    await call.answer()
    await call.message.answer("🎟 Promo-kodni kiriting:")


@dp.message(PromoState.waiting_code)
async def promo_code_received(message: Message, state: FSMContext):
    ok, msg, amount = apply_promo_code_for_user(message.from_user.id, message.text or "")
    await state.clear()
    text = f"✅ {msg}\n💰 +{fmt(amount)} so‘m balansingizga qo‘shildi." if ok else f"❌ {msg}"
    await message.answer(text, reply_markup=main_kb(message.from_user.id))


# =========================================================
# NOMER OLISH (SMS-MAN)
# =========================================================
@dp.callback_query(F.data.startswith("nm_p_"))
async def nm_page(call: CallbackQuery):
    page = int(call.data.split("_")[-1]) if call.data.split("_")[-1].isdigit() else 1
    await show_numbers_menu(call.message, page, edit=True)
    await call.answer()


@dp.callback_query(F.data.startswith("nm_r_"))
async def nm_refresh(call: CallbackQuery):
    page = int(call.data.split("_")[-1]) if call.data.split("_")[-1].isdigit() else 1
    await get_number_offers(force=True)
    await show_numbers_menu(call.message, page, edit=True)
    await call.answer("Yangilandi")


@dp.callback_query(F.data.startswith("nm_o_"))
async def nm_open(call: CallbackQuery):
    cid = call.data.split("_", 2)[2]
    offer = find_offer(await get_number_offers(), cid)
    if not offer:
        await call.answer("Bu davlatda hozir nomer yo‘q.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Sotib olish", callback_data=f"nm_b_{cid}")],
        [InlineKeyboardButton(text="⬅️ Ortga", callback_data="nm_p_1")]])
    await safe_edit_text(call.message,
                         f"🌍 {offer['name']}\n\n💰 Narx: {fmt(offer['price'])} so‘m\n📦 Xizmat: Telegram\n"
                         f"📊 Mavjud: {offer['count']} ta\n\n"
                         f"ℹ️ SMS {NUMBER_TTL // 60} daqiqa ichida kelmasa, pul avtomatik qaytariladi.\n"
                         "👇 Sotib olish uchun tugmani bosing.", kb)
    await call.answer()


@dp.callback_query(F.data.startswith("nm_b_"))
async def nm_buy(call: CallbackQuery):
    cid = call.data.split("_", 2)[2]
    uid = call.from_user.id
    user = get_user(uid)
    if user and int(user["is_frozen"] or 0):
        await call.answer("Hisobingiz muzlatilgan", show_alert=True)
        return
    offer = find_offer(await get_number_offers(), cid)
    if not offer:
        await call.answer("Bu davlatda hozir nomer yo‘q.", show_alert=True)
        return
    price = float(offer["price"])
    if not try_debit(uid, price):
        await call.answer(f"Balans yetarli emas. Kerak: {fmt(price)} so‘m", show_alert=True)
        return

    await safe_edit_text(call.message, "⏳ Nomer olinmoqda...", None)
    back = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Davlatlar", callback_data="nm_p_1")]])
    try:
        data = await sm_get("get-number", country_id=cid, application_id=await sm_app_id())
        err = sm_error(data)
        rid = data.get("request_id") if isinstance(data, dict) else None
        number = str(data.get("number") or "") if isinstance(data, dict) else ""
        if err or not rid or not number:
            raise RuntimeError(err or f"Noto'g'ri javob: {str(data)[:150]}")
    except Exception as e:
        update_balance(uid, price)  # pul qaytariladi
        await get_number_offers(force=True)
        logger.warning("Nomer olinmadi (country=%s): %s", cid, e)
        await safe_edit_text(call.message, f"❌ Nomer olib bo‘lmadi.\n{pretty_sm_error(str(e))}\n\n💰 Pulingiz qaytarildi.", back)
        await call.answer()
        return

    if not number.startswith("+"):
        number = "+" + number
    cost_uzs = math.ceil(offer["cost_usd"] * cost_unit_uzs())
    with db_conn() as conn:
        cur = conn.execute("""INSERT INTO numbers (user_id, request_id, country_id, country, number, price,
            provider_cost, status, created_at) VALUES (?,?,?,?,?,?,?,'waiting',?)""",
                           (uid, str(rid), cid, offer["name"], number, price, cost_uzs, now_iso()))
        num_id = cur.lastrowid
    add_transaction(uid, "sms_number_buy", price, f"SMS-Man | {offer['name']} | RID:{rid} | {number}", "approved")
    await send_log(ORDER_LOG_CHAT_ID, f"📲 Yangi nomer\n\n👤 {call.from_user.full_name} ({uid})\n🌍 {offer['name']}\n"
                                      f"💰 Sotuv: {fmt(price)} | Tannarx: {fmt(cost_uzs)} | Foyda: {fmt(price - cost_uzs)}")
    await safe_edit_text(
        call.message,
        f"✅ Nomer muvaffaqiyatli olindi\n\n🌍 {offer['name']}\n📱 Nomer: {number}\n💰 Narx: {fmt(price)} so‘m\n\n"
        f"📩 Telegramda shu raqamga kod yuboring. SMS kelishi bilan kod avtomatik shu yerga yuboriladi.\n"
        f"⏳ {NUMBER_TTL // 60} daqiqada kelmasa, pul qaytariladi.\n\n{WARNING_TEXT}",
        number_kb(num_id))
    await call.answer()


@dp.callback_query(F.data.startswith("nm_s_"))
async def nm_check_sms(call: CallbackQuery):
    num_id = int(call.data.split("_")[2])
    row = get_number_row(num_id)
    if not row or int(row["user_id"]) != call.from_user.id:
        await call.answer("Nomer topilmadi.", show_alert=True)
        return
    if row["status"] == "done":
        await call.answer(f"Kod: {row['sms_code']}", show_alert=True)
        return
    if row["status"] != "waiting":
        await call.answer("Bu nomer bekor qilingan.", show_alert=True)
        return
    code = parse_sms_code(await sm_get("get-sms", request_id=row["request_id"]))
    if code:
        if mark_number_done(num_id, code):
            with suppress(Exception):
                await sm_get("set-status", request_id=row["request_id"], status="close")
        await safe_edit_text(call.message, f"✅ SMS kodi keldi\n\n🌍 {row['country']}\n📱 Nomer: {row['number']}\n"
                                           f"🔐 Kod: {code}", number_kb(num_id, waiting=False))
        await call.answer("SMS topildi")
        return
    await call.answer("⏳ SMS hali kelmadi. Telegramda kod so‘rang va biroz kuting.", show_alert=True)


@dp.callback_query(F.data.startswith("nm_c_"))
async def nm_cancel(call: CallbackQuery):
    num_id = int(call.data.split("_")[2])
    row = get_number_row(num_id)
    if not row or int(row["user_id"]) != call.from_user.id or row["status"] != "waiting":
        await call.answer("Bekor qilib bo‘lmaydi.", show_alert=True)
        return
    if parse_sms_code(await sm_get("get-sms", request_id=row["request_id"])):
        await call.answer("SMS allaqachon kelgan, bekor qilib bo‘lmaydi.", show_alert=True)
        return
    data = await sm_get("set-status", request_id=row["request_id"], status="reject")
    err = sm_error(data)
    if err:
        await call.answer(f"Hozir bekor qilib bo‘lmadi: {pretty_sm_error(err)}\nBirozdan keyin urinib ko‘ring.",
                          show_alert=True)
        return
    ok, amount, _ = refund_number(num_id)
    if ok:
        await safe_edit_text(call.message, f"❌ Nomer bekor qilindi.\n💰 {fmt(amount)} so‘m balansingizga qaytarildi.",
                             number_kb(num_id, waiting=False))
    await call.answer()


# =========================================================
# XIZMATLAR MENYUSI
# =========================================================
@dp.callback_query(F.data == "back_services")
async def back_services(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit_text(call.message, "✅ Kerakli bo‘limni tanlang:", services_inline_kb())
    await call.answer()


@dp.callback_query(F.data.startswith("menu_platform_"))
async def menu_platform(call: CallbackQuery, state: FSMContext):
    await state.clear()
    pk = call.data.replace("menu_platform_", "")
    if pk not in SERVICE_CATALOG:
        await call.answer("Bo‘lim topilmadi.")
        return
    await safe_edit_text(call.message, f"{SERVICE_CATALOG[pk]['title']} - 👇 Kerakli bo‘limni tanlang:",
                         build_platform_categories_kb(pk))
    await call.answer()


@dp.callback_query(F.data.startswith("menu_cat_"))
async def menu_category(call: CallbackQuery, state: FSMContext):
    await state.clear()
    _, _, pk, ck = call.data.split("_", 3)
    cats = SERVICE_CATALOG.get(pk, {}).get("categories", {})
    if ck not in cats:
        await call.answer("Kategoriya topilmadi.")
        return
    await safe_edit_text(call.message, f"🔔 Bo‘lim: {cats[ck]['title']}\n💰 Narxlar 1000 tasi uchun keltirilgan!\n"
                                       f"👇 Kerakli xizmatni tanlang:", build_category_services_kb(pk, ck))
    await call.answer()


# =========================================================
# BUYURTMA
# =========================================================
@dp.callback_query(F.data.startswith("srv_"))
async def service_select(call: CallbackQuery, state: FSMContext):
    user = get_user(call.from_user.id)
    if user and int(user["is_frozen"] or 0):
        await call.answer("Hisobingiz muzlatilgan", show_alert=True)
        return
    code = call.data[4:]
    svc = SERVICE_LOOKUP.get(code)
    if not svc or not svc["ok"]:
        await call.answer("Xizmat hozircha mavjud emas.", show_alert=True)
        return
    price = get_effective_price(code)
    await state.clear()
    await state.set_state(ServiceOrderState.entering_qty)
    await state.update_data(service_code=code, service_title=sanitize_display_text(svc["name"]), calc_price=price)
    max_line = f"\n📦 Max buyurtma: {fmt(svc['max'])} {svc['unit']}" if svc["max"] else ""
    await call.message.answer(
        f"✨ {sanitize_display_text(svc['name'])}\n\n💵 Narx: {fmt(price)} so‘m / 1000 {svc['unit']}\n"
        f"📦 Min buyurtma: {fmt(svc['min'])} {svc['unit']}{max_line}\n⚙️ Tezlik: {speed_text(svc)}\n"
        f"📝 {svc['platform']} / {sanitize_display_text(svc['category'])}\n\n👇 Kerakli miqdorni kiriting:")
    await call.answer()


@dp.message(ServiceOrderState.entering_qty)
async def service_qty(message: Message, state: FSMContext):
    qty = parse_num(message.text)
    if not qty:
        await message.answer("Iltimos, to‘g‘ri miqdor kiriting.")
        return
    data = await state.get_data()
    svc = SERVICE_LOOKUP[data["service_code"]]
    qty = int(qty)
    if qty < svc["min"]:
        await message.answer(f"❌ Minimal buyurtma: {fmt(svc['min'])} {svc['unit']}")
        return
    if svc["max"] and qty > svc["max"]:
        await message.answer(f"❌ Maksimal buyurtma: {fmt(svc['max'])} {svc['unit']}")
        return
    total = calc_total(float(data["calc_price"]), qty)
    await state.update_data(qty=qty, total=total)
    await state.set_state(ServiceOrderState.entering_link)
    await message.answer(f"📦 Miqdor: {fmt(qty)} {svc['unit']}\n💰 Jami: {fmt(total)} so‘m\n\n🔗 Link / username yuboring:")


def normalize_link(code: str, link: str) -> str:
    link = link.strip()
    if code.startswith("tg_") and link.startswith("@"):
        return "https://t.me/" + link[1:]
    return link


@dp.message(ServiceOrderState.entering_link)
async def service_link(message: Message, state: FSMContext):
    link_text = (message.text or "").strip()
    if not link_text:
        await message.answer("Iltimos, link yoki username yuboring.")
        return
    data = await state.get_data()
    svc = SERVICE_LOOKUP[data["service_code"]]
    link_text = normalize_link(svc["code"], link_text)
    total, qty = float(data["total"]), int(data["qty"])
    balance = get_balance(message.from_user.id)
    user = get_user(message.from_user.id)
    if user and int(user["is_frozen"] or 0):
        await state.clear()
        await message.answer("❄️ Hisobingiz muzlatilgan. Admin bilan bog‘laning.", reply_markup=main_kb(message.from_user.id))
        return
    if balance < total:
        await state.clear()
        await message.answer(f"❌ Balans yetarli emas.\nKerak: {fmt(total)} so‘m\nMavjud: {fmt(balance)} so‘m",
                             reply_markup=main_kb(message.from_user.id))
        return
    await state.update_data(link_text=link_text)
    await state.set_state(ServiceOrderState.confirming)
    await message.answer(
        "🧾 Buyurtmani tasdiqlang:\n\n"
        f"📦 Xizmat: {sanitize_display_text(data['service_title'])}\n⚙️ Tezlik: {speed_text(svc)}\n"
        f"🔢 Miqdor: {fmt(qty)} {svc['unit']}\n🔗 Link: {link_text}\n💰 Jami: {fmt(total)} so‘m\n\n"
        f"💳 Balans: {fmt(balance)} so‘m\n\nTasdiqlasangiz buyurtma servisga yuboriladi va balansdan pul yechiladi.",
        reply_markup=build_order_confirm_kb(), link_preview_options=LinkPreviewOptions(is_disabled=True))


@dp.callback_query(F.data == "confirm_order_no")
async def confirm_order_no(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit_text(call.message, "❌ Buyurtma bekor qilindi.", None)
    await call.answer()


@dp.callback_query(F.data == "confirm_order_yes")
async def confirm_order_yes(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data or data.get("service_code") is None or data.get("link_text") is None:
        await state.clear()
        await call.answer("Buyurtma ma'lumotlari topilmadi.", show_alert=True)
        return
    # ikki marta bosishdan himoya: state'ni darrov tozalaymiz
    await state.clear()
    uid = call.from_user.id
    total, qty = float(data["total"]), int(data["qty"])
    svc = SERVICE_LOOKUP[data["service_code"]]
    link_text = data["link_text"]

    provider_total = provider_cost_uzs(svc["rate"], qty)
    if total < provider_total * (1 + MIN_PROFIT_PERCENT / 100.0):
        await safe_edit_text(call.message, "❌ Bu xizmat narxi hozir yangilanmoqda. Iltimos, xizmatni qaytadan tanlang.", None)
        await call.answer()
        return
    if not try_debit(uid, total):
        await safe_edit_text(call.message, f"❌ Balans yetarli emas.\nKerak: {fmt(total)} so‘m\nMavjud: {fmt(get_balance(uid))} so‘m", None)
        await call.answer()
        return
    try:
        provider_order_id = await create_provider_order(svc["provider_id"], link_text, qty)
    except Exception as e:
        update_balance(uid, total)  # pul qaytariladi
        logger.exception("Provider order xato: %s", e)
        await safe_edit_text(call.message, "❌ Buyurtma yaratilmadi (servis vaqtincha ishlamayapti yoki link noto‘g‘ri).\n"
                                           "💰 Pulingiz balansga qaytarildi.", None)
        await bot.send_message(uid, "Asosiy menyu 👇", reply_markup=main_kb(uid))
        await call.answer()
        return

    title = sanitize_display_text(data["service_title"])
    order_id = add_order(uid, "service", title, qty, total, link_text, "processing", provider_order_id, provider_total)
    await send_log(ORDER_LOG_CHAT_ID,
                   f"🛍 Yangi buyurtma\n\n👤 {call.from_user.full_name}\n🔗 @{call.from_user.username or 'username_yoq'}\n"
                   f"🪪 ID: {uid}\n📦 Xizmat: {title}\n🔢 Miqdor: {fmt(qty)}\n💰 Sotuv: {fmt(total)} so‘m\n"
                   f"💸 Tannarx: {fmt(provider_total)} so‘m\n📈 Foyda: {fmt(total - provider_total)} so‘m\n"
                   f"🧩 Provider service: {svc['provider_id']}\n🧾 Provider order: {provider_order_id}\n"
                   f"🔗 Link: {link_text}\n🧾 Local order ID: {order_id}")
    await safe_edit_text(call.message, f"✅ Buyurtma servisga yuborildi.\n🪪 Buyurtma ID: {order_id}\n"
                                       f"🧾 Servis order ID: {provider_order_id}\n💰 Balansdan yechildi: {fmt(total)} so‘m", None)
    await bot.send_message(uid, "Asosiy menyu 👇", reply_markup=main_kb(uid))
    await call.answer()


@dp.callback_query(F.data.startswith("orders_page_"))
async def orders_page_callback(call: CallbackQuery):
    tail = call.data.split("_")[-1]
    text_out, kb = render_orders_page(get_last_orders(call.from_user.id), int(tail) if tail.isdigit() else 1)
    await safe_edit_text(call.message, text_out, kb)
    await call.answer()


# =========================================================
# ADMIN
# =========================================================
@dp.callback_query(F.data == "admin_home", ADMIN)
async def admin_home(call: CallbackQuery):
    await safe_edit_text(call.message, "🎛 Admin panel", admin_panel_kb())
    await call.answer()


@dp.callback_query(F.data == "admin_users", ADMIN)
async def admin_users(call: CallbackQuery):
    await safe_edit_text(call.message, "👥 Foydalanuvchilar bo‘limi", admin_users_kb())
    await call.answer()


@dp.callback_query(F.data == "admin_promos", ADMIN)
async def admin_promos(call: CallbackQuery):
    await safe_edit_text(call.message, "🎟 Promo kod bo‘limi", admin_promos_kb())
    await call.answer()


@dp.callback_query(F.data == "admin_user_activity_stats", ADMIN)
async def admin_user_activity_stats(call: CallbackQuery):
    s = get_user_activity_stats(7)
    await safe_edit_text(call.message, "📊 Foydalanuvchilar faolligi\n\n"
                                       f"👥 Jami: {fmt(s['total_users'])}\n"
                                       f"✅ Aktiv (oxirgi {s['active_days']} kun): {fmt(s['active_users'])}\n"
                                       f"❌ Noaktiv: {fmt(s['inactive_users'])}",
                         InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_users")]]))
    await call.answer()


@dp.callback_query(F.data == "admin_transactions", ADMIN)
async def admin_transactions(call: CallbackQuery):
    s = get_stats_summary()
    await safe_edit_text(call.message, "💸 Tranzaksiyalar\n\n"
                                       f"👥 Jami foydalanuvchilar: {fmt(s['total_users'])}\n"
                                       f"💰 Kiritilgan summa: {fmt(s['total_deposit'])} so‘m\n"
                                       f"💸 Tannarx (xizmat + nomer): {fmt(s['provider_spent'])} so‘m\n"
                                       f"🛒 Sotuv (xizmat + nomer): {fmt(s['order_sales'])} so‘m\n"
                                       f"📈 Foyda: {fmt(s['profit'])} so‘m",
                         InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_home")]]))
    await call.answer()


# Bitta ID so'raydigan amallar
ID_ACTIONS = {
    "admin_user_block": (AdminState.waiting_user_block, "⛔ Bloklash uchun foydalanuvchi ID sini kiriting:",
                         lambda u: set_user_flag(u, "is_blocked", True), "✅ Foydalanuvchi bloklandi.", "⛔ Siz admin tomonidan bloklandingiz."),
    "admin_user_unblock": (AdminState.waiting_user_unblock, "✅ Blokdan chiqarish uchun ID kiriting:",
                           lambda u: set_user_flag(u, "is_blocked", False), "✅ Blokdan chiqarildi.", "✅ Siz blokdan chiqarildingiz."),
    "admin_user_freeze": (AdminState.waiting_user_freeze, "🧊 Muzlatish uchun ID kiriting:",
                          lambda u: set_user_flag(u, "is_frozen", True), "✅ Foydalanuvchi muzlatildi.", "🧊 Hisobingiz admin tomonidan muzlatildi."),
    "admin_user_unfreeze": (AdminState.waiting_user_unfreeze, "🔥 Muzdan chiqarish uchun ID kiriting:",
                            lambda u: set_user_flag(u, "is_frozen", False), "✅ Muzdan chiqarildi.", "🔥 Hisobingiz muzdan chiqarildi."),
}
ID_STATE_MAP = {v[0].state: v for v in ID_ACTIONS.values()}


@dp.callback_query(F.data.in_(set(ID_ACTIONS)), ADMIN)
async def admin_id_action_start(call: CallbackQuery, state: FSMContext):
    st, prompt, *_ = ID_ACTIONS[call.data]
    await state.set_state(st)
    await call.answer()
    await call.message.answer(prompt)


@dp.message(ADMIN, StateFilter(*[v[0] for v in ID_ACTIONS.values()]))
async def admin_id_action_done(message: Message, state: FSMContext):
    _, _, action, ok_text, notify = ID_STATE_MAP[await state.get_state()]
    try:
        uid = int((message.text or "").strip())
    except Exception:
        await message.answer("ID noto‘g‘ri.")
        return
    if not get_user(uid):
        await message.answer("Foydalanuvchi topilmadi.")
        return
    action(uid)
    await state.clear()
    with suppress(Exception):
        await bot.send_message(uid, notify)
    await message.answer(ok_text, reply_markup=main_kb(message.from_user.id))


@dp.callback_query(F.data.in_({"admin_balance_add", "admin_balance_remove", "admin_msg_one"}), ADMIN)
async def admin_user_prompt(call: CallbackQuery, state: FSMContext):
    mapping = {"admin_balance_add": (AdminState.waiting_balance_add_user, "➕ Hisob to‘ldirish uchun ID kiriting:"),
               "admin_balance_remove": (AdminState.waiting_balance_remove_user, "➖ Hisobdan yechish uchun ID kiriting:"),
               "admin_msg_one": (AdminState.waiting_broadcast_user, "✉️ Xabar yuborish uchun ID kiriting:")}
    st, prompt = mapping[call.data]
    await state.set_state(st)
    await call.answer()
    await call.message.answer(prompt)


@dp.callback_query(F.data == "admin_msg_all", ADMIN)
async def admin_msg_all_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_broadcast_all_text)
    await call.answer()
    await call.message.answer("📢 Barcha foydalanuvchilarga yuboriladigan xabarni kiriting:")


@dp.callback_query(F.data.in_({"admin_promo_new", "admin_promo_delete"}), ADMIN)
async def admin_promo_start(call: CallbackQuery, state: FSMContext):
    if call.data == "admin_promo_new":
        await state.set_state(AdminState.waiting_new_promo_code)
        text = "🎟 Yangi promo-kodni kiriting:"
    else:
        await state.set_state(AdminState.waiting_delete_promo_code)
        text = "🗑 O‘chiriladigan promo-kodni kiriting:"
    await call.answer()
    await call.message.answer(text)


async def _admin_target(message: Message, state: FSMContext, next_state, prompt: str, show_balance=False):
    try:
        uid = int((message.text or "").strip())
    except Exception:
        await message.answer("ID noto‘g‘ri.")
        return
    user = get_user(uid)
    if not user:
        await message.answer("Foydalanuvchi topilmadi.")
        return
    await state.update_data(target_user_id=uid)
    await state.set_state(next_state)
    await message.answer(prompt + (f"\nJoriy balans: {fmt(user['balance'])} so‘m" if show_balance else ""))


@dp.message(AdminState.waiting_balance_add_user, ADMIN)
async def admin_balance_add_user(message: Message, state: FSMContext):
    await _admin_target(message, state, AdminState.waiting_balance_add_amount, "➕ Qancha summa qo‘shilsin? (so‘m)")


@dp.message(AdminState.waiting_balance_remove_user, ADMIN)
async def admin_balance_remove_user(message: Message, state: FSMContext):
    await _admin_target(message, state, AdminState.waiting_balance_remove_amount, "➖ Qancha summa yechilsin?", True)


@dp.message(AdminState.waiting_broadcast_user, ADMIN)
async def admin_broadcast_user(message: Message, state: FSMContext):
    await _admin_target(message, state, AdminState.waiting_broadcast_text, "✉️ Yuboriladigan xabarni kiriting:")


@dp.message(AdminState.waiting_balance_add_amount, ADMIN)
async def admin_balance_add_amount(message: Message, state: FSMContext):
    amount = parse_num(message.text)
    if not amount:
        await message.answer("Summa noto‘g‘ri.")
        return
    uid = int((await state.get_data())["target_user_id"])
    update_balance(uid, amount)
    add_transaction(uid, "admin_balance_add", amount, "Admin tomonidan hisob to‘ldirildi", "approved")
    await state.clear()
    with suppress(Exception):
        await bot.send_message(uid, f"✅ Admin tomonidan +{fmt(amount)} so‘m balansingizga qo‘shildi.")
    await message.answer("✅ Hisob to‘ldirildi.", reply_markup=main_kb(message.from_user.id))


@dp.message(AdminState.waiting_balance_remove_amount, ADMIN)
async def admin_balance_remove_amount(message: Message, state: FSMContext):
    amount = parse_num(message.text)
    if not amount:
        await message.answer("Summa noto‘g‘ri.")
        return
    uid = int((await state.get_data())["target_user_id"])
    if not try_debit(uid, amount):
        await message.answer("Balans yetarli emas.")
        return
    add_transaction(uid, "admin_balance_remove", amount, "Admin tomonidan hisobdan yechildi", "approved")
    await state.clear()
    with suppress(Exception):
        await bot.send_message(uid, f"➖ Admin tomonidan {fmt(amount)} so‘m balansingizdan yechildi.")
    await message.answer("✅ Hisobdan summa yechildi.", reply_markup=main_kb(message.from_user.id))


@dp.message(AdminState.waiting_broadcast_text, ADMIN)
async def admin_broadcast_text(message: Message, state: FSMContext):
    uid = int((await state.get_data())["target_user_id"])
    try:
        await bot.send_message(uid, f"📩 Admin xabari\n\n{message.text}")
        await state.clear()
        await message.answer("✅ Xabar yuborildi.", reply_markup=main_kb(message.from_user.id))
    except Exception as e:
        await message.answer(f"❌ Xabar yuborilmadi: {e}")


@dp.message(AdminState.waiting_broadcast_all_text, ADMIN)
async def admin_broadcast_all(message: Message, state: FSMContext):
    text = f"📢 Admin xabari\n\n{message.text}"
    await state.clear()
    sent = 0
    for uid in get_all_user_ids():
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.04)
    await message.answer(f"✅ Xabar yuborildi: {sent} ta foydalanuvchi.", reply_markup=main_kb(message.from_user.id))


@dp.message(AdminState.waiting_new_promo_code, ADMIN)
async def admin_new_promo_code(message: Message, state: FSMContext):
    code = normalize_promo_code(message.text)
    if not code:
        await message.answer("Promo-kod bo‘sh bo‘lmasin.")
        return
    await state.update_data(new_promo_code=code)
    await state.set_state(AdminState.waiting_new_promo_amount)
    await message.answer("💰 Necha so‘m promo-kod uchun berilsin?")


@dp.message(AdminState.waiting_new_promo_amount, ADMIN)
async def admin_new_promo_amount(message: Message, state: FSMContext):
    amount = parse_num(message.text)
    if not amount:
        await message.answer("Summa noto‘g‘ri.")
        return
    await state.update_data(new_promo_amount=amount)
    await state.set_state(AdminState.waiting_new_promo_days)
    await message.answer("⏳ Promo-kodga necha kun muddat qo‘yilsin?\nCheksiz bo‘lsa 0 deb yozing.")


@dp.message(AdminState.waiting_new_promo_days, ADMIN)
async def admin_new_promo_days(message: Message, state: FSMContext):
    try:
        days = int((message.text or "").strip())
        if days < 0:
            raise ValueError
    except Exception:
        await message.answer("Kun soni noto‘g‘ri.")
        return
    await state.update_data(new_promo_days=days)
    await state.set_state(AdminState.waiting_new_promo_max_uses)
    await message.answer("👥 Promo-kodni nechta odam ishlata olsin?\nCheksiz bo‘lsa 0 deb yozing.")


@dp.message(AdminState.waiting_new_promo_max_uses, ADMIN)
async def admin_new_promo_max_uses(message: Message, state: FSMContext):
    try:
        max_uses = int((message.text or "").strip())
        if max_uses < 0:
            raise ValueError
    except Exception:
        await message.answer("Limit noto‘g‘ri.")
        return
    d = await state.get_data()
    days = int(d["new_promo_days"])
    expires = None if days == 0 else (datetime.now() + timedelta(days=days)).isoformat()
    create_promo_code(d["new_promo_code"], float(d["new_promo_amount"]), max_uses, expires)
    await state.clear()
    await message.answer(f"✅ Promo-kod saqlandi.\n\n🎟 Kod: {d['new_promo_code']}\n💰 Summa: {fmt(d['new_promo_amount'])} so‘m\n"
                         f"⏳ Muddat: {'cheksiz' if days == 0 else f'{days} kun'}\n"
                         f"👥 Limit: {'cheksiz' if max_uses == 0 else max_uses}", reply_markup=main_kb(message.from_user.id))


@dp.message(AdminState.waiting_delete_promo_code, ADMIN)
async def admin_delete_promo_code(message: Message, state: FSMContext):
    code = normalize_promo_code(message.text)
    deleted = bool(code) and delete_promo_code(code)
    await state.clear()
    await message.answer(f"✅ Promo-kod o‘chirildi: {code}" if deleted else "❌ Promo-kod topilmadi.",
                         reply_markup=main_kb(message.from_user.id))


# ---- admin buyruqlari ----
@dp.message(Command("setprice"), ADMIN)
async def set_price_cmd(message: Message):
    parts = (message.text or "").split()
    if len(parts) != 3 or parts[1] not in SERVICE_LOOKUP or not parse_num(parts[2]):
        await message.answer("Foydalanish: /setprice service_code narx\nMasalan: /setprice tg_1111 9000\n"
                             "Kodlarni /prices dan ko‘ring. Qo‘lda narxni o‘chirish: /delprice kod")
        return
    set_manual_price(parts[1], parse_num(parts[2]))
    await message.answer(f"✅ {parts[1]} narxi {fmt(parts[2])} so‘m ga o‘rnatildi (qo‘lda).")


@dp.message(Command("delprice"), ADMIN)
async def del_price_cmd(message: Message):
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Foydalanish: /delprice service_code")
        return
    set_manual_price(parts[1], 0)
    await message.answer(f"✅ {parts[1]} avtomatik narxga qaytdi.")


@dp.message(Command("prices"), ADMIN)
async def prices_cmd(message: Message):
    lines = [f"📋 USD kursi: {fmt(USD_UZS_RATE)} so‘m\n"]
    for code, s in SERVICE_LOOKUP.items():
        price = get_effective_price(code)
        cost = s["rate"] * USD_UZS_RATE
        pct = (price / cost - 1) * 100 if cost else 0
        mark = "" if s["ok"] else " ⚠️yo‘q"
        lines.append(f"{code} | {s['name']} | {fmt(price)} (tannarx {fmt(cost)}, +{pct:.0f}%){mark}")
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500])


@dp.message(Command("syncprices"), ADMIN)
async def sync_prices_cmd(message: Message):
    try:
        await refresh_usd_rate()
        total, missing = await sync_with_provider(force=True)
        text = f"✅ Provider: {total} ta xizmat. Kurs: {fmt(USD_UZS_RATE)}"
        if missing:
            text += f"\n⚠️ Providerda topilmadi (yashirildi): {', '.join(missing)}"
        await message.answer(text)
    except Exception as e:
        await message.answer(f"❌ Yangilanmadi: {e}")


@dp.message(Command("provider_refresh"), ADMIN)
async def provider_refresh_cmd(message: Message):
    await sync_prices_cmd(message)


@dp.message(Command("balances"), ADMIN)
async def balances_cmd(message: Message):
    smm = await smm_api({"action": "balance"}) if SMM_API_KEY else {}
    sm = await sm_get("get-balance") if SMSMAN_API_KEY else {}
    await message.answer(f"💼 SMM provider: {smm}\n📲 SMS-Man: {sm}")


@dp.message(Command("smsman_debug"), ADMIN)
async def smsman_debug_cmd(message: Message):
    """SMS-Man nima qaytarayotganini ko'rish uchun (nomer chiqmasa shuni yuboring)."""
    app = await sm_app_id()
    lim = await sm_get("get-prices")
    bal = await sm_get("get-balance")
    offers = await get_number_offers(force=True)
    await message.answer(f"app_id={app}\nbalance={str(bal)[:200]}\nprices={str(lim)[:700]}\n"
                         f"topilgan davlatlar: {len(offers)}\nvalyuta: {_detected_unit['cur']} (1 birlik = {fmt(cost_unit_uzs())} so'm)\n"
                         + "\n".join(f"{o['name']}: narx {o['cost_usd']} -> {fmt(o['price'])} so'm" for o in offers[:8]))


# =========================================================
# BACKGROUND
# =========================================================
async def monitor_orders_loop():
    await asyncio.sleep(8)
    while True:
        try:
            rows = get_processing_orders(100)
            for i in range(0, len(rows), 50):
                chunk = rows[i:i + 50]
                result = await smm_api({"action": "orders", "orders": ",".join(str(r["order_api_id"]) for r in chunk)})
                if not isinstance(result, dict):
                    continue
                for row in chunk:
                    st = result.get(str(row["order_api_id"]))
                    if not isinstance(st, dict) or st.get("error"):
                        continue
                    raw = str(st.get("status") or "")
                    normalized = normalize_provider_status(raw)
                    is_partial = raw.strip().lower() == "partial"
                    note = ""
                    if is_partial:
                        remains = _f(st.get("remains"))
                        part = math.ceil(float(row["amount"]) * remains / max(float(row["qty"]), 1)) if remains > 0 else 0
                        ok, amount, _, _ = refund_order(int(row["id"]), part, "completed")
                        normalized = "completed"
                        if ok and amount > 0:
                            note = f"\n💸 Bajarilmagan qism qaytarildi: {fmt(amount)} so‘m"
                    elif normalized == "canceled":
                        ok, amount, _, _ = refund_order(int(row["id"]))
                        if ok and amount > 0:
                            note = f"\n💸 Qaytarildi: {fmt(amount)} so‘m"
                    elif normalized == (row["status"] or "").lower():
                        continue
                    else:
                        update_order_status(int(row["id"]), normalized)
                    if normalized in {"completed", "canceled"}:
                        label = "✅ Tugallangan" if normalized == "completed" else "❌ Bekor qilingan"
                        with suppress(Exception):
                            await bot.send_message(
                                int(row["user_id"]),
                                f"📢 Buyurtma holati yangilandi\n\n🪪 ID: {row['id']}\n📦 Xizmat: {row['item']}\n"
                                f"📄 Holat: {label}\n🔢 Miqdor: {fmt(row['qty'])}\n💰 Summa: {fmt(row['amount'])} so‘m"
                                f"{note}\n🔗 Link: {row['link']}",
                                link_preview_options=LinkPreviewOptions(is_disabled=True))
        except Exception as e:
            logger.exception("Order monitor error: %s", e)
        await asyncio.sleep(30)


async def offers_loop():
    """Davlatlar va narxlarni fonda yangilab turadi - foydalanuvchi API kutmaydi."""
    while True:
        with suppress(Exception):
            await get_number_offers(force=True)
        await asyncio.sleep(60)


async def sync_loop():
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        try:
            await refresh_usd_rate()
            await sync_with_provider(force=True)
        except Exception as e:
            logger.warning("Davriy sinxron xato: %s", e)


# =========================================================
# FALLBACK
# =========================================================
@dp.message()
async def fallback(message: Message, state: FSMContext):
    if await state.get_state():
        return
    if (message.text or "").strip().startswith(("http://", "https://", "@", "t.me/")):
        return
    await message.answer("Kerakli bo‘limni menyudan tanlang.", reply_markup=main_kb(message.from_user.id))


# =========================================================
# RUN
# =========================================================
async def main():
    global BOT_USERNAME
    init_db()
    load_manual_prices()
    await refresh_usd_rate()
    try:
        await sync_with_provider(force=True)
    except Exception as e:
        logger.warning("Boshlang'ich provider sinxron bo'lmadi (katalog qiymatlari ishlatiladi): %s", e)
    BOT_USERNAME = (await bot.get_me()).username or ""
    await bot.delete_webhook(drop_pending_updates=True)
    tasks = [asyncio.create_task(monitor_orders_loop()), asyncio.create_task(numbers_loop()),
             asyncio.create_task(sync_loop()), asyncio.create_task(offers_loop())]
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        if _http and not _http.closed:
            await _http.close()


if __name__ == "__main__":
    asyncio.run(main())
