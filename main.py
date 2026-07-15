"""
Taxi Bot — Main entry point
Production-ready Telegram bot for taxi orders in groups.
"""

import asyncio
import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    filters,
)

from database import Database
from ai_parser import LocationsManager, AIParser, VoiceTranscriber, LOCATIONS_PATH, strip_uz_suffix

# ─── Config ───────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]

PASSENGER_COST = 1000
REFERRAL_BONUS = 2000
AUTO_CLOSE_MINUTES = 15
SPAM_WINDOW = 120
SPAM_MAX = 3

# ─── Payment info ─────────────────────────────────────────────
PAYMENT_INFO = (
    "💳 <b>Pul to'ldirish</b>\n"
    "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
    "║ 📱 <b>Click:</b> 1234 5678 9012 3456\n"
    "║ 📱 <b>Payme:</b> 9876 5432 1098 7654\n"
    "║ 💳 <b>Humo:</b> 8765 4321 0987 6543\n"
    "║ 🏦 <b>Bank:</b> NBU — 1234 5678 9012 3456\n"
    "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
    "║ 📸 <b>Chek yuborish:</b>\n"
    "║ To'lov qilgandan so'ng chek\n"
    "║ screenshotini shu yerga\n"
    "║ yuboring — admin tasdiqlaydi!\n"
    "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
)

# ─── Logging ──────────────────────────────────────────────────

def format_direction(loc: str, direction: str) -> str:
    """Format location name with Uzbek direction suffix.
    
    direction='from' → 'dan' suffix: Qizilqoshdan, Samarqanddan, Ishtxonadan
    direction='to'   → 'ga/qa' suffix: Qizilqoshga, Ishtxonga, Samarqandga, Qiziltepaqa
    
    Uzbek suffix rules:
    - FROM: always add 'dan'
    - TO: 'qa' after q/k ending, 'ga' after everything else
    """
    if direction == "from":
        return loc + "dan"
    
    # TO direction
    last_char = loc[-1].lower()
    
    if last_char in ('q', 'k'):
        return loc + "qa"
    else:
        return loc + "ga"


def format_route(from_loc: str, to_loc: str) -> str:
    """Format a route with full Uzbek direction suffixes.
    e.g. 'Qizilqosh', 'Ishtxon' → 'Qizilqoshdan → Ishtxonga'
    """
    return f"{format_direction(from_loc, 'from')} → {format_direction(to_loc, 'to')}"


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Globals ──────────────────────────────────────────────────
db = Database()
locations_mgr = LocationsManager()
ai_parser = AIParser(locations_mgr)
voice_transcriber = None  # Lazy-initialized on first voice message

# ─── Helpers ──────────────────────────────────────────────────


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def stars_text(rating: float) -> str:
    if rating <= 0:
        return "—"
    full = int(rating)
    half = rating - full >= 0.5
    return "⭐" * full + ("✨" if half else "") + f" ({rating})"


def estimate_price(from_loc: str, to_loc: str) -> int:
    """Simple price estimate based on route."""
    known_routes = {
        ("Qizilqosh", "Ishtxon"): 15000,
        ("Qizilqosh", "Samarqand"): 30000,
        ("Qizilqosh", "Andijon"): 50000,
        ("Qizilqosh", "Andoq"): 10000,
        ("Ishtxon", "Samarqand"): 15000,
    }
    key = (from_loc, to_loc)
    reverse = (to_loc, from_loc)
    if key in known_routes:
        return known_routes[key]
    if reverse in known_routes:
        return known_routes[reverse]
    return 20000


def car_info_line(driver_info: dict) -> str:
    """Build a formatted car info line: 🎨color | 🚗model | 🔢number"""
    parts = []
    color = driver_info.get("car_color", "")
    if color:
        parts.append(f"🎨{color}")
    model = driver_info.get("car_model", "")
    if model:
        parts.append(f"🚗{model}")
    number = driver_info.get("car_number", "")
    if number:
        parts.append(f"🔢{number}")
    return " | ".join(parts) if parts else ""


# ─── Command Handlers ─────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message with main menu."""
    user = update.effective_user
    db.upsert_user(
        user.id,
        username=user.username,
        first_name=user.first_name,
        language_code=user.language_code,
    )

    # Check referral
    referral_code = context.args[0] if context.args else None
    if referral_code:
        try:
            referrer_id = int(referral_code)
            if referrer_id != user.id:
                db.add_referral(referrer_id, user.id)
        except (ValueError, TypeError):
            pass

    keyboard = ReplyKeyboardMarkup(
        [
            ["🚖 Buyurtma", "📋 Buyurtmalarim"],
            ["✏️ Profil", "💳 Hisobim"],
            ["⭐ Reyting", "🧹 Tozalash"],
        ],
        resize_keyboard=True,
    )

    text = (
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ 🚖 <b>Taxi Bot ga xush kelibsiz!</b>\n"
        f"║ {user.first_name}, salom!\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        "║ Guruhda buyurtma yaratish:\n"
        "║ • Yo'lovchi: «ishtxonga boraman»\n"
        "║ • Haydovchi: «ishtxonnga 4 kishiga\n"
        "║   joy bor»\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        "║ 🤖 Bot avtomatik tarzda\n"
        "║ yo'nalishni tushunadi!\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        "║ 🔗 Referal link:\n"
        f"║ <code>https://t.me/{context.bot.username}?start={user.id}</code>\n"
        "║ Har do'st uchun +2000 so'm!\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
    )
    menu_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💳 Hisobim", callback_data="menu_hisobim"),
            InlineKeyboardButton("📋 Buyurtmalarim", callback_data="menu_my_orders"),
        ],
        [
            InlineKeyboardButton("🚖 Haydovchilar", callback_data="menu_drivers"),
            InlineKeyboardButton("✏️ Profil", callback_data="menu_profile"),
        ],
        [
            InlineKeyboardButton("🔗 Referal", callback_data="menu_referral"),
            InlineKeyboardButton("🧹 Tozalash", callback_data="menu_tozalash"),
        ],
    ])
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")
    await update.message.reply_text("👇 Menyu:", reply_markup=menu_keyboard)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message with inline keyboard menu."""
    text = (
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ 🚖 <b>Taxi Bot — Yo'riqnoma</b>\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        "║ 📝 <b>Buyurtma yaratish:</b>\n"
        "║  «ishtxonga boraman» — yo'lovchi\n"
        "║  «ishtxonnga joy bor» — haydovchi\n"
        "║  «samarqanddan ishtxonga»\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        "║ 🎤 <b>Ovozli xabar</b> — bot\n"
        "║ transkribatsiya qiladi\n"
        "║ 📍 <b>@t1</b> — inline joylashuv\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        "║ Quyidagi menyu orqali\n"
        "║ kerakli bo'limni tanlang:\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💳 Hisobim", callback_data="menu_hisobim"),
            InlineKeyboardButton("📋 Buyurtmalarim", callback_data="menu_my_orders"),
        ],
        [
            InlineKeyboardButton("🚖 Haydovchilar", callback_data="menu_drivers"),
            InlineKeyboardButton("✏️ Profil", callback_data="menu_profile"),
        ],
        [
            InlineKeyboardButton("🔗 Referal", callback_data="menu_referral"),
            InlineKeyboardButton("🧹 Tozalash", callback_data="menu_tozalash"),
        ],
    ])
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")


async def cmd_hisobim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user balance and account info."""
    user_id = update.effective_user.id
    db.upsert_user(user_id, username=update.effective_user.username,
                   first_name=update.effective_user.first_name)
    balance = db.get_balance(user_id)
    stats = db.get_user_stats(user_id)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 To'ldirish", callback_data="hisobim_toldir"),
            InlineKeyboardButton("📊 Tarix", callback_data="hisobim_tarix"),
        ],
        [InlineKeyboardButton("🔙 Orqaga", callback_data="hisobim_back")],
    ])

    text = (
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ 💳 <b>Hisobim</b>\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        f"║ 💰 Balans: <b>{balance['balance']} so'm</b>\n"
        f"║ 📈 Jami daromad: {balance['total_earned']} so'm\n"
        f"║ 📉 Jami sarflar: {balance['total_spent']} so'm\n"
        f"║ 🚖 Yo'lovchilar: {balance['passengers_count']}\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        f"║ 📋 Buyurtmalar: {stats['total_orders']}\n"
        f"║ ⭐ Reyting: {stars_text(stats['rating_avg'])}\n"
        f"║ 🔗 Referallar: {stats['referrals']}\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
    )
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")


async def cmd_my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's active orders."""
    user_id = update.effective_user.id
    orders = db.get_user_orders(user_id, limit=10)

    if not orders:
        await update.message.reply_text(
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 📋 Sizda hali buyurtmalar\n"
            "║ yo'q.\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
            parse_mode="HTML",
        )
        return

    text = (
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ 📋 <b>Mening buyurtmalarim:</b>\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
    )
    buttons = []
    for order in orders[:5]:
        status_emoji = {"active": "🟢", "closed": "🔴", "matched": "🟡", "completed": "✅", "cancelled": "❌"}
        emoji = status_emoji.get(order["status"], "⚪")
        type_emoji = "🧑" if order["order_type"] == "passenger" else "🚗"
        text += (
            f"║ {emoji} {type_emoji} #{order['id']}\n"
            f"║ {format_route(order['from_location'], order['to_location'])}\n"
            f"║ Seats: {order['seats']} | {order['status']}\n"
            "║\n"
        )
        if order["status"] == "active":
            buttons.append(
                [InlineKeyboardButton(f"❌ Cancel #{order['id']}",
                                      callback_data=f"cancel_{order['id']}_{user_id}")]
            )
    text += "╚━━━━━━━━━━━━━━━━━━━━━╝\n"

    markup = InlineKeyboardMarkup(buttons) if buttons else None
    await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def cmd_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List available drivers."""
    drivers = db.get_available_drivers()

    if not drivers:
        await update.message.reply_text(
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 🚖 Hozircha haydovchilar\n"
            "║ yo'q.\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
            parse_mode="HTML",
        )
        return

    text = (
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ 🚖 <b>Mavjud haydovchilar:</b>\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
    )
    for d in drivers[:10]:
        name = d.get("first_name", "N/A")
        rating = stars_text(d.get("rating_avg", 0))
        car_line = car_info_line(d)
        text += f"║ ⭐ {name} | {rating}\n"
        if car_line:
            text += f"║ {car_line}\n"
        text += "║\n"
    text += "╚━━━━━━━━━━━━━━━━━━━━━╝\n"

    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show referral info and link."""
    user_id = update.effective_user.id
    db.upsert_user(user_id, username=update.effective_user.username,
                   first_name=update.effective_user.first_name)
    count = db.get_referral_count(user_id)
    referrals = db.get_referrals(user_id)

    text = (
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ 🔗 <b>Referal dasturi</b>\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        f"║ 👥 Do'stlar: {count}\n"
        f"║ 💰 Bonus: {count * REFERRAL_BONUS} so'm\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        "║ 📎 Referal link:\n"
        f"║ <code>https://t.me/{context.bot.username}?start={user_id}</code>\n"
        "║ Har yangi do'st uchun +2000 so'm!\n"
    )
    if referrals:
        text += "╠━━━━━━━━━━━━━━━━━━━━━╣\n║ 📋 <b>Do'stlar:</b>\n"
        for r in referrals[:5]:
            text += f"║  • {r.get('referred_name', 'User')}\n"
    text += "╚━━━━━━━━━━━━━━━━━━━━━╝\n"

    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user profile with inline edit buttons."""
    user_id = update.effective_user.id
    user = update.effective_user
    db.upsert_user(user_id, username=user.username, first_name=user.first_name)

    user_info = db.get_user(user_id)
    driver_info = db.get_driver(user_id)

    if driver_info:
        # Driver profile
        car_line = car_info_line(driver_info)
        avail = "🟢 Mavjud" if driver_info.get("available", 1) else "🔴 Band"
        avg_rating = stars_text(driver_info.get("rating_avg", 0))

        text = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ ✏️ <b>Haydovchi profili</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 👤 {user_info.get('first_name', 'N/A')}\n"
            f"║ 📱 {driver_info.get('phone', 'N/A')}\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ {car_line}\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ ⭐ Reyting: {avg_rating}\n"
            f"║ 🚖 Safarlar: {driver_info.get('total_rides', 0)}\n"
            f"║ Holat: {avail}\n"
            f"║ 📍 Terminal: {driver_info.get('terminal') or 'Belgilanmagan'}\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        )

        buttons = [
            [InlineKeyboardButton("📱 Telefon", callback_data="edit_phone"),
             InlineKeyboardButton("🚗 Model", callback_data="edit_car_model")],
            [InlineKeyboardButton("🎨 Rang", callback_data="edit_car_color"),
             InlineKeyboardButton("🔢 Raqam", callback_data="edit_car_number")],
            [InlineKeyboardButton("📍 Terminal", callback_data="set_terminal"),
             InlineKeyboardButton("🔄 Yo'lovchi bo'lish", callback_data="switch_to_passenger")],
            [InlineKeyboardButton("🔙 Menyu", callback_data="menu_back")],
        ]
    else:
        # Passenger profile
        text = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ ✏️ <b>Yo'lovchi profili</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 👤 {user_info.get('first_name', 'N/A')}\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 🚖 Haydovchi emas\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        )

        buttons = [
            [InlineKeyboardButton("✏️ Ismni tahrirlash", callback_data="edit_name")],
            [InlineKeyboardButton("🚖 Haydovchi bo'lish", callback_data="become_driver")],
            [InlineKeyboardButton("🔙 Menyu", callback_data="menu_back")],
        ]

    keyboard = InlineKeyboardMarkup(buttons)
    # If called from command, use update.message; if from callback, it's handled elsewhere
    if update.message:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")


async def cmd_addbal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add balance (admin or user request)."""
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(PAYMENT_INFO, parse_mode="HTML")
        return

    if is_admin(user_id) and len(context.args) >= 2:
        try:
            target_id = int(context.args[0])
            amount = int(context.args[1])
            db.add_balance(target_id, amount)
            await update.message.reply_text(
                f"✅ {target_id} ga {amount} so'm qo'shildi."
            )
        except (ValueError, TypeError):
            await update.message.reply_text("❌ Format: addbal user_id amount")
    else:
        await update.message.reply_text(PAYMENT_INFO, parse_mode="HTML")


async def cmd_setbal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set user balance (admin only)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Faqat adminlar uchun.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("❌ Format: setbal user_id amount")
        return

    try:
        target_id = int(context.args[0])
        amount = int(context.args[1])
        db.ensure_balance(target_id)
        conn = db._get_conn()
        conn.execute("UPDATE balances SET balance = ? WHERE user_id = ?", (amount, target_id))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ {target_id} balansi {amount} so'm ga o'rnatildi.")
    except (ValueError, TypeError):
        await update.message.reply_text("❌ Format: setbal user_id amount")


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin panel."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Faqat adminlar uchun.")
        return

    stats = db.get_stats()
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Statistika", callback_data="admin_stats"),
            InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="admin_users"),
        ],
        [
            InlineKeyboardButton("🚖 Haydovchilar", callback_data="admin_drivers"),
            InlineKeyboardButton("📋 Buyurtmalar", callback_data="admin_orders"),
        ],
        [
            InlineKeyboardButton("📍 Locations", callback_data="admin_locs"),
            InlineKeyboardButton("🔔 Broadcast", callback_data="admin_broadcast"),
        ],
    ])

    text = (
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ ⚙️ <b>Admin Panel</b>\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        f"║ 👥 Foydalanuvchilar: {stats['users_count']}\n"
        f"║ 🚖 Haydovchilar: {stats['drivers_count']}\n"
        f"║ 🟢 Faol buyurtmalar: {stats['active_orders']}\n"
        f"║ ✅ Yakunlangan: {stats['completed_orders']}\n"
        f"║ 📊 Jami buyurtmalar: {stats['total_orders']}\n"
        f"║ 💰 Jami daromad: {stats['total_revenue']} so'm\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
    )
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")


async def cmd_addloc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a new location (admin only)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Faqat adminlar uchun.")
        return

    if not context.args:
        await update.message.reply_text("❌ Format: addloc LocationName")
        return

    loc_name = " ".join(context.args)
    with open(LOCATIONS_PATH, "r", encoding="utf-8") as f:
        locs = json.load(f)

    if loc_name in locs:
        await update.message.reply_text(f"❌ '{loc_name}' allaqachon mavjud.")
        return

    locs[loc_name] = [loc_name.lower()]
    with open(LOCATIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(locs, f, ensure_ascii=False, indent=2)

    locations_mgr.reload()
    await update.message.reply_text(f"✅ '{loc_name}' qo'shildi!")


async def cmd_pendinglocs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show pending locations (admin only)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Faqat adminlar uchun.")
        return

    pending = locations_mgr.get_pending()
    if not pending:
        await update.message.reply_text("📋 Kutilayotgan joylashuvlar yo'q.")
        return

    text = (
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ 📋 <b>Kutilayotgan joylashuvlar:</b>\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
    )
    for p in pending:
        text += f"║  • {p}\n"
    text += "╚━━━━━━━━━━━━━━━━━━━━━╝\n"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Tozalash", callback_data="admin_clear_pending")],
    ])
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast message to all users (admin only)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Faqat adminlar uchun.")
        return

    if not context.args:
        await update.message.reply_text("❌ Format: broadcast message_text")
        return

    msg = " ".join(context.args)
    users = db.get_all_users()
    sent = 0
    for u in users:
        try:
            await context.bot.send_message(u["user_id"], f"📢 {msg}", parse_mode="HTML")
            sent += 1
        except Exception:
            pass

    await update.message.reply_text(f"✅ {sent}/{len(users)} foydalanuvchilarga yuborildi.")


# ─── Inline Query ────────────────────────────────────────────


async def inline_query_locations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline queries — show route suggestions with seat count variants."""
    query = update.inline_query.query.strip()
    results = []
    all_locs = locations_mgr.get_all_names()
    popular_dests = all_locs[:6]
    popular_origins = all_locs[:4]
    base = "Qizilqosh"
    seat_labels = {1: "1 yo'lovchi", 2: "2 yo'lovchi", 3: "3 yo'lovchi", 4: "4 yo'lovchi"}

    if not query:
        # Empty query — show routes FROM base location to popular destinations
        idx = 0
        for to_loc in popular_dests:
            if to_loc == base:
                continue
            route_display = format_route(base, to_loc)
            for seats, seat_label in seat_labels.items():
                route_text = f"{format_direction(base, 'from')} {seats} kishiga {format_direction(to_loc, 'to')}"
                results.append(
                    InlineQueryResultArticle(
                        id=f"r_{idx}_{seats}",
                        title=f"{route_display} ({seat_label})",
                        input_message_content=InputTextMessageContent(
                            message_text=route_text
                        ),
                        description="🚖 Yo'nalish",
                    )
                )
                idx += 1
        # Routes TO base from popular origins
        for from_loc in popular_origins:
            if from_loc == base:
                continue
            route_display = format_route(from_loc, base)
            for seats, seat_label in seat_labels.items():
                route_text = f"{format_direction(from_loc, 'from')} {seats} kishiga {format_direction(base, 'to')}"
                results.append(
                    InlineQueryResultArticle(
                        id=f"r2_{idx}_{seats}",
                        title=f"{route_display} ({seat_label})",
                        input_message_content=InputTextMessageContent(
                            message_text=route_text
                        ),
                        description="🚖 Yo'nalish",
                    )
                )
                idx += 1
    else:
        # Try to parse as full route first
        parsed_route = locations_mgr.extract_route_from_text(query, base)
        if parsed_route and parsed_route[0] and parsed_route[1] and parsed_route[0] != parsed_route[1]:
            from_loc, to_loc = parsed_route
            route_display = format_route(from_loc, to_loc)
            for seats, seat_label in seat_labels.items():
                route_text = f"{format_direction(from_loc, 'from')} {seats} kishiga {format_direction(to_loc, 'to')}"
                results.append(
                    InlineQueryResultArticle(
                        id=f"parsed_{seats}",
                        title=f"{route_display} ({seat_label})",
                        input_message_content=InputTextMessageContent(
                            message_text=route_text
                        ),
                        description="🚖 Yo'nalish",
                    )
                )

        # Find matching locations → show routes FROM and TO
        matched_locs = locations_mgr.search_locations(query, limit=3)
        idx = len(results)
        for from_loc in matched_locs:
            # Routes FROM matched location
            for to_loc in popular_dests[:4]:
                if to_loc == from_loc:
                    continue
                route_display = format_route(from_loc, to_loc)
                for seats in [1, 2, 3, 4]:
                    seat_label = seat_labels[seats]
                    route_text = f"{format_direction(from_loc, 'from')} {seats} kishiga {format_direction(to_loc, 'to')}"
                    results.append(
                        InlineQueryResultArticle(
                            id=f"from_{idx}_{seats}",
                            title=f"{route_display} ({seat_label})",
                            input_message_content=InputTextMessageContent(
                                message_text=route_text
                            ),
                            description=f"🚖 {from_loc}dan",
                        )
                    )
                    idx += 1
            # Routes TO matched location
            for origin in popular_origins[:3]:
                if origin == from_loc:
                    continue
                route_display = format_route(origin, from_loc)
                for seats in [1, 2, 3]:
                    seat_label = seat_labels[seats]
                    route_text = f"{format_direction(origin, 'from')} {seats} kishiga {format_direction(from_loc, 'to')}"
                    results.append(
                        InlineQueryResultArticle(
                            id=f"to_{idx}_{seats}",
                            title=f"{route_display} ({seat_label})",
                            input_message_content=InputTextMessageContent(
                                message_text=route_text
                            ),
                            description=f"🚖 {from_loc}ga",
                        )
                    )
                    idx += 1
        # Single location results
        for i, name in enumerate(matched_locs):
            results.append(
                InlineQueryResultArticle(
                    id=f"loc_{i}",
                    title=f"📍 {name}",
                    input_message_content=InputTextMessageContent(
                        message_text=f"📍 {name}"
                    ),
                    description=f"Joylashuv: {name}",
                )
            )

    await update.inline_query.answer(results[:50], cache_time=30)


# ─── Group Message Handlers ──────────────────────────────────


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Parse group messages for taxi orders."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    if not user:
        return

    text = update.message.text
    if not text:
        return

    # Skip @bot mention messages — handled by handle_mention_ask_route
    bot_username = context.bot.username
    mention_pattern = f"@{bot_username}"
    if mention_pattern.lower() in text.lower():
        return

    # Check if user has started the bot (subscribed)
    user_data = db.get_user(user.id)
    if not user_data:
        # User hasn't started the bot yet
        try:
            await context.bot.send_message(
                user.id,
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ ⚠️ <b>Botga obuna bo'ling!</b>\n"
                "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                "║ Buyurtma berish uchun avval botga\n"
                "║ /start yuboring.\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                parse_mode="HTML",
            )
        except Exception:
            pass
        # Delete the group message
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    # Register user (update info)
    db.upsert_user(user.id, username=user.username, first_name=user.first_name)

    # Save group settings
    chat_title = update.effective_chat.title or ""
    db.save_group_settings(chat_id, chat_title, "Qizilqosh")

    # Spam check - delete message without reply
    if db.check_spam(user.id, chat_id, SPAM_WINDOW, SPAM_MAX):
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    # Parse the message
    group_settings = db.get_group_settings(chat_id)
    base_location = group_settings.get("base_location", "Qizilqosh") if group_settings else "Qizilqosh"

    parsed = ai_parser.parse(text, base_location)
    if not parsed:
        return

    # Override order type based on user's ACTUAL role — not keyword parsing
    if db.is_driver(user.id):
        parsed["type"] = "driver"
    else:
        parsed["type"] = "passenger"

    # Check for active duplicate order - delete message without reply
    existing = db.get_active_order(user.id, chat_id, parsed["type"])
    if existing:
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    # Check balance — must have money to create order
    balance_info = db.get_balance(user.id)
    if balance_info["balance"] <= 0:
        try:
            await update.message.delete()
        except Exception:
            pass
        try:
            await context.bot.send_message(
                user.id,
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ ⚠️ <b>Hisobda pul yo'q!</b>\n"
                "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                "║ Avval hisobni to'ldiring:\n"
                "║ 💳 Hisobim → 📸 Chek yuborish\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # Estimate price
    price = estimate_price(parsed.get("from", base_location), parsed.get("to", base_location))

    # Create order
    order_id = db.create_order(
        user_id=user.id,
        chat_id=chat_id,
        order_type=parsed["type"],
        from_location=parsed.get("from", base_location),
        to_location=parsed.get("to", base_location),
        seats=parsed.get("seats", 1),
        price=price,
        departure_time=parsed.get("time"),
        message_id=update.message.message_id,
    )

    # Build order message with box formatting
    type_label = "🧑 Yo'lovchi" if parsed["type"] == "passenger" else "🚗 Haydovchi"
    order_msg = (
        f"╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        f"║ {type_label} #{order_id}\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        f"║ 📍 {format_route(parsed.get('from', base_location), parsed.get('to', base_location))}\n"
        f"║ 💺 O'rindiq: {parsed.get('seats', 1)}\n"
        f"║ 💰 ~{price} so'm\n"
    )
    if parsed.get("time"):
        order_msg += f"║ 🕐 Soat: {parsed['time']}\n"
    order_msg += (
        f"║ 👤 {user.first_name}\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
    )

    # Accept/cancel buttons
    accept_pattern = f"accept_{parsed['type']}_{order_id}"
    cancel_pattern = f"cancel_{order_id}_{user.id}"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Qabul", callback_data=accept_pattern),
            InlineKeyboardButton("❌ Bekor", callback_data=cancel_pattern),
        ],
        [InlineKeyboardButton("🔄 Qayta post", callback_data=f"repost_{order_id}")],
    ])

    # Delete the user's original message, keep only the bot's formatted order
    try:
        await update.message.delete()
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=chat_id,
        text=order_msg,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def handle_at_t1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle @t1 mentions in groups — show location picker."""
    text = (
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ 📍 <b>Joylashuv tanlash</b>\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        "║ Inline mode'dan foydalaning:\n"
        f"║ @{context.bot.username} joylashuv nomi\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        "║ Mavjud joylashuvlar:\n"
    )
    locs = locations_mgr.get_all_names()[:15]
    for loc in locs:
        text += f"║  • {loc}\n"
    text += "╚━━━━━━━━━━━━━━━━━━━━━╝\n"

    await update.message.reply_text(text, parse_mode="HTML")


async def handle_mention_ask_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle @bot mentions in groups — parse route directly or ask 'qayerdan qayerga?'."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    text = update.message.text
    if not user or not text:
        return

    # Only trigger if message mentions the bot (@bot_username)
    bot_username = context.bot.username
    mention_pattern = f"@{bot_username}"
    if mention_pattern.lower() not in text.lower():
        return

    # Remove @bot mention from text to get pure route text
    route_text = text.replace(mention_pattern, "").strip()
    # Also handle case-insensitive mention
    route_text_ci = text.lower().replace(mention_pattern.lower(), "").strip()
    if not route_text and route_text_ci:
        route_text = text.replace(text.lower().replace(mention_pattern.lower(), "").strip(), "").replace(mention_pattern, "").strip()

    # Check if user has started the bot
    user_data = db.get_user(user.id)
    if not user_data:
        try:
            await context.bot.send_message(
                user.id,
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ ⚠️ <b>Botga obuna bo'ling!</b>\n"
                "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                "║ Buyurtma berish uchun avval botga\n"
                "║ /start yuboring.\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                parse_mode="HTML",
            )
        except Exception:
            pass
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    db.upsert_user(user.id, username=user.username, first_name=user.first_name)

    # Try to parse route directly from the mention text
    if route_text:
        group_settings = db.get_group_settings(chat_id)
        base_location = group_settings.get("base_location", "Qizilqosh") if group_settings else "Qizilqosh"
        parsed = ai_parser.parse(route_text, base_location)
        if parsed:
            # Override order type based on user's ACTUAL role
            if db.is_driver(user.id):
                parsed["type"] = "driver"
            else:
                parsed["type"] = "passenger"

            # Check balance
            balance_info = db.get_balance(user.id)
            if balance_info["balance"] <= 0:
                try:
                    await update.message.delete()
                except Exception:
                    pass
                try:
                    await context.bot.send_message(
                        user.id,
                        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                        "║ ⚠️ <b>Hisobda pul yo'q!</b>\n"
                        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                        "║ Avval hisobni to'ldiring:\n"
                        "║ 💳 Hisobim → 📸 Chek yuborish\n"
                        "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                return

            # Check for active duplicate
            existing = db.get_active_order(user.id, chat_id, parsed["type"])
            if existing:
                try:
                    await update.message.delete()
                except Exception:
                    pass
                return

            # Store parsed route for seat selection
            context.user_data["pending_order"] = {
                "from": parsed.get("from", base_location),
                "to": parsed.get("to", base_location),
                "type": parsed["type"],
                "chat_id": chat_id,
                "time": parsed.get("time"),
                "price": price,
                "user_first_name": user.first_name,
                "user_id": user.id,
            }

            try:
                await update.message.delete()
            except Exception:
                pass

            # Show seat selection buttons in private chat
            route_display = format_route(parsed.get("from", base_location), parsed.get("to", base_location))
            type_label = "🧑 Yo'lovchi" if parsed["type"] == "passenger" else "🚗 Haydovchi"
            seat_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("1 yo'lovchi", callback_data="select_seats_1"),
                    InlineKeyboardButton("2 yo'lovchi", callback_data="select_seats_2"),
                ],
                [
                    InlineKeyboardButton("3 yo'lovchi", callback_data="select_seats_3"),
                    InlineKeyboardButton("4 yo'lovchi", callback_data="select_seats_4"),
                ],
            ])
            try:
                await context.bot.send_message(
                    user.id,
                    f"╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                    f"║ {type_label}\n"
                    "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                    f"║ 📍 {route_display}\n"
                    f"║ 💰 ~{price} so'm\n"
                    "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                    "║ <b>Yo'lovchi sonini tanlang:</b>\n"
                    "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                    reply_markup=seat_keyboard,
                    parse_mode="HTML",
                )
            except Exception:
                # If user hasn't started bot, ask them to
                await context.bot.send_message(
                    chat_id,
                    f"👤 {user.first_name}, botga /start yuboring, keyin yo'lovchi sonini tanlaysiz!",
                )
            return

    # No route parsed — ask "qayerdan qayerga?"
    context.user_data["ask_route_chat_id"] = chat_id
    context.user_data["ask_route_user_id"] = user.id

    question = (
        f"👤 {user.first_name}\n"
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ 🗺️ <b>Qayerdan → Qayerga?</b>\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        "║ Javob yozing:\n"
        "║   «samarqanddan ishtxonga»\n"
        "║   «andijon → toshkent»\n"
        "║   «qo'qondan 2 kishiga marg'ilon»\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
    )
    reply_msg = await update.message.reply_text(question, parse_mode="HTML")
    try:
        await update.message.delete()
    except Exception:
        pass

    context.user_data["ask_route_msg_id"] = reply_msg.message_id


async def handle_mention_answer_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the user's answer to 'qayerdan qayerga?' question."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    if not user:
        return

    # Check if this user has an active ask_route flow
    if context.user_data.get("ask_route_user_id") != user.id:
        return
    if context.user_data.get("ask_route_chat_id") != chat_id:
        return

    text = update.message.text.strip()
    context.user_data.pop("ask_route_user_id", None)
    context.user_data.pop("ask_route_chat_id", None)
    ask_route_msg_id = context.user_data.pop("ask_route_msg_id", None)

    # Delete the question message
    if ask_route_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=ask_route_msg_id)
        except Exception:
            pass

    # Parse the answer as an order
    # Check balance — must have money to create order
    balance_info = db.get_balance(user.id)
    if balance_info["balance"] <= 0:
        try:
            await update.message.delete()
        except Exception:
            pass
        try:
            await context.bot.send_message(
                user.id,
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ ⚠️ <b>Hisobda pul yo'q!</b>\n"
                "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                "║ Avval hisobni to'ldiring:\n"
                "║ 💳 Hisobim → 📸 Chek yuborish\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    group_settings = db.get_group_settings(chat_id)
    base_location = group_settings.get("base_location", "Qizilqosh") if group_settings else "Qizilqosh"

    parsed = ai_parser.parse(text, base_location)
    if not parsed:
        await update.message.reply_text(
            "⚠️ Tushunilmadi. Masalan:\n"
            "«samarqanddan ishtxonga»\n"
            "«qo'qondan 2 kishiga marg'ilon»",
        )
        return

    # Override order type based on user's ACTUAL role
    if db.is_driver(user.id):
        parsed["type"] = "driver"
    else:
        parsed["type"] = "passenger"

    # Check for active duplicate
    existing = db.get_active_order(user.id, chat_id, parsed["type"])
    if existing:
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    # Store parsed route for seat selection
    context.user_data["pending_order"] = {
        "from": parsed.get("from", base_location),
        "to": parsed.get("to", base_location),
        "type": parsed["type"],
        "chat_id": chat_id,
        "time": parsed.get("time"),
        "price": price,
        "user_first_name": user.first_name,
        "user_id": user.id,
    }

    # Delete user's answer and the "qayerdan qayerga?" question
    try:
        await update.message.delete()
    except Exception:
        pass
    if ask_route_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=ask_route_msg_id)
        except Exception:
            pass

    # Show seat selection buttons in private chat
    route_display = format_route(parsed.get("from", base_location), parsed.get("to", base_location))
    type_label = "🧑 Yo'lovchi" if parsed["type"] == "passenger" else "🚗 Haydovchi"
    seat_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1 yo'lovchi", callback_data="select_seats_1"),
            InlineKeyboardButton("2 yo'lovchi", callback_data="select_seats_2"),
        ],
        [
            InlineKeyboardButton("3 yo'lovchi", callback_data="select_seats_3"),
            InlineKeyboardButton("4 yo'lovchi", callback_data="select_seats_4"),
        ],
    ])
    try:
        await context.bot.send_message(
            user.id,
            f"╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            f"║ {type_label}\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 📍 {route_display}\n"
            f"║ 💰 ~{price} so'm\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            "║ <b>Yo'lovchi sonini tanlang:</b>\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
            reply_markup=seat_keyboard,
            parse_mode="HTML",
        )
    except Exception:
        await context.bot.send_message(
            chat_id,
            f"👤 {user.first_name}, botga /start yuboring, keyin yo'lovchi sonini tanlaysiz!",
        )


async def handle_voice_in_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Transcribe voice messages in groups and parse orders."""
    global voice_transcriber

    chat_id = update.effective_chat.id
    user = update.effective_user
    if not user:
        return

    db.upsert_user(user.id, username=user.username, first_name=user.first_name)

    voice = update.message.voice
    if not voice:
        return

    # Lazy-initialize voice transcriber on first use
    if voice_transcriber is None:
        try:
            voice_transcriber = VoiceTranscriber(model_size="small", device="cpu")
        except Exception as e:
            logger.error(f"Failed to initialize VoiceTranscriber: {e}")
            await update.message.reply_text("⚠️ Ovozli xabar funksiyasi hozircha mavjud emas.")
            return

    # Download voice file
    voice_file = await voice.get_file()
    voice_path = f"/tmp/voice_{user.id}_{voice.file_unique_id}.ogg"
    await voice_file.download_to_drive(voice_path)

    try:
        transcribed = voice_transcriber.transcribe_auto_language(voice_path)
    except Exception as e:
        logger.error(f"Voice transcription failed: {e}")
        await update.message.reply_text("⚠️ Ovozli xabarni tushunib bo'lmadi.")
        return

    if not transcribed:
        await update.message.reply_text("⚠️ Ovozli xabar bo'sh.")
        return

    # Parse transcribed text directly as order
    group_settings = db.get_group_settings(chat_id)
    base_location = group_settings.get("base_location", "Qizilqosh") if group_settings else "Qizilqosh"

    parsed = ai_parser.parse(transcribed, base_location)
    if parsed:
        # Override order type based on user's ACTUAL role
        if db.is_driver(user.id):
            parsed["type"] = "driver"
        else:
            parsed["type"] = "passenger"

        # Check for active duplicate order
        existing = db.get_active_order(user.id, chat_id, parsed["type"])
        if existing:
            await update.message.reply_text(
                f"⚠️ Sizda faol buyurtma bor: #{existing['id']}\n"
                f"{format_route(existing['from_location'], existing['to_location'])}"
            )
            return

        price = estimate_price(parsed.get("from", base_location), parsed.get("to", base_location))
        order_id = db.create_order(
            user_id=user.id,
            chat_id=chat_id,
            order_type=parsed["type"],
            from_location=parsed.get("from", base_location),
            to_location=parsed.get("to", base_location),
            seats=parsed.get("seats", 1),
            price=price,
            departure_time=parsed.get("time"),
            message_id=update.message.message_id,
        )

        type_label = "🧑 Yo'lovchi" if parsed["type"] == "passenger" else "🚗 Haydovchi"
        order_msg = (
            f"╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            f"║ {type_label} #{order_id}\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 🎤 {transcribed}\n"
            f"║ 📍 {format_route(parsed.get('from', base_location), parsed.get('to', base_location))}\n"
            f"║ 💺 O'rindiq: {parsed.get('seats', 1)}\n"
            f"║ 💰 ~{price} so'm\n"
        )
        if parsed.get("time"):
            order_msg += f"║ 🕐 Soat: {parsed['time']}\n"
        order_msg += (
            f"║ 👤 {user.first_name}\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Qabul", callback_data=f"accept_{parsed['type']}_{order_id}"),
                InlineKeyboardButton("❌ Bekor", callback_data=f"cancel_{order_id}_{user.id}"),
            ],
            [InlineKeyboardButton("🔄 Qayta post", callback_data=f"repost_{order_id}")],
        ])

        # Delete original voice message, keep only the bot's order
        try:
            await update.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=chat_id,
            text=order_msg,
            reply_markup=keyboard,
            parse_mode="HTML",
        )


# ─── Callback Handlers ───────────────────────────────────────


async def callback_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle accept button for orders."""
    query = update.callback_query
    await query.answer()

    data = query.data  # accept_passenger_123 or accept_driver_123
    parts = data.split("_")
    if len(parts) != 3:
        return

    order_type = parts[1]
    order_id = int(parts[2])
    acceptor_id = query.from_user.id

    order = db.get_order(order_id)
    if not order or order["status"] != "active":
        await query.edit_message_text("❌ Buyurtma faol emas.")
        return

    # Check balance — must have money to accept order
    balance_info = db.get_balance(acceptor_id)
    if balance_info["balance"] <= 0:
        await query.answer("⚠️ Hisobda pul yo'q! Avval to'ldiring.", show_alert=True)
        return

    order_owner = order["user_id"]

    # Passenger accepts driver order → decrement seats
    if order_type == "driver" and acceptor_id != order_owner:
        new_seats = db.decrement_seats(order_id)

        if new_seats <= 0:
            db.update_order(order_id, status="matched")
            await query.edit_message_text(
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ ✅ Buyurtma to'ldi!\n"
                "║ Barcha joylar band.\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                parse_mode="HTML",
            )
        else:
            # Notify order owner about new passenger
            db.add_contact(acceptor_id, order_owner, order_id)
            try:
                await context.bot.send_message(
                    order_owner,
                    f"╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                    f"║ 🧑 Yangi yo'lovchi\n"
                    f"║ {query.from_user.first_name}\n"
                    f"╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                    f"║ Buyurtma #{order_id}\n"
                    f"║ Qolgan joy: {new_seats}\n"
                    f"╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                    parse_mode="HTML",
                )
            except Exception:
                pass

            # Update message with new seat count
            type_label = "🚗 Haydovchi"
            updated_msg = (
                f"╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                f"║ {type_label} #{order_id}\n"
                "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                f"║ 📍 {format_route(order['from_location'], order['to_location'])}\n"
                f"║ 💺 Qolgan joy: {new_seats}\n"
                f"║ 💰 ~{order['price']} so'm\n"
                f"║ 👤 {query.from_user.first_name} qabul qildi\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Qabul", callback_data=f"accept_driver_{order_id}"),
                    InlineKeyboardButton("❌ Bekor", callback_data=f"cancel_{order_id}_{order_owner}"),
                ],
            ])
            await query.edit_message_text(updated_msg, reply_markup=keyboard, parse_mode="HTML")

    # Driver accepts passenger order → connect them
    elif order_type == "passenger" and acceptor_id != order_owner:
        db.update_order(order_id, status="matched")
        db.add_contact(order_owner, acceptor_id, order_id)

        driver_info = db.get_driver(acceptor_id)
        driver_name = query.from_user.first_name
        car_line = car_info_line(driver_info) if driver_info else ""

        # Notify passenger
        try:
            notify_msg = (
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ 🚗 Haydovchi topildi!\n"
                "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                f"║ 👤 {driver_name}\n"
            )
            if car_line:
                notify_msg += f"║ {car_line}\n"
            notify_msg += (
                "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                f"║ Buyurtma #{order_id}\n"
                f"║ {format_route(order['from_location'], order['to_location'])}\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
            )
            await context.bot.send_message(
                order_owner,
                notify_msg,
                parse_mode="HTML",
            )
        except Exception:
            pass

        await query.edit_message_text(
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            f"║ ✅ Haydovchi qabul qildi!\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 🚗 {driver_name}\n"
            f"║ {format_route(order['from_location'], order['to_location'])}\n"
            f"║ Buyurtma #{order_id} matched\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
            parse_mode="HTML",
        )


async def callback_select_seats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle seat selection — create order after user picks passenger count."""
    query = update.callback_query
    await query.answer()

    data = query.data  # select_seats_1, select_seats_2, etc.
    seats = int(data.split("_")[-1])

    pending = context.user_data.get("pending_order")
    if not pending:
        await query.edit_message_text("⚠️ Buyurtma ma'lumotlari topilmadi. Qayta urinib ko'ring.")
        return

    from_loc = pending["from"]
    to_loc = pending["to"]
    order_type = pending["type"]
    chat_id = pending["chat_id"]
    departure_time = pending.get("time")
    price = pending["price"]
    user_first_name = pending["user_first_name"]
    user_id = pending["user_id"]

    # Clean up pending data
    context.user_data.pop("pending_order", None)

    # Check for active duplicate
    existing = db.get_active_order(user_id, chat_id, order_type)
    if existing:
        await query.edit_message_text(f"⚠️ Sizda faol buyurtma bor: #{existing['id']}")
        return

    order_id = db.create_order(
        user_id=user_id,
        chat_id=chat_id,
        order_type=order_type,
        from_location=from_loc,
        to_location=to_loc,
        seats=seats,
        price=price,
        departure_time=departure_time,
    )

    type_label = "🧑 Yo'lovchi" if order_type == "passenger" else "🚗 Haydovchi"
    order_msg = (
        f"╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        f"║ {type_label} #{order_id}\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        f"║ 📍 {format_route(from_loc, to_loc)}\n"
        f"║ 💺 O'rindiq: {seats}\n"
        f"║ 💰 ~{price} so'm\n"
    )
    if departure_time:
        order_msg += f"║ 🕐 Soat: {departure_time}\n"
    order_msg += (
        f"║ 👤 {user_first_name}\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
    )

    # Group message: only ✅ Qabul
    group_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Qabul", callback_data=f"accept_{order_type}_{order_id}")],
    ])
    await context.bot.send_message(
        chat_id=chat_id,
        text=order_msg,
        reply_markup=group_keyboard,
        parse_mode="HTML",
    )

    # Private message to creator: confirmation + control buttons
    private_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❌ Bekor", callback_data=f"cancel_{order_id}_{user_id}"),
            InlineKeyboardButton("🔄 Qayta post", callback_data=f"repost_{order_id}"),
        ],
    ])
    await query.edit_message_text(
        f"✅ Buyurtma yaratildi!\n\n{order_msg}",
        reply_markup=private_keyboard,
        parse_mode="HTML",
    )


async def callback_cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle cancel order button."""
    query = update.callback_query
    await query.answer()

    data = query.data  # cancel_123_456
    parts = data.split("_")
    if len(parts) != 3:
        return

    order_id = int(parts[1])
    user_id = int(parts[2])

    order = db.get_order(order_id)
    if not order:
        await query.edit_message_text("❌ Buyurtma topilmadi.")
        return

    if query.from_user.id != user_id and not is_admin(query.from_user.id):
        await query.answer("❌ Faqat egasi bekor qilishi mumkin.", show_alert=True)
        return

    db.cancel_order(order_id)

    # Delete the bot's order message from the group
    try:
        await query.message.delete()
    except Exception:
        # If delete fails (no permission), just edit the text
        await query.edit_message_text(
            f"❌ Buyurtma #{order_id} bekor qilindi."
        )

    # Try to delete the user's original message too (if bot is admin)
    if order.get("message_id"):
        try:
            await context.bot.delete_message(
                chat_id=order["chat_id"],
                message_id=order["message_id"],
            )
        except Exception:
            pass


async def callback_t1_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle t1 pagination."""
    query = update.callback_query
    await query.answer()
    # Pagination for location listing — future feature
    await query.edit_message_text("📍 Sahifa navigatsiyasi keyin qo'shiladi.")


async def callback_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle rating callback."""
    query = update.callback_query
    await query.answer()

    data = query.data  # rate_orderId_targetId_rating
    parts = data.split("_")
    if len(parts) != 4:
        return

    order_id = int(parts[1])
    target_id = int(parts[2])
    rating = int(parts[3])

    db.add_rating(order_id, query.from_user.id, target_id, rating)
    avg = db.get_avg_rating(target_id)

    await query.edit_message_text(
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        f"║ ⭐ Reyting berildi: {rating}/5\n"
        f"║ O'rtacha: {stars_text(avg)}\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
        parse_mode="HTML",
    )


async def callback_complete_trip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle complete trip callback."""
    query = update.callback_query
    await query.answer()

    data = query.data  # complete_orderId_targetId
    parts = data.split("_")
    if len(parts) != 3:
        return

    order_id = int(parts[1])
    target_id = int(parts[2])

    db.complete_order(order_id)
    db.complete_contact(order_id)

    # Update driver terminal to destination
    order = db.get_order(order_id)
    if order and order.get("to_location"):
        driver_id = query.from_user.id
        if db.is_driver(driver_id):
            db.set_driver_terminal(driver_id, order["to_location"])

    # Ask for rating
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐ 1", callback_data=f"rate_{order_id}_{target_id}_1"),
            InlineKeyboardButton("⭐ 2", callback_data=f"rate_{order_id}_{target_id}_2"),
            InlineKeyboardButton("⭐ 3", callback_data=f"rate_{order_id}_{target_id}_3"),
        ],
        [
            InlineKeyboardButton("⭐ 4", callback_data=f"rate_{order_id}_{target_id}_4"),
            InlineKeyboardButton("⭐ 5", callback_data=f"rate_{order_id}_{target_id}_5"),
        ],
    ])

    await query.edit_message_text(
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        f"║ ✅ Trip #{order_id} yakunlandi!\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        "║ ⭐ Haydovchi reytingini\n"
        "║ bering:\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def callback_repost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Repost an order."""
    query = update.callback_query
    await query.answer()

    data = query.data  # repost_123
    order_id = int(data.split("_")[1])

    order = db.get_order(order_id)
    if not order or order["status"] != "active":
        await query.edit_message_text("❌ Buyurtma faol emas.")
        return

    type_label = "🧑 Yo'lovchi" if order["order_type"] == "passenger" else "🚗 Haydovchi"
    user_info = db.get_user(order["user_id"])
    user_name = user_info.get("first_name", "Foydalanuvchi") if user_info else "Foydalanuvchi"

    order_msg = (
        f"╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        f"║ {type_label} #{order_id}\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        f"║ 📍 {format_route(order['from_location'], order['to_location'])}\n"
        f"║ 💺 O'rindiq: {order['seats']}\n"
        f"║ 💰 ~{order['price']} so'm\n"
        f"║ 👤 {user_name}\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Qabul", callback_data=f"accept_{order['order_type']}_{order_id}"),
            InlineKeyboardButton("❌ Bekor", callback_data=f"cancel_{order_id}_{order['user_id']}"),
        ],
    ])
    await query.message.reply_text(order_msg, reply_markup=keyboard, parse_mode="HTML")


async def callback_driver_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle driver availability."""
    query = update.callback_query
    await query.answer()

    data = query.data  # driver_toggle_123
    user_id = int(data.split("_")[2])

    if not db.is_driver(user_id):
        await query.answer("❌ Siz haydovchi emas.", show_alert=True)
        return

    current = db.is_driver_available(user_id)
    db.set_driver_available(user_id, not current)

    status = "🟢 Mavjud" if not current else "🔴 Band"
    await query.edit_message_text(
        f"╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        f"║ 🚗 Holat: {status}\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
        parse_mode="HTML",
    )


async def callback_hisobim_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle hisobim menu callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id

    if data == "hisobim_toldir":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="hisobim_back")]])
        await query.edit_message_text(PAYMENT_INFO, reply_markup=keyboard, parse_mode="HTML")
    elif data == "hisobim_tarix":
        transactions = db.get_transactions(user_id, limit=10)
        if not transactions:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="hisobim_back")]])
            await query.edit_message_text(
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ 📋 Tranzaksiyalar yo'q.\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                reply_markup=keyboard, parse_mode="HTML",
            )
            return
        text = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 📊 <b>Tranzaksiyalar:</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        )
        for t in transactions:
            emoji = "📈" if t["type"] == "credit" else "📉"
            text += f"║ {emoji} {t['amount']} so'm — {t['description']}\n"
        text += "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="hisobim_back")]])
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    elif data == "hisobim_back":
        text = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 🚖 <b>Taxi Bot — Menyu</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            "║ Quyidagi bo'limni tanlang:\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💳 Hisobim", callback_data="menu_hisobim"),
                InlineKeyboardButton("📋 Buyurtmalarim", callback_data="menu_my_orders"),
            ],
            [
                InlineKeyboardButton("🚖 Haydovchilar", callback_data="menu_drivers"),
                InlineKeyboardButton("✏️ Profil", callback_data="menu_profile"),
            ],
            [
                InlineKeyboardButton("🔗 Referal", callback_data="menu_referral"),
                InlineKeyboardButton("🧹 Tozalash", callback_data="menu_tozalash"),
            ],
        ])
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")


async def callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle main menu inline keyboard callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id
    db.upsert_user(user_id, username=query.from_user.username,
                   first_name=query.from_user.first_name)

    if data == "menu_hisobim":
        balance = db.get_balance(user_id)
        stats = db.get_user_stats(user_id)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💰 To'ldirish", callback_data="hisobim_toldir"),
                InlineKeyboardButton("📊 Tarix", callback_data="hisobim_tarix"),
            ],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="hisobim_back")],
        ])
        text = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 💳 <b>Hisobim</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 💰 Balans: <b>{balance['balance']} so'm</b>\n"
            f"║ 📈 Jami daromad: {balance['total_earned']} so'm\n"
            f"║ 📉 Jami sarflar: {balance['total_spent']} so'm\n"
            f"║ 🚖 Yo'lovchilar: {balance['passengers_count']}\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 📋 Buyurtmalar: {stats['total_orders']}\n"
            f"║ ⭐ Reyting: {stars_text(stats['rating_avg'])}\n"
            f"║ 🔗 Referallar: {stats['referrals']}\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        )
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")

    elif data == "menu_my_orders":
        orders = db.get_user_orders(user_id, limit=10)
        if not orders:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="menu_back")]])
            await query.edit_message_text(
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ 📋 Sizda hali buyurtmalar\n"
                "║ yo'q.\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                reply_markup=keyboard, parse_mode="HTML",
            )
            return
        text = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 📋 <b>Mening buyurtmalarim:</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        )
        buttons = []
        for order in orders[:5]:
            status_emoji = {"active": "🟢", "closed": "🔴", "matched": "🟡", "completed": "✅", "cancelled": "❌"}
            emoji = status_emoji.get(order["status"], "⚪")
            type_emoji = "🧑" if order["order_type"] == "passenger" else "🚗"
            text += (
                f"║ {emoji} {type_emoji} #{order['id']}\n"
                f"║ {format_route(order['from_location'], order['to_location'])}\n"
                f"║ Seats: {order['seats']} | {order['status']}\n"
                "║\n"
            )
            if order["status"] == "active":
                buttons.append(
                    [InlineKeyboardButton(f"❌ Cancel #{order['id']}",
                                          callback_data=f"cancel_{order['id']}_{user_id}")]
                )
        text += "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        back_btn = [InlineKeyboardButton("🔙 Orqaga", callback_data="menu_back")]
        back_keyboard = InlineKeyboardMarkup(buttons + [back_btn]) if buttons else InlineKeyboardMarkup([back_btn])
        await query.edit_message_text(text, reply_markup=back_keyboard, parse_mode="HTML")

    elif data == "menu_drivers":
        drivers = db.get_available_drivers()
        if not drivers:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="menu_back")]])
            await query.edit_message_text(
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ 🚖 Hozircha haydovchilar\n"
                "║ yo'q.\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                reply_markup=keyboard, parse_mode="HTML",
            )
            return
        text = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 🚖 <b>Mavjud haydovchilar:</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        )
        for d in drivers[:10]:
            name = d.get("first_name", "N/A")
            rating = stars_text(d.get("rating_avg", 0))
            car_line = car_info_line(d)
            text += f"║ ⭐ {name} | {rating}\n"
            if car_line:
                text += f"║ {car_line}\n"
            text += "║\n"
        text += "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="menu_back")]])
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")

    elif data == "menu_referral":
        count = db.get_referral_count(user_id)
        referrals = db.get_referrals(user_id)
        text = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 🔗 <b>Referal dasturi</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 👥 Do'stlar: {count}\n"
            f"║ 💰 Bonus: {count * REFERRAL_BONUS} so'm\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            "║ 📎 Referal link:\n"
            f"║ <code>https://t.me/{context.bot.username}?start={user_id}</code>\n"
            "║ Har yangi do'st uchun +2000 so'm!\n"
        )
        if referrals:
            text += "╠━━━━━━━━━━━━━━━━━━━━━╣\n║ 📋 <b>Do'stlar:</b>\n"
            for r in referrals[:5]:
                text += f"║  • {r.get('referred_name', 'User')}\n"
        text += "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="menu_back")]])
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")

    elif data == "menu_tozalash":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❌ Faol buyurtmalarni yopish", callback_data="menu_tozalash_close_active"),
                InlineKeyboardButton("📋 Eski buyurtmalar", callback_data="menu_tozalash_old_orders"),
            ],
            [
                InlineKeyboardButton("🗑️ Barchasini o'chirish", callback_data="menu_tozalash_delete_all"),
            ],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="menu_back")],
        ])
        text = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 🧹 <b>Tozalash</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            "║ ❌ <b>Faol buyurtmalarni yopish</b>\n"
            "║ — sizning faol buyurtmalar\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            "║ 📋 <b>Eski buyurtmalar</b>\n"
            "║ — 7 kunlik eski buyurtmalar\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            "║ 🗑️ <b>Barchasini o'chirish</b>\n"
            "║ — faqat admin\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        )
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")

    elif data == "menu_tozalash_close_active":
        closed = db.close_all_user_orders(user_id)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="menu_tozalash")]])
        await query.edit_message_text(
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 🧹 <b>Faol buyurtmalar yopildi!</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ ✅ Yopilgan: {closed} ta buyurtma\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
            reply_markup=keyboard, parse_mode="HTML",
        )

    elif data == "menu_tozalash_old_orders":
        old_count = db.clean_old_orders(days_old=7)
        spam_count = db.clean_spam_log(days_old=7)
        contact_count = db.clean_old_contacts(days_old=7)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="menu_tozalash")]])
        await query.edit_message_text(
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 🧹 <b>Eski ma'lumotlar tozalandi!</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 📋 Buyurtmalar: {old_count} o'chirildi\n"
            f"║ 🚫 Spam: {spam_count} o'chirildi\n"
            f"║ 📞 Kontaktlar: {contact_count} o'chirildi\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
            reply_markup=keyboard, parse_mode="HTML",
        )

    elif data == "menu_tozalash_delete_all":
        if is_admin(user_id):
            result = db.purge_all_orders()
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="menu_tozalash")]])
            await query.edit_message_text(
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ 🧹 <b>Barcha buyurtmalar o'chirildi!</b>\n"
                "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                f"║ 📋 Buyurtmalar: {result['orders']} ta\n"
                f"║ 📞 Kontaktlar: {result['contacts']} ta\n"
                f"║ 🚫 Spam: {result['spam']} ta\n"
                "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                "║ ⚠️ Admin funksiyasi\n"
                "║ — barcha ma'lumotlar tozalandi!\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                reply_markup=keyboard, parse_mode="HTML",
            )
        else:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="menu_tozalash")]])
            await query.edit_message_text(
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ ❌ <b>Ruxsat yo'q</b>\n"
                "║ Bu funksiya faqat admin\n"
                "║ uchun mavjud.\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                reply_markup=keyboard, parse_mode="HTML",
            )

    elif data == "menu_back":
        text = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 🚖 <b>Taxi Bot — Menyu</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            "║ Quyidagi bo'limni tanlang:\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💳 Hisobim", callback_data="menu_hisobim"),
                InlineKeyboardButton("📋 Buyurtmalarim", callback_data="menu_my_orders"),
            ],
            [
                InlineKeyboardButton("🚖 Haydovchilar", callback_data="menu_drivers"),
                InlineKeyboardButton("✏️ Profil", callback_data="menu_profile"),
            ],
            [
                InlineKeyboardButton("🔗 Referal", callback_data="menu_referral"),
                InlineKeyboardButton("🧹 Tozalash", callback_data="menu_tozalash"),
            ],
        ])
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")


async def callback_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin menu callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Admin emas.", show_alert=True)
        return

    if data == "admin_stats":
        stats = db.get_stats()
        text = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 📊 <b>Statistika</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 👥 Foydalanuvchilar: {stats['users_count']}\n"
            f"║ 🚖 Haydovchilar: {stats['drivers_count']}\n"
            f"║ 🟢 Faol: {stats['active_orders']}\n"
            f"║ ✅ Yakunlangan: {stats['completed_orders']}\n"
            f"║ 📊 Jami: {stats['total_orders']}\n"
            f"║ 💰 Jami daromad: {stats['total_revenue']} so'm\n"
            f"║ ⭐ O'rtacha reyting: {stars_text(stats['avg_rating'])}\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        )
        await query.edit_message_text(text, parse_mode="HTML")
    elif data == "admin_users":
        users = db.get_all_users()[:20]
        text = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 👥 <b>Foydalanuvchilar:</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        )
        for u in users[:10]:
            text += f"║  • {u.get('first_name', 'N/A')} ({u['user_id']})\n"
        text += "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        await query.edit_message_text(text, parse_mode="HTML")
    elif data == "admin_drivers":
        drivers = db.get_all_drivers()
        text = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 🚖 <b>Haydovchilar:</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        )
        for d in drivers[:10]:
            car_line = car_info_line(d)
            text += f"║ ⭐ {d.get('first_name', 'N/A')} | {stars_text(d.get('rating_avg', 0))}\n"
            if car_line:
                text += f"║ {car_line}\n"
            text += "║\n"
        text += "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        await query.edit_message_text(text, parse_mode="HTML")
    elif data == "admin_orders":
        stats = db.get_stats()
        text = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 📋 <b>Buyurtmalar:</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 🟢 Faol: {stats['active_orders']}\n"
            f"║ ✅ Yakunlangan: {stats['completed_orders']}\n"
            f"║ 📊 Jami: {stats['total_orders']}\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        )
        await query.edit_message_text(text, parse_mode="HTML")
    elif data == "admin_locs":
        locs = locations_mgr.get_all_names()
        text = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            f"║ 📍 <b>Joylashuvlar ({len(locs)}):</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        )
        for loc in locs[:20]:
            text += f"║  • {loc}\n"
        if len(locs) > 20:
            text += f"║ ... va {len(locs) - 20} ta\n"
        text += "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        await query.edit_message_text(text, parse_mode="HTML")
    elif data == "admin_broadcast":
        await query.edit_message_text(
            "📢 Broadcast: broadcast matn yuboring",
            parse_mode="HTML",
        )
    elif data == "admin_clear_pending":
        locations_mgr.clear_pending()
        await query.edit_message_text("✅ Kutilayotgan joylashuvlar tozalandi.")


# ─── Profile Callback Handlers (NEW) ─────────────────────────


async def callback_menu_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle menu_profile callback — show profile via inline menu."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    db.upsert_user(user_id, username=query.from_user.username,
                   first_name=query.from_user.first_name)

    user_info = db.get_user(user_id)
    driver_info = db.get_driver(user_id)

    if driver_info:
        car_line = car_info_line(driver_info)
        avail = "🟢 Mavjud" if driver_info.get("available", 1) else "🔴 Band"
        avg_rating = stars_text(driver_info.get("rating_avg", 0))

        text = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ ✏️ <b>Haydovchi profili</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 👤 {user_info.get('first_name', 'N/A')}\n"
            f"║ 📱 {driver_info.get('phone', 'N/A')}\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ {car_line}\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ ⭐ Reyting: {avg_rating}\n"
            f"║ 🚖 Safarlar: {driver_info.get('total_rides', 0)}\n"
            f"║ Holat: {avail}\n"
            f"║ 📍 Terminal: {driver_info.get('terminal') or 'Belgilanmagan'}\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        )

        buttons = [
            [InlineKeyboardButton("📱 Telefon", callback_data="edit_phone"),
             InlineKeyboardButton("🚗 Model", callback_data="edit_car_model")],
            [InlineKeyboardButton("🎨 Rang", callback_data="edit_car_color"),
             InlineKeyboardButton("🔢 Raqam", callback_data="edit_car_number")],
            [InlineKeyboardButton("📍 Terminal", callback_data="set_terminal"),
             InlineKeyboardButton("🔄 Yo'lovchi bo'lish", callback_data="switch_to_passenger")],
            [InlineKeyboardButton("🔙 Menyu", callback_data="menu_back")],
        ]
    else:
        text = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ ✏️ <b>Yo'lovchi profili</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 👤 {user_info.get('first_name', 'N/A')}\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            "║ 🚖 Haydovchi emas\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        )

        buttons = [
            [InlineKeyboardButton("✏️ Ismni tahrirlash", callback_data="edit_name")],
            [InlineKeyboardButton("🚖 Haydovchi bo'lish", callback_data="become_driver")],
            [InlineKeyboardButton("🔙 Menyu", callback_data="menu_back")],
        ]

    keyboard = InlineKeyboardMarkup(buttons)
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")


async def callback_edit_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle edit_* callbacks — set edit_step in user_data and prompt for new value."""
    query = update.callback_query
    await query.answer()

    data = query.data  # edit_phone, edit_car_model, edit_car_color, edit_car_number, edit_name
    field = data.replace("edit_", "")  # phone, car_model, car_color, car_number, name

    user_id = query.from_user.id

    # For edit_name on drivers, also allow it (edit their first_name)
    context.user_data["edit_step"] = field

    field_labels = {
        "phone": "📱 Telefon raqamini kiriting:",
        "car_model": "🚗 Mashina modelini kiriting:",
        "car_color": "🎨 Mashina rangini kiriting:",
        "car_number": "🔢 Mashina raqamini kiriting:",
        "name": "✏️ Ismingizni kiriting:",
    }

    prompt = field_labels.get(field, f"Yangi qiymatni kiriting ({field}):")
    await context.bot.send_message(user_id, prompt)


async def callback_become_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle become_driver callback — start driver registration flow."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if db.is_driver(user_id):
        await query.answer("✅ Siz allaqachon haydovchi!", show_alert=True)
        return

    context.user_data["driver_reg_step"] = "phone"
    keyboard = ReplyKeyboardMarkup(
        [["📱 Tel raqam yuborish"]],
        resize_keyboard=True,
    )
    await context.bot.send_message(
        user_id,
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ 🚖 <b>Haydovchi registratsiyasi</b>\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        "║ 📱 Telefon raqamini kiriting:\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def callback_menu_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle menu_back callback — navigate back to main menu."""
    query = update.callback_query
    await query.answer()

    text = (
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ 🚖 <b>Taxi Bot — Menyu</b>\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        "║ Menyu orqali bo'lim tanlang:\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💳 Hisobim", callback_data="menu_hisobim"),
            InlineKeyboardButton("📋 Buyurtmalarim", callback_data="menu_my_orders"),
        ],
        [
            InlineKeyboardButton("🚖 Haydovchilar", callback_data="menu_drivers"),
            InlineKeyboardButton("✏️ Profil", callback_data="menu_profile"),
        ],
        [
            InlineKeyboardButton("🔗 Referal", callback_data="menu_referral"),
            InlineKeyboardButton("🧹 Tozalash", callback_data="menu_tozalash"),
        ],
    ])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")


async def callback_switch_to_passenger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch from driver to passenger role."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    removed = db.remove_driver(user_id)
    if not removed:
        await query.answer("✅ Siz allaqachon yo'lovchi!", show_alert=True)
        return

    user_info = db.get_user(user_id)
    text = (
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ ✅ <b>Rol o'zgartirildi!</b>\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        f"║ 👤 {user_info.get('first_name', 'N/A')}\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        "║ 🚶 Yo'lovchi holatiga o'tdingiz\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
    )
    buttons = [
        [InlineKeyboardButton("✏️ Ismni tahrirlash", callback_data="edit_name")],
        [InlineKeyboardButton("🚖 Haydovchi bo'lish", callback_data="become_driver")],
        [InlineKeyboardButton("🔙 Menyu", callback_data="menu_back")],
    ]
    keyboard = InlineKeyboardMarkup(buttons)
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")


async def callback_set_terminal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt driver to enter their terminal/standing location."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not db.is_driver(user_id):
        await query.answer("⚠️ Siz haydovchi emas!", show_alert=True)
        return

    context.user_data["set_terminal"] = True
    await query.edit_message_text(
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ 📍 <b>Terminal belgilash</b>\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        "║ Hozir turgan joyini kiriting:\n"
        "║ (masalan: Ishtexona, Oloi,\n"
        "║ Marg'ilon, Qo'qon va h.k.)\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
        parse_mode="HTML",
    )


async def callback_approve_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin approves driver registration."""
    query = update.callback_query
    await query.answer()

    admin_id = query.from_user.id
    if admin_id not in ADMIN_IDS:
        await query.answer("⚠️ Siz admin emas!", show_alert=True)
        return

    parts = query.data.split("_")
    pending_id = int(parts[2])

    success = db.approve_pending_driver(pending_id, admin_id)
    if not success:
        await query.answer("⚠️ Bu ariza allaqachon tasdiqlangan!", show_alert=True)
        return

    pending = db.get_pending_driver(pending_id)
    driver_user_id = pending["user_id"]

    # Edit admin message
    await query.edit_message_text(
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ ✅ <b>Haydovchi tasdiqlandi!</b>\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        f"║ 👤 ID: {driver_user_id}\n"
        f"║ 📱 {pending['phone']}\n"
        f"║ 🚗 {pending['car_model']} | 🎨 {pending['car_color']}\n"
        f"║ 🔢 {pending['car_number']}\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
        parse_mode="HTML",
    )

    # Notify driver
    try:
        main_keyboard = ReplyKeyboardMarkup(
            [
                ["🚖 Buyurtma", "📋 Buyurtmalarim"],
                ["✏️ Profil", "💳 Hisobim"],
                ["⭐ Reyting", "🧹 Tozalash"],
            ],
            resize_keyboard=True,
        )
        await context.bot.send_message(
            driver_user_id,
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ ✅ <b>Haydovchi tasdiqlandi!</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 📱 Tel: {pending['phone']}\n"
            f"║ 🚗 {pending['car_model']} | 🎨 {pending['car_color']}\n"
            f"║ 🔢 {pending['car_number']}\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            "║ 🟢 Holat: Mavjud\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
            reply_markup=main_keyboard,
            parse_mode="HTML",
        )
    except Exception:
        pass


async def callback_reject_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin rejects driver registration."""
    query = update.callback_query
    await query.answer()

    admin_id = query.from_user.id
    if admin_id not in ADMIN_IDS:
        await query.answer("⚠️ Siz admin emas!", show_alert=True)
        return

    parts = query.data.split("_")
    pending_id = int(parts[2])

    success = db.reject_pending_driver(pending_id, admin_id)
    if not success:
        await query.answer("⚠️ Bu ariza allaqachon tasdiqlangan!", show_alert=True)
        return

    pending = db.get_pending_driver(pending_id)
    driver_user_id = pending["user_id"]

    # Edit admin message
    await query.edit_message_text(
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ ❌ <b>Haydovchi rad etildi!</b>\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        f"║ 👤 ID: {driver_user_id}\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
        parse_mode="HTML",
    )

    # Notify driver
    try:
        await context.bot.send_message(
            driver_user_id,
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ ❌ <b>Ariza rad etildi</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            "║ Haydovchi arizasi tasdiqlanmadi.\n"
            "║ Qayta urinib ko'ring yoki\n"
            "║ admin bilan bog'laning.\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def handle_deposit_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle payment screenshot photos sent in private chat."""
    user_id = update.effective_user.id
    if update.effective_chat.type != "private":
        return

    photo = update.message.photo
    if not photo:
        return

    # Get the largest photo (best quality)
    photo_file_id = photo[-1].file_id

    # Register user
    db.upsert_user(user_id, username=update.effective_user.username,
                   first_name=update.effective_user.first_name)

    # Create deposit record
    deposit_id = db.create_deposit(user_id, photo_file_id)

    # Send to all admins with approve/reject + amount buttons
    user = db.get_user(user_id) or {}
    admin_text = (
        f"📸 <b>Chek #{deposit_id}</b>\n"
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        f"║ 👤 {user.get('first_name', 'N/A')}\n"
        f"║ 🆔 ID: {user_id}\n"
        f"║ 📋 Holat: Kutilmoqda\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n\n"
        "✅ Qabul — summa kiriting\n"
        "❌ Rad — chek rad etiladi"
    )

    # Amount selection buttons
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 5000 so'm", callback_data=f"dep_approve_{deposit_id}_5000"),
            InlineKeyboardButton("✅ 10000 so'm", callback_data=f"dep_approve_{deposit_id}_10000"),
        ],
        [
            InlineKeyboardButton("✅ 20000 so'm", callback_data=f"dep_approve_{deposit_id}_20000"),
            InlineKeyboardButton("✅ 50000 so'm", callback_data=f"dep_approve_{deposit_id}_50000"),
        ],
        [
            InlineKeyboardButton("✅ 100000 so'm", callback_data=f"dep_approve_{deposit_id}_100000"),
            InlineKeyboardButton("📝 Boshqa summa", callback_data=f"dep_custom_{deposit_id}"),
        ],
        [InlineKeyboardButton("❌ Rad etish", callback_data=f"dep_reject_{deposit_id}")],
    ])

    for admin_id in ADMIN_IDS:
        try:
            msg = await context.bot.send_photo(
                chat_id=admin_id,
                photo=photo_file_id,
                caption=admin_text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            # Store admin message_id for later editing
            db.update_deposit_admin_msg(deposit_id, msg.message_id)
        except Exception:
            pass

    # Notify user
    await update.message.reply_text(
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ 📸 <b>Chek yuborildi!</b>\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        f"║ 📋 Chek #{deposit_id}\n"
        "║ ⏳ Admin tasdiqlashini kuting.\n"
        "║ ✅ Tasdiqlansa — hisobga\n"
        "║    pul tushadi!\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
        parse_mode="HTML",
    )


async def callback_deposit_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin approving a deposit with a specific amount."""
    query = update.callback_query
    await query.answer()

    data = query.data  # dep_approve_{id}_{amount} or dep_custom_{id}
    parts = data.split("_")

    if len(parts) == 4 and parts[1] == "approve":
        deposit_id = int(parts[2])
        amount = int(parts[3])
    elif len(parts) == 3 and parts[1] == "custom":
        # Admin needs to input a custom amount
        deposit_id = int(parts[2])
        context.user_data["dep_custom_id"] = deposit_id
        await query.edit_message_caption(
            f"📝 <b>Summa kiriting</b>\n"
            "Chek #" + str(deposit_id) + " uchun\n"
            "summani yozib yuboring (faqat raqam):\n"
            "Masalan: 25000",
            parse_mode="HTML",
        )
        return
    else:
        return

    admin_id = query.from_user.id

    # Approve the deposit — check for double approval
    success = db.approve_deposit(deposit_id, admin_id, amount)
    if not success:
        await query.answer("⚠️ Bu chek allaqachon tasdiqlangan!", show_alert=True)
        return

    # Get user info
    deposit = db.get_deposit(deposit_id)
    if not deposit:
        return
    user_id = deposit["user_id"]
    user = db.get_user(user_id) or {}
    balance = db.get_balance(user_id)

    # Update admin message
    await query.edit_message_caption(
        f"✅ <b>Tasdiqlangan chek #{deposit_id}</b>\n"
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        f"║ 👤 {user.get('first_name', 'N/A')} (ID: {user_id})\n"
        f"║ 💰 Summa: {amount} so'm\n"
        f"║ ✅ Admin tasdiqladi\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
        parse_mode="HTML",
    )

    # Notify user — balance updated immediately
    try:
        await context.bot.send_message(
            user_id,
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ ✅ <b>Chek tasdiqlandi!</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 📋 Chek #{deposit_id}\n"
            f"║ 💰 +{amount} so'm\n"
            f"║ 💳 Hisob: {balance['balance']} so'm\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def callback_deposit_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin rejecting a deposit."""
    query = update.callback_query
    await query.answer()

    data = query.data  # dep_reject_{id}
    parts = data.split("_")
    deposit_id = int(parts[2])
    admin_id = query.from_user.id

    success = db.reject_deposit(deposit_id, admin_id)
    if not success:
        await query.answer("⚠️ Bu chek allaqachon tasdiqlangan!", show_alert=True)
        return

    deposit = db.get_deposit(deposit_id)
    if not deposit:
        return
    user_id = deposit["user_id"]
    user = db.get_user(user_id) or {}

    await query.edit_message_caption(
        f"❌ <b>Rad etilgan chek #{deposit_id}</b>\n"
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        f"║ 👤 {user.get('first_name', 'N/A')} (ID: {user_id})\n"
        "║ ❌ Admin rad etdi\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
        parse_mode="HTML",
    )

    # Notify user
    try:
        await context.bot.send_message(
            user_id,
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ ❌ <b>Chek rad etildi</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 📋 Chek #{deposit_id}\n"
            "║ ❌ Admin rad etdi.\n"
            "║ Qayta urinib ko'ring!\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ─── Private Message Handlers ────────────────────────────────


async def handle_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle private text messages — deposit custom amount, profile edit FSM, then driver registration flow."""
    user_id = update.effective_user.id
    text = update.message.text.strip()

    db.upsert_user(user_id, username=update.effective_user.username,
                   first_name=update.effective_user.first_name)

    # ── Custom deposit amount (admin entering amount) ──
    if context.user_data and context.user_data.get("dep_custom_id"):
        deposit_id = context.user_data.pop("dep_custom_id")
        try:
            amount = int(text)
            if amount <= 0:
                await update.message.reply_text("❌ Summa musbat raqam bo'lishi kerak!")
                return
        except ValueError:
            await update.message.reply_text("❌ Faqat raqam kiriting! Masalan: 25000")
            context.user_data["dep_custom_id"] = deposit_id
            return

        success = db.approve_deposit(deposit_id, user_id, amount)
        if not success:
            await update.message.reply_text("⚠️ Bu chek allaqachon tasdiqlangan yoki rad etilgan!")
            return
        deposit = db.get_deposit(deposit_id)
        if deposit:
            target_user_id = deposit["user_id"]
            target_user = db.get_user(target_user_id) or {}
            target_balance = db.get_balance(target_user_id)

            await update.message.reply_text(
                f"✅ Chek #{deposit_id} tasdiqlandi!\n"
                f"💰 {amount} so'm → {target_user.get('first_name', 'N/A')}\n"
                f"💳 Hisob: {target_balance['balance']} so'm",
            )

            try:
                await context.bot.send_message(
                    target_user_id,
                    "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                    "║ ✅ <b>Chek tasdiqlandi!</b>\n"
                    "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                    f"║ 📋 Chek #{deposit_id}\n"
                    f"║ 💰 +{amount} so'm\n"
                    f"║ 💳 Hisob: {target_balance['balance']} so'm\n"
                    "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    # ── Set terminal FSM ──
    if context.user_data and context.user_data.get("set_terminal"):
        context.user_data.pop("set_terminal", None)
        terminal = text.strip()
        db.set_driver_terminal(user_id, terminal)

        driver_info = db.get_driver(user_id)
        car_line = car_info_line(driver_info)
        avail = "🟢 Mavjud" if driver_info.get("available") else "🔴 Band"

        await update.message.reply_text(
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ ✅ <b>Terminal belgilandi!</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            f"║ 📍 Terminal: {terminal}\n"
            f"║ {car_line}\n"
            f"║ Holat: {avail}\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
            parse_mode="HTML",
        )
        return

    # ── Profile edit FSM (handled BEFORE driver reg) ──
    if context.user_data and context.user_data.get("edit_step"):
        field = context.user_data["edit_step"]
        context.user_data.pop("edit_step", None)

        # Map field to DB update
        field_map = {
            "phone": "phone",
            "car_model": "car_model",
            "car_color": "car_color",
            "car_number": "car_number",
        }

        if field == "name":
            # Update user's first_name
            db.upsert_user(user_id, first_name=text)
        elif field in field_map:
            # Update driver field
            if db.is_driver(user_id):
                db.update_driver(user_id, **{field_map[field]: text})
            else:
                await update.message.reply_text("❌ Siz haydovchi emas, bu maydonni tahrirlash mumkin emas.")
                return

        # Show updated profile
        user_info = db.get_user(user_id)
        driver_info = db.get_driver(user_id)

        if driver_info:
            car_line = car_info_line(driver_info)
            avail = "🟢 Mavjud" if driver_info.get("available", 1) else "🔴 Band"
            avg_rating = stars_text(driver_info.get("rating_avg", 0))

            profile_text = (
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ ✅ <b>Profil yangilandi!</b>\n"
                "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                f"║ 👤 {user_info.get('first_name', 'N/A')}\n"
                f"║ 📱 {driver_info.get('phone', 'N/A')}\n"
                "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                f"║ {car_line}\n"
                "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                f"║ ⭐ Reyting: {avg_rating}\n"
                f"║ 🚖 Safarlar: {driver_info.get('total_rides', 0)}\n"
                f"║ Holat: {avail}\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
            )

            buttons = [
                [InlineKeyboardButton("📱 Telefon", callback_data="edit_phone"),
                 InlineKeyboardButton("🚗 Model", callback_data="edit_car_model")],
                [InlineKeyboardButton("🎨 Rang", callback_data="edit_car_color"),
                 InlineKeyboardButton("🔢 Raqam", callback_data="edit_car_number")],
                [InlineKeyboardButton("📍 Terminal", callback_data="set_terminal"),
                 InlineKeyboardButton("🔄 Yo'lovchi bo'lish", callback_data="switch_to_passenger")],
                [InlineKeyboardButton("🔙 Menyu", callback_data="menu_back")],
            ]
        else:
            profile_text = (
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ ✅ <b>Profil yangilandi!</b>\n"
                "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                f"║ 👤 {user_info.get('first_name', 'N/A')}\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
            )

            buttons = [
                [InlineKeyboardButton("✏️ Ismni tahrirlash", callback_data="edit_name")],
                [InlineKeyboardButton("🚖 Haydovchi bo'lish", callback_data="become_driver")],
                [InlineKeyboardButton("🔙 Menyu", callback_data="menu_back")],
            ]

        keyboard = InlineKeyboardMarkup(buttons)
        # Restore normal keyboard
        main_keyboard = ReplyKeyboardMarkup(
            [
                ["🚖 Buyurtma", "📋 Buyurtmalarim"],
                ["✏️ Profil", "💳 Hisobim"],
                ["⭐ Reyting", "🧹 Tozalash"],
            ],
            resize_keyboard=True,
        )
        await update.message.reply_text(profile_text, reply_markup=main_keyboard, parse_mode="HTML")
        return

    # ── Driver registration FSM ──
    if context.user_data and context.user_data.get("driver_reg_step"):
        step = context.user_data["driver_reg_step"]

        if step == "phone":
            context.user_data["driver_phone"] = text
            context.user_data["driver_reg_step"] = "car_number"
            await update.message.reply_text(
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ 🚗 Mashina raqamini kiriting\n"
                "║ (masalan: 01A123AB):\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                parse_mode="HTML",
            )
        elif step == "car_number":
            context.user_data["driver_car_number"] = text
            context.user_data["driver_reg_step"] = "car_model"
            await update.message.reply_text(
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ 🚗 Mashina modelini kiriting\n"
                "║ (masalan: Chevrolet Lacetti):\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                parse_mode="HTML",
            )
        elif step == "car_model":
            context.user_data["driver_car_model"] = text
            context.user_data["driver_reg_step"] = "car_color"
            await update.message.reply_text(
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ 🎨 Mashina rangini kiriting\n"
                "║ (masalan: Oq, Qora, Ko'k):\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                parse_mode="HTML",
            )
        elif step == "car_color":
            phone = context.user_data.get("driver_phone", "")
            car_number = context.user_data.get("driver_car_number", "")
            car_model = context.user_data.get("driver_car_model", "")
            car_color = text

            # Save to pending_drivers — admin must approve
            pending_id = db.create_pending_driver(user_id, phone, car_number, car_model, car_color)
            context.user_data.clear()

            user_info = db.get_user(user_id)
            user_name = user_info.get("first_name", "N/A") if user_info else "N/A"

            # Notify user
            await update.message.reply_text(
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ ⏳ <b>Ariza yuborildi!</b>\n"
                "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                f"║ 📱 Tel: {phone}\n"
                f"║ 🚗 {car_model} | 🎨 {car_color}\n"
                f"║ 🔢 {car_number}\n"
                "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                "║ Admin tasdiqlashini kuting...\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                parse_mode="HTML",
            )

            # Send to all admins
            admin_text = (
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ 🚖 <b>Haydovchi arizasi</b>\n"
                "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                f"║ 👤 {user_name}\n"
                f"║ 🆔 ID: {user_id}\n"
                f"║ 📱 Tel: {phone}\n"
                f"║ 🚗 {car_model} | 🎨 {car_color}\n"
                f"║ 🔢 {car_number}\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
            )
            admin_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"drv_approve_{pending_id}")],
                [InlineKeyboardButton("❌ Rad etish", callback_data=f"drv_reject_{pending_id}")],
            ])
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        admin_id, admin_text, reply_markup=admin_keyboard, parse_mode="HTML"
                    )
                except Exception:
                    pass
        return

    # Default: show comprehensive guide
    is_driver = db.is_driver(user_id)
    
    guide_text = (
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ 📖 <b>Botdan qanday foydalanish?</b>\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        "║\n"
        "║ 🧑 <b>Yo'lovchi uchun:</b>\n"
        "║ ─────────────────────\n"
        "║ 1. Guruhda buyurtma yozing:\n"
        "║    • «Ishtxonga boraman»\n"
        "║    • «Samarqandga ketaman»\n"
        "║    • «Andijondan Ishtxonga»\n"
        "║\n"
        "║ 2. Haydovchi sizni qabul qiladi\n"
        "║ 3. Kontaktlar almashinadi\n"
        "║ 4. Safar yakunlangach reyting bering\n"
        "║\n"
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
    )
    
    if is_driver:
        guide_text += (
            "║ 🚗 <b>Haydovchi uchun:</b>\n"
            "║ ─────────────────────\n"
            "║ 1. Guruhda joy e'lon qiling:\n"
            "║    • «Ishtxonga 4 ta joy bor»\n"
            "║    • «Samarqanddan 3 kishiga joy»\n"
            "║\n"
            "║ 2. Yo'lovchilar sizni qabul qiladi\n"
            "║ 3. Har bir yo'lovchiiga xabar ketadi\n"
            "║ 4. Safar yakunlangach reyting oling\n"
            "║\n"
            "║ 🟢 Holatni o'zgartirish:\n"
            "║    «🚖» tugmasini bosing\n"
            "║\n"
        )
    else:
        guide_text += (
            "║ 🚗 <b>Haydovchi bo'lish:</b>\n"
            "║ ─────────────────────\n"
            "║ «🚖» tugmasini bosib ro'yxatdan\n"
            "║ o'ting va buyurtma bering!\n"
            "║\n"
        )
    
    guide_text += (
        "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        "║ 💡 <b>Maslahatlar:</b>\n"
        "║ ─────────────────────\n"
        "║ • Aniq manzil yozing\n"
        "║ • Vaqtini ko'rsating (ixtiyoriy)\n"
        "║ • Ovozli xabar ham ishlaydi!\n"
        "║\n"
        "║ 📍 <b>Joylashuv tanlash:</b>\n"
        "║    Guruhda @t1 yozing\n"
        "║\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
    )
    
    await update.message.reply_text(guide_text, parse_mode="HTML")


async def handle_private_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle private keyboard button presses."""
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if text.startswith("✏️"):
        await cmd_profile(update, context)
    elif text.startswith("💳"):
        await cmd_hisobim(update, context)
    elif text.startswith("📋"):
        await cmd_my_orders(update, context)
    elif text.startswith("🚖"):
        if db.is_driver(user_id):
            current = db.is_driver_available(user_id)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🟢 Mavjud" if not current else "🔴 Band",
                    callback_data=f"driver_toggle_{user_id}",
                )],
            ])
            await update.message.reply_text(
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                f"║ 🚖 Haydovchi holati:\n"
                f"║ {'🟢 Mavjud' if current else '🔴 Band'}\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        else:
            # Start driver registration
            context.user_data["driver_reg_step"] = "phone"
            keyboard = ReplyKeyboardMarkup(
                [["📱 Tel raqam yuborish"]],
                resize_keyboard=True,
            )
            await update.message.reply_text(
                "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                "║ 🚖 <b>Haydovchi registratsiyasi</b>\n"
                "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                "║ 📱 Telefon raqamini kiriting:\n"
                "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                reply_markup=keyboard,
                parse_mode="HTML",
            )
    elif text.startswith("⭐"):
        avg = db.get_avg_rating(user_id)
        ratings = db.get_user_ratings(user_id, limit=5)
        text_msg = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ ⭐ <b>Reyting</b>\n"
            f"║ O'rtacha: {stars_text(avg)}\n"
        )
        if ratings:
            text_msg += "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            for r in ratings:
                text_msg += f"║ ⭐{r['rating']}/5 — {r.get('rater_name', 'User')}\n"
        text_msg += "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        await update.message.reply_text(text_msg, parse_mode="HTML")
    elif text.startswith("📍"):
        locs = locations_mgr.get_all_names()
        text_msg = (
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 📍 <b>Joylashuvlar:</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
        )
        for loc in locs[:20]:
            text_msg += f"║  • {loc}\n"
        if len(locs) > 20:
            text_msg += f"║ ... va {len(locs) - 20} ta\n"
        text_msg += "╚━━━━━━━━━━━━━━━━━━━━━╝\n"
        await update.message.reply_text(text_msg, parse_mode="HTML")
    elif text.startswith("🧹"):
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❌ Faol buyurtmalarni yopish", callback_data="menu_tozalash_close_active"),
                InlineKeyboardButton("📋 Eski buyurtmalar", callback_data="menu_tozalash_old_orders"),
            ],
            [
                InlineKeyboardButton("🗑️ Barchasini o'chirish", callback_data="menu_tozalash_delete_all"),
            ],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="menu_back")],
        ])
        await update.message.reply_text(
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 🧹 <b>Tozalash</b>\n"
            "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
            "║ Quyidagi variantlarni\n"
            "║ tanlang:\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    elif text.startswith("⚙️"):
        await cmd_help(update, context)
    elif text.startswith("🔙"):
        keyboard = ReplyKeyboardMarkup(
            [
                ["🚖 Buyurtma", "📋 Buyurtmalarim"],
                ["✏️ Profil", "💳 Hisobim"],
                ["⭐ Reyting", "🧹 Tozalash"],
            ],
            resize_keyboard=True,
        )
        await update.message.reply_text("🔙 Asosiy menyu", reply_markup=keyboard)


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle contact sharing — driver phone registration."""
    user_id = update.effective_user.id
    contact = update.message.contact

    if context.user_data and context.user_data.get("driver_reg_step") == "phone":
        phone = contact.phone_number
        context.user_data["driver_phone"] = phone
        context.user_data["driver_reg_step"] = "car_number"
        await update.message.reply_text(
            "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
            "║ 🚗 Mashina raqamini kiriting\n"
            "║ (masalan: 01A123AB):\n"
            "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
            parse_mode="HTML",
        )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle location sharing — save to active_connections."""
    user_id = update.effective_user.id
    location = update.message.location

    db.save_connection(
        user_id,
        update.effective_chat.id,
        update.message.message_id,
        is_live=location.live_location_period_seconds > 0 if hasattr(location, 'live_location_period_seconds') else False,
    )
    await update.message.reply_text(
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "║ 📍 Joylashuv qabul qilindi!\n"
        f"║ Lat: {location.latitude}\n"
        f"║ Lon: {location.longitude}\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
        parse_mode="HTML",
    )


# ─── Periodic Jobs ────────────────────────────────────────────


async def periodic_auto_close(context: ContextTypes.DEFAULT_TYPE):
    """Auto-close expired orders."""
    max_age = AUTO_CLOSE_MINUTES * 60
    closed_ids = db.close_expired_orders(max_age)

    for order_id in closed_ids:
        order = db.get_order(order_id)
        if order:
            try:
                await context.bot.send_message(
                    order["user_id"],
                    "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
                    "║ ⏰ Buyurtma avtomatik yopildi\n"
                    "╠━━━━━━━━━━━━━━━━━━━━━╣\n"
                    f"║ #{order_id}\n"
                    f"║ {format_route(order['from_location'], order['to_location'])}\n"
                    "╚━━━━━━━━━━━━━━━━━━━━━╝\n",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    if closed_ids:
        logger.info(f"Auto-closed {len(closed_ids)} expired orders")


async def periodic_terminal_board(context: ContextTypes.DEFAULT_TYPE):
    """Send/update terminal board in all groups every 15 minutes."""
    drivers = db.get_terminal_drivers()
    if not drivers:
        return

    # Build terminal board text
    text = "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
    text += "║ 🚖 <b>HAYDOVCHI TERMINALLARI</b>\n"
    text += "╠━━━━━━━━━━━━━━━━━━━━━╣\n"

    for d in drivers:
        name = d.get("first_name", "N/A")
        terminal = d.get("terminal", "?")
        car_model = d.get("car_model", "")
        car_color = d.get("car_color", "")
        car_number = d.get("car_number", "")
        if car_model or car_color or car_number:
            car_info = f"🚗{car_model} | 🎨{car_color} | 🔢{car_number}"
        else:
            car_info = ""
        text += f"║ 👤 {name} → 📍 {terminal}\n"
        if car_info:
            text += f"║   {car_info}\n"
        text += "╠━━━━━━━━━━━━━━━━━━━━━╣\n"

    # Remove last separator and add closing
    text = text.rstrip("╠━━━━━━━━━━━━━━━━━━━━━╣\n")
    text += "╚━━━━━━━━━━━━━━━━━━━━━╝\n"

    groups = db.get_all_groups()
    for group in groups:
        chat_id = group["chat_id"]
        old_msg_id = group.get("terminal_board_msg_id", 0)

        # Delete old terminal board message first
        if old_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
            except Exception:
                pass

        # Always send new message
        try:
            msg = await context.bot.send_message(
                chat_id, text, parse_mode="HTML"
            )
            db.save_terminal_board_msg(chat_id, msg.message_id)
        except Exception:
            pass

# ─── Main ─────────────────────────────────────────────────────


def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN muhit o'zgaruvchisi sozlanmagan!")
        print("❌ BOT_TOKEN muhit o'zgaruvchisi kerak!")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("balance", cmd_hisobim))
    app.add_handler(CommandHandler("hisobim", cmd_hisobim))
    app.add_handler(CommandHandler("my_orders", cmd_my_orders))
    app.add_handler(CommandHandler("drivers", cmd_drivers))
    app.add_handler(CommandHandler("referral", cmd_referral))
    app.add_handler(CommandHandler("toldir", cmd_addbal))
    app.add_handler(CommandHandler("addbal", cmd_addbal))
    app.add_handler(CommandHandler("setbal", cmd_setbal))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("addloc", cmd_addloc))
    app.add_handler(CommandHandler("pendinglocs", cmd_pendinglocs))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.GROUPS & filters.Regex(r"(?i)^@t1"),
            handle_at_t1,
        )
    )

    # Handler for @bot mention — ask "qayerdan qayerga?"
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.GROUPS,
            handle_mention_ask_route,
        ),
        group=1,  # Run before general group handler
    )

    # Handler for the user's answer to "qayerdan qayerga?"
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.GROUPS,
            handle_mention_answer_route,
        ),
        group=2,
    )

    app.add_handler(InlineQueryHandler(inline_query_locations))
    app.add_handler(CallbackQueryHandler(callback_accept, pattern=r"^accept_(passenger|driver)_\d+"))
    app.add_handler(CallbackQueryHandler(callback_select_seats, pattern=r"^select_seats_[1-4]$"))
    app.add_handler(CallbackQueryHandler(callback_cancel_order, pattern=r"^cancel_\d+_\d+"))
    app.add_handler(CallbackQueryHandler(callback_t1_page, pattern="^t1_page_"))
    app.add_handler(CallbackQueryHandler(callback_rating, pattern=r"^rate_\d+_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_complete_trip, pattern=r"^complete_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_repost, pattern=r"^repost_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_driver_toggle, pattern=r"^driver_toggle_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_hisobim_menu, pattern=r"^hisobim_"))
    # More specific patterns BEFORE generic ^menu_ so they get priority
    app.add_handler(CallbackQueryHandler(callback_menu_profile, pattern=r"^menu_profile"))
    app.add_handler(CallbackQueryHandler(callback_become_driver, pattern=r"^become_driver"))
    app.add_handler(CallbackQueryHandler(callback_switch_to_passenger, pattern=r"^switch_to_passenger"))
    app.add_handler(CallbackQueryHandler(callback_set_terminal, pattern=r"^set_terminal"))
    app.add_handler(CallbackQueryHandler(callback_approve_driver, pattern=r"^drv_approve"))
    app.add_handler(CallbackQueryHandler(callback_reject_driver, pattern=r"^drv_reject"))
    app.add_handler(CallbackQueryHandler(callback_menu, pattern=r"^menu_"))
    app.add_handler(CallbackQueryHandler(callback_admin_menu, pattern=r"^admin_"))
    # NEW: profile editing handlers
    app.add_handler(CallbackQueryHandler(callback_edit_profile, pattern=r"^edit_"))

    # Payment deposit callbacks
    app.add_handler(CallbackQueryHandler(callback_deposit_approve, pattern=r"^dep_approve"))
    app.add_handler(CallbackQueryHandler(callback_deposit_reject, pattern=r"^dep_reject"))
    app.add_handler(CallbackQueryHandler(callback_deposit_approve, pattern=r"^dep_custom"))

    # Photo handler for payment screenshots in private chat
    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.ChatType.PRIVATE,
            handle_deposit_photo,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.GROUPS,
            handle_group_message,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.VOICE & filters.ChatType.GROUPS,
            handle_voice_in_group,
        )
    )
    app.add_handler(
        MessageHandler(filters.LOCATION, handle_location)
    )
    app.add_handler(
        MessageHandler(
            filters.Regex("^(💳|📋|🚖|⭐|📍|🧹|⚙️|🔙|🟢|🔴|✏️)") & filters.ChatType.PRIVATE,
            handle_private_buttons,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.PRIVATE,
            handle_private_text,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.CONTACT & filters.ChatType.PRIVATE,
            handle_contact,
        )
    )

    job_queue = app.job_queue
    job_queue.run_repeating(periodic_auto_close, interval=300, first=60)
    job_queue.run_repeating(periodic_terminal_board, interval=900, first=120)

    logger.info("🚖 Taxi Bot ishga tushmoqda...")
    print("🚖 Taxi Bot ishlatildi!")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
