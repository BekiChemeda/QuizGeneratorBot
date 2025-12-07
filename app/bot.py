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
from .services.gemini import generate_questions, validate_gemini_api_key
from .services.file_parser import fetch_and_parse_file, chunk_text
from .services.quota import (
    has_quota,
    can_submit_note_now,
    update_last_note_time,
    reset_notes_if_new_day,
    increment_quota,
    increase_total_notes,
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
except Exception:
    db = None

settings_repo = SettingsRepository(db) if db is not None else None
users_repo = UsersRepository(db) if db is not None else None
channels_repo = ChannelsRepository(db) if db is not None else None
payments_repo = PaymentsRepository(db) if db is not None else None
schedules_repo = SchedulesRepository(db) if db is not None else None

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


def main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("📝 Generate", callback_data="generate"),
        InlineKeyboardButton("👤 Profile", callback_data="profile"),
    )
    kb.row(
        InlineKeyboardButton("📢 My Channels", callback_data="channels"),
        InlineKeyboardButton("⏰ Schedule", callback_data="schedule_menu"),
    )
    kb.row(
        InlineKeyboardButton("ℹ️ About", callback_data="about"),
        InlineKeyboardButton("🆘 FAQs", callback_data="faq"),
    )
    kb.row(
        InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
        InlineKeyboardButton("👨‍💻 Developer", url="https://t.me/Bek_i"),
    )
    return kb


@bot.message_handler(commands=["start"]) 
@error_handler
def handle_start(message: Message):
    user_id = message.chat.id
    username = message.from_user.username or "No"

    users_repo.upsert_user(user_id, username)

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
        "Your support makes this bot better!"
    )
    bot.send_message(user_id, text, parse_mode="HTML", reply_markup=main_menu())


@bot.callback_query_handler(func=lambda call: call.data == "profile")
def handle_profile(call: CallbackQuery):
    user_id = call.from_user.id
    user = users_repo.get(user_id)
    if not user:
        bot.answer_callback_query(call.id, "User not found. Press /start")
        return

    premium_status = "Premium" if user.get("type") == "premium" else "Free"
    premium_since = user.get("premium_since")
    premium_since_str = premium_since.strftime("%Y-%m-%d") if premium_since else "N/A"

    text = (
        f"<b>User Profile</b>\n"
        f"<b>Name:</b> {call.from_user.full_name} <b>ID:</b> <code>{user_id}</code>\n"
        f"<b>Registered At:</b> {user.get('registered_at','')}\n"
        f"<b>Status:</b> {premium_status}\n"
        f"<b>Premium Since:</b> {premium_since_str}\n"
        f"<b>Last Used:</b> {user.get('last_note_time','Never')}\n"
        f"<b>Notes Today:</b> {user.get('notes_today',0)}\n"
        f"<b>Total Notes:</b> {user.get('total_notes',0)}\n"
        f"<b>Question Type:</b> {user.get('default_question_type','text').capitalize()}\n"
        f"<b>Questions Per Note:</b> {user.get('questions_per_note',5)}\n"
    )
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_message(user_id, text, parse_mode="HTML", reply_markup=home_keyboard())


# Channels Management
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
    bot.send_message(user_id, text, reply_markup=home_keyboard())


@bot.message_handler(func=lambda m: m.forward_from_chat is not None and m.forward_from_chat.type == "channel")
def handle_channel_forward(message: Message):
    chat = message.forward_from_chat
    chat_id = chat.id
    title = chat.title or "Channel"
    username = chat.username
    user_id = message.from_user.id
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

    if not can_submit_note_now(db, user_id, cooldown_seconds=10):
        bot.answer_callback_query(call.id, "Please wait a few seconds before sending another note.")
        return

    pending_notes[user_id] = {"stage": "await_input_type"}
    bot.answer_callback_query(call.id)
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("📝 Use a Note", callback_data="input_note"),
        InlineKeyboardButton("🏷️ Title Only", callback_data="input_title"),
        InlineKeyboardButton("📄 File (PDF/DOCX/TXT) [Premium]", callback_data="input_file"),
        InlineKeyboardButton("🔙 Home", callback_data="home"),
    )
    tip = ""
    user = users_repo.get(user_id) or {}
    if not user.get("gemini_api_key"):
        tip = "\n\nTip: Add your own Gemini API key to lift the 2/day limit. Use Settings → Set/Change Gemini API Key."
    bot.send_message(user_id, "Choose input type:" + tip, reply_markup=kb)


@bot.message_handler(func=lambda m: m.from_user and m.from_user.id in pending_notes and pending_notes[m.from_user.id].get("stage") == "await_note")
def handle_note_submission(message: Message):
    user_id = message.from_user.id
    note = message.text or ""
    pending_notes[user_id]["note"] = note

    # Destination choices: PM or one of user's channels
    user_channels = channels_repo.list_channels(user_id)
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🔁 Allow Beyond Note", callback_data="toggle_beyond_yes"))
    kb.add(InlineKeyboardButton("📥 Send to PM", callback_data="dst_pm"))
    for ch in user_channels:
        label = f"{ch.get('title','Channel')} ({ch.get('username') or ch.get('chat_id')})"
        kb.add(InlineKeyboardButton(f"📣 {label}", callback_data=f"dst_ch_{ch['chat_id']}"))
    kb.add(InlineKeyboardButton("🔙 Home", callback_data="home"))

    pending_notes[user_id]["stage"] = "choose_destination"
    bot.send_message(user_id, "Choose where to send the quiz:", reply_markup=kb)


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
        bot.send_message(user_id, "Send a delay in seconds (5-60):")
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
    target = state.get("target_chat_id", user_id)
    delay = int(state.get("delay_seconds", 5))

    user = users_repo.get(user_id) or {}
    num_questions = int(user.get("questions_per_note", 5))
    q_format = (user.get("default_question_type") or cfg.question_type_default).lower()

    if not has_quota(db, user_id):
        bot.answer_callback_query(call.id, "Daily quota reached")
        return

    if not can_submit_note_now(db, user_id, cooldown_seconds=10):
        bot.answer_callback_query(call.id, "Wait a few seconds before next note")
        return

    update_last_note_time(db, user_id)
    bot.answer_callback_query(call.id)

    generating = bot.send_message(user_id, "Generating...")
    try:
        allow_beyond = bool(state.get("allow_beyond"))
        if file_content:
            # Chunking to avoid limits; distribute questions across chunks up to requested number
            chunks = chunk_text(file_content, max_chars=3500)
            per_chunk = max(1, num_questions // max(1, len(chunks)))
            questions = []
            for idx, ch in enumerate(chunks):
                if len(questions) >= num_questions:
                    break
                qbatch = generate_questions(ch, per_chunk, user_id=user_id, title_only=False, allow_beyond=True)
                questions.extend(qbatch)
            questions = questions[:num_questions]
        elif title:
            warn = "⚠️ Title-only mode: AI may include info beyond your intended scope."
            bot.send_message(user_id, warn)
            questions = generate_questions("", num_questions, user_id=user_id, title_only=True, allow_beyond=True, topic_title=title)
        else:
            questions = generate_questions(note, num_questions, user_id=user_id, title_only=False, allow_beyond=allow_beyond)
        if not questions:
            bot.send_message(user_id, "An error occurred while generating questions. Please try again.")
            return
        bot.delete_message(user_id, generating.id)
        letters = ["A", "B", "C", "D"]
        for idx, q in enumerate(questions, start=1):
            time.sleep(delay)
            if q_format == "text":
                text = f"{idx}. {q['question']}\n"
                for i, c in enumerate(q["choices"]):
                    prefix = letters[i] if i < len(letters) else str(i + 1)
                    text += f"{prefix}. {c}\n"
                text += f"\n<b>Correct Answer</b>: {letters[q['answer_index']]} - {q['choices'][q['answer_index']]}"
                explanation = (q.get("explanation") or "")
                if explanation:
                    text += f"\n<b>Explanation:</b> {explanation[:195]}"
                bot.send_message(target, text, parse_mode="HTML")
            else:
                bot.send_poll(
                    target,
                    q["question"],
                    q["choices"],
                    type="quiz",
                    correct_option_id=q["answer_index"],
                    explanation=(q.get("explanation") or "")[:195],
                )
        increment_quota(db, user_id)
        increase_total_notes(db, user_id)
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
    now = datetime.utcnow()
    bot.send_message(user_id, f"Send schedule time in format YYYY-MM-DD HH:MM (UTC+3). Example: 2025-01-01 12:30\nNow (UTC+3): {format_dt_utc3(now)}")


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
            "scheduled_at": scheduled_utc,
            "status": "pending",
            "created_at": datetime.utcnow(),
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
    buttons = [InlineKeyboardButton(str(i), callback_data=f"set_qpernote_{i}") for i in range(1, 11)]
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
    max_limit = 10
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
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
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


@bot.message_handler(commands=["addcbe"]) 
def admin_add_cbe(message: Message):
    if not users_repo:
        bot.reply_to(message, "DB unavailable.")
        return
    admin = users_repo.get(message.from_user.id)
    if not admin or admin.get("role") != "admin":
        bot.reply_to(message, "Not authorized.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /addcbe 1000123456")
        return
    current = SettingsRepository(db).get("cbe_numbers", [])
    if parts[1] not in current:
        current.append(parts[1])
    SettingsRepository(db).set("cbe_numbers", current)
    bot.reply_to(message, f"cbe_numbers: {', '.join(current)}")


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
    req = users_repo.get(message.from_user.id)
    if not req or req.get("role") != "admin":
        bot.reply_to(message, "Not authorized.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /addadmin <user_id>")
        return
    target_id = int(parts[1])
    users_repo.set_role(target_id, "admin")
    bot.reply_to(message, f"User {target_id} promoted to admin.")


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