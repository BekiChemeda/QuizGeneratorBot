from telebot import TeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from typing import List
from datetime import datetime, timedelta, timezone
from .config import get_config
from .services.settings_service import SettingsService
from .db import get_db


def is_admin(user_doc: dict) -> bool:
    return (user_doc or {}).get("role") == "admin"


def is_subscribed(bot: TeleBot, user_id: int) -> bool:
    # Prefer DB settings; fallback to env
    db = get_db()
    ss = SettingsService(db)
    force = ss.get_bool("force_subscription", default=get_config().force_subscription)
    if not force:
        return True
    channels = ss.get_list_str("force_channels", default=get_config().force_channels)
    for channel in channels:
        try:
            status = bot.get_chat_member(channel, user_id).status
            if status not in ["member", "administrator", "creator"]:
                return False
        except Exception:
            return False
    return True


def home_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔙home", callback_data="home"))
    return kb


# Time utilities: enforce UTC+3 for display and scheduling inputs
UTC = timezone.utc
UTC_PLUS_3 = timezone(timedelta(hours=3))


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def to_utc3(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC_PLUS_3)


def from_utc3_to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC_PLUS_3)
    return dt.astimezone(UTC)


def format_dt_utc3(dt: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return to_utc3(dt).strftime(fmt)