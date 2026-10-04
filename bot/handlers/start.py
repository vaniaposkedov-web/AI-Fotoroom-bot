from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import BufferedInputFile
from aiogram.filters import CommandStart
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from bot.keyboards import (
    terms_keyboard,
    main_menu_keyboard,
    create_product_keyboard_dynamic,
    ready_presets_keyboard,
    female_clothes_keyboard,
    male_clothes_keyboard,
    boy_clothes_keyboard,
    girl_clothes_keyboard,
    child_gender_keyboard,
    infographic_selection_keyboard,
    back_step_keyboard,
    back_main_keyboard,
    model_select_keyboard,
    garment_length_keyboard,
    form_view_keyboard,
    confirm_generation_keyboard,
    result_actions_keyboard,
    result_actions_own_keyboard,
    pants_style_keyboard,
    aspect_ratio_keyboard,
    form_generate_keyboard,
    sleeve_length_keyboard,
    camera_distance_keyboard,
    random_gender_keyboard,
    random_loc_group_keyboard,
    random_location_keyboard,
    profile_keyboard,
    plans_keyboard,
    settings_keyboard,
    language_keyboard,
    random_vibe_keyboard,
    random_season_keyboard,
    random_decor_keyboard,
    random_shot_keyboard,
    pose_keyboard,
    angle_keyboard,
    plus_location_keyboard,
    plus_season_keyboard,
    plus_vibe_keyboard,
    plus_gender_keyboard,
    info_lang_keyboard,
    skip_step_keyboard,
    infographic_gender_keyboard,
    infographic_style_keyboard,
    yes_no_keyboard,
    video_mode_keyboard,
    video_aspect_keyboard,
    video_duration_keyboard,
    video_confirm_keyboard,
)
from bot.db import Database
from bot.key_dispatcher import (
    acquire_key_lock_with_wait,
    order_keys_after_last_used,
    release_key_lock,
)
from bot.strings import get_string
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from bot.config import load_settings
from bot.gemini import generate_image, upscale_image_vertex
from bot.telegram_images import telegram_file_ids_to_bytes_list
from bot.prompt_builder import build_final_prompt as _build_final_prompt
import asyncio
import math
import time
from asyncio import Lock
from aiogram.enums import ChatAction

state_lock = Lock()
import logging
import os
import json
import aiosqlite


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TELEGRAM_DOWNLOAD_BASE_URL = os.getenv("TELEGRAM_DOWNLOAD_BASE_URL", "http://g-box.space").rstrip("/")
logger = logging.getLogger(__name__)


def _public_url_for(db_path: str) -> str:
    filename = os.path.basename(db_path)
    # Направляем пользователя на новый маршрут /dl/, который форсирует скачивание
    return f"{TELEGRAM_DOWNLOAD_BASE_URL}/dl/{filename}"


def _telegram_candidate_urls(url: str) -> list[str]:
    if not url:
        return []

    candidates: list[str] = [url]

    if url.startswith("https://"):
        candidates.append(url.replace("https://", "http://", 1))

    if "g-box.space" in url:
        candidates.append(url.replace("g-box.space", "130.49.148.147"))
        if url.startswith("https://"):
            candidates.append(url.replace("https://", "http://", 1).replace("g-box.space", "130.49.148.147"))

    seen = set()
    uniq: list[str] = []
    for u in candidates:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)
    return uniq


def _result_keyboard_with_download(base_kb: InlineKeyboardMarkup | None, download_url: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="⬇️ Скачать оригинал", url=download_url)]
    ]
    if base_kb and getattr(base_kb, "inline_keyboard", None):
        rows.extend(base_kb.inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _generation_timeout_seconds() -> int:
    raw = str(os.getenv("GENERATION_TIMEOUT_SECONDS", "260")).strip()
    try:
        value = int(raw)
    except Exception:
        value = 260
    return max(90, value)


def _progress_bar_expected_wall_seconds() -> float:
    """Ожидаемая длительность генерации для шкалы прогресса (сек), без привязки к таймауту API."""
    raw = str(os.getenv("GENERATION_PROGRESS_EXPECT_SECONDS", "")).strip()
    if raw:
        try:
            return max(25.0, float(raw.replace(",", ".")))
        except Exception:
            pass
    timeout = float(_generation_timeout_seconds())
    return max(42.0, min(88.0, timeout * 0.30))


def _primary_image_model() -> str:
    """Основная модель генерации для Zveno."""
    return "gemini-3.1-flash-image-preview"


def _dedupe_non_empty(values: list[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value:
            continue
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


async def _resolve_model_ref_photo_id(db: Database, model_id: str | int | None) -> str | None:
    if not model_id:
        return None
    try:
        async with aiosqlite.connect(db._db_path) as conn:
            async with conn.execute("SELECT photo_file_id FROM models WHERE id=?", (int(model_id),)) as cur:
                row = await cur.fetchone()
                if row and row[0]:
                    return str(row[0])
    except Exception as exc:
        logger.warning("[payload] failed to resolve model ref photo model_id=%s err=%s", model_id, exc)
    return None


async def _collect_input_photos(data: dict, category: str | None, db: Database) -> tuple[list[str], bool]:
    """
    Единая точка сборки входных фото для генерации/редактирования.
    Возвращает (список фото, флаг missing_product_for_presets).
    """
    missing_product_for_presets = False
    input_photos: list[str | None] = []

    if data.get("normal_gen_mode"):
        photos = data.get("photos", [])
        if isinstance(photos, list):
            input_photos.extend(photos)
        if not input_photos:
            input_photos.append(data.get("user_photo_id") or data.get("photo"))

    elif category == "own_variant":
        # Фото 1 — фон/сцена клиента, фото 2 — товар (как в «Своём варианте» модель+фон)
        bg = data.get("own_bg_photo_id") or data.get("bg_photo") or data.get("user_photo_id")
        prod = data.get("own_product_photo_id") or data.get("photo")
        input_photos.extend([bg, prod])

    elif category == "storefront":
        # Витрина: только фото товара клиента + текст промпта из категории (без фото модели из БД)
        prod = data.get("user_photo_id") or data.get("photo")
        if prod:
            input_photos.append(prod)

    elif category in ("female", "male", "child") or data.get("is_preset") or category == "presets":
        ref = data.get("own_ref_photo_id")
        if not ref:
            ref = await _resolve_model_ref_photo_id(db, data.get("model_id"))

        prod = data.get("user_photo_id") or data.get("photo")
        if ref and prod and str(ref) == str(prod):
            prod = None

        input_photos.extend([ref, prod])
        if not prod and (data.get("is_preset") or category == "presets" or data.get("repeat_mode")):
            missing_product_for_presets = True

    elif data.get("own_mode") or category == "own":
        ref = data.get("own_ref_photo_id") or data.get("bg_photo") or data.get("user_photo_id")
        prod = data.get("own_product_photo_id") or data.get("photo")
        input_photos.extend([ref, prod])

    elif category == "collage" or data.get("collage_mode"):
        model_ph = data.get("collage_model_photo")
        front_ph = data.get("collage_front_photo")
        back_ph = data.get("collage_back_photo")
        input_photos.extend([model_ph, front_ph, back_ph])
    else:
        input_photos.append(data.get("user_photo_id") or data.get("photo"))

    finalized = _dedupe_non_empty(input_photos)
    logger.info(
        "[payload] category=%s collected %s photo(s), missing_product_for_presets=%s",
        category,
        len(finalized),
        missing_product_for_presets,
    )
    return finalized, missing_product_for_presets


router = Router()


class CreateForm(StatesGroup):
    waiting_age = State()
    waiting_child_gender = State()
    waiting_info_gender = State()
    waiting_size = State()
    waiting_height = State()
    waiting_body_type = State()
    waiting_length = State()
    waiting_view = State()
    waiting_prompt = State()
    waiting_aspect = State()
    waiting_support_message = State()
    waiting_sleeve = State()
    waiting_foot = State()
    waiting_pants_style = State()
    waiting_edit_text = State()
    result_ready = State()

    random_mode = State()
    random_other_mode = State()
    random_dummy = State()
    waiting_custom_location = State()
    waiting_has_person = State()

    own_mode = State()
    waiting_ref_photo = State()
    waiting_product_photo = State()
    waiting_own_view = State()
    waiting_own_size = State()
    waiting_own_length = State()
    waiting_own_sleeve = State()
    waiting_own_cut = State()
    plus_loc = State()
    plus_season = State()
    plus_vibe = State()
    waiting_model_search = State()
    category = State()
    cloth = State()

    waiting_info_load = State()
    waiting_info_lang = State()
    waiting_info_lang_custom = State()
    waiting_info_brand = State()
    waiting_info_adv1 = State()
    waiting_info_adv2 = State()
    waiting_info_adv3 = State()
    waiting_info_extra = State()
    waiting_info_angle = State()
    waiting_info_pose = State()
    waiting_info_age = State()
    waiting_info_holiday = State()
    waiting_info_season = State()
    waiting_info_has_person = State()

    waiting_preset_pose = State()
    waiting_preset_dist = State()
    waiting_preset_view = State()
    waiting_preset_season = State()
    waiting_preset_holiday = State()

    waiting_rand_other_has_person = State()
    waiting_rand_other_gender = State()
    waiting_rand_other_name = State()
    waiting_rand_other_angle = State()
    waiting_rand_other_dist = State()
    waiting_rand_other_height = State()
    waiting_rand_other_width = State()
    waiting_rand_other_length = State()
    waiting_rand_other_season = State()
    waiting_rand_other_style = State()
    waiting_rand_other_style_custom = State()
    waiting_rand_loc_group = State()
    waiting_rand_gender = State()
    waiting_rand_loc = State()
    waiting_rand_vibe = State()
    waiting_rand_decor = State()
    waiting_rand_shot = State()
    index = State()
    model_id = State()
    prompt_id = State()

    waiting_own_bg_photo = State()
    waiting_own_product_photo = State()
    waiting_dynamic_step = State()

    waiting_collage_model_photo = State()
    waiting_collage_front_photo = State()
    waiting_collage_back_photo = State()

    waiting_video_photo = State()
    waiting_video_prompt = State()
    waiting_video_aspect = State()
    waiting_video_duration = State()
    waiting_video_confirm = State()


WELCOME_TEXT = (
    "👋 Добро пожаловать в Fashion AI Generator!\n\n"
    "Превращаем фотографии вашей одежды в профессиональные снимки на моделях.\n\n"
    "📋 Перед использованием ознакомьтесь с:\n"
    "1. Условиями использования\n"
    "2. Согласием на обработку данных"
)


async def _safe_answer(callback: CallbackQuery, text: str | None = None, show_alert: bool = False) -> None:
    try:
        if callback.id != "0":
            await callback.answer(text, show_alert=show_alert)
    except Exception:
        pass


async def _replace_with_text(callback: CallbackQuery, text: str, reply_markup=None, **kwargs) -> None:
    try:
        if getattr(callback.message, "photo", None):
            await callback.message.delete()
            await callback.message.answer(text, reply_markup=reply_markup, **kwargs)
        else:
            await callback.message.edit_text(text, reply_markup=reply_markup, **kwargs)
    except TelegramBadRequest:
        try:
            await callback.message.answer(text, reply_markup=reply_markup, **kwargs)
        except TelegramBadRequest:
            pass
    except TelegramRetryAfter:
        try:
            await callback.message.answer(text, reply_markup=reply_markup, **kwargs)
        except Exception:
            pass


async def _ask_sleeve_length(message_or_callback: Message | CallbackQuery, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(message_or_callback.from_user.id)
    from bot.keyboards import own_sleeve_length_keyboard
    text = get_string("select_sleeve_length", lang)
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(text, reply_markup=own_sleeve_length_keyboard(lang))
    else:
        await _replace_with_text(message_or_callback, text, reply_markup=own_sleeve_length_keyboard(lang))
    await state.set_state(CreateForm.waiting_own_sleeve)


@router.callback_query(F.data.startswith("own_sleeve:"))
async def on_own_sleeve(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    val = callback.data.split(":", 1)[1]
    data = await state.get_data()
    lang = await db.get_user_language(callback.from_user.id)

    sleeve_map = {
        "normal": "Обычный",
        "long": "Длинные",
        "three_quarter": "Три четверти",
        "elbow": "До локтей",
        "short": "Короткие",
        "none": "Без рукав",
        "skip": "Пропустить",
    }
    sleeve_text = sleeve_map.get(val, "Пропустить")
    await state.update_data(own_sleeve=sleeve_text)

    if data.get("own_mode"):
        back_kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=get_string("back", lang), callback_data="back_step")]])
        await _replace_with_text(callback, get_string("upload_product", lang), reply_markup=back_kb)
        await state.set_state(CreateForm.waiting_own_product_photo)
        await _safe_answer(callback)
        return

    await _ask_garment_length(callback, state, db)
    await _safe_answer(callback)


async def _ask_garment_length(message_or_callback: Message | CallbackQuery, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(message_or_callback.from_user.id)
    text = get_string("select_garment_length", lang)
    kb = garment_length_keyboard(lang)

    await state.set_state(CreateForm.waiting_length)

    if isinstance(message_or_callback, CallbackQuery):
        await _replace_with_text(message_or_callback, text, reply_markup=kb)
    else:
        await message_or_callback.answer(text, reply_markup=kb)


async def _run_generation_progress(bot, chat_id: int, message_id: int, stop_event: asyncio.Event) -> None:
    start_time = time.time()
    steps_text = [
        "Изучаю ваш запрос",
        "Обрабатываю детали",
        "Применяю нейронные фильтры",
        "Улучшаю качество",
        "Финализирую"
    ]
    total_steps = 5
    step = 1
    try:
        while not stop_event.is_set() and step <= total_steps:
            for sub in range(4):
                if stop_event.is_set(): break
                elapsed = int(time.time() - start_time)
                progress = int(((step - 1) / total_steps + (sub / 4) / total_steps) * 100)
                if progress > 99: progress = 99

                filled = int(progress / 10)
                bar = "🟦" * filled + "⬜️" * (10 - filled)

                text = (
                    f"🚀 Генерация\n\n"
                    f"{steps_text[step - 1]}\n\n"
                    f"{bar} {progress}%\n\n"
                    f"Прошло: {elapsed}с • Шаг {step}/{total_steps}\n\n"
                    f"Результат вас приятно удивит"
                )
                try:
                    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
                    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
                except:
                    pass
                await asyncio.sleep(1.5)
            step += 1
    except:
        pass


async def _answer_model_photo(callback: CallbackQuery, photo: str, caption: str, reply_markup=None) -> Message | None:
    from aiogram.types import InputMediaPhoto, FSInputFile

    if photo.startswith("AgAC"):
        media = InputMediaPhoto(media=photo, caption=caption)
    else:
        file_path = photo if os.path.exists(photo) else os.path.join("/app", photo)
        if os.path.exists(file_path):
            media = InputMediaPhoto(media=FSInputFile(file_path), caption=caption)
        else:
            logger.error(f"Файл фото модели не найден: {photo}")
            await _replace_with_text(callback, caption, reply_markup=reply_markup)
            return None

    try:

        return await callback.message.edit_media(media=media, reply_markup=reply_markup)
    except Exception as e:

        try:
            await callback.message.delete()
        except:
            pass

        try:
            if photo.startswith("AgAC"):
                return await callback.message.answer_photo(photo=photo, caption=caption, reply_markup=reply_markup)
            else:
                file_path = photo if os.path.exists(photo) else os.path.join("/app", photo)
                return await callback.message.answer_photo(photo=FSInputFile(file_path), caption=caption,
                                                           reply_markup=reply_markup)
        except Exception as e2:
            logger.error(f"Ошибка отправки фото модели: {e2}")
            await callback.message.answer(caption, reply_markup=reply_markup)
            return None


@router.callback_query(F.data.startswith("child_gender:"))
async def on_child_gender_select(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    gender = callback.data.split(":")[1]

    await state.clear()
    await state.update_data(child_gender=gender, category="child", cloth=gender, is_preset=True)
    lang = await db.get_user_language(callback.from_user.id)

    await _replace_with_text(callback, "📏 Введите размер одежды или телосложение числом:",
                             reply_markup=back_step_keyboard(lang))
    await state.set_state(CreateForm.waiting_dynamic_step)
    await state.update_data(current_step_key="size")
    await _safe_answer(callback)


async def _check_subscription(user_id: int, bot: Bot, db: Database) -> bool:
    channel_id = await db.get_app_setting("required_channel_id")
    if not channel_id:
        return True
    try:

        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)

        is_subbed = member.status in ("member", "administrator", "creator")
        logger.info(f"Subscription check for {user_id} in {channel_id}: {member.status} (is_subbed: {is_subbed})")
        return is_subbed
    except Exception as e:
        err_msg = str(e).lower()
        logger.error(f"Error checking subscription for {user_id} in {channel_id}: {e}")

        if "chat not found" in err_msg:
            logger.error(f"CRITICAL: Required channel {channel_id} not found. Check if bot is admin there.")
            return False

        if "user not found" in err_msg or "member not found" in err_msg:
            return False

        return True


async def _show_confirmation(message_or_callback: Message | CallbackQuery, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    category_key = data.get("category")
    lang = await db.get_user_language(message_or_callback.from_user.id)

    category = await db.get_category_by_key(category_key)
    cat_name = category[2] if category else category_key

    parts = ["📋 Проверьте выбранные параметры:\n\n"]
    parts.append(f"📦 **Раздел**: {cat_name}\n")

    logic_cat = category_key
    if data.get("is_preset") and category_key in ("female", "male", "child"):
        logic_cat = "presets"

    cat_info = await db.get_category_by_key(logic_cat)
    if cat_info:
        steps = await db.list_steps(cat_info[0])
        for step in steps:
            s_id, s_key, s_question, s_type, s_optional, s_order = step
            val = data.get(s_key)

            if category_key == PHOTOSTUDIO_CAT_KEY:
                title = PHOTOSTUDIO_STEP_LABELS.get(s_key) or s_question.split("\n")[0].split(":")[0].strip()
                if not val and s_key == "photo":
                    # После «Повторить» новое фото приходит в user_photo_id
                    val = data.get("user_photo_id")
                if not val:
                    parts.append(f"🔹 **{title}**: — пропущено\n")
                    continue
                label = data.get(f"{s_key}_label", val)
                if s_type == "photo" or (isinstance(label, str) and label.startswith(("AgAC", "AQAD"))):
                    label = "Фото получено"
                label = str(label)
                if len(label) > 120:
                    label = label[:120].rstrip() + "…"
                parts.append(f"🔹 **{title}**: {label}\n")
                continue

            if val:

                label = data.get(f"{s_key}_label", val)

                if s_type == "photo":
                    label = "Фото получено"
                elif isinstance(label, str) and (label.startswith("AgAC") or label.startswith("AQAD")):
                    label = "Фото получено"
                elif isinstance(label, str) and label.startswith("AIza") and len(label) > 20:
                    label = "Скрыто"

                clean_question = s_question.split(':')[0].strip()
                parts.append(f"🔹 **{clean_question}**: {label}\n")

    if data.get("normal_gen_mode"):
        parts.append(f"📝 **Промпт**: {data.get('prompt', '—')}\n")
        parts.append(f"🖼 **Формат фото**: {data.get('aspect', '1:1')}\n")

    quality_choice = await db.resolve_photo_quality(data.get("selected_quality", "1k"))
    if data.get("selected_quality") != quality_choice:
        await state.update_data(selected_quality=quality_choice)
    if quality_choice == "4k":
        parts.append("💎 **Качество**: 4K (Ультра)\n")
    else:
        parts.append("🖼 **Качество**: 1K (Стандарт)\n")

    parts.append(f"\n{get_string('generation_confirm', lang)}")

    text = "".join(parts)
    from bot.keyboards import confirm_generation_keyboard
    kb = confirm_generation_keyboard(lang)

    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(text, reply_markup=kb)
    else:
        await _replace_with_text(message_or_callback, text, reply_markup=kb)


async def _show_model_selection(message_or_callback: Message | CallbackQuery, state: FSMContext, db: Database) -> None:
    import logging
    logger = logging.getLogger(__name__)
    data = await state.get_data()

    if data.get("is_preset") and data.get("gender"):
        category = data.get("gender")
        cloth = "all"
    else:
        category = data.get("display_category") or data.get("category", "female")
        cloth = data.get("selected_cloth") or data.get("cloth", "all")

    total = await db.count_models(category, cloth)
    logger.info("[flow] model_select category=%s cloth=%s total=%s", category, cloth, total)
    if total <= 0:
        cloth = "all"
        total = await db.count_models(category, cloth)

    if total <= 0:

        text = "Извините, в этой категории пока нет доступных моделей."
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(text)
        else:
            await _replace_with_text(message_or_callback, text)
        return

    index = data.get("model_index", 0)
    if index >= total: index = 0

    model = await db.get_model_by_index(category, cloth, index)
    lang = await db.get_user_language(message_or_callback.from_user.id)

    from bot.handlers.start import _model_header
    text = _model_header(index, total)

    from bot.keyboards import model_select_keyboard
    kb = model_select_keyboard(category, cloth, index, total, lang, logic_category=data.get("category"))

    if model and model[3]:
        if isinstance(message_or_callback, CallbackQuery):
            res_msg = await _answer_model_photo(message_or_callback, model[3], text, kb)
            if res_msg and res_msg.photo and not model[3].startswith("AgAC"):
                await db.set_model_photo(model[0], res_msg.photo[-1].file_id)
        else:
            await message_or_callback.answer_photo(photo=model[3], caption=text, reply_markup=kb)
    else:
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(text, reply_markup=kb)
        else:
            await _replace_with_text(message_or_callback, text, reply_markup=kb)


async def _show_next_step(message_or_callback: Message | CallbackQuery, state: FSMContext, db: Database) -> None:
    import logging
    logger = logging.getLogger(__name__)
    data = await state.get_data()
    cat_key = data.get("category")
    if not cat_key:
        return

    logic_cat = cat_key
    if data.get("is_preset") and cat_key in ("female", "male", "child"):
        logic_cat = "presets"

    category = await db.get_category_by_key(logic_cat)
    if not category:
        return

    cat_id = category[0]
    steps = await db.list_steps(cat_id)

    current_step_index = data.get("current_step_index", 0)

    logger.info("[flow] show_next_step data=%s", {k: v for k, v in data.items() if not k.startswith("_")})

    while current_step_index < len(steps):
        step = steps[current_step_index]
        step_id, step_key, question, input_type, is_optional, order = step
        gender = data.get("gender") or data.get("rand_gender") or data.get("info_gender") or data.get("child_gender")

        if step_key == "age" and gender in ("boy", "girl"):
            current_step_index += 1
            continue

        person_absent = False
        for k, v in data.items():

            if k.endswith("_label"):
                label_v = str(v).lower()

                if any(x in label_v for x in ("без человека", "without person", "không có người")):
                    person_absent = True
                    break

                if any(x in label_v for x in ("нет", "no", "không")):
                    low_k = k.lower()
                    if any(x in low_k for x in ("person", "presence", "человек", "присутствие")):
                        person_absent = True
                        break

            if isinstance(v, str):
                v_low = v.lower()
                if v_low in ("person_no", "without_person", "no_person", "no-person"):
                    person_absent = True
                    break

                if v_low in ("no", "off", "нет"):
                    low_k = k.lower()
                    if any(x in low_k for x in ("person", "presence", "человек", "присутствие")):
                        person_absent = True
                        break

        low_step_key = step_key.lower()

        if cat_key in ("random_other", "infographic_other"):
            is_skip_target = any(x in low_step_key for x in ("age", "pose", "size", "возраст", "поза", "телосложение"))
        else:
            is_skip_target = any(
                x in low_step_key for x in ("age", "pose", "height", "size", "возраст", "поза", "рост", "телосложение"))

        if cat_key in ["infographic_other", "random_other"] and person_absent:
            if any(x in low_step_key for x in ("age", "возраст", "pose", "поза", "size", "телосложение")):
                logger.info("[flow] SPECIAL SKIP step=%s for %s because person_absent=True", step_key, cat_key)
                current_step_index += 1
                continue

        if person_absent and is_skip_target:
            logger.info("[flow] SKIP step=%s because person_absent=True found in data", step_key)
            current_step_index += 1
            continue

        loc_group = data.get("rand_loc_group")
        if loc_group:
            if loc_group == "indoor" and step_key == "rand_location_outdoor":
                logger.info("[flow] SKIP rand_location_outdoor because loc_group is indoor")
                current_step_index += 1
                continue
            if loc_group == "outdoor" and step_key == "rand_location_indoor":
                logger.info("[flow] SKIP rand_location_indoor because loc_group is outdoor")
                current_step_index += 1
                continue

            if loc_group == "indoor" and "season" in step_key.lower():
                logger.info("[flow] SKIP %s because loc_group is indoor", step_key)
                current_step_index += 1
                continue

        if is_skip_target:
            logger.info("[flow] CHECK skip target=%s person_absent=%s", step_key, person_absent)

        is_going_back = data.get("is_going_back", False)
        if step_key in data and data.get(step_key) is not None and not is_going_back:
            current_step_index += 1
            continue

        if input_type == "buttons":
            options = await db.list_step_options(step_id)
            if not options and not is_optional:
                current_step_index += 1
                continue

        break

    await state.update_data(current_step_index=current_step_index)

    await state.update_data(is_going_back=False)

    if current_step_index >= len(steps):

        aspect = data.get("aspect")
        if not aspect:
            await state.set_state(CreateForm.waiting_aspect)
            lang = await db.get_user_language(message_or_callback.from_user.id)
            from bot.keyboards import aspect_ratio_keyboard
            text = get_string("select_format", lang)
            if isinstance(message_or_callback, Message):
                await message_or_callback.answer(text, reply_markup=aspect_ratio_keyboard(lang))
            else:
                await _replace_with_text(message_or_callback, text, reply_markup=aspect_ratio_keyboard(lang))
            return

        quality = data.get("selected_quality")
        if not quality:
            await _ask_quality(message_or_callback, state, db)
            return

        await _show_confirmation(message_or_callback, state, db)
        return

    step = steps[current_step_index]
    step_id, step_key, question, input_type, is_optional, order = step
    lang = await db.get_user_language(message_or_callback.from_user.id)
    logger.info("[flow] show_step category=%s step=%s type=%s index=%s", cat_key, step_key, input_type,
                current_step_index)
    question = await db.get_step_text(step_id, lang)

    await state.update_data(current_step_id=step_id, current_step_key=step_key)
    await state.set_state(CreateForm.waiting_dynamic_step)

    if input_type == "buttons":
        options = await db.list_step_options_localized(step_id, lang)
        from bot.keyboards import dynamic_keyboard

        show_skip = bool(is_optional)
        if any(x in step_key.lower() for x in (
        "adv_", "extra_info", "brand_name", "info_load", "преимущество", "доп_инфо", "доп_текст", "название", "бренд",
        "product_name")):
            show_skip = True

        kb = dynamic_keyboard(options, show_skip, lang)

        if step_key == "length":
            if isinstance(message_or_callback, CallbackQuery):
                await _replace_with_text(message_or_callback, question, reply_markup=kb)
            else:
                await message_or_callback.answer(question, reply_markup=kb)
            return

        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(question, reply_markup=kb)
        else:
            await _replace_with_text(message_or_callback, question, reply_markup=kb)

    elif input_type == "photo":
        kb = back_step_keyboard(lang)
        show_skip = bool(is_optional)
        if any(x in step_key.lower() for x in (
        "adv_", "extra_info", "brand_name", "info_load", "преимущество", "доп_инфо", "доп_текст", "название", "бренд",
        "product_name")):
            show_skip = True

        if show_skip:
            from bot.keyboards import skip_step_keyboard
            kb = skip_step_keyboard("dyn_opt", lang)

        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(question, reply_markup=kb)
        else:
            await _replace_with_text(message_or_callback, question, reply_markup=kb)

    elif input_type == "model_select":

        try:

            await _show_model_selection(message_or_callback, state, db)
        except Exception as e:
            logger.exception("Error in model_select step")
            err_text = "⚠️ Ошибка при показе выбора модели. Попробуйте ещё раз."
            if isinstance(message_or_callback, Message):
                await message_or_callback.answer(err_text)
            else:
                await _replace_with_text(message_or_callback, err_text)
            return

    else:
        kb = back_step_keyboard(lang)
        show_skip = bool(is_optional)
        if any(x in step_key.lower() for x in (
        "adv_", "extra_info", "brand_name", "info_load", "преимущество", "доп_инфо", "доп_текст", "название", "бренд",
        "product_name")):
            show_skip = True

        if show_skip:
            from bot.keyboards import skip_step_keyboard
            kb = skip_step_keyboard("dyn_opt", lang)

        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(question, reply_markup=kb)
        else:
            await _replace_with_text(message_or_callback, question, reply_markup=kb)


@router.callback_query(F.data == "menu_support")
async def on_menu_support(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    await _replace_with_text(
        callback,
        get_string("support_contact_message", lang),
        reply_markup=back_main_keyboard(lang),
    )
    await _safe_answer(callback)


@router.message(CreateForm.waiting_support_message)
async def on_support_message(message: Message, state: FSMContext, db: Database) -> None:
    if message.text and message.text.startswith("/"):
        return

    f_id = None
    f_type = 'text'
    text = message.text or message.caption

    if message.photo:
        f_id = message.photo[-1].file_id
        f_type = 'photo'
    elif message.video:
        f_id = message.video.file_id
        f_type = 'video'
    elif not message.text:
        return

    await db.add_support_message(message.from_user.id, text=text, file_id=f_id, file_type=f_type, is_admin=False)
    lang = await db.get_user_language(message.from_user.id)

    await message.answer("✅ Ваше сообщение отправлено поддержке. Ожидайте ответа.")
    from bot.keyboards import back_main_keyboard
    await message.answer("Вы можете отправить ещё что-то или вернуться в меню:", reply_markup=back_main_keyboard(lang))


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    await state.clear()
    user = message.from_user
    await db.upsert_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )

    if not await _ensure_access(message, db, bot):
        return

    lang = await db.get_user_language(user.id)
    await message.answer(get_string("start_welcome", lang), reply_markup=main_menu_keyboard(lang))


@router.message(F.text == "/profile")
async def cmd_profile(message: Message, db: Database) -> None:
    class FakeCallback:
        def __init__(self, message, from_user):
            self.message = message
            self.from_user = from_user

        async def answer(self, *args, **kwargs): pass

    await on_menu_profile(FakeCallback(message, message.from_user), db)


@router.message(F.text == "/settings")
async def cmd_settings(message: Message, db: Database) -> None:
    class FakeCallback:
        def __init__(self, message, from_user):
            self.message = message
            self.from_user = from_user

        async def answer(self, *args, **kwargs): pass

    await on_menu_settings(FakeCallback(message, message.from_user), db)


@router.message(F.text == "/reset")
async def cmd_reset(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    lang = await db.get_user_language(message.from_user.id)
    await message.answer(get_string("main_menu_title", lang), reply_markup=main_menu_keyboard(lang))


@router.message(F.text == "/help")
async def cmd_help(message: Message, db: Database) -> None:
    class FakeCallback:
        def __init__(self, message, from_user):
            self.message = message
            self.from_user = from_user

        async def answer(self, *args, **kwargs): pass

    await on_menu_howto(FakeCallback(message, message.from_user), db)


@router.callback_query(F.data.startswith("dyn_opt:"))
async def on_dynamic_option(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    val = callback.data.split(":", 1)[1]
    data = await state.get_data()
    step_key = data.get("current_step_key")
    lang = await db.get_user_language(callback.from_user.id)

    if val == "skip":

        if step_key:
            await state.update_data({step_key: ""})
            await state.update_data({f"{step_key}_label": "Пропущено"})
    else:

        try:
            opt_id = int(val)

            import aiosqlite
            async with aiosqlite.connect(db._db_path) as conn:
                async with conn.execute("SELECT option_text, option_value, custom_prompt FROM step_options WHERE id=?",
                                        (opt_id,)) as cur:
                    row = await cur.fetchone()
                    if row:
                        opt_text, opt_val, custom_prompt = row

                        if opt_val == "back" and str(opt_text).lower() in ("назад", "back", "quay lại"):
                            await on_back_step(callback, state, db)
                            await _safe_answer(callback)
                            return

                        if opt_val == "skip":
                            opt_text = None
                            opt_val = None

                        if custom_prompt:
                            await state.update_data(waiting_custom_for=step_key)
                            await _replace_with_text(callback, custom_prompt, reply_markup=back_step_keyboard(lang))
                            await _safe_answer(callback)
                            return

                        if opt_val is not None:
                            await state.update_data({step_key: opt_val})

                            await state.update_data({f"{step_key}_label": opt_text})
        except ValueError:

            await state.update_data({step_key: val})

    new_data = await state.get_data()
    current_idx = new_data.get("current_step_index", 0)

    if new_data.get("is_preset") and step_key == "gender":
        cat_db = await db.get_category_by_key("presets")
        if cat_db:
            steps = await db.list_steps(cat_db[0])
            for idx, s in enumerate(steps):
                if s[1] == "model_select":
                    await state.update_data(current_step_index=idx)
                    await _show_next_step(callback, state, db)
                    await _safe_answer(callback)
                    return

    await state.update_data(current_step_index=current_idx + 1)
    await _show_next_step(callback, state, db)
    await _safe_answer(callback)


@router.message(CreateForm.waiting_dynamic_step, F.photo)
async def on_dynamic_photo(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    step_key = data.get("current_step_key")
    if not step_key:
        return

    photo_id = message.photo[-1].file_id
    await state.update_data({step_key: photo_id, f"{step_key}_label": "Фото загружено"})

    current_idx = data.get("current_step_index", 0)
    await state.update_data(current_step_index=current_idx + 1)
    await _show_next_step(message, state, db)


@router.message(CreateForm.waiting_dynamic_step)
async def on_dynamic_input(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()

    if message.text and message.text.lower() in ("пропустить", "skip", "/skip"):
        step_key = data.get("current_step_key")
        if step_key:
            await state.update_data({step_key: ""})
            await state.update_data({f"{step_key}_label": "Пропущено"})
        current_idx = data.get("current_step_index", 0)
        await state.update_data(current_step_index=current_idx + 1)
        await _show_next_step(message, state, db)
        return

    if message.text:
        step_key = data.get("current_step_key")
        waiting_custom_for = data.get("waiting_custom_for")

        target_key = waiting_custom_for if waiting_custom_for else step_key

        text_len = len(message.text)
        max_len = 200

        if target_key:
            low_key = target_key.lower()
            if low_key == PHOTOSTUDIO_FREEFORM_STEP:
                max_len = TELEGRAM_TEXT_LIMIT
            elif any(x in low_key for x in ("adv_", "преимущество")):
                max_len = 100
            elif any(x in low_key for x in ("extra_info", "доп_текст")):
                max_len = 80
            elif waiting_custom_for:
                max_len = 70

        if text_len > max_len:
            lang = await db.get_user_language(message.from_user.id)
            await message.answer(
                f"⚠️ Текст слишком длинный ({text_len}/{max_len} симв.). Пожалуйста, сократите до {max_len} символов.")
            return

        if max_len == TELEGRAM_TEXT_LIMIT and text_len >= TELEGRAM_TEXT_LIMIT - 96:
            lang = await db.get_user_language(message.from_user.id)
            await message.answer(get_string("photostudio_text_maybe_cut", lang))

    if data.get("waiting_custom_for"):
        step_key = data.get("waiting_custom_for")
        await state.update_data({step_key: message.text})
        await state.update_data({f"{step_key}_label": message.text})
        await state.update_data(waiting_custom_for=None)
    else:
        step_key = data.get("current_step_key")

        if message.photo:
            await state.update_data({step_key: message.photo[-1].file_id})
        else:
            await state.update_data({step_key: message.text})
            await state.update_data({f"{step_key}_label": message.text})

    new_data = await state.get_data()
    current_idx = new_data.get("current_step_index", 0)

    await state.update_data(current_step_index=current_idx + 1)
    await _show_next_step(message, state, db)


@router.callback_query(F.data == "accept_terms")
async def on_accept_terms(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    await db.set_terms_acceptance(callback.from_user.id, True)

    if await _ensure_access(callback, db, bot):
        lang = await db.get_user_language(callback.from_user.id)
        await _replace_with_text(callback, get_string("main_menu_title", lang), reply_markup=main_menu_keyboard(lang))
    await _safe_answer(callback)


@router.callback_query(F.data == "check_subscription")
async def on_check_subscription(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if await _ensure_access(callback, db, bot):
        lang = await db.get_user_language(callback.from_user.id)
        await _replace_with_text(callback, get_string("main_menu_title", lang), reply_markup=main_menu_keyboard(lang))
    else:

        await _safe_answer(callback, "Вы все еще не подписаны!", show_alert=True)


@router.callback_query(F.data == "back_main")
async def on_back_main(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    current = await state.get_state()
    lang = await db.get_user_language(callback.from_user.id)
    text = get_string("main_menu_title", lang)

    if current == CreateForm.result_ready.state:
        await callback.message.answer(text, reply_markup=main_menu_keyboard(lang))
        await state.clear()
        await _safe_answer(callback)
        return
    await state.clear()
    if callback.message and callback.message.text == text:
        await _safe_answer(callback)
        return
    await _replace_with_text(callback, text, reply_markup=main_menu_keyboard(lang))
    await _safe_answer(callback)


@router.callback_query(F.data == "menu_create")
async def on_create_photo(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    lang = await db.get_user_language(callback.from_user.id)

    if await db.get_maintenance():
        settings = load_settings()
        if callback.from_user.id not in (settings.admin_ids or []):
            await _safe_answer(callback, get_string("maintenance_alert", lang), show_alert=True)
            return
    balance = await db.get_user_balance(callback.from_user.id)
    price = await db.get_user_generation_price(callback.from_user.id)

    if await db.get_user_blocked(callback.from_user.id):
        await _safe_answer(callback, get_string("user_blocked", lang), show_alert=True)
        return
    if balance < price:
        await _safe_answer(callback, get_string("limit_rem_zero", lang), show_alert=True)
        return

    await state.update_data(category="normal", normal_gen_mode=True, photos=[], last_photos_msg_id=None)

    text = "📸 Пришлите до 4 фото (можно по одному или серией)."
    await _replace_with_text(callback, text, reply_markup=back_main_keyboard(lang))
    await state.set_state(CreateForm.waiting_view)


@router.callback_query(F.data == "menu_market")
async def on_marketplace_menu(callback: CallbackQuery, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)

    if await db.get_maintenance():
        settings = load_settings()
        if callback.from_user.id not in (settings.admin_ids or []):
            await _safe_answer(callback, get_string("maintenance_alert", lang), show_alert=True)
            return
    balance = await db.get_user_balance(callback.from_user.id)
    price = await db.get_user_generation_price(callback.from_user.id)

    if await db.get_user_blocked(callback.from_user.id):
        await _safe_answer(callback, get_string("user_blocked", lang), show_alert=True)
        return
    if balance < price:
        await _safe_answer(callback, get_string("limit_rem_zero", lang), show_alert=True)
        return

    statuses = await db.list_categories_enabled()
    from bot.keyboards import marketplace_menu_keyboard
    await _replace_with_text(callback, get_string("marketplace_menu", lang),
                             reply_markup=marketplace_menu_keyboard(statuses, lang))
    await _safe_answer(callback)


PHOTOSTUDIO_CAT_KEY = "photostudio_wb"
PHOTOSTUDIO_FREEFORM_STEP = "photostudio_details"
TELEGRAM_TEXT_LIMIT = 4096

# Короткие подписи для экрана подтверждения — иначе туда печатается весь текст вопроса
PHOTOSTUDIO_STEP_LABELS = {
    "photo": "Фото товара",
    "length": "Длина изделия",
    "sleeve": "Длина рукава",
    "pants_style": "Тип кроя",
    "photostudio_details": "Доп. информация",
}


@router.callback_query(F.data == "photostudio_wb")
async def on_photostudio_wb(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    lang = await db.get_user_language(callback.from_user.id)

    category_db = await db.get_category_by_key(PHOTOSTUDIO_CAT_KEY)
    steps = await db.list_steps(category_db[0]) if category_db else []

    if not steps:
        await _replace_with_text(callback, get_string("photostudio_wb_dev", lang),
                                 reply_markup=back_main_keyboard(lang))
        await _safe_answer(callback)
        return

    await state.clear()
    await state.update_data(category=PHOTOSTUDIO_CAT_KEY)
    reset_payload = {s[1]: None for s in steps}
    reset_payload["current_step_index"] = 0
    await state.update_data(**reset_payload)
    await _show_next_step(callback, state, db)
    await _safe_answer(callback)


async def _ensure_access(message_or_callback: Message | CallbackQuery, db: Database, bot: Bot) -> bool:
    user_id = message_or_callback.from_user.id
    lang = await db.get_user_language(user_id)
    from bot.keyboards import terms_keyboard, subscription_check_keyboard

    if await db.get_user_blocked(user_id):
        text = get_string("user_blocked", lang)
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(text)
        else:
            await _replace_with_text(message_or_callback, text)
        return False

    settings = load_settings()
    if user_id in (settings.admin_ids or []):
        logger.info(f"Admin {user_id} bypasses access checks")
        return True

    accepted = await db.get_user_accepted_terms(user_id)
    if not accepted:
        text = get_string("start_welcome", lang)
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(text, reply_markup=terms_keyboard(lang))
        else:
            await _replace_with_text(message_or_callback, text, reply_markup=terms_keyboard(lang))
        return False

    channel_id = await db.get_app_setting("required_channel_id")
    logger.info(f"Checking subscription for {user_id} in channel '{channel_id}'")
    if channel_id and str(channel_id).strip():
        is_subbed = await _check_subscription(user_id, bot, db)
        logger.info(f"User {user_id} sub status: {is_subbed}")
        if not is_subbed:
            channel_url = await db.get_app_setting("required_channel_url", "https://t.me/bnbslow")
            text = get_string("subscribe_channel", lang)
            if isinstance(message_or_callback, Message):
                await message_or_callback.answer(text, reply_markup=subscription_check_keyboard(channel_url, lang))
            else:
                await _replace_with_text(message_or_callback, text,
                                         reply_markup=subscription_check_keyboard(channel_url, lang))
            return False

    return True


@router.callback_query(F.data.startswith("create_cat:"))
async def on_create_category_universal(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    cat_key = callback.data.split(":")[1]
    lang = await db.get_user_language(callback.from_user.id)

    if await db.get_maintenance():
        settings = load_settings()
        if callback.from_user.id not in (settings.admin_ids or []):
            await _safe_answer(callback, get_string("maintenance_alert", lang), show_alert=True)
            return

    if not await db.get_category_enabled(cat_key):
        await _safe_answer(callback, get_string("no_models_in_category_alert", lang), show_alert=True)
        return

    if cat_key == "infographics":
        await on_infographic_selection_menu(callback, db)
        return

    await state.clear()
    await state.update_data(category=cat_key)

    if cat_key == "random":
        await state.update_data(random_mode=True)
    elif cat_key == "random_other":
        await state.update_data(random_other_mode=True)
    elif cat_key == "own":
        await state.update_data(own_mode=True)
    elif cat_key == "presets":
        await state.update_data(is_preset=True)
    elif cat_key.startswith("infographic"):
        await state.update_data(infographic_mode=True)
    elif cat_key == "storefront":
        await state.update_data(storefront_mode=True)
    elif cat_key == "collage":
        await state.update_data(collage_mode=True)

    category_db = await db.get_category_by_key(cat_key)
    if category_db:
        steps = await db.list_steps(category_db[0])
        if steps:
            reset_payload = {s[1]: None for s in steps}
            reset_payload["current_step_index"] = 0
            await state.update_data(**reset_payload)
            await _show_next_step(callback, state, db)
            await _safe_answer(callback)
            return

    if cat_key == "female":
        await on_female_category(callback, db, state)
    elif cat_key == "male":
        await on_male_category(callback, db, state)
    elif cat_key == "child":
        await on_child_category(callback, db, state)
    elif cat_key == "storefront":
        await on_storefront_category(callback, db, state)
    elif cat_key == "whitebg":
        await on_whitebg_category(callback, db, state)
    elif cat_key == "own":
        await on_create_own(callback, db, state)
    elif cat_key == "own_variant":
        await on_create_own_variant(callback, db, state)
    elif cat_key == "collage":
        await on_collage_category(callback, db, state)
    elif cat_key == "random":
        await on_create_random(callback, state, db)
    elif cat_key == "random_other":
        await _show_next_step(callback, state, db)
    elif cat_key.startswith("infographic"):
        await _show_next_step(callback, state, db)
    elif cat_key == "presets":
        await on_ready_presets(callback, db, state)

    await _safe_answer(callback)


@router.callback_query(F.data.startswith("preset_gender:"))
async def on_preset_gender_selected(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    gender = callback.data.split(":")[1]
    await state.clear()

    cat_db = await db.get_category_by_key("presets")
    if cat_db:
        steps = await db.list_steps(cat_db[0])
        for idx, s in enumerate(steps):
            if s[1] == "model_select":
                reset_payload = {st[1]: None for st in steps}
                reset_payload.update({
                    "category": "presets",
                    "is_preset": True,
                    "gender": gender,
                    "gender_label": get_string("cat_female",
                                               await db.get_user_language(callback.from_user.id)) if gender == "female"
                    else get_string("cat_male", await db.get_user_language(callback.from_user.id)) if gender == "male"
                    else get_string("gender_boy", await db.get_user_language(callback.from_user.id)) if gender == "boy"
                    else get_string("gender_girl", await db.get_user_language(callback.from_user.id)),
                    "current_step_index": idx
                })
                await state.update_data(**reset_payload)
                await _show_next_step(callback, state, db)
                await _safe_answer(callback)
                return

    await state.update_data(category="presets", gender=gender, is_preset=True)

    await _show_models_for_category(callback, db, category=gender, cloth="all", index=0, logic_category="presets")
    await _safe_answer(callback)


async def on_infographic_selection_menu(callback: CallbackQuery, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    enabled = await db.list_categories_enabled()
    from bot.keyboards import infographic_selection_keyboard
    await _replace_with_text(callback, get_string("cat_infographics", lang),
                             reply_markup=infographic_selection_keyboard(enabled, lang))


async def on_ready_presets(callback: CallbackQuery, db: Database, state: FSMContext | None = None) -> None:
    lang = await db.get_user_language(callback.from_user.id)

    cat_db = await db.get_category_by_key("presets")
    if cat_db and state:
        steps = await db.list_steps(cat_db[0])
        if steps:
            reset_payload = {s[1]: None for s in steps}
            reset_payload.update({"category": "presets", "is_preset": True, "current_step_index": 0})
            await state.clear()
            await state.update_data(**reset_payload)
            await _show_next_step(callback, state, db)
            await _safe_answer(callback)
            return
    enabled = await db.list_categories_enabled()
    from bot.keyboards import ready_presets_keyboard
    await _replace_with_text(callback, get_string("cat_presets", lang),
                             reply_markup=ready_presets_keyboard(enabled, lang))


async def on_female_category(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(category="female", cloth="all", is_preset=True)

    category_db = await db.get_category_by_key("female")
    if category_db:
        await state.update_data(current_step_index=0)
        await _show_next_step(callback, state, db)
    else:
        lang = await db.get_user_language(callback.from_user.id)
        await _replace_with_text(callback, "🎂 Введите возраст модели числом:", reply_markup=back_step_keyboard(lang))
        await state.set_state(CreateForm.waiting_dynamic_step)
        await state.update_data(current_step_key="age")


async def on_male_category(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(category="male", cloth="all", is_preset=True)

    category_db = await db.get_category_by_key("male")
    if category_db:
        await state.update_data(current_step_index=0)
        await _show_next_step(callback, state, db)
    else:
        lang = await db.get_user_language(callback.from_user.id)
        await _replace_with_text(callback, "🎂 Введите возраст модели числом:", reply_markup=back_step_keyboard(lang))
        await state.set_state(CreateForm.waiting_dynamic_step)
        await state.update_data(current_step_key="age")


async def _show_models_for_category(callback: CallbackQuery, db: Database, category: str, cloth: str, index: int = 0,
                                    logic_category: str = None) -> None:
    total = await db.count_models(category, cloth)
    if total <= 0:
        await _safe_answer(callback, "Модели не найдены", show_alert=True)
        return

    if index < 0: index = total - 1
    if index >= total: index = 0

    text = _model_header(index, total)
    model = await db.get_model_by_index(category, cloth, index)

    lang = await db.get_user_language(callback.from_user.id)
    kb = model_select_keyboard(category, cloth, index, total, lang, logic_category=logic_category)

    if model and model[3]:

        res_msg = await _answer_model_photo(callback, model[3], text, kb)

        if res_msg and res_msg.photo and not model[3].startswith("AgAC"):
            new_file_id = res_msg.photo[-1].file_id
            await db.set_model_photo(model[0], new_file_id)
            logger.info(f"Cached file_id for model {model[0]}")
    else:
        await _replace_with_text(callback, text, reply_markup=kb)


async def on_child_category(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.update_data(category="child")
    lang = await db.get_user_language(callback.from_user.id)
    await _replace_with_text(callback, get_string("select_gender", lang), reply_markup=child_gender_keyboard(lang))


async def on_create_random(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    await state.update_data(random_mode=True, category="random")
    lang = await db.get_user_language(callback.from_user.id)
    await _replace_with_text(callback, get_string("select_loc_group", lang),
                             reply_markup=random_loc_group_keyboard(lang))
    await state.set_state(CreateForm.waiting_rand_loc_group)


@router.callback_query(CreateForm.waiting_rand_loc, F.data.startswith("rand_location:"))
@router.callback_query(CreateForm.waiting_custom_location)
async def on_random_location_after(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if callback.data.startswith("rand_location:"):
        loc = callback.data.split(":", 1)[1]
        await state.update_data(rand_location=loc)

    lang = await db.get_user_language(callback.from_user.id)

    await _replace_with_text(callback, get_string("select_model_gender", lang),
                             reply_markup=random_gender_keyboard(lang))
    await state.set_state(CreateForm.waiting_rand_gender)
    await _safe_answer(callback)


@router.message(CreateForm.waiting_custom_location)
async def on_random_location_custom_msg(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    await state.update_data(rand_location_custom=text, rand_location="custom")
    lang = await db.get_user_language(message.from_user.id)

    await message.answer(get_string("select_model_gender", lang), reply_markup=random_gender_keyboard(lang))
    await state.set_state(CreateForm.waiting_rand_gender)


@router.callback_query(F.data.startswith("rand_gender:"))
async def on_random_gender(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    gender = callback.data.split(":")[1]
    await state.update_data(rand_gender=gender)
    lang = await db.get_user_language(callback.from_user.id)

    if gender in ("male", "female"):
        await _replace_with_text(callback, "🎂 Введите возраст модели числом:", reply_markup=back_step_keyboard(lang))
        await state.set_state(CreateForm.waiting_dynamic_step)
        await state.update_data(current_step_key="age")
    else:

        await _replace_with_text(callback, "📏 Введите размер одежды или телосложение числом:",
                                 reply_markup=back_step_keyboard(lang))
        await state.set_state(CreateForm.waiting_dynamic_step)
        await state.update_data(current_step_key="size")
    await _safe_answer(callback)


async def on_create_random_other(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    await state.update_data(random_other_mode=True, category="random_other")
    lang = await db.get_user_language(callback.from_user.id)
    await _replace_with_text(callback, get_string("has_person_ask", lang), reply_markup=yes_no_keyboard(lang))
    await state.set_state(CreateForm.waiting_rand_other_has_person)


@router.callback_query(CreateForm.waiting_rand_other_has_person, F.data.startswith("choice:"))
async def on_rand_other_has_person(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    choice = callback.data.split(":")[1]
    has_person = (choice == "yes")
    await state.update_data(has_person=has_person)
    lang = await db.get_user_language(callback.from_user.id)

    if has_person:

        from bot.keyboards import infographic_gender_keyboard
        await _replace_with_text(callback, get_string("select_gender", lang),
                                 reply_markup=infographic_gender_keyboard(lang))
        await state.set_state(CreateForm.waiting_rand_other_gender)
    else:

        await _replace_with_text(callback, get_string("enter_info_load", lang),
                                 reply_markup=skip_step_keyboard("info_load", lang))
        await state.set_state(CreateForm.waiting_info_load)
    await _safe_answer(callback)


@router.callback_query(CreateForm.waiting_rand_other_gender, F.data.startswith("info_gender:"))
async def on_rand_other_gender(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    gender = callback.data.split(":")[1]
    await state.update_data(gender=gender)
    lang = await db.get_user_language(callback.from_user.id)

    await _replace_with_text(callback, get_string("enter_info_load", lang),
                             reply_markup=skip_step_keyboard("info_load", lang))
    await state.set_state(CreateForm.waiting_info_load)
    await _safe_answer(callback)


@router.message(CreateForm.waiting_rand_other_name)
@router.callback_query(F.data == "rand_name:skip")
async def on_rand_other_name(message_or_callback: Message | CallbackQuery, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(message_or_callback.from_user.id)

    if isinstance(message_or_callback, Message):
        text = (message_or_callback.text or "").strip()
        if not text or len(text) > 50:
            await message_or_callback.answer("⚠️ Название слишком длинное (максимум 50 символов). Попробуйте еще раз:")
            return
        await state.update_data(product_name=text)
    else:
        await state.update_data(product_name="")

    from bot.keyboards import form_view_keyboard
    msg_text = "Выберите угол камеры (Спереди/Сзади):"
    markup = form_view_keyboard(lang)

    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(msg_text, reply_markup=markup)
    else:
        await _replace_with_text(message_or_callback, msg_text, reply_markup=markup)
        await _safe_answer(message_or_callback)
    await state.set_state(CreateForm.waiting_rand_other_angle)


@router.callback_query(CreateForm.waiting_rand_other_angle, F.data.startswith("form_view:"))
async def on_rand_other_angle(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    view = callback.data.split(":")[1]
    await state.update_data(view=view)
    lang = await db.get_user_language(callback.from_user.id)

    from bot.keyboards import camera_dist_keyboard
    await _replace_with_text(callback, "Выберите ракурс фотографии (Дальний/Средний/Близкий):",
                             reply_markup=camera_dist_keyboard(lang))
    await state.set_state(CreateForm.waiting_rand_other_dist)
    await _safe_answer(callback)


@router.callback_query(CreateForm.waiting_rand_other_dist, F.data.startswith("angle:"))
async def on_rand_other_dist(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    dist = callback.data.split(":")[1]
    await state.update_data(dist=dist)
    lang = await db.get_user_language(callback.from_user.id)

    await _replace_with_text(callback, "Введите высоту (см):", reply_markup=skip_step_keyboard("rand_height", lang))
    await state.set_state(CreateForm.waiting_rand_other_height)
    await _safe_answer(callback)


@router.message(CreateForm.waiting_rand_other_height)
@router.callback_query(F.data == "rand_height:skip")
async def on_rand_other_height(message_or_callback: Message | CallbackQuery, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(message_or_callback.from_user.id)
    if isinstance(message_or_callback, Message):
        text = (message_or_callback.text or "").strip()
        await state.update_data(height_cm=text)
    else:
        await state.update_data(height_cm="")

    msg_text = "Введите ширину (см):"
    markup = skip_step_keyboard("rand_width", lang)
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(msg_text, reply_markup=markup)
    else:
        await _replace_with_text(message_or_callback, msg_text, reply_markup=markup)
        await _safe_answer(message_or_callback)
    await state.set_state(CreateForm.waiting_rand_other_width)


@router.message(CreateForm.waiting_rand_other_width)
@router.callback_query(F.data == "rand_width:skip")
async def on_rand_other_width(message_or_callback: Message | CallbackQuery, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(message_or_callback.from_user.id)
    if isinstance(message_or_callback, Message):
        text = (message_or_callback.text or "").strip()
        await state.update_data(width_cm=text)
    else:
        await state.update_data(width_cm="")

    msg_text = "Введите длину (см):"
    markup = skip_step_keyboard("rand_length", lang)
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(msg_text, reply_markup=markup)
    else:
        await _replace_with_text(message_or_callback, msg_text, reply_markup=markup)
        await _safe_answer(message_or_callback)
    await state.set_state(CreateForm.waiting_rand_other_length)


@router.message(CreateForm.waiting_rand_other_length)
@router.callback_query(F.data == "rand_length:skip")
async def on_rand_other_length(message_or_callback: Message | CallbackQuery, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(message_or_callback.from_user.id)
    if isinstance(message_or_callback, Message):
        text = (message_or_callback.text or "").strip()
        await state.update_data(length_cm=text)
    else:
        await state.update_data(length_cm="")

    from bot.keyboards import random_season_keyboard
    msg_text = "Выберите сезон:"
    markup = random_season_keyboard(lang)
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(msg_text, reply_markup=markup)
    else:
        await _replace_with_text(message_or_callback, msg_text, reply_markup=markup)
        await _safe_answer(message_or_callback)
    await state.set_state(CreateForm.waiting_rand_other_season)


@router.callback_query(CreateForm.waiting_rand_other_season, F.data.startswith("rand_season:"))
async def on_rand_other_season(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    season = callback.data.split(":")[1]
    await state.update_data(season="" if season == "skip" else season)
    lang = await db.get_user_language(callback.from_user.id)

    from bot.keyboards import style_keyboard
    await _replace_with_text(callback, get_string("select_style", lang), reply_markup=style_keyboard(lang))
    await state.set_state(CreateForm.waiting_rand_other_style)
    await _safe_answer(callback)


@router.message(CreateForm.waiting_rand_other_season)
async def on_rand_other_season_message(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    await state.update_data(season=text)
    lang = await db.get_user_language(message.from_user.id)
    from bot.keyboards import style_keyboard
    await message.answer(get_string("select_style", lang), reply_markup=style_keyboard(lang))
    await state.set_state(CreateForm.waiting_rand_other_style)


@router.callback_query(CreateForm.waiting_rand_other_style, F.data.startswith("style:"))
async def on_rand_other_style(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    val = callback.data.split(":")[1]
    lang = await db.get_user_language(callback.from_user.id)
    if val == "custom":
        await _replace_with_text(callback, get_string("enter_custom_style", lang),
                                 reply_markup=back_step_keyboard(lang))
        await state.set_state(CreateForm.waiting_rand_other_style_custom)
    else:
        if val != "skip":
            await state.update_data(style=val)
        else:
            await state.update_data(style="")

        back_kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=get_string("back", lang), callback_data="back_step")]])
        await _replace_with_text(callback, get_string("upload_photo", lang), reply_markup=back_kb)
        await state.set_state(CreateForm.waiting_view)
    await _safe_answer(callback)


@router.message(CreateForm.waiting_rand_other_style_custom)
async def on_rand_other_style_custom(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    await state.update_data(style=text)
    lang = await db.get_user_language(message.from_user.id)

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=get_string("back", lang), callback_data="back_step")]])
    await message.answer(get_string("upload_photo", lang), reply_markup=back_kb)
    await state.set_state(CreateForm.waiting_view)


@router.callback_query(F.data == "create_cat:storefront")
async def on_storefront_category(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await db.get_maintenance():
        settings = load_settings()
        if callback.from_user.id in (settings.admin_ids or []):
            pass
        else:
            await _safe_answer(callback,
                               get_string("maintenance_alert", await db.get_user_language(callback.from_user.id)),
                               show_alert=True)
            return
    if not await db.get_category_enabled("storefront"):
        await _safe_answer(callback,
                           get_string("no_models_in_category_alert", await db.get_user_language(callback.from_user.id)),
                           show_alert=True)
        return
    await state.clear()
    await state.update_data(category="storefront", storefront_mode=True)
    lang = await db.get_user_language(callback.from_user.id)

    from bot.keyboards import gender_selection_keyboard
    await _replace_with_text(callback, get_string("select_gender", lang),
                             reply_markup=gender_selection_keyboard("storefront", lang))
    await _safe_answer(callback)


async def on_whitebg_category(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await db.get_maintenance():
        settings = load_settings()
        if callback.from_user.id not in (settings.admin_ids or []):
            await _safe_answer(callback,
                               get_string("maintenance_alert", await db.get_user_language(callback.from_user.id)),
                               show_alert=True)
            return
    if not await db.get_category_enabled("whitebg"):
        await _safe_answer(callback,
                           get_string("no_models_in_category_alert", await db.get_user_language(callback.from_user.id)),
                           show_alert=True)
        return
    await state.clear()
    await state.update_data(category="whitebg")
    lang = await db.get_user_language(callback.from_user.id)

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=get_string("back", lang), callback_data="menu_market")]])
    await _replace_with_text(callback, get_string("upload_photo", lang), reply_markup=back_kb)
    await state.set_state(CreateForm.waiting_view)
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("gender_select:"))
async def on_generic_gender_select(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    parts = callback.data.split(":")
    category = parts[1]
    gender = parts[2]

    await state.update_data(category=category, gender=gender, cloth="all")

    if gender in ("boy", "girl") or category == "child":
        await state.update_data(child_gender=gender)

    if category == "storefront":
        total_sf = await db.count_models("storefront", gender)
        if total_sf > 0:
            await _show_models_for_category(callback, db, "storefront", gender)
        else:

            await _safe_answer(
                callback,
                get_string("no_models_in_category_alert", await db.get_user_language(callback.from_user.id)),
                show_alert=True,
            )
            return
    else:
        await _show_models_for_category(callback, db, category, "all")
    await _safe_answer(callback)


@router.callback_query(F.data == "create_cat:infographics")
async def on_infographics_menu(callback: CallbackQuery, db: Database) -> None:
    enabled = await db.list_categories_enabled()
    lang = await db.get_user_language(callback.from_user.id)
    from bot.keyboards import infographic_selection_keyboard
    await _replace_with_text(callback, get_string("select_infographic_type", lang),
                             reply_markup=infographic_selection_keyboard(enabled, lang))
    await _safe_answer(callback)


async def on_infographic_category(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if await db.get_maintenance():
        settings = load_settings()
        if callback.from_user.id not in (settings.admin_ids or []):
            await _safe_answer(callback,
                               get_string("maintenance_alert", await db.get_user_language(callback.from_user.id)),
                               show_alert=True)
            return
    cat = callback.data.split(":")[1]

    if not await db.get_category_enabled(cat):
        await _safe_answer(callback,
                           get_string("no_models_in_category_alert", await db.get_user_language(callback.from_user.id)),
                           show_alert=True)
        return
    await state.clear()
    await state.update_data(category=cat, infographic_mode=True)
    lang = await db.get_user_language(callback.from_user.id)

    if cat == "infographic_clothing":
        await state.update_data(has_person=True)
        await _replace_with_text(callback, get_string("select_gender", lang),
                                 reply_markup=infographic_gender_keyboard(lang, back_data="create_cat:infographics"))
        await state.set_state(CreateForm.waiting_info_gender)
    else:

        await _replace_with_text(callback, "👤 Присутствует ли человек на фото?", reply_markup=yes_no_keyboard(lang))
        await state.set_state(CreateForm.waiting_info_has_person)
    await _safe_answer(callback)


@router.callback_query(CreateForm.waiting_info_has_person, F.data.startswith("choice:"))
async def on_info_has_person(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    choice = callback.data.split(":")[1]
    has_person = (choice == "yes")
    await state.update_data(has_person=has_person)
    lang = await db.get_user_language(callback.from_user.id)

    if has_person:
        from bot.keyboards import infographic_gender_keyboard
        await _replace_with_text(callback, get_string("select_gender", lang),
                                 reply_markup=infographic_gender_keyboard(lang))
        await state.set_state(CreateForm.waiting_info_gender)
    else:

        await _replace_with_text(callback, get_string("enter_info_load", lang),
                                 reply_markup=skip_step_keyboard("info_load", lang))
        await state.set_state(CreateForm.waiting_info_load)
    await _safe_answer(callback)


@router.callback_query(CreateForm.waiting_info_gender, F.data.startswith("info_gender:"))
async def on_infographic_gender(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    g = callback.data.split(":")[1]
    await state.update_data(info_gender=g)
    lang = await db.get_user_language(callback.from_user.id)

    await _replace_with_text(callback, "🔢 Введите возраст модели числом:", reply_markup=back_step_keyboard(lang))
    await state.set_state(CreateForm.waiting_info_age)
    await _safe_answer(callback)


@router.message(CreateForm.waiting_info_age)
async def on_info_age(message: Message, state: FSMContext, db: Database) -> None:
    age_text = (message.text or "").strip()
    await state.update_data(age=age_text)
    lang = await db.get_user_language(message.from_user.id)

    await message.answer(get_string("enter_info_load", lang), reply_markup=skip_step_keyboard("info_load", lang))
    await state.set_state(CreateForm.waiting_info_load)


@router.callback_query(F.data.startswith("info_style:"))
async def on_infographic_style(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    val = callback.data.split(":")[1]
    await state.update_data(info_style=val)
    lang = await db.get_user_language(callback.from_user.id)

    await _replace_with_text(callback, get_string("enter_info_load", lang),
                             reply_markup=skip_step_keyboard("info_load", lang))
    await state.set_state(CreateForm.waiting_info_load)
    await _safe_answer(callback)


@router.message(CreateForm.waiting_info_load)
async def on_infographic_load_input(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    lang = await db.get_user_language(message.from_user.id)
    data = await state.get_data()

    load_value = ""
    if text.lower() not in ("пропустить", "skip"):
        digits = ''.join(ch for ch in text if ch.isdigit())
        if not digits or not (1 <= int(digits) <= 10):
            await message.answer(get_string("enter_info_load_error", lang))
            return
        load_value = digits

    await state.update_data(info_load=load_value)

    if data.get("random_other_mode"):

        from bot.keyboards import skip_step_keyboard
        await message.answer(get_string("enter_product_name", lang), reply_markup=skip_step_keyboard("rand_name", lang))
        await state.set_state(CreateForm.waiting_rand_other_name)
    else:
        from bot.keyboards import info_lang_keyboard
        await message.answer(get_string("select_info_lang", lang), reply_markup=info_lang_keyboard(lang))
        await state.set_state(CreateForm.waiting_info_lang)


@router.callback_query(F.data == "info_load:skip")
async def on_infographic_load_skip_btn(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.update_data(info_load="")
    lang = await db.get_user_language(callback.from_user.id)
    data = await state.get_data()

    if data.get("random_other_mode"):

        await _replace_with_text(callback, get_string("enter_product_name", lang),
                                 reply_markup=back_step_keyboard(lang))
        await state.set_state(CreateForm.waiting_rand_other_name)
    else:

        from bot.keyboards import info_lang_keyboard
        await _replace_with_text(callback, get_string("select_info_lang", lang), reply_markup=info_lang_keyboard(lang))
        await state.set_state(CreateForm.waiting_info_lang)
    await _safe_answer(callback)


@router.callback_query(CreateForm.waiting_info_lang, F.data.startswith("info_lang:"))
async def on_infographic_lang(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    val = callback.data.split(":")[1]
    lang = await db.get_user_language(callback.from_user.id)

    if val == "custom":
        await _replace_with_text(callback, get_string("enter_info_lang_custom", lang),
                                 reply_markup=back_step_keyboard(lang))
        await state.set_state(CreateForm.waiting_info_lang_custom)
        await _safe_answer(callback)
        return

    await state.update_data(info_lang="" if val == "skip" else val)

    await _replace_with_text(callback, get_string("enter_info_brand", lang), reply_markup=back_step_keyboard(lang))
    await state.set_state(CreateForm.waiting_info_brand)
    await _safe_answer(callback)


@router.message(CreateForm.waiting_info_lang_custom)
async def on_infographic_lang_custom(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    lang = await db.get_user_language(message.from_user.id)
    if not text:
        await message.answer(get_string("enter_lang_error", lang))
        return
    await state.update_data(info_lang=text)
    await message.answer(get_string("enter_info_brand", lang), reply_markup=back_step_keyboard(lang))
    await state.set_state(CreateForm.waiting_info_brand)


@router.message(CreateForm.waiting_info_brand)
async def on_infographic_brand(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    lang = await db.get_user_language(message.from_user.id)
    if not text:
        await message.answer(get_string("enter_info_brand_error", lang))
        return
    if len(text) > 50:
        await message.answer("⚠️ Название слишком длинное (максимум 50 символов). Попробуйте еще раз:")
        return
    await state.update_data(info_brand=text)

    await message.answer(get_string("enter_adv1_skip", lang), reply_markup=skip_step_keyboard("info_adv1", lang))
    await state.set_state(CreateForm.waiting_info_adv1)


@router.message(CreateForm.waiting_info_adv1)
async def on_infographic_adv1(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    lang = await db.get_user_language(message.from_user.id)
    if len(text) > 100:
        await message.answer("⚠️ Текст слишком длинный (максимум 100 символов). Попробуйте еще раз:")
        return
    await state.update_data(info_adv1=text)
    await message.answer(get_string("enter_adv2_skip", lang), reply_markup=skip_step_keyboard("info_adv2", lang))
    await state.set_state(CreateForm.waiting_info_adv2)


@router.message(CreateForm.waiting_info_adv2)
async def on_infographic_adv2(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    lang = await db.get_user_language(message.from_user.id)
    if len(text) > 100:
        await message.answer("⚠️ Текст слишком длинный (максимум 100 символов). Попробуйте еще раз:")
        return
    await state.update_data(info_adv2=text)
    await message.answer(get_string("enter_adv3_skip", lang), reply_markup=skip_step_keyboard("info_adv3", lang))
    await state.set_state(CreateForm.waiting_info_adv3)


@router.message(CreateForm.waiting_info_adv3)
async def on_infographic_adv3(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    lang = await db.get_user_language(message.from_user.id)
    if len(text) > 100:
        await message.answer("⚠️ Текст слишком длинный (максимум 100 символов). Попробуйте еще раз:")
        return
    await state.update_data(info_adv3=text)
    await message.answer(get_string("enter_extra_info_skip", lang), reply_markup=skip_step_keyboard("info_extra", lang))
    await state.set_state(CreateForm.waiting_info_extra)


@router.callback_query(F.data == "info_adv1:skip")
async def on_infographic_adv1_skip(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    await state.update_data(info_adv1="")
    await _replace_with_text(callback, get_string("enter_adv2_skip", lang),
                             reply_markup=skip_step_keyboard("info_adv2", lang))
    await state.set_state(CreateForm.waiting_info_adv2)
    await _safe_answer(callback)


@router.callback_query(F.data == "info_adv2:skip")
async def on_infographic_adv2_skip(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    await state.update_data(info_adv2="")
    await _replace_with_text(callback, get_string("enter_adv3_skip", lang),
                             reply_markup=skip_step_keyboard("info_adv3", lang))
    await state.set_state(CreateForm.waiting_info_adv3)
    await _safe_answer(callback)


@router.callback_query(F.data == "info_adv3:skip")
async def on_infographic_adv3_skip(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    await state.update_data(info_adv3="")
    await _replace_with_text(callback, get_string("enter_extra_info_skip", lang),
                             reply_markup=skip_step_keyboard("info_extra", lang))
    await state.set_state(CreateForm.waiting_info_extra)
    await _safe_answer(callback)


@router.message(CreateForm.waiting_info_extra)
async def on_infographic_extra(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    lang = await db.get_user_language(message.from_user.id)
    if len(text) > 65:
        await message.answer("⚠️ Текст слишком длинный (максимум 65 символов). Попробуйте еще раз:")
        return
    await state.update_data(info_extra=text)

    data = await state.get_data()
    if data.get("category") == "infographic_other":

        from bot.keyboards import form_view_keyboard
        await message.answer("Выберите угол камеры (Спереди/Сзади):", reply_markup=form_view_keyboard(lang))
        await state.set_state(CreateForm.waiting_info_angle)
    else:

        await message.answer("📏 Введите размер одежды или телосложение числом:", reply_markup=back_step_keyboard(lang))
        await state.set_state(CreateForm.waiting_dynamic_step)
        await state.update_data(current_step_key="size")


@router.callback_query(F.data == "info_extra:skip")
async def on_infographic_extra_skip(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    await state.update_data(info_extra="")

    data = await state.get_data()
    if data.get("category") == "infographic_other":
        from bot.keyboards import form_view_keyboard
        await _replace_with_text(callback, "Выберите угол камеры (Спереди/Сзади):",
                                 reply_markup=form_view_keyboard(lang))
        await state.set_state(CreateForm.waiting_info_angle)
    else:

        await _replace_with_text(callback, "📏 Введите размер одежды или телосложение числом:",
                                 reply_markup=back_step_keyboard(lang))
        await state.set_state(CreateForm.waiting_dynamic_step)
        await state.update_data(current_step_key="size")
    await _safe_answer(callback)


@router.callback_query(CreateForm.waiting_has_person, F.data.startswith("choice:"))
async def on_has_person_selected(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    val = callback.data.split(":")[1]
    has_person = (val == "yes")
    await state.update_data(has_person=has_person)

    lang = await db.get_user_language(callback.from_user.id)

    await _replace_with_text(callback, "Где будет находиться товар?", reply_markup=random_loc_group_keyboard(lang))

    await _safe_answer(callback)


async def on_create_own(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if await db.get_maintenance():
        settings = load_settings()
        if callback.from_user.id not in (settings.admin_ids or []):
            await _safe_answer(callback, "Идут техработы. Пожалуйста, попробуйте позже.", show_alert=True)
            return

    if not await db.get_category_enabled("own"):
        await _safe_answer(callback, "Категория временно недоступна", show_alert=True)
        return
    await state.clear()
    await state.update_data(own_mode=True, category="own")

    await _ask_garment_length(callback, state, db)
    await _safe_answer(callback)


async def on_create_own_variant(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if await db.get_maintenance():
        settings = load_settings()
        if callback.from_user.id not in (settings.admin_ids or []):
            await _safe_answer(callback,
                               get_string("maintenance_alert", await db.get_user_language(callback.from_user.id)),
                               show_alert=True)
            return
    if not await db.get_category_enabled("own_variant"):
        await _safe_answer(callback,
                           get_string("no_models_in_category_alert", await db.get_user_language(callback.from_user.id)),
                           show_alert=True)
        return
    await state.clear()
    await state.update_data(category="own_variant")
    lang = await db.get_user_language(callback.from_user.id)
    await _replace_with_text(callback, get_string("upload_background", lang), reply_markup=back_step_keyboard(lang))
    await state.set_state(CreateForm.waiting_own_bg_photo)
    await _safe_answer(callback)


async def on_collage_category(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await db.get_maintenance():
        settings = load_settings()
        if callback.from_user.id not in (settings.admin_ids or []):
            await _safe_answer(callback,
                               get_string("maintenance_alert", await db.get_user_language(callback.from_user.id)),
                               show_alert=True)
            return
    if not await db.get_category_enabled("collage"):
        await _safe_answer(callback,
                           get_string("no_models_in_category_alert", await db.get_user_language(callback.from_user.id)),
                           show_alert=True)
        return
    await state.clear()
    await state.update_data(category="collage", collage_mode=True)
    lang = await db.get_user_language(callback.from_user.id)
    await _replace_with_text(callback, get_string("collage_step1_model", lang), reply_markup=back_step_keyboard(lang))
    await state.set_state(CreateForm.waiting_collage_model_photo)
    await _safe_answer(callback)


@router.message(CreateForm.waiting_collage_model_photo, F.photo)
async def on_collage_model_photo(message: Message, state: FSMContext, db: Database) -> None:
    photo_id = message.photo[-1].file_id
    await state.update_data(collage_model_photo=photo_id)
    lang = await db.get_user_language(message.from_user.id)
    await message.answer(get_string("collage_step2_front", lang), reply_markup=back_step_keyboard(lang))
    await state.set_state(CreateForm.waiting_collage_front_photo)


@router.message(CreateForm.waiting_collage_front_photo, F.photo)
async def on_collage_front_photo(message: Message, state: FSMContext, db: Database) -> None:
    photo_id = message.photo[-1].file_id
    await state.update_data(collage_front_photo=photo_id)
    lang = await db.get_user_language(message.from_user.id)
    await message.answer(get_string("collage_step3_back", lang), reply_markup=back_step_keyboard(lang))
    await state.set_state(CreateForm.waiting_collage_back_photo)


@router.message(CreateForm.waiting_collage_back_photo, F.photo)
async def on_collage_back_photo(message: Message, state: FSMContext, db: Database) -> None:
    photo_id = message.photo[-1].file_id
    await state.update_data(collage_back_photo=photo_id)
    await _ask_quality(message, state, db)


@router.message(CreateForm.waiting_own_bg_photo, F.photo)
async def on_own_bg_photo(message: Message, state: FSMContext, db: Database) -> None:
    photo_id = message.photo[-1].file_id
    await state.update_data(own_bg_photo_id=photo_id)
    lang = await db.get_user_language(message.from_user.id)
    await message.answer(get_string("upload_product", lang), reply_markup=back_step_keyboard(lang))
    await state.set_state(CreateForm.waiting_own_product_photo)


@router.message(CreateForm.waiting_own_product_photo, F.photo)
async def on_own_variant_product_photo(message: Message, state: FSMContext, db: Database) -> None:
    photo_id = message.photo[-1].file_id
    await state.update_data(own_product_photo_id=photo_id)
    data = await state.get_data()
    if data.get("own_mode"):
        quality = data.get("selected_quality")
        if not quality:
            await _ask_quality(message, state, db)
        else:
            await _show_confirmation(message, state, db)
        return
    await _ask_sleeve_length(message, state, db)


@router.message(CreateForm.waiting_prompt, F.text)
async def on_prompt_input(message: Message, state: FSMContext, db: Database) -> None:
    prompt = (message.text or "").strip()
    lang = await db.get_user_language(message.from_user.id)
    if len(prompt) > 2500:
        await message.answer(get_string("enter_prompt_error", lang), reply_markup=back_step_keyboard(lang))
        return

    await state.update_data(prompt=prompt)
    data = await state.get_data()

    await message.answer(get_string("select_format", lang), reply_markup=aspect_ratio_keyboard(lang))
    await state.set_state(CreateForm.waiting_aspect)


@router.callback_query(F.data == "normal_photos_done")
async def on_normal_photos_done(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    lang = await db.get_user_language(callback.from_user.id)
    photos = data.get("photos") or []
    if not photos:
        await _safe_answer(callback)
        await _replace_with_text(callback, "📸 Пришлите хотя бы одно фото.", reply_markup=back_step_keyboard(lang))
        return
    await _replace_with_text(callback, "✍️ Теперь отправьте промпт (до 2500 символов).",
                             reply_markup=back_step_keyboard(lang))
    await state.set_state(CreateForm.waiting_prompt)
    await _safe_answer(callback)


@router.callback_query(CreateForm.waiting_aspect, F.data.startswith("form_aspect:"))
async def on_aspect_selected(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    aspect = callback.data.split(":", 1)[1]

    aspect = aspect.replace('x', ':')
    await state.update_data(aspect=aspect)
    data = await state.get_data()

    if data.get("normal_gen_mode"):
        quality = data.get("selected_quality")
        if not quality:
            await _ask_quality(callback, state, db)
        else:
            await _show_confirmation(callback, state, db)
    else:
        await _show_next_step(callback, state, db)
    await _safe_answer(callback)


@router.message(CreateForm.waiting_ref_photo, F.photo)
async def on_own_ref_photo(message: Message, state: FSMContext, db: Database) -> None:
    ref_id = message.photo[-1].file_id
    await state.update_data(own_ref_photo_id=ref_id)

    await _ask_garment_length(message, state, db)


@router.message(CreateForm.waiting_product_photo, F.photo)
async def on_own_model_product_photo(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    if data.get("repeat_mode"):
        await state.update_data(repeat_mode=False)
        from bot.keyboards import aspect_ratio_keyboard
        lang = await db.get_user_language(message.from_user.id)
        await message.answer(get_string("select_format", lang), reply_markup=aspect_ratio_keyboard(lang))
        await state.set_state(CreateForm.waiting_aspect)
        return

    prod_id = message.photo[-1].file_id
    await state.update_data(own_product_photo_id=prod_id)

    if data.get("own_mode"):
        await _ask_sleeve_length(message, state, db)
    else:

        lang = await db.get_user_language(message.from_user.id)
        from bot.keyboards import aspect_ratio_keyboard
        await message.answer(get_string("select_format", lang), reply_markup=aspect_ratio_keyboard(lang))
        await state.set_state(CreateForm.waiting_aspect)


@router.callback_query(F.data.startswith("own_view:"))
async def on_own_view(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    view = callback.data.split(":", 1)[1]
    await state.update_data(own_view=view)

    await _ask_garment_length(callback, state, db)
    await _safe_answer(callback)


@router.callback_query(CreateForm.waiting_own_size, F.data.startswith("form_size:"))
async def on_own_size(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    val = callback.data.split(":", 1)[1]
    size_map = {"thin": "Худая", "curvy": "Пышная", "plus": "Очень пышная"}
    current = await state.get_data()
    if current.get("own_mode"):
        await state.update_data(own_size=size_map.get(val, ""))
        await _ask_garment_length(callback, state, db)
        await _safe_answer(callback)
        return
    await _safe_answer(callback)


@router.message(CreateForm.waiting_own_length)
async def on_own_length(message: Message, state: FSMContext, db: Database) -> None:
    length_text = (message.text or "").strip()
    lang = await db.get_user_language(message.from_user.id)
    if not length_text:
        await message.answer("Длина не может быть пустой. Укажите числом (см) или словами.")
        return
    await state.update_data(own_length=length_text)
    await state.set_state(CreateForm.waiting_own_sleeve)
    await message.answer(get_string("select_sleeve_length", lang), reply_markup=sleeve_length_keyboard(lang))


@router.callback_query(F.data.startswith("cut_type:"))
async def on_own_cut(callback: CallbackQuery, state: FSMContext) -> None:
    val = callback.data.split(":", 1)[1]
    cut_map = {
        "fitted": "Приталенный",
        "straight": "Прямой",
        "oversize": "Оверсайз",
        "a_line": "А-силуэт",
        "skip": "",
    }
    await state.update_data(own_cut=cut_map.get(val, ""))

    data = await state.get_data()
    size = data.get("own_size") or "—"
    length = data.get("own_length") or "—"
    sleeve = data.get("own_sleeve") or "—"
    cut = data.get("own_cut") or "—"
    view = "Спереди" if (data.get("own_view") == "front") else "Сзади"
    preview = (
        "📋 Проверьте параметры:\n\n"
        f"👀 Вид: {view}\n"
        f"📐 Телосложение: {size}\n"
        f"📏 Длина изделия: {length}\n"
        f"🧥 Длина рукава: {sleeve}\n"
        f"✂️ Тип кроя: {cut}\n"
    )
    await _replace_with_text(callback, preview, reply_markup=confirm_generation_keyboard())
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("rand_locgroup:"))
async def on_random_locgroup(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    group = callback.data.split(":", 1)[1]
    await state.update_data(rand_loc_group=group)
    lang = await db.get_user_language(callback.from_user.id)
    from bot.keyboards import random_location_keyboard
    await _replace_with_text(callback, get_string("select_location", lang),
                             reply_markup=random_location_keyboard(group, lang))
    await state.set_state(CreateForm.waiting_rand_loc)
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("rand_location:"))
async def on_random_location(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    loc = callback.data.split(":", 1)[1]
    await state.update_data(rand_location=loc)
    lang = await db.get_user_language(callback.from_user.id)
    from bot.keyboards import random_vibe_keyboard
    await _replace_with_text(callback, get_string("select_vibe", lang), reply_markup=random_vibe_keyboard(lang))
    await state.set_state(CreateForm.waiting_rand_vibe)
    await _safe_answer(callback)


@router.callback_query(F.data == "rand_location_custom")
async def on_random_location_custom(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    await state.set_state(CreateForm.waiting_custom_location)
    await _replace_with_text(callback, get_string("enter_custom_loc", lang))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("rand_vibe:"))
async def on_random_vibe(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    vibe = callback.data.split(":", 1)[1]
    await state.update_data(rand_vibe=vibe)
    data = await state.get_data()
    lang = await db.get_user_language(callback.from_user.id)
    if data.get("rand_location") == "photo_studio":
        from bot.keyboards import random_decor_keyboard
        await _replace_with_text(callback, "Декор фотостудии:", reply_markup=random_decor_keyboard(lang))
    elif data.get("random_other_mode"):
        from bot.keyboards import aspect_ratio_keyboard
        await _replace_with_text(callback, get_string("select_format", lang), reply_markup=aspect_ratio_keyboard(lang))
        await state.set_state(CreateForm.waiting_aspect)
    else:
        from bot.keyboards import random_shot_keyboard
        await _replace_with_text(callback, get_string("select_view", lang), reply_markup=random_shot_keyboard(lang))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("rand_decor:"))
async def on_random_decor(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    decor = callback.data.split(":", 1)[1]
    await state.update_data(rand_decor=decor)
    data = await state.get_data()
    if data.get("random_other_mode"):
        lang = await db.get_user_language(callback.from_user.id)
        await _replace_with_text(callback, get_string("select_format", lang), reply_markup=aspect_ratio_keyboard(lang))
        await state.set_state(CreateForm.waiting_aspect)
    else:
        await _replace_with_text(callback, "Выберите ракурс:", reply_markup=random_shot_keyboard())
    await _safe_answer(callback)


def _unused_random_age_input():
    return None


@router.callback_query(F.data.startswith("rand_shot:"))
async def on_random_shot(callback: CallbackQuery, state: FSMContext) -> None:
    shot = callback.data.split(":", 1)[1]
    await state.update_data(rand_shot=shot)
    lang = await db.get_user_language(callback.from_user.id)
    await _replace_with_text(callback, "🎂 Введите возраст модели числом:", reply_markup=back_step_keyboard(lang))
    await state.set_state(CreateForm.waiting_dynamic_step)
    await state.update_data(current_step_key="age")
    await _safe_answer(callback)


@router.callback_query(F.data == "female_mode:model_bg")
async def on_female_mode_model_bg(callback: CallbackQuery) -> None:
    text = "👕 Выберите тип одежды:"
    if callback.message and callback.message.text == text:
        await _safe_answer(callback)
        return
    await _replace_with_text(callback, text, reply_markup=female_clothes_keyboard())
    await _safe_answer(callback)


@router.callback_query(F.data == "female_mode:plus")
async def on_female_mode_plus(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(plus_mode=True)
    text = "🚻 Выберите пол для большого размера:"
    if callback.message and callback.message.text == text:
        await _safe_answer(callback)
        return
    await _replace_with_text(callback, text, reply_markup=plus_gender_keyboard())
    await _safe_answer(callback)


def _cloth_from_callback(data: str) -> tuple[str, str] | None:
    try:
        prefix, cloth = data.split(":", 1)
        category = prefix.split("_", 1)[0]
        return category, cloth
    except Exception:
        return None


def _model_header(index: int, total: int = 31) -> str:
    i = max(1, min(total, index + 1))
    return f"👤 Модель {i} из {total}\n\n" \
           "⚠️ Примерный вид модели и фона.\n" \
           "Может быть изменен в последующем.\n\n" \
           "Используйте кнопки ⬅️ ➡️ для просмотра вариантов\n" \
           "или нажмите ✅ для выбора этой модели."


@router.callback_query(
    F.data.startswith("female_cloth:") | F.data.startswith("male_cloth:") | F.data.startswith("child_cloth:"))
async def on_any_cloth(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    parsed = _cloth_from_callback(callback.data)
    if not parsed:
        await _safe_answer(callback)
        return
    category, cloth = parsed

    await state.update_data(selected_cloth=cloth)
    total = await db.count_models(category, cloth)
    if total <= 0:
        await _safe_answer(callback, "Модели не найдены", show_alert=True)
        return
    text = _model_header(0, total)
    model = await db.get_model_by_index(category, cloth, 0)
    lang = await db.get_user_language(callback.from_user.id)
    if model and model[3]:
        res_msg = await _answer_model_photo(
            callback,
            model[3],
            text,
            model_select_keyboard(category, cloth, 0, total, lang),
        )
        if res_msg and res_msg.photo and not model[3].startswith("AgAC"):
            await db.set_model_photo(model[0], res_msg.photo[-1].file_id)
    else:
        await _replace_with_text(callback, text, reply_markup=model_select_keyboard(category, cloth, 0, total, lang))
    await _safe_answer(callback)


@router.message(CreateForm.random_dummy)
async def on_random_age_input(message: Message, state: FSMContext) -> None:
    txt = (message.text or "").strip()
    if txt.lower() in ("пропустить", "skip"):
        await state.update_data(age="")
    else:
        digits = ''.join(ch for ch in txt if ch.isdigit())
        if not digits:
            await message.answer("Введите возраст числом, например: 25 или нажмите 'Пропустить'")
            return
        await state.update_data(age=f"{digits} лет")
    await state.set_state(CreateForm.waiting_height)
    await message.answer("📏 Введите рост модели в см (например: 170):")


@router.callback_query(F.data == "rand_age:skip")
async def on_random_age_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(age="")
    await _replace_with_text(callback, "📏 Введите рост модели в см (например: 170):")
    await state.set_state(CreateForm.waiting_height)
    await _safe_answer(callback)


@router.message(CreateForm.waiting_custom_location)
async def on_random_location_custom_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст пуст. Введите локацию (до 100 символов):")
        return
    if len(text) > 100:
        await message.answer("Слишком длинно. Сократите до 100 символов.")
        return
    await state.update_data(rand_location="custom", rand_location_custom=text)
    await message.answer("Выберите вайб:", reply_markup=random_vibe_keyboard())


@router.callback_query(F.data.startswith("model_pick:"))
async def on_model_pick(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    try:
        parts = callback.data.split(":")

        category = parts[1]
        display_cat = parts[2]
        cloth = parts[3]
        index = int(parts[4])
    except Exception:
        await _safe_answer(callback)
        return

    total = await db.count_models(display_cat, cloth)
    if total <= 0:
        await _safe_answer(callback, "Модели отсутствуют", show_alert=True)
        return

    model = await db.get_model_by_index(display_cat, cloth, index)
    if not model:
        await _safe_answer(callback, "Модель не найдена", show_alert=True)
        return

    model_id, name, prompt_id, _photo = model

    await state.update_data(
        category=category,
        display_category=display_cat,
        cloth=cloth,
        index=index,
        model_id=model_id,
        prompt_id=prompt_id
    )

    await state.update_data(model_select=model_id, model_select_label=name)

    lang = await db.get_user_language(callback.from_user.id)
    data = await state.get_data()

    if category == "storefront" or data.get("storefront_mode"):
        await state.update_data(current_step_index=0)
        await _show_next_step(callback, state, db)
        await _safe_answer(callback)
        return

    if category == "presets" or data.get("is_preset"):
        model_photo = _photo or model[3]
        await state.update_data(own_ref_photo_id=model_photo)

        await state.update_data(user_photo_id=None, photo=None, photos=[])

        await state.update_data(current_step_index=0)
        await _show_next_step(callback, state, db)
        await _safe_answer(callback)
        return

    if category in ("female", "male"):
        await _replace_with_text(callback, "🎂 Введите возраст модели числом:", reply_markup=back_step_keyboard(lang))
        await state.set_state(CreateForm.waiting_dynamic_step)
        await state.update_data(current_step_key="age")
    else:

        await _replace_with_text(callback, "📏 Введите размер одежды или телосложение числом:",
                                 reply_markup=back_step_keyboard(lang))
        await state.set_state(CreateForm.waiting_dynamic_step)
        await state.update_data(current_step_key="size")

    await _safe_answer(callback)


@router.callback_query(CreateForm.waiting_pants_style, F.data.startswith("pants_style:"))
@router.callback_query(CreateForm.waiting_own_cut, F.data.startswith("pants_style:"))
async def on_pants_style(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    style = callback.data.split(":", 1)[1]
    data = await state.get_data()
    lang = await db.get_user_language(callback.from_user.id)

    if data.get("own_mode"):
        cut_map = {
            "fitted": "Приталенный",
            "straight": "Прямой",
            "oversize": "Оверсайз",
            "a_line": "А-силуэт",
            "skip": "",
        }
        await state.update_data(own_cut=cut_map.get(style, ""))
        await state.set_state(CreateForm.waiting_own_sleeve)
        await _replace_with_text(callback, get_string("select_sleeve_length", lang),
                                 reply_markup=sleeve_length_keyboard(lang))
        await _safe_answer(callback)
        return

    await state.update_data(pants_style=style)
    category = data.get("category")

    if data.get("infographic_mode") and category == "infographic_clothing":
        await _replace_with_text(callback, get_string("select_sleeve_length", lang),
                                 reply_markup=sleeve_length_keyboard(lang))
        await state.set_state(CreateForm.waiting_sleeve)
        await _safe_answer(callback)
        return

    if category in ("female", "male", "child") and not data.get("random_mode") and not data.get("infographic_mode"):
        await state.set_state(CreateForm.waiting_sleeve)
        await _replace_with_text(callback, get_string("select_sleeve_length", lang),
                                 reply_markup=sleeve_length_keyboard(lang))
        await _safe_answer(callback)
        return

    if data.get("infographic_mode"):
        await state.set_state(CreateForm.waiting_sleeve)
        await _replace_with_text(callback, "Выберите тип рукава (или пропустите):",
                                 reply_markup=sleeve_length_keyboard(lang))
        return

    if data.get("random_mode"):

        await state.set_state(CreateForm.waiting_sleeve)
        await _replace_with_text(callback, get_string("select_sleeve_length", lang),
                                 reply_markup=sleeve_length_keyboard(lang))
    else:

        if data.get("plus_mode"):
            await _replace_with_text(callback, "Выберите локацию:", reply_markup=plus_location_keyboard())
            await state.set_state(CreateForm.plus_loc)
        else:
            await _replace_with_text(callback, "📏 Введите размер одежды или телосложение числом:",
                                     reply_markup=back_step_keyboard(lang))
            await state.set_state(CreateForm.waiting_dynamic_step)
            await state.update_data(current_step_key="size")

    await _safe_answer(callback)


@router.callback_query(F.data.startswith("plus_loc:"))
async def on_plus_loc(callback: CallbackQuery, state: FSMContext) -> None:
    loc = callback.data.split(":", 1)[1]
    await state.update_data(plus_loc=loc)
    await _replace_with_text(callback, "Выберите время года:", reply_markup=plus_season_keyboard())
    await state.set_state(CreateForm.plus_season)
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("plus_season:"))
async def on_plus_season(callback: CallbackQuery, state: FSMContext) -> None:
    season = callback.data.split(":", 1)[1]
    await state.update_data(plus_season=season)
    await _replace_with_text(callback, "Выберите вайб:", reply_markup=plus_vibe_keyboard())
    await state.set_state(CreateForm.plus_vibe)
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("plus_vibe:"))
async def on_plus_vibe(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    vibe = callback.data.split(":", 1)[1]
    await state.update_data(plus_vibe=vibe)

    data = await state.get_data()
    cat_key = data.get("category")
    category_db = await db.get_category_by_key(cat_key)
    if category_db:
        await state.update_data(current_step_index=0)
        await _show_next_step(callback, state, db)
    else:

        lang = await db.get_user_language(callback.from_user.id)
        await _replace_with_text(callback, "🎂 Введите возраст модели числом:", reply_markup=back_step_keyboard(lang))
        await state.set_state(CreateForm.waiting_dynamic_step)
        await state.update_data(current_step_key="age")

    await _safe_answer(callback)


@router.callback_query(F.data.startswith("garment_len:"))
async def on_garment_len_callback(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    val = callback.data.split(":", 1)[1]
    data = await state.get_data()
    lang = await db.get_user_language(callback.from_user.id)
    category = data.get("category")

    if val == "custom":
        await _replace_with_text(callback, get_string("enter_length_custom", lang),
                                 reply_markup=back_step_keyboard(lang))
        await state.set_state(CreateForm.waiting_length)
        await _safe_answer(callback)
        return

    len_map = {
        "short_top": "Короткий топ", "regular_top": "Обычный топ",
        "to_waist": "До талии", "below_waist": "Ниже талии",
        "mid_buttocks": "До середины ягодиц", "mid_thigh": "До середины бедра",
        "to_knees": "До колен", "below_knees": "Ниже колен",
        "midi": "Миди", "to_ankles": "До щиколоток",
        "to_floor": "До пола",
        "skip": ""
    }

    length_text = len_map.get(val, "")
    await state.update_data(length=length_text)

    if data.get("own_mode") or category == "own_variant" or category == "storefront" or data.get("infographic_mode"):
        await state.update_data(own_length=length_text)

        if data.get("own_mode"):
            await _ask_sleeve_length(callback, state, db)
            await _safe_answer(callback)
            return

        if category == "own_variant":
            from bot.keyboards import aspect_ratio_keyboard
            await _replace_with_text(callback, get_string("select_format", lang),
                                     reply_markup=aspect_ratio_keyboard(lang))
            await state.set_state(CreateForm.waiting_aspect)
            await _safe_answer(callback)
            return

        if category == "infographic_clothing":
            from bot.keyboards import aspect_ratio_keyboard
            await _replace_with_text(callback, get_string("select_format", lang),
                                     reply_markup=aspect_ratio_keyboard(lang))
            await state.set_state(CreateForm.waiting_aspect)
            await _safe_answer(callback)
            return

        back_kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=get_string("back", lang), callback_data="back_step")]])
        await _replace_with_text(callback, get_string("upload_photo", lang), reply_markup=back_kb)
        await state.set_state(CreateForm.waiting_view)
        await _safe_answer(callback)
        return

    if (category in ("female", "male", "child") or data.get("random_mode")) and not data.get("infographic_mode"):
        await state.set_state(CreateForm.waiting_preset_pose)
        gender = data.get("gender") or data.get("rand_gender") or data.get("info_gender") or data.get("child_gender")
        show_vulgar = (gender not in ("boy", "girl"))
        await _replace_with_text(callback, "Выберите тип позы:",
                                 reply_markup=pose_keyboard(lang, show_vulgar=show_vulgar))
        await _safe_answer(callback)
        return

    cloth = data.get("cloth")
    plus_mode = bool(data.get("plus_mode"))

    if data.get("random_mode") or cloth == "dress" or (
            plus_mode and cloth in ("top", "coat", "suit", "overall", "loungewear")):
        await state.set_state(CreateForm.waiting_sleeve)
        await _replace_with_text(callback, get_string("select_sleeve_length", lang),
                                 reply_markup=sleeve_length_keyboard(lang))
    elif plus_mode and cloth == "pants":
        await state.set_state(CreateForm.waiting_pants_style)
        await _replace_with_text(callback, "Тип кроя штанов (опционально):", reply_markup=pants_style_keyboard(lang))
    else:
        await state.set_state(CreateForm.waiting_view)
        await _replace_with_text(callback, get_string("select_camera_dist", lang),
                                 reply_markup=form_view_keyboard(lang))

    await _safe_answer(callback)


@router.message(CreateForm.waiting_length)
async def form_set_length(message: Message, state: FSMContext, db: Database) -> None:
    length = (message.text or "").strip()
    await state.update_data(length=length)
    data = await state.get_data()
    lang = await db.get_user_language(message.from_user.id)

    if data.get("own_mode") or data.get("category") == "own_variant" or data.get("infographic_mode"):
        if data.get("infographic_mode"):
            await state.set_state(CreateForm.waiting_aspect)
            await message.answer(get_string("select_format", lang), reply_markup=aspect_ratio_keyboard(lang))
            return

        await state.update_data(own_length=length)
        await state.set_state(CreateForm.waiting_aspect)
        await message.answer(get_string("select_format", lang), reply_markup=aspect_ratio_keyboard(lang))
        return


@router.message(CreateForm.waiting_foot)
async def form_set_foot(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.lower() in ("пропустить", "skip"):
        await state.update_data(foot_size="")
    else:
        digits = ''.join(ch for ch in text if (ch.isdigit()))
        if not digits:
            await message.answer("Введите размер ноги числом, например: 31 или отправьте 'Пропустить'")
            return
        await state.update_data(foot_size=digits)
    lang = await db.get_user_language(message.from_user.id)
    await state.set_state(CreateForm.waiting_view)
    await message.answer(get_string("select_camera_dist", lang), reply_markup=form_view_keyboard(lang))


@router.callback_query(CreateForm.waiting_sleeve, F.data.startswith("form_sleeve:"))
async def form_set_sleeve(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    val = callback.data.split(":", 1)[1]
    data = await state.get_data()
    lang = await db.get_user_language(callback.from_user.id)
    category = data.get("category")

    sleeve_map = {
        "short": "Короткий", "three_quarters": "3/4",
        "long": "Длинный", "extra_long": "Удлиненный",
        "skip": "—"
    }
    sleeve_text = sleeve_map.get(val, val)

    if data.get("own_mode") or category == "own_variant":
        await state.update_data(own_sleeve=sleeve_text)

        await state.set_state(CreateForm.waiting_view)
        await _replace_with_text(callback, "👀 Пожалуйста выберите ракурс:", reply_markup=form_view_keyboard(lang))
        await _safe_answer(callback)
        return

    if data.get("infographic_mode") and category == "infographic_clothing":
        await _replace_with_text(callback, "Выберите угол камеры (Спереди/Сзади):",
                                 reply_markup=form_view_keyboard(lang))
        await state.set_state(CreateForm.waiting_info_angle)
        await _safe_answer(callback)
        return

    if category in ("female", "male", "child") and not data.get("random_mode") and not data.get("infographic_mode"):
        await _ask_garment_length(callback, state, db)
        await _safe_answer(callback)
        return

    if data.get("infographic_mode"):
        await state.set_state(CreateForm.waiting_info_angle)
        await _replace_with_text(callback, get_string("select_camera_dist", lang),
                                 reply_markup=form_view_keyboard(lang))
        return

    if data.get("random_mode"):

        await _ask_garment_length(callback, state, db)
    else:
        await _replace_with_text(callback, get_string("select_camera_dist", lang),
                                 reply_markup=form_view_keyboard(lang))
        await state.set_state(CreateForm.waiting_view)
    await _safe_answer(callback)


@router.callback_query(CreateForm.waiting_view, F.data.startswith("form_view:"))
@router.callback_query(CreateForm.waiting_info_angle, F.data.startswith("form_view:"))
@router.callback_query(CreateForm.waiting_rand_other_angle, F.data.startswith("form_view:"))
async def form_set_view(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    view = callback.data.split(":", 1)[1]
    data = await state.get_data()
    lang = await db.get_user_language(callback.from_user.id)
    category = data.get("category")
    current_state = await state.get_state()

    if current_state == CreateForm.waiting_info_angle.state:
        await state.update_data(info_angle=view)

        await _replace_with_text(callback, "Выберите ракурс (Дальний/Средний/Близкий):",
                                 reply_markup=angle_keyboard(lang))
        await state.set_state(CreateForm.waiting_preset_dist)
        await _safe_answer(callback)
        return

    if data.get("own_mode") or category == "own_variant":
        await state.update_data(view=view)

        if current_state == CreateForm.waiting_view.state and not data.get("own_product_photo_id"):
            if category == "own_variant":
                await _replace_with_text(callback, get_string("upload_bg_photo", lang),
                                         reply_markup=back_step_keyboard(lang))
                await state.set_state(CreateForm.waiting_own_bg_photo)
            else:
                await _replace_with_text(callback, get_string("upload_model_photo", lang),
                                         reply_markup=back_step_keyboard(lang))
                await state.set_state(CreateForm.waiting_ref_photo)
            await _safe_answer(callback)
            return

        await _replace_with_text(callback, get_string("select_format", lang), reply_markup=aspect_ratio_keyboard(lang))
        await state.set_state(CreateForm.waiting_aspect)
        await _safe_answer(callback)
        return

    if current_state == CreateForm.waiting_rand_other_angle.state:
        await state.update_data(view=view)

        await _replace_with_text(callback, get_string("select_camera_dist", lang),
                                 reply_markup=camera_distance_keyboard(lang))
        await state.set_state(CreateForm.waiting_rand_other_dist)
        await _safe_answer(callback)
        return

    await state.update_data(view=view)
    await _show_next_step(callback, state, db)
    await _safe_answer(callback)


@router.callback_query(CreateForm.waiting_preset_pose, F.data.startswith("pose:"))
async def on_preset_pose(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    pose = callback.data.split(":", 1)[1]
    pose_map = {"vulgar": "Вульгарная", "unusual": "Нестандартная", "normal": "Обычная"}
    await state.update_data(pose=pose_map.get(pose, pose))
    lang = await db.get_user_language(callback.from_user.id)

    await state.set_state(CreateForm.waiting_preset_dist)
    await _replace_with_text(callback, "Выберите ракурс фотографии:", reply_markup=angle_keyboard(lang))
    await _safe_answer(callback)


@router.callback_query(CreateForm.waiting_preset_dist, F.data.startswith("form_dist:") | F.data.startswith("angle:"))
@router.callback_query(CreateForm.waiting_view, F.data.startswith("form_dist:") | F.data.startswith("angle:"))
@router.callback_query(CreateForm.waiting_rand_other_dist,
                       F.data.startswith("form_dist:") | F.data.startswith("angle:"))
async def on_dist_selected(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    val = callback.data.split(":", 1)[1]
    dist_map = {"far": "Дальний", "medium": "Средний", "close": "Близкий", "skip": ""}
    dist_val = dist_map.get(val, val)

    data = await state.get_data()
    lang = await db.get_user_language(callback.from_user.id)
    category = data.get("category")

    if data.get("infographic_mode"):
        await state.update_data(info_dist=dist_val)
        if data.get("has_person"):
            from bot.keyboards import pose_keyboard
            gender = data.get("gender") or data.get("rand_gender") or data.get("info_gender") or data.get(
                "child_gender")
            show_vulgar = (gender not in ("boy", "girl"))
            await _replace_with_text(callback, "Выберите позу модели:",
                                     reply_markup=pose_keyboard(lang, show_vulgar=show_vulgar))
            await state.set_state(CreateForm.waiting_info_pose)
        else:
            if category == "infographic_other":
                from bot.keyboards import random_season_keyboard
                await _replace_with_text(callback, "Выберите сезон:", reply_markup=random_season_keyboard(lang))
                await state.set_state(CreateForm.waiting_info_season)
            else:
                await _ask_garment_length(callback, state, db)
        await _safe_answer(callback)
        return

    if data.get("random_other_mode"):
        await state.update_data(dist=dist_val)
        from bot.keyboards import skip_step_keyboard
        await _replace_with_text(callback, get_string("enter_height_cm", lang),
                                 reply_markup=skip_step_keyboard("rand_height", lang))
        await state.set_state(CreateForm.waiting_rand_other_height)
        await _safe_answer(callback)
        return

    await state.update_data(dist=dist_val)

    if data.get("category") == "storefront":
        await _ask_garment_length(callback, state, db)
        await _safe_answer(callback)
        return

    # --- ХИРУРГИЧЕСКОЕ ИСПРАВЛЕНИЕ ---
    # Создаем уникальные кнопки с префиксом 'safe_view:', чтобы обойти все баги перехвата
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    safe_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Спереди", callback_data="safe_view:front"),
            InlineKeyboardButton(text="Сзади", callback_data="safe_view:back"),
        ],
        [InlineKeyboardButton(text=get_string("back", lang), callback_data="back_step")]
    ])

    await state.set_state(CreateForm.waiting_preset_view)
    await _replace_with_text(callback, "📸 Выберите вид фотографии (Спереди/Сзади):", reply_markup=safe_kb)
    await _safe_answer(callback)


# Новый изолированный обработчик, который ловит ТОЛЬКО наши новые уникальные кнопки
@router.callback_query(F.data.startswith("safe_view:"), CreateForm.waiting_preset_view)
async def on_safe_preset_view(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    view = callback.data.split(":", 1)[1]
    await state.update_data(view=view)
    lang = await db.get_user_language(callback.from_user.id)

    # Гарантированно запрашиваем фото без обращения к динамическому движку
    from bot.keyboards import back_step_keyboard
    await state.set_state(CreateForm.waiting_view)
    await _replace_with_text(callback, "📸 Пришлите фото товара:", reply_markup=back_step_keyboard(lang))
    await _safe_answer(callback)


@router.callback_query(CreateForm.waiting_preset_view, F.data.startswith("form_view:"))
async def on_preset_view(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    view = callback.data.split(":", 1)[1]
    await state.update_data(view=view)
    lang = await db.get_user_language(callback.fromuser.id) if hasattr(callback,
                                                                       "fromuser") else await db.get_user_language(
        callback.from_user.id)

    # ГАРАНТИРОВАННЫЙ ПЕРЕХОД НА ФОТО ТОВАРА
    from bot.keyboards import back_step_keyboard
    await state.set_state(CreateForm.waiting_view)
    await _replace_with_text(callback, "📸 Пришлите фото товара:", reply_markup=back_step_keyboard(lang))
    await _safe_answer(callback)


@router.callback_query(CreateForm.waiting_preset_season,
                       F.data.startswith("rand_season:") | F.data.startswith("season:"))
async def on_preset_season(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    season = callback.data.split(":")[1]
    season_map = {"summer": "Лето", "winter": "Зима", "autumn": "Осень", "spring": "Весна", "skip": ""}
    await state.update_data(season=season_map.get(season, season))
    lang = await db.get_user_language(callback.from_user.id)
    data = await state.get_data()

    from bot.keyboards import random_holiday_keyboard
    await _replace_with_text(callback, "Выберите праздник (если есть):", reply_markup=random_holiday_keyboard(lang))
    await state.set_state(CreateForm.waiting_preset_holiday)
    await _safe_answer(callback)


@router.callback_query(CreateForm.waiting_preset_holiday,
                       F.data.startswith("rand_holiday:") | F.data.startswith("holiday:"))
async def on_preset_holiday(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    val = callback.data.split(":", 1)[1]
    holiday_map = {
        "wedding": "Свадьба", "bday": "День рождения", "may9": "9 мая",
        "newyear": "Новый год", "christmas": "Рождество", "feb23": "23 февраля",
        "march8": "8 марта", "sale": "Распродажа", "skip": ""
    }
    await state.update_data(holiday=holiday_map.get(val, val))
    lang = await db.get_user_language(callback.from_user.id)

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=get_string("back", lang), callback_data="back_step")]])
    await _replace_with_text(callback, get_string("upload_photo", lang), reply_markup=back_kb)
    await state.set_state(CreateForm.waiting_view)
    await _safe_answer(callback)


@router.callback_query(CreateForm.waiting_info_pose, F.data.startswith("pose:"))
async def on_info_pose(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    pose = callback.data.split(":", 1)[1]
    pose_map = {"vulgar": "Вульгарная", "unusual": "Нестандартная", "normal": "Обычная"}
    await state.update_data(info_pose=pose_map.get(pose, pose))

    data = await state.get_data()
    lang = await db.get_user_language(callback.from_user.id)
    category = data.get("category")

    if data.get("infographic_mode"):
        if category == "infographic_clothing":

            await _ask_garment_length(callback, state, db)
        else:

            from bot.keyboards import random_season_keyboard
            await _replace_with_text(callback, "Выберите сезон:", reply_markup=random_season_keyboard(lang))
            await state.set_state(CreateForm.waiting_info_season)
        await _safe_answer(callback)
        return
    elif category in ("female", "male", "child") and not data.get("random_mode") and not data.get("infographic_mode"):

        await state.set_state(CreateForm.waiting_preset_dist)
        await _replace_with_text(callback, "Выберите ракурс фотографии:", reply_markup=angle_keyboard(lang))
    elif data.get("random_other_mode"):

        await _replace_with_text(callback, get_string("enter_height_cm", lang),
                                 reply_markup=skip_step_keyboard("rand_height", lang))
        await state.set_state(CreateForm.waiting_rand_other_height)
    else:

        await _ask_garment_length(callback, state, db)
    await _safe_answer(callback)


from collections import defaultdict

user_locks = defaultdict(asyncio.Lock)

gen_locks = defaultdict(asyncio.Lock)

processed_msg_ids = set()


@router.message(CreateForm.waiting_view, F.photo)
async def handle_user_photo(message: Message, state: FSMContext, db: Database) -> None:
    user_id = message.from_user.id
    msg_id = message.message_id

    if msg_id in processed_msg_ids:
        return

    async with user_locks[user_id]:

        if msg_id in processed_msg_ids:
            return
        processed_msg_ids.add(msg_id)

        if len(processed_msg_ids) > 100:
            list(processed_msg_ids)[:50]

        await asyncio.sleep(0.05)

        data = await state.get_data()
        current_state = await state.get_state()

        logger.info(
            f"[handle_user_photo] User {user_id}, State: {current_state}, Photos in data: {len(data.get('photos', [])) if data else 'N/A'}")

        if not data or current_state != CreateForm.waiting_view.state:
            return

        photo_id = message.photo[-1].file_id
        lang = await db.get_user_language(user_id)
        category = data.get("category")

        if data.get("normal_gen_mode"):
            photos = data.get("photos") or []

            if photo_id not in photos:
                photos.append(photo_id)
                photos = photos[:4]

                await state.update_data(photos=photos)

                data["photos"] = photos

            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Далее" if len(photos) < 4 else "Перейти к промпту",
                                      callback_data="normal_photos_done")],
                [InlineKeyboardButton(text=get_string("back", lang), callback_data="back_step")]
            ])

            if len(photos) < 4:
                text = f"📸 Фото {len(photos)}/4 получено.\n\nВы можете отправить еще до {4 - len(photos)} фото или нажмите «Далее», чтобы продолжить."
            else:
                text = "✅ Получено 4/4 фото. Теперь нажмите «Далее», чтобы отправить промпт."
                await state.set_state(CreateForm.waiting_prompt)

            last_msg_id = data.get("last_photos_msg_id")
            if last_msg_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=last_msg_id,
                        text=text,
                        reply_markup=kb
                    )
                    return
                except Exception as e:
                    logger.debug(f"Could not edit counter message: {e}")

            msg = await message.answer(text, reply_markup=kb)
            await state.update_data(last_photos_msg_id=msg.message_id)
            return

        photo_key = "user_photo_id"
        if category in ("own", "own_variant") or data.get("own_mode"):
            photo_key = "own_product_photo_id"

        await state.update_data({photo_key: photo_id})

        if data.get("repeat_mode"):
            await state.update_data(repeat_mode=False)
            quality = data.get("selected_quality")
            if not quality:
                await _ask_quality(message, state, db)
            else:
                await _show_confirmation(message, state, db)
            return
        elif category == "infographic_clothing":
            dummy_callback = CallbackQuery(
                id="0", from_user=message.from_user, chat_instance="0",
                message=message, data=f"form_aspect:{data.get('aspect', '1:1')}"
            ).as_(message.bot)
            await on_aspect_selected(dummy_callback, state, db)
            return
        elif data.get("random_other_mode"):
            await _show_confirmation(message, state, db)
            return
        else:
            await _show_next_step(message, state, db)
            return

    return


@router.callback_query(F.data == "back_step")
async def on_back_step(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    current_state = await state.get_state()
    data = await state.get_data()
    lang = await db.get_user_language(callback.from_user.id)
    category = data.get("category")

    if not current_state:
        await on_back_main(callback, state, db)
        return

    if data.get("random_other_mode"):
        from bot.keyboards import yes_no_keyboard, skip_step_keyboard, infographic_gender_keyboard, form_view_keyboard, \
            camera_dist_keyboard, random_season_keyboard, style_keyboard
        if current_state == CreateForm.waiting_rand_other_has_person.state:
            await on_marketplace_menu(callback, db)
            await state.clear()
            await _safe_answer(callback)
            return
        elif current_state == CreateForm.waiting_rand_other_gender.state:
            await _replace_with_text(callback, get_string("has_person_ask", lang), reply_markup=yes_no_keyboard(lang))
            await state.set_state(CreateForm.waiting_rand_other_has_person)
            await _safe_answer(callback)
            return
        elif current_state == CreateForm.waiting_info_load.state:
            if data.get("has_person"):
                await state.set_state(CreateForm.waiting_rand_other_gender)
                await _replace_with_text(callback, get_string("select_gender", lang),
                                         reply_markup=infographic_gender_keyboard(lang))
            else:
                await state.set_state(CreateForm.waiting_rand_other_has_person)
                await _replace_with_text(callback, get_string("has_person_ask", lang),
                                         reply_markup=yes_no_keyboard(lang))
            await _safe_answer(callback)
            return
        elif current_state == CreateForm.waiting_rand_other_name.state:
            await state.set_state(CreateForm.waiting_info_load)
            await _replace_with_text(callback, get_string("enter_info_load", lang),
                                     reply_markup=skip_step_keyboard("info_load", lang))
            await _safe_answer(callback)
            return
        elif current_state == CreateForm.waiting_rand_other_angle.state:
            await _replace_with_text(callback, get_string("enter_product_name", lang),
                                     reply_markup=skip_step_keyboard("rand_name", lang))
            await state.set_state(CreateForm.waiting_rand_other_name)
            await _safe_answer(callback)
            return
        elif current_state == CreateForm.waiting_rand_other_dist.state:
            await _replace_with_text(callback, "Выберите угол камеры (Спереди/Сзади):",
                                     reply_markup=form_view_keyboard(lang))
            await state.set_state(CreateForm.waiting_rand_other_angle)
            await _safe_answer(callback)
            return
        elif current_state == CreateForm.waiting_rand_other_height.state:
            await _replace_with_text(callback, "Выберите ракурс фотографии (Дальний/Средний/Близкий):",
                                     reply_markup=camera_dist_keyboard(lang))
            await state.set_state(CreateForm.waiting_rand_other_dist)
            await _safe_answer(callback)
            return
        elif current_state == CreateForm.waiting_rand_other_width.state:
            await _replace_with_text(callback, "Введите высоту (см):",
                                     reply_markup=skip_step_keyboard("rand_height", lang))
            await state.set_state(CreateForm.waiting_rand_other_height)
            await _safe_answer(callback)
            return
        elif current_state == CreateForm.waiting_rand_other_length.state:
            await _replace_with_text(callback, "Введите ширину (см):",
                                     reply_markup=skip_step_keyboard("rand_width", lang))
            await state.set_state(CreateForm.waiting_rand_other_width)
            await _safe_answer(callback)
            return
        elif current_state == CreateForm.waiting_rand_other_season.state:
            await _replace_with_text(callback, "Введите длину (см):",
                                     reply_markup=skip_step_keyboard("rand_length", lang))
            await state.set_state(CreateForm.waiting_rand_other_length)
            await _safe_answer(callback)
            return
        elif current_state == CreateForm.waiting_rand_other_style.state:
            await _replace_with_text(callback, "Выберите сезон:", reply_markup=random_season_keyboard(lang))
            await state.set_state(CreateForm.waiting_rand_other_season)
            await _safe_answer(callback)
            return
        elif current_state == CreateForm.waiting_view.state:
            await _replace_with_text(callback, get_string("select_style", lang), reply_markup=style_keyboard(lang))
            await state.set_state(CreateForm.waiting_rand_other_style)
            await _safe_answer(callback)
            return

    current_index = data.get("current_step_index")
    if current_index is not None:
        await state.update_data(is_going_back=True)

        waiting_custom_for = data.get("waiting_custom_for")
        if waiting_custom_for:
            cat_db = await db.get_category_by_key(category)
            if cat_db:
                steps = await db.list_steps(cat_db[0])
                for idx, step in enumerate(steps):
                    if step[1] == waiting_custom_for:
                        await state.update_data(waiting_custom_for=None, current_step_index=idx)
                        await _show_next_step(callback, state, db)
                        await _safe_answer(callback)
                        return
        if current_index > 0:

            new_index = current_index - 1

            cat_db = await db.get_category_by_key(category)
            if cat_db:
                steps = await db.list_steps(cat_db[0])
                if not steps:
                    await _show_main_menu_by_obj(callback, db)
                    await _safe_answer(callback)
                    return
                if new_index >= len(steps):
                    new_index = len(steps) - 1
                while new_index >= 0:
                    step = steps[new_index]
                    s_key = step[1]
                    gender = data.get("gender") or data.get("rand_gender") or data.get("info_gender") or data.get(
                        "child_gender")

                    should_skip = False

                    if s_key == "age" and gender in ("boy", "girl"):
                        should_skip = True

                    elif s_key in ("gender", "rand_gender", "info_gender", "child_gender") and data.get(s_key):

                        if new_index > 0:
                            should_skip = True

                    loc_group = data.get("rand_loc_group")
                    if loc_group:
                        if loc_group == "indoor" and s_key == "rand_location_outdoor":
                            should_skip = True
                        elif loc_group == "outdoor" and s_key == "rand_location_indoor":
                            should_skip = True
                        elif loc_group == "indoor" and "season" in s_key.lower():
                            should_skip = True

                    person_absent = False
                    for k, v in data.items():
                        if k.endswith("_label") and any(
                                x in str(v).lower() for x in ("без человека", "without person", "нет", "no")):
                            if any(x in k.lower() for x in ("person", "presence", "человек", "присутствие")):
                                person_absent = True;
                                break
                        if isinstance(v, str) and v.lower() in (
                        "person_no", "without_person", "no_person", "no", "нет"):
                            if any(x in k.lower() for x in ("person", "presence", "человек", "присутствие")):
                                person_absent = True;
                                break

                    if person_absent:
                        low_s_key = s_key.lower()
                        if any(x in low_s_key for x in
                               ("age", "pose", "height", "size", "возраст", "поза", "рост", "телосложение")):

                            if category not in ("random_other", "infographic_other") or not any(
                                    x in low_s_key for x in ("height", "рост")):
                                should_skip = True

                    if should_skip:
                        new_index -= 1
                        continue
                    break

            if new_index < 0:

                if data.get("is_preset") and data.get("gender"):
                    await _show_models_for_category(
                        callback, db,
                        category=data.get("gender"),
                        cloth=data.get("cloth", "all"),
                        index=data.get("index", 0),
                        logic_category="presets"
                    )
                    await _safe_answer(callback)
                    return

                await on_marketplace_menu(callback, db)
                await state.clear()
                await _safe_answer(callback)
                return

            if cat_db:
                target_step_key = steps[new_index][1]
                await state.update_data({target_step_key: None})

            await state.update_data(current_step_index=new_index)
            await _show_next_step(callback, state, db)
            await _safe_answer(callback)
            return
        else:

            if data.get("is_preset") and data.get("gender"):
                await _show_models_for_category(
                    callback, db,
                    category=data.get("gender"),
                    cloth=data.get("cloth", "all"),
                    index=data.get("index", 0),
                    logic_category="presets"
                )
                await _safe_answer(callback)
                return

            await on_marketplace_menu(callback, db)
            await state.clear()
            await _safe_answer(callback)
            return

    if current_state == CreateForm.waiting_prompt.state:
        if data.get("normal_gen_mode"):
            photos = data.get("photos") or []
            if len(photos) > 0:
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Далее" if len(photos) < 4 else "Перейти к промпту",
                                          callback_data="normal_photos_done")],
                    [InlineKeyboardButton(text=get_string("back", lang), callback_data="back_step")]
                ])
                text = f"📸 Фото {len(photos)}/4 получено.\n\nВы можете отправить еще до {4 - len(photos)} фото или нажмите «Далее», чтобы продолжить."
            else:
                from bot.keyboards import back_main_keyboard
                kb = back_main_keyboard(lang)
                text = "📸 Пришлите до 4 фото (можно по одному или серией)."

            await _replace_with_text(callback, text, reply_markup=kb)
            await state.set_state(CreateForm.waiting_view)
            await _safe_answer(callback)
            return

        await _replace_with_text(callback, get_string("upload_photo", lang), reply_markup=back_main_keyboard(lang))
        await state.set_state(CreateForm.waiting_view)
        await _safe_answer(callback)
        return

    if current_state == CreateForm.waiting_edit_text.state:
        await state.set_state(CreateForm.result_ready)
        kb = result_actions_own_keyboard(lang) if (
                    data.get("own_mode") or category == "own_variant") else result_actions_keyboard(lang)
        await _replace_with_text(callback, get_string("gen_ready", lang), reply_markup=kb)
        await _safe_answer(callback)
        return

    await on_marketplace_menu(callback, db)
    await state.clear()
    await _safe_answer(callback)
    return


@router.callback_query(CreateForm.waiting_info_season, F.data.startswith("season:") | F.data.startswith("rand_season:"))
async def on_info_season(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    season = callback.data.split(":")[1]
    season_map = {"summer": "Лето", "winter": "Зима", "autumn": "Осень", "spring": "Весна", "skip": ""}
    await state.update_data(info_season=season_map.get(season, season))
    lang = await db.get_user_language(callback.from_user.id)

    from bot.keyboards import random_holiday_keyboard
    await _replace_with_text(callback, "Выберите праздник (если есть):", reply_markup=random_holiday_keyboard(lang))
    await state.set_state(CreateForm.waiting_info_holiday)
    await _safe_answer(callback)


@router.callback_query(CreateForm.waiting_info_holiday,
                       F.data.startswith("holiday:") | F.data.startswith("rand_holiday:"))
async def on_info_holiday(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    holiday = callback.data.split(":")[1]
    holiday_map = {
        "wedding": "Свадьба", "bday": "День рождения", "may9": "9 мая",
        "newyear": "Новый год", "christmas": "Рождество", "feb23": "23 февраля",
        "march8": "8 марта", "sale": "Распродажа", "skip": ""
    }
    await state.update_data(info_holiday=holiday_map.get(holiday, holiday))
    lang = await db.get_user_language(callback.from_user.id)

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=get_string("back", lang), callback_data="back_step")]])
    await _replace_with_text(callback, get_string("upload_photo", lang), reply_markup=back_kb)
    await state.set_state(CreateForm.waiting_view)
    await _safe_answer(callback)


async def _ask_quality(message_or_callback: Message | CallbackQuery, state: FSMContext, db: Database) -> None:
    # При глобальном запрете 4K (включая фотостудию) сразу идём в подтверждение с 1K.
    if await db.is_4k_disabled():
        await state.update_data(selected_quality="1k")
        await _show_confirmation(message_or_callback, state, db)
        return

    user_id = message_or_callback.from_user.id
    base_price = await db.get_user_generation_price(user_id)
    p4 = await db.get_user_generation_charge_rub(user_id, "4k")
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"1K (Стандарт) - {base_price}₽", callback_data="gen_qual_pre:1k")],
        [InlineKeyboardButton(text=f"4K (Ультра) - {p4}₽", callback_data="gen_qual_pre:4k")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_step")]
    ])
    text = "✨ <b>Выберите качество финальной фотографии:</b>\n\nЧем выше качество, тем больше деталей и четкости будет на изображении."

    if isinstance(message_or_callback, CallbackQuery):
        await _replace_with_text(message_or_callback, text, reply_markup=kb, parse_mode="HTML")
    else:
        await message_or_callback.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("gen_qual_pre:"))
async def process_quality_choice_pre(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    quality_choice = callback.data.split(":")[1]
    if quality_choice == "4k" and await db.is_4k_disabled():
        quality_choice = "1k"
    await state.update_data(selected_quality=quality_choice)
    # После выбора качества показываем финальное окно
    await _show_confirmation(callback, state, db)
    await _safe_answer(callback)

@router.callback_query(F.data == "form_generate")
async def form_generate(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    user_id = callback.from_user.id
    if gen_locks[user_id].locked():
        await _safe_answer(callback)
        return
    async with gen_locks[user_id]:
        # Подтверждено. Запускаем без лишних вопросов
        await _do_generate_real(callback, state, db)


async def _do_generate_real(message_or_callback: Message | CallbackQuery, state: FSMContext, db: Database) -> None:
    user_id = message_or_callback.from_user.id
    data = await state.get_data()
    logger.info(f"[_do_generate] Начало генерации для пользователя {user_id}. Данные сессии: {data}")

    if isinstance(message_or_callback, CallbackQuery):
        ans_obj = message_or_callback.message
        bot = message_or_callback.bot
    else:
        ans_obj = message_or_callback
        bot = message_or_callback.bot

    if await db.get_maintenance():
        settings = load_settings()
        if user_id not in (settings.admin_ids or []):
            lang = await db.get_user_language(user_id)
            if isinstance(message_or_callback, CallbackQuery):
                await _safe_answer(message_or_callback, get_string("maintenance_alert", lang), show_alert=True)
            else:
                await ans_obj.answer(get_string("maintenance_alert", lang))
            return

    if await db.get_user_blocked(user_id):
        lang = await db.get_user_language(user_id)
        text = get_string("user_blocked", lang)
        if isinstance(message_or_callback, CallbackQuery):
            await _safe_answer(message_or_callback, text, show_alert=True)
        else:
            await ans_obj.answer(text)
        return

    category = data.get("category")
    lang = await db.get_user_language(user_id)

    input_photos, missing_product_for_presets = await _collect_input_photos(data, category, db)
    if missing_product_for_presets:
            logger.error(f"[_do_generate] Нет фото товара для пресетов. Input photos: {input_photos}")
            text = get_string("upload_product", lang)
            await state.set_state(CreateForm.waiting_view)
            if isinstance(message_or_callback, CallbackQuery):
                await message_or_callback.message.answer(text)
                await _safe_answer(message_or_callback)
            else:
                await ans_obj.answer(text)
            return
    if not input_photos:
        logger.error(f"[_do_generate] Нет фото для генерации. Category: {category}, Data keys: {list(data.keys())}")
        text = get_string("upload_product", lang)
        await state.set_state(CreateForm.waiting_view)
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.message.answer(text)
            await _safe_answer(message_or_callback)
        else:
            await message_or_callback.answer(text)
        return

    balance = await db.get_user_balance(user_id)
    quality_choice = await db.resolve_photo_quality(data.get("selected_quality", "1k"))
    if data.get("selected_quality") != quality_choice:
        await state.update_data(selected_quality=quality_choice)
        data = await state.get_data()
    total_price = await db.get_user_generation_charge_rub(user_id, quality_choice)

    if balance < total_price:
        msg = f"❌ Недостаточно средств.\n\nСтоимость генерации ({quality_choice.upper()}) = {total_price} руб.\nВаш баланс: {balance} руб.\n\nПожалуйста, пополните баланс в профиле."
        if isinstance(message_or_callback, CallbackQuery):
            await _safe_answer(message_or_callback, msg, show_alert=True)
        else:
            await ans_obj.answer(msg)
        return

    try:
        quality_choice = await db.resolve_photo_quality(data.get("selected_quality", "1k"))
        quality = '4k' if quality_choice == '4k' else 'HD'

        if not data:
            logger.error(f"[_do_generate] КРИТИЧЕСКАЯ ОШИБКА: Данные сессии пусты для пользователя {user_id}")
            if isinstance(message_or_callback, CallbackQuery):
                await _safe_answer(message_or_callback, get_string("session_not_found", lang) + " (пустые данные)",
                                   show_alert=True)
            else:
                await ans_obj.answer(get_string("session_not_found", lang) + " (пустые данные)")
            return

        prompt_filled = await _build_final_prompt(data, db)
        logger.info(f"[_do_generate] Сформирован финальный промпт (длина: {len(prompt_filled)} симв.)")
        await state.update_data(last_sent_prompt=prompt_filled)

        # Не дописываем лишний текст к промпту — используем только то что из админки.

        process_msg = None
        if category == PHOTOSTUDIO_CAT_KEY and isinstance(message_or_callback, CallbackQuery):
            # Экран подтверждения превращаем в прогресс, а не плодим новое сообщение
            try:
                await ans_obj.edit_text("🎨 ⚡️ ⏳")
                process_msg = ans_obj
            except Exception:
                process_msg = None
        if process_msg is None:
            process_msg = await ans_obj.answer("🎨 ⚡️ ⏳")

        gen_done = asyncio.Event()

        async def _generation_progress_loop() -> None:
            start_time = time.time()
            steps_text = [
                "Изучаю ваш запрос",
                "Обрабатываю детали",
                "Применяю нейронные фильтры",
                "Улучшаю качество",
                "Финализирую",
            ]
            n = len(steps_text)
            expected = _progress_bar_expected_wall_seconds()
            timeout = float(_generation_timeout_seconds())
            try:
                while not gen_done.is_set():
                    elapsed = time.time() - start_time
                    base = 100.0 * elapsed / max(18.0, expected)
                    if elapsed <= expected:
                        progress = int(min(97, max(0, base)))
                    else:
                        over = elapsed - expected
                        tail = max(12.0, min(55.0, timeout - expected))
                        progress = int(min(99, 97 + 2.0 * (1.0 - math.exp(-over / tail))))
                    step_idx = min(n - 1, max(0, int(progress * n / 101)))
                    filled = min(10, int(progress / 10))
                    bar = "🟦" * filled + "⬜️" * (10 - filled)
                    text = (
                        f"🚀 Генерация\n\n"
                        f"{steps_text[step_idx]}\n\n"
                        f"{bar} {progress}%\n\n"
                        f"Прошло: {int(elapsed)}с\n\n"
                        f"Результат вас приятно удивит"
                    )
                    try:
                        if process_msg is None:
                            break
                        await process_msg.edit_text(text)
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(gen_done.wait(), timeout=1.15)
                        break
                    except asyncio.TimeoutError:
                        pass
                elapsed = time.time() - start_time
                try:
                    if process_msg is not None:
                        await process_msg.edit_text(
                            f"🚀 Генерация\n\n{steps_text[-1]}\n\n{'🟦' * 10} 100%\n\n"
                            f"Прошло: {int(elapsed)}с\n\nГотово…"
                        )
                except Exception:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        progress_task = asyncio.create_task(_generation_progress_loop())
        try:
            images_data = await telegram_file_ids_to_bytes_list(bot, input_photos, base_dir=BASE_DIR)

            last_error_msg = ""
            result_path = None
            last_key_id = 0
            last_key_preview = "zveno"
            last_model_used = "gemini-3-pro-image-preview"

            if not images_data:
                last_error_msg = "input_photo_download_failed"
            else:
                from bot.gemini import generate_image, upscale_image_vertex
                raw_aspect = data.get("aspect") or "1:1"
                aspect = raw_aspect.replace(":", "x")
                if aspect == "auto": aspect = "1x1"

                gen_meta = {"category": category, "user_id": user_id}
                try:
                    result_path = await asyncio.wait_for(
                        generate_image(
                            api_key="dummy",
                            prompt=prompt_filled,
                            images_bytes=images_data,
                            aspect_ratio=aspect,
                            quality=quality,
                            key_id=0,
                            db_instance=db,
                            generation_meta=gen_meta,
                        ),
                        timeout=_generation_timeout_seconds()
                    )
                    if result_path:

                        used_project_id = gen_meta.get("project_id")
                        if used_project_id:
                            await db.increment_gcp_usage(used_project_id)

                        last_model_used = gen_meta.get("model_used", "unknown")
                        last_key_preview = gen_meta.get("zveno_key_preview") or gen_meta.get("kie_key_preview") or "zveno"
                        await db.subtract_user_balance(user_id, total_price)
                        from bot.keyboards import result_actions_keyboard, result_actions_own_keyboard
                        from aiogram.types import FSInputFile

                        file_name = os.path.basename(result_path)

                        if data.get("own_mode") or category in ("own", "own_variant"):
                            kb_res = result_actions_own_keyboard(lang, file_name=file_name)
                        else:
                            kb_res = result_actions_keyboard(lang, file_name=file_name)

                        pid = await db.generate_pid()
                        db_result_path = result_path.replace("\\", "/").replace(BASE_DIR.replace("\\", "/") + "/", "")
                        if not db_result_path.startswith("data/history/"):
                            db_result_path = f"data/history/{file_name}"

                        await state.update_data(result_photo_id=db_result_path)

                        if category == PHOTOSTUDIO_CAT_KEY and process_msg is not None:
                            # Карточка встаёт на место прогресса, а не под ним
                            try:
                                await process_msg.delete()
                            except Exception:
                                pass
                            process_msg = None

                        await ans_obj.answer_document(
                            document=FSInputFile(result_path),
                            caption=get_string("gen_success", lang),
                            reply_markup=kb_res
                        )

                        await state.set_state(CreateForm.result_ready)

                        try:
                            local_input_paths = []
                            for i, f_id in enumerate(input_photos):
                                if not f_id: continue
                                db_inp_path = f"data/history/input_{pid}_{i}.jpg"
                                local_inp_path = os.path.join(BASE_DIR, db_inp_path)
                                try:
                                    f_info = await bot.get_file(f_id)
                                    await bot.download_file(f_info.file_path, local_inp_path)
                                    local_input_paths.append(db_inp_path)
                                except:
                                    pass
                        except:
                            pass

                        history_payload = dict(data or {})
                        history_payload["_generation_meta"] = {"model_used": last_model_used}
                        await db.add_generation_history(
                            pid=pid, user_id=user_id, category=category,
                            params=json.dumps(history_payload), input_photos=json.dumps(input_photos),
                            result_photo_id=db_result_path, input_paths=json.dumps(local_input_paths),
                            result_path=db_result_path, prompt=prompt_filled
                        )

                        try:
                            if process_msg is not None:
                                await process_msg.delete()
                        except:
                            pass
                        if isinstance(message_or_callback, CallbackQuery): await _safe_answer(message_or_callback)
                        return
                    else:
                        last_error_msg = "empty_result"
                except asyncio.TimeoutError:
                    last_error_msg = "generation_timeout"
                except Exception as e:
                    last_key_preview = gen_meta.get("zveno_key_preview") or gen_meta.get("kie_key_preview") or last_key_preview
                    last_model_used = gen_meta.get("model_used", target_model if 'target_model' in locals() else last_model_used)
                    last_error_msg = str(e)
        finally:
            gen_done.set()
            await asyncio.sleep(0.28)
            progress_task.cancel()
            try:
                await progress_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            if process_msg is not None:
                await process_msg.delete()
        except:
            pass

        try:
            cooldown_s = 90
            now_ts = int(time.time())
            last_ts_raw = await db.get_app_setting("tg_error_notify_last_ts", "0")
            try:
                last_ts = int(str(last_ts_raw or "0").strip() or "0")
            except Exception:
                last_ts = 0
            if now_ts - last_ts >= cooldown_s:
                try:
                    await db.set_app_setting("tg_error_notify_last_ts", str(now_ts))
                except Exception:
                    pass

                settings = load_settings()
                admin_ids = settings.admin_ids or [1440716472, 916948327]
                short_err = (last_error_msg or "unknown_error_unclassified").strip()
                if len(short_err) > 600:
                    short_err = short_err[:600] + "..."
                provider_reason = ""
                if "zveno_insufficient_credits" in short_err.lower() or "insufficient credits" in short_err.lower():
                    provider_reason = "Provider status: insufficient credits (upstream billing)"
                elif "image_generation_provider_location_unsupported" in short_err.lower() or "location is not supported" in short_err.lower():
                    provider_reason = "Provider status: Google AI Studio location unsupported"
                elif "image_generation_empty_image" in short_err.lower() or "zveno_empty_image" in short_err.lower():
                    provider_reason = "Provider status: empty image response (200 OK without image)"
                elif "image_generation_safety_blocked" in short_err.lower():
                    provider_reason = "Provider status: image safety/content filter"
                notify_text = (
                    "⚠️ Ошибка генерации\n"
                    f"User: {user_id}\n"
                    f"Category: {category}\n"
                    f"Quality: {quality_choice}\n"
                    f"Input photos: {len(input_photos) if isinstance(input_photos, list) else 'n/a'}\n"
                    f"Prompt len: {len(prompt_filled) if 'prompt_filled' in locals() and prompt_filled else 0}\n"
                    f"Error: {short_err}\n"
                    f"Key: {last_key_id or 'n/a'} ({last_key_preview or 'n/a'})\n"
                    f"Model: {last_model_used or 'n/a'}"
                )
                if provider_reason:
                    notify_text = notify_text.replace(f"Error: {short_err}\n", f"{provider_reason}\nError: {short_err}\n")
                for aid in admin_ids:
                    try:
                        await bot.send_message(chat_id=int(aid), text=notify_text)
                    except Exception:
                        pass
        except Exception:
            pass

        msg_l = (last_error_msg or "").lower()
        show_retry = False

        if last_error_msg and any(
                word in msg_l for word in
                ["safety", "blocked", "prohibited", "violated", "filtered", "policy", "sensitive"]):
            err_text = "🔞 <b>Генерация заблокирована нейросетью.</b>\n\nСработал фильтр безопасности контента. Убедитесь, что на фото нет глубокого декольте, нижнего белья, открытых частей тела или провокационных поз. Пожалуйста, выберите другое фото."
            show_retry = False
        elif "503" in msg_l or "high demand" in msg_l or "unavailable" in msg_l:
            err_text = "⚠️ Сейчас сервис генерации перегружен. Попробуйте повторить через 1-2 минуты."
            show_retry = True
        elif "generation_total_timeout" in msg_l:
            err_text = "⚠️ Генерация превысила лимит времени. Попробуйте еще раз с менее сложным промптом/фото."
            show_retry = True
        elif "timed out" in msg_l or "timeout" in msg_l or "proxy/network error" in msg_l or "таймаут" in msg_l:
            use_proxy_mode = str(os.getenv("GEMINI_USE_PROXY", "")).lower() in ("1", "true", "yes")
            if use_proxy_mode:
                err_text = "⚠️ Временная сетевая ошибка сервиса генерации. Серверы сейчас под высокой нагрузкой. Попробуйте еще раз через 20-60 секунд."
            else:
                err_text = "⚠️ Временная сетевая ошибка сервиса генерации. Серверы сейчас под высокой нагрузкой. Попробуйте еще раз через 10-30 секунд."
            show_retry = True
        elif "all_keys_busy" in msg_l or "kie_keys_busy" in msg_l or "zveno_keys_busy" in msg_l:
            err_text = "⚠️ Сейчас все ключи заняты активными запросами. Повторите через 10-20 секунд."
            show_retry = True
        elif "zveno_insufficient_credits" in msg_l or "insufficient credits" in msg_l:
            err_text = "⚠️ Сервис генерации временно недоступен. Мы уже получили уведомление и работаем над восстановлением. Попробуйте повторить чуть позже."
            show_retry = True
        elif "image_generation_provider_location_unsupported" in msg_l or "location is not supported" in msg_l:
            err_text = "⚠️ Сервис генерации временно недоступен на стороне провайдера. Мы уже получили уведомление. Попробуйте повторить чуть позже."
            show_retry = True
        elif "апскейл превысил" in msg_l or "upscale timeout" in msg_l:
            err_text = "⚠️ Изображение подготовлено в стандартном качестве, этап 4K занял слишком много времени. Попробуйте повторить позже."
            show_retry = True
        elif "image_generation_empty_image" in msg_l or "zveno_empty_image" in msg_l or "empty_result" in msg_l:
            err_text = "⚠️ Нейросеть вернула ответ без изображения. Попробуйте другой ракурс/промпт и повторите."
            show_retry = True
        elif "image_generation_failed_try_again" in msg_l:
            err_text = (
                "⚠️ Не удалось завершить генерацию в этот раз. Нажмите «Попробовать еще раз». "
                "Если ошибка повторится, выберите другое фото модели и повторите."
            )
            show_retry = True
        elif "input_photo_download_failed" in msg_l:
            err_text = "⚠️ Не удалось получить входные фото. Пожалуйста, отправьте фото еще раз."

        else:
            err_text = get_string("api_error_user", lang)
            show_retry = True

        retry_kb = None
        if show_retry:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            retry_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Попробовать еще раз", callback_data="form_generate")]
            ])

        if isinstance(message_or_callback, CallbackQuery):
            await _replace_with_text(message_or_callback, err_text, reply_markup=retry_kb)
        else:
            await ans_obj.answer(err_text, reply_markup=retry_kb)

    except Exception as e:
        logger.error(f"Критическая ошибка в _do_generate: {e}")
        if "progress_task" in locals() and progress_task:
            try:
                if "gen_done" in locals() and gen_done:
                    gen_done.set()
                progress_task.cancel()
                await progress_task
            except Exception:
                pass
        try:
            settings = load_settings()
            admin_ids = settings.admin_ids or [1440716472, 916948327]
            crit_err = f"critical_exception:{type(e).__name__}:{str(e)[:400]}"
            notify_text = (
                "⚠️ Ошибка генерации\n"
                f"User: {user_id}\n"
                f"Category: {category}\n"
                f"Quality: {quality_choice if 'quality_choice' in locals() else 'n/a'}\n"
                f"Input photos: {len(input_photos) if 'input_photos' in locals() and isinstance(input_photos, list) else 'n/a'}\n"
                f"Prompt len: {len(prompt_filled) if 'prompt_filled' in locals() and prompt_filled else 0}\n"
                f"Error: {crit_err}\n"
                f"Key: {last_key_id or 'n/a'} ({last_key_preview or 'n/a'})\n"
                f"Model: {last_model_used or 'n/a'}"
            )
            for aid in admin_ids:
                try:
                    await bot.send_message(chat_id=int(aid), text=notify_text)
                except Exception:
                    pass
        except Exception:
            pass

        err_text = get_string("gen_error_contact_support", lang)

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        crit_retry_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Попробовать еще раз", callback_data="form_generate")]
        ])

        if isinstance(message_or_callback, CallbackQuery):
            await _replace_with_text(message_or_callback, err_text, reply_markup=crit_retry_kb)
        else:
            await ans_obj.answer(err_text, reply_markup=crit_retry_kb)


@router.callback_query(F.data == "result_edit")
async def on_result_edit(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.set_state(CreateForm.waiting_edit_text)
    lang = await db.get_user_language(callback.from_user.id)
    await callback.message.answer(get_string("enter_edit_description", lang))
    await _safe_answer(callback)


@router.message(CreateForm.waiting_edit_text)
async def on_result_edit_text(message: Message, state: FSMContext, db: Database) -> None:
    user_id = message.from_user.id
    if gen_locks[user_id].locked():
        await message.answer("Пожалуйста, подождите, выполняется обработка ваших правок.")
        return
    async with gen_locks[user_id]:
        await on_result_edit_text_real(message, state, db)


async def on_result_edit_text_real(message: Message, state: FSMContext, db: Database) -> None:
    edit_text = (message.text or "").strip()
    data = await state.get_data()
    user_id = message.from_user.id
    lang = await db.get_user_language(user_id)

    logger.info(f"[on_result_edit_text] Пользователь {user_id} ввел правки: {edit_text}")

    if not data:
        await message.answer(get_string("session_not_found", lang))
        await state.clear()
        return

    balance = await db.get_user_balance(user_id)

    quality_choice = await db.resolve_photo_quality(data.get("selected_quality", "1k"))
    if data.get("selected_quality") != quality_choice:
        await state.update_data(selected_quality=quality_choice)
        data = await state.get_data()
    total_price = await db.get_user_generation_charge_rub(user_id, quality_choice)

    if balance < total_price:
        await message.answer(
            f"❌ Недостаточно средств на балансе для правок.\n\nСтоимость правки = {total_price} руб.\nВаш баланс: {balance} руб.")
        return

    category = data.get("category", "female")

    base_prompt = await _build_final_prompt(data, db)
    prompt_filled = f"{base_prompt}\n\nПравки: {edit_text}"

    quality_choice = await db.resolve_photo_quality(data.get("selected_quality", "1k"))
    quality = '4k' if quality_choice == '4k' else 'HD'

    input_photos, _ = await _collect_input_photos(data, category, db)
    if not input_photos:
        logger.error(f"[on_result_edit_text] Фото не найдены в данных сессии: {data}")
        await message.answer("Не найдены исходные фото. Начните заново.")
        return

    process_msg = await message.answer("🎨 ⚡️ ⏳")

    edit_done = asyncio.Event()

    async def _edit_progress_loop() -> None:
        start_time = time.time()
        steps_text = [
            "Понимаю, что изменить",
            "Сверяю с оригиналом",
            "Вношу корректировки",
            "Фокусирую детали",
            "Финализирую",
        ]
        n = len(steps_text)
        expected = _progress_bar_expected_wall_seconds()
        timeout = float(_generation_timeout_seconds())
        try:
            while not edit_done.is_set():
                elapsed = time.time() - start_time
                base = 100.0 * elapsed / max(18.0, expected)
                if elapsed <= expected:
                    progress = int(min(97, max(0, base)))
                else:
                    over = elapsed - expected
                    tail = max(12.0, min(55.0, timeout - expected))
                    progress = int(min(99, 97 + 2.0 * (1.0 - math.exp(-over / tail))))
                step_idx = min(n - 1, max(0, int(progress * n / 101)))
                filled = min(10, int(progress / 10))
                bar = "🟦" * filled + "⬜️" * (10 - filled)
                text = (
                    f"✏️ Редактирование\n\n"
                    f"{steps_text[step_idx]}\n\n"
                    f"{bar} {progress}%\n\n"
                    f"Прошло: {int(elapsed)}с\n\n"
                    f"Результат вас приятно удивит"
                )
                try:
                    await process_msg.edit_text(text)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(edit_done.wait(), timeout=1.1)
                    break
                except asyncio.TimeoutError:
                    pass
            elapsed = time.time() - start_time
            try:
                await process_msg.edit_text(
                    f"✏️ Редактирование\n\n{steps_text[-1]}\n\n{'🟦' * 10} 100%\n\n"
                    f"Прошло: {int(elapsed)}с\n\nГотово…"
                )
            except Exception:
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    progress_task = asyncio.create_task(_edit_progress_loop())

    try:

        downloaded_paths = []
        import uuid
        for fid in input_photos:
            if not fid: continue
            try:

                if str(fid).startswith("data/"):
                    local_path = os.path.join(BASE_DIR, fid)
                    if os.path.exists(local_path):
                        ext = local_path.split('.')[-1]
                        p = os.path.join(BASE_DIR, "data", f"temp_edit_{uuid.uuid4()}.{ext}")
                        import shutil
                        os.makedirs(os.path.dirname(p), exist_ok=True)
                        shutil.copy2(local_path, p)
                        downloaded_paths.append(p)
                        logger.info(f"[Edit] Added local file: {fid}")
                        continue

                f_info = await message.bot.get_file(fid)

                logger.info(f"[Edit] File {fid} size from Telegram: {f_info.file_size} bytes")

                ext = f_info.file_path.split('.')[-1]
                p = os.path.join(BASE_DIR, "data", f"temp_edit_{uuid.uuid4()}.{ext}")
                os.makedirs(os.path.dirname(p), exist_ok=True)
                await message.bot.download_file(f_info.file_path, p)

                if os.path.exists(p):
                    sz = os.path.getsize(p)
                    logger.info(f"[Edit] Real file size on disk: {sz} bytes")

                downloaded_paths.append(p)
            except Exception as e:
                logger.error(f"[Edit] Ошибка загрузки фото {fid}: {e}")

        last_error_msg = ""
        result_path = None
        kid_used = 0
        key_preview_used = "zveno"
        model_used = "gemini-3-pro-image-preview"
        gen_meta: dict = {"category": category, "user_id": user_id}

        images_data = []
        for p in downloaded_paths:
            read_path = p if os.path.isabs(p) else os.path.join(BASE_DIR, p)
            try:
                with open(read_path, "rb") as f:
                    images_data.append(f.read())
            except Exception as ex:
                logger.error(f"[Edit] Failed to read {read_path}: {ex}")

        if not images_data:
            last_error_msg = "input_photo_download_failed"
        else:
            from bot.gemini import generate_image, upscale_image_vertex
            raw_aspect = data.get("aspect") or "1:1"
            aspect = raw_aspect.replace(":", "x")
            if aspect == "auto":
                aspect = "1x1"

            try:
                result_path = await asyncio.wait_for(
                    generate_image(
                        api_key="dummy",
                        prompt=prompt_filled,
                        images_bytes=images_data,
                        aspect_ratio=aspect,
                        quality=quality,
                        key_id=0,
                        db_instance=db,
                        generation_meta=gen_meta,
                    ),
                    timeout=_generation_timeout_seconds()
                )
                if not result_path:
                    last_error_msg = "empty_result"
                else:
                    model_used = gen_meta.get("model_used", "unknown")
                    key_preview_used = gen_meta.get("zveno_key_preview") or gen_meta.get("kie_key_preview") or "zveno"
            except asyncio.TimeoutError:
                last_error_msg = "generation_timeout"
            except Exception as ex:
                key_preview_used = gen_meta.get("zveno_key_preview") or gen_meta.get("kie_key_preview") or key_preview_used
                model_used = gen_meta.get("model_used", model_used)
                last_error_msg = str(ex)

        for p in downloaded_paths:
            try:
                os.remove(p)
            except:
                pass

        if result_path:

            used_project_id = gen_meta.get("project_id")
            if used_project_id:
                await db.increment_gcp_usage(used_project_id)

            await db.subtract_user_balance(user_id, total_price)

            await db.update_daily_usage(user_id)

            pid = await db.generate_pid()
            history_dir = os.path.join("data", "history")
            os.makedirs(history_dir, exist_ok=True)

            db_result_path = f"data/history/result_{pid}.jpg"
            local_result_path = os.path.join(BASE_DIR, db_result_path)
            os.makedirs(os.path.dirname(local_result_path), exist_ok=True)

            try:

                src = result_path if os.path.exists(result_path) else os.path.join(BASE_DIR, str(result_path))
                try:
                    os.replace(src, local_result_path)
                except Exception:
                    import shutil
                    shutil.copyfile(src, local_result_path)
                    try:
                        os.remove(src)
                    except Exception:
                        pass

                local_input_paths = []
                for i, f_id in enumerate(input_photos):
                    if not f_id: continue
                    db_inp_path = f"data/history/input_{pid}_{i}.jpg"
                    local_inp_path = os.path.join(BASE_DIR, db_inp_path)
                    try:
                        f_info = await message.bot.get_file(f_id)
                        await message.bot.download_file(f_info.file_path, local_inp_path)
                        local_input_paths.append(db_inp_path)
                    except:
                        pass
            except Exception as e:
                logger.error(f"Error downloading images for history in edit: {e}")

            from bot.keyboards import result_actions_keyboard, result_actions_own_keyboard
            file_name = os.path.basename(db_result_path)
            if category == "own_variant" or data.get("own_mode"):
                kb = result_actions_own_keyboard(lang, file_name=file_name)
            else:
                kb = result_actions_keyboard(lang, file_name=file_name)

            caption = f"✅ Правки применены!\n\nТекст правок: {edit_text}"
            if len(caption) > 1000:
                caption = caption[:1000] + "…"
            send_src = local_result_path if os.path.exists(local_result_path) else (
                result_path if result_path and os.path.exists(str(result_path)) else None
            )
            if send_src:
                await message.answer_document(
                    document=FSInputFile(send_src),
                    caption=caption,
                    reply_markup=kb,
                )
            else:
                await message.answer(text=caption, reply_markup=kb)

            await db.add_generation_history(
                pid=pid,
                user_id=user_id,
                category=category,
                params=json.dumps({
                    **(data or {}),
                    "_generation_meta": {
                        "key_id": kid_used,
                        "api_key_preview": key_preview_used,
                        "model_used": model_used or "gemini-3-pro-image-preview",
                    },
                }),
                input_photos=json.dumps(input_photos),
                result_photo_id=db_result_path,
                input_paths=json.dumps(local_input_paths),
                result_path=db_result_path,
                prompt=prompt_filled
            )

            await state.set_state(CreateForm.result_ready)
        else:
            msg_l = (last_error_msg or "").lower()
            if any(word in msg_l for word in ["safety", "blocked", "prohibited", "violated", "filtered", "policy", "sensitive"]):
                await message.answer(
                    "🔞 <b>Запрос отклонен нейросетью.</b> Сработал фильтр безопасности контента. Попробуйте загрузить менее откровенное фото или изменить промпт.",
                    parse_mode="HTML")
            elif "all_keys_busy" in msg_l or "kie_keys_busy" in msg_l or "zveno_keys_busy" in msg_l:
                await message.answer("⚠️ Сейчас все ключи заняты активными запросами. Попробуйте через 10-20 секунд.")
            elif "zveno_insufficient_credits" in msg_l or "insufficient credits" in msg_l:
                await message.answer("⚠️ Сервис генерации временно недоступен. Мы уже получили уведомление и работаем над восстановлением. Попробуйте чуть позже.")
            elif "timeout" in msg_l or "таймаут" in msg_l:
                await message.answer(
                    "⚠️ Генерация заняла слишком много времени. Серверы сейчас под высокой нагрузкой. Попробуйте еще раз через 20-60 секунд.")
            elif "image_generation_empty_image" in msg_l or "zveno_empty_image" in msg_l or "empty_result" in msg_l:
                await message.answer("⚠️ Нейросеть вернула ответ без изображения. Попробуйте изменить текст правок.")
            else:
                await message.answer(get_string("gen_error", lang))

    except Exception as e:
        logger.error(f"Critical error in on_result_edit_text: {e}")
        try:
            await message.answer(get_string("gen_error", lang))
        except Exception:
            pass
    finally:
        edit_done.set()
        await asyncio.sleep(0.28)
        progress_task.cancel()
        try:
            await progress_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await process_msg.delete()
        except Exception:
            pass

@router.callback_query(F.data == "result_repeat")
async def on_result_repeat(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    lang = await db.get_user_language(callback.from_user.id)
    if not data:
        await _safe_answer(callback, get_string("session_not_found", lang), show_alert=True)
        return

        # Для "повторить" сбрасываем только фото/результат,
        # а параметры (ракурс, длина, крой, рукав, промпт и т.д.) сохраняем.
    remove_keys = [
        "photo", "photo_id", "user_photo_id", "own_product_photo_id",
        "last_pid", "user_photo_count", "photos", "normal_gen_prompt_msg",
        "result_photo_id", "photo_label",
        "downloaded_paths",
        "collage_front_photo", "collage_back_photo",
    ]

    new_data = {k: v for k, v in data.items() if k not in remove_keys}
    new_data["repeat_mode"] = True

    # Clear stale own_ref_photo_id for modes that resolve the model photo from DB
    _cat = data.get("category", "")
    if _cat not in ("own", "own_variant") and not data.get("own_mode"):
        new_data.pop("own_ref_photo_id", None)

    await state.clear()
    await state.update_data(**new_data)

    category = data.get("category", "")
    if category == "collage" or data.get("collage_mode"):
        await state.set_state(CreateForm.waiting_collage_front_photo)
        await callback.message.answer(get_string("collage_step2_front", lang), reply_markup=back_step_keyboard(lang))
    else:
        await state.set_state(CreateForm.waiting_view)
        await callback.message.answer(get_string("repeat_photo_prompt", lang), reply_markup=back_step_keyboard(lang))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("model_nav:"))
async def on_model_nav(callback: CallbackQuery, db: Database) -> None:
    try:
        parts = callback.data.split(":")
        category = parts[1]
        cloth = parts[2]
        index = int(parts[3])
        logic_category = parts[4] if len(parts) > 4 else None
    except Exception:
        await _safe_answer(callback)
        return

    await _show_models_for_category(callback, db, category, cloth, index, logic_category=logic_category)
    await _safe_answer(callback)


@router.callback_query(F.data == "presets_back")
async def on_presets_back(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    logic_cat = data.get("category")

    if logic_cat == "storefront":
        await on_marketplace_menu(callback, db)
    else:
        await on_ready_presets(callback, db, state)
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("model_search:"))
async def on_model_search(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    parts = callback.data.split(":")
    category = parts[1]
    cloth = parts[2]
    logic_category = parts[3] if len(parts) > 3 else None

    await state.update_data(search_cat=category, search_cloth=cloth, search_logic=logic_category)
    await state.set_state(CreateForm.waiting_model_search)

    lang = await db.get_user_language(callback.from_user.id)
    await callback.message.answer("🔍 Введите номер модели для быстрого перехода (например: 10):")
    await _safe_answer(callback)


@router.message(CreateForm.waiting_model_search)
async def on_model_search_input(message: Message, state: FSMContext, db: Database) -> None:
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("⚠️ Пожалуйста, введите только число.")
        return

    requested_index = int(text) - 1
    if requested_index < 0:
        requested_index = 0

    data = await state.get_data()
    category = data.get("search_cat")
    cloth = data.get("search_cloth")
    logic_category = data.get("search_logic")

    await state.set_state(None)

    total = await db.count_models(category, cloth)
    if total <= 0:
        await message.answer("Модели не найдены.")
        return

    if requested_index >= total:
        requested_index = total - 1

    header_text = _model_header(requested_index, total)
    model = await db.get_model_by_index(category, cloth, requested_index)

    lang = await db.get_user_language(message.from_user.id)
    kb = model_select_keyboard(category, cloth, requested_index, total, lang, logic_category=logic_category)

    if model and model[3]:
        photo = model[3]
        if photo.startswith("AgAC"):
            await message.answer_photo(photo=photo, caption=header_text, reply_markup=kb)
        else:
            from aiogram.types import FSInputFile
            import os
            file_path = photo if os.path.exists(photo) else os.path.join("/app", photo)
            if os.path.exists(file_path):
                await message.answer_photo(photo=FSInputFile(file_path), caption=header_text, reply_markup=kb)
            else:
                await message.answer(header_text, reply_markup=kb)
    else:
        await message.answer(header_text, reply_markup=kb)


@router.callback_query(F.data == "menu_profile")
async def on_menu_profile(callback: CallbackQuery, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    balance = await db.get_user_balance(callback.from_user.id)
    price = await db.get_user_generation_price(callback.from_user.id)
    text = get_string("profile_info", lang, id=callback.from_user.id, balance=balance, price=price)

    await _replace_with_text(callback, text, reply_markup=profile_keyboard(lang))
    await _safe_answer(callback)


@router.callback_query(F.data == "menu_subscription")
async def on_sub_menu(callback: CallbackQuery, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    balance = await db.get_user_balance(callback.from_user.id)
    price = await db.get_user_generation_price(callback.from_user.id)
    text = get_string("top_up_info", lang, id=callback.from_user.id, balance=balance, price=price)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=get_string("contact_admin", lang), url="https://t.me/bnbslow")],
        [InlineKeyboardButton(text=get_string("back", lang), callback_data="menu_profile")]
    ])

    await _replace_with_text(callback, text, reply_markup=kb)
    await _safe_answer(callback)


@router.callback_query(F.data == "menu_history")
async def on_history(callback: CallbackQuery, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    history = await db.list_user_generations(callback.from_user.id, limit=20)

    if not history:
        await callback.answer(get_string("history_empty", lang), show_alert=True)
        return

    await callback.message.answer(get_string("history_title", lang))

    for i, item in enumerate(history, 1):
        pid, result_photo_id, created_at = item

        date_str = created_at if isinstance(created_at, str) else created_at.strftime("%Y-%m-%d %H:%M")

        caption = get_string("history_item", lang, num=i, pid=pid, date=date_str)
        try:
            if result_photo_id.startswith("AgAC"):
                await callback.message.answer_photo(photo=result_photo_id, caption=caption, parse_mode="Markdown")
            elif result_photo_id.startswith("BQAC"):
                await callback.message.answer_document(document=result_photo_id, caption=caption, parse_mode="Markdown")
            else:

                file_path = result_photo_id if os.path.exists(result_photo_id) else os.path.join("/app",
                                                                                                 result_photo_id)
                url = _public_url_for(result_photo_id)
                kb_download = _result_keyboard_with_download(None, url)
                if os.path.exists(file_path):
                    await callback.message.answer_photo(
                        photo=FSInputFile(file_path),
                        caption=caption,
                        parse_mode="Markdown",
                        reply_markup=kb_download,
                    )
                else:
                    await callback.message.answer(caption, parse_mode="Markdown", reply_markup=kb_download)
        except Exception as e:
            logger.error(f"Error sending history item {pid}: {e}")
            await callback.message.answer(caption, parse_mode="Markdown")

        await asyncio.sleep(0.1)

    await _safe_answer(callback)


@router.callback_query(F.data == "menu_settings")
async def on_menu_settings(callback: CallbackQuery, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    from bot.keyboards import settings_keyboard
    await _replace_with_text(callback, get_string("menu_settings", lang), reply_markup=settings_keyboard(lang))
    await _safe_answer(callback)


@router.callback_query(F.data == "settings_lang")
async def on_settings_lang(callback: CallbackQuery, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    from bot.keyboards import language_keyboard
    await _replace_with_text(callback, get_string("select_lang", lang), reply_markup=language_keyboard(lang))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("lang:"))
async def on_set_lang(callback: CallbackQuery, db: Database) -> None:
    new_lang = callback.data.split(":")[1]
    await db.set_user_language(callback.from_user.id, new_lang)
    await on_menu_settings(callback, db)


@router.callback_query(F.data == "menu_howto")
async def on_menu_howto(callback: CallbackQuery, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    text = await db.get_howto_text() or "Инструкция в процессе наполнения."
    await _replace_with_text(callback, text, reply_markup=back_main_keyboard(lang))
    await _safe_answer(callback)


@router.callback_query(F.data == "menu_proxy")
async def on_menu_proxy(callback: CallbackQuery, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    import subprocess
    import base64

    try:

        running_str = await db.get_app_setting("mtproxy_running")
        running = running_str == "1"

        secret = await db.get_app_setting("mtproxy_secret")
        port_str = await db.get_app_setting("mtproxy_port")
        port = int(port_str) if port_str else 8888

        if secret and running:

            server_ip = os.getenv("MTPROXY_SERVER_IP", "130.49.148.147")
            secret_hex = secret.replace("-", "").replace("dd", "")
            if len(secret_hex) == 32:
                link = f"tg://proxy?server={server_ip}&port={port}&secret=dd{secret_hex}"
                text = f"{get_string('proxy_title', lang)}\n\n{get_string('proxy_info', lang)}\n\n`{link}`"
                from bot.keyboards import InlineKeyboardMarkup, InlineKeyboardButton, back_main_keyboard
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=get_string("proxy_get_link", lang), url=link)],
                    [InlineKeyboardButton(text=get_string("back", lang), callback_data="menu_settings")]
                ])
            else:
                text = get_string("proxy_not_available", lang)
                from bot.keyboards import back_main_keyboard
                kb = back_main_keyboard(lang)
        else:
            text = get_string("proxy_not_available", lang)
            from bot.keyboards import back_main_keyboard
            kb = back_main_keyboard(lang)
    except Exception as e:
        import logging
        logging.error(f"MTProxy error: {e}")
        text = get_string("proxy_not_available", lang)
        from bot.keyboards import back_main_keyboard
        kb = back_main_keyboard(lang)

    await _replace_with_text(callback, text, reply_markup=kb)
    await _safe_answer(callback)


@router.callback_query(F.data == "menu_agreement")
async def on_menu_agreement(callback: CallbackQuery, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    text = await db.get_agreement_text()
    if not text or text.strip() == "":
        text = get_string("agreement_not_set", lang)

    user_accepted = await db.get_user_accepted_terms(callback.from_user.id)
    from bot.keyboards import terms_keyboard, back_main_keyboard

    reply_markup = back_main_keyboard(lang) if user_accepted else terms_keyboard(lang)

    await _replace_with_text(callback, text, reply_markup=reply_markup)
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("buy_plan:"))
async def on_buy_plan(callback: CallbackQuery, db: Database) -> None:
    plan_id = int(callback.data.split(":")[1])
    lang = await db.get_user_language(callback.from_user.id)
    plan = await db.get_subscription_plan(plan_id)
    if not plan:
        await _safe_answer(callback, "План не найден.", show_alert=True)
        return

    name = plan[1] if lang == "ru" else (plan[2] if lang == "en" else plan[3])
    desc = plan[4] if lang == "ru" else (plan[5] if lang == "en" else plan[6])
    price = plan[7]

    text = get_string("buy_sub_text", lang, name=name, desc=desc, price=price, id=callback.from_user.id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=get_string("contact_admin", lang), url="https://t.me/bnbslow")],
        [InlineKeyboardButton(text=get_string("back", lang), callback_data="menu_subscription")]
    ])

    await _replace_with_text(callback, text, reply_markup=kb)
    await _safe_answer(callback)


from aiogram.types import FSInputFile


@router.callback_query(F.data.startswith("dl_res:"))
async def download_original_photo(callback: CallbackQuery):
    filename = callback.data.split(":", 1)[1]
    file_path = f"data/history/{filename}"

    if os.path.exists(file_path):
        safe_filename = filename if filename.lower().endswith(('.jpg', '.jpeg', '.png')) else f"{filename}.jpg"

        document = FSInputFile(file_path, filename=safe_filename)
        await callback.message.answer_document(document, caption="📸 Оригинал в высоком качестве")
        await callback.answer()
    else:
        await callback.answer("⚠️ Ошибка: файл не найден на сервере.", show_alert=True)


# ──────────────────────────────────────────────
#  VIDEO GENERATION HANDLERS
# ──────────────────────────────────────────────

@router.callback_query(F.data == "video_menu")
async def on_video_menu(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    await state.clear()
    await _replace_with_text(callback, get_string("video_mode_select", lang),
                             reply_markup=video_mode_keyboard(lang))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("video_mode:"))
async def on_video_mode(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    mode = callback.data.split(":")[1]

    await state.clear()
    await state.update_data(video_mode=mode)
    await state.set_state(CreateForm.waiting_video_photo)
    await callback.message.answer(get_string("video_send_photo", lang),
                                  reply_markup=back_step_keyboard(lang))
    await _safe_answer(callback)


@router.message(CreateForm.waiting_video_photo, F.photo)
async def on_video_photo(message: Message, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(message.from_user.id)
    photo_id = message.photo[-1].file_id
    await state.update_data(video_photo_id=photo_id)
    data = await state.get_data()
    if data.get("video_mode") == "normal":
        await state.set_state(CreateForm.waiting_video_prompt)
        await message.answer(get_string("video_normal_send_prompt", lang),
                             reply_markup=back_step_keyboard(lang))
    else:
        await state.set_state(CreateForm.waiting_video_aspect)
        await message.answer(get_string("video_select_aspect", lang),
                             reply_markup=video_aspect_keyboard(lang))


_VIDEO_PROMPT_MAX_LEN = 1000


@router.message(CreateForm.waiting_video_prompt)
async def on_video_prompt(message: Message, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(message.from_user.id)
    text = (message.text or "").strip()
    if not text:
        await message.answer(get_string("video_normal_send_prompt", lang),
                             reply_markup=back_step_keyboard(lang))
        return
    if len(text) > _VIDEO_PROMPT_MAX_LEN:
        await message.answer(get_string("video_prompt_too_long", lang),
                             reply_markup=back_step_keyboard(lang))
        return
    await state.update_data(video_user_prompt=text)
    await state.set_state(CreateForm.waiting_video_aspect)
    await message.answer(get_string("video_select_aspect", lang),
                         reply_markup=video_aspect_keyboard(lang))


@router.callback_query(F.data.startswith("video_aspect:"))
async def on_video_aspect(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    raw = callback.data
    if raw == "video_aspect:4:5":
        aspect = "4:5"
    else:
        aspect = "9:16"
    await state.update_data(video_aspect=aspect)
    await state.set_state(CreateForm.waiting_video_duration)
    await _replace_with_text(callback, get_string("video_select_duration", lang),
                             reply_markup=video_duration_keyboard(lang))
    await _safe_answer(callback)


VIDEO_PRICES = {5: 65, 10: 99}

_VIDEO_PROGRESS_STEPS = [
    (0,   "📸 Подготовка фото..."),
    (15,  "🔍 Анализ сцены..."),
    (40,  "🎞 Генерация кадров..."),
    (80,  "🎨 Рендеринг видео..."),
    (140, "✨ Финализация..."),
]


def _video_progress_text(elapsed: float, duration: int) -> str:
    """Build progress bar text based on elapsed seconds."""
    expected = 120 if duration == 5 else 200
    pct = min(95, int(elapsed / expected * 100))
    filled = pct // 5
    empty = 20 - filled
    bar = "▓" * filled + "░" * empty

    step_label = _VIDEO_PROGRESS_STEPS[0][1]
    for threshold, label in _VIDEO_PROGRESS_STEPS:
        if elapsed >= threshold:
            step_label = label

    return f"🎬 Генерация видео\n\n{bar} {pct}%\n\n{step_label}"


_VIDEO_MODE_LABELS = {"normal": "🎬 Обычная генерация", "clothing": "👗 Анимация одежды"}


@router.callback_query(F.data.startswith("video_dur:"))
async def on_video_duration(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    duration = int(callback.data.split(":")[1])
    await state.update_data(video_duration=duration)

    data = await state.get_data()
    mode = data.get("video_mode", "normal")
    aspect = data.get("video_aspect", "9:16")
    price = VIDEO_PRICES.get(duration, 65)

    mode_label = _VIDEO_MODE_LABELS.get(mode, mode)
    user_prompt_preview = data.get("video_user_prompt", "")
    prompt_line = ""
    if user_prompt_preview:
        short = user_prompt_preview[:80] + ("…" if len(user_prompt_preview) > 80 else "")
        prompt_line = f"✏️ Описание: {short}\n"
    confirm_text = (
        f"📋 Подтвердите генерацию видео:\n\n"
        f"📸 Фото: загружено\n"
        f"🎬 Режим: {mode_label}\n"
        f"{prompt_line}"
        f"📺 Формат: {aspect}\n"
        f"⏱ Длительность: {duration} сек\n"
        f"💰 Стоимость: {price} ₽"
    )

    await state.set_state(CreateForm.waiting_video_confirm)
    await _replace_with_text(callback, confirm_text,
                             reply_markup=video_confirm_keyboard(lang))
    await _safe_answer(callback)


@router.callback_query(F.data == "video_confirm:yes")
async def on_video_confirm(callback: CallbackQuery, state: FSMContext, db: Database, bot: Bot) -> None:
    user_id = callback.from_user.id
    if gen_locks[user_id].locked():
        await _safe_answer(callback)
        return
    async with gen_locks[user_id]:
        await _on_video_confirm_real(callback, state, db, bot)


async def _notify_admins_video_error(db: Database, bot: Bot, user_id: int, err: str) -> None:
    """Throttled admin alert for video failures that need operator action (e.g. provider out of funds)."""
    try:
        cooldown_s = 300
        now_ts = int(time.time())
        try:
            last_ts = int(str(await db.get_app_setting("tg_video_error_notify_last_ts", "0") or "0").strip() or "0")
        except Exception:
            last_ts = 0
        if now_ts - last_ts < cooldown_s:
            return
        try:
            await db.set_app_setting("tg_video_error_notify_last_ts", str(now_ts))
        except Exception:
            pass
        settings = load_settings()
        admin_ids = settings.admin_ids or [1440716472, 916948327]
        short = (err or "").strip()[:600]
        text = (
            "⚠️ Видео: провайдер отклонил генерацию\n"
            f"User: {user_id}\n"
            "Похоже, у провайдера видео недостаточно средств — пополните баланс.\n"
            f"Error: {short}"
        )
        for aid in admin_ids:
            try:
                await bot.send_message(chat_id=int(aid), text=text)
            except Exception:
                pass
    except Exception:
        pass


async def _on_video_confirm_real(callback: CallbackQuery, state: FSMContext, db: Database, bot: Bot) -> None:
    lang = await db.get_user_language(callback.from_user.id)
    user_id = callback.from_user.id
    data = await state.get_data()
    video_mode = data.get("video_mode", "normal")
    duration = data.get("video_duration", 5)
    aspect_ratio = data.get("video_aspect", "9:16")

    if await db.get_maintenance():
        settings = load_settings()
        if user_id not in (settings.admin_ids or []):
            await _safe_answer(callback, get_string("maintenance_alert", lang), show_alert=True)
            return

    if await db.get_user_blocked(user_id):
        await _safe_answer(callback, get_string("user_blocked", lang), show_alert=True)
        return

    price = VIDEO_PRICES.get(duration, 65)
    balance = await db.get_user_balance(user_id)
    if balance < price:
        await _safe_answer(callback,
                           get_string("video_insufficient_balance", lang, price=price, balance=balance),
                           show_alert=True)
        return

    user_prompt = (data.get("video_user_prompt") or "").strip()
    if video_mode == "clothing":
        prompt = (await db.get_app_setting("video_clothing_prompt") or "").strip()
    else:
        prompt = user_prompt
    logger.info("[VIDEO] user=%s mode=%s prompt_len=%d user_prompt_len=%d",
                user_id, video_mode, len(prompt), len(user_prompt))

    progress_msg = await callback.message.answer(_video_progress_text(0, duration))
    await _safe_answer(callback)

    async def _update_progress(elapsed: float, status: str):
        try:
            new_text = _video_progress_text(elapsed, duration)
            await progress_msg.edit_text(new_text)
        except Exception:
            pass

    from bot.gemini import acquire_zveno_key_slot, release_zveno_key_slot
    from bot.video_gen import generate_video, VideoError, VideoRateLimitError, VideoTimeoutError
    from bot.telegram_images import telegram_file_ids_to_bytes_list

    z_key = await acquire_zveno_key_slot()
    if not z_key:
        await progress_msg.edit_text(get_string("video_err_no_keys", lang))
        return

    try:
        photo_ids = [data.get("video_photo_id")]
        images_bytes = await telegram_file_ids_to_bytes_list(bot, photo_ids, base_dir=BASE_DIR)

        if not images_bytes:
            await progress_msg.edit_text(get_string("video_failed", lang))
            return

        video_path, job_data = await asyncio.wait_for(
            generate_video(
                api_key=z_key,
                prompt=prompt,
                duration=duration,
                image_bytes=images_bytes[0],
                aspect_ratio=aspect_ratio,
                on_progress=_update_progress,
            ),
            timeout=660,
        )

        await db.subtract_user_balance(user_id, price, reason="video_generation")

        video_file = FSInputFile(video_path)
        await callback.message.answer_video(video_file, caption=get_string("video_success", lang))
        try:
            await progress_msg.delete()
        except Exception:
            pass

        cost = None
        if isinstance(job_data, dict) and isinstance(job_data.get("usage"), dict):
            cost = job_data["usage"].get("cost")
        logger.info("[VIDEO] user=%s mode=%s dur=%ss aspect=%s price=%s zveno_cost=%s",
                    user_id, video_mode, duration, aspect_ratio, price, cost)

        video_pid = f"VID{user_id}_{int(time.time())}"
        history_video_path = ""
        try:
            import shutil
            history_dir = "/app/data/history"
            os.makedirs(history_dir, exist_ok=True)
            video_filename = f"{video_pid}.mp4"
            dest = os.path.join(history_dir, video_filename)
            shutil.copy2(video_path, dest)
            history_video_path = video_filename
        except Exception as e:
            logger.warning("[VIDEO] failed to copy video to history: %s", e)
        try:
            await db.add_generation_history(
                pid=video_pid, user_id=user_id,
                category=f"video_{video_mode}",
                params=json.dumps({"mode": video_mode, "duration": duration,
                                   "aspect_ratio": aspect_ratio, "price": price,
                                   "zveno_cost": cost}),
                input_photos=json.dumps([data.get("video_photo_id", "")]),
                result_photo_id="",
                input_paths="",
                result_path=history_video_path,
                prompt=prompt[:2000] if prompt else "",
            )
        except Exception as e:
            logger.warning("[VIDEO] failed to save history: %s", e)

    except asyncio.TimeoutError:
        logger.error("[VIDEO] timeout user=%s", user_id)
        await progress_msg.edit_text(get_string("video_err_timeout", lang))
    except VideoError as exc:
        logger.error("[VIDEO] error user=%s mode=%s cat=%s: %s", user_id, video_mode, exc.category, exc, exc_info=True)
        err_key = {
            "rate_limit": "video_err_rate_limit",
            "timeout": "video_err_timeout",
            "safety": "video_err_safety",
            "insufficient_funds": "video_err_unavailable",
        }.get(exc.category, "video_failed")
        await progress_msg.edit_text(get_string(err_key, lang))
        if exc.category == "insufficient_funds":
            await _notify_admins_video_error(db, bot, user_id, str(exc))
    except Exception as exc:
        logger.error("[VIDEO] unexpected error user=%s: %s", user_id, exc, exc_info=True)
        await progress_msg.edit_text(get_string("video_failed", lang))
    finally:
        await release_zveno_key_slot(z_key)
        await state.clear()
