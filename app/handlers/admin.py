from telebot import TeleBot
from ..repositories.users import UsersRepository
from ..repositories.settings import SettingsRepository
from ..config import get_config
from ..db import get_db

def register_admin_handlers(bot: TeleBot):
    db = get_db()
    cfg = get_config()
    users_repo = UsersRepository(db)
    settings_repo = SettingsRepository(db)

    # Move admin-related handlers here...
    pass
