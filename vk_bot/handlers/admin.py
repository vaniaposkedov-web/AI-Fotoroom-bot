import os

from vkbottle.bot import BotLabeler, Message
from vkbottle.dispatch.rules import ABCRule

from vk_bot.context import get_db, get_settings

router = BotLabeler()


def _parse_admin_ids(raw: str | None) -> set[int]:
    result: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            continue
    return result


class IsAdmin(ABCRule):
    async def check(self, message: Message) -> bool:
        settings = get_settings()
        vk_admin_ids = _parse_admin_ids(os.getenv("VK_ADMIN_IDS"))
        return message.from_id in vk_admin_ids or message.from_id in (settings.admin_ids or [])


class IsAdminCommand(ABCRule):
    async def check(self, message: Message) -> bool:
        text = (message.text or "").strip().lower()
        return text in {"/admin", "admin"}


@router.message(IsAdmin(), IsAdminCommand())
async def handle_admin(message: Message):
    db = get_db()
    users_count = 0
    try:
        users_count = len(await db.list_all_user_ids())
    except Exception:
        pass
    await message.answer(
        "VK Admin\n"
        f"Пользователей в базе: {users_count}\n"
        "Базовые админ-команды будут расширены далее."
    )
