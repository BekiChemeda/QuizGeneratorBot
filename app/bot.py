import time
from datetime import datetime, timedelta, timezone
from pymongo.errors import DuplicateKeyError
from telebot import TeleBot
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    Message,
)
from bson import ObjectId

from .config import get_config
from .db import init_db, get_db
from .repositories.settings import SettingsRepository
from .repositories.users import UsersRepository
from .repositories.channels import ChannelsRepository
from .repositories.payments import PaymentsRepository
from .repositories.schedules import SchedulesRepository
from .repositories.stats import StatsRepository
from .repositories.quizzes import QuizzesRepository
from .services.exporter import QuizExporter
from .services.gemini import generate_questions, validate_gemini_api_key
from .services.file_parser import fetch_and_parse_file, chunk_text

from .services.youtube_service import get_youtube_transcript
from .services.quota import (
    has_quota,
    can_submit_note_now,
    update_last_note_time,
    reset_notes_if_new_day,
    increment_quota,
    increase_total_notes,
    is_premium
)
from .services.scheduler import QuizScheduler
from .services.scheduler import QuizScheduler
from .utils import is_subscribed, home_keyboard, format_dt_utc3, from_utc3_to_utc, notify_admins
from .logger import logger
import traceback
import functools


cfg = get_config()
# Defer DB connection until runtime; guard when Mongo is unavailable
try:
    init_db()
    db = get_db()
except Exception as e:
    logger.error(f"Database connection failed: {e}")
    logger.error(traceback.format_exc())
    db = None
    # Fail fast if DB is critical? For now just log.
    print(f"CRITICAL: Database failed to initialize: {e}")

settings_repo = SettingsRepository(db) if db is not None else None
users_repo = UsersRepository(db) if db is not None else None
channels_repo = ChannelsRepository(db) if db is not None else None
payments_repo = PaymentsRepository(db) if db is not None else None
payments_repo = PaymentsRepository(db) if db is not None else None
schedules_repo = SchedulesRepository(db) if db is not None else None
quizzes_repo = QuizzesRepository(db) if db is not None else None

bot = TeleBot(cfg.bot_token)
if db is not None:
    scheduler = QuizScheduler(db, bot)
    scheduler.start()

pending_notes: dict[int, dict] = {}
pending_subscriptions: dict[int, dict] = {}
pending_keys: dict[int, dict] = {}


def error_handler(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            user_id = None
            if args and isinstance(args[0], (Message, CallbackQuery)):
                user_id = args[0].from_user.id
            
            error_msg = f"Error in {func.__name__}: {str(e)}"
            logger.error(error_msg)
            logger.error(traceback.format_exc())
            
            if db is not None:
                notify_admins(bot, f"⚠️ Error in `{func.__name__}`:\n`{str(e)}`", db)
            
            if user_id:
                try:
                    bot.send_message(user_id, "An unexpected error occurred. The admins have been notified.")
                except Exception:
                    pass
    return wrapper


def main_menu(user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    # Admin / Owner check
    user = users_repo.get(user_id) if users_repo else None
    if user and (user.get("role") == "admin" or user_id == cfg.owner_id):
        kb.add(InlineKeyboardButton("🛠 Admin Manage", callback_data="admin_menu"))
    kb.add(
        InlineKeyboardButton("📝 Generate", callback_data="generate"),
        InlineKeyboardButton("👤 Profile", callback_data="profile"),
        InlineKeyboardButton("📢 My Channels", callback_data="channels"),
        InlineKeyboardButton("📂 My Quizzes", callback_data="my_quizzes"),
        InlineKeyboardButton("⏰ Schedule", callback_data="schedule_menu"),
        InlineKeyboardButton("📊 Features", callback_data="features"),
        InlineKeyboardButton("ℹ️ About", callback_data="about"),
        InlineKeyboardButton("🆘 FAQs", callback_data="faq"),
        InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
    )
    kb.add(InlineKeyboardButton("👨‍💻 Developer", url="https://t.me/Bek_i"))
    return kb


@bot.message_handler(commands=["start"]) 
@error_handler
def handle_start(message: Message):
    user_id = message.chat.id
    # Use full name or username for display
    username = message.from_user.username
    display_name = message.from_user.first_name or username or "Someone"
    
    # Check for referral args
    parts = message.text.split()
    referrer_id = None
    if len(parts) > 1 and parts[1].startswith("ref"):
        try:
            referrer_id = int(parts[1][3:])
        except ValueError:
            pass

    users_repo.upsert_user(user_id, username)

    # Process Referral
    if referrer_id:
        if users_repo.set_referrer(user_id, referrer_id):
            # Notify referrer
            try:
                bot.send_message(referrer_id, f"🎉 New user {display_name} joined via your link!")
                # Check for milestone rewards
                users_repo.check_and_reward_referral_milestone(referrer_id, bot, settings_repo)
            except Exception:
                pass # Referral notification failed (blocked bot etc)

    if cfg.maintenance_mode:
        bot.send_message(user_id, "The bot is currently under maintenance. Please try again later.")
        return

    if not is_subscribed(bot, user_id):
        channels_txt = "\n".join(cfg.force_channels) if cfg.force_channels else ""
        bot.send_message(user_id, f"Please join required channels to use the bot:\n{channels_txt}")
        return

    text = (
        "<b>Welcome to SmartQuiz Bot!</b>\n\n"
        "Turn your notes into interactive questions effortlessly.\n\n"
        "✨ Features:\n"
        "- Convert study notes into quizzes\n"
        "- Choose between text or quiz mode\n"
        "- Deliver to PM or your channel\n"
        "- Configure delay and schedule delivery\n\n"
        f"Your referral link: https://t.me/{bot.get_me().username}?start=ref{user_id}\n"
        "Invite 2 users to get Premium!\n\n"
        "Your support makes this bot better!"
    )
    bot.send_message(user_id, text, parse_mode="HTML", reply_markup=main_menu(user_id), disable_web_page_preview=True)


@bot.callback_query_handler(func=lambda call: call.data == "home")
def handle_home(call: CallbackQuery):
    user_id = call.from_user.id
    pending_notes.pop(user_id, None)
    try:
        bot.edit_message_text(
            "🏠 **Home**\nSelect an option below:", 
            call.message.chat.id, 
            call.message.message_id, 
            parse_mode="Markdown", 
            reply_markup=main_menu(user_id)
        )
    except Exception:
        bot.send_message(user_id, "🏠 **Home**", parse_mode="Markdown", reply_markup=main_menu(user_id))


@bot.callback_query_handler(func=lambda call: call.data == "faq")
def handle_faq(call: CallbackQuery):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    text = (
        "📚 Frequently Asked Questions (FAQs)\n\n"
        "1) Why limits? Resource management.\n"
        "2) 24/7? Use a VPS for always-on.\n"
        "3) Why slow? Free hosting limits.\n"
        "4) Updates? Yes, more features coming.\n"
        "5) Note size? Up to Telegram limits (~4096 chars).\n"
        "6) AI? Gemini by Google.\n"
        "7) Poll mode? Settings → Question Type → Poll.\n"
    )
    bot.send_message(call.message.chat.id, text, reply_markup=home_keyboard()) # Using home_keyboard (Back to Home) is safer here as it likely uses utils.py logic? No, let's stick to utils home_keyboard for sub-menus, or main_menu(user_id)? 
    # Actually, FAQ usually has a 'Back' button, which is home_keyboard().
    # The error is explicitly main_menu() calls.
    # Let me check where main_menu() is called.

@bot.callback_query_handler(func=lambda call: call.data == "about")
def handle_about(call: CallbackQuery):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    text = (
        "ℹ️ <b>About the Bot</b>\n\n"
        "🤖 Version: <b><i>v2.0.0</i></b>\n"
        "📚 Converts your text notes into MCQ quizzes.\n"
        "🎓 For students, educators, creators.\n\n"
        "🛠 New: MongoDB, user channels, delay, scheduling.\n"
    )
    bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=home_keyboard())


@bot.callback_query_handler(func=lambda call: call.data == "features")
def handle_features(call: CallbackQuery):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    
    text = (
        "📊 <b>Feature Comparison</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🆓 FREE USERS</b>\n"
        "• 5 quizzes per day\n"
        "• Text notes only\n"
        "• PDF/TXT/DOCX files\n"
        "• Up to 100 questions\n"
        "• View last 2 saved quizzes\n"
        "• Text or Poll mode\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🔑 WITH CUSTOM API KEY</b>\n"
        "• 50 quizzes per day\n"
        "• All free features\n"
        "• Up to 300 questions\n"
        "• No Gemini API costs from bot\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>⭐ PREMIUM USERS</b>\n"
        "• 10 quizzes per day\n"
        "• All free features\n"
        "• 🎥 YouTube video quizzes\n"
        "• 🎵 Audio file quizzes\n"
        "• PPT/PPTX support\n"
        "• Up to 150 questions\n"
        "• View all saved quizzes\n"
        "• Export to PDF/DOCX/TXT\n"
        "• Priority support\n\n"
        "💡 <i>Get Premium by inviting friends or subscribing!</i>"
    )
    
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💎 Get Premium", callback_data="subscribe_premium"))
    kb.add(InlineKeyboardButton("🔙 Home", callback_data="home"))
    
    bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=kb)


@bot.message_handler(commands=["addadmin"])
def handle_add_admin(message: Message):
    if message.from_user.id != cfg.owner_id:
        return
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Usage: /addadmin <user_id>")
            return
        target_id = int(args[1])
        users_repo.set_admin(target_id)
        bot.reply_to(message, f"User {target_id} is now an admin.")
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")


def admin_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📊 Settings Overview", callback_data="admin_settings_overview"),
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
        InlineKeyboardButton("🔐 Force Subscription", callback_data="admin_manage_sub"),
        InlineKeyboardButton("💰 Set Premium Price", callback_data="admin_set_price"),
        InlineKeyboardButton("👥 Manage Users", callback_data="admin_users"),
        InlineKeyboardButton("🔙 Close", callback_data="close_admin"),
    )
    return kb


@bot.callback_query_handler(func=lambda call: call.data == "admin_menu")
def handle_admin_menu_btn(call: CallbackQuery):
    user_id = call.from_user.id
    # Auth Check
    admin = users_repo.get(user_id)
    is_owner = (user_id == cfg.owner_id)
    if not is_owner and (not admin or admin.get("role") != "admin"):
        bot.answer_callback_query(call.id, "Not authorized.")
        return
    
    # Admin Dashboard Menu
    kb = admin_keyboard()
    
    try:
        bot.edit_message_text("🔧 **Admin Dashboard**", call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        bot.send_message(user_id, "🔧 **Admin Dashboard**", parse_mode="Markdown", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data == "admin_manage_sub")
def handle_admin_manage_sub(call: CallbackQuery):
    user_id = call.from_user.id
    # Auth Check
    admin = users_repo.get(user_id)
    is_owner = (user_id == cfg.owner_id)
    if not is_owner and (not admin or admin.get("role") != "admin"):
        bot.answer_callback_query(call.id, "Not authorized.")
        return

    # Get settings
    sr = SettingsRepository(db)
    force = sr.get("force_subscription", cfg.force_subscription)
    channels = sr.get("force_channels", cfg.force_channels)
    
    status_icon = "mV" if force else "❌"
    toggle_btn_text = "Disable Force Sub" if force else "Enable Force Sub"
    
    text = (
        f"🔐 **Force Subscription Management**\n\n"
        f"Status: {status_icon} **{'Enabled' if force else 'Disabled'}**\n"
        f"Channels Required: {len(channels)}"
    )
    
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton(toggle_btn_text, callback_data="admin_toggle_force"))
    
    for ch in channels:
        kb.add(InlineKeyboardButton(f"❌ Remove {ch}", callback_data=f"admin_rm_sub_{ch}"))
        
    kb.add(InlineKeyboardButton("➕ Add Channel", callback_data="admin_add_sub_prompt"))
    kb.add(InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_menu"))
    
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        bot.send_message(user_id, text, parse_mode="Markdown", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data == "admin_toggle_force")
def toggle_force_sub(call: CallbackQuery):
    user_id = call.from_user.id
    # Auth Check
    admin = users_repo.get(user_id)
    is_owner = (user_id == cfg.owner_id)
    if not is_owner and (not admin or admin.get("role") != "admin"):
        bot.answer_callback_query(call.id)
        return

    sr = SettingsRepository(db)
    current = sr.get("force_subscription", cfg.force_subscription)
    sr.set("force_subscription", not current)
    handle_admin_manage_sub(call)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_rm_sub_"))
def remove_force_channel(call: CallbackQuery):
    channel = call.data.replace("admin_rm_sub_", "")
    sr = SettingsRepository(db)
    channels = sr.get("force_channels", cfg.force_channels)
    if channel in channels:
        channels.remove(channel)
        sr.set("force_channels", channels)
    handle_admin_manage_sub(call)

@bot.callback_query_handler(func=lambda call: call.data == "admin_add_sub_prompt")
def prompt_add_force_channel(call: CallbackQuery):
    user_id = call.from_user.id
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    msg = bot.send_message(user_id, "Send the channel @username (bot must be admin there).")
    pending_notes[user_id] = {"stage": "await_admin_force_channel", "last_msg_id": msg.message_id}
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: m.from_user and m.from_user.id in pending_notes and pending_notes[m.from_user.id].get("stage") == "await_admin_force_channel")
def handle_add_force_channel_msg(message: Message):
    user_id = message.from_user.id
    channel = message.text.strip()
    if not channel.startswith("@"):
        bot.reply_to(message, "Invalid format. Use @channelname.")
        return
        
    # Delete prompt and user message
    state = pending_notes.get(user_id)
    if state and "last_msg_id" in state:
        try:
            bot.delete_message(user_id, state["last_msg_id"])
        except:
            pass
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except:
        pass

    sr = SettingsRepository(db)
    channels = sr.get("force_channels", cfg.force_channels)
    if channel not in channels:
        channels.append(channel)
        sr.set("force_channels", channels)
    
    pending_notes.pop(user_id, None)
    bot.reply_to(message, f"Added {channel} to required channels.")
    
    # Show menu again
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔙 Manage Subs", callback_data="admin_manage_sub"))
    bot.send_message(user_id, "Channel added.", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "admin_settings_overview")
def handle_admin_settings_overview(call: CallbackQuery):
    user_id = call.from_user.id
    # Auth Check
    admin = users_repo.get(user_id)
    is_owner = (user_id == cfg.owner_id)
    if not is_owner and (not admin or admin.get("role") != "admin"):
        bot.answer_callback_query(call.id, "Not authorized.")
        return
    
    # Get all settings
    sr = SettingsRepository(db) if db is not None else None
    
    # Premium & Payment
    premium_price = sr.get("premium_price", cfg.premium_price) if sr else cfg.premium_price
    payment_channel = sr.get("payment_channel", cfg.payment_channel) if sr else cfg.payment_channel
    
    # Force Subscription
    force_sub = sr.get("force_subscription", cfg.force_subscription) if sr else cfg.force_subscription
    force_channels = sr.get("force_channels", cfg.force_channels) if sr else cfg.force_channels
    
    # Limits
    max_notes_regular = sr.get("max_notes_regular", cfg.max_notes_regular) if sr else cfg.max_notes_regular
    max_notes_premium = sr.get("max_notes_premium", cfg.max_notes_premium) if sr else cfg.max_notes_premium
    max_notes_custom = sr.get("max_notes_custom_key", cfg.max_notes_custom_key) if sr else cfg.max_notes_custom_key
    
    max_q_regular = sr.get("max_questions_regular", cfg.max_questions_regular) if sr else cfg.max_questions_regular
    max_q_premium = sr.get("max_questions_premium", cfg.max_questions_premium) if sr else cfg.max_questions_premium
    max_q_custom = sr.get("max_questions_custom_key", cfg.max_questions_custom_key) if sr else cfg.max_questions_custom_key
    
    # Referral (future feature - placeholder)
    referral_target = sr.get("referral_target", 2) if sr else 2
    referral_reward_days = sr.get("referral_reward_days", 30) if sr else 30
    
    text = (
        "📊 **Current Settings Overview**\n\n"
        "**💰 Premium & Payment:**\n"
        f"• Premium Price: {premium_price} ETB\n"
        f"• Payment Channel: {payment_channel or 'Not set'}\n\n"
        "**🔐 Force Subscription:**\n"
        f"• Status: {'✅ Enabled' if force_sub else '❌ Disabled'}\n"
        f"• Required Channels: {len(force_channels)}\n"
        f"  {', '.join(force_channels) if force_channels else 'None'}\n\n"
        "**📊 Daily Limits (Quizzes):**\n"
        f"• Free Users: {max_notes_regular}\n"
        f"• Premium Users: {max_notes_premium}\n"
        f"• Custom API Key: {max_notes_custom}\n\n"
        "**❓ Max Questions Per Quiz:**\n"
        f"• Free Users: {max_q_regular}\n"
        f"• Premium Users: {max_q_premium}\n"
        f"• Custom API Key: {max_q_custom}\n\n"
        "**🎁 Referral Rewards:**\n"
        f"• Target: {referral_target} invites\n"
        f"• Reward: {referral_reward_days} days premium\n\n"
        "_Use the admin menu buttons to modify these settings._"
    )
    
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_menu"))
    
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        bot.send_message(user_id, text, parse_mode="Markdown", reply_markup=kb)


@bot.message_handler(commands=["addpremium"])
def handle_add_premium(message: Message):
    user_id = message.from_user.id
    user = users_repo.get(user_id)
    if not user or (user.get("role") != "admin" and user_id != cfg.owner_id):
        return

    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Usage: /addpremium <user_id> [days]")
            return
        
        target_id = int(args[1])
        duration = int(args[2]) if len(args) > 2 else None
        
        users_repo.set_premium(target_id, duration)
        dur_str = f"{duration} days" if duration else "Permanent"
        bot.reply_to(message, f"User {target_id} is now Premium ({dur_str}).")
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")



# Payment Handlers (Telegram Stars)
@bot.callback_query_handler(func=lambda call: call.data == "upgrade_premium")
def handle_upgrade_premium(call: CallbackQuery):
    user_id = call.from_user.id
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    title = "Premium Subscription (30 Days)"
    description = "Unlock all features: Unlimited quizzes (fair use), YouTube/Audio support, Exports, higher limits."
    payload = f"premium_30_{user_id}"
    currency = "XTR" # Stars
    price = 100 # 100 Stars (example price, can be configured)
    
    # Note: send_invoice for Stars (Digital Goods) requires 'XTR' currency
    # and provider_token is usually empty for Stars if using BotFather's setup for Stars?
    # Actually, for Stars specifically, we use a specific price object labeled in XTR.
    # Telebot 4.22 supports Stars via `LabeledPrice` with amount.
    
    from telebot.types import LabeledPrice
    prices = [LabeledPrice(label="30 Days Premium", amount=price)]
    
    try:
        bot.send_invoice(
            chat_id=user_id,
            title=title,
            description=description,
            invoice_payload=payload,
            provider_token="", # Empty for Stars
            currency="XTR",
            prices=prices,
            start_parameter="premium-upgrade"
        )
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error creating invoice: {e}", show_alert=True)


@bot.pre_checkout_query_handler(func=lambda query: True)
def checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    payment = message.successful_payment
    user_id = message.from_user.id
    payload = payment.invoice_payload # e.g. "premium_30_123"
    
    if payload.startswith("premium_"):
        parts = payload.split("_")
        days = int(parts[1])
        users_repo.set_premium(user_id, days)
        bot.send_message(user_id, f"🎉 Payment successful! You assume Premium status for {days} days.\nReference: {payment.telegram_payment_charge_id}")
        # Notify admins?
        notify_admins(bot, f"💰 New Payment (Stars) from {message.from_user.full_name}: {payment.total_amount} XTR", db)



@bot.callback_query_handler(func=lambda call: call.data == "profile")
def handle_profile(call: CallbackQuery):
    user_id = call.from_user.id
    user = users_repo.get(user_id) or {}
    
    status = "🌟 Premium" if is_premium(user) else "Regular"
    role = user.get("role", "user").capitalize()
    
    # Quota info
    today_str = datetime.now().strftime("%Y-%m-%d")
    used_today = user.get("daily_count", 0)
    last_date = user.get("last_note_date", "")
    if last_date != today_str:
        used_today = 0
        
    if is_premium(user):
        limit_notes = cfg.max_notes_premium
        limit_questions = cfg.max_questions_premium
    elif user.get("gemini_api_key"):
        limit_notes = cfg.max_notes_custom_key
        limit_questions = cfg.max_questions_custom_key
    else:
        limit_notes = cfg.max_notes_regular
        limit_questions = cfg.max_questions_regular

    referrer_count = user.get("referral_count", 0)
    
    msg = (
        f"👤 **User Profile**\n\n"
        f"🆔 ID: `{user_id}`\n"
        f"🔰 Status: **{status}**\n"
        f"👮 Role: {role}\n\n"
        f"📊 **Usage (Today)**:\n"
        f"Notes: {used_today} / {limit_notes}\n"
        f"Max Questions/Note: {limit_questions}\n\n"
        f"🔗 **Referrals**: {referrer_count}\n"
        f"Invite friends to get Premium!"
    )
    
    kb = InlineKeyboardMarkup()
    if not is_premium(user):
        kb.add(InlineKeyboardButton("💎 Upgrade to Premium", callback_data="subscribe_premium"))
    kb.add(InlineKeyboardButton("🔙 Home", callback_data="home"))
    
    try:
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        bot.send_message(user_id, msg, parse_mode="Markdown", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "channels")
def handle_channels(call: CallbackQuery):
    user_id = call.from_user.id
    user_channels = channels_repo.list_channels(user_id)
    kb = InlineKeyboardMarkup(row_width=1)
    for ch in user_channels:
        label = f"{ch.get('title','Channel')} ({ch.get('username') or ch.get('chat_id')})"
        kb.add(InlineKeyboardButton(f"❌ Remove {label}", callback_data=f"removech_{ch['chat_id']}"))
    kb.add(InlineKeyboardButton("➕ Add a Channel", callback_data="add_channel_info"))
    kb.add(InlineKeyboardButton("🔙 Home", callback_data="home"))

    text = (
        "Manage your channels.\n\n"
        "- Add channels where you are admin/owner and where the bot is also admin.\n"
        "- You can later select any of them as quiz targets."
    )
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
    except Exception:
        bot.send_message(user_id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "add_channel_info")
def handle_add_channel_info(call: CallbackQuery):
    user_id = call.from_user.id
    text = (
        "To add a channel:\n"
        "1) Add this bot as an admin in your channel.\n"
        "2) Forward a message from that channel here OR send the channel @username."
    )
    bot.answer_callback_query(call.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    msg = bot.send_message(user_id, text, reply_markup=home_keyboard())
    # We might need to store this last_msg_id in a separate state if we want to delete it later
    # but channel addition doesn't use pending_notes state yet.
    # Let's add it.
    pending_notes[user_id] = {"stage": "await_channel", "last_msg_id": msg.message_id}


@bot.message_handler(func=lambda m: m.forward_from_chat is not None and m.forward_from_chat.type == "channel")
def handle_channel_forward(message: Message):
    chat = message.forward_from_chat
    chat_id = chat.id
    title = chat.title or "Channel"
    username = chat.username
    user_id = message.from_user.id
    
    # Try delete previous prompt and user msg
    state = pending_notes.get(user_id)
    if state and state.get("stage") == "await_channel":
        if "last_msg_id" in state:
            try:
                bot.delete_message(user_id, state["last_msg_id"])
            except:
                pass
        pending_notes.pop(user_id, None)
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except:
        pass

    try:
        member = bot.get_chat_member(chat_id, user_id)
        if member.status not in ["administrator", "creator"]:
            bot.reply_to(message, "You must be admin of that channel.")
            return
        bot_member = bot.get_chat_member(chat_id, bot.get_me().id)
        can_post = bot_member.status in ["administrator", "creator"]
        channels_repo.add_channel(user_id, chat_id, title, username, can_post)
        bot.reply_to(message, f"Channel saved: {title}")
    except Exception as e:
        bot.reply_to(message, f"Failed to verify channel: {e}")


@bot.message_handler(func=lambda m: bool(m.text) and m.text.startswith("@"))
def handle_channel_username(message: Message):
    # Attempt to resolve channel by username
    user_id = message.from_user.id

    # Try delete previous prompt and user msg
    state = pending_notes.get(user_id)
    if state and state.get("stage") == "await_channel":
        if "last_msg_id" in state:
            try:
                bot.delete_message(user_id, state["last_msg_id"])
            except:
                pass
        pending_notes.pop(user_id, None)
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except:
        pass

    try:
        chat = bot.get_chat(message.text)
        if not chat or chat.type != "channel":
            bot.reply_to(message, "Not a valid channel username.")
            return
        member = bot.get_chat_member(chat.id, user_id)
        if member.status not in ["administrator", "creator"]:
            bot.reply_to(message, "You must be admin of that channel.")
            return
        bot_member = bot.get_chat_member(chat.id, bot.get_me().id)
        can_post = bot_member.status in ["administrator", "creator"]
        channels_repo.add_channel(user_id, chat.id, chat.title or "Channel", chat.username, can_post)
        bot.reply_to(message, f"Channel saved: {chat.title}")
    except Exception as e:
        bot.reply_to(message, f"Failed to add channel: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith("removech_"))
def handle_remove_channel(call: CallbackQuery):
    user_id = call.from_user.id
    chat_id = int(call.data.split("_")[1])
    channels_repo.remove_channel(user_id, chat_id)
    bot.answer_callback_query(call.id, "Removed")
    handle_channels(call)


# Quizzes Management
@bot.callback_query_handler(func=lambda call: call.data == "my_quizzes")
def handle_my_quizzes(call: CallbackQuery):
    user_id = call.from_user.id
    user = users_repo.get(user_id) or {}
    is_prem = is_premium(user)
    
    # If not premium, only fetch last 2. If premium, fetch last 20 (pagination later if needed)
    limit = 20 if is_prem else 2
    quizzes = quizzes_repo.get_user_quizzes(user_id, limit=limit)
    
    kb = InlineKeyboardMarkup(row_width=1)
    for q in quizzes:
        title = q.get("title", "Quiz")
        created = q.get("created_at").strftime("%Y-%m-%d") if q.get("created_at") else ""
        kb.add(InlineKeyboardButton(f"📄 {title} ({created})", callback_data=f"viewquiz_{q['_id']}"))
    
    if not quizzes:
        kb.add(InlineKeyboardButton("No saved quizzes found", callback_data="settings")) # Dummy

    if not is_prem:
        kb.add(InlineKeyboardButton("🔒 Upgrade to see all", callback_data="settings")) # Placeholder link

    kb.add(InlineKeyboardButton("🔙 Home", callback_data="home"))
    
    text = "<b>My Quizzes</b>\nSelect a quiz to view or export."
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
    except Exception:
        bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("viewquiz_"))
def handle_view_quiz(call: CallbackQuery):
    user_id = call.from_user.id
    quiz_id = call.data.split("_")[1]
    quiz = quizzes_repo.get_quiz(quiz_id)
    
    if not quiz:
        bot.answer_callback_query(call.id, "Quiz not found")
        return

    text = f"<b>{quiz.get('title')}</b>\n"
    text += f"Questions: {len(quiz.get('questions', []))}\n"
    text += f"Date: {quiz.get('created_at')}\n"

    kb = InlineKeyboardMarkup(row_width=2)
    # Exports
    kb.add(InlineKeyboardButton("📄 Export PDF", callback_data=f"exp_{quiz_id}_pdf"))
    kb.add(InlineKeyboardButton("📝 Export DOCX", callback_data=f"exp_{quiz_id}_docx"))
    kb.add(InlineKeyboardButton("📃 Export TXT", callback_data=f"exp_{quiz_id}_txt"))
    kb.add(InlineKeyboardButton("🔙 Back", callback_data="my_quizzes"))

    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
    except Exception:
        bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("exp_more_"))
def handle_explain_more(call: CallbackQuery):
    user_id = call.from_user.id
    q_index = int(call.data.split("_")[-1])
    
    # Attempt to retrieve the last quiz generated for this user
    last_quiz = quizzes_repo.collection.find_one({"user_id": user_id}, sort=[("created_at", -1)])
    if not last_quiz or "questions" not in last_quiz:
        bot.answer_callback_query(call.id, "Context lost. Please start a new quiz.")
        return
        
    try:
        q_data = last_quiz["questions"][q_index - 1]
    except (IndexError, KeyError):
        bot.answer_callback_query(call.id, "Question not found.")
        return

    bot.answer_callback_query(call.id, "🤖 Thinking...")
    
    # Use Gemini to explain this specific question in detail
    prompt = f"Explain this quiz question in more detail. Why is the correct answer right and why might someone get it wrong?\n\nQuestion: {q_data['question']}\nCorrect Answer: {q_data['choices'][q_data['answer_index']]}\nExplanation: {q_data.get('explanation','')}"
    
    try:
        # Re-using the generate logic but for a simple chat/explanation
        api_key = _choose_api_key(user_id)
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        explanation = response.text or "Could not generate explanation."
        bot.send_message(user_id, f"<b>🤖 AI Deep Dive (Q{q_index}):</b>\n\n{explanation}", parse_mode="HTML")
    except Exception as e:
        bot.send_message(user_id, f"Failed to get deep dive: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith("exp_"))
def handle_export_quiz(call: CallbackQuery):
    user_id = call.from_user.id
    user = users_repo.get(user_id) or {}
    
    # Check premium
    if not is_premium(user) and user.get("role") != "admin" and user_id != cfg.owner_id:
        bot.answer_callback_query(call.id, "Export is a Premium feature!", show_alert=True)
        return

    parts = call.data.split("_")
    quiz_id = parts[1]
    fmt = parts[2]
    
    quiz = quizzes_repo.get_quiz(quiz_id)
    if not quiz:
        bot.answer_callback_query(call.id, "Quiz not found")
        return

    processing = bot.send_message(user_id, "Generating file...")
    try:
        title = quiz.get("title", "Quiz")
        questions = quiz.get("questions", [])
        
        file_io = None
        filename = f"{title[:20]}_{fmt}.{fmt}".replace(" ", "_")
        
        bot_username = bot.get_me().username or "SmartQuizBot"

        if fmt == "pdf":
            file_io = QuizExporter.to_pdf(title, questions, bot_username)
        elif fmt == "docx":
            file_io = QuizExporter.to_docx(title, questions, bot_username)
        elif fmt == "txt":
            file_io = QuizExporter.to_txt(title, questions, bot_username)
            
        if file_io:
            bot.send_document(user_id, (filename, file_io))
            bot.delete_message(user_id, processing.message_id)
        else:
            bot.embed_message_text("Export failed.", user_id, processing.message_id)

    except Exception as e:
        bot.edit_message_text(f"Export Error: {e}", user_id, processing.message_id)


# Generate flow
@bot.callback_query_handler(func=lambda call: call.data == "generate")
@error_handler
def handle_generate(call: CallbackQuery):
    user_id = call.from_user.id
    users_repo.reset_notes_if_new_day(user_id)

    if not is_subscribed(bot, user_id):
        bot.answer_callback_query(call.id, "Please join required channels first.")
        bot.send_message(user_id, "Please Join All Our Channels!\n/start - To start again")
        return

    if not has_quota(db, user_id):
        bot.answer_callback_query(call.id, "You have reached your daily limit. Add your own Gemini API key in Settings to increase limits.")
        return

    pending_notes[user_id] = {"stage": "await_input_type"}
    bot.answer_callback_query(call.id)
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("📝 Use a Note", callback_data="input_note"),
        InlineKeyboardButton("🏷️ Title Only", callback_data="input_title"),
        InlineKeyboardButton("📄 File (PDF/DOCX/TXT/PPT) [Premium]", callback_data="input_file"),
        InlineKeyboardButton("📺 YouTube [Premium]", callback_data="input_youtube"),
        InlineKeyboardButton("🎙️ Audio [Premium]", callback_data="input_audio"),
        InlineKeyboardButton("🔙 Home", callback_data="home"),
    )
    tip = ""
    user = users_repo.get(user_id) or {}
    if not user.get("gemini_api_key"):
        tip = "\n\nTip: Add your own Gemini API key to lift the 2/day limit. Use Settings → Set/Change Gemini API Key."
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.send_message(user_id, "Choose input type:" + tip, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data in ["input_note", "input_title", "input_file", "input_youtube", "input_audio"])
def handle_input_choice(call: CallbackQuery):
    user_id = call.from_user.id
    if user_id not in pending_notes:
        bot.answer_callback_query(call.id, "Session expired.")
        return
    
    choice = call.data
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass

    if choice == "input_note":
        pending_notes[user_id]["stage"] = "await_note"
        msg = bot.send_message(user_id, "Please send your note now.")
        pending_notes[user_id]["last_msg_id"] = msg.message_id
    elif choice == "input_title":
        pending_notes[user_id]["stage"] = "await_title"
        msg = bot.send_message(user_id, "Please send the topic/title.")
        pending_notes[user_id]["last_msg_id"] = msg.message_id
    elif choice == "input_file":
        pending_notes[user_id]["stage"] = "await_file"
        msg = bot.send_message(user_id, "Please upload your file (PDF, DOCX, TXT, PPT).")
        pending_notes[user_id]["last_msg_id"] = msg.message_id
    elif choice == "input_youtube":
        user = users_repo.get(user_id) or {}
        if not is_premium(user) and user.get("role") != "admin" and user_id != cfg.owner_id:
             bot.answer_callback_query(call.id, "Premium feature only!", show_alert=True)
             return
        pending_notes[user_id]["stage"] = "await_youtube"
        msg = bot.send_message(user_id, "Please send a YouTube video link.")
        pending_notes[user_id]["last_msg_id"] = msg.message_id
    elif choice == "input_audio":
        user = users_repo.get(user_id) or {}
        if not is_premium(user) and user.get("role") != "admin" and user_id != cfg.owner_id:
             bot.answer_callback_query(call.id, "Premium feature only!", show_alert=True)
             return
        pending_notes[user_id]["stage"] = "await_audio"
        msg = bot.send_message(user_id, "Please send an audio file (Voice Note or MP3/OGG/WAV). English Only.")
        pending_notes[user_id]["last_msg_id"] = msg.message_id
    
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass


def ask_difficulty(user_id: int):
    # Try delete previous prompt
    state = pending_notes.get(user_id)
    if state and "last_msg_id" in state:
        try:
            bot.delete_message(user_id, state["last_msg_id"])
        except:
            pass

    # Ask Difficulty
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🟢 Beginner", callback_data="diff_Beginner"),
        InlineKeyboardButton("🟡 Medium", callback_data="diff_Medium"),
        InlineKeyboardButton("🔴 Hard", callback_data="diff_Hard"),
        InlineKeyboardButton("🔙 Home", callback_data="home"),
    )
    pending_notes[user_id]["stage"] = "choose_difficulty"
    bot.send_message(user_id, "Choose difficulty:", reply_markup=kb)


@bot.message_handler(func=lambda m: m.from_user and m.from_user.id in pending_notes and pending_notes[m.from_user.id].get("stage") == "await_title")
def handle_title_submission(message: Message):
    user_id = message.from_user.id
    title = message.text or ""
    pending_notes[user_id]["title"] = title
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except:
        pass
    ask_difficulty(user_id)


@bot.message_handler(content_types=["document"], func=lambda m: m.from_user and m.from_user.id in pending_notes and pending_notes[m.from_user.id].get("stage") == "await_file")
@error_handler
def handle_file_submission(message: Message):
    user_id = message.from_user.id
    try:
        text, filename = fetch_and_parse_file(bot, db, message)
        if not text or not text.strip():
            bot.reply_to(message, "Could not extract text. Scanned PDFs/Images are not supported. Please send a text/DOCX/PPTX file.")
            return

        pending_notes[user_id]["file_content"] = text
        pending_notes[user_id]["file_name"] = filename
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except:
            pass
        # Note: we don't reply_to here because we want it to "disappear", or we can keep the reply and ask_difficulty will delete the prompt.
        # But ask_difficulty deletes the PROMPT (last_msg_id).
        # Better to just delete the user's file message too.
        ask_difficulty(user_id)
    except ValueError as e:
        bot.reply_to(message, f"File Error: {e}")
    except Exception as e:
        bot.reply_to(message, "Failed to process file. Ensure it is a valid text-based PDF, DOCX, or PPTX.")


@bot.message_handler(func=lambda m: m.from_user and m.from_user.id in pending_notes and pending_notes[m.from_user.id].get("stage") == "await_note")
def handle_note_submission(message: Message):
    user_id = message.from_user.id
    note = message.text or ""
    pending_notes[user_id]["note"] = note
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except:
        pass
    ask_difficulty(user_id)


@bot.message_handler(func=lambda m: m.from_user and m.from_user.id in pending_notes and pending_notes[m.from_user.id].get("stage") == "await_youtube")
@error_handler
def handle_youtube_submission(message: Message):
    user_id = message.from_user.id
    url = message.text or ""
    processing = bot.reply_to(message, "Fetching content (Transcript or Audio)...")
    try:
        text, audio_data, mime_type, video_title, video_description = get_youtube_transcript(url)
        bot.delete_message(message.chat.id, processing.message_id)
        
        # Use video title as the quiz title
        if video_title:
            pending_notes[user_id]["title"] = video_title
        
        if text:
            pending_notes[user_id]["note"] = text
        elif audio_data:
            pending_notes[user_id]["media_data"] = audio_data
            pending_notes[user_id]["mime_type"] = mime_type
            # Add video description as context if available
            if video_description:
                # Truncate description to reasonable length
                context = video_description[:500] if len(video_description) > 500 else video_description
                pending_notes[user_id]["note"] = f"Video Description: {context}"
        else:
             bot.send_message(user_id, "Could not fetch content. Video might be restricted or too long.")
             return

        try:
            bot.delete_message(message.chat.id, message.message_id)
        except:
            pass
        ask_difficulty(user_id)
    except Exception as e:
        logger.error(f"YouTube Error: {e}")
        try:
             bot.delete_message(message.chat.id, processing.message_id)
        except:
             pass
        bot.send_message(user_id, "Failed to process YouTube video. Please try a shorter video or check the URL.")


@bot.message_handler(content_types=["audio", "voice"], func=lambda m: m.from_user and m.from_user.id in pending_notes and pending_notes[m.from_user.id].get("stage") == "await_audio")
@error_handler
def handle_audio_submission(message: Message):
    user_id = message.from_user.id
    
    file_info = message.voice or message.audio
    if file_info.file_size > 20 * 1024 * 1024:
        bot.reply_to(message, "⚠️ File is too big. Please send audio files under 20MB.")
        return

    file_id = file_info.file_id
    mime_type = file_info.mime_type
    
    processing = bot.reply_to(message, "Downloading audio...")
    try:
        file_path = bot.get_file(file_id).file_path
        downloaded_file = bot.download_file(file_path)
        
        if not downloaded_file:
             raise ValueError("Download failed")
             
        pending_notes[user_id]["media_data"] = downloaded_file
        pending_notes[user_id]["mime_type"] = mime_type or "audio/ogg" 
        pending_notes[user_id]["title"] = "Audio Note"
        
        bot.delete_message(message.chat.id, processing.message_id)
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except:
            pass
        ask_difficulty(user_id)
    except Exception as e:
         logger.error(f"Audio processing error: {e}")
         bot.edit_message_text("Error processing audio. Please try again.", message.chat.id, processing.message_id)


# (Rest of code continues...)



@bot.callback_query_handler(func=lambda call: call.data.startswith("diff_"))
def handle_difficulty_selection(call: CallbackQuery):
    user_id = call.from_user.id
    state = pending_notes.get(user_id)
    if not state:
        bot.answer_callback_query(call.id, "Session expired.")
        return

    diff = call.data.split("_")[1]
    state["difficulty"] = diff
    
    # Now ask destination
    user_channels = channels_repo.list_channels(user_id)
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🔁 Allow Beyond Note", callback_data="toggle_beyond_yes"))
    kb.add(InlineKeyboardButton("📥 Send to PM", callback_data="dst_pm"))
    for ch in user_channels:
        label = f"{ch.get('title','Channel')} ({ch.get('username') or ch.get('chat_id')})"
        kb.add(InlineKeyboardButton(f"📣 {label}", callback_data=f"dst_ch_{ch['chat_id']}"))
    kb.add(InlineKeyboardButton("🔙 Home", callback_data="home"))

    state["stage"] = "choose_destination"
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.send_message(user_id, f"Difficulty: {diff}\nChoose where to send the quiz:", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("toggle_beyond_"))
def toggle_beyond_note(call: CallbackQuery):
    user_id = call.from_user.id
    state = pending_notes.get(user_id)
    if not state:
        bot.answer_callback_query(call.id)
        return
    allow = call.data.endswith("yes")
    state["allow_beyond"] = allow
    bot.answer_callback_query(call.id, "Will use knowledge beyond note." if allow else "Will stick to provided note only.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("dst_"))
def handle_destination_selection(call: CallbackQuery):
    user_id = call.from_user.id
    state = pending_notes.get(user_id)
    if not state:
        bot.answer_callback_query(call.id, "Start again with Generate")
        return

    if call.data == "dst_pm":
        state["target_chat_id"] = user_id
        state["target_label"] = "PM"
    elif call.data.startswith("dst_ch_"):
        chat_id = int(call.data.split("_")[2])
        ch = channels_repo.get_channel(user_id, chat_id)
        if not ch:
            bot.answer_callback_query(call.id, "Channel not found")
            return
        state["target_chat_id"] = chat_id
        state["target_label"] = ch.get("title") or str(chat_id)
    else:
        bot.answer_callback_query(call.id)
        return

    # Ask delay (5-60 seconds)
    kb = InlineKeyboardMarkup(row_width=5)
    for s in [5, 10, 15, 20, 30, 45, 60]:
        kb.add(InlineKeyboardButton(f"{s}s", callback_data=f"delay_{s}"))
    kb.add(InlineKeyboardButton("Custom", callback_data="delay_custom"))
    kb.add(InlineKeyboardButton("🔙 Home", callback_data="home"))

    state["stage"] = "choose_delay"
    bot.answer_callback_query(call.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.send_message(user_id, "Choose delay between questions:", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("delay_"))
def handle_delay(call: CallbackQuery):
    user_id = call.from_user.id
    state = pending_notes.get(user_id)
    if not state:
        bot.answer_callback_query(call.id)
        return

    if call.data == "delay_custom":
        state["stage"] = "await_custom_delay"
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        msg = bot.send_message(user_id, "Send a delay in seconds (5-60):")
        state["last_msg_id"] = msg.message_id
        return

    delay = int(call.data.split("_")[1])
    delay = max(5, min(60, delay))
    state["delay_seconds"] = delay

    # Ask schedule or send now
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("Send Now", callback_data="sendnow"))
    kb.add(InlineKeyboardButton("Schedule", callback_data="doschedule"))
    kb.add(InlineKeyboardButton("🔙 Home", callback_data="home"))

    state["stage"] = "confirm_send_or_schedule"
    bot.answer_callback_query(call.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.send_message(user_id, f"Delay set to {delay}s. Send now or schedule?", reply_markup=kb)


@bot.message_handler(func=lambda m: m.from_user and m.from_user.id in pending_notes and pending_notes[m.from_user.id].get("stage") == "await_custom_delay")
def handle_custom_delay(message: Message):
    user_id = message.from_user.id
    state = pending_notes.get(user_id)
    if not state:
        return
    try:
        delay = int(message.text.strip())
        if delay < 5 or delay > 60:
            raise ValueError
        state["delay_seconds"] = delay
    except Exception:
        bot.reply_to(message, "Invalid delay. Send a number 5-60.")
        return

    # Delete prompt and user msg
    if "last_msg_id" in state:
        try:
            bot.delete_message(user_id, state["last_msg_id"])
        except:
            pass
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except:
        pass

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("Send Now", callback_data="sendnow"))
    kb.add(InlineKeyboardButton("Schedule", callback_data="doschedule"))
    kb.add(InlineKeyboardButton("🔙 Home", callback_data="home"))

    state["stage"] = "confirm_send_or_schedule"
    bot.send_message(user_id, f"Delay set to {state['delay_seconds']}s. Send now or schedule?", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "sendnow")
@error_handler
def send_now(call: CallbackQuery):
    user_id = call.from_user.id
    state = pending_notes.get(user_id)
    if not state:
        bot.answer_callback_query(call.id)
        return

    note = state.get("note", "")
    title = state.get("title")
    file_content = state.get("file_content")
    media_data = state.get("media_data")
    mime_type = state.get("mime_type")
    
    target = state.get("target_chat_id", user_id)
    delay = int(state.get("delay_seconds", 5))
    difficulty = state.get("difficulty", "Medium")

    user = users_repo.get(user_id) or {}
    num_questions = int(user.get("questions_per_note", 5))
    q_format = (user.get("default_question_type") or cfg.question_type_default).lower()

    if not has_quota(db, user_id):
        bot.answer_callback_query(call.id, "Daily quota reached")
        return

    update_last_note_time(db, user_id)
    bot.answer_callback_query(call.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass

    generating = bot.send_message(user_id, f"Generating {num_questions} questions ({difficulty})...")
    try:
        allow_beyond = bool(state.get("allow_beyond"))
        questions = []
        
        if file_content:
            # Chunking to avoid limits; distribute questions across chunks up to requested number
            chunks = chunk_text(file_content, max_chars=3500)
            per_chunk = max(1, num_questions // max(1, len(chunks)))
            for idx, ch in enumerate(chunks):
                if len(questions) >= num_questions:
                    break
                qbatch = generate_questions(ch, per_chunk, user_id=user_id, title_only=False, allow_beyond=True, difficulty=difficulty)
                questions.extend(qbatch)
            questions = questions[:num_questions]
        elif title:
            warn = "⚠️ Title-only mode: AI may include info beyond your intended scope."
            bot.send_message(user_id, warn)
            questions = generate_questions("", num_questions, user_id=user_id, title_only=True, allow_beyond=True, topic_title=title, difficulty=difficulty)
        elif media_data:
            # Multimodal (Audio/Image)
            questions = generate_questions(
                "", 
                num_questions, 
                user_id=user_id, 
                title_only=False, 
                allow_beyond=True, 
                difficulty=difficulty,
                media_data=media_data,
                mime_type=mime_type
            )
        else:
            questions = generate_questions(note, num_questions, user_id=user_id, title_only=False, allow_beyond=allow_beyond, difficulty=difficulty)
        
        if not questions:
            bot.send_message(user_id, "An error occurred while generating questions (or none returned). Please try again.")
            bot.delete_message(user_id, generating.id)
            return
        bot.delete_message(user_id, generating.id)
        letters = ["A", "B", "C", "D"]
        for idx, q in enumerate(questions, start=1):
            time.sleep(delay)
            kb = None
            if q_format == "text":
                text = f"{idx}. {q['question']}\n"
                for i, c in enumerate(q["choices"]):
                    prefix = letters[i] if i < len(letters) else str(i + 1)
                    text += f"{prefix}. {c}\n"
                text += f"\n<b>Correct Answer</b>: {letters[q['answer_index']]} - {q['choices'][q['answer_index']]}"
                explanation = (q.get("explanation") or "")
                if explanation:
                    text += f"\n<b>Explanation:</b> {explanation[:195]}"
                
                kb = InlineKeyboardMarkup()
                # Store enough context in callback_data to explain the question
                # Limitation: callback_data max 64 bytes. We'll use a short ID.
                kb.add(InlineKeyboardButton("🤖 Explain More", callback_data=f"exp_more_{idx}"))
                bot.send_message(target, text, parse_mode="HTML", reply_markup=kb)
            else:
                bot.send_poll(
                    target,
                    q["question"],
                    q["choices"],
                    type="quiz",
                    correct_option_id=q["answer_index"],
                    explanation=(q.get("explanation") or "")[:195],
                )
        
        # Save generated questions in state for "Explain More" sessions
        if q_format == "text":
             # We need a more persistent way to store these for the callback, 
             # but for now we'll use a temporary cache or similar logic.
             # Actually, we already save the quiz to DB.
             pass
        increment_quota(db, user_id)
        increase_total_notes(db, user_id)
        
        # Save Quiz
        quiz_title = title or (note[:30] + "..." if note else "Quiz") or "Generated Quiz"
        if media_data:
             quiz_title = f"{state.get('title', 'Media Quiz')}"

        if quizzes_repo:
            quizzes_repo.create({
                "user_id": user_id,
                "title": quiz_title,
                "questions": questions,
                "created_at": datetime.now()
            })

        # Send summary
        destinations = state.get("target_label", "PM")
        try:
            if isinstance(destinations, list):
                destinations_str = ", ".join(destinations)
            else:
                destinations_str = str(destinations)
        except Exception:
            destinations_str = "PM"
        summary = (
            f"✅ Generated {len(questions)} questions.\n"
            f"📍 Posted to: {destinations_str}"
        )
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🏠 Home", callback_data="home"))
        bot.send_message(user_id, summary, reply_markup=kb)
    except Exception as e:
        bot.send_message(user_id, f"Something went wrong: {e}")
    finally:
        pending_notes.pop(user_id, None)


@bot.callback_query_handler(func=lambda call: call.data == "doschedule")
def do_schedule(call: CallbackQuery):
    user_id = call.from_user.id
    state = pending_notes.get(user_id)
    if not state:
        bot.answer_callback_query(call.id)
        return
    state["stage"] = "await_schedule_time"
    bot.answer_callback_query(call.id)
    # Show local UTC+3 time hint
    now = datetime.now()
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    msg = bot.send_message(user_id, f"Send schedule time in format YYYY-MM-DD HH:MM (UTC+3). Example: 2025-01-01 12:30\nNow (UTC+3): {format_dt_utc3(now)}")
    state["last_msg_id"] = msg.message_id


@bot.message_handler(func=lambda m: m.from_user and m.from_user.id in pending_notes and pending_notes[m.from_user.id].get("stage") == "await_schedule_time")
def handle_schedule_time(message: Message):
    user_id = message.from_user.id
    state = pending_notes.get(user_id)
    if not state:
        return
    try:
        dt_local = datetime.strptime(message.text.strip(), "%Y-%m-%d %H:%M")
    except Exception:
        bot.reply_to(message, "Invalid format. Use YYYY-MM-DD HH:MM (UTC+3)")
        return

    # Delete prompt and user msg
    if "last_msg_id" in state:
        try:
            bot.delete_message(user_id, state["last_msg_id"])
        except:
            pass
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except:
        pass

    # Save schedule
    user = users_repo.get(user_id) or {}
    num_questions = int(user.get("questions_per_note", 5))
    q_format = (user.get("default_question_type") or cfg.question_type_default).lower()

    # Convert provided UTC+3 time to UTC for storage
    scheduled_utc = from_utc3_to_utc(dt_local)

    schedules_repo.create(
        {
            "user_id": user_id,
            "target_chat_id": state.get("target_chat_id", user_id),
            "target_label": state.get("target_label", "PM"),
            "note": state.get("note", ""),
            "title": state.get("title"),
            "file_content": state.get("file_content"),
            "allow_beyond": bool(state.get("allow_beyond")),
            "num_questions": num_questions,
            "question_type": q_format,
            "delay_seconds": int(state.get("delay_seconds", 5)),
            "difficulty": state.get("difficulty", "Medium"),
            "scheduled_at": scheduled_utc,
            "status": "pending",
            "created_at": datetime.now(),
        }
    )
    pending_notes.pop(user_id, None)
    bot.send_message(user_id, "📅 Scheduled successfully.", reply_markup=home_keyboard())


# Settings
@bot.callback_query_handler(func=lambda call: call.data == "settings")
def handle_settings(call: CallbackQuery):
    user_id = call.from_user.id
    user = users_repo.get(user_id)
    if not user:
        bot.answer_callback_query(call.id, "User not found.")
        return

    question_type = user.get("default_question_type", "text")
    questions_per_note = user.get("questions_per_note", 5)
    key_status = "✅ Set" if user.get("gemini_api_key") else "❌ Not set"
    msg = (
        f"**Settings**\n"
        f"• Question Type: `{question_type}`\n"
        f"• Questions per Note: `{questions_per_note}`\n"
        f"• Gemini API Key: {key_status}"
    )

    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("Change Question Type", callback_data="change_qtype"),
        InlineKeyboardButton("Change Questions/Note", callback_data="change_qpernote"),
        InlineKeyboardButton("Set/Change Gemini API Key", callback_data="set_gemini_key"),
        InlineKeyboardButton("Remove Gemini API Key", callback_data="remove_gemini_key"),
        InlineKeyboardButton("Back to Home", callback_data="home"),
    )

    try:
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
    except Exception:
        bot.send_message(user_id, msg, parse_mode="Markdown", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "change_qtype")
def change_question_type(call: CallbackQuery):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("Text", callback_data="set_qtype_text"),
        InlineKeyboardButton("Poll", callback_data="set_qtype_poll"),
        InlineKeyboardButton("Back", callback_data="settings"),
    )
    try:
        bot.edit_message_text("Choose a question type:", call.message.chat.id, call.message.message_id, reply_markup=markup)
    except Exception:
        bot.send_message(call.message.chat.id, "Choose a question type:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("set_qtype_"))
def set_question_type(call: CallbackQuery):
    user_id = call.from_user.id
    new_type = call.data.split("_")[-1]
    users_repo.set_default_qtype(user_id, new_type)
    bot.answer_callback_query(call.id, f"Question type updated to {new_type.capitalize()}")
    handle_settings(call)


@bot.callback_query_handler(func=lambda call: call.data == "change_qpernote")
def change_questions_per_note(call: CallbackQuery):
    markup = InlineKeyboardMarkup(row_width=5)
    # Common options
    options = [5, 10, 15, 20, 25, 30, 40, 50, 75, 100]
    buttons = [InlineKeyboardButton(str(i), callback_data=f"set_qpernote_{i}") for i in options]
    for i in range(0, len(buttons), 5):
        markup.row(*buttons[i : i + 5])
    markup.add(InlineKeyboardButton("Back", callback_data="settings"))
    try:
        bot.edit_message_text("Choose number of questions per note:", call.message.chat.id, call.message.message_id, reply_markup=markup)
    except Exception:
        bot.send_message(call.message.chat.id, "Choose number of questions per note:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "set_gemini_key")
def start_set_gemini_key(call: CallbackQuery):
    user_id = call.from_user.id
    pending_keys[user_id] = {"stage": "await_key"}
    bot.answer_callback_query(call.id)
    bot.send_message(user_id, "Send your Gemini API key now. You can create one at https://aistudio.google.com/app/apikey")


@bot.message_handler(func=lambda m: m.from_user and m.from_user.id in pending_keys and pending_keys[m.from_user.id].get("stage") == "await_key")
def handle_set_gemini_key(message: Message):
    user_id = message.from_user.id
    key = (message.text or "").strip()
    if not key:
        bot.reply_to(message, "Key cannot be empty.")
        return
    verifying = bot.reply_to(message, "Validating key...")
    ok = validate_gemini_api_key(key)
    if not ok:
        try:
            bot.delete_message(message.chat.id, verifying.id)
        except Exception:
            pass
        bot.reply_to(message, "Invalid Gemini API key. Please create one at https://aistudio.google.com/app/apikey and try again.")
        return
    try:
        users_repo.set_gemini_api_key(user_id, key)
        bot.delete_message(message.chat.id, verifying.id)
        bot.reply_to(message, "✅ Key saved to your account.", reply_markup=home_keyboard())
    except DuplicateKeyError:
        bot.delete_message(message.chat.id, verifying.id)
        bot.reply_to(message, "This key is already used by another user. Please use a unique key.")
    finally:
        pending_keys.pop(user_id, None)


@bot.callback_query_handler(func=lambda call: call.data == "remove_gemini_key")
def remove_gemini_key(call: CallbackQuery):
    user_id = call.from_user.id
    users_repo.set_gemini_api_key(user_id, None)
    bot.answer_callback_query(call.id, "Key removed.")
    handle_settings(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("set_qpernote_"))
def set_questions_per_note(call: CallbackQuery):
    user_id = call.from_user.id
    new_value = int(call.data.split("_")[-1])
    
    user = users_repo.get(user_id) or {}
    personal_key = user.get("gemini_api_key")
    
    # Determine max limit
    if personal_key and str(personal_key).strip():
         max_limit = cfg.max_questions_custom_key or 300
    elif is_premium(user):
         max_limit = cfg.max_questions_premium or 150
    else:
         max_limit = cfg.max_questions_regular or 100

    if new_value > max_limit:
        bot.answer_callback_query(call.id, f"Limit is {max_limit} for your plan.")
        return
    users_repo.set_questions_per_note(user_id, new_value)
    bot.answer_callback_query(call.id, f"Updated to {new_value} questions per note.")
    handle_settings(call)


@bot.callback_query_handler(func=lambda call: call.data == "home")
def handle_home(call: CallbackQuery):
    user_id = call.from_user.id
    pending_notes.pop(user_id, None)
    handle_start(call.message)


@bot.callback_query_handler(func=lambda call: call.data == "faq")
def handle_faq(call: CallbackQuery):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    text = (
        "📚 Frequently Asked Questions (FAQs)\n\n"
        "1) Why limits? Resource management.\n"
        "2) 24/7? Use a VPS for always-on.\n"
        "3) Why slow? Free hosting limits.\n"
        "4) Updates? Yes, more features coming.\n"
        "5) Note size? Up to Telegram limits (~4096 chars).\n"
        "6) AI? Gemini by Google.\n"
        "7) Poll mode? Settings → Question Type → Poll.\n"
    )
    bot.send_message(call.message.chat.id, text, reply_markup=home_keyboard())


@bot.callback_query_handler(func=lambda call: call.data == "about")
def handle_about(call: CallbackQuery):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    text = (
        "ℹ️ <b>About the Bot</b>\n\n"
        "🤖 Version: <b><i>v2.0.0</i></b>\n"
        "📚 Converts your text notes into MCQ quizzes.\n"
        "🎓 For students, educators, creators.\n\n"
        "🛠 New: MongoDB, user channels, delay, scheduling.\n"
    )
    bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=home_keyboard())


# Simple payment flow (pending → accept/decline)
@bot.callback_query_handler(func=lambda call: call.data == "subscribe_premium")
def subscribe_premium_start(call: CallbackQuery):
    user_id = call.from_user.id
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("Telebirr", callback_data="pay_telebirr"),
        InlineKeyboardButton("CBE", callback_data="pay_cbe"),
    )
    kb.row(
        InlineKeyboardButton("USDT TRC-20", callback_data="pay_trc"),
        InlineKeyboardButton("USDT ERC-20", callback_data="pay_erc"),
    )
    kb.row(InlineKeyboardButton("🔙 Home", callback_data="home"))
    amount = cfg.premium_price if not settings_repo else settings_repo.get("premium_price", cfg.premium_price)
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_message(user_id, f"Premium is {amount} ETB or ~0.5 USDT per month. Choose payment method:", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_"))
def choose_payment_method(call: CallbackQuery):
    user_id = call.from_user.id
    method = call.data.split("_")[1]
    pending_subscriptions[user_id] = {"method": method}

    if method == "telebirr":
        numbers = (settings_repo.get("telebirr_numbers", cfg.telebirr_numbers) if settings_repo else cfg.telebirr_numbers)
    elif method == "cbe":
        numbers = (settings_repo.get("cbe_numbers", cfg.cbe_numbers) if settings_repo else cfg.cbe_numbers)
    else:
        numbers = ["TRC20 Wallet: <provide>", "ERC20 Wallet: <provide>"]

    amount = (settings_repo.get("premium_price", cfg.premium_price) if settings_repo else cfg.premium_price)
    number_list = "\n".join(numbers)
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_message(user_id, f"Send {amount} ETB or 0.5 USDT to:\n{number_list}\nAfter payment send a screenshot.")
    bot.send_message(user_id, "Send the transaction screenshot now (as a photo).", reply_markup=home_keyboard())


@bot.message_handler(content_types=["photo"]) 
def handle_payment_photo(message: Message):
    user_id = message.from_user.id
    if user_id not in pending_subscriptions:
        return
    pending_subscriptions[user_id]["screenshot"] = message.photo[-1].file_id
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("Done", callback_data="confirm_payment"), InlineKeyboardButton("Cancel", callback_data="cancel_payment"))
    bot.send_message(user_id, "Submit this payment?", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "cancel_payment")
def cancel_payment(call: CallbackQuery):
    user_id = call.from_user.id
    pending_subscriptions.pop(user_id, None)
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_message(user_id, "Payment process canceled.", reply_markup=home_keyboard())


@bot.callback_query_handler(func=lambda call: call.data == "confirm_payment")
@error_handler
def confirm_payment(call: CallbackQuery):
    user_id = call.from_user.id
    info = pending_subscriptions.get(user_id)
    if not info:
        return

    method = info["method"]
    screenshot_id = info.get("screenshot")
    if not screenshot_id:
        bot.send_message(user_id, "Please send a photo of your payment.")
        return

    amount = (settings_repo.get("premium_price", cfg.premium_price) if settings_repo else cfg.premium_price)
    payments_repo.insert(user_id, method, amount, screenshot_id)

    # Notify admins: for demo, anyone with role admin in DB
    admins = [u.get("id") for u in db["users"].find({"role": "admin"})]
    for admin_id in admins:
        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("Accept", callback_data=f"acceptpay_{user_id}"),
            InlineKeyboardButton("Decline", callback_data=f"declinepay_{user_id}"),
        )
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.send_photo(admin_id, screenshot_id, caption=f"New Payment\nUser: {user_id}\nMethod: {method}\nAmount: {amount}", reply_markup=kb)

    bot.send_message(user_id, "Payment submitted for review. You'll be notified soon.", reply_markup=home_keyboard())
    pending_subscriptions.pop(user_id, None)


@bot.callback_query_handler(func=lambda call: call.data.startswith("acceptpay_"))
def accept_payment(call: CallbackQuery):
    # Only admins should accept
    admin_user = users_repo.get(call.from_user.id)
    if (admin_user or {}).get("role") != "admin":
        return
    user_id = int(call.data.split("_")[1])
    users_repo.set_premium(user_id, 30)
    payments_repo.update_status(user_id, "accepted")
    amount = (settings_repo.get("premium_price", cfg.premium_price) if settings_repo else cfg.premium_price)
    bot.send_message(user_id, f"Your premium subscription for {amount} Birr has been approved!")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pay_channel = (settings_repo.get("payment_channel", cfg.payment_channel) if settings_repo else cfg.payment_channel)
    if pay_channel:
        try:
            bot.send_message(pay_channel, f"New Premium Subscription\nUser ID: {user_id}\nAmount Paid: {amount}\nDate: {now}")
        except Exception:
            pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("declinepay_"))
def decline_payment(call: CallbackQuery):
    admin_user = users_repo.get(call.from_user.id)
    if (admin_user or {}).get("role") != "admin":
        return
    user_id = int(call.data.split("_")[1])
    payments_repo.update_status(user_id, "declined")
    bot.send_message(user_id, "Your premium request was declined. If this is a mistake, please try again.")


# FAQ/About handlers already added

@bot.callback_query_handler(func=lambda call: call.data == "schedule_menu")
def handle_schedule_menu(call: CallbackQuery):
    user_id = call.from_user.id
    items = schedules_repo.get_user_schedules(user_id)
    if not items:
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔙 Home", callback_data="home"))
        bot.answer_callback_query(call.id)
        bot.send_message(user_id, "No schedules yet. Use Generate → pick destination → Schedule.", reply_markup=kb)
        return
    kb = InlineKeyboardMarkup(row_width=1)
    for s in items:
        sched_id = str(s.get("_id"))
        when = s.get("scheduled_at")
        label = f"{s.get('target_label','PM')} @ {when} ({s.get('status','pending')})"
        kb.add(InlineKeyboardButton(f"❌ Delete {label}", callback_data=f"delsch_{sched_id}"))
    kb.add(InlineKeyboardButton("🔙 Home", callback_data="home"))
    try:
        bot.edit_message_text("Your schedules:", call.message.chat.id, call.message.message_id, reply_markup=kb)
    except Exception:
        bot.send_message(user_id, "Your schedules:", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("delsch_"))
def handle_delete_schedule(call: CallbackQuery):
    user_id = call.from_user.id
    sched_id = call.data.split("_")[1]
    ok = schedules_repo.delete(user_id, sched_id)
    bot.answer_callback_query(call.id, "Deleted" if ok else "Not found")
    handle_schedule_menu(call)


print("Bot running...")
if __name__ == "__main__":
    bot.infinity_polling()


@bot.message_handler(commands=["setforcesub"]) 
def admin_set_force_subscription(message: Message):
    if not users_repo:
        bot.reply_to(message, "DB unavailable.")
        return
    admin = users_repo.get(message.from_user.id)
    if not admin or admin.get("role") != "admin":
        bot.reply_to(message, "Not authorized.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /setforcesub on|off")
        return
    val = parts[1].lower() in ("on", "true", "1", "yes")
    SettingsRepository(db).set("force_subscription", val)
    bot.reply_to(message, f"force_subscription set to {val}")


@bot.message_handler(commands=["setforcechannels"]) 
def admin_set_force_channels(message: Message):
    if not users_repo:
        bot.reply_to(message, "DB unavailable.")
        return
    admin = users_repo.get(message.from_user.id)
    if not admin or admin.get("role") != "admin":
        bot.reply_to(message, "Not authorized.")
        return
    # Example: /setforcechannels @Ch1 @Ch2 @Ch3
    parts = message.text.strip().split()
    channels = [p for p in parts[1:] if p.startswith("@")]
    if not channels:
        bot.reply_to(message, "Usage: /setforcechannels @Ch1 @Ch2 ...")
        return
    SettingsRepository(db).set("force_channels", channels)
    bot.reply_to(message, f"force_channels updated: {', '.join(channels)}")


@bot.message_handler(commands=["setpremiumprice"]) 
def admin_set_premium_price(message: Message):
    if not users_repo:
        bot.reply_to(message, "DB unavailable.")
        return
    admin = users_repo.get(message.from_user.id)
    if not admin or admin.get("role") != "admin":
        bot.reply_to(message, "Not authorized.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /setpremiumprice 40")
        return
    price = int(parts[1])
    SettingsRepository(db).set("premium_price", price)
    bot.reply_to(message, f"premium_price set to {price}")


@bot.message_handler(commands=["setpaymentchannel"]) 
def admin_set_payment_channel(message: Message):
    if not users_repo:
        bot.reply_to(message, "DB unavailable.")
        return
    admin = users_repo.get(message.from_user.id)
    if not admin or admin.get("role") != "admin":
        bot.reply_to(message, "Not authorized.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].startswith("@"):
        bot.reply_to(message, "Usage: /setpaymentchannel @PaymentsChannel")
        return
    SettingsRepository(db).set("payment_channel", parts[1])
    bot.reply_to(message, f"payment_channel set to {parts[1]}")


@bot.message_handler(commands=["addtelebirr"]) 
def admin_add_telebirr(message: Message):
    if not users_repo:
        bot.reply_to(message, "DB unavailable.")
        return
    admin = users_repo.get(message.from_user.id)
    if not admin or admin.get("role") != "admin":
        bot.reply_to(message, "Not authorized.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /addtelebirr 0912345678")
        return
    current = SettingsRepository(db).get("telebirr_numbers", [])
    if parts[1] not in current:
        current.append(parts[1])
    SettingsRepository(db).set("telebirr_numbers", current)
    bot.reply_to(message, f"telebirr_numbers: {', '.join(current)}")


@bot.message_handler(commands=["admin"]) 
def admin_dashboard(message: Message):
    if not users_repo:
        bot.reply_to(message, "DB unavailable.")
        return
    
    user_id = message.from_user.id
    admin = users_repo.get(user_id)
    is_owner = (user_id == cfg.owner_id)
    
    if not is_owner and (not admin or admin.get("role") != "admin"):
        bot.reply_to(message, "Not authorized.")
        return
    
    # Admin Dashboard Menu
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
        InlineKeyboardButton("� Force Subscription", callback_data="admin_manage_sub"),
        InlineKeyboardButton("�💰 Set Premium Price", callback_data="admin_set_price"),
        InlineKeyboardButton("👥 Manage Users", callback_data="admin_users"),
        InlineKeyboardButton("🔙 Close", callback_data="close_admin"),
    )
    bot.reply_to(message, "🔧 **Admin Dashboard**", parse_mode="Markdown", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_") or call.data == "close_admin")
def handle_admin_callbacks(call: CallbackQuery):
    user_id = call.from_user.id
    admin = users_repo.get(user_id)
    is_owner = (user_id == cfg.owner_id)
    if not is_owner and (not admin or admin.get("role") != "admin"):
        bot.answer_callback_query(call.id, "Not authorized.")
        return

    # Skip delete if it's admin_menu (which edits) or some others? 
    # Actually, the original code deletes it.
    if call.data != "admin_menu":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass

    if call.data == "admin_broadcast":
        msg = bot.send_message(user_id, "Send the message you want to broadcast (Text, Photo, or Forward).")
        bot.register_next_step_handler(msg, process_broadcast)
    elif call.data == "admin_set_price":
        msg = bot.send_message(user_id, "Send the new premium price in ETB (digits only).")
        bot.register_next_step_handler(msg, process_set_price)
    elif call.data == "admin_users":
        total_users = users_repo.collection.count_documents({})
        premium_users = users_repo.collection.count_documents({"type": "premium"})
        admins = users_repo.collection.count_documents({"role": "admin"})
        total_quizzes = quizzes_repo.collection.count_documents({})
        
        text = (
            "👥 **User Management Stats**\n\n"
            f"• Total Users: {total_users}\n"
            f"• Premium Users: {premium_users}\n"
            f"• Admins: {admins}\n"
            f"• Total Quizzes: {total_quizzes}\n\n"
            "Use `/addadmin <id>` or `/addpremium <id>` to manage."
        )
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_menu"))
        bot.send_message(user_id, text, parse_mode="Markdown", reply_markup=kb)
    elif call.data == "close_admin":
        # Already deleted above if call.data != "admin_menu"
        bot.answer_callback_query(call.id, "Closed.")

def process_set_price(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()
    if not text.isdigit():
        bot.reply_to(message, "Invalid price. Please send digits only.")
        return
    
    price = int(text)
    sr = SettingsRepository(db)
    sr.set("premium_price", price)
    
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_menu"))
    bot.reply_to(message, f"✅ Premium price updated to {price} ETB.", reply_markup=kb)


def process_broadcast(message: Message):
    user_id = message.from_user.id
    # Confirm broadcast
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Yes, Send", callback_data="confirm_broadcast"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_broadcast")
    )
    
    # Store message to broadcast in state (simplified)
    pending_notes[user_id] = {"broadcast_msg": message} 
    bot.reply_to(message, "Are you sure you want to broadcast this message to ALL users?", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data in ["confirm_broadcast", "cancel_broadcast"])
def execute_broadcast(call: CallbackQuery):
    user_id = call.from_user.id
    if call.data == "cancel_broadcast":
        pending_notes.pop(user_id, None)
        try:
            bot.edit_message_text("Broadcast cancelled.", call.message.chat.id, call.message.message_id)
        except:
            bot.send_message(user_id, "Broadcast cancelled.")
        return
        
    state = pending_notes.get(user_id, {})
    broadcast_msg = state.get("broadcast_msg")
    if not broadcast_msg:
        bot.answer_callback_query(call.id, "Session expired.")
        return

    try:
        bot.edit_message_text("Broadcasting started in background. You will be notified when complete.", call.message.chat.id, call.message.message_id)
    except:
        bot.send_message(user_id, "Broadcasting started in background.")
    
    import threading
    def run_broadcast(msg, requester_id):
        all_users = list(users_repo.collection.find({}))
        success_count = 0
        total = len(all_users)
        
        for i, u in enumerate(all_users):
            target_id = u.get("id")
            if not target_id: continue
            try:
                if msg.content_type == "text":
                    bot.send_message(target_id, msg.text)
                elif msg.content_type == "photo":
                    bot.send_photo(target_id, msg.photo[-1].file_id, caption=msg.caption)
                elif msg.content_type == "document":
                    bot.send_document(target_id, msg.document.file_id, caption=msg.caption)
                elif msg.content_type == "video":
                    bot.send_video(target_id, msg.video.file_id, caption=msg.caption)
                elif msg.content_type == "audio":
                    bot.send_audio(target_id, msg.audio.file_id, caption=msg.caption)
                elif msg.content_type == "voice":
                    bot.send_voice(target_id, msg.voice.file_id, caption=msg.caption)
                elif msg.content_type == "video_note":
                    bot.send_video_note(target_id, msg.video_note.file_id)
                elif msg.content_type == "animation":
                    bot.send_animation(target_id, msg.animation.file_id, caption=msg.caption)
                elif msg.content_type == "sticker":
                    bot.send_sticker(target_id, msg.sticker.file_id)
                else:
                    bot.copy_message(target_id, msg.chat.id, msg.message_id)
                success_count += 1
            except Exception as e:
                logger.error(f"Failed to send broadcast to {target_id}: {e}")
            
            # Rate limiting: 20 messages per second (Telegram's limit)
            if (i + 1) % 20 == 0:
                time.sleep(1)
        
        try:
            bot.send_message(requester_id, f"✅ Broadcast complete.\nSent to: {success_count} / {total} users.")
        except:
            pass

    threading.Thread(target=run_broadcast, args=(broadcast_msg, user_id), daemon=True).start()
    pending_notes.pop(user_id, None)


@bot.message_handler(commands=["setmaxnotes"]) 
def admin_set_max_notes(message: Message):
    if not users_repo:
        bot.reply_to(message, "DB unavailable.")
        return
    admin = users_repo.get(message.from_user.id)
    if not admin or admin.get("role") != "admin":
        bot.reply_to(message, "Not authorized.")
        return
    # Usage: /setmaxnotes regular 5  OR  /setmaxnotes premium 10
    parts = message.text.strip().split()
    if len(parts) < 3 or parts[1] not in ("regular", "premium") or not parts[2].isdigit():
        bot.reply_to(message, "Usage: /setmaxnotes regular|premium <num>")
        return
    key = f"max_notes_{parts[1]}"
    SettingsRepository(db).set(key, int(parts[2]))
    bot.reply_to(message, f"{key} set to {parts[2]}")


@bot.message_handler(commands=["setmaxquestions"]) 
def admin_set_max_questions(message: Message):
    if not users_repo:
        bot.reply_to(message, "DB unavailable.")
        return
    admin = users_repo.get(message.from_user.id)
    if not admin or admin.get("role") != "admin":
        bot.reply_to(message, "Not authorized.")
        return
    # Usage: /setmaxquestions regular 5  OR  /setmaxquestions premium 10
    parts = message.text.strip().split()
    if len(parts) < 3 or parts[1] not in ("regular", "premium") or not parts[2].isdigit():
        bot.reply_to(message, "Usage: /setmaxquestions regular|premium <num>")
        return
    key = f"max_questions_{parts[1]}"
    SettingsRepository(db).set(key, int(parts[2]))
    bot.reply_to(message, f"{key} set to {parts[2]}")


@bot.message_handler(commands=["maintenancemode"]) 
def admin_maintenance_mode(message: Message):
    if not users_repo:
        bot.reply_to(message, "DB unavailable.")
        return
    admin = users_repo.get(message.from_user.id)
    if not admin or admin.get("role") != "admin":
        bot.reply_to(message, "Not authorized.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /maintenancemode on|off")
        return
    val = parts[1].lower() in ("on", "true", "1", "yes")
    SettingsRepository(db).set("maintenance_mode", val)
    bot.reply_to(message, f"maintenance_mode set to {val}")


@bot.message_handler(commands=["addadmin"]) 
def admin_add_admin(message: Message):
    if not users_repo:
        bot.reply_to(message, "DB unavailable.")
        return
    user_id = message.from_user.id
    req = users_repo.get(user_id)
    is_owner = (user_id == cfg.owner_id)
    if not is_owner and (not req or req.get("role") != "admin"):
        bot.reply_to(message, "Not authorized.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /addadmin <user_id>")
        return
    target_id = int(parts[1])
    users_repo.set_admin(target_id)
    bot.reply_to(message, f"User {target_id} promoted to admin.")


@bot.message_handler(commands=["addpremium"])
def admin_add_premium(message: Message):
    if not users_repo:
        bot.reply_to(message, "DB unavailable.")
        return
    user_id = message.from_user.id
    req = users_repo.get(user_id)
    is_owner = (user_id == cfg.owner_id)
    if not is_owner and (not req or req.get("role") != "admin"):
        bot.reply_to(message, "Not authorized.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /addpremium <user_id> [days]")
        return
    target_id = int(parts[1])
    days = int(parts[2]) if len(parts) > 2 else 30
    users_repo.set_premium(target_id, days)
    bot.reply_to(message, f"User {target_id} is now Premium for {days} days.")


@bot.message_handler(commands=["removeadmin"]) 
def admin_remove_admin(message: Message):
    if not users_repo:
        bot.reply_to(message, "DB unavailable.")
        return
    req = users_repo.get(message.from_user.id)
    if not req or req.get("role") != "admin":
        bot.reply_to(message, "Not authorized.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /removeadmin <user_id>")
        return
    target_id = int(parts[1])
    users_repo.set_role(target_id, "user")
    bot.reply_to(message, f"User {target_id} demoted from admin.")