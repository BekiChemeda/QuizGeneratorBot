import time
from datetime import datetime, timedelta, timezone
from pymongo.errors import DuplicateKeyError
from telebot import TeleBot, apihelper
from telebot.apihelper import ApiTelegramException
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    Message,
)
from bson import ObjectId
import re

from .config import get_config
from .db import init_db, get_db
from .repositories.settings import SettingsRepository
from .repositories.users import UsersRepository
from .repositories.channels import ChannelsRepository
from .repositories.payments import PaymentsRepository
from .repositories.schedules import SchedulesRepository
from .repositories.stats import StatsRepository
from .repositories.quizzes import QuizzesRepository
from .repositories.battles import BattlesRepository
from .repositories.progress import ProgressRepository
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
battles_repo = BattlesRepository(db) if db is not None else None
progress_repo = ProgressRepository(db) if db is not None else None

bot = TeleBot(cfg.bot_token)
BOT_INFO = None

def get_bot_info():
    global BOT_INFO
    if BOT_INFO is None:
        try:
            BOT_INFO = bot.get_me()
        except Exception:
            # Fallback if network is unreachable on startup
            class MockBot:
                def __init__(self):
                    self.username = "Bot"
                    self.id = 0
            return MockBot()
    return BOT_INFO
if db is not None:
    scheduler = QuizScheduler(db, bot)
    scheduler.start()

pending_notes: dict[int, dict] = {}
pending_subscriptions: dict[int, dict] = {}
pending_keys: dict[int, dict] = {}
pending_quizzes: dict[int, dict] = {}  # Interactive quiz sessions
pending_battles: dict[int, dict] = {}  # Battle quiz sessions


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
            ignored_errors = [
                "query is too old",
                "Network is unreachable",
                "Read timed out",
                "connection was forcibly closed",
                "Max retries exceeded",
                "Bad Request: query ID is invalid"
            ]
            
            is_ignored = any(ignored in str(e) for ignored in ignored_errors)
            
            logger.error(error_msg)
            if not is_ignored:
                logger.error(traceback.format_exc())
            
            if db is not None and not is_ignored:
                notify_admins(bot, f"⚠️ Error in `{func.__name__}`:\n`{str(e)}`", db)
            
            if user_id and not is_ignored:
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
        InlineKeyboardButton("📈 Progress", callback_data="progress"),
        InlineKeyboardButton("🏆 Battles", callback_data="battle_menu"),
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
    
    # Check for deep link args
    parts = message.text.split()
    referrer_id = None
    deep_link_quiz_id = None
    deep_link_battle_id = None
    if len(parts) > 1:
        arg = parts[1]
        if arg.startswith("ref"):
            try:
                referrer_id = int(arg[3:])
            except ValueError:
                pass
        elif arg.startswith("quiz_"):
            deep_link_quiz_id = arg[5:]
        elif arg.startswith("battle_"):
            deep_link_battle_id = arg[7:]

    users_repo.upsert_user(user_id, username)

    # Store pending referral — actual credit happens after channel join
    if referrer_id and referrer_id != user_id:
        existing = users_repo.get(user_id) or {}
        if not existing.get("invited_by"):
            users_repo.set_pending_referrer(user_id, referrer_id)

    if cfg.maintenance_mode:
        bot.send_message(user_id, "The bot is currently under maintenance. Please try again later.")
        return

    if not is_subscribed(bot, user_id):
        channels = settings_repo.get("force_channels", cfg.force_channels) if settings_repo else cfg.force_channels
        channels_txt = "\n".join(channels) if channels else ""
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔄 Check Subscription", callback_data="home"))
        
        msg_text = (
            "📢 <b>Requirement to Continue</b>\n\n"
            "To ensure you receive updates on new features, bug fixes, and announcements about new bots, "
            "please join the following channels. This is not for promotion, but for keeping you informed:\n\n"
            f"{channels_txt}"
        )
        
        bot.send_message(user_id, msg_text, parse_mode="HTML", reply_markup=kb)
        return

    # User is subscribed — process pending referral now
    _process_pending_referral(user_id, display_name)

    # Handle deep link quiz/battle
    if deep_link_quiz_id:
        _start_shared_quiz(user_id, deep_link_quiz_id)
        return
    if deep_link_battle_id:
        _start_battle_quiz(user_id, deep_link_battle_id)
        return

    text = (
        "<b>Welcome to SmartQuiz Bot!</b>\n\n"
        "Turn your notes into interactive questions effortlessly.\n\n"
        "✨ Features:\n"
        "- Convert study notes into quizzes\n"
        "- Choose between text or quiz mode\n"
        "- Deliver to PM or your channel\n"
        "- Configure delay and schedule delivery\n\n"
        f"Your referral link: https://t.me/{get_bot_info().username}?start=ref{user_id}\n"
        "Invite 2 users to get Premium!\n\n"
        "Your support makes this bot better!"
    )
    bot.send_message(user_id, text, parse_mode="HTML", reply_markup=main_menu(user_id), disable_web_page_preview=True)


def _process_pending_referral(user_id: int, display_name: str = "Someone"):
    """Process pending referral after user joins the required channels."""
    if not users_repo:
        return
    pending_ref = users_repo.get_pending_referrer(user_id)
    if pending_ref:
        if users_repo.set_referrer(user_id, pending_ref):
            try:
                bot.send_message(pending_ref, f"🎉 New user {display_name} joined via your link!")
                users_repo.check_and_reward_referral_milestone(pending_ref, bot, settings_repo)
            except Exception:
                pass
        users_repo.clear_pending_referrer(user_id)


@bot.callback_query_handler(func=lambda call: call.data == "home")
def handle_home(call: CallbackQuery):
    user_id = call.from_user.id
    if not is_subscribed(bot, user_id):
        channels = settings_repo.get("force_channels", cfg.force_channels) if settings_repo else cfg.force_channels
        channels_txt = "\n".join(channels) if channels else ""
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔄 Check Subscription", callback_data="home"))
        
        msg_text = (
            "📢 <b>Requirement to Continue</b>\n\n"
            "To ensure you receive updates on new features, bug fixes, and announcements about new bots, "
            "please join the following channels. This is not for promotion, but for keeping you informed:\n\n"
            f"{channels_txt}"
        )
        
        try:
            bot.edit_message_text(msg_text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
        except Exception:
            bot.send_message(user_id, msg_text, parse_mode="HTML", reply_markup=kb)
        return

    # User is now subscribed — process any pending referral
    display_name = call.from_user.first_name or call.from_user.username or "Someone"
    _process_pending_referral(user_id, display_name)

    pending_notes.pop(user_id, None)
    pending_quizzes.pop(user_id, None)
    pending_battles.pop(user_id, None)
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
        "📚 **Frequently Asked Questions (FAQs)**\n\n"
        "1️⃣ **How do I create a quiz?**\n"
        "Simply send a text note, upload a document (PDF, DOCX, PPTX, TXT), or paste a YouTube link. The AI will analyze the content and generate questions for you.\n\n"
        "2️⃣ **What file formats are supported?**\n"
        "We support PDF, Word (.docx), PowerPoint (.pptx), and Text (.txt) files. Max file size is 20MB.\n\n"
        "3️⃣ **Can I generate quizzes from YouTube?**\n"
        "Yes! Just send the YouTube URL. The bot extracts the transcript or audio to create your quiz.\n\n"
        "4️⃣ **Is there a limit on usage?**\n"
        "Free users can generate up to 5 quizzes with 10 questions each. Premium users enjoy higher limits (10 quizzes, 20 questions).\n\n"
        "5️⃣ **How do I get Premium?**\n"
        "Open the main menu and click on 💎 **Premium**. Follow the instructions there to upgrade your account.\n\n"
        "6️⃣ **What AI technology is used?**\n"
        "The bot is powered by Google's **Gemini AI**, ensuring high-quality and contextually accurate quiz questions.\n\n"
        "7️⃣ **Can I change the question type?**\n"
        "Yes! Go to **Settings** → **Question Type** to choose between 'Text' (multiple choice in message) or 'Poll' (Telegram native polls)."
    )
    bot.send_message(call.message.chat.id, text, parse_mode="Markdown", reply_markup=home_keyboard())

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
        InlineKeyboardButton("📈 Analytics", callback_data="admin_analytics"),
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
        InlineKeyboardButton("🔐 Force Subscription", callback_data="admin_manage_sub"),
        InlineKeyboardButton("💰 Set Premium Price", callback_data="admin_set_price"),
        InlineKeyboardButton("👥 Manage Users", callback_data="admin_users"),
        InlineKeyboardButton("🔍 Lookup User", callback_data="admin_lookup"),
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
    bot.answer_callback_query(call.id)
    
    try:
        bot.edit_message_text("🔧 **Admin Dashboard**", call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        bot.send_message(user_id, "🔧 **Admin Dashboard**", parse_mode="Markdown", reply_markup=kb)

# Manager handler (called by others as well)
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
    
    status_icon = "✅" if force else "❌"
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
    bot.answer_callback_query(call.id)
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
    bot.answer_callback_query(call.id)
    channel = call.data.replace("admin_rm_sub_", "")
    sr = SettingsRepository(db)
    channels = sr.get("force_channels", cfg.force_channels)
    if channel in channels:
        channels.remove(channel)
        sr.set("force_channels", channels)
    handle_admin_manage_sub(call)

@bot.callback_query_handler(func=lambda call: call.data == "admin_add_sub_prompt")
def prompt_add_force_channel(call: CallbackQuery):
    bot.answer_callback_query(call.id)
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


# Overview handler
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
        bot_member = bot.get_chat_member(chat_id, get_bot_info().id)
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
        bot_member = bot.get_chat_member(chat.id, get_bot_info().id)
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

    shares = quiz.get('share_count', 0)
    plays = quiz.get('play_count', 0)
    text = f"<b>{quiz.get('title')}</b>\n"
    text += f"Questions: {len(quiz.get('questions', []))}\n"
    text += f"Date: {quiz.get('created_at')}\n"
    if shares or plays:
        text += f"\n📊 Shared: {shares} | Played: {plays}\n"

    bot_username = get_bot_info().username or "SmartQuizBot"
    share_link = f"https://t.me/{bot_username}?start=quiz_{quiz_id}"

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("🔗 Share Link", url=f"https://t.me/share/url?url={share_link}&text=Try this quiz!"))
    kb.add(InlineKeyboardButton("⚔️ Challenge Friend", callback_data=f"startbattle_{quiz_id}"))
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
        
        bot_username = get_bot_info().username or "SmartQuizBot"

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
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    users_repo.reset_notes_if_new_day(user_id)

    if not is_subscribed(bot, user_id):
        bot.send_message(user_id, "Please Join All Our Channels!\n/start - To start again")
        return

    if not has_quota(db, user_id):
        bot.send_message(user_id, "You have reached your daily limit. Add your own Gemini API key in Settings to increase limits.")
        return

    pending_notes[user_id] = {"stage": "await_input_type"}
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
    url = (message.text or "").strip()
    
    if not url:
        bot.reply_to(message, "⚠️ Please send a valid YouTube URL.")
        return

    processing = bot.reply_to(message, "🔄 Fetching YouTube content (transcript or audio)...\nThis may take up to 60 seconds.")
    try:
        text, audio_data, mime_type, video_title, video_description = get_youtube_transcript(url)
        try:
            bot.delete_message(message.chat.id, processing.message_id)
        except Exception:
            pass
        
        # Use video title as the quiz title
        if video_title:
            pending_notes[user_id]["title"] = video_title
        
        if text:
            pending_notes[user_id]["note"] = text
        elif audio_data:
            pending_notes[user_id]["media_data"] = audio_data
            pending_notes[user_id]["mime_type"] = mime_type
            if video_description:
                context = video_description[:500] if len(video_description) > 500 else video_description
                pending_notes[user_id]["note"] = f"Video Description: {context}"
        else:
            bot.send_message(user_id, "❌ Could not fetch any content from this video.\n\nPossible reasons:\n• Video has no subtitles/captions\n• Video is age-restricted or region-locked\n• Audio could not be downloaded\n\nTry a different video.", reply_markup=home_keyboard())
            pending_notes.pop(user_id, None)
            return

        try:
            bot.delete_message(message.chat.id, message.message_id)
        except:
            pass
        ask_difficulty(user_id)
    except ValueError as e:
        logger.error(f"YouTube URL Error: {e}")
        try:
            bot.delete_message(message.chat.id, processing.message_id)
        except:
            pass
        bot.send_message(user_id, f"⚠️ {e}", reply_markup=home_keyboard())
        pending_notes.pop(user_id, None)
    except RuntimeError as e:
        logger.error(f"YouTube Error: {e}")
        try:
            bot.delete_message(message.chat.id, processing.message_id)
        except:
            pass
        bot.send_message(user_id, f"❌ {e}", reply_markup=home_keyboard())
        pending_notes.pop(user_id, None)
    except Exception as e:
        logger.error(f"YouTube Error: {e}")
        try:
            bot.delete_message(message.chat.id, processing.message_id)
        except:
            pass
        bot.send_message(user_id, "❌ Failed to process YouTube video.\n\nPlease try:\n• A different/shorter video\n• A video with English subtitles\n• Check the URL is correct", reply_markup=home_keyboard())
        pending_notes.pop(user_id, None)


@bot.message_handler(content_types=["audio", "voice"], func=lambda m: m.from_user and m.from_user.id in pending_notes and pending_notes[m.from_user.id].get("stage") == "await_audio")
@error_handler
def handle_audio_submission(message: Message):
    user_id = message.from_user.id
    
    file_info = message.voice or message.audio
    if not file_info:
        bot.reply_to(message, "⚠️ Could not detect audio file. Please send a voice note or audio file (MP3/OGG/WAV).")
        return

    if file_info.file_size and file_info.file_size > 20 * 1024 * 1024:
        bot.reply_to(message, "⚠️ File is too big. Please send audio files under 20MB.")
        return

    file_id = file_info.file_id
    mime_type = getattr(file_info, "mime_type", None) or "audio/ogg"
    
    processing = bot.reply_to(message, "🔄 Processing audio...")
    try:
        file_path = bot.get_file(file_id).file_path
        downloaded_file = bot.download_file(file_path)
        
        if not downloaded_file or len(downloaded_file) == 0:
            raise ValueError("Download returned empty data")
              
        pending_notes[user_id]["media_data"] = downloaded_file
        pending_notes[user_id]["mime_type"] = mime_type
        pending_notes[user_id]["title"] = "Audio Note"
        
        try:
            bot.delete_message(message.chat.id, processing.message_id)
        except:
            pass
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except:
            pass
        ask_difficulty(user_id)
    except Exception as e:
        logger.error(f"Audio processing error: {e}")
        try:
            bot.edit_message_text("❌ Error processing audio. Please try again with a different file.", message.chat.id, processing.message_id)
        except:
            bot.send_message(user_id, "❌ Error processing audio. Please try again.")


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

        # Update streak and progress
        if users_repo:
            users_repo.update_streak(user_id)
        if progress_repo and target == user_id:
            progress_repo.record_quiz_attempt(
                user_id=user_id,
                quiz_id="",
                score=len(questions),
                total=len(questions),
                topic=quiz_title,
            )

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

    kb = admin_keyboard()
    bot.send_message(user_id, "🔧 **Admin Dashboard**", parse_mode="Markdown", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_") or call.data == "close_admin")
@error_handler
def handle_admin_callbacks(call: CallbackQuery):
    user_id = call.from_user.id
    admin = users_repo.get(user_id)
    is_owner = (user_id == cfg.owner_id)
    if not is_owner and (not admin or admin.get("role") != "admin"):
        bot.answer_callback_query(call.id, "Not authorized.")
        return

    bot.answer_callback_query(call.id)

    if call.data == "admin_broadcast":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        msg = bot.send_message(user_id, "📢 **Broadcast Message**\n\nSend your message.")
        bot.register_next_step_handler(msg, process_broadcast)
    
    elif call.data == "admin_set_price":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        msg = bot.send_message(user_id, "💰 **Set Price**\n\nSend digits.")
        bot.register_next_step_handler(msg, process_set_price)
    
    elif call.data == "admin_analytics":
        _show_admin_analytics(call)

    elif call.data == "admin_lookup":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        msg = bot.send_message(user_id, "🔍 **User Lookup**\n\nEnter the User ID or Username (with or without @).")
        bot.register_next_step_handler(msg, process_admin_user_lookup)

    elif call.data == "admin_users":
        total_users = users_repo.count_all()
        premium = users_repo.count_premium()
        text = (
            f"👥 <b>User Management</b>\n\n"
            f"Total Users: {total_users}\n"
            f"Premium: {premium}\n\n"
            f"Use /addpremium, /addadmin, /removeadmin commands."
        )
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔙 Back", callback_data="admin_menu"))
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
        except:
            bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)
            
    elif call.data == "close_admin":
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
            
    elif call.data == "admin_settings_overview":
        handle_admin_settings_overview(call)
        
    elif call.data == "admin_manage_sub":
        handle_admin_manage_sub(call)
        
    elif call.data == "admin_menu":
        handle_admin_menu_btn(call)

    elif call.data.startswith("admin_give_prem_"):
        target_id = int(call.data.split("_")[-1])
        users_repo.set_premium(target_id, 30) # Default 30 days
        bot.answer_callback_query(call.id, f"User {target_id} is now Premium for 30 days.", show_alert=True)
        # Refresh details
        user_doc = users_repo.get(target_id)
        if user_doc: _show_user_details(user_id, user_doc)

    elif call.data.startswith("admin_give_admin_"):
        target_id = int(call.data.split("_")[-1])
        if user_id != cfg.owner_id:
            bot.answer_callback_query(call.id, "Only owner can promote admins.", show_alert=True)
            return
        users_repo.set_admin(target_id)
        bot.answer_callback_query(call.id, f"User {target_id} is now an Admin.", show_alert=True)
        # Refresh details
        user_doc = users_repo.get(target_id)
        if user_doc: _show_user_details(user_id, user_doc)

def process_set_price(message: Message):
    user_id = message.from_user.id
    if not message.text or not message.text.isdigit():
        bot.reply_to(message, "Error.")
        return
    SettingsRepository(db).set("premium_price", int(message.text))
    bot.reply_to(message, "Done.")

def process_broadcast(message: Message):
    user_id = message.from_user.id
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Send", callback_data="confirm_broadcast"), InlineKeyboardButton("❌ Cancel", callback_data="cancel_broadcast"))
    pending_notes[user_id] = {"broadcast_msg": message} 
    bot.reply_to(message, "Confirm broadcast?", reply_markup=kb)

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
                # Mark as NOT blocked if send was successful
                users_repo.update_blocked_status(target_id, False)
            except ApiTelegramException as e:
                if e.error_code == 403: # Forbidden: bot was blocked by the user
                    users_repo.update_blocked_status(target_id, True)
                    logger.info(f"User {target_id} has blocked the bot.")
                else:
                    logger.error(f"Failed to send broadcast to {target_id}: {e}")
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

def process_admin_user_lookup(message: Message):
    admin_id = message.from_user.id
    query = message.text.strip()
    
    user_doc = None
    if query.isdigit():
        user_doc = users_repo.get(int(query))
    
    if not user_doc:
        user_doc = users_repo.get_by_username(query)
    
    if not user_doc:
        bot.reply_to(message, "❌ **User not found.**", parse_mode="Markdown", reply_markup=admin_keyboard())
        return

    _show_user_details(admin_id, user_doc)

def _show_user_details(admin_id: int, user_doc: dict):
    target_id = user_doc["id"]
    username = user_doc.get("username") or "N/A"
    role = user_doc.get("role", "user")
    u_type = user_doc.get("type", "regular")
    reg_at = user_doc.get("registered_at", "N/A")
    if hasattr(reg_at, "strftime"): reg_at = reg_at.strftime("%Y-%m-%d %H:%M")
    
    premium_until = user_doc.get("premium_until")
    status = "Regular"
    if u_type == "premium":
        if premium_until:
            if hasattr(premium_until, "strftime"): 
                status = f"⭐ Premium (until {premium_until.strftime('%Y-%m-%d')})"
            else:
                status = "⭐ Premium"
        else:
            status = "⭐ Premium (Permanent)"
            
    is_blocked = user_doc.get("is_blocked", False)
    blocked_status = "🔴 BLOCKED" if is_blocked else "🟢 Active"
    
    streak = users_repo.get_streak_info(target_id)
    referrals = user_doc.get("referral_count", 0)
    
    # Get quiz stats
    total_quizzes = quizzes_repo.collection.count_documents({"user_id": target_id}) if quizzes_repo else 0
    
    text = (
        f"👤 **User Details**\n\n"
        f"• **ID**: `{target_id}`\n"
        f"• **Username**: @{username}\n"
        f"• **Role**: `{role}`\n"
        f"• **Status**: {status}\n"
        f"• **Bot Interaction**: {blocked_status}\n\n"
        f"📈 **Activity**:\n"
        f"• Registered: {reg_at}\n"
        f"• Streak: {streak.get('current', 0)} days (Best: {streak.get('best', 0)})\n"
        f"• Quizzes Generated: {total_quizzes}\n\n"
        f"🔗 **Referrals**: {referrals}\n"
        f"• Invited By: `{user_doc.get('invited_by', 'None')}`"
    )
    
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("⭐ Add Premium", callback_data=f"admin_give_prem_{target_id}"),
        InlineKeyboardButton("👮 Promote Admin", callback_data=f"admin_give_admin_{target_id}"),
        InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_menu")
    )
    
    bot.send_message(admin_id, text, parse_mode="Markdown", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data in ["confirm_broadcast", "cancel_broadcast"])
@error_handler
def handle_broadcast_confirmation(call: CallbackQuery):
    execute_broadcast(call)


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


# ═══════════════════════════════════════════════════════
# ▶ ADMIN ANALYTICS DASHBOARD
# ═══════════════════════════════════════════════════════

def _show_admin_analytics(call: CallbackQuery):
    user_id = call.from_user.id
    try:
        total_users = users_repo.count_all()
        premium_users = users_repo.count_premium()
        admin_count = users_repo.count_admins()
        api_key_users = users_repo.count_with_api_key()
        active_today = users_repo.count_active_today()
        active_week = users_repo.count_active_week()
        new_today = users_repo.count_new_today()
        new_week = users_repo.count_new_week()
        unblocked = users_repo.count_unblocked()
        top_inviters = users_repo.get_top_inviters(5)

        total_quizzes = quizzes_repo.count_all() if quizzes_repo else 0
        quizzes_today = quizzes_repo.count_today() if quizzes_repo else 0

        # Build top inviters text
        top_text = ""
        for i, inv in enumerate(top_inviters, 1):
            name = inv.get("username") or str(inv.get("id"))
            count = inv.get("referral_count", 0)
            medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i - 1] if i <= 5 else f"{i}."
            top_text += f"  {medal} @{name} — {count} invites\n"

        if not top_text:
            top_text = "  No referrals yet\n"

        text = (
            "📈 <b>Analytics Dashboard</b>\n\n"
            "━━━━━ <b>👥 Users</b> ━━━━━\n"
            f"  Total: <b>{total_users}</b>\n"
            f"  Premium: <b>{premium_users}</b> ⭐\n"
            f"  Admins: <b>{admin_count}</b>\n"
            f"  Unblocked Users: <b>{unblocked}</b> ✅\n"
            f"  With Own API Key: <b>{api_key_users}</b> 🔑\n\n"
            "━━━━━ <b>📊 Activity</b> ━━━━━\n"
            f"  Active Today: <b>{active_today}</b>\n"
            f"  Active This Week: <b>{active_week}</b>\n"
            f"  New Users Today: <b>{new_today}</b>\n"
            f"  New This Week: <b>{new_week}</b>\n\n"
            "━━━━━ <b>📝 Quizzes</b> ━━━━━\n"
            f"  Total Generated: <b>{total_quizzes}</b>\n"
            f"  Generated Today: <b>{quizzes_today}</b>\n\n"
            "━━━━━ <b>🏆 Top Inviters</b> ━━━━━\n"
            f"{top_text}\n"
            "━━━━━ <b>📉 Conversion</b> ━━━━━\n"
            f"  Premium Rate: <b>{round(premium_users / total_users * 100, 1) if total_users else 0}%</b>\n"
            f"  API Key Rate: <b>{round(api_key_users / total_users * 100, 1) if total_users else 0}%</b>\n"
        )

        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔄 Refresh", callback_data="admin_analytics"))
        kb.add(InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_menu"))

        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
        except Exception:
            bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        bot.send_message(user_id, f"Error loading analytics: {e}")


# ═══════════════════════════════════════════════════════
# ▶ PROGRESS DASHBOARD & STREAKS
# ═══════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda call: call.data == "progress")
@error_handler
def handle_progress(call: CallbackQuery):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)

    streak = users_repo.get_streak_info(user_id) if users_repo else {"current": 0, "best": 0}
    stats = progress_repo.get_user_stats(user_id) if progress_repo else {}

    current_streak = streak.get("current", 0)
    best_streak = streak.get("best", 0)
    total_quizzes = stats.get("total_quizzes", 0)
    total_questions = stats.get("total_questions", 0)
    total_correct = stats.get("total_correct", 0)
    avg_accuracy = stats.get("avg_accuracy", 0)
    best_topic = stats.get("best_topic", "N/A")

    # Streak fire emoji scaling
    fire = "🔥" * min(current_streak, 5) if current_streak > 0 else "❄️"

    text = (
        f"📈 <b>Your Progress Dashboard</b>\n\n"
        f"━━━━━ <b>🔥 Streaks</b> ━━━━━\n"
        f"  Current: <b>{current_streak} days</b> {fire}\n"
        f"  Best: <b>{best_streak} days</b> 🏅\n\n"
        f"━━━━━ <b>📊 Stats</b> ━━━━━\n"
        f"  Quizzes Taken: <b>{total_quizzes}</b>\n"
        f"  Questions Answered: <b>{total_questions}</b>\n"
        f"  Correct Answers: <b>{total_correct}</b>\n"
        f"  Average Accuracy: <b>{avg_accuracy}%</b>\n\n"
        f"━━━━━ <b>🎯 Best Topic</b> ━━━━━\n"
        f"  {best_topic}\n\n"
        f"💡 <i>Generate more quizzes to keep your streak alive!</i>"
    )

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📝 Generate Quiz", callback_data="generate"))
    kb.add(InlineKeyboardButton("🔙 Home", callback_data="home"))

    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
    except Exception:
        bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)


# ═══════════════════════════════════════════════════════
# ▶ QUIZ BATTLE MODE
# ═══════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda call: call.data == "battle_menu")
@error_handler
def handle_battle_menu(call: CallbackQuery):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)

    battles = battles_repo.get_user_battles(user_id, limit=5) if battles_repo else []

    text = "🏆 <b>Quiz Battles</b>\n\n"
    if battles:
        text += "<b>Recent Battles:</b>\n"
        for b in battles:
            status = b.get("status", "waiting")
            quiz = quizzes_repo.get_quiz(str(b.get("quiz_id", ""))) if quizzes_repo else None
            title = quiz.get("title", "Quiz") if quiz else "Quiz"

            if status == "waiting":
                text += f"⏳ {title} — waiting for opponent\n"
            else:
                c_score = b.get("challenger_score", 0)
                o_score = b.get("opponent_score", 0)
                total = b.get("challenger_total", 0)
                if b.get("challenger_id") == user_id:
                    icon = "🎉" if c_score > o_score else ("🤝" if c_score == o_score else "😤")
                    text += f"{icon} {title}: You {c_score}/{total} vs Opponent {o_score}/{total}\n"
                else:
                    icon = "🎉" if o_score > c_score else ("🤝" if c_score == o_score else "😤")
                    text += f"{icon} {title}: You {o_score}/{total} vs Challenger {c_score}/{total}\n"
    else:
        text += "No battles yet!\n\n"

    text += "\n💡 <i>Go to My Quizzes → select a quiz → ⚔️ Challenge Friend</i>"

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📂 My Quizzes", callback_data="my_quizzes"))
    kb.add(InlineKeyboardButton("🔙 Home", callback_data="home"))

    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
    except Exception:
        bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("startbattle_"))
@error_handler
def handle_create_battle(call: CallbackQuery):
    """Challenger creates a battle — they take the quiz first, then get a link."""
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    quiz_id = call.data.split("_", 1)[1]
    quiz = quizzes_repo.get_quiz(quiz_id) if quizzes_repo else None

    if not quiz:
        bot.send_message(user_id, "Quiz not found.")
        return

    questions = quiz.get("questions", [])
    if not questions:
        bot.send_message(user_id, "This quiz has no questions.")
        return

    # Start interactive quiz for the challenger
    pending_battles[user_id] = {
        "quiz_id": quiz_id,
        "questions": questions,
        "current_index": 0,
        "score": 0,
        "total": len(questions),
        "mode": "challenger",
        "title": quiz.get("title", "Quiz"),
    }

    bot.send_message(user_id, f"⚔️ <b>Battle Mode!</b>\n\nTake the quiz first, then challenge your friend.\n\nQuiz: <b>{quiz.get('title')}</b>\nQuestions: {len(questions)}", parse_mode="HTML")
    _send_battle_question(user_id)


def _start_battle_quiz(user_id: int, battle_id: str):
    """Opponent starts a battle from deep link."""
    if not battles_repo:
        bot.send_message(user_id, "Battles unavailable.")
        return

    battle = battles_repo.get_battle(battle_id)
    if not battle:
        bot.send_message(user_id, "Battle not found or expired.", reply_markup=home_keyboard())
        return

    if battle.get("status") != "waiting":
        c_score = battle.get("challenger_score", 0)
        o_score = battle.get("opponent_score", 0)
        total = battle.get("challenger_total", 0)
        bot.send_message(user_id, f"This battle is already completed!\n\nChallenger: {c_score}/{total}\nOpponent: {o_score}/{total}", reply_markup=home_keyboard())
        return

    if battle.get("challenger_id") == user_id:
        bot.send_message(user_id, "You can't battle yourself! Share the link with a friend.", reply_markup=home_keyboard())
        return

    quiz = quizzes_repo.get_quiz(str(battle.get("quiz_id", ""))) if quizzes_repo else None
    if not quiz:
        bot.send_message(user_id, "Quiz for this battle not found.", reply_markup=home_keyboard())
        return

    questions = quiz.get("questions", [])
    pending_battles[user_id] = {
        "battle_id": battle_id,
        "quiz_id": str(battle.get("quiz_id")),
        "questions": questions,
        "current_index": 0,
        "score": 0,
        "total": len(questions),
        "mode": "opponent",
        "title": quiz.get("title", "Quiz"),
        "challenger_id": battle.get("challenger_id"),
        "challenger_score": battle.get("challenger_score", 0),
    }

    bot.send_message(user_id, f"⚔️ <b>Battle Challenge!</b>\n\nSomeone challenged you to a quiz battle!\n\nQuiz: <b>{quiz.get('title')}</b>\nQuestions: {len(questions)}", parse_mode="HTML")
    _send_battle_question(user_id)


def _send_battle_question(user_id: int):
    state = pending_battles.get(user_id)
    if not state:
        return

    idx = state["current_index"]
    questions = state["questions"]

    if idx >= len(questions):
        _finish_battle(user_id)
        return

    q = questions[idx]
    letters = ["A", "B", "C", "D"]
    text = f"<b>Question {idx + 1}/{len(questions)}</b>\n\n"
    text += f"{q['question']}\n\n"
    for i, c in enumerate(q["choices"]):
        prefix = letters[i] if i < len(letters) else str(i + 1)
        text += f"{prefix}. {c}\n"

    kb = InlineKeyboardMarkup(row_width=2)
    for i in range(len(q["choices"])):
        letter = letters[i] if i < len(letters) else str(i + 1)
        kb.add(InlineKeyboardButton(letter, callback_data=f"ba_{i}"))

    bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("ba_"))
@error_handler
def handle_battle_answer(call: CallbackQuery):
    user_id = call.from_user.id
    state = pending_battles.get(user_id)

    if not state:
        bot.answer_callback_query(call.id, "No active battle.")
        return

    bot.answer_callback_query(call.id)
    chosen = int(call.data.split("_")[1])
    idx = state["current_index"]
    q = state["questions"][idx]
    correct = q.get("answer_index", -1)

    letters = ["A", "B", "C", "D"]
    if chosen == correct:
        state["score"] += 1
        result_text = "✅ Correct!"
    else:
        correct_letter = letters[correct] if correct < len(letters) else "?"
        result_text = f"❌ Wrong! Answer: {correct_letter}"

    state["current_index"] += 1
    try:
        bot.edit_message_text(
            f"{result_text}\n\nScore: {state['score']}/{state['current_index']}",
            call.message.chat.id, call.message.message_id
        )
    except Exception:
        pass

    import threading
    threading.Timer(1.0, _send_battle_question, args=[user_id]).start()


def _finish_battle(user_id: int):
    state = pending_battles.pop(user_id, None)
    if not state:
        return

    score = state["score"]
    total = state["total"]
    title = state["title"]

    if state["mode"] == "challenger":
        # Create battle record
        battle_id = battles_repo.create_battle(
            challenger_id=user_id,
            quiz_id=state["quiz_id"],
            challenger_score=score,
            challenger_total=total,
        )

        bot_username = get_bot_info().username or "SmartQuizBot"
        battle_link = f"https://t.me/{bot_username}?start=battle_{battle_id}"

        text = (
            f"🏆 <b>Battle Created!</b>\n\n"
            f"Quiz: <b>{title}</b>\n"
            f"Your Score: <b>{score}/{total}</b>\n\n"
            f"Share this link to challenge a friend:\n"
            f"<code>{battle_link}</code>\n\n"
            f"⏳ Waiting for opponent..."
        )

        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("📤 Share Battle", url=f"https://t.me/share/url?url={battle_link}&text=I challenge you to a quiz battle! 🏆"))
        kb.add(InlineKeyboardButton("🔙 Home", callback_data="home"))
        bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)

    elif state["mode"] == "opponent":
        battle_id = state.get("battle_id")
        challenger_id = state.get("challenger_id")
        challenger_score = state.get("challenger_score", 0)

        # Record opponent score
        if battles_repo and battle_id:
            battles_repo.set_opponent_score(battle_id, user_id, score, total)

        # Determine winner
        if score > challenger_score:
            result = "🎉 <b>YOU WIN!</b>"
            challenger_result = "😤 You lost the battle!"
        elif score < challenger_score:
            result = "😤 <b>You lost!</b>"
            challenger_result = "🎉 You won the battle!"
        else:
            result = "🤝 <b>It's a tie!</b>"
            challenger_result = "🤝 It's a tie!"

        text = (
            f"⚔️ <b>Battle Results</b>\n\n"
            f"Quiz: <b>{title}</b>\n\n"
            f"{result}\n\n"
            f"📊 Your Score: <b>{score}/{total}</b>\n"
            f"📊 Challenger: <b>{challenger_score}/{total}</b>"
        )
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔙 Home", callback_data="home"))
        bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)

        # Notify challenger
        if challenger_id:
            try:
                notify_text = (
                    f"⚔️ <b>Battle Result!</b>\n\n"
                    f"Quiz: <b>{title}</b>\n"
                    f"{challenger_result}\n\n"
                    f"📊 You: <b>{challenger_score}/{total}</b>\n"
                    f"📊 Opponent: <b>{score}/{total}</b>"
                )
                bot.send_message(challenger_id, notify_text, parse_mode="HTML", reply_markup=home_keyboard())
            except Exception:
                pass

    # Record progress for both
    if progress_repo:
        progress_repo.record_quiz_attempt(user_id, state.get("quiz_id", ""), score, total, title)
    if users_repo:
        users_repo.update_streak(user_id)


# ═══════════════════════════════════════════════════════
# ▶ SHAREABLE QUIZ LINKS — Interactive Quiz Taking
# ═══════════════════════════════════════════════════════

def _start_shared_quiz(user_id: int, quiz_id: str):
    """Start an interactive quiz from a shared deep link."""
    if not quizzes_repo:
        bot.send_message(user_id, "Quizzes unavailable.", reply_markup=home_keyboard())
        return

    quiz = quizzes_repo.get_quiz(quiz_id)
    if not quiz:
        bot.send_message(user_id, "Quiz not found or expired.", reply_markup=home_keyboard())
        return

    questions = quiz.get("questions", [])
    if not questions:
        bot.send_message(user_id, "This quiz has no questions.", reply_markup=home_keyboard())
        return

    # Track play count
    quizzes_repo.increment_play_count(quiz_id)

    pending_quizzes[user_id] = {
        "quiz_id": quiz_id,
        "questions": questions,
        "current_index": 0,
        "score": 0,
        "total": len(questions),
        "title": quiz.get("title", "Quiz"),
    }

    owner_name = ""
    if quiz.get("user_id"):
        owner = users_repo.get(quiz["user_id"]) if users_repo else None
        if owner:
            owner_name = f"\nCreated by: @{owner.get('username', 'unknown')}"

    bot.send_message(
        user_id,
        f"📋 <b>Shared Quiz</b>\n\n"
        f"<b>{quiz.get('title')}</b>{owner_name}\n"
        f"Questions: {len(questions)}\n\n"
        f"Let's start! 🚀",
        parse_mode="HTML"
    )
    _send_shared_question(user_id)


def _send_shared_question(user_id: int):
    state = pending_quizzes.get(user_id)
    if not state:
        return

    idx = state["current_index"]
    questions = state["questions"]

    if idx >= len(questions):
        _finish_shared_quiz(user_id)
        return

    q = questions[idx]
    letters = ["A", "B", "C", "D"]
    text = f"<b>Question {idx + 1}/{len(questions)}</b>\n\n"
    text += f"{q['question']}\n\n"
    for i, c in enumerate(q["choices"]):
        prefix = letters[i] if i < len(letters) else str(i + 1)
        text += f"{prefix}. {c}\n"

    kb = InlineKeyboardMarkup(row_width=2)
    for i in range(len(q["choices"])):
        letter = letters[i] if i < len(letters) else str(i + 1)
        kb.add(InlineKeyboardButton(letter, callback_data=f"qa_{i}"))

    bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("qa_"))
@error_handler
def handle_shared_quiz_answer(call: CallbackQuery):
    user_id = call.from_user.id
    state = pending_quizzes.get(user_id)

    if not state:
        bot.answer_callback_query(call.id, "No active quiz.")
        return

    bot.answer_callback_query(call.id)
    chosen = int(call.data.split("_")[1])
    idx = state["current_index"]
    q = state["questions"][idx]
    correct = q.get("answer_index", -1)

    letters = ["A", "B", "C", "D"]
    if chosen == correct:
        state["score"] += 1
        result_text = "✅ Correct!"
    else:
        correct_letter = letters[correct] if correct < len(letters) else "?"
        result_text = f"❌ Wrong! Answer: {correct_letter}"

    explanation = q.get("explanation", "")
    if explanation:
        result_text += f"\n💡 {explanation}"

    state["current_index"] += 1
    try:
        bot.edit_message_text(
            f"{result_text}\n\nScore: {state['score']}/{state['current_index']}",
            call.message.chat.id, call.message.message_id
        )
    except Exception:
        pass

    import threading
    threading.Timer(1.5, _send_shared_question, args=[user_id]).start()


def _finish_shared_quiz(user_id: int):
    state = pending_quizzes.pop(user_id, None)
    if not state:
        return

    score = state["score"]
    total = state["total"]
    title = state["title"]
    accuracy = round((score / total) * 100, 1) if total else 0

    # Performance rating
    if accuracy >= 90:
        rating = "🌟 Outstanding!"
    elif accuracy >= 70:
        rating = "👏 Great job!"
    elif accuracy >= 50:
        rating = "👍 Not bad!"
    else:
        rating = "📚 Keep studying!"

    text = (
        f"🏁 <b>Quiz Complete!</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"📊 Score: <b>{score}/{total}</b>\n"
        f"📈 Accuracy: <b>{accuracy}%</b>\n\n"
        f"{rating}"
    )

    kb = InlineKeyboardMarkup()
    # Let them share the same quiz
    bot_username = get_bot_info().username or "SmartQuizBot"
    share_link = f"https://t.me/{bot_username}?start=quiz_{state['quiz_id']}"
    kb.add(InlineKeyboardButton("📤 Share This Quiz", url=f"https://t.me/share/url?url={share_link}&text=I scored {score}/{total}! Can you beat me?"))
    kb.add(InlineKeyboardButton("🔙 Home", callback_data="home"))
    bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)

    # Record progress
    if progress_repo:
        progress_repo.record_quiz_attempt(user_id, state.get("quiz_id", ""), score, total, title)
    if users_repo:
        users_repo.update_streak(user_id)


print("Bot running...")
if __name__ == "__main__":
    bot.infinity_polling()
