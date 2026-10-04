from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from io import BytesIO
import asyncio
from typing import Optional
import subprocess
import logging
import requests
import os

from bot.config import Settings, load_settings
from bot.db import Database

logger = logging.getLogger(__name__)
from bot.keyboards import (
    admin_main_keyboard,
    admin_categories_keyboard,
    admin_users_keyboard,
    admin_user_actions_keyboard,
    admin_user_history_keyboard,
    admin_models_keyboard,
    admin_models_category_keyboard,
    admin_models_cloth_keyboard,
    admin_models_actions_keyboard,
    admin_model_list_keyboard,
    admin_model_edit_keyboard,
    admin_prompt_pick_keyboard,
    admin_model_created_keyboard,
    main_menu_keyboard,
    broadcast_skip_keyboard,
    broadcast_confirm_keyboard,
    admin_own_prompts_keyboard,
    admin_own_variant_prompts_keyboard,
    admin_category_prices_keyboard,
)


from bot.strings import get_string


router = Router()


def _is_admin(user_id: int, settings: Settings) -> bool:
    return user_id in (settings.admin_ids or [])


async def _safe_answer(callback: CallbackQuery, text: str | None = None, show_alert: bool = False) -> None:
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest:
        pass
    except Exception:
        pass


async def _replace_with_text(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    try:
        if getattr(callback.message, "photo", None):
            await callback.message.delete()
            await callback.message.answer(text, reply_markup=reply_markup)
        else:
            await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        try:
            await callback.message.answer(text, reply_markup=reply_markup)
        except TelegramBadRequest:
            pass


@router.message(Command("admin"))
async def admin_entry(message: Message, settings: Settings, db: Database) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    maint = await db.get_maintenance()
    status = get_string("admin_maint_enabled", lang) if maint else get_string("admin_maint_disabled", lang)
    await message.answer(get_string("admin_panel_title", lang, status=status), reply_markup=admin_main_keyboard(lang))


from aiogram.fsm.storage.base import StorageKey

@router.message(Command("reset_user"))
async def cmd_reset_user(message: Message, db: Database, settings: Settings, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(get_string("admin_reset_usage", lang))
        return
    try:
        user_id = int(parts[1])
    except Exception:
        await message.answer(get_string("admin_reset_invalid", lang))
        return
    
    try:
        
        key = StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id)
        target_state = FSMContext(
            storage=state.storage,
            key=key
        )
        await target_state.clear()
        
        
        await db.execute("UPDATE users SET trial_used=0, balance=0 WHERE id=?", (user_id,))
        await db.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
        await db.commit()
        
        await message.answer(get_string("admin_reset_success", lang, user_id=user_id))
    except Exception as e:
        logger.error(f"Ошибка при сбросе состояния пользователя {user_id}: {e}")
        await message.answer(get_string("admin_reset_error", lang, e=str(e)))


@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    stats = await db.get_stats()
    text = (
        get_string("admin_stats_title", lang) + "\n\n"
        f"{get_string('admin_stats_total_users', lang)}: {stats['total_users']}\n"
        f"{get_string('admin_stats_today_users', lang)}: {stats['today_users']}\n"
        f"{get_string('admin_stats_total_generations', lang)}: {stats.get('total_generations', 0)}\n"
        f"{get_string('admin_stats_today_generations', lang)}: {stats.get('today_generations', 0)}\n"
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_main_keyboard(lang))
    except TelegramBadRequest:
        pass
    await _safe_answer(callback)


@router.callback_query(F.data == "admin_main")
async def admin_main_back(callback: CallbackQuery, settings: Settings, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    maint = await db.get_maintenance()
    status = get_string("admin_maint_enabled", lang) if maint else get_string("admin_maint_disabled", lang)
    await callback.message.edit_text(get_string("admin_panel_title", lang, status=status), reply_markup=admin_main_keyboard(lang))
    await _safe_answer(callback)

@router.callback_query(F.data == "admin_categories")
async def admin_categories(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    status = await db.list_categories_enabled()
    await _replace_with_text(callback, get_string("admin_cats_edit", lang), reply_markup=admin_categories_keyboard(status, lang))
    await _safe_answer(callback)

@router.callback_query(F.data.startswith("admin_toggle_cat:"))
async def admin_toggle_category(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    name = callback.data.split(":", 1)[1]
    current = await db.get_category_enabled(name)
    await db.set_category_enabled(name, not current)
    status = await db.list_categories_enabled()
    await _replace_with_text(callback, get_string("admin_cats_edit", lang), reply_markup=admin_categories_keyboard(status, lang))
    await _safe_answer(callback, get_string("admin_saved", lang), show_alert=False)



@router.callback_query(F.data == "admin_category_prices")
async def admin_category_prices(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    prices = await db.list_category_prices()
    await _replace_with_text(callback, get_string("admin_prices_title", lang), reply_markup=admin_category_prices_keyboard(prices, lang))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin_price_edit:"))
async def admin_price_edit_start(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    category = callback.data.split(":", 1)[1]
    current_price = await db.get_category_price(category)
    category_names = {
        "female": get_string("cat_female", lang),
        "male": get_string("cat_male", lang),
        "child": get_string("cat_child", lang),
        "storefront": get_string("cat_storefront", lang),
        "whitebg": get_string("cat_whitebg", lang),
        "random": get_string("cat_random", lang),
        "random_other": get_string("cat_random_other", lang),
        "own": get_string("cat_own", lang),
        "own_variant": get_string("cat_own_variant", lang),
    }
    cat_name = category_names.get(category, category)
    is_enabled = await db.get_category_enabled(category)
    status_text = get_string("admin_maint_enabled", lang) if is_enabled else get_string("admin_maint_disabled", lang)
    await _replace_with_text(callback, get_string("admin_prices_edit_notice", lang, name=cat_name, status=status_text))


class UserSearch(StatesGroup):
    waiting_id = State()

@router.callback_query(F.data == "admin_user_search")
async def admin_user_search_start(callback: CallbackQuery, settings: Settings, state: FSMContext, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    await state.set_state(UserSearch.waiting_id)
    await _replace_with_text(callback, get_string("admin_enter_id", lang))
    await _safe_answer(callback)

@router.message(UserSearch.waiting_id)
async def admin_user_search_finish(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    txt = (message.text or "").strip()
    try:
        uid = int(txt)
    except Exception:
        await message.answer(get_string("admin_enter_id_error", lang))
        return
    blocked = await db.get_user_blocked(uid)
    state_txt = get_string("admin_block", lang) if blocked else get_string("admin_unblock", lang)
    text = get_string("admin_user_title", lang, uid=uid) + f"\n{get_string('admin_user_status', lang, status=state_txt)}"
    await state.clear()
    await message.answer(text, reply_markup=admin_user_actions_keyboard(uid, lang, blocked=blocked))


PAGE_SIZE = 10


@router.callback_query(F.data.startswith("admin_users_page:"))
async def admin_users_page(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    try:
        page = int(callback.data.split(":", 1)[1])
    except Exception:
        page = 0
    offset = page * PAGE_SIZE
    users = await db.list_users_page(offset=offset, limit=PAGE_SIZE + 1)
    has_next = len(users) > PAGE_SIZE
    users_page = users[:PAGE_SIZE]
    text = get_string("admin_users_list", lang)
    try:
        await callback.message.edit_text(text, reply_markup=admin_users_keyboard(users_page, page, has_next, lang))
    except TelegramBadRequest:
        pass
    await _safe_answer(callback)





@router.callback_query(F.data == "admin_models")
async def admin_models_root(callback: CallbackQuery, settings: Settings, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    await callback.message.edit_text(get_string("admin_models_title", lang), reply_markup=admin_models_keyboard(lang))
    await _safe_answer(callback)
@router.callback_query(F.data == "admin_maint_on")
async def admin_maintenance_on(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    await db.set_maintenance(True)
    status = get_string("admin_maint_enabled", lang)
    try:
        await callback.message.edit_text(get_string("admin_panel_title", lang, status=status), reply_markup=admin_main_keyboard(lang))
    except TelegramBadRequest:
        pass
    await _safe_answer(callback, get_string("admin_maint_on_success", lang), show_alert=False)


@router.callback_query(F.data == "admin_maint_off")
async def admin_maintenance_off(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    await db.set_maintenance(False)
    status = get_string("admin_maint_disabled", lang)
    try:
        await callback.message.edit_text(get_string("admin_panel_title", lang, status=status), reply_markup=admin_main_keyboard(lang))
    except TelegramBadRequest:
        pass
    await _safe_answer(callback, get_string("admin_maint_off_success", lang), show_alert=False)


def _get_docker_logs(lines: int = 100) -> str:
    
    
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(lines), "fashion-bot"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            return result.stdout or "Логи пусты"
        else:
            
            pass
    except FileNotFoundError:
        
        pass
    except subprocess.TimeoutExpired:
        return "⏱ Таймаут при получении логов через docker."
    except Exception:
        pass
    
    
    try:
        result = subprocess.run(
            ["docker", "compose", "logs", "--tail", str(lines), "bot"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd="/app"  
        )
        if result.returncode == 0:
            return result.stdout or "Логи пусты"
    except Exception:
        pass
    
    
    log_files = [
        "/app/data/bot.log",
        "/app/bot.log",
        "/var/log/bot.log",
        "/tmp/bot.log",
    ]
    
    checked_files = []
    for log_file in log_files:
        try:
            checked_files.append(f"{log_file} ({'существует' if os.path.exists(log_file) else 'не найден'})")
            if os.path.exists(log_file):
                
                file_size = os.path.getsize(log_file)
                if file_size == 0:
                    continue
                    
                with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                    all_lines = f.readlines()
                    if all_lines:
                        
                        selected_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                        log_content = "".join(selected_lines)
                        if log_content.strip():
                            return f"📋 Логи из файла `{log_file}` (последние {len(selected_lines)} строк из {len(all_lines)}):\n\n```\n{log_content}```"
        except PermissionError:
            checked_files[-1] = f"{log_file} (нет доступа)"
            continue
        except Exception as e:
            checked_files[-1] = f"{log_file} (ошибка: {str(e)[:50]})"
            continue
    
    
    debug_info = "\n".join([f"• {f}" for f in checked_files])
    return (
        "⚠️ Не удалось получить логи автоматически.\n\n"
        f"**Проверенные файлы:**\n{debug_info}\n\n"
        "**Возможные причины:**\n"
        "• Файл логов еще не создан (бот только что запущен)\n"
        "• Файл пустой (логи еще не записаны)\n"
        "• Нет прав доступа к файлу\n\n"
        "**Для просмотра логов используйте на сервере:**\n"
        "```bash\n"
        "docker compose logs -f bot --tail=200\n"
        "```\n\n"
        "Или для последних 100 строк:\n"
        "```bash\n"
        "docker compose logs bot --tail=100\n"
        "```\n\n"
        "**Примечание:** После перезапуска бота логи начнут записываться в `/app/data/bot.log`"
    )


@router.callback_query(F.data == "admin_logs")
async def admin_logs(callback: CallbackQuery, settings: Settings, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    await _safe_answer(callback, get_string("admin_logs_getting", lang), show_alert=False)
    
    logs = await asyncio.to_thread(_get_docker_logs, 200)
    
    
    def escape_html(text: str) -> str:
        
        return (text
                .replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;'))
    
    
    max_length = 3500  
    if len(logs) > max_length:
        logs_parts = []
        current_part = ""
        for line in logs.split('\n'):
            if len(current_part) + len(line) + 1 > max_length:
                if current_part:
                    logs_parts.append(current_part)
                current_part = line + '\n'
            else:
                current_part += line + '\n'
        if current_part:
            logs_parts.append(current_part)
        
        
        header = get_string("admin_logs_parts", lang, part=1, total=len(logs_parts))
        escaped_logs = escape_html(logs_parts[0][:max_length])
        try:
            await callback.message.answer(
                f"{header}\n\n<pre>{escaped_logs}</pre>",
                parse_mode="HTML"
            )
        except Exception as e:
            
            logger.warning(f"Ошибка отправки логов с HTML: {e}")
            await callback.message.answer(
                f"{header}\n\n{logs_parts[0][:max_length]}"
            )
        
        
        for i, part in enumerate(logs_parts[1:], 2):
            escaped_part = escape_html(part[:max_length])
            header_part = get_string("admin_logs_parts", lang, part=i, total=len(logs_parts))
            try:
                await callback.message.answer(
                    f"{header_part}\n\n<pre>{escaped_part}</pre>",
                    parse_mode="HTML"
                )
            except Exception:
                await callback.message.answer(
                    f"{header_part}\n\n{part[:max_length]}"
                )
    else:
        
        if logs.startswith("📋"):
            
            if "\n\n" in logs:
                header_part, content_part = logs.split("\n\n", 1)
                escaped_content = escape_html(content_part)
                try:
                    await callback.message.answer(
                        f"{header_part}\n\n<pre>{escaped_content}</pre>",
                        parse_mode="HTML"
                    )
                except Exception:
                    await callback.message.answer(logs)
            else:
                await callback.message.answer(logs)
        else:
            escaped_logs = escape_html(logs)
            header_logs = get_string("admin_logs_server", lang)
            try:
                await callback.message.answer(
                    f"{header_logs}\n\n<pre>{escaped_logs}</pre>",
                    parse_mode="HTML"
                )
            except Exception:
                await callback.message.answer(
                    f"{header_logs}\n\n{logs}"
                )
    
    await callback.message.answer(get_string("admin_title", lang), reply_markup=admin_main_keyboard(lang))


def _check_proxy_status(settings: Settings) -> dict:
    
    proxy_config = settings.proxy
    result = {
        "configured": False,
        "url": None,
        "status": "Не настроен",
        "test_url": "https://www.google.com",
        "test_result": None,
        "error": None
    }
    
    if not proxy_config.host or not proxy_config.port:
        return result
    
    result["configured"] = True
    result["url"] = proxy_config.as_url()
    
    
    proxies = {}
    if proxy_config.scheme:
        proxy_url = proxy_config.as_url()
        if proxy_config.scheme in ("http", "https"):
            proxies["http"] = proxy_url
            proxies["https"] = proxy_url
        elif proxy_config.scheme in ("socks5", "socks5h"):
            proxies["http"] = proxy_url
            proxies["https"] = proxy_url
    
    
    env_proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
    if env_proxy:
        result["env_proxy"] = env_proxy
    
    
    try:
        response = requests.get(
            result["test_url"],
            proxies=proxies if proxies else None,
            timeout=10
        )
        result["status"] = "✅ Работает"
        result["test_result"] = f"HTTP {response.status_code}"
    except requests.exceptions.ProxyError as e:
        result["status"] = "❌ Ошибка прокси"
        error_msg = str(e)
        
        if "Unable to connect to proxy" in error_msg:
            result["error"] = "Не удалось подключиться к прокси-серверу"
        elif "ReadTimeoutError" in error_msg:
            result["error"] = "Таймаут при подключении к прокси"
        elif "Connection refused" in error_msg:
            result["error"] = "Прокси-сервер отклонил подключение"
        else:
            result["error"] = error_msg[:150]
    except requests.exceptions.Timeout:
        result["status"] = "⏱ Таймаут"
        result["error"] = "Запрос превысил время ожидания (10 сек)"
    except requests.exceptions.ConnectionError as e:
        result["status"] = "❌ Ошибка подключения"
        error_msg = str(e)
        if "Max retries exceeded" in error_msg:
            result["error"] = "Превышено максимальное количество попыток подключения"
        else:
            result["error"] = error_msg[:150]
    except requests.exceptions.RequestException as e:
        result["status"] = "❌ Ошибка запроса"
        result["error"] = str(e)[:150]
    except Exception as e:
        result["status"] = "❌ Неизвестная ошибка"
        result["error"] = str(e)[:150]
    
    return result


@router.callback_query(F.data == "admin_proxy_status")
async def admin_proxy_status(callback: CallbackQuery, settings: Settings, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    await _safe_answer(callback, get_string("admin_proxy_checking", lang), show_alert=False)
    
    proxy_status = await asyncio.to_thread(_check_proxy_status, settings)
    
    status_text = get_string("admin_proxy_title", lang) + "\n\n"
    
    
    
    
    
    
    if proxy_status["configured"]:
        status_text += f"**Настроен:** ✅\n"
        
        if proxy_status['url']:
            
            url = proxy_status['url']
            if '@' in url:
                parts = url.split('@')
                if len(parts) == 2:
                    auth_part = parts[0]
                    if ':' in auth_part:
                        user_pass = auth_part.split('://', 1)[-1]
                        if ':' in user_pass:
                            user, _ = user_pass.split(':', 1)
                            scheme = url.split('://')[0]
                            masked_url = f"{scheme}://{user}:***@{parts[1]}"
                            status_text += f"**URL:** `{masked_url}`\n"
                        else:
                            status_text += f"**URL:** `{url}`\n"
                    else:
                        status_text += f"**URL:** `{url}`\n"
                else:
                    status_text += f"**URL:** `{url}`\n"
            else:
                status_text += f"**URL:** `{url}`\n"
        if proxy_status.get("env_proxy"):
            env_proxy = proxy_status['env_proxy']
            if '@' in env_proxy:
                parts = env_proxy.split('@')
                if len(parts) == 2 and ':' in parts[0]:
                    user_pass = parts[0].split('://', 1)[-1]
                    if ':' in user_pass:
                        user, _ = user_pass.split(':', 1)
                        scheme = env_proxy.split('://')[0]
                        masked_env = f"{scheme}://{user}:***@{parts[1]}"
                        status_text += f"**Переменная окружения:** `{masked_env}`\n"
                    else:
                        status_text += f"**Переменная окружения:** `{env_proxy}`\n"
                else:
                    status_text += f"**Переменная окружения:** `{env_proxy}`\n"
            else:
                status_text += f"**Переменная окружения:** `{env_proxy}`\n"
        status_text += f"\n**Статус:** {proxy_status['status']}\n"
        if proxy_status.get("test_result"):
            status_text += f"**Тестовый запрос:** {proxy_status['test_result']}\n"
        if proxy_status.get("error"):
            status_text += f"\n**Ошибка:**\n`{proxy_status['error']}`\n"
    else:
        status_text += "**Настроен:** ❌\n"
        status_text += "Прокси не настроен в конфигурации.\n"
    
    await callback.message.answer(status_text, parse_mode="Markdown")
    await callback.message.answer(get_string("admin_title", lang), reply_markup=admin_main_keyboard(lang))


@router.callback_query(F.data == "admin_models_browse")
async def admin_models_browse(callback: CallbackQuery, settings: Settings, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    await callback.message.edit_text(get_string("admin_pick_cat", lang), reply_markup=admin_models_category_keyboard(lang))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin_cat:"))
async def admin_models_pick_category(callback: CallbackQuery, settings: Settings, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    category = callback.data.split(":", 1)[1]
    await callback.message.edit_text(get_string("admin_pick_cloth", lang), reply_markup=admin_models_cloth_keyboard(category, lang))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin_cloth:"))
async def admin_models_pick_cloth(callback: CallbackQuery, settings: Settings, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    _, category, cloth = callback.data.split(":", 2)
    
    if category == "whitebg":
        await callback.message.edit_text(get_string("admin_whitebg_notice", lang))
    else:
        await callback.message.edit_text(get_string("admin_actions", lang), reply_markup=admin_models_actions_keyboard(category, cloth, lang))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin_model_list:"))
async def admin_model_list(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    _, category, cloth, page_str = callback.data.split(":", 3)
    page = int(page_str)
    limit = 8
    offset = page * limit
    models = await db.list_models_page(category, cloth, offset, limit + 1)
    has_next = len(models) > limit
    models = models[:limit]
    items: list[tuple[int, str, str]] = []
    for mid, name, prompt_id, _pos in models:
        title = await db.get_prompt_title(prompt_id)
        items.append((mid, name, title))
    await callback.message.edit_text(get_string("admin_models_list", lang), reply_markup=admin_model_list_keyboard(category, cloth, items, page, has_next, lang))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin_models_actions:"))
async def admin_models_actions(callback: CallbackQuery, settings: Settings, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    _, category, cloth = callback.data.split(":", 2)
    await callback.message.edit_text(get_string("admin_actions", lang), reply_markup=admin_models_actions_keyboard(category, cloth, lang))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin_model_edit:"))
async def admin_model_edit(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    model_id = int(callback.data.split(":", 1)[1])
    await callback.message.edit_text(get_string("admin_model_num", lang, mid=model_id), reply_markup=admin_model_edit_keyboard(model_id, lang))
    await _safe_answer(callback)


class ModelCreate(StatesGroup):
    waiting_photo = State()
    waiting_prompt = State()
    waiting_name = State()
    category: str
    cloth: str


@router.callback_query(F.data.startswith("admin_model_add:"))
async def admin_model_add(callback: CallbackQuery, settings: Settings, state: FSMContext, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    _, category, cloth = callback.data.split(":", 2)
    await state.clear()
    await state.update_data(category=category, cloth=cloth)
    await state.set_state(ModelCreate.waiting_photo)
    await callback.message.edit_text(get_string("admin_send_photo", lang))
    await _safe_answer(callback)


@router.message(ModelCreate.waiting_photo)
async def admin_model_add_photo(message: Message, state: FSMContext, settings: Settings, db: Database) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    if not message.photo:
        await message.answer(get_string("admin_need_photo", lang))
        return
    file_id = message.photo[-1].file_id
    await state.update_data(photo_file_id=file_id)
    await state.set_state(ModelCreate.waiting_prompt)
    await message.answer(get_string("admin_send_prompt", lang))


@router.message(ModelCreate.waiting_prompt)
async def admin_model_add_prompt_text(message: Message, state: FSMContext, settings: Settings, bot: Bot, db: Database) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    prompt_text: str | None = None
    
    if message.document:
        doc = message.document
        filename = (doc.file_name or "").lower()
        mime = (doc.mime_type or "").lower()
        if filename.endswith(".txt") or mime == "text/plain":
            buf = BytesIO()
            try:
                await bot.download(doc, destination=buf)
                content = buf.getvalue().decode("utf-8", errors="ignore").strip()
                if content:
                    prompt_text = content
            except Exception:
                pass
        if prompt_text is None:
            await message.answer(get_string("admin_prompt_empty", lang))
            return
    else:
        
        if not message.text or not message.text.strip():
            await message.answer(get_string("admin_prompt_empty", lang))
            return
        prompt_text = message.text.strip()
    await state.update_data(prompt_text=prompt_text)
    await state.set_state(ModelCreate.waiting_name)
    await message.answer(get_string("admin_send_name", lang))


@router.message(ModelCreate.waiting_name)
async def admin_model_add_finish(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    data = await state.get_data()
    category = data.get("category")
    cloth = data.get("cloth")
    name = message.text.strip() or "Model"
    prompt_title = name
    
    prompt_text = (data.get("prompt_text") or "").strip()
    if not prompt_text:
        await state.set_state(ModelCreate.waiting_prompt)
        await message.answer(get_string("admin_prompt_empty", lang))
        return
    prompt_id = await db.add_prompt(prompt_title, prompt_text)
    model_id = await db.add_model(category, cloth, name=name, prompt_id=prompt_id)
    if data.get("photo_file_id"):
        await db.set_model_photo(model_id, data["photo_file_id"])
    await state.clear()
    await message.answer(
        get_string("admin_model_created", lang, mid=model_id),
        reply_markup=admin_model_created_keyboard(category, cloth, lang),
    )





class PromptCreate(StatesGroup):
    waiting_title = State()
    waiting_text = State()


class ModelRename(StatesGroup):
    waiting_name = State()

class RebindState(StatesGroup):
    running = State()


class BroadcastState(StatesGroup):
    waiting_message = State()
    waiting_media = State()
    confirming = State()


class OwnPromptState(StatesGroup):
    waiting_text = State()
    key = State()

@router.callback_query(F.data == "admin_own_prompts")
async def admin_own_prompts_root(callback: CallbackQuery, settings: Settings, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    await _replace_with_text(callback, get_string("admin_own_prompts_title", lang), reply_markup=admin_own_prompts_keyboard(lang))
    await _safe_answer(callback)

@router.callback_query(F.data.startswith("admin_own_prompt_edit:"))
async def admin_own_prompt_edit_start(callback: CallbackQuery, state: FSMContext, settings: Settings, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    _, idx = callback.data.split(":", 1)
    idx = idx.strip()
    if idx not in ("1", "2", "3"):
        await _safe_answer(callback)
        return
    await state.set_state(OwnPromptState.waiting_text)
    await state.update_data(own_prompt_idx=idx)
    names = {
        "1": get_string("admin_own_prompt_step1", lang),
        "2": get_string("admin_own_prompt_step2", lang),
        "3": get_string("admin_own_prompt_step3", lang)
    }
    await _replace_with_text(callback, get_string("admin_own_prompt_edit_req", lang, name=names[idx]))
    await _safe_answer(callback)

@router.message(OwnPromptState.waiting_text)
async def admin_own_prompt_edit_save(message: Message, state: FSMContext, settings: Settings, db: Database, bot: Bot) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    data = await state.get_data()
    price_category = data.get("price_category")
    
    
    if price_category:
        try:
            price_text = (message.text or "").strip()
            if not price_text:
                await message.answer(get_string("admin_price_empty", lang))
                return
            
            price_float = float(price_text)
            if price_float <= 0:
                await message.answer(get_string("admin_price_invalid", lang))
                return
            
            price_tenths = int(price_float * 10)
            await db.set_category_price(price_category, price_tenths)
            await state.clear()
            prices = await db.list_category_prices()
            await message.answer(get_string("admin_saved", lang), reply_markup=admin_category_prices_keyboard(prices, lang))
            return
        except ValueError:
            await message.answer(get_string("admin_price_format_error", lang))
            return
    
    
    idx = (data.get("own_prompt_idx") or "").strip()
    prompt_type = data.get("prompt_type")
    new_text: str | None = None
    if message.document:
        doc = message.document
        filename = (doc.file_name or "").lower()
        mime = (doc.mime_type or "").lower()
        if filename.endswith(".txt") or mime == "text/plain":
            from io import BytesIO
            buf = BytesIO()
            try:
                await bot.download(doc, destination=buf)
                content = buf.getvalue().decode("utf-8", errors="ignore").strip()
                if content:
                    new_text = content
            except Exception:
                pass
        if new_text is None:
            await message.answer(get_string("admin_txt_req", lang))
            return
    else:
        if message.text and message.text.strip():
            new_text = message.text.strip()
        else:
            await message.answer(get_string("admin_prompt_empty", lang))
            return
    if prompt_type == "own_variant":
        await db.set_own_variant_prompt(new_text)
        await state.clear()
        await message.answer(get_string("admin_saved", lang), reply_markup=admin_own_variant_prompts_keyboard(lang))
    elif idx == "1":
        await db.set_own_prompt1(new_text)
        await state.clear()
        await message.answer(get_string("admin_saved", lang), reply_markup=admin_own_prompts_keyboard(lang))
    elif idx == "2":
        await db.set_own_prompt2(new_text)
        await state.clear()
        await message.answer(get_string("admin_saved", lang), reply_markup=admin_own_prompts_keyboard(lang))
    else:
        await db.set_own_prompt3(new_text)
        await state.clear()
        await message.answer(get_string("admin_saved", lang), reply_markup=admin_own_prompts_keyboard(lang))


class ModelSetPhoto(StatesGroup):
    waiting_photo = State()


@router.callback_query(F.data == "admin_prompt_add")
async def on_prompt_add(callback: CallbackQuery, settings: Settings, state: FSMContext, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    await state.clear()
    await state.set_state(PromptCreate.waiting_title)
    await callback.message.edit_text(get_string("admin_enter_prompt_title", lang))
    await _safe_answer(callback)


@router.message(PromptCreate.waiting_title)
async def on_prompt_title(message: Message, state: FSMContext, settings: Settings, db: Database) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    await state.update_data(title=message.text.strip())
    await state.set_state(PromptCreate.waiting_text)
    await message.answer(get_string("admin_enter_prompt_text", lang))


@router.message(PromptCreate.waiting_text)
async def on_prompt_text(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    data = await state.get_data()
    title = str(data.get("title", "Prompt"))
    text = message.text
    pid = await db.add_prompt(title, text)
    await state.clear()
    await message.answer(get_string("admin_prompt_created", lang, pid=pid))


@router.callback_query(F.data.startswith("admin_model_rename:"))
async def admin_model_rename_start(callback: CallbackQuery, settings: Settings, state: FSMContext, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer()
        return
    lang = await db.get_user_language(callback.from_user.id)
    model_id = int(callback.data.split(":", 1)[1])
    await state.set_state(ModelRename.waiting_name)
    await state.update_data(model_id=model_id)
    await callback.message.edit_text(get_string("admin_enter_new_name", lang))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin_model_setphoto:"))
async def admin_model_setphoto_start(callback: CallbackQuery, settings: Settings, state: FSMContext, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    model_id = int(callback.data.split(":", 1)[1])
    await state.set_state(ModelSetPhoto.waiting_photo)
    await state.update_data(model_id=model_id)
    await _replace_with_text(callback, get_string("admin_send_new_photo", lang))
    await _safe_answer(callback)


@router.message(ModelSetPhoto.waiting_photo)
async def admin_model_setphoto_finish(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    if not message.photo:
        await message.answer(get_string("admin_need_photo", lang))
        return
    data = await state.get_data()
    model_id = int(data.get("model_id"))
    file_id = message.photo[-1].file_id
    await db.set_model_photo(model_id, file_id)
    await state.clear()
    await message.answer(get_string("admin_photo_updated", lang, mid=model_id))


@router.message(ModelRename.waiting_name)
async def admin_model_rename_finish(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    data = await state.get_data()
    model_id = int(data.get("model_id"))
    name = message.text.strip()[:100]
    await db.rename_model(model_id, name)
    await state.clear()
    await message.answer(get_string("admin_name_updated", lang))


@router.callback_query(F.data.startswith("admin_model_prompt:"))
async def admin_model_prompt(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    _, model_id_str, page_str = callback.data.split(":", 2)
    model_id = int(model_id_str)
    page = int(page_str)
    limit = 8
    offset = page * limit
    prompts = await db.list_prompts_page(offset, limit + 1)
    has_next = len(prompts) > limit
    prompts = prompts[:limit]
    await callback.message.edit_text(get_string("admin_pick_prompt", lang), reply_markup=admin_prompt_pick_keyboard(model_id, prompts, page, has_next, lang))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin_model_setprompt:"))
async def admin_model_setprompt(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    _, model_id_str, prompt_id_str = callback.data.split(":", 2)
    await db.set_model_prompt(int(model_id_str), int(prompt_id_str))
    try:
        await callback.message.edit_text(get_string("admin_model_num", lang, mid=int(model_id_str)), reply_markup=admin_model_edit_keyboard(int(model_id_str), lang))
    except TelegramBadRequest:
        pass
    await _safe_answer(callback, get_string("admin_saved", lang), show_alert=False)


@router.callback_query(F.data.startswith("admin_model_delete:"))
async def admin_model_delete(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    model_id = int(callback.data.split(":", 1)[1])
    await db.delete_model(model_id)
    try:
        await callback.message.edit_text(get_string("admin_deleted_return", lang), reply_markup=admin_models_keyboard(lang))
    except TelegramBadRequest:
        pass
    await _safe_answer(callback, get_string("admin_deleted", lang), show_alert=False)


@router.message(Command("rebind_photos"))
async def cmd_rebind_photos(message: Message, db: Database, settings: Settings, bot: Bot, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    if not settings.old_bot_token:
        await message.answer(get_string("admin_rebind_no_token", lang))
        return
    await message.answer(get_string("admin_rebind_start", lang))

    
    from aiogram import Bot as AioBot
    old_bot = AioBot(token=settings.old_bot_token)
    updated = 0
    failed = 0
    models = await db.list_all_models_with_photo()
    for model_id, file_id in models:
        if not file_id:
            continue
        try:
            
            try:
                await bot.get_file(file_id)
                continue
            except Exception:
                pass

            
            f = await old_bot.get_file(file_id)
            b = await old_bot.download_file(f.file_path)
            content = b.read()
            from aiogram.types import BufferedInputFile
            new_photo = BufferedInputFile(content, filename=f"model_{model_id}.jpg")
            sent = await bot.send_photo(chat_id=message.chat.id, photo=new_photo)
            new_id = sent.photo[-1].file_id
            await db.set_model_photo(model_id, new_id)
            updated += 1
            await asyncio.sleep(0.1)
        except Exception:
            failed += 1
            continue
    await old_bot.session.close()
    await message.answer(get_string("admin_rebind_done", lang, updated=updated, failed=failed))


@router.message(Command("all"))
async def cmd_broadcast(message: Message, db: Database, settings: Settings, bot: Bot, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    
    if message.reply_to_message:
        await _broadcast_forward(bot, db, message.reply_to_message)
        await message.answer(get_string("admin_broadcast_done", lang))
        return
    
    await state.set_state(BroadcastState.waiting_message)
    preset = ""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        preset = parts[1].strip()
        await state.update_data(bc_text=preset)
        await state.set_state(BroadcastState.waiting_media)
        await message.answer(get_string("admin_broadcast_text_ok", lang), reply_markup=broadcast_skip_keyboard(lang))
    else:
        await message.answer(get_string("admin_broadcast_text_req", lang))


@router.message(BroadcastState.waiting_message)
async def cmd_broadcast_text(message: Message, db: Database, settings: Settings, bot: Bot, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    text = (message.text or "").strip()
    if not text:
        await message.answer(get_string("admin_prompt_empty", lang))
        return
    await state.update_data(bc_text=text)
    await state.set_state(BroadcastState.waiting_media)
    await message.answer(get_string("admin_broadcast_text_ok", lang), reply_markup=broadcast_skip_keyboard(lang))

@router.message(BroadcastState.waiting_media)
async def cmd_broadcast_media(message: Message, state: FSMContext, settings: Settings, db: Database) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    data = await state.get_data()
    text = data.get("bc_text") or ""
    
    
    if message.photo:
        file_id = message.photo[-1].file_id
        await state.update_data(bc_photo=file_id, bc_video=None)
        await state.set_state(BroadcastState.confirming)
        await message.answer_photo(photo=file_id, caption=get_string("admin_broadcast_preview", lang, text=text), reply_markup=broadcast_confirm_keyboard(lang))
    elif message.video:
        file_id = message.video.file_id
        await state.update_data(bc_video=file_id, bc_photo=None)
        await state.set_state(BroadcastState.confirming)
        await message.answer_video(video=file_id, caption=get_string("admin_broadcast_preview", lang, text=text), reply_markup=broadcast_confirm_keyboard(lang))
    else:
        await message.answer(get_string("admin_broadcast_text_ok", lang))

@router.callback_query(F.data == "broadcast_skip")
async def on_broadcast_skip(callback: CallbackQuery, state: FSMContext, settings: Settings, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    data = await state.get_data()
    text = data.get("bc_text") or ""
    await state.set_state(BroadcastState.confirming)
    await _replace_with_text(callback, get_string("admin_broadcast_preview", lang, text=text), reply_markup=broadcast_confirm_keyboard(lang))
    await _safe_answer(callback)

@router.callback_query(F.data == "broadcast_cancel")
async def on_broadcast_cancel(callback: CallbackQuery, state: FSMContext, settings: Settings, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    await state.clear()
    await _replace_with_text(callback, get_string("admin_broadcast_cancel", lang))
    await _safe_answer(callback)

@router.callback_query(F.data == "broadcast_send")
async def on_broadcast_send(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    data = await state.get_data()
    text = data.get("bc_text") or ""
    photo = data.get("bc_photo")
    video = data.get("bc_video")
    await state.clear()
    try:
        await _broadcast_send(bot, db, text, photo, video)
        await _replace_with_text(callback, get_string("admin_broadcast_success", lang))
    except Exception as e:
        await _replace_with_text(callback, get_string("admin_broadcast_error", lang, e=e))
    await _safe_answer(callback)


async def _broadcast_text(bot: Bot, db: Database, text: str) -> None:
    user_ids = await db.list_all_user_ids()
    for uid in user_ids:
        try:
            await bot.send_message(chat_id=uid, text=text)
            await asyncio.sleep(0.03)
        except Exception:
            continue

async def _broadcast_send(bot: Bot, db: Database, text: str, photo_file_id: Optional[str], video_file_id: Optional[str]) -> None:
    user_ids = await db.list_all_user_ids()
    for uid in user_ids:
        try:
            if video_file_id:
                await bot.send_video(chat_id=uid, video=video_file_id, caption=text or "")
            elif photo_file_id:
                await bot.send_photo(chat_id=uid, photo=photo_file_id, caption=text or "")
            else:
                await bot.send_message(chat_id=uid, text=text or "")
            await asyncio.sleep(0.03)
        except Exception:
            continue

async def _broadcast_forward(bot: Bot, db: Database, src_msg: Message) -> None:
    user_ids = await db.list_all_user_ids()
    for uid in user_ids:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=src_msg.chat.id, message_id=src_msg.message_id)
            await asyncio.sleep(0.03)
        except Exception:
            continue


@router.callback_query(F.data.startswith("admin_user:"))
async def admin_choose_user(callback: CallbackQuery, settings: Settings, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    try:
        user_id = int(callback.data.split(":", 1)[1])
    except Exception:
        await _safe_answer(callback, get_string("admin_user_invalid", lang), show_alert=True)
        return
    blocked = await db.get_user_blocked(user_id)
    state_txt = get_string("admin_block", lang) if blocked else get_string("admin_unblock", lang)
    text = get_string("admin_user_title", lang, uid=user_id) + f"\n{get_string('admin_user_status', lang, status=state_txt)}"
    try:
        await callback.message.edit_text(text, reply_markup=admin_user_actions_keyboard(user_id, lang, blocked=blocked))
    except TelegramBadRequest:
        pass
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin_user_history:"))
async def admin_user_history(callback: CallbackQuery, settings: Settings, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    try:
        user_id = int(callback.data.split(":", 1)[1])
    except Exception:
        await _safe_answer(callback, "Invalid User ID", show_alert=True)
        return

    history = await db.list_user_generations(user_id, limit=20)
    if not history:
        await callback.answer(get_string("history_empty", lang), show_alert=True)
        return
        
    await callback.message.answer(f"🕒 История генераций пользователя {user_id}:")
    
    for i, item in enumerate(history, 1):
        pid, result_photo_id, created_at = item
        date_str = created_at if isinstance(created_at, str) else created_at.strftime("%Y-%m-%d %H:%M")
        caption = get_string("history_item", lang, num=i, pid=pid, date=date_str)
        try:
            if result_photo_id.startswith("AgAC"):
                await callback.message.answer_photo(photo=result_photo_id, caption=caption, parse_mode="Markdown")
            else:
                from aiogram.types import FSInputFile
                import os
                file_path = result_photo_id if os.path.exists(result_photo_id) else os.path.join("/app", result_photo_id)
                if os.path.exists(file_path):
                    await callback.message.answer_photo(photo=FSInputFile(file_path), caption=caption, parse_mode="Markdown")
                else:
                    await callback.message.answer(caption, parse_mode="Markdown")
        except Exception:
            await callback.message.answer(caption, parse_mode="Markdown")
        await asyncio.sleep(0.1)

    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin_block:"))
async def admin_block_user(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    try:
        _, uid_str, flag_str = callback.data.split(":", 2)
        user_id = int(uid_str)
        blocked = (flag_str == "1")
    except Exception:
        await _safe_answer(callback, "Неверный формат", show_alert=True)
        return
    await db.upsert_user(user_id=user_id, username=None, first_name=None, last_name=None)
    await db.set_user_blocked(user_id, blocked)
    state_txt = get_string("admin_block", lang) if blocked else get_string("admin_unblock", lang)
    text = get_string("admin_user_title", lang, uid=user_id) + f"\n{get_string('admin_user_status', lang, status=state_txt)}"
    try:
        await callback.message.edit_text(text, reply_markup=admin_user_actions_keyboard(user_id, lang, blocked=blocked))
    except TelegramBadRequest:
        pass
    await _safe_answer(callback, get_string("admin_status_updated", lang), show_alert=False)


class ApiKeyAddState(StatesGroup):
    waiting_token = State()
    waiting_priority = State()


class ApiKeyEditState(StatesGroup):
    waiting_token = State()
    waiting_priority = State()
    key_id = State()


@router.callback_query(F.data == "admin_api_keys")
async def admin_api_keys(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    keys = await db.list_api_keys()
    await callback.message.edit_text(get_string("admin_gemini_keys", lang), reply_markup=admin_api_keys_keyboard(keys, lang))
    await _safe_answer(callback)


@router.callback_query(F.data == "api_key_add")
async def admin_api_key_add_start(callback: CallbackQuery, settings: Settings, state: FSMContext, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    await state.set_state(ApiKeyAddState.waiting_token)
    await _replace_with_text(callback, get_string("admin_gemini_add_key", lang))
    await _safe_answer(callback)


@router.message(ApiKeyAddState.waiting_token)
async def admin_api_key_add_token(message: Message, state: FSMContext, settings: Settings, db: Database) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    token = (message.text or "").strip()
    if not token:
        await message.answer(get_string("admin_gemini_token_empty", lang))
        return
    
    if not all(ord(c) < 128 for c in token):
        await message.answer("❌ Ошибка: Ключ содержит недопустимые символы (например, русские буквы или спецсимволы). Пожалуйста, скопируйте ключ заново.")
        return

    await state.update_data(token=token)
    await state.set_state(ApiKeyAddState.waiting_priority)
    await message.answer(get_string("admin_gemini_priority", lang))


@router.message(ApiKeyAddState.waiting_priority)
async def admin_api_key_add_priority(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    data = await state.get_data()
    token = data.get("token")
    pr_text = (message.text or "").strip()
    priority = 0
    if pr_text and pr_text != "/skip":
        try:
            priority = int(pr_text)
        except Exception:
            await message.answer(get_string("admin_gemini_key_val_error", lang))
            return
    await db.add_api_key(token, priority)
    await state.clear()
    keys = await db.list_api_keys()
    await message.answer(get_string("admin_gemini_key_added", lang), reply_markup=admin_api_keys_keyboard(keys, lang))


@router.callback_query(F.data.startswith("api_key_toggle:"))
async def admin_api_key_toggle(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    key_id = int(callback.data.split(":", 1)[1])
    keys = await db.list_api_keys()
    status = 1
    for k_row in keys:
        kid = k_row[0]
        is_active = k_row[2]
        if kid == key_id:
            status = 0 if is_active else 1
            break
    await db.update_api_key(key_id, is_active=status)
    keys = await db.list_api_keys()
    await callback.message.edit_text(get_string("admin_gemini_keys", lang), reply_markup=admin_api_keys_keyboard(keys, lang))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("api_key_delete:"))
async def admin_api_key_delete(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    key_id = int(callback.data.split(":", 1)[1])
    await db.delete_api_key(key_id)
    keys = await db.list_api_keys()
    await callback.message.edit_text(get_string("admin_gemini_keys", lang), reply_markup=admin_api_keys_keyboard(keys, lang))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("api_key_show:"))
async def admin_api_key_show(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    key_id = int(callback.data.split(":", 1)[1])
    keys = await db.list_api_keys()
    token = None
    state_txt = ""
    for k_row in keys:
        kid = k_row[0]
        tok = k_row[1]
        is_active = k_row[2]
        if kid == key_id:
            token = tok
            state_txt = get_string("admin_maint_enabled", lang) if is_active else get_string("admin_maint_disabled", lang)
            break
    if token is None:
        await _safe_answer(callback, get_string("admin_user_not_found", lang), show_alert=True)
        return
    try:
        await callback.answer(f"#{key_id} {state_txt}: {token}", show_alert=True)
    except Exception:
        pass


@router.callback_query(F.data.startswith("api_key_edit:"))
async def admin_api_key_edit_start(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    key_id = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_key_id=key_id)
    await state.set_state(ApiKeyEditState.waiting_token)
    await _replace_with_text(callback, get_string("admin_gemini_send_token", lang))
    await _safe_answer(callback)


@router.message(ApiKeyEditState.waiting_token)
async def admin_api_key_edit_token(message: Message, state: FSMContext, settings: Settings, db: Database) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    txt = (message.text or "").strip()
    if txt != "/skip" and not txt:
        await message.answer(get_string("admin_gemini_token_empty", lang))
        return
    if txt != "/skip":
        await state.update_data(new_token=txt)
    await state.set_state(ApiKeyEditState.waiting_priority)
    await message.answer(get_string("admin_gemini_priority_req", lang))


@router.message(ApiKeyEditState.waiting_priority)
async def admin_api_key_edit_priority(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    lang = await db.get_user_language(message.from_user.id)
    data = await state.get_data()
    key_id = int(data.get("edit_key_id"))
    token = data.get("new_token")
    pr_text = (message.text or "").strip()
    pr_value = None
    if pr_text and pr_text != "/skip":
        try:
            pr_value = int(pr_text)
        except Exception:
            await message.answer(get_string("admin_gemini_key_val_error", lang))
            return
    await db.update_api_key(key_id, token=token if token else None, priority=pr_value)
    await state.clear()
    keys = await db.list_api_keys()
    await message.answer(get_string("admin_gemini_key_updated", lang), reply_markup=admin_api_keys_keyboard(keys, lang))



@router.callback_query(F.data == "admin_own_variant_prompts")
async def admin_own_variant_prompts_root(callback: CallbackQuery, settings: Settings, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    await _replace_with_text(callback, get_string("admin_own_variant_prompt", lang), reply_markup=admin_own_variant_prompts_keyboard(lang))
    await _safe_answer(callback)

@router.callback_query(F.data == "admin_own_variant_prompt_view")
async def admin_own_variant_prompt_view(callback: CallbackQuery, settings: Settings, db: Database) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    current = await db.get_own_variant_prompt()
    if current:
        
        max_length = 4000
        if len(current) > max_length:
            
            text = get_string("admin_own_variant_prompt_full", lang, text=current[:max_length] + "...")
        else:
            text = get_string("admin_own_variant_prompt_full", lang, text=current)
        await _replace_with_text(callback, text, reply_markup=admin_own_variant_prompts_keyboard(lang))
    else:
        await _replace_with_text(callback, get_string("admin_own_variant_prompt_none", lang), reply_markup=admin_own_variant_prompts_keyboard(lang))
    await _safe_answer(callback)


@router.callback_query(F.data == "admin_own_variant_prompt_edit")
async def admin_own_variant_prompt_edit_start(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await _safe_answer(callback)
        return
    lang = await db.get_user_language(callback.from_user.id)
    current = await db.get_own_variant_prompt()
    if current:
        await _replace_with_text(callback, f"{get_string('admin_own_variant_prompt_full', lang, text=current)}\n\n{get_string('admin_own_variant_prompt_edit_req', lang)}")
    else:
        await _replace_with_text(callback, get_string("admin_own_variant_prompt_edit_req", lang))
    await state.set_state(OwnPromptState.waiting_text)
    await state.update_data(prompt_type="own_variant")
    await _safe_answer(callback)


