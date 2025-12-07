import logging
import os
from logging.handlers import RotatingFileHandler

def setup_logger(name: str = "bot", log_file: str = "bot.log", level: int = logging.INFO) -> logging.Logger:
    """
    Sets up a logger that writes to a file and the console.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Create formatters
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # File Handler (Rotating)
    file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=5)
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    # Console Handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    # Add handlers if not already added
    if not logger.handlers:
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

    return logger

# Default logger instance
logger = setup_logger()
