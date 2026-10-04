import logging
import math
import os
import time
import asyncio
import json
import uuid
import hashlib
from dataclasses import dataclass, field
from urllib.parse import quote

import aiosqlite
import httpx
from vkbottle import Keyboard, KeyboardButtonColor, Text
from vkbottle.bot import BotLabeler
from vkbottle.bot import Message
from vkbottle.dispatch.rules import ABCRule
from vkbottle.exception_factory.base_exceptions import VKAPIError

from bot.db import Database
from bot.gemini import generate_image
from bot.key_dispatcher import (
    acquire_key_lock_with_wait,
    order_keys_after_last_used,
    release_key_lock,
)
from bot.prompt_builder import build_final_prompt
from bot.strings import get_string
from vk_bot.context import get_db
from vk_bot.keyboards import (
    aspect_ratio_keyboard,
    back_to_main_keyboard,
    infographics_keyboard,
    language_keyboard,
    main_menu_keyboard,
    marketplace_keyboard,
    preset_gender_keyboard,
    profile_keyboard,
    settings_keyboard,
    support_keyboard,
    terms_keyboard,
    topup_keyboard,
)

router = BotLabeler()
logger = logging.getLogger(__name__)

NORMAL_PHOTOS_DONE_TEXT = "Далее"
NORMAL_PHOTOS_GO_PROMPT_TEXT = "Перейти к промпту"
QUALITY_1K_PREFIX = "1K"
QUALITY_4K_PREFIX = "4K"
DUPLICATE_EVENT_TTL_SECONDS = 10

STATE_IDLE = "idle"
STATE_WAIT_PHOTO = "wait_photo"
STATE_WAIT_PROMPT = "wait_prompt"
STATE_WAIT_ASPECT = "wait_aspect"
STATE_CONSTRUCTOR_STEP = "constructor_step"
STATE_CONSTRUCTOR_CONFIRM = "constructor_confirm"
STATE_WAIT_QUALITY = "wait_quality"
STATE_NORMAL_CONFIRM = "normal_confirm"
STATE_SUPPORT = "support"
STATE_PRESET_GENDER = "preset_gender"
STATE_PRESET_MODEL = "preset_model"
STATE_RESULT_READY = "result_ready"
STATE_WAIT_EDIT_TEXT = "wait_edit_text"
STATE_WAIT_REPEAT_PHOTO = "wait_repeat_photo"
STATE_TTL_SECONDS = 120 * 60


@dataclass
class VkUserState:
    stage: str = STATE_IDLE
    category: str | None = None
    image_bytes_list: list[bytes] | None = None
    constructor_category_id: int | None = None
    constructor_steps: list[tuple] = field(default_factory=list)
    current_step_index: int = 0
    current_step_id: int | None = None
    current_step_key: str | None = None
    waiting_custom_for: str | None = None
    step_values: dict[str, str] = field(default_factory=dict)
    step_labels: dict[str, str] = field(default_factory=dict)
    step_photos: dict[str, list[bytes]] = field(default_factory=dict)
    model_choices: dict[int, tuple[int, str, int, str | None]] = field(default_factory=dict)
    model_browser_items: list[tuple[int, str, int, str | None]] = field(default_factory=list)
    model_browser_index: int = 0
    selected_quality: str | None = None
    quality_flow: str | None = None
    last_incoming_msg_id: int | None = None
    last_incoming_fingerprint: str | None = None
    last_incoming_at: float = 0.0
    last_incoming_text: str | None = None
    last_incoming_text_at: float = 0.0
    last_generation_prompt: str | None = None
    last_generation_images: list[bytes] = field(default_factory=list)
    last_generation_category: str | None = None
    last_generation_flow: str | None = None
    last_generation_aspect: str | None = None
    last_generation_quality: str | None = None
    updated_at: float = 0.0


_states: dict[int, VkUserState] = {}
_recent_incoming_events: dict[str, float] = {}
from collections import defaultdict
_vk_gen_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


class IsPrivate(ABCRule):
    async def check(self, message: Message) -> bool:
        return message.peer_id == message.from_id


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def _is_start(text: str) -> bool:
    return text in {"/start", "start", "старт"}


def _is_help(text: str) -> bool:
    return text in {"/help", "help", "помощь"}


def _is_reset_text(text: str) -> bool:
    return text in {
        _norm("🔄 Сброс"),
        _norm("сброс"),
        _norm("/reset"),
        _norm("reset"),
        _norm("отмена"),
        _norm("cancel"),
    }


def _normalize_vk_contact_url(contact: str | None, *, allow_handle: bool = True) -> str | None:
    value = (contact or "").strip()
    if not value:
        return None
    lower = value.lower()
    if lower.startswith(("https://vk.com/", "http://vk.com/", "https://vk.me/", "http://vk.me/")):
        return value
    if lower.startswith(("vk.com/", "vk.me/")):
        return f"https://{value}"
    if lower.startswith(("http://", "https://", "tg://")) or "t.me/" in lower:
        return None
    if not allow_handle:
        return None
    handle = value.lstrip("@").strip("/")
    if not handle or any(ch.isspace() for ch in handle):
        return None
    return f"https://vk.com/{handle}"


def _vk_dialog_target_from_url(contact_url: str | None) -> str | None:
    value = (contact_url or "").strip()
    if not value:
        return None
    lower = value.lower()
    for prefix in ("https://vk.com/", "http://vk.com/", "https://vk.me/", "http://vk.me/"):
        if lower.startswith(prefix):
            slug = value[len(prefix):].strip().strip("/")
            if not slug or "/" in slug or "?" in slug:
                return None
            if slug.lower().startswith("id") and slug[2:].isdigit():
                return slug[2:]
            return slug
    return None


def _vk_topup_chat_url(contact_url: str | None, user_id: int) -> str | None:
    target = _vk_dialog_target_from_url(contact_url)
    if not target:
        return contact_url
    message_text = f"Здравствуйте, хочу пополнить баланс. Мой id - {user_id}"
    if target.isdigit():
        return f"https://vk.com/write{target}?message={quote(message_text)}"
    return f"https://vk.com/im?sel={quote(target)}&message={quote(message_text)}"


def _vk_topup_message_text(user_id: int) -> str:
    return f"Здравствуйте, хочу пополнить баланс. Мой id - {user_id}"


def _default_vk_topup_contact_url() -> str | None:
    for key in ("VK_TOPUP_ADMIN_URL", "VK_ADMIN_URL"):
        url = _normalize_vk_contact_url(os.getenv(key), allow_handle=True)
        if url:
            return url
    raw_ids = os.getenv("VK_TOPUP_ADMIN_ID") or os.getenv("VK_ADMIN_IDS") or ""
    for part in raw_ids.replace(";", ",").split(","):
        admin_id = part.strip()
        if admin_id.isdigit():
            return f"https://vk.com/id{admin_id}"
    return None


async def _ensure_user(db: Database, user_id: int) -> None:
    await db.upsert_user(user_id=user_id, username=None, first_name=None, last_name=None, referrer_id=None, source="vk")


def _get_state(user_id: int) -> VkUserState:
    st = _states.get(user_id)
    if st is None:
        st = VkUserState(stage=STATE_IDLE, image_bytes_list=None, updated_at=time.time())
        _states[user_id] = st
        return st
    if (time.time() - st.updated_at) > STATE_TTL_SECONDS:
        st = VkUserState(stage=STATE_IDLE, updated_at=time.time())
        _states[user_id] = st
    st.updated_at = time.time()
    return st


def _reset_state(user_id: int) -> None:
    _states[user_id] = VkUserState(stage=STATE_IDLE, category=None, image_bytes_list=None, updated_at=time.time())


def _incoming_fingerprint(message: Message) -> str:
    peer_id = getattr(message, "peer_id", "")
    msg_id = getattr(message, "id", None)
    conv_id = getattr(message, "conversation_message_id", None)
    if msg_id is not None or conv_id is not None:
        return f"id:{peer_id}:{msg_id}:{conv_id}"
    attachments = getattr(message, "attachments", None) or []
    att_sig = ",".join(str(getattr(att, "type", "")) for att in attachments)
    return f"body:{peer_id}:{getattr(message, 'from_id', '')}:{message.text or ''}:{len(attachments)}:{att_sig}"


def _vk_random_id(message: Message, text: str, keyboard: str | None = None, attachment: str | None = None) -> int:
    payload = "|".join(
        [
            _incoming_fingerprint(message),
            text or "",
            keyboard or "",
            attachment or "",
        ]
    )
    digest = hashlib.blake2s(payload.encode("utf-8"), digest_size=4).digest()
    rid = int.from_bytes(digest, "big") & 0x7FFFFFFF
    return rid or 1


def _is_duplicate_incoming_event(fingerprint: str, now: float) -> bool:
    expired = [key for key, ts in _recent_incoming_events.items() if now - ts > DUPLICATE_EVENT_TTL_SECONDS]
    for key in expired:
        _recent_incoming_events.pop(key, None)
    if fingerprint and fingerprint in _recent_incoming_events:
        return True
    if fingerprint:
        _recent_incoming_events[fingerprint] = now
    return False


async def _reply(message: Message, text: str, keyboard: str | None = None) -> bool:
    logger.info("[REPLY] peer=%s rid_text=%.60s kb=%s", message.peer_id, text.replace('\n', ' ')[:60], bool(keyboard))
    rid = _vk_random_id(message, text, keyboard=keyboard)
    try:
        if keyboard:
            logger.info("[SEND] peer=%s WITH keyboard rid=%s", message.peer_id, rid)
            sent_id = await message.ctx_api.messages.send(peer_id=message.peer_id, random_id=rid, message=text, keyboard=keyboard)
        else:
            logger.info("[SEND] peer=%s NO keyboard rid=%s", message.peer_id, rid)
            sent_id = await message.ctx_api.messages.send(peer_id=message.peer_id, random_id=rid, message=text)
        logger.info("[SEND] peer=%s SUCCESS sent_msg_id=%s", message.peer_id, sent_id)
        return True
    except VKAPIError as e:
        err = str(e).lower()
        logger.warning("[SEND] peer=%s VKAPIError: %s", message.peer_id, str(e)[:200])
        if (
            "chat bot feature" in err
            or "error_code=912" in err
            or "error_code=911" in err
            or "keyboard format is invalid" in err
            or "too much rows" in err
        ):
            logger.info("[SEND] peer=%s RETRY without keyboard rid=%s", message.peer_id, rid)
            await message.ctx_api.messages.send(peer_id=message.peer_id, random_id=rid, message=text)
            logger.info("[SEND] peer=%s RETRY SUCCESS", message.peer_id)
            return False
        else:
            raise
    except Exception as e:
        logger.warning("[SEND] peer=%s Exception: %s", message.peer_id, str(e)[:200])
        if "chat bot feature" in str(e).lower():
            logger.info("[SEND] peer=%s RETRY without keyboard rid=%s", message.peer_id, rid)
            await message.ctx_api.messages.send(peer_id=message.peer_id, random_id=rid, message=text)
            return False
        else:
            raise


async def _reply_with_id(message: Message, text: str, keyboard: str | None = None) -> int | None:
    rid = _vk_random_id(message, text, keyboard=keyboard)
    try:
        if keyboard:
            msg_id = await message.ctx_api.messages.send(
                peer_id=message.peer_id, random_id=rid, message=text, keyboard=keyboard,
            )
        else:
            msg_id = await message.ctx_api.messages.send(
                peer_id=message.peer_id, random_id=rid, message=text,
            )
        return int(msg_id) if msg_id else None
    except Exception:
        logger.exception("VK _reply_with_id failed peer_id=%s", message.peer_id)
        return None


async def _edit_vk_message(message: Message, msg_id: int, text: str) -> bool:
    try:
        await message.ctx_api.messages.edit(
            peer_id=message.peer_id, message_id=msg_id, message=text,
        )
        return True
    except Exception:
        return False


async def _delete_vk_message(message: Message, msg_id: int) -> None:
    try:
        await message.ctx_api.messages.delete(
            message_ids=[msg_id], delete_for_all=True,
        )
    except Exception:
        pass


async def _upload_photo_via_messages(message: Message, data: bytes, filename: str) -> str | None:
    """Legacy photos.getMessagesUploadServer + saveMessagesPhoto flow.

    Note: VK migrated this app's messages-photo upload to the new
    pu.vk.ru/gu/photo/v2/bulk_upload endpoint, whose response can no longer be
    persisted via saveMessagesPhoto. This helper therefore returns None in
    practice today, but is kept so photo attachments resume automatically if VK
    restores the legacy behaviour. Callers fall back to _upload_doc_attachment.
    """
    try:
        if not data:
            return None
        server = await message.ctx_api.photos.get_messages_upload_server(peer_id=message.peer_id)
        upload_url = getattr(server, "upload_url", None) or (server.get("upload_url") if isinstance(server, dict) else None)
        if not upload_url:
            return None
        timeout = httpx.Timeout(connect=20.0, read=60.0, write=60.0, pool=20.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(upload_url, files={"photo": (filename, data, "image/jpeg")})
        if resp.status_code != 200:
            return None
        up = resp.json()
        photo = up.get("photo")
        server_id = up.get("server")
        h = up.get("hash")
        if not (photo and server_id and h):
            return None
        saved = await message.ctx_api.photos.save_messages_photo(photo=photo, server=server_id, hash=h)
        if not saved:
            return None
        item = saved[0]
        owner_id = getattr(item, "owner_id", None) or (item.get("owner_id") if isinstance(item, dict) else None)
        pid = getattr(item, "id", None) or (item.get("id") if isinstance(item, dict) else None)
        access_key = getattr(item, "access_key", None) or (item.get("access_key") if isinstance(item, dict) else None)
        if owner_id is None or pid is None:
            return None
        if access_key:
            return f"photo{owner_id}_{pid}_{access_key}"
        return f"photo{owner_id}_{pid}"
    except Exception:
        # Non-fatal: caller falls back to the document upload path.
        logger.debug("VK messages-photo upload unavailable, will fall back to doc", exc_info=True)
        return None


async def _upload_doc_attachment(message: Message, data: bytes, filename: str = "result.jpg") -> str | None:
    """Upload an image via the still-working legacy document endpoint.

    docs.getMessagesUploadServer/docs.save are unaffected by the photo-upload
    migration, so this reliably delivers the image (VK renders image documents
    with an inline preview). Returns a ``doc{owner}_{id}`` attachment string.
    """
    try:
        if not data:
            return None
        server = await message.ctx_api.docs.get_messages_upload_server(peer_id=message.peer_id, type="doc")
        upload_url = getattr(server, "upload_url", None) or (server.get("upload_url") if isinstance(server, dict) else None)
        if not upload_url:
            return None
        timeout = httpx.Timeout(connect=20.0, read=60.0, write=60.0, pool=20.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(upload_url, files={"file": (filename, data, "image/jpeg")})
        if resp.status_code != 200:
            return None
        up = resp.json()
        file_token = up.get("file")
        if not file_token:
            return None
        saved = await message.ctx_api.docs.save(file=file_token, title=filename)
        doc = getattr(saved, "doc", None)
        if doc is None and isinstance(saved, dict):
            doc = saved.get("doc")
        if doc is None:
            return None
        owner_id = getattr(doc, "owner_id", None) or (doc.get("owner_id") if isinstance(doc, dict) else None)
        did = getattr(doc, "id", None) or (doc.get("id") if isinstance(doc, dict) else None)
        access_key = getattr(doc, "access_key", None) or (doc.get("access_key") if isinstance(doc, dict) else None)
        if owner_id is None or did is None:
            return None
        if access_key:
            return f"doc{owner_id}_{did}_{access_key}"
        return f"doc{owner_id}_{did}"
    except Exception:
        logger.exception("VK upload doc fallback failed")
        return None


async def _upload_result_photo_attachment(message: Message, file_path: str) -> str | None:
    try:
        import os

        if not file_path:
            return None
        p = file_path
        if not os.path.isabs(p):
            p = os.path.join("/app", str(file_path).lstrip("/"))
        if not os.path.exists(p):
            return None

        with open(p, "rb") as f:
            data = f.read()

        attachment = await _upload_photo_via_messages(message, data, "result.jpg")
        if attachment:
            return attachment
        return await _upload_doc_attachment(message, data, "result.jpg")
    except Exception:
        logger.exception("VK upload result photo failed")
        return None


async def _upload_photo_bytes_attachment(message: Message, photo_bytes: bytes) -> str | None:
    try:
        if not photo_bytes:
            return None
        attachment = await _upload_photo_via_messages(message, photo_bytes, "model.jpg")
        if attachment:
            return attachment
        return await _upload_doc_attachment(message, photo_bytes, "model.jpg")
    except Exception:
        logger.exception("VK upload photo bytes failed")
        return None


async def _reply_with_attachment(message: Message, text: str, attachment: str, keyboard: str | None = None) -> bool:
    
    rid = _vk_random_id(message, text, keyboard=keyboard, attachment=attachment)
    try:
        if keyboard:
            await message.ctx_api.messages.send(
                peer_id=message.peer_id,
                random_id=rid,
                message=text,
                keyboard=keyboard,
                attachment=attachment,
            )
        else:
            await message.ctx_api.messages.send(peer_id=message.peer_id, random_id=rid, message=text, attachment=attachment)
        return True
    except VKAPIError as e:
        err = str(e).lower()
        if (
            "chat bot feature" in err
            or "error_code=912" in err
            or "error_code=911" in err
            or "keyboard format is invalid" in err
            or "too much rows" in err
        ):
            await message.ctx_api.messages.send(peer_id=message.peer_id, random_id=rid, message=text, attachment=attachment)
            return False
        raise
    except Exception as e:
        if "chat bot feature" in str(e).lower():
            await message.ctx_api.messages.send(peer_id=message.peer_id, random_id=rid, message=text, attachment=attachment)
            return False
        raise


async def _get_lang(db: Database, user_id: int) -> str:
    lang = await db.get_user_language(user_id)
    if lang not in {"ru", "en", "vi"}:
        return "ru"
    return lang


async def _send_main_menu(message: Message, db: Database, user_id: int) -> None:
    lang = await _get_lang(db, user_id)
    ok = await _reply(message, get_string("main_menu_title", lang), keyboard=main_menu_keyboard(lang))
    if not ok:
        if lang == "en":
            fallback = (
                "VK buttons are disabled in community settings.\n"
                "Use text commands:\n"
                "- Profile\n- Top up balance\n- Support\n- Instructions\n- Settings\n"
                "- NORMAL GENERATION\n- Marketplaces"
            )
        elif lang == "vi":
            fallback = (
                "Nút VK đang bị tắt trong cài đặt cộng đồng.\n"
                "Hãy dùng lệnh văn bản:\n"
                "- Hồ sơ\n- Nạp tiền\n- Hỗ trợ kỹ thuật\n- Hướng dẫn\n- Cài đặt\n"
                "- TẠO ẢNH THƯỜNG\n- Cho sàn thương mại"
            )
        else:
            fallback = (
                "Кнопки VK выключены в настройках сообщества.\n"
                "Используйте текстовые команды:\n"
                "- Профиль\n- Пополнить баланс\n- Тех.поддержка\n- Инструкция\n- Настройки\n"
                "- ОБЫЧНАЯ ГЕНЕРАЦИЯ\n- Для маркетплейсов"
            )
        await _reply(message, fallback)


async def _send_marketplace_menu(message: Message, db: Database, user_id: int) -> None:
    lang = await _get_lang(db, user_id)
    try:
        enabled = await db.list_categories_enabled()
    except Exception:
        enabled = {}
    await _reply(message, get_string("marketplace_menu", lang), keyboard=marketplace_keyboard(enabled, lang))


async def _send_infographics_menu(message: Message, db: Database, user_id: int) -> None:
    lang = await _get_lang(db, user_id)
    try:
        enabled = await db.list_categories_enabled()
    except Exception:
        enabled = {}
    await _reply(message, get_string("select_infographic_type", lang), keyboard=infographics_keyboard(enabled, lang))


async def _start_presets_flow(message: Message, db: Database, user_id: int, lang: str) -> None:
    try:
        enabled = await db.list_categories_enabled()
    except Exception:
        enabled = {}
    st = _get_state(user_id)
    st.stage = STATE_PRESET_GENDER
    st.category = "presets"
    st.step_values = {"category": "presets"}
    st.step_labels = {}
    st.step_photos = {}
    st.model_choices = {}
    st.model_browser_items = []
    st.model_browser_index = 0
    st.selected_quality = None
    st.quality_flow = None
    await _reply(message, get_string("cat_presets", lang), keyboard=preset_gender_keyboard(enabled, lang))


def _preset_gender_by_text(text: str, lang: str) -> str | None:
    mapping = {
        _norm(get_string("cat_female", lang)): "female",
        _norm(get_string("cat_male", lang)): "male",
        _norm(get_string("gender_boy", lang)): "boy",
        _norm(get_string("gender_girl", lang)): "girl",
    }
    return mapping.get(text)


async def _get_category_prefix_prompt(db: Database, category: str | None) -> str:
    if not category:
        return ""
    key_map = {
        "presets": "presets_prompt",
        "random": "random_prompt",
        "random_other": "random_other_prompt",
        "infographic_clothing": "infographic_clothing_prompt",
        "infographic_other": "infographic_other_prompt",
        "storefront": "storefront_prompt",
        "whitebg": "whitebg_prompt",
        "own": "own_prompt",
        "own_variant": "own_variant_prompt",
        "collage": "collage_prompt",
    }
    setting_key = key_map.get(category)
    if not setting_key:
        return ""
    return (await db.get_app_setting(setting_key, "") or "").strip()


async def _send_agreement(message: Message, db: Database, user_id: int) -> None:
    lang = await _get_lang(db, user_id)
    agreement_text = await db.get_app_setting("agreement_text", "") or ""
    body = agreement_text.strip() or get_string("agreement", lang)
    await _reply(message, body, keyboard=back_to_main_keyboard(lang))


async def _send_support(message: Message, db: Database, user_id: int) -> None:
    lang = await _get_lang(db, user_id)
    _reset_state(user_id)
    await _reply(
        message,
        get_string("support_contact_message", lang),
        keyboard=back_to_main_keyboard(lang),
    )


async def _send_howto(message: Message, db: Database, user_id: int) -> None:
    lang = await _get_lang(db, user_id)
    howto_text = (await db.get_app_setting("howto_text", "") or "").strip()
    body = howto_text or get_string("how_to", lang)
    await _reply(message, body, keyboard=back_to_main_keyboard(lang))


async def _send_profile(message: Message, db: Database, user_id: int) -> None:
    lang = await _get_lang(db, user_id)
    balance = await db.get_user_balance(user_id)
    price = await db.get_user_generation_price(user_id)
    text = get_string("profile_info", lang).format(id=user_id, balance=balance, price=price)
    await _reply(message, text, keyboard=profile_keyboard(lang))


async def _send_topup(message: Message, db: Database, user_id: int) -> None:
    lang = await _get_lang(db, user_id)
    balance = await db.get_user_balance(user_id)
    price = await db.get_user_generation_price(user_id)
    text = get_string("top_up_info", lang).format(id=user_id, balance=balance, price=price)
    vk_contact = (await db.get_app_setting("support_contact_vk", "") or "").strip()
    contact_url = _normalize_vk_contact_url(vk_contact, allow_handle=True)
    contact = vk_contact
    if not contact_url:
        contact_url = _default_vk_topup_contact_url()
        contact = contact_url or contact
    if not contact:
        contact = (await db.get_app_setting("support_contact", "") or "").strip()
        contact_url = _normalize_vk_contact_url(contact, allow_handle=False)
    if not contact:
        contact = "@bnbslow"
    contact_url = _vk_topup_chat_url(contact_url, user_id)
    fallback_text = _vk_topup_message_text(user_id)
    await _reply(message, f"{text}\n{contact}\n\nТекст для сообщения:\n{fallback_text}", keyboard=topup_keyboard(contact_url, lang))


async def _send_history(message: Message, db: Database, user_id: int) -> None:
    lang = await _get_lang(db, user_id)
    try:
        async with aiosqlite.connect(db._db_path) as conn:
            async with conn.execute(
                """
                SELECT pid, COALESCE(NULLIF(result_path, ''), NULLIF(result_photo_id, '')) AS result_ref, created_at
                FROM generation_history
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT 20
                """,
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
    except Exception:
        logger.exception("VK history query failed user_id=%s", user_id)
        await _reply(message, get_string("vk_temp_error", lang), keyboard=back_to_main_keyboard(lang))
        return

    if not rows:
        await _reply(message, get_string("history_empty", lang), keyboard=back_to_main_keyboard(lang))
        return

    await _reply(message, get_string("history_title", lang), keyboard=back_to_main_keyboard(lang))
    for i, row in enumerate(rows, 1):
        pid, result_ref, created_at = row
        caption = get_string("history_item", lang).format(num=i, pid=pid, date=created_at)
        ref = str(result_ref or "").strip()
        attachment = None
        if ref and not ref.startswith(("AgAC", "BQAC")):
            file_path = ref
            if not os.path.isabs(file_path):
                file_path = os.path.join("/app", file_path)
            if os.path.exists(file_path):
                attachment = await _upload_result_photo_attachment(message, file_path)
        if attachment:
            await _reply_with_attachment(message, caption, attachment=attachment)
        else:
            await _reply(message, caption)
        await asyncio.sleep(0.1)


async def _send_proxy(message: Message, db: Database, user_id: int) -> None:
    lang = await _get_lang(db, user_id)
    running = (await db.get_app_setting("mtproxy_running", "0") or "0") == "1"
    secret = (await db.get_app_setting("mtproxy_secret", "") or "").strip()
    port = (await db.get_app_setting("mtproxy_port", "8888") or "8888").strip()
    server_ip = (await db.get_app_setting("mtproxy_server_ip", "") or "").strip()
    if not server_ip:
        import os

        server_ip = os.getenv("MTPROXY_SERVER_IP", "130.49.148.147")

    if not running or not secret:
        await _reply(message, get_string("proxy_not_available", lang), keyboard=back_to_main_keyboard(lang))
        return

    secret_hex = secret.replace("-", "")
    if secret_hex.startswith("dd") and len(secret_hex) == 34:
        secret_hex = secret_hex[2:]
    if len(secret_hex) != 32:
        await _reply(message, get_string("proxy_not_available", lang), keyboard=back_to_main_keyboard(lang))
        return

    link = f"tg://proxy?server={server_ip}&port={port}&secret=dd{secret_hex}"
    text = f"{get_string('proxy_title', lang)}\n\n{get_string('proxy_link', lang)}\n{link}"
    await _reply(message, text, keyboard=back_to_main_keyboard(lang))


def _extract_photo_urls(message: Message) -> list[str]:
    urls: list[str] = []
    attachments = getattr(message, "attachments", None) or []
    for att in attachments:
        photo = getattr(att, "photo", None)
        if photo is None and getattr(att, "type", None) == "photo":
            photo = getattr(att, "value", None) or getattr(att, "photo", None)
        if photo is None:
            continue

        sizes = getattr(photo, "sizes", None) or []
        best_url = None
        best_area = -1
        for s in sizes:
            url = getattr(s, "url", None) or getattr(s, "src", None)
            w = getattr(s, "width", 0) or 0
            h = getattr(s, "height", 0) or 0
            area = int(w) * int(h)
            if url and area >= best_area:
                best_area = area
                best_url = url
        if best_url:
            urls.append(best_url)
    return urls


async def _download_photo_bytes_list(message: Message) -> list[bytes]:
    urls = _extract_photo_urls(message)
    if not urls:
        return []
    timeout = httpx.Timeout(connect=20.0, read=60.0, write=20.0, pool=20.0)
    images: list[bytes] = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.content:
                    images.append(resp.content)
            except Exception:
                continue
    return images


def _kb_options(
    options: list[str],
    include_back: bool,
    include_skip: bool,
    include_create: bool,
    lang: str,
    columns: int = 2,
) -> str:
    keyboard = Keyboard(one_time=False, inline=False)
    back_values = {
        _norm(get_string("back", lang)),
        _norm(get_string("back_main", lang)),
        _norm("назад"),
        _norm("back"),
        _norm("quay lại"),
    }
    skip_values = {
        _norm(get_string("skip", lang)),
        _norm("пропустить"),
        _norm("skip"),
        _norm("bỏ qua"),
    }
    columns = max(1, min(columns, 4))
    col = 0
    for opt in options:
        opt_norm = _norm(opt)
        if opt_norm in back_values:
            color = KeyboardButtonColor.NEGATIVE
        elif opt_norm in skip_values:
            color = KeyboardButtonColor.SECONDARY
        else:
            color = KeyboardButtonColor.PRIMARY
        keyboard.add(Text(opt), color=color)
        col += 1
        if col >= columns:
            keyboard.row()
            col = 0
    if col:
        keyboard.row()

    has_skip_like_option = any(_norm(opt) in skip_values for opt in options)
    if include_skip and not has_skip_like_option:
        keyboard.add(Text(get_string("skip", lang)), color=KeyboardButtonColor.SECONDARY)
        keyboard.row()
    if include_create:
        keyboard.add(Text(f"✅ {get_string('create_photo', lang)}"), color=KeyboardButtonColor.POSITIVE)
        keyboard.row()
    has_back_like_option = any(_norm(opt) in back_values for opt in options)
    if include_back and not has_back_like_option:
        keyboard.add(Text(get_string("back", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()


def _numbered_options_text(options: list[str], columns: int = 2) -> str:
    if not options:
        return ""
    columns = max(1, min(columns, 3))
    rows = math.ceil(len(options) / columns)
    cells = [f"{idx}. {text}" for idx, text in enumerate(options, 1)]
    col_widths: list[int] = []
    for col in range(columns):
        col_cells = cells[col * rows:(col + 1) * rows]
        col_widths.append(max((len(cell) for cell in col_cells), default=0))
    lines: list[str] = []
    for row in range(rows):
        parts: list[str] = []
        for col in range(columns):
            idx = col * rows + row
            if idx >= len(cells):
                continue
            cell = cells[idx]
            if col < columns - 1:
                parts.append(cell.ljust(col_widths[col] + 4))
            else:
                parts.append(cell)
        lines.append("".join(parts).rstrip())
    return "\n".join(lines)


def _generation_reset_keyboard(lang: str) -> str:
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("🔄 Сброс"), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()


def _normal_photos_keyboard(count: int, lang: str) -> str:
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text(NORMAL_PHOTOS_GO_PROMPT_TEXT if count >= 4 else NORMAL_PHOTOS_DONE_TEXT), color=KeyboardButtonColor.POSITIVE)
    keyboard.row()
    keyboard.add(Text(get_string("back_main", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()


async def _quality_keyboard(db: Database, user_id: int, lang: str) -> str:
    price_1k = await db.get_user_generation_price(user_id)
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text(f"{QUALITY_1K_PREFIX} - {price_1k} RUB"), color=KeyboardButtonColor.PRIMARY)
    if not await db.is_4k_disabled():
        price_4k = await db.get_user_generation_charge_rub(user_id, "4k")
        keyboard.row()
        keyboard.add(Text(f"{QUALITY_4K_PREFIX} - {price_4k} RUB"), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text(get_string("back", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()


def _confirm_generation_keyboard(lang: str) -> str:
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text(f"✅ {get_string('create_photo', lang)}"), color=KeyboardButtonColor.POSITIVE)
    keyboard.row()
    keyboard.add(Text(get_string("back", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()


def _vk_subscription_check_keyboard(lang: str) -> str:
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("✅ Проверить подписку"), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text(get_string("back_main", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()


def _storefront_model_keyboard(lang: str) -> str:
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("⬅️ Предыдущая"), color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("✅ Выбрать"), color=KeyboardButtonColor.POSITIVE)
    keyboard.add(Text("➡️ Следующая"), color=KeyboardButtonColor.SECONDARY)
    keyboard.row()
    keyboard.add(Text("🔍 Поиск по номеру"), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text(get_string("back", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()


def _result_actions_keyboard(lang: str, allow_edit: bool = True) -> str:
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text(get_string("btn_repeat", lang)), color=KeyboardButtonColor.SECONDARY)
    keyboard.row()
    if allow_edit:
        keyboard.add(Text(get_string("btn_edit", lang)), color=KeyboardButtonColor.SECONDARY)
        keyboard.row()
    keyboard.add(Text(get_string("back_main", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()




def _is_retry_text(text: str) -> bool:
    return text in {
        _norm("🔄 Попробовать еще раз"),
        _norm("попробовать еще раз"),
        _norm("retry"),
    }


def _is_create_text(text: str, lang: str) -> bool:
    return text in {
        _norm("создать"),
        _norm(f"✅ {get_string('create_photo', lang)}"),
        _norm(get_string("create_photo", lang)),
        _norm(f"✅ {get_string('create_photo', lang)}"),
        _norm("generate"),
    } or _is_retry_text(text)


def _parse_quality_text(text_raw: str | None) -> str | None:
    val = (text_raw or "").strip().lower()
    if val.startswith("1k") or val.startswith("1к"):
        return "1k"
    if val.startswith("4k") or val.startswith("4к"):
        return "4k"
    return None


async def _parse_quality_text_async(db: Database, text_raw: str | None) -> str | None:
    quality = _parse_quality_text(text_raw)
    if quality == "4k" and await db.is_4k_disabled():
        return None
    return quality


def _is_repeat_text(text: str, lang: str) -> bool:
    return text in {
        _norm(get_string("btn_repeat", lang)),
        _norm("повторить"),
        _norm("repeat"),
    }


def _is_edit_text(text: str, lang: str) -> bool:
    return text in {
        _norm(get_string("btn_edit", lang)),
        _norm("внести правки"),
        _norm("make changes"),
    }


def _normalize_aspect_for_gemini(raw_aspect: str | None) -> str:
    val = (raw_aspect or "").strip().replace(" ", "")
    if not val:
        return "1x1"
    if val.lower() in {"auto", "square", "1", "1x1", "1:1"}:
        return "1x1"
    if ":" in val:
        val = val.replace(":", "x")
    parts = val.lower().split("x")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and int(parts[0]) > 0 and int(parts[1]) > 0:
        return f"{int(parts[0])}x{int(parts[1])}"
    return "1x1"


def _market_category_by_text(text: str, lang: str) -> str | None:
    mapping = {
        _norm(get_string("cat_presets", lang)): "presets",
        _norm(get_string("cat_collage", lang)): "collage",
        _norm(get_string("cat_random", lang)): "random",
        _norm(get_string("cat_random_other", lang)): "random_other",
        _norm(get_string("cat_infographics", lang)): "infographics",
        _norm(get_string("cat_infographic_clothing", lang)): "infographic_clothing",
        _norm(get_string("cat_infographic_other", lang)): "infographic_other",
        _norm(get_string("cat_storefront", lang)): "storefront",
        _norm(get_string("cat_whitebg", lang)): "whitebg",
        _norm(get_string("cat_own", lang)): "own",
        _norm(get_string("cat_own_variant", lang)): "own_variant",
    }
    return mapping.get(text)


def _is_person_absent(values: dict[str, str]) -> bool:
    for key, value in values.items():
        key_low = str(key).lower()
        if isinstance(value, bool):
            if not value and any(x in key_low for x in ("person", "presence", "человек", "присутствие")):
                return True
            continue
        value_low = str(value).lower()
        if value_low in {"person_no", "without_person", "no_person", "no-person"}:
            return True
        if value_low in {"no", "off", "нет"} and any(x in key_low for x in ("person", "presence", "человек", "присутствие")):
            return True
    return False


def _should_show_skip_for_step(step_key: str, is_optional: bool) -> bool:
    if is_optional:
        return True
    low_key = step_key.lower()
    return any(
        x in low_key
        for x in (
            "adv_",
            "extra_info",
            "brand_name",
            "info_load",
            "преимущество",
            "доп_инфо",
            "доп_текст",
            "название",
            "бренд",
            "product_name",
        )
    )


def _should_skip_step(step_key: str, values: dict[str, str], category: str | None = None) -> bool:
    cat_key = category or values.get("category")
    person_absent = _is_person_absent(values)
    low_key = step_key.lower()
    if cat_key in ("random_other", "infographic_other"):
        is_skip_target = any(x in low_key for x in ("age", "pose", "size", "возраст", "поза", "телосложение"))
    else:
        is_skip_target = any(x in low_key for x in ("age", "pose", "height", "size", "возраст", "поза", "рост", "телосложение"))
    if person_absent and is_skip_target:
        return True
    if step_key == "rand_location_indoor" and values.get("rand_loc_group") != "indoor":
        return True
    if step_key == "rand_location_outdoor" and values.get("rand_loc_group") != "outdoor":
        return True
    if values.get("rand_loc_group") == "indoor" and "season" in low_key:
        return True
    return False


async def _load_model_photo_bytes(raw: str | None) -> bytes | None:
    if not raw:
        return None
    
    
    if raw.startswith("AgAC") or raw.startswith("AQAD"):
        token = (os.getenv("BOT_TOKEN", "") or os.getenv("OLD_BOT_TOKEN", "")).strip()
        if not token:
            return None
        try:
            timeout = httpx.Timeout(connect=20.0, read=60.0, write=20.0, pool=20.0)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                meta = await client.get(
                    f"https://api.telegram.org/bot{token}/getFile",
                    params={"file_id": raw},
                )
                if meta.status_code != 200:
                    return None
                payload = meta.json()
                if not payload.get("ok"):
                    return None
                file_path = ((payload.get("result") or {}).get("file_path") or "").strip()
                if not file_path:
                    return None
                f = await client.get(f"https://api.telegram.org/file/bot{token}/{file_path}")
                if f.status_code == 200 and f.content:
                    return f.content
        except Exception:
            return None
        return None
    try:
        if raw.startswith("http://") or raw.startswith("https://"):
            timeout = httpx.Timeout(connect=20.0, read=60.0, write=20.0, pool=20.0)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                r = await client.get(raw)
                if r.status_code == 200 and r.content:
                    return r.content
            return None
        p = raw if os.path.isabs(raw) else os.path.join("/app", raw.lstrip("/"))
        if os.path.exists(p):
            with open(p, "rb") as f:
                return f.read()
    except Exception:
        return None
    return None


async def _start_constructor_flow(message: Message, db: Database, user_id: int, lang: str, category_key: str) -> None:
    cat = await db.get_category_by_key(category_key)
    if not cat:
        await _reply(message, get_string("vk_category_not_found", lang), keyboard=back_to_main_keyboard(lang))
        return
    steps = await db.list_steps(int(cat[0]))
    if not steps:
        await _reply(message, get_string("vk_category_not_configured", lang), keyboard=back_to_main_keyboard(lang))
        return

    st = _get_state(user_id)
    st.stage = STATE_CONSTRUCTOR_STEP
    st.category = category_key
    st.constructor_category_id = int(cat[0])
    st.constructor_steps = steps
    st.current_step_index = 0
    st.current_step_id = None
    st.current_step_key = None
    st.waiting_custom_for = None
    st.step_values = {}
    st.step_labels = {}
    st.step_photos = {}
    st.model_choices = {}
    st.selected_quality = None
    st.quality_flow = None
    st.updated_at = time.time()
    await _show_constructor_step(message, db, user_id, lang)


async def _show_constructor_step(message: Message, db: Database, user_id: int, lang: str) -> None:
    st = _get_state(user_id)
    while st.current_step_index < len(st.constructor_steps):
        step_id, step_key, _q, input_type, is_optional, _order = st.constructor_steps[st.current_step_index]
        step_key = str(step_key)
        if _should_skip_step(step_key, st.step_values, st.category):
            st.current_step_index += 1
            continue

        st.current_step_id = int(step_id)
        st.current_step_key = step_key
        question = (await db.get_step_text(int(step_id), lang) or "").strip() or str(_q or get_string("vk_enter_step_value", lang))
        optional = int(is_optional) == 1
        show_skip = _should_show_skip_for_step(step_key, optional)

        if input_type == "buttons":
            opts = await db.list_step_options_localized(int(step_id), lang)
            option_texts = [str(o[1]) for o in opts]
            numbered = _numbered_options_text(option_texts, columns=2)
            text = f"{question}\n\n{numbered}\n\n{get_string('vk_reply_option_or_text', lang)}" if numbered else question
            await _reply(
                message,
                text,
                keyboard=_kb_options(
                    option_texts,
                    include_back=True,
                    include_skip=show_skip,
                    include_create=False,
                    lang=lang,
                    columns=2,
                ),
            )
            return

        if input_type == "model_select":
            cloth = st.step_values.get("gender")
            if cloth == "unisex":
                cloth = "all"
            model_category = "storefront" if st.category == "storefront" else "presets"
            count = await db.count_models(model_category, cloth)
            items: list[tuple[int, str, int, str | None]] = []
            choices: dict[int, tuple[int, str, int, str | None]] = {}
            labels: list[str] = []
            for idx in range(min(count, 200)):
                model = await db.get_model_by_index(model_category, cloth, idx)
                if not model:
                    continue
                num = idx + 1
                items.append(model)
                choices[num] = model
                labels.append(f"{num}. {model[1]}")
            st.model_choices = choices
            st.model_browser_items = items
            st.model_browser_index = 0
            if not choices:
                await _reply(message, get_string("vk_no_models", lang), keyboard=_kb_options([], include_back=True, include_skip=False, include_create=False, lang=lang))
                return
            if st.category == "storefront":
                await _show_storefront_model_card(message, user_id, lang, question)
                return
            await _reply(message, f"{question}\n\n" + "\n".join(labels) + f"\n\n{get_string('vk_reply_model_number', lang)}", keyboard=_kb_options(labels, include_back=True, include_skip=False, include_create=False, lang=lang, columns=1))
            return

        if input_type == "photo":
            await _reply(message, question, keyboard=_kb_options([], include_back=True, include_skip=show_skip, include_create=False, lang=lang))
            return

        if input_type == "aspect":
            await _reply(message, question, keyboard=aspect_ratio_keyboard(lang))
            return

        await _reply(message, question, keyboard=_kb_options([], include_back=True, include_skip=show_skip, include_create=False, lang=lang))
        return

    if not st.step_values.get("aspect"):
        if not any(str(step[1]) == "aspect" for step in st.constructor_steps):
            st.constructor_steps.append((0, "aspect", get_string("select_format", lang), "aspect", 0, 9999))
        for idx, step in enumerate(st.constructor_steps):
            if str(step[1]) == "aspect":
                st.current_step_index = idx
                st.current_step_id = 0
                st.current_step_key = "aspect"
                st.stage = STATE_CONSTRUCTOR_STEP
                await _reply(message, get_string("select_format", lang), keyboard=aspect_ratio_keyboard(lang))
                return

    if not st.selected_quality:
        await _ask_quality(message, db, user_id, lang, flow="constructor")
        return
    await _show_constructor_confirmation(message, db, user_id, lang)


async def _show_constructor_confirmation(message: Message, db: Database, user_id: int, lang: str) -> None:
    st = _get_state(user_id)
    lines = ["📋 Проверьте выбранные параметры:\n"]
    for step in st.constructor_steps:
        _id, step_key, question, _input_type, _opt, _ord = step
        key = str(step_key)
        if key in st.step_labels:
            lines.append(f"- {question}: {st.step_labels[key]}")
        elif key in st.step_values:
            lines.append(f"- {question}: {st.step_values[key]}")
    lines.append(f"\n{get_string('generation_confirm', lang)}")
    st.stage = STATE_CONSTRUCTOR_CONFIRM
    await _reply(message, "\n".join(lines), keyboard=_confirm_generation_keyboard(lang))


async def _ask_quality(message: Message, db: Database, user_id: int, lang: str, flow: str) -> None:
    st = _get_state(user_id)
    if await db.is_4k_disabled():
        st.selected_quality = "1k"
        st.step_values = st.step_values or {}
        st.step_values["selected_quality"] = "1k"
        if flow == "constructor":
            await _show_constructor_confirmation(message, db, user_id, lang)
        else:
            await _show_normal_confirmation(message, db, user_id, lang)
        return
    st.stage = STATE_WAIT_QUALITY
    st.quality_flow = flow
    text = "Выберите качество финальной фотографии:"
    await _reply(message, text, keyboard=await _quality_keyboard(db, user_id, lang))


async def _show_normal_confirmation(message: Message, db: Database, user_id: int, lang: str) -> None:
    st = _get_state(user_id)
    prompt = (st.step_values or {}).get("prompt", "")
    aspect = (st.step_values or {}).get("aspect", "1:1")
    quality = await db.resolve_photo_quality(st.selected_quality or "1k")
    if st.selected_quality != quality:
        st.selected_quality = quality
        st.step_values = st.step_values or {}
        st.step_values["selected_quality"] = quality
    images_count = len(st.image_bytes_list or [])
    price = await db.get_user_generation_charge_rub(user_id, quality)
    text = (
        "Проверьте параметры:\n\n"
        f"- Фото: {images_count}\n"
        f"- Формат: {aspect}\n"
        f"- Качество: {quality.upper()}\n"
        f"- Стоимость: {price} RUB\n"
        f"- Промпт: {prompt or 'none'}\n\n"
        f"{get_string('generation_confirm', lang)}"
    )
    st.stage = STATE_NORMAL_CONFIRM
    await _reply(message, text, keyboard=_confirm_generation_keyboard(lang))


async def _show_storefront_model_card(message: Message, user_id: int, lang: str, question: str) -> None:
    st = _get_state(user_id)
    if not st.model_browser_items:
        await _reply(message, get_string("vk_no_models", lang), keyboard=back_to_main_keyboard(lang))
        return
    st.model_browser_index = max(0, min(st.model_browser_index, len(st.model_browser_items) - 1))
    idx = st.model_browser_index
    model_id, model_name, _prompt_id, photo_ref = st.model_browser_items[idx]
    text = (
        f"{question}\n\n"
        f"Модель: {model_name}\n"
        f"Номер: {idx + 1}/{len(st.model_browser_items)}\n\n"
        "Кнопки: Следующая / Предыдущая / Выбрать / Поиск по номеру"
    )
    attachment = None
    photo_bytes = await _load_model_photo_bytes(photo_ref)
    if photo_bytes:
        attachment = await _upload_photo_bytes_attachment(message, photo_bytes)
    if attachment:
        await _reply_with_attachment(message, text, attachment=attachment, keyboard=_storefront_model_keyboard(lang))
    else:
        await _reply(message, text, keyboard=_storefront_model_keyboard(lang))


async def _handle_presets_message(message: Message, db: Database, user_id: int, lang: str) -> bool:
    st = _get_state(user_id)
    if st.stage not in {STATE_PRESET_GENDER, STATE_PRESET_MODEL}:
        return False
    text_raw = (message.text or "").strip()
    text = _norm(text_raw)
    if text in {_norm(get_string("back", lang)), _norm(get_string("back_main", lang))}:
        _reset_state(user_id)
        await _send_marketplace_menu(message, db, user_id)
        return True

    if st.stage == STATE_PRESET_GENDER:
        gender = _preset_gender_by_text(text, lang)
        if not gender:
            await _start_presets_flow(message, db, user_id, lang)
            return True
        count = await db.count_models(gender, "all")
        items: list[tuple[int, str, int, str | None]] = []
        choices: dict[int, tuple[int, str, int, str | None]] = {}
        for idx in range(min(count, 200)):
            model = await db.get_model_by_index(gender, "all", idx)
            if model:
                items.append(model)
                choices[idx + 1] = model
        if not items:
            await _reply(message, get_string("vk_no_models", lang), keyboard=back_to_main_keyboard(lang))
            return True
        st.step_values["gender"] = gender
        st.step_labels["gender"] = text_raw
        st.model_browser_items = items
        st.model_choices = choices
        st.model_browser_index = 0
        st.stage = STATE_PRESET_MODEL
        await _show_storefront_model_card(message, user_id, lang, get_string("cat_presets", lang))
        return True

    if text == _norm("➡️ Следующая") or text == _norm("вћЎпёЏ РЎР»РµРґСѓСЋС‰Р°СЏ"):
        st.model_browser_index = (st.model_browser_index + 1) % len(st.model_browser_items)
        await _show_storefront_model_card(message, user_id, lang, get_string("cat_presets", lang))
        return True
    if text == _norm("⬅️ Предыдущая") or text == _norm("в¬…пёЏ РџСЂРµРґС‹РґСѓС‰Р°СЏ"):
        st.model_browser_index = (st.model_browser_index - 1) % len(st.model_browser_items)
        await _show_storefront_model_card(message, user_id, lang, get_string("cat_presets", lang))
        return True
    if text == _norm("🔍 Поиск по номеру") or text == _norm("рџ”Ќ РџРѕРёСЃРє РїРѕ РЅРѕРјРµСЂСѓ"):
        await _reply(message, "Введите номер модели:", keyboard=_storefront_model_keyboard(lang))
        return True
    if text_raw.isdigit():
        idx = int(text_raw) - 1
        if 0 <= idx < len(st.model_browser_items):
            st.model_browser_index = idx
            await _show_storefront_model_card(message, user_id, lang, get_string("cat_presets", lang))
            return True
    if text == _norm("✅ Выбрать") or text == _norm("вњ… Р’С‹Р±СЂР°С‚СЊ"):
        model_id, model_name, prompt_id, photo_ref = st.model_browser_items[st.model_browser_index]
        st.step_values["model_id"] = str(model_id)
        st.step_values["prompt_id"] = str(prompt_id)
        st.step_values["model_select_photo"] = photo_ref or ""
        st.step_values["own_ref_photo_id"] = photo_ref or ""
        st.step_labels["model_select"] = model_name
        st.stage = STATE_CONSTRUCTOR_STEP
        st.constructor_steps = [
            (0, "photo", get_string("upload_product", lang), "photo", 0, 1),
            (0, "aspect", get_string("select_format", lang), "aspect", 0, 2),
        ]
        st.current_step_index = 0
        st.current_step_id = None
        st.current_step_key = None
        await _show_constructor_step(message, db, user_id, lang)
        return True
    await _show_storefront_model_card(message, user_id, lang, get_string("cat_presets", lang))
    return True


async def _constructor_back(message: Message, db: Database, user_id: int, lang: str) -> None:
    st = _get_state(user_id)
    if st.waiting_custom_for:
        target_key = st.waiting_custom_for
        st.waiting_custom_for = None
        for idx, step in enumerate(st.constructor_steps):
            if str(step[1]) == target_key:
                st.current_step_index = idx
                st.stage = STATE_CONSTRUCTOR_STEP
                await _show_constructor_step(message, db, user_id, lang)
                return
    if st.stage == STATE_CONSTRUCTOR_CONFIRM:
        st.stage = STATE_CONSTRUCTOR_STEP
        st.current_step_index = max(0, len(st.constructor_steps) - 1)
        await _show_constructor_step(message, db, user_id, lang)
        return
    
    if st.current_step_index <= 0:
        _reset_state(user_id)
        await _send_marketplace_menu(message, db, user_id)
        return
    st.current_step_index = max(0, st.current_step_index - 1)
    st.stage = STATE_CONSTRUCTOR_STEP
    await _show_constructor_step(message, db, user_id, lang)


async def _constructor_advance(message: Message, db: Database, user_id: int, lang: str) -> None:
    st = _get_state(user_id)
    st.current_step_index += 1
    st.stage = STATE_CONSTRUCTOR_STEP
    st.current_step_id = None
    st.current_step_key = None
    await _show_constructor_step(message, db, user_id, lang)


async def _build_constructor_prompt(db: Database, st: VkUserState) -> str:
    base = await _get_category_prefix_prompt(db, st.category)
    
    values = dict(st.step_values)
    if "rand_height" in values and "height_cm" not in values:
        values["height_cm"] = values["rand_height"]
    if "rand_width" in values and "width_cm" not in values:
        values["width_cm"] = values["rand_width"]
    if "rand_length" in values and "length_cm" not in values:
        values["length_cm"] = values["rand_length"]
    if "info_angle" in values and "view" not in values:
        values["view"] = values["info_angle"]
    if st.category == "random":
        values["random_mode"] = True
    if st.category == "random_other":
        values["random_other_mode"] = True
    if st.category in {"infographic_clothing", "infographic_other"}:
        values["infographic_mode"] = True
    if st.category == "storefront":
        values["storefront_mode"] = True
    if st.category == "own":
        values["own_mode"] = True
    if st.category == "collage":
        values["collage_mode"] = True
    if st.category == "presets":
        values["is_preset"] = True
    values["category"] = st.category
    if not values.get("aspect"):
        values["aspect"] = "1:1"

    
    prompt = (await build_final_prompt(values, db) or "").strip()

    
    extra_keys = ("prompt", "custom_prompt", "user_prompt", "extra_prompt", "note", "notes", "comment", "description")
    extra = ""
    for k in extra_keys:
        v = (values.get(k) or "").strip()
        if v:
            extra = v
            break
    if extra:
        prompt = f"{prompt}\n\nДополнительно: {extra}".strip()

    return prompt or base


async def _collect_constructor_images(db: Database, st: VkUserState) -> list[bytes]:
    photos: list[bytes] = []
    if st.category == "collage":
        collage_keys = ["collage_model_photo", "collage_front_photo", "collage_back_photo"]
        for k in collage_keys:
            cand = st.step_photos.get(k) or []
            if cand:
                photos.extend(cand)
        return [p for p in photos if p]

    if st.category == "own_variant":
        
        bg_keys = ["bg_photo", "own_bg_photo_id", "own_bg_photo", "background_photo", "background", "bg"]
        product_keys = ["photo", "own_product_photo_id", "own_product_photo", "product_photo", "product", "item_photo"]
        bg: list[bytes] = []
        product: list[bytes] = []
        for k in bg_keys:
            cand = st.step_photos.get(k) or []
            if cand:
                bg = cand
                break
        for k in product_keys:
            cand = st.step_photos.get(k) or []
            if cand:
                product = cand
                break
        photos.extend(bg + product)
        return [p for p in photos if p]

    if st.category == "presets" and st.step_values.get("model_select_photo"):
        model_bytes = await _load_model_photo_bytes(st.step_values.get("model_select_photo"))
        if model_bytes:
            photos.append(model_bytes)

    for step in st.constructor_steps:
        key = str(step[1])
        if key in st.step_photos:
            photos.extend(st.step_photos.get(key, []))
    return [p for p in photos if p]


def _render_generation_error(lang: str, err: Exception) -> tuple[str, bool]:
    msg = str(err or "")
    low = msg.lower()

    if any(w in low for w in ("safety", "blocked", "prohibited", "violated", "filtered", "policy", "sensitive")):
        return ("🔞 Генерация заблокирована нейросетью.\n\n"
                "Сработал фильтр безопасности контента. Убедитесь, что на фото нет глубокого декольте, "
                "нижнего белья, открытых частей тела или провокационных поз. Пожалуйста, выберите другое фото."), False
    if "503" in low or "high demand" in low or "unavailable" in low:
        return "⚠️ Сейчас сервис генерации перегружен. Попробуйте повторить через 1-2 минуты.", True
    if "generation_total_timeout" in low:
        return "⚠️ Генерация превысила лимит времени. Попробуйте еще раз с менее сложным промптом/фото.", True
    if "timed out" in low or "timeout" in low or "proxy/network error" in low or "таймаут" in low:
        use_proxy = str(os.getenv("GEMINI_USE_PROXY", "")).lower() in ("1", "true", "yes")
        if use_proxy:
            return "⚠️ Временная сетевая ошибка сервиса генерации. Серверы сейчас под высокой нагрузкой. Попробуйте еще раз через 20-60 секунд.", True
        return "⚠️ Временная сетевая ошибка сервиса генерации. Серверы сейчас под высокой нагрузкой. Попробуйте еще раз через 10-30 секунд.", True
    if "all_keys_busy" in low or "kie_keys_busy" in low or "zveno_keys_busy" in low:
        return "⚠️ Сейчас все ключи заняты активными запросами. Повторите через 10-20 секунд.", True
    if "zveno_insufficient_credits" in low or "insufficient credits" in low:
        return "⚠️ Сервис генерации временно недоступен. Мы уже получили уведомление и работаем над восстановлением. Попробуйте повторить чуть позже.", True
    if "image_generation_provider_location_unsupported" in low or "location is not supported" in low:
        return "⚠️ Сервис генерации временно недоступен на стороне провайдера. Мы уже получили уведомление. Попробуйте повторить чуть позже.", True
    if "апскейл превысил" in low or "upscale timeout" in low:
        return "⚠️ Изображение подготовлено в стандартном качестве, этап 4K занял слишком много времени. Попробуйте повторить позже.", True
    if "image_generation_empty_image" in low or "zveno_empty_image" in low or "empty_result" in low:
        return "⚠️ Нейросеть вернула ответ без изображения. Попробуйте другой ракурс/промпт и повторите.", True
    if "image_generation_failed_try_again" in low:
        return ("⚠️ Не удалось завершить генерацию в этот раз. Попробуйте еще раз. "
                "Если ошибка повторится, выберите другое фото модели и повторите."), True
    if "input_photo_download_failed" in low:
        return "⚠️ Не удалось получить входные фото. Пожалуйста, отправьте фото еще раз.", False
    if "network" in low or "connection reset" in low:
        return "⚠️ Временная сетевая ошибка сервиса генерации. Попробуйте еще раз через 20-60 секунд.", True
    return get_string("gen_error_contact_support", lang), True


async def _notify_admins_vk(db: Database, text: str) -> None:
    try:
        from bot.settings_reader import load_settings
        settings = load_settings()
        admin_ids = settings.admin_ids or [1440716472, 916948327]
        for aid in admin_ids:
            try:
                logger.warning("[VK Admin Notify] %s | admin=%s", text[:200], aid)
            except Exception:
                pass
    except Exception:
        pass


def _retry_keyboard(lang: str) -> str:
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("🔄 Попробовать еще раз"), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text(get_string("back_main", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()


async def _handle_generation_error(
    message: Message,
    db: Database,
    user_id: int,
    lang: str,
    err: Exception,
    *,
    category: str | None = None,
    prompt: str | None = None,
    quality: str | None = None,
) -> None:
    err_text, show_retry = _render_generation_error(lang, err)
    kb = _retry_keyboard(lang) if show_retry else back_to_main_keyboard(lang)
    await _reply(message, err_text, keyboard=kb)

    short_err = str(err)[:400]
    low_err = short_err.lower()
    provider_reason = ""
    if "image_generation_provider_location_unsupported" in low_err or "location is not supported" in low_err:
        provider_reason = "Provider status: Google AI Studio location unsupported\n"
    elif "image_generation_empty_image" in low_err or "zveno_empty_image" in low_err:
        provider_reason = "Provider status: empty image response (200 OK without image)\n"
    elif "image_generation_safety_blocked" in low_err or "safety" in low_err:
        provider_reason = "Provider status: image safety/content filter\n"
    notify_text = (
        "⚠️ VK Ошибка генерации\n"
        f"User: {user_id}\n"
        f"Category: {category or 'n/a'}\n"
        f"Quality: {quality or 'n/a'}\n"
        f"Prompt len: {len(prompt) if prompt else 0}\n"
        f"{provider_reason}"
        f"Error: {short_err}"
    )
    await _notify_admins_vk(db, notify_text)


async def _save_generation_history(
    db: Database,
    user_id: int,
    category: str | None,
    prompt: str,
    result_path: str,
    params: dict | None = None,
) -> None:
    try:
        pid = f"VK{uuid.uuid4().hex[:10].upper()}"
        rp = (result_path or "").replace("\\", "/")
        if "/data/" in rp:
            rp_db = rp[rp.index("/data/") + 1 :]
        elif rp.startswith("data/"):
            rp_db = rp
        else:
            rp_db = rp.lstrip("/")
        await db.add_generation_history(
            pid=pid,
            user_id=user_id,
            category=category or "normal",
            params=json.dumps(params or {}, ensure_ascii=False),
            input_photos="[]",
            result_photo_id=rp_db,
            input_paths="[]",
            result_path=rp_db,
            prompt=(prompt or "")[:2000],
        )
    except Exception:
        logger.exception("VK add_generation_history failed user_id=%s pid=%s result_path=%s", user_id, pid, result_path)


def _remember_last_generation(
    st: VkUserState,
    *,
    flow: str,
    category: str | None,
    prompt: str,
    images: list[bytes],
    aspect: str | None = None,
    quality: str | None = None,
) -> None:
    st.last_generation_flow = flow
    st.last_generation_category = category
    st.last_generation_prompt = prompt
    st.last_generation_images = list(images or [])
    st.last_generation_aspect = (aspect or "").strip() or None
    st.last_generation_quality = (quality or "").strip() or None
    st.stage = STATE_RESULT_READY
    st.updated_at = time.time()


async def _send_generation_result(
    message: Message,
    db: Database,
    user_id: int,
    lang: str,
    result_path: str,
    allow_edit: bool = True,
) -> None:
    attachment = None
    for attempt in range(1, 4):
        attachment = await _upload_result_photo_attachment(message, str(result_path))
        if attachment:
            break
        await asyncio.sleep(0.7 * attempt)
    if attachment:
        await _reply_with_attachment(
            message,
            get_string("gen_success", lang),
            attachment=attachment,
            keyboard=_result_actions_keyboard(lang, allow_edit=allow_edit),
        )
    else:
        await _reply(
            message,
            "⚠️ Не удалось прикрепить фото в чат VK. Нажмите 'Повторить' или отправьте фото заново.",
            keyboard=back_to_main_keyboard(lang),
        )


def _generation_timeout_seconds() -> int:
    raw = str(os.getenv("GENERATION_TIMEOUT_SECONDS", "260")).strip()
    try:
        value = int(raw)
    except Exception:
        value = 260
    return max(90, value)


def _progress_bar_expected_wall_seconds() -> float:
    raw = str(os.getenv("GENERATION_PROGRESS_EXPECT_SECONDS", "")).strip()
    if raw:
        try:
            return max(25.0, float(raw.replace(",", ".")))
        except Exception:
            pass
    timeout = float(_generation_timeout_seconds())
    return max(42.0, min(88.0, timeout * 0.30))


async def _vk_generation_progress_loop(
    message: Message,
    progress_msg_id: int,
    gen_done: asyncio.Event,
    *,
    title: str = "🚀 Генерация",
) -> None:
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
                f"{title}\n\n"
                f"{steps_text[step_idx]}\n\n"
                f"{bar} {progress}%\n\n"
                f"Прошло: {int(elapsed)}с\n\n"
                f"Результат вас приятно удивит"
            )
            await _edit_vk_message(message, progress_msg_id, text)
            try:
                await asyncio.wait_for(gen_done.wait(), timeout=1.15)
                break
            except asyncio.TimeoutError:
                pass
        elapsed = time.time() - start_time
        await _edit_vk_message(
            message, progress_msg_id,
            f"{title}\n\n{steps_text[-1]}\n\n{'🟦' * 10} 100%\n\nПрошло: {int(elapsed)}с\n\nГотово…",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def _run_constructor_generation(message: Message, db: Database, user_id: int, lang: str) -> None:
    if _vk_gen_locks[user_id].locked():
        return
    async with _vk_gen_locks[user_id]:
        await _run_constructor_generation_real(message, db, user_id, lang)


async def _run_constructor_generation_real(message: Message, db: Database, user_id: int, lang: str) -> None:
    if await db.get_maintenance():
        try:
            from bot.settings_reader import load_settings
            settings = load_settings()
            if user_id not in (settings.admin_ids or []):
                await _reply(message, get_string("maintenance_alert", lang), keyboard=back_to_main_keyboard(lang))
                return
        except Exception:
            await _reply(message, get_string("maintenance_alert", lang), keyboard=back_to_main_keyboard(lang))
            return

    if await db.get_user_blocked(user_id):
        await _reply(message, get_string("user_blocked", lang), keyboard=back_to_main_keyboard(lang))
        return

    st = _get_state(user_id)
    images = await _collect_constructor_images(db, st)
    if not images:
        await _reply(message, get_string("vk_missing_photos", lang), keyboard=back_to_main_keyboard(lang))
        return

    prompt = await _build_constructor_prompt(db, st)
    if not prompt:
        await _reply(message, get_string("vk_prompt_build_error", lang), keyboard=back_to_main_keyboard(lang))
        return

    quality_choice = await db.resolve_photo_quality(st.selected_quality or "1k")
    if st.selected_quality != quality_choice:
        st.selected_quality = quality_choice
        st.step_values = st.step_values or {}
        st.step_values["selected_quality"] = quality_choice
    balance = await db.get_user_balance(user_id)
    price = await db.get_user_generation_charge_rub(user_id, quality_choice)
    if balance < price:
        await _send_topup(message, db, user_id)
        return

    key_id, api_key, key_lock = await _pick_api_key(db)
    if not api_key:
        await _reply(message, get_string("api_limit_reached", lang), keyboard=back_to_main_keyboard(lang))
        return

    progress_msg_id = await _reply_with_id(message, "🎨 ⚡️ ⏳", keyboard=_generation_reset_keyboard(lang))
    gen_done = asyncio.Event()
    progress_task = None
    if progress_msg_id:
        progress_task = asyncio.create_task(
            _vk_generation_progress_loop(message, progress_msg_id, gen_done)
        )
    try:
        aspect = _normalize_aspect_for_gemini(st.step_values.get("aspect") or "1:1")
        quality = "4k" if quality_choice == "4k" else "HD"
        try:
            total_timeout_s = int(os.getenv("GENERATION_TOTAL_TIMEOUT_SECONDS", "240"))
        except Exception:
            total_timeout_s = 240
        total_timeout_s = max(90, min(total_timeout_s, 600))
        result_path = await asyncio.wait_for(
            generate_image(
                api_key=api_key,
                prompt=prompt,
                images_bytes=images,
                aspect_ratio=aspect,
                quality=quality,
                key_id=key_id,
                db_instance=db,
                generation_meta={"category": st.category or "constructor", "user_id": user_id},
            ),
            timeout=total_timeout_s,
        )
        if not result_path:
            await _reply(message, get_string("gen_no_image", lang), keyboard=back_to_main_keyboard(lang))
            return
        await db.subtract_user_balance(user_id=user_id, amount=price, reason="generation")
        if key_id is not None:
            try:
                await _vk_record_api_usage(db, key_id)
            except Exception:
                pass
        await _save_generation_history(
            db=db,
            user_id=user_id,
            category=st.category,
            prompt=prompt,
            result_path=str(result_path),
            params={"flow": "constructor", "category": st.category, "aspect": aspect.replace("x", ":"), "quality": quality_choice},
        )
        _remember_last_generation(st, flow="constructor", category=st.category, prompt=prompt, images=images, aspect=aspect, quality=quality_choice)
        allow_edit = st.category not in {"own", "own_variant"}
        await _send_generation_result(message, db, user_id, lang, str(result_path), allow_edit=allow_edit)
    except Exception as e:
        logger.exception("VK constructor generation failed for user_id=%s", user_id)
        await _handle_generation_error(message, db, user_id, lang, e, category=st.category, prompt=prompt, quality=quality_choice)
    finally:
        gen_done.set()
        if progress_task:
            await asyncio.sleep(0.3)
            progress_task.cancel()
            try:
                await progress_task
            except (asyncio.CancelledError, Exception):
                pass
        if progress_msg_id:
            await _delete_vk_message(message, progress_msg_id)
        release_key_lock(key_lock)


async def _handle_constructor_message(message: Message, db: Database, user_id: int, lang: str) -> bool:
    st = _get_state(user_id)
    if st.stage not in {STATE_CONSTRUCTOR_STEP, STATE_CONSTRUCTOR_CONFIRM}:
        return False

    text_raw = (message.text or "").strip()
    text = _norm(text_raw)
    back_text = _norm(get_string("back", lang))
    skip_text = _norm(get_string("skip", lang))

    if text in {back_text, _norm(get_string("back_main", lang))}:
        if text == back_text:
            await _constructor_back(message, db, user_id, lang)
        else:
            _reset_state(user_id)
            await _send_main_menu(message, db, user_id)
        return True

    if st.stage == STATE_CONSTRUCTOR_CONFIRM:
        if _is_create_text(text, lang):
            await _run_constructor_generation(message, db, user_id, lang)
            return True
        await _show_constructor_confirmation(message, db, user_id, lang)
        return True

    
    if st.waiting_custom_for:
        step_key = st.waiting_custom_for
        if not text_raw:
            await _reply(message, get_string("vk_enter_step_value", lang), keyboard=_kb_options([], include_back=True, include_skip=False, include_create=False, lang=lang))
            return True
        st.step_values[step_key] = text_raw
        st.step_labels[step_key] = text_raw
        st.waiting_custom_for = None
        await _constructor_advance(message, db, user_id, lang)
        return True

    if st.current_step_id is None or st.current_step_key is None:
        await _show_constructor_step(message, db, user_id, lang)
        return True

    step_key = st.current_step_key
    step = st.constructor_steps[st.current_step_index]
    input_type = str(step[3])
    optional = int(step[4]) == 1
    show_skip = _should_show_skip_for_step(step_key, optional)

    if show_skip and text == skip_text:
        st.step_values[step_key] = ""
        st.step_labels[step_key] = get_string("skip", lang)
        await _constructor_advance(message, db, user_id, lang)
        return True

    if input_type == "photo":
        images = await _download_photo_bytes_list(message)
        if not images:
            await _reply(message, get_string("vk_send_step_photo", lang), keyboard=_kb_options([], include_back=True, include_skip=show_skip, include_create=False, lang=lang))
            return True
        st.step_photos[step_key] = [images[-1]]
        st.step_values[step_key] = "photo_uploaded"
        st.step_labels[step_key] = get_string("upload_photo", lang)
        await _constructor_advance(message, db, user_id, lang)
        return True

    if input_type == "aspect":
        aspect_map = {"1:1": "1:1", "9:16": "9:16", "16:9": "16:9", "4:5": "4:5", "3:4": "3:4", "4:3": "4:3", "3:2": "3:2", "2:3": "2:3", "5:4": "5:4", "21:9": "21:9"}
        aspect = aspect_map.get(text_raw.replace("x", ":").replace(" ", ""))
        if not aspect:
            await _reply(message, get_string("select_format", lang), keyboard=aspect_ratio_keyboard(lang))
            return True
        st.step_values[step_key] = aspect
        st.step_labels[step_key] = aspect
        await _constructor_advance(message, db, user_id, lang)
        return True

    if input_type == "model_select":
        if st.category == "storefront" and st.model_browser_items:
            if text == _norm("➡️ Следующая"):
                st.model_browser_index = (st.model_browser_index + 1) % len(st.model_browser_items)
                await _show_storefront_model_card(message, user_id, lang, str(step[2]))
                return True
            if text == _norm("⬅️ Предыдущая"):
                st.model_browser_index = (st.model_browser_index - 1) % len(st.model_browser_items)
                await _show_storefront_model_card(message, user_id, lang, str(step[2]))
                return True
            if text == _norm("🔍 Поиск по номеру"):
                await _reply(message, "Введите номер модели (например: 12):", keyboard=_storefront_model_keyboard(lang))
                return True
            model = None
            if text_raw.isdigit():
                idx = int(text_raw) - 1
                if 0 <= idx < len(st.model_browser_items):
                    st.model_browser_index = idx
                    await _show_storefront_model_card(message, user_id, lang, str(step[2]))
                    return True
            if text == _norm("✅ Выбрать"):
                model = st.model_browser_items[st.model_browser_index]
            if not model:
                await _reply(message, "Выберите модель кнопками или введите номер.", keyboard=_storefront_model_keyboard(lang))
                return True
            model_id, model_name, prompt_id, photo_ref = model
            st.step_values["model_id"] = str(model_id)
            st.step_values["prompt_id"] = str(prompt_id)
            st.step_values["model_select_photo"] = photo_ref or ""
            st.step_values[step_key] = str(model_id)
            st.step_labels[step_key] = model_name
            await _constructor_advance(message, db, user_id, lang)
            return True
        model = None
        if text_raw.isdigit():
            model = st.model_choices.get(int(text_raw))
        if not model:
            for idx, item in st.model_choices.items():
                if text == _norm(f"{idx}. {item[1]}") or text == _norm(item[1]):
                    model = item
                    break
        if not model:
            await _reply(message, get_string("vk_reply_model_number", lang))
            return True
        model_id, model_name, prompt_id, photo_ref = model
        st.step_values["model_id"] = str(model_id)
        st.step_values["prompt_id"] = str(prompt_id)
        st.step_values["model_select_photo"] = photo_ref or ""
        st.step_values[step_key] = str(model_id)
        st.step_labels[step_key] = model_name
        await _constructor_advance(message, db, user_id, lang)
        return True

    if input_type == "buttons":
        opts = await db.list_step_options_localized(int(st.current_step_id), lang)
        selected = None
        if text_raw.isdigit():
            idx = int(text_raw) - 1
            if 0 <= idx < len(opts):
                selected = opts[idx]
        if not selected:
            for o in opts:
                if text == _norm(str(o[1])):
                    selected = o
                    break
        if not selected:
            await _reply(message, get_string("vk_reply_option_or_text", lang))
            return True
        _opt_id, opt_text, opt_value, _order, custom_prompt = selected
        opt_value_norm = str(opt_value).lower()
        opt_text_norm = _norm(str(opt_text))
        if opt_value_norm == "back" and opt_text_norm in {_norm(get_string("back", lang)), _norm("назад"), _norm("back"), _norm("quay lại")}:
            await _constructor_back(message, db, user_id, lang)
            return True
        if opt_value_norm == "skip":
            st.step_values[step_key] = ""
            st.step_labels[step_key] = get_string("skip", lang)
            await _constructor_advance(message, db, user_id, lang)
            return True
        custom_prompt = (custom_prompt or "").strip()
        if custom_prompt:
            st.waiting_custom_for = step_key
            await _reply(message, custom_prompt, keyboard=_kb_options([], include_back=True, include_skip=False, include_create=False, lang=lang))
            return True
        st.step_values[step_key] = str(opt_value)
        st.step_labels[step_key] = str(opt_text)
        await _constructor_advance(message, db, user_id, lang)
        return True

    if not text_raw:
        await _reply(message, get_string("vk_enter_step_value", lang))
        return True
    st.step_values[step_key] = text_raw
    st.step_labels[step_key] = text_raw
    await _constructor_advance(message, db, user_id, lang)
    return True


async def _vk_ensure_api_keys_schema(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 0,
            usage_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            daily_usage INTEGER NOT NULL DEFAULT 0,
            daily_limit INTEGER NOT NULL DEFAULT 0,
            last_usage_reset TEXT,
            last_used_at TIMESTAMP,
            last_error TEXT,
            disabled_until TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    async with conn.execute("PRAGMA table_info(api_keys)") as cur:
        cols = {str(row[1]) for row in await cur.fetchall()}
    additions = {
        "is_active": "INTEGER NOT NULL DEFAULT 1",
        "priority": "INTEGER NOT NULL DEFAULT 0",
        "usage_count": "INTEGER NOT NULL DEFAULT 0",
        "error_count": "INTEGER NOT NULL DEFAULT 0",
        "daily_usage": "INTEGER NOT NULL DEFAULT 0",
        "daily_limit": "INTEGER NOT NULL DEFAULT 0",
        "last_usage_reset": "TEXT",
        "last_used_at": "TIMESTAMP",
        "last_error": "TEXT",
        "disabled_until": "TIMESTAMP",
        "created_at": "TIMESTAMP",
        "updated_at": "TIMESTAMP",
    }
    for col, decl in additions.items():
        if col not in cols:
            await conn.execute(f"ALTER TABLE api_keys ADD COLUMN {col} {decl}")


async def _vk_reactivate_api_keys_for_new_day(db: Database) -> None:
    async with aiosqlite.connect(db._db_path) as conn:
        await _vk_ensure_api_keys_schema(conn)
        await conn.execute(
            """
            UPDATE api_keys
            SET daily_usage=0, last_usage_reset=date('now')
            WHERE COALESCE(last_usage_reset, '') != date('now')
            """
        )
        await conn.execute(
            """
            UPDATE api_keys
            SET is_active=1, disabled_until=NULL
            WHERE disabled_until IS NOT NULL AND datetime(disabled_until) <= CURRENT_TIMESTAMP
            """
        )
        await conn.commit()


async def _vk_list_api_keys(db: Database) -> list[tuple]:
    async with aiosqlite.connect(db._db_path) as conn:
        await _vk_ensure_api_keys_schema(conn)
        await conn.execute(
            """
            UPDATE api_keys
            SET daily_usage=0, last_usage_reset=date('now')
            WHERE COALESCE(last_usage_reset, '') != date('now')
            """
        )
        await conn.commit()
        async with conn.execute(
            """
            SELECT id, token, is_active, usage_count, error_count, daily_usage, daily_limit, priority
            FROM api_keys
            ORDER BY is_active DESC, priority DESC, id ASC
            """
        ) as cur:
            rows = await cur.fetchall()
    if rows:
        return rows
    env_key = (
        os.getenv("ZVENO_API_KEY")
        or os.getenv("KIE_API_KEY")
        or os.getenv("GEMINI_API_KEY")
        or ""
    ).strip()
    if env_key:
        return [(0, env_key, 1, 0, 0, 0, 0, 0)]
    return []


async def _vk_record_api_usage(db: Database, key_id: int | None) -> None:
    if key_id is None or int(key_id) <= 0:
        return
    async with aiosqlite.connect(db._db_path) as conn:
        await _vk_ensure_api_keys_schema(conn)
        await conn.execute(
            """
            UPDATE api_keys
            SET usage_count=COALESCE(usage_count, 0) + 1,
                daily_usage=COALESCE(daily_usage, 0) + 1,
                last_usage_reset=COALESCE(last_usage_reset, date('now')),
                last_used_at=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (int(key_id),),
        )
        await conn.commit()


async def _pick_api_key(db: Database) -> tuple[int | None, str | None, asyncio.Lock | None]:
    try:
        await _vk_reactivate_api_keys_for_new_day(db)
    except Exception:
        pass
    rows = await _vk_list_api_keys(db)
    active_rows = [row for row in rows if int(row[2]) == 1]
    if not active_rows:
        return None, None, None

    last_used_key_id = None
    try:
        last_used_raw = await db.get_app_setting("last_used_api_key_id")
        last_used_key_id = int(last_used_raw) if last_used_raw else None
    except Exception:
        last_used_key_id = None
    active_rows = order_keys_after_last_used(active_rows, last_used_key_id)

    # Конкуренцию на ключ держит сам generate_image() через слоты Zveno
    # (ZVENO_MAX_CONCURRENT_PER_KEY=20). Прежний строгий per-key lock с ожиданием
    # 6с при единственном ключе выдавал ложное "Все API ключи исчерпали лимиты"
    # на любом параллельном запросе. Берём активный ключ без эксклюзивного лока.
    row = active_rows[0]
    key_id = int(row[0])
    try:
        await db.set_app_setting("last_used_api_key_id", str(key_id))
    except Exception:
        pass
    return key_id, str(row[1]), None


async def _start_generation_mode(
    message: Message,
    db: Database,
    user_id: int,
    lang: str,
    category: str | None = None,
) -> None:
    st = _get_state(user_id)
    st.stage = STATE_WAIT_PHOTO
    st.category = category
    st.image_bytes_list = []
    st.step_values = {}
    st.selected_quality = None
    st.quality_flow = None
    st.updated_at = time.time()
    await _reply(message, "📸 Пришлите до 4 фото. Можно отправить несколько фото или нажать «Далее».", keyboard=back_to_main_keyboard(lang))


async def _handle_generation_photo_step(message: Message, db: Database, user_id: int, lang: str) -> bool:
    st = _get_state(user_id)
    if st.stage != STATE_WAIT_PHOTO:
        return False
    text_raw = (message.text or "").strip()
    if _norm(text_raw) in {_norm(NORMAL_PHOTOS_DONE_TEXT), _norm(NORMAL_PHOTOS_GO_PROMPT_TEXT)}:
        if not st.image_bytes_list:
            await _reply(message, get_string("upload_photo", lang), keyboard=back_to_main_keyboard(lang))
            return True
        st.stage = STATE_WAIT_PROMPT
        st.updated_at = time.time()
        await _reply(message, get_string("enter_prompt", lang), keyboard=back_to_main_keyboard(lang))
        return True
    images = await _download_photo_bytes_list(message)
    if not images:
        await _reply(message, get_string("upload_photo", lang), keyboard=_normal_photos_keyboard(len(st.image_bytes_list or []), lang))
        return True
    current = list(st.image_bytes_list or [])
    current.extend(images)
    st.image_bytes_list = current[:4]
    st.updated_at = time.time()
    count = len(st.image_bytes_list)
    if count >= 4:
        text = "Получено 4/4 фото. Нажмите «Перейти к промпту»."
    else:
        text = f"Фото {count}/4 получено. Можно отправить еще {4 - count} фото или нажать «Далее»."
    await _reply(message, text, keyboard=_normal_photos_keyboard(count, lang))
    return True


async def _handle_generation_prompt_step(message: Message, db: Database, user_id: int, lang: str) -> bool:
    st = _get_state(user_id)
    if st.stage != STATE_WAIT_PROMPT:
        return False

    prompt = (message.text or "").strip()
    if not prompt:
        await _reply(message, get_string("enter_prompt", lang), keyboard=back_to_main_keyboard(lang))
        return True
    if len(prompt) > 2500:
        await _reply(message, get_string("enter_prompt_error", lang), keyboard=back_to_main_keyboard(lang))
        return True
    if not st.image_bytes_list:
        st.stage = STATE_WAIT_PHOTO
        await _reply(message, get_string("upload_photo", lang), keyboard=back_to_main_keyboard(lang))
        return True

    
    st.step_values = st.step_values or {}
    st.step_values["prompt"] = prompt
    st.stage = STATE_WAIT_ASPECT
    st.updated_at = time.time()
    await _reply(
        message,
        get_string("select_format", lang),
        keyboard=aspect_ratio_keyboard(lang),
    )
    return True


async def _run_normal_generation(message: Message, db: Database, user_id: int, lang: str) -> None:
    if _vk_gen_locks[user_id].locked():
        return
    async with _vk_gen_locks[user_id]:
        await _run_normal_generation_real(message, db, user_id, lang)


async def _run_normal_generation_real(message: Message, db: Database, user_id: int, lang: str) -> None:
    if await db.get_maintenance():
        try:
            from bot.settings_reader import load_settings
            settings = load_settings()
            if user_id not in (settings.admin_ids or []):
                await _reply(message, get_string("maintenance_alert", lang), keyboard=back_to_main_keyboard(lang))
                return
        except Exception:
            await _reply(message, get_string("maintenance_alert", lang), keyboard=back_to_main_keyboard(lang))
            return

    if await db.get_user_blocked(user_id):
        await _reply(message, get_string("user_blocked", lang), keyboard=back_to_main_keyboard(lang))
        return

    st = _get_state(user_id)
    prompt = (st.step_values or {}).get("prompt", "")
    aspect_raw = (st.step_values or {}).get("aspect", "1:1")
    quality_choice = await db.resolve_photo_quality(st.selected_quality or "1k")
    if st.selected_quality != quality_choice:
        st.selected_quality = quality_choice
        st.step_values = st.step_values or {}
        st.step_values["selected_quality"] = quality_choice
    if not prompt or not st.image_bytes_list:
        st.stage = STATE_WAIT_PHOTO
        await _reply(message, get_string("upload_photo", lang), keyboard=back_to_main_keyboard(lang))
        return

    balance = await db.get_user_balance(user_id)
    price = await db.get_user_generation_charge_rub(user_id, quality_choice)
    if balance < price:
        await _send_topup(message, db, user_id)
        return

    key_id, api_key, key_lock = await _pick_api_key(db)
    if not api_key:
        await _reply(message, get_string("api_limit_reached", lang), keyboard=back_to_main_keyboard(lang))
        return

    progress_msg_id = await _reply_with_id(message, "🎨 ⚡️ ⏳", keyboard=_generation_reset_keyboard(lang))
    gen_done = asyncio.Event()
    progress_task = None
    if progress_msg_id:
        progress_task = asyncio.create_task(
            _vk_generation_progress_loop(message, progress_msg_id, gen_done)
        )
    try:
        category_prompt = await _get_category_prefix_prompt(db, st.category)
        final_prompt = f"{category_prompt}\n\n{prompt}".strip() if category_prompt else prompt
        aspect = _normalize_aspect_for_gemini(aspect_raw)
        quality = "4k" if quality_choice == "4k" else "HD"
        try:
            total_timeout_s = int(os.getenv("GENERATION_TOTAL_TIMEOUT_SECONDS", "240"))
        except Exception:
            total_timeout_s = 240
        total_timeout_s = max(90, min(total_timeout_s, 600))
        result_path = await asyncio.wait_for(
            generate_image(
                api_key=api_key,
                prompt=final_prompt,
                images_bytes=st.image_bytes_list,
                aspect_ratio=aspect,
                quality=quality,
                key_id=key_id,
                db_instance=db,
                generation_meta={"category": st.category or "normal", "user_id": user_id},
            ),
            timeout=total_timeout_s,
        )
        if not result_path:
            await _reply(message, get_string("gen_no_image", lang), keyboard=back_to_main_keyboard(lang))
            return

        await db.subtract_user_balance(user_id=user_id, amount=price, reason="generation")
        if key_id is not None:
            try:
                await _vk_record_api_usage(db, key_id)
            except Exception:
                pass
        await _save_generation_history(
            db=db,
            user_id=user_id,
            category=st.category,
            prompt=final_prompt,
            result_path=str(result_path),
            params={"flow": "normal", "category": st.category, "aspect": aspect.replace("x", ":"), "quality": quality_choice},
        )
        _remember_last_generation(
            st,
            flow="normal",
            category=st.category,
            prompt=final_prompt,
            images=st.image_bytes_list or [],
            aspect=aspect,
            quality=quality_choice,
        )
        await _send_generation_result(message, db, user_id, lang, str(result_path), allow_edit=True)
    except Exception as e:
        logger.exception("VK generation failed for user_id=%s", user_id)
        await _handle_generation_error(message, db, user_id, lang, e, category=st.category, prompt=final_prompt, quality=quality_choice)
    finally:
        gen_done.set()
        if progress_task:
            await asyncio.sleep(0.3)
            progress_task.cancel()
            try:
                await progress_task
            except (asyncio.CancelledError, Exception):
                pass
        if progress_msg_id:
            await _delete_vk_message(message, progress_msg_id)
        release_key_lock(key_lock)


async def _handle_generation_aspect_step(message: Message, db: Database, user_id: int, lang: str) -> bool:
    st = _get_state(user_id)
    if st.stage != STATE_WAIT_ASPECT:
        return False

    text_raw = (message.text or "").strip()
    if _norm(text_raw) in {_norm(get_string("back", lang)), _norm(get_string("back_main", lang))}:
        st.stage = STATE_WAIT_PROMPT
        st.updated_at = time.time()
        await _reply(message, get_string("enter_prompt", lang), keyboard=back_to_main_keyboard(lang))
        return True

    
    aspect_map = {"1:1": "1:1", "9:16": "9:16", "16:9": "16:9", "4:5": "4:5", "3:4": "3:4", "4:3": "4:3", "3:2": "3:2", "2:3": "2:3", "5:4": "5:4", "21:9": "21:9"}
    aspect = aspect_map.get(text_raw.replace("x", ":").replace(" ", ""))
    if not aspect:
        aspect = "1:1"

    st.step_values = st.step_values or {}
    st.step_values["aspect"] = aspect
    if not st.selected_quality:
        await _ask_quality(message, db, user_id, lang, flow="normal")
        return True
    await _show_normal_confirmation(message, db, user_id, lang)
    return True


async def _handle_quality_and_confirm(message: Message, db: Database, user_id: int, lang: str) -> bool:
    st = _get_state(user_id)
    text_raw = (message.text or "").strip()
    text = _norm(text_raw)
    back = _norm(get_string("back", lang))
    back_main = _norm(get_string("back_main", lang))

    if st.stage == STATE_WAIT_QUALITY:
        if text in {back, back_main}:
            if st.quality_flow == "constructor":
                st.stage = STATE_CONSTRUCTOR_STEP
                await _show_constructor_step(message, db, user_id, lang)
            else:
                st.stage = STATE_WAIT_ASPECT
                await _reply(message, get_string("select_format", lang), keyboard=aspect_ratio_keyboard(lang))
            return True
        quality = await _parse_quality_text_async(db, text_raw)
        if not quality:
            await _ask_quality(message, db, user_id, lang, flow=st.quality_flow or "normal")
            return True
        st.selected_quality = quality
        st.step_values = st.step_values or {}
        st.step_values["selected_quality"] = quality
        if st.quality_flow == "constructor":
            await _show_constructor_confirmation(message, db, user_id, lang)
        else:
            await _show_normal_confirmation(message, db, user_id, lang)
        return True

    if st.stage == STATE_NORMAL_CONFIRM:
        if text in {back, back_main}:
            st.stage = STATE_WAIT_QUALITY
            await _ask_quality(message, db, user_id, lang, flow="normal")
            return True
        if _is_create_text(text, lang):
            await _run_normal_generation(message, db, user_id, lang)
            return True
        await _show_normal_confirmation(message, db, user_id, lang)
        return True

    return False




async def _handle_result_actions(message: Message, db: Database, user_id: int, lang: str) -> bool:
    st = _get_state(user_id)
    if st.stage not in {STATE_RESULT_READY, STATE_WAIT_EDIT_TEXT, STATE_WAIT_REPEAT_PHOTO}:
        return False

    text_raw = (message.text or "").strip()
    text = _norm(text_raw)
    back = _norm(get_string("back", lang))
    back_main = _norm(get_string("back_main", lang))

    if text in {back, back_main}:
        if st.stage == STATE_RESULT_READY:
            _reset_state(user_id)
            await _send_main_menu(message, db, user_id)
            return True
        st.stage = STATE_RESULT_READY
        await _reply(
            message,
            get_string("gen_success", lang),
            keyboard=_result_actions_keyboard(lang, allow_edit=st.last_generation_category not in {"own", "own_variant"}),
        )
        return True

    if st.stage == STATE_RESULT_READY:
        if _is_repeat_text(text, lang):
            if st.last_generation_category == "collage":
                cat = await db.get_category_by_key("collage")
                if cat:
                    steps = await db.list_steps(int(cat[0]))
                    saved_model = st.step_photos.get("collage_model_photo", [])
                    st.stage = STATE_CONSTRUCTOR_STEP
                    st.category = "collage"
                    st.constructor_category_id = int(cat[0])
                    st.constructor_steps = steps
                    st.current_step_index = 1
                    st.current_step_id = None
                    st.current_step_key = None
                    st.waiting_custom_for = None
                    st.step_values = {}
                    st.step_labels = {}
                    st.step_photos = {"collage_model_photo": saved_model}
                    st.model_choices = {}
                    st.selected_quality = None
                    st.quality_flow = None
                    st.updated_at = time.time()
                    await _show_constructor_step(message, db, user_id, lang)
                else:
                    await _start_constructor_flow(message, db, user_id, lang, "collage")
                return True
            st.stage = STATE_WAIT_REPEAT_PHOTO
            await _reply(message, get_string("repeat_photo_prompt", lang), keyboard=back_to_main_keyboard(lang))
            return True
        if _is_edit_text(text, lang):
            if st.last_generation_category in {"own", "own_variant"}:
                await _reply(message, get_string("gen_success", lang), keyboard=_result_actions_keyboard(lang, allow_edit=False))
                return True
            st.stage = STATE_WAIT_EDIT_TEXT
            await _reply(message, get_string("enter_edit_description", lang), keyboard=back_to_main_keyboard(lang))
            return True
        return False

    
    if st.stage == STATE_WAIT_EDIT_TEXT:
        edit_text = text_raw.strip()
        if not edit_text:
            await _reply(message, get_string("enter_edit_description", lang), keyboard=back_to_main_keyboard(lang))
            return True
        if _vk_gen_locks[user_id].locked():
            return True
        if not st.last_generation_prompt or not st.last_generation_images:
            _reset_state(user_id)
            await _send_main_menu(message, db, user_id)
            return True

        if await db.get_maintenance():
            try:
                from bot.settings_reader import load_settings
                settings = load_settings()
                if user_id not in (settings.admin_ids or []):
                    await _reply(message, get_string("maintenance_alert", lang), keyboard=back_to_main_keyboard(lang))
                    return True
            except Exception:
                await _reply(message, get_string("maintenance_alert", lang), keyboard=back_to_main_keyboard(lang))
                return True
        if await db.get_user_blocked(user_id):
            await _reply(message, get_string("user_blocked", lang), keyboard=back_to_main_keyboard(lang))
            return True

        quality_choice = await db.resolve_photo_quality(st.last_generation_quality or st.selected_quality or "1k")
        if st.selected_quality != quality_choice:
            st.selected_quality = quality_choice
        price = await db.get_user_generation_charge_rub(user_id, quality_choice)
        balance = await db.get_user_balance(user_id)
        if balance < price:
            await _send_topup(message, db, user_id)
            return True

        key_id, api_key, key_lock = await _pick_api_key(db)
        if not api_key:
            await _reply(message, get_string("api_limit_reached", lang), keyboard=back_to_main_keyboard(lang))
            return True

        final_prompt = f"{st.last_generation_prompt}\n\nПравки: {edit_text}".strip()
        progress_msg_id = await _reply_with_id(message, "🎨 ⚡️ ⏳", keyboard=_generation_reset_keyboard(lang))
        gen_done = asyncio.Event()
        progress_task = None
        if progress_msg_id:
            progress_task = asyncio.create_task(
                _vk_generation_progress_loop(message, progress_msg_id, gen_done, title="🚀 Редактирование")
            )
        try:
            aspect = _normalize_aspect_for_gemini(st.last_generation_aspect or (st.step_values.get("aspect") if st.step_values else "1:1"))
            quality = "4k" if quality_choice == "4k" else "HD"
            try:
                total_timeout_s = int(os.getenv("GENERATION_TOTAL_TIMEOUT_SECONDS", "240"))
            except Exception:
                total_timeout_s = 240
            total_timeout_s = max(90, min(total_timeout_s, 600))
            result_path = await asyncio.wait_for(
                generate_image(
                    api_key=api_key,
                    prompt=final_prompt,
                    images_bytes=st.last_generation_images,
                    aspect_ratio=aspect,
                    quality=quality,
                    key_id=key_id,
                    db_instance=db,
                    generation_meta={"category": st.last_generation_category or "normal", "user_id": user_id},
                ),
                timeout=total_timeout_s,
            )
            if not result_path:
                await _reply(message, get_string("gen_no_image", lang), keyboard=back_to_main_keyboard(lang))
                return True
            await db.subtract_user_balance(user_id=user_id, amount=price, reason="generation")
            if key_id is not None:
                try:
                    await _vk_record_api_usage(db, key_id)
                except Exception:
                    pass
            await _save_generation_history(
                db=db,
                user_id=user_id,
                category=st.last_generation_category,
                prompt=final_prompt,
                result_path=str(result_path),
                params={"flow": st.last_generation_flow or "normal", "edit": True, "aspect": aspect.replace("x", ":"), "quality": quality_choice},
            )
            _remember_last_generation(
                st,
                flow=st.last_generation_flow or "normal",
                category=st.last_generation_category,
                prompt=final_prompt,
                images=st.last_generation_images,
                aspect=aspect,
                quality=quality_choice,
            )
            await _send_generation_result(message, db, user_id, lang, str(result_path), allow_edit=True)
            return True
        except Exception as e:
            logger.exception("VK edit generation failed for user_id=%s", user_id)
            await _handle_generation_error(message, db, user_id, lang, e, category=st.last_generation_category, prompt=final_prompt, quality=quality_choice)
            return True
        finally:
            gen_done.set()
            if progress_task:
                await asyncio.sleep(0.3)
                progress_task.cancel()
                try:
                    await progress_task
                except (asyncio.CancelledError, Exception):
                    pass
            if progress_msg_id:
                await _delete_vk_message(message, progress_msg_id)
            release_key_lock(key_lock)

    
    images = await _download_photo_bytes_list(message)
    if not images:
        await _reply(message, get_string("repeat_photo_prompt", lang), keyboard=back_to_main_keyboard(lang))
        return True
    if _vk_gen_locks[user_id].locked():
        return True
    if not st.last_generation_prompt:
        _reset_state(user_id)
        await _send_main_menu(message, db, user_id)
        return True

    if await db.get_maintenance():
        try:
            from bot.settings_reader import load_settings
            settings = load_settings()
            if user_id not in (settings.admin_ids or []):
                await _reply(message, get_string("maintenance_alert", lang), keyboard=back_to_main_keyboard(lang))
                return True
        except Exception:
            await _reply(message, get_string("maintenance_alert", lang), keyboard=back_to_main_keyboard(lang))
            return True
    if await db.get_user_blocked(user_id):
        await _reply(message, get_string("user_blocked", lang), keyboard=back_to_main_keyboard(lang))
        return True

    quality_choice = await db.resolve_photo_quality(st.last_generation_quality or st.selected_quality or "1k")
    if st.selected_quality != quality_choice:
        st.selected_quality = quality_choice
    price = await db.get_user_generation_charge_rub(user_id, quality_choice)
    balance = await db.get_user_balance(user_id)
    if balance < price:
        await _send_topup(message, db, user_id)
        return True

    key_id, api_key, key_lock = await _pick_api_key(db)
    if not api_key:
        await _reply(message, get_string("api_limit_reached", lang), keyboard=back_to_main_keyboard(lang))
        return True

    repeat_images = images[:4]
    progress_msg_id = await _reply_with_id(message, "🎨 ⚡️ ⏳", keyboard=_generation_reset_keyboard(lang))
    gen_done = asyncio.Event()
    progress_task = None
    if progress_msg_id:
        progress_task = asyncio.create_task(
            _vk_generation_progress_loop(message, progress_msg_id, gen_done, title="🚀 Повтор генерации")
        )
    try:
        aspect = _normalize_aspect_for_gemini(st.last_generation_aspect or (st.step_values.get("aspect") if st.step_values else "1:1"))
        quality = "4k" if quality_choice == "4k" else "HD"
        try:
            total_timeout_s = int(os.getenv("GENERATION_TOTAL_TIMEOUT_SECONDS", "240"))
        except Exception:
            total_timeout_s = 240
        total_timeout_s = max(90, min(total_timeout_s, 600))
        result_path = await asyncio.wait_for(
            generate_image(
                api_key=api_key,
                prompt=st.last_generation_prompt,
                images_bytes=repeat_images,
                aspect_ratio=aspect,
                quality=quality,
                key_id=key_id,
                db_instance=db,
                generation_meta={"category": st.last_generation_category or "normal", "user_id": user_id},
            ),
            timeout=total_timeout_s,
        )
        if not result_path:
            await _reply(message, get_string("gen_no_image", lang), keyboard=back_to_main_keyboard(lang))
            return True
        await db.subtract_user_balance(user_id=user_id, amount=price, reason="generation")
        if key_id is not None:
            try:
                await _vk_record_api_usage(db, key_id)
            except Exception:
                pass
        await _save_generation_history(
            db=db,
            user_id=user_id,
            category=st.last_generation_category,
            prompt=st.last_generation_prompt,
            result_path=str(result_path),
            params={"flow": st.last_generation_flow or "normal", "repeat": True, "aspect": aspect.replace("x", ":"), "quality": quality_choice},
        )
        _remember_last_generation(
            st,
            flow=st.last_generation_flow or "normal",
            category=st.last_generation_category,
            prompt=st.last_generation_prompt,
            images=repeat_images,
            aspect=aspect,
            quality=quality_choice,
        )
        await _send_generation_result(
            message,
            db,
            user_id,
            lang,
            str(result_path),
            allow_edit=st.last_generation_category not in {"own", "own_variant"},
        )
        return True
    except Exception as e:
        logger.exception("VK repeat generation failed for user_id=%s", user_id)
        await _handle_generation_error(message, db, user_id, lang, e, category=st.last_generation_category, prompt=st.last_generation_prompt, quality=quality_choice)
        return True
    finally:
        gen_done.set()
        if progress_task:
            await asyncio.sleep(0.3)
            progress_task.cancel()
            try:
                await progress_task
            except (asyncio.CancelledError, Exception):
                pass
        if progress_msg_id:
            await _delete_vk_message(message, progress_msg_id)
        release_key_lock(key_lock)


async def _handle_support_message(message: Message, db: Database, user_id: int, lang: str) -> bool:
    st = _get_state(user_id)
    if st.stage != STATE_SUPPORT:
        return False
    text = (message.text or "").strip()
    if _norm(text) in {_norm(get_string("back_main", lang)), _norm(get_string("back", lang))}:
        _reset_state(user_id)
        await _send_main_menu(message, db, user_id)
        return True
    images = await _download_photo_bytes_list(message)
    if not text and not images:
        await _reply(message, get_string("vk_enter_step_value", lang), keyboard=support_keyboard(lang))
        return True
    try:
        file_type = "photo" if images else "text"
        await db.add_support_message(user_id, text=text or None, file_id=None, file_type=file_type, is_admin=False)
    except Exception:
        logger.exception("VK add support message failed user_id=%s", user_id)
        await _reply(message, get_string("vk_temp_error", lang), keyboard=support_keyboard(lang))
        return True
    await _reply(message, "Сообщение отправлено в поддержку.", keyboard=support_keyboard(lang))
    return True


async def _is_vk_group_member(message: Message, group_id: int, user_id: int) -> bool:
    try:
        res = await message.ctx_api.groups.is_member(group_id=group_id, user_id=user_id)
    except Exception:
        res = await message.ctx_api.request(
            "groups.isMember",
            {"group_id": int(group_id), "user_id": int(user_id)},
        )
    if isinstance(res, dict):
        return int(res.get("member", 0)) == 1
    member_val = getattr(res, "member", res)
    try:
        return int(member_val) == 1
    except Exception:
        return False


async def _ensure_vk_subscription_access(message: Message, db: Database, user_id: int, lang: str) -> bool:
    group_raw = (await db.get_app_setting("required_vk_group_id", "")) or ""
    group_raw = str(group_raw).strip()
    if not group_raw:
        return True
    try:
        group_id = abs(int(group_raw))
    except Exception:
        return True
    is_member = await _is_vk_group_member(message, group_id=group_id, user_id=user_id)
    if is_member:
        return True

    group_url = (await db.get_app_setting("required_vk_group_url", "")) or ""
    group_url = str(group_url).strip() or f"https://vk.com/public{group_id}"
    text = (
        "Для использования бота нужно подписаться на сообщество.\n"
        f"Подпишитесь: {group_url}\n\n"
        "После подписки нажмите «Проверить подписку»."
    )
    await _reply(message, text, keyboard=_vk_subscription_check_keyboard(lang))
    return False


@router.message(IsPrivate())
async def handle_private_message(message: Message) -> None:
    db: Database = get_db()
    user_id = int(message.from_id)

    try:
        now = time.time()
        fingerprint = _incoming_fingerprint(message)
        logger.info("[INCOMING] user=%s msg_id=%s conv_id=%s text=%.60s fp=%s",
                    user_id, getattr(message, 'id', None),
                    getattr(message, 'conversation_message_id', None),
                    (message.text or '').replace('\n', ' ')[:60], fingerprint)
        if _is_duplicate_incoming_event(fingerprint, now):
            logger.info("VK duplicate event skipped user_id=%s fingerprint=%s", user_id, fingerprint)
            return

        st = _get_state(user_id)
        if (
            fingerprint
            and st.last_incoming_fingerprint == fingerprint
            and now - st.last_incoming_at <= DUPLICATE_EVENT_TTL_SECONDS
        ):
            logger.info("VK duplicate event skipped user_id=%s fingerprint=%s", user_id, fingerprint)
            return
        st.last_incoming_fingerprint = fingerprint
        st.last_incoming_at = now
        incoming_id = getattr(message, "id", None) or getattr(message, "conversation_message_id", None)
        if incoming_id is not None and st.last_incoming_msg_id == int(incoming_id):
            return
        if incoming_id is not None:
            st.last_incoming_msg_id = int(incoming_id)

        raw_text = (message.text or "").strip()
        if (
            raw_text
            and raw_text == st.last_incoming_text
            and now - st.last_incoming_text_at < 2.0
        ):
            logger.info("VK duplicate text skipped user_id=%s text=%.40s", user_id, raw_text)
            return
        st.last_incoming_text = raw_text
        st.last_incoming_text_at = now

        await _ensure_user(db, user_id)
        lang = await _get_lang(db, user_id)

        if await db.get_user_blocked(user_id):
            await _reply(message, get_string("user_blocked", lang))
            return

        text = _norm(message.text)

        if text in {"/admin", "admin"}:
            return

        
        if _is_reset_text(text):
            _reset_state(user_id)
            await _send_main_menu(message, db, user_id)
            return

        
        accepted = await db.get_user_accepted_terms(user_id)
        accept_text = _norm(get_string("accept_terms", lang))
        agreement_text = _norm(get_string("agreement", lang))
        if not accepted:
            if text == accept_text:
                await db.set_terms_acceptance(user_id, True)
                await _send_main_menu(message, db, user_id)
                return
            if text == agreement_text:
                await _send_agreement(message, db, user_id)
                return
            if _is_start(text):
                ok = await _reply(message, get_string("start_welcome", lang), keyboard=terms_keyboard(lang))
                if not ok:
                    await _reply(
                        message,
                        f"Напишите текстом:\n- {get_string('accept_terms', lang)}\n- {get_string('agreement', lang)}",
                    )
                return

            ok = await _reply(message, get_string("start_welcome", lang), keyboard=terms_keyboard(lang))
            if not ok:
                await _reply(
                    message,
                    f"Напишите текстом:\n- {get_string('accept_terms', lang)}\n- {get_string('agreement', lang)}",
                )
            return

        if not await _ensure_vk_subscription_access(message, db, user_id, lang):
            return

        
        if _is_start(text):
            _reset_state(user_id)
            await _send_main_menu(message, db, user_id)
            return
        if _is_help(text):
            _reset_state(user_id)
            await _send_howto(message, db, user_id)
            return

        
        if await _handle_support_message(message, db, user_id, lang):
            return
        if await _handle_result_actions(message, db, user_id, lang):
            return
        if await _handle_quality_and_confirm(message, db, user_id, lang):
            return
        if await _handle_presets_message(message, db, user_id, lang):
            return
        if await _handle_constructor_message(message, db, user_id, lang):
            return

        
        if text == _norm(get_string("create_normal_gen", lang)):
            await _start_generation_mode(message, db, user_id, lang, category=None)
            return
        if text == _norm(get_string("menu_market", lang)):
            _reset_state(user_id)
            await _send_marketplace_menu(message, db, user_id)
            return
        market_cat = _market_category_by_text(text, lang)
        if market_cat:
            if market_cat == "infographics":
                await _send_infographics_menu(message, db, user_id)
                return
            if market_cat == "presets":
                await _start_presets_flow(message, db, user_id, lang)
                return
            await _start_constructor_flow(message, db, user_id, lang, market_cat)
            return
        if text == _norm(get_string("menu_settings", lang)):
            _reset_state(user_id)
            await _reply(message, get_string("menu_settings", lang), keyboard=settings_keyboard(lang))
            return
        if text == _norm(get_string("menu_profile", lang)):
            _reset_state(user_id)
            await _send_profile(message, db, user_id)
            return
        if text == _norm(get_string("menu_history", lang)):
            _reset_state(user_id)
            await _send_history(message, db, user_id)
            return
        if text == _norm(get_string("buy_plan", lang)) or text == _norm(get_string("menu_subscription", lang)):
            _reset_state(user_id)
            await _send_topup(message, db, user_id)
            return
        if text == _norm(get_string("menu_support", lang)):
            _reset_state(user_id)
            await _send_support(message, db, user_id)
            return
        if text == _norm(get_string("menu_howto", lang)):
            _reset_state(user_id)
            await _send_howto(message, db, user_id)
            return
        if text == _norm(get_string("agreement", lang)):
            _reset_state(user_id)
            await _send_agreement(message, db, user_id)
            return
        
        if text == _norm(get_string("select_lang", lang)):
            _reset_state(user_id)
            await _reply(message, get_string("select_lang", lang), keyboard=language_keyboard(lang))
            return
        if text == _norm(get_string("lang_ru", lang)):
            await db.set_user_language(user_id, "ru")
            _reset_state(user_id)
            await _send_main_menu(message, db, user_id)
            return
        if text == _norm(get_string("lang_en", lang)):
            await db.set_user_language(user_id, "en")
            _reset_state(user_id)
            await _send_main_menu(message, db, user_id)
            return
        if text == _norm(get_string("lang_vi", lang)):
            await db.set_user_language(user_id, "vi")
            _reset_state(user_id)
            await _send_main_menu(message, db, user_id)
            return

        
        if text == _norm(get_string("back", lang)):
            _reset_state(user_id)
            await _send_marketplace_menu(message, db, user_id)
            return
        if text == _norm(get_string("back_main", lang)):
            _reset_state(user_id)
            await _send_main_menu(message, db, user_id)
            return

        
        if await _handle_generation_photo_step(message, db, user_id, lang):
            return
        if await _handle_generation_aspect_step(message, db, user_id, lang):
            return
        if await _handle_generation_prompt_step(message, db, user_id, lang):
            return

        
        if not text:
            await _send_main_menu(message, db, user_id)
            return

        
        if _is_repeat_text(text, lang):
            await _reply(
                message,
                "Сессия истекла. Начните новую генерацию из главного меню.",
                keyboard=main_menu_keyboard(lang),
            )
            return
        await _send_main_menu(message, db, user_id)
    except Exception:
        logger.exception("VK handler error for user_id=%s", user_id)
        try:
            safe_lang = locals().get("lang", "ru")
            await _reply(message, get_string("vk_temp_error", safe_lang if safe_lang in {"ru", "en", "vi"} else "ru"))
        except Exception:
            pass
