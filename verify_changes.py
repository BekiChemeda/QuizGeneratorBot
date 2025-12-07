import sys
import os

# Add the current directory to sys.path
sys.path.append(os.getcwd())

try:
    print("Mocking TeleBot...")
    import telebot
    from unittest.mock import MagicMock
    telebot.TeleBot = MagicMock()

    print("Attempting to import app.bot...")
    from app import bot
    print("Successfully imported app.bot")
    
    print("Checking for error_handler...")
    if hasattr(bot, 'error_handler'):
        print("error_handler is present in app.bot")
    else:
        print("error_handler is MISSING in app.bot")
        
    print("Checking for logger...")
    from app.logger import logger
    print("Successfully imported app.logger")
    
    print("Checking for notify_admins...")
    from app.utils import notify_admins
    print("Successfully imported app.utils.notify_admins")

    print("Verification successful!")

except ImportError as e:
    print(f"ImportError: {e}")
    sys.exit(1)
except Exception as e:
    print(f"An error occurred: {e}")
    sys.exit(1)
