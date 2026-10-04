import asyncio
import logging
import os

from vkbottle import Bot

from bot.config import load_settings
from bot.db import Database
from vk_bot.context import set_context


log_dir = "/app/data"
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "vk_bot.log")

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers.clear()

from logging.handlers import RotatingFileHandler
file_handler = RotatingFileHandler(
    log_file,
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding='utf-8'
)
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
file_handler.setFormatter(file_formatter)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter('%(levelname)s:%(name)s:%(message)s')
console_handler.setFormatter(console_formatter)

root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)
logger.info(f"VK Bot logging configured. Log file: {log_file}")


def main() -> None:
    settings = load_settings(require_bot_token=False)
    
    if not settings.vk_bot_token:
        logger.error("VK_BOT_TOKEN is required in .env")
        return
    
    
    db_url = settings.database_url
    if "sqlite+aiosqlite:///" in db_url:
        db_path = db_url.replace("sqlite+aiosqlite:///", "")
    else:
        db_path = "data/bot.db"
    
    if not os.path.isabs(db_path) and not db_path.startswith("/app"):
        db_path = os.path.join(os.getcwd(), db_path)
    
    db = Database(db_path=db_path)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(db.init())
    
    
    bot = Bot(token=settings.vk_bot_token)
    
    
    from vk_bot.handlers.start import router as start_router
    from vk_bot.handlers.admin import router as admin_router
    
    bot.labeler.load(admin_router)
    bot.labeler.load(start_router)
    set_context(db, settings)
    
    logger.info("VK Bot started")
    bot.run_forever()


if __name__ == "__main__":
    main()
