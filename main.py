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
    "💳 <b>Pul to'ldirish usullari:</b>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "📱 <b>Click:</b> 1234 5678 9012 3456\n"
    "📱 <b>Payme:</b> 9876 5432 1098 7654\n"
    "💳 <b>Humo:</b> 8765 4321 0987 6543\n"
    "🏦 <b>Bank o'tkazma:</b> NBU\n"
    "   Hisob raqam: 1234 5678 9012 3456\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "💡 To'ldirgandan so'ng admin ga screenshot yuboring."
)

# ─── Logging ──────────────────────────────────────────────────
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


# ─── Command Handlers ────────────────────────────────────────


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
            ["🚖 Buyurtma", "📋 Mening buyurtmalarim"],
            ["💳 Hisobim", "⭐ Reyting"],
            ["📍 Joylashuv", "🧹 Tozalash"],
        ],
        resize_keyboard=True,
    )

    text = (
        f"🚖 <b>Taxi Bot ga xush kelibsiz, {user.first_name}!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Guruhda buyurtma yaratish uchun:\n"
        "• Yo'lovchi: «ishtxonga boraman»\n"
        "• Haydovchi: «ishtxonnga 4 kishiga joy bor»\n\n"
        "🤖 Bot avtomatik tarzda yo'nalishni tushunadi!\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Referal link:\n"
        f"<code>https://t.me/{context.bot.username}?start={user.id}</code>\n"
        "Har bir do'stingiz uchun +2000 so'm bonus!"
    )
    menu_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💳 Hisobim", callback_data="menu_hisobim"),
            InlineKeyboardButton("📋 Buyurtmalarim", callback_data="menu_my_orders"),
        ],
        [
            InlineKeyboardButton("🚖 Haydovchilar", callback_data="menu_drivers"),
            InlineKeyboardButton("🔗 Referal", callback_data="menu_referral"),
        ],
        [
            InlineKeyboardButton("🧹 Tozalash", callback_data="menu_tozalash"),
        ],
    ])
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")
    await update.message.reply_text("👇 Menyu:", reply_markup=menu_keyboard)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message with inline keyboard menu."""
    text = (
        "🚖 <b>Taxi Bot — Yo'riqnoma</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📝 <b>Buyurtma yaratish (guruhda):</b>\n"
        "  «ishtxonga boraman» — yo'lovchi\n"
        "  «ishtxonnga joy bor» — haydovchi\n"
        "  «samarqanddan ishtxonga» — yo'nalish\n\n"
        "🎤 <b>Ovozli xabar:</b> — bot transkribatsiya qiladi\n\n"
        "📍 <b>@t1</b> — inline joylashuv tanlash\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Quyidagi menyu orqali kerakli bo'limni tanlang:"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💳 Hisobim", callback_data="menu_hisobim"),
            InlineKeyboardButton("📋 Buyurtmalarim", callback_data="menu_my_orders"),
        ],
        [
            InlineKeyboardButton("🚖 Haydovchilar", callback_data="menu_drivers"),
            InlineKeyboardButton("🔗 Referal", callback_data="menu_referral"),
        ],
        [
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
        "💳 <b>Hisobim</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Balans: <b>{balance['balance']} so'm</b>\n"
        f"📈 Jami daromad: {balance['total_earned']} so'm\n"
        f"📉 Jami sarflar: {balance['total_spent']} so'm\n"
        f"🚖 Yo'lovchilar: {balance['passengers_count']}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 Buyurtmalar: {stats['total_orders']}\n"
        f"⭐ Reyting: {stars_text(stats['rating_avg'])}\n"
        f"🔗 Referallar: {stats['referrals']}\n"
    )
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")


async def cmd_my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's active orders."""
    user_id = update.effective_user.id
    orders = db.get_user_orders(user_id, limit=10)

    if not orders:
        await update.message.reply_text("📋 Sizda hali buyurtmalar yo'q.")
        return

    text = "📋 <b>Mening buyurtmalarim:</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
    buttons = []
    for order in orders[:5]:
        status_emoji = {"active": "🟢", "closed": "🔴", "matched": "🟡", "completed": "✅", "cancelled": "❌"}
        emoji = status_emoji.get(order["status"], "⚪")
        type_emoji = "🧑" if order["order_type"] == "passenger" else "🚗"
        text += (
            f"{emoji} {type_emoji} #{order['id']} "
            f"{order['from_location']} → {order['to_location']}\n"
            f"   Seats: {order['seats']} | Status: {order['status']}\n"
        )
        if order["status"] == "active":
            buttons.append(
                [InlineKeyboardButton(f"❌ Cancel #{order['id']}",
                                      callback_data=f"cancel_{order['id']}_{user_id}")]
            )

    markup = InlineKeyboardMarkup(buttons) if buttons else None
    await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def cmd_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List available drivers."""
    drivers = db.get_available_drivers()

    if not drivers:
        await update.message.reply_text("🚖 Hozircha haydovchilar yo'q.")
        return

    text = "🚖 <b>Mavjud haydovchilar:</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
    for d in drivers[:10]:
        name = d.get("first_name", "N/A")
        rating = stars_text(d.get("rating_avg", 0))
        car = d.get("car_model", "") or d.get("car_number", "")
        text += f"⭐ {name} | {rating}\n   🚗 {car}\n\n"

    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show referral info and link."""
    user_id = update.effective_user.id
    db.upsert_user(user_id, username=update.effective_user.username,
                   first_name=update.effective_user.first_name)
    count = db.get_referral_count(user_id)
    referrals = db.get_referrals(user_id)

    text = (
        "🔗 <b>Referal dasturi</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Do'stlar: {count}\n"
        f"💰 Bonus: {count * REFERRAL_BONUS} so'm\n\n"
        f"📎 Referal link:\n"
        f"<code>https://t.me/{context.bot.username}?start={user_id}</code>\n\n"
        "Har bir yangi do'st uchun +2000 so'm!"
    )
    if referrals:
        text += "\n━━━━━━━━━━━━━━━━━━━━━\n📋 <b>Do'stlar:</b>\n"
        for r in referrals[:5]:
            text += f"  • {r.get('referred_name', 'User')}\n"

    await update.message.reply_text(text, parse_mode="HTML")


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
        "⚙️ <b>Admin Panel</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Foydalanuvchilar: {stats['users_count']}\n"
        f"🚖 Haydovchilar: {stats['drivers_count']}\n"
        f"🟢 Faol buyurtmalar: {stats['active_orders']}\n"
        f"✅ Yakunlangan: {stats['completed_orders']}\n"
        f"📊 Jami buyurtmalar: {stats['total_orders']}\n"
        f"💰 Jami daromad: {stats['total_revenue']} so'm\n"
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

    text = "📋 <b>Kutilayotgan joylashuvlar:</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
    for p in pending:
        text += f"  • {p}\n"

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
    """Handle inline queries for location selection."""
    query = update.inline_query.query.strip()
    results = []

    if not query:
        loc_names = locations_mgr.get_all_names()[:20]
    else:
        loc_names = locations_mgr.search_locations(query, limit=20)

    for i, name in enumerate(loc_names):
        results.append(
            InlineQueryResultArticle(
                id=str(i),
                title=name,
                input_message_content=InputTextMessageContent(
                    message_text=f"📍 {name}"
                ),
                description=f"Joylashuv: {name}",
            )
        )

    await update.inline_query.answer(results, cache_time=60)


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

    # Register user
    db.upsert_user(user.id, username=user.username, first_name=user.first_name)

    # Save group settings
    chat_title = update.effective_chat.title or ""
    db.save_group_settings(chat_id, chat_title, "Qizilqosh")

    # Spam check
    if db.check_spam(user.id, chat_id, SPAM_WINDOW, SPAM_MAX):
        await update.message.reply_text("⚠️ Juda ko'p buyurtma! Kuting.")
        return

    # Parse the message
    group_settings = db.get_group_settings(chat_id)
    base_location = group_settings.get("base_location", "Qizilqosh") if group_settings else "Qizilqosh"

    parsed = ai_parser.parse(text, base_location)
    if not parsed:
        return

    # Check for active duplicate order
    existing = db.get_active_order(user.id, chat_id, parsed["type"])
    if existing:
        await update.message.reply_text(
            f"⚠️ Sizda faol buyurtma bor: #{existing['id']}\n"
            f"{existing['from_location']} → {existing['to_location']}"
        )
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

    # Build order message
    type_label = "🧑 Yo'lovchi" if parsed["type"] == "passenger" else "🚗 Haydovchi"
    order_msg = (
        f"{type_label} #{order_id}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 {parsed.get('from', base_location)} → {parsed.get('to', base_location)}\n"
        f"💺 O'rindiq: {parsed.get('seats', 1)}\n"
        f"💰 ~{price} so'm\n"
    )
    if parsed.get("time"):
        order_msg += f"🕐 Soat: {parsed['time']}\n"
    order_msg += (
        f"👤 {user.first_name}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
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

    reply = await update.message.reply_text(
        order_msg, reply_markup=keyboard, parse_mode="HTML"
    )

    # Delete the user's original message (replace with bot's formatted order)
    try:
        await update.message.delete()
    except Exception:
        pass


async def handle_at_t1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle @t1 mentions in groups — show location picker."""
    text = (
        "📍 <b>Joylashuv tanlash</b>\n"
        "Inline mode'dan foydalaning:\n"
        f"@{context.bot.username} joylashuv nomi\n\n"
        "Mavjud joylashuvlar:\n"
    )
    locs = locations_mgr.get_all_names()[:15]
    for loc in locs:
        text += f"  • {loc}\n"

    await update.message.reply_text(text, parse_mode="HTML")


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
        # Check for active duplicate order
        existing = db.get_active_order(user.id, chat_id, parsed["type"])
        if existing:
            await update.message.reply_text(
                f"⚠️ Sizda faol buyurtma bor: #{existing['id']}\n"
                f"{existing['from_location']} → {existing['to_location']}"
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
            f"{type_label} #{order_id}\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎤 {transcribed}\n"
            f"📍 {parsed.get('from', base_location)} → {parsed.get('to', base_location)}\n"
            f"💺 O'rindiq: {parsed.get('seats', 1)}\n"
            f"💰 ~{price} so'm\n"
            f"👤 {user.first_name}\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
        )
        if parsed.get("time"):
            order_msg += f"🕐 Soat: {parsed['time']}\n"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Qabul", callback_data=f"accept_{parsed['type']}_{order_id}"),
                InlineKeyboardButton("❌ Bekor", callback_data=f"cancel_{order_id}_{user.id}"),
            ],
            [InlineKeyboardButton("🔄 Qayta post", callback_data=f"repost_{order_id}")],
        ])
        await update.message.reply_text(order_msg, reply_markup=keyboard, parse_mode="HTML")
    else:
        # No order parsed — just reply with transcription text
        await update.message.reply_text(f"🎤 <i>{transcribed}</i>", parse_mode="HTML")


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

    order_owner = order["user_id"]

    # Passenger accepts driver order → decrement seats
    if order_type == "driver" and acceptor_id != order_owner:
        new_seats = db.decrement_seats(order_id)
        if new_seats <= 0:
            db.update_order(order_id, status="matched")
            await query.edit_message_text(
                "✅ Buyurtma to'ldi! Barcha joylar band."
            )
        else:
            # Notify order owner about new passenger
            db.add_contact(acceptor_id, order_owner, order_id)
            try:
                await context.bot.send_message(
                    order_owner,
                    f"🧑 Yangi yo'lovchi: {query.from_user.first_name}\n"
                    f"Buyurtma #{order_id}\n"
                    f"Qolgan joy: {new_seats}",
                )
            except Exception:
                pass

            # Update message with new seat count
            type_label = "🚗 Haydovchi"
            updated_msg = (
                f"{type_label} #{order_id}\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"📍 {order['from_location']} → {order['to_location']}\n"
                f"💺 Qolgan joy: {new_seats}\n"
                f"💰 ~{order['price']} so'm\n"
                f"👤 {query.from_user.first_name} qabul qildi\n"
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
        car_info = ""
        if driver_info:
            car_info = f"🚗 {driver_info.get('car_model', '')} {driver_info.get('car_number', '')}"

        # Notify passenger
        try:
            await context.bot.send_message(
                order_owner,
                f"🚗 Haydovchi topildi!\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 {driver_name}\n"
                f"{car_info}\n"
                f"Buyurtma #{order_id}\n"
                f"{order['from_location']} → {order['to_location']}",
            )
        except Exception:
            pass

        await query.edit_message_text(
            f"✅ Haydovchi qabul qildi!\n"
            f"🚗 {driver_name} → 🧑 {order['from_location']} → {order['to_location']}\n"
            f"Buyurtma #{order_id} matched",
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
        f"⭐ Reyting berildi: {rating}/5\n"
        f"O'rtacha reyting: {stars_text(avg)}"
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
        f"✅ Trip #{order_id} yakunlandi!\n"
        f"⭐ Haydovchi reytingini bering:",
        reply_markup=keyboard,
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
        f"{type_label} #{order_id}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 {order['from_location']} → {order['to_location']}\n"
        f"💺 O'rindiq: {order['seats']}\n"
        f"💰 ~{order['price']} so'm\n"
        f"👤 {user_name}\n"
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
    await query.edit_message_text(f"🚗 Holat: {status}")


async def callback_hisobim_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle hisobim menu callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id

    if data == "hisobim_toldir":
        await query.edit_message_text(PAYMENT_INFO, parse_mode="HTML")
    elif data == "hisobim_tarix":
        transactions = db.get_transactions(user_id, limit=10)
        if not transactions:
            await query.edit_message_text("📋 Tranzaksiyalar yo'q.")
            return
        text = "📊 <b>Tranzaksiyalar:</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
        for t in transactions:
            emoji = "📈" if t["type"] == "credit" else "📉"
            text += f"{emoji} {t['amount']} so'm — {t['description']}\n"
        await query.edit_message_text(text, parse_mode="HTML")
    elif data == "hisobim_back":
        text = (
            "🚖 <b>Taxi Bot — Menyu</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Quyidagi bo'limni tanlang:"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💳 Hisobim", callback_data="menu_hisobim"),
                InlineKeyboardButton("📋 Buyurtmalarim", callback_data="menu_my_orders"),
            ],
            [
                InlineKeyboardButton("🚖 Haydovchilar", callback_data="menu_drivers"),
                InlineKeyboardButton("🔗 Referal", callback_data="menu_referral"),
            ],
            [
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
            "💳 <b>Hisobim</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Balans: <b>{balance['balance']} so'm</b>\n"
            f"📈 Jami daromad: {balance['total_earned']} so'm\n"
            f"📉 Jami sarflar: {balance['total_spent']} so'm\n"
            f"🚖 Yo'lovchilar: {balance['passengers_count']}\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 Buyurtmalar: {stats['total_orders']}\n"
            f"⭐ Reyting: {stars_text(stats['rating_avg'])}\n"
            f"🔗 Referallar: {stats['referrals']}\n"
        )
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")

    elif data == "menu_my_orders":
        orders = db.get_user_orders(user_id, limit=10)
        if not orders:
            await query.edit_message_text("📋 Sizda hali buyurtmalar yo'q.")
            return
        text = "📋 <b>Mening buyurtmalarim:</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
        buttons = []
        for order in orders[:5]:
            status_emoji = {"active": "🟢", "closed": "🔴", "matched": "🟡", "completed": "✅", "cancelled": "❌"}
            emoji = status_emoji.get(order["status"], "⚪")
            type_emoji = "🧑" if order["order_type"] == "passenger" else "🚗"
            text += (
                f"{emoji} {type_emoji} #{order['id']} "
                f"{order['from_location']} → {order['to_location']}\n"
                f"   Seats: {order['seats']} | Status: {order['status']}\n"
            )
            if order["status"] == "active":
                buttons.append(
                    [InlineKeyboardButton(f"❌ Cancel #{order['id']}",
                                          callback_data=f"cancel_{order['id']}_{user_id}")]
                )
        back_keyboard = InlineKeyboardMarkup(buttons + [[InlineKeyboardButton("🔙 Orqaga", callback_data="menu_back")]]) if buttons else InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="menu_back")]])
        await query.edit_message_text(text, reply_markup=back_keyboard, parse_mode="HTML")

    elif data == "menu_drivers":
        drivers = db.get_available_drivers()
        if not drivers:
            await query.edit_message_text("🚖 Hozircha haydovchilar yo'q.")
            return
        text = "🚖 <b>Mavjud haydovchilar:</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
        for d in drivers[:10]:
            name = d.get("first_name", "N/A")
            rating = stars_text(d.get("rating_avg", 0))
            car = d.get("car_model", "") or d.get("car_number", "")
            text += f"⭐ {name} | {rating}\n   🚗 {car}\n\n"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="menu_back")]])
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")

    elif data == "menu_referral":
        count = db.get_referral_count(user_id)
        referrals = db.get_referrals(user_id)
        text = (
            "🔗 <b>Referal dasturi</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Do'stlar: {count}\n"
            f"💰 Bonus: {count * REFERRAL_BONUS} so'm\n\n"
            f"📎 Referal link:\n"
            f"<code>https://t.me/{context.bot.username}?start={user_id}</code>\n\n"
            "Har bir yangi do'st uchun +2000 so'm!"
        )
        if referrals:
            text += "\n━━━━━━━━━━━━━━━━━━━━━\n📋 <b>Do'stlar:</b>\n"
            for r in referrals[:5]:
                text += f"  • {r.get('referred_name', 'User')}\n"
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
            "🧹 <b>Tozalash</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "❌ <b>Faol buyurtmalarni yopish</b> — sizning faol buyurtmalaringizni yopadi\n"
            "📋 <b>Eski buyurtmalar</b> — 7 kunlik eski buyurtmalarni o'chiradi\n"
            "🗑️ <b>Barchasini o'chirish</b> — faqat admin (barcha buyurtmalar o'chadi)"
        )
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")

    elif data == "menu_tozalash_close_active":
        closed = db.close_all_user_orders(user_id)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="menu_tozalash")]])
        await query.edit_message_text(
            f"🧹 <b>Faol buyurtmalar yopildi!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Yopilgan: {closed} ta buyurtma",
            reply_markup=keyboard, parse_mode="HTML",
        )

    elif data == "menu_tozalash_old_orders":
        old_count = db.clean_old_orders(days_old=7)
        spam_count = db.clean_spam_log(days_old=7)
        contact_count = db.clean_old_contacts(days_old=7)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="menu_tozalash")]])
        await query.edit_message_text(
            f"🧹 <b>Eski ma'lumotlar tozalandi!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 Buyurtmalar: {old_count} ta o'chirildi\n"
            f"🚫 Spam: {spam_count} ta o'chirildi\n"
            f"📞 Kontaktlar: {contact_count} ta o'chirildi",
            reply_markup=keyboard, parse_mode="HTML",
        )

    elif data == "menu_tozalash_delete_all":
        if is_admin(user_id):
            result = db.purge_all_orders()
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="menu_tozalash")]])
            await query.edit_message_text(
                f"🧹 <b>Barcha buyurtmalar o'chirildi!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 Buyurtmalar: {result['orders']} ta\n"
                f"📞 Kontaktlar: {result['contacts']} ta\n"
                f"🚫 Spam: {result['spam']} ta\n"
                f"⚠️ Admin funksiyasi — barcha ma'lumotlar tozalandi!",
                reply_markup=keyboard, parse_mode="HTML",
            )
        else:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="menu_tozalash")]])
            await query.edit_message_text(
                "❌ <b>Ruxsat yo'q</b>\n"
                "Bu funksiya faqat admin uchun mavjud.\n"
                "Siz faqat o'z buyurtmalaringizni yopishingiz mumkin.",
                reply_markup=keyboard, parse_mode="HTML",
            )

    elif data == "menu_back":
        text = (
            "🚖 <b>Taxi Bot — Yo'riqnoma</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "📝 <b>Buyurtma yaratish (guruhda):</b>\n"
            "  «ishtxonga boraman» — yo'lovchi\n"
            "  «ishtxonnga joy bor» — haydovchi\n"
            "  «samarqanddan ishtxonga» — yo'nalish\n\n"
            "🎤 <b>Ovozli xabar:</b> — bot transkribatsiya qiladi\n\n"
            "📍 <b>@t1</b> — inline joylashuv tanlash\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Quyidagi menyu orqali kerakli bo'limni tanlang:"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💳 Hisobim", callback_data="menu_hisobim"),
                InlineKeyboardButton("📋 Buyurtmalarim", callback_data="menu_my_orders"),
            ],
            [
                InlineKeyboardButton("🚖 Haydovchilar", callback_data="menu_drivers"),
                InlineKeyboardButton("🔗 Referal", callback_data="menu_referral"),
            ],
            [
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
            "📊 <b>Statistika</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Foydalanuvchilar: {stats['users_count']}\n"
            f"🚖 Haydovchilar: {stats['drivers_count']}\n"
            f"🟢 Faol: {stats['active_orders']}\n"
            f"✅ Yakunlangan: {stats['completed_orders']}\n"
            f"📊 Jami: {stats['total_orders']}\n"
            f"💰 Jami daromad: {stats['total_revenue']} so'm\n"
            f"⭐ O'rtacha reyting: {stars_text(stats['avg_rating'])}\n"
        )
        await query.edit_message_text(text, parse_mode="HTML")
    elif data == "admin_users":
        users = db.get_all_users()[:20]
        text = "👥 <b>Foydalanuvchilar:</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
        for u in users[:10]:
            text += f"  • {u.get('first_name', 'N/A')} ({u['user_id']})\n"
        await query.edit_message_text(text, parse_mode="HTML")
    elif data == "admin_drivers":
        drivers = db.get_all_drivers()
        text = "🚖 <b>Haydovchilar:</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
        for d in drivers[:10]:
            text += (
                f"  ⭐ {d.get('first_name', 'N/A')} "
                f"| {stars_text(d.get('rating_avg', 0))}\n"
            )
        await query.edit_message_text(text, parse_mode="HTML")
    elif data == "admin_orders":
        stats = db.get_stats()
        text = (
            "📋 <b>Buyurtmalar statistikasi:</b>\n"
            f"🟢 Faol: {stats['active_orders']}\n"
            f"✅ Yakunlangan: {stats['completed_orders']}\n"
            f"📊 Jami: {stats['total_orders']}\n"
        )
        await query.edit_message_text(text, parse_mode="HTML")
    elif data == "admin_locs":
        locs = locations_mgr.get_all_names()
        text = f"📍 <b>Joylashuvlar ({len(locs)}):</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
        for loc in locs[:20]:
            text += f"  • {loc}\n"
        if len(locs) > 20:
            text += f"\n  ... va {len(locs) - 20} ta"
        await query.edit_message_text(text, parse_mode="HTML")
    elif data == "admin_broadcast":
        await query.edit_message_text(
            "📢 Broadcast: broadcast matn yuboring",
            parse_mode="HTML",
        )
    elif data == "admin_clear_pending":
        locations_mgr.clear_pending()
        await query.edit_message_text("✅ Kutilayotgan joylashuvlar tozalandi.")


# ─── Private Message Handlers ────────────────────────────────


async def handle_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle private text messages — driver registration flow."""
    user_id = update.effective_user.id
    text = update.message.text.strip()

    db.upsert_user(user_id, username=update.effective_user.username,
                   first_name=update.effective_user.first_name)

    # Check if in driver registration flow
    if context.user_data and context.user_data.get("driver_reg_step"):
        step = context.user_data["driver_reg_step"]

        if step == "phone":
            context.user_data["driver_phone"] = text
            context.user_data["driver_reg_step"] = "car_number"
            await update.message.reply_text(
                "🚗 Mashina raqamini kiriting (masalan: 01A123AB):"
            )
        elif step == "car_number":
            context.user_data["driver_car_number"] = text
            context.user_data["driver_reg_step"] = "car_model"
            await update.message.reply_text(
                "🚗 Mashina modelini kiriting (masalan: Chevrolet Lacetti):"
            )
        elif step == "car_model":
            phone = context.user_data.get("driver_phone", "")
            car_number = context.user_data.get("driver_car_number", "")
            car_model = text

            db.register_driver(user_id, phone, car_number, car_model)
            context.user_data.clear()

            await update.message.reply_text(
                f"️ <b>Haydovchi ro'yxatdan o'tdi!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"📱 Tel: {phone}\n"
                f"🚗 {car_model} | {car_number}\n\n"
                "🟢 Holat: Mavjud\n"
                "Toggle: drivers",
                parse_mode="HTML",
            )
        return

    # Default: show help
    await update.message.reply_text(
        "🚖 Buyurtmalar guruhda yaratiladi.\n"
        "help — yo'riqnoma"
    )


async def handle_private_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle private keyboard button presses."""
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if text.startswith("💳"):
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
                f"🚖 Haydovchi holati: {'🟢 Mavjud' if current else '🔴 Band'}",
                reply_markup=keyboard,
            )
        else:
            # Start driver registration
            context.user_data["driver_reg_step"] = "phone"
            keyboard = ReplyKeyboardMarkup(
                [["📱 Tel raqam yuborish"]],
                resize_keyboard=True,
            )
            await update.message.reply_text(
                "🚖 <b>Haydovchi registratsiyasi</b>\n"
                "📱 Telefon raqamini kiriting:",
                reply_markup=keyboard,
                parse_mode="HTML",
            )
    elif text.startswith("⭐"):
        avg = db.get_avg_rating(user_id)
        ratings = db.get_user_ratings(user_id, limit=5)
        text_msg = (
            f"⭐ <b>Reyting</b>\n"
            f"O'rtacha: {stars_text(avg)}\n"
        )
        if ratings:
            text_msg += "━━━━━━━━━━━━━━━━━━━━━\n"
            for r in ratings:
                text_msg += f"  ⭐{r['rating']}/5 — {r.get('rater_name', 'User')}\n"
        await update.message.reply_text(text_msg, parse_mode="HTML")
    elif text.startswith("📍"):
        locs = locations_mgr.get_all_names()
        text_msg = "📍 <b>Joylashuvlar:</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
        for loc in locs[:20]:
            text_msg += f"  • {loc}\n"
        if len(locs) > 20:
            text_msg += f"\n  ... va {len(locs) - 20} ta"
        await update.message.reply_text(text_msg, parse_mode="HTML")
    elif text.startswith("🧹"):
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❌ Faol buyurtmalarni yopish", callback_data="menu_tozalash_close_active"),
                InlineKeyboardButton("📋 Eski buyurtmalar", callback_data="menu_tozalash_old_orders"),
            ],
            [
                InlineKeyboardButton("🗑️ Barcha ma'lumotlarni o'chirish", callback_data="menu_tozalash_delete_all"),
            ],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="menu_back")],
        ])
        await update.message.reply_text(
            "🧹 <b>Tozalash — Ma'lumotlarni tochalk</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Quyidagi variantlarni tanlang:",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    elif text.startswith("⚙️"):
        await cmd_help(update, context)
    elif text.startswith("🔙"):
        keyboard = ReplyKeyboardMarkup(
            [
                ["🚖 Buyurtma", "📋 Mening buyurtmalarim"],
                ["💳 Hisobim", "⭐ Reyting"],
                ["📍 Joylashuv", "🧹 Tozalash"],
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
            "🚗 Mashina raqamini kiriting (masalan: 01A123AB):"
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
        f"📍 Joylashuv qabul qilindi!\n"
        f"Lat: {location.latitude}, Lon: {location.longitude}"
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
                    f"⏰ Buyurtma #{order_id} avtomatik yopildi.\n"
                    f"{order['from_location']} → {order['to_location']}",
                )
            except Exception:
                pass

    if closed_ids:
        logger.info(f"Auto-closed {len(closed_ids)} expired orders")

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

    app.add_handler(InlineQueryHandler(inline_query_locations))
    app.add_handler(CallbackQueryHandler(callback_accept, pattern=r"^accept_(passenger|driver)_\d+"))
    app.add_handler(CallbackQueryHandler(callback_cancel_order, pattern=r"^cancel_\d+_\d+"))
    app.add_handler(CallbackQueryHandler(callback_t1_page, pattern="^t1_page_"))
    app.add_handler(CallbackQueryHandler(callback_rating, pattern=r"^rate_\d+_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_complete_trip, pattern=r"^complete_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_repost, pattern=r"^repost_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_driver_toggle, pattern=r"^driver_toggle_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_hisobim_menu, pattern=r"^hisobim_"))
    app.add_handler(CallbackQueryHandler(callback_menu, pattern=r"^menu_"))
    app.add_handler(CallbackQueryHandler(callback_admin_menu, pattern=r"^admin_"))

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
            filters.Regex("^(💳|📋|🚖|⭐|📍|🧹|⚙️|🔙|🟢|🔴)") & filters.ChatType.PRIVATE,
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

    logger.info("🚖 Taxi Bot ishga tushmoqda...")
    print("🚖 Taxi Bot ishlatildi!")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
