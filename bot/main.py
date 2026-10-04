import asyncio
import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from bot.config import load_settings
from bot.db import Database
from bot.handlers.start import router as start_router
from bot.handlers.admin import router as admin_router



log_dir = "/app/data"
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "bot.log")


root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)


root_logger.handlers.clear()


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
console_formatter = logging.Formatter(
    '%(levelname)s:%(name)s:%(message)s'
)
console_handler.setFormatter(console_formatter)


root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)


logger = logging.getLogger(__name__)
logger.info(f"Логирование настроено. Файл логов: {log_file}")


@asynccontextmanager
async def lifespan(dp: Dispatcher, db: Database):
    await db.init()
    yield


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="🏠 Главное меню"),
            BotCommand(command="profile", description="👤 Мой профиль"),
            BotCommand(command="settings", description="⚙️ Настройки"),
            BotCommand(command="reset", description="🔄 Сброс"),
            BotCommand(command="help", description="❓ Инструкция"),
        ]
    )


from aiogram.enums import ParseMode, ChatType
from aiogram.types import Message, CallbackQuery
from bot.handlers.start import _ensure_access
import logging

class AccessMiddleware:
    async def __call__(self, handler, event, data):
        
        actual_event = event.message or event.callback_query
        if not actual_event:
            return await handler(event, data)

        
        chat = actual_event.chat if event.message else actual_event.message.chat
        if chat.type != ChatType.PRIVATE:
            return await handler(event, data)
        
        user_id = actual_event.from_user.id
        is_callback = bool(event.callback_query)
        
        
        if not is_callback and actual_event.text and actual_event.text.startswith("/start"):
            
            
            return await handler(event, data)
            
        exceptions = ["accept_terms", "check_subscription", "menu_agreement"]
        if is_callback and event.callback_query.data in exceptions:
            return await handler(event, data)
            
        
        db = data.get("db")
        if await db.get_user_blocked(user_id):
            lang = await db.get_user_language(user_id)
            from bot.strings import get_string
            text = get_string("user_blocked", lang)
            if is_callback:
                await event.callback_query.answer(text, show_alert=True)
            else:
                await event.message.answer(text)
            return

        
        settings = data.get("settings")
        if settings and user_id in (settings.admin_ids or []):
            return await handler(event, data)
            
        
        bot = data.get("bot")
        
        
        if await _ensure_access(actual_event, db, bot):
            return await handler(event, data)
            
        return

async def main() -> None:
    settings = load_settings()

    # Жестко определяем корень проекта (D:\ai_fotoroom)
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    BASE_DIR = "/app"
    db_path = "/app/data/bot.db"

    print(f"Бот подключается к базе по пути: {db_path}")  # Для отладки в консоли

    db = Database(db_path=db_path)
    await db.init()

    from aiogram.client.session.aiohttp import AiohttpSession

    session = AiohttpSession(timeout=300)

    bot = Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher(storage=MemoryStorage())
    
    
    dp.update.outer_middleware(AccessMiddleware())

    dp.include_router(admin_router)
    dp.include_router(start_router)

    async with lifespan(dp, db):
        dp['db'] = db  
        dp['settings'] = settings
        await set_commands(bot)
        await dp.start_polling(bot, db=db, settings=settings)


if __name__ == "__main__":
    asyncio.run(main())


