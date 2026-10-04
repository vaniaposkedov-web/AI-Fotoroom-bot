import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import time
import uuid
from typing import Optional

import aiohttp
import aiosqlite

logger = logging.getLogger(__name__)

# Определение базовой директории для сохранения истории
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Настройки сжатия и лимитов для работы с API
MAX_IMAGE_DIM = 1536
JPEG_QUALITY = 98

_ZVENO_KEY_SLOT_LOCK = asyncio.Lock()
_ZVENO_CACHE_LOCK = asyncio.Lock()
_ZVENO_KEYS_CACHE: list[str] = []
_ZVENO_KEYS_CACHE_TS = 0.0
_ZVENO_ACTIVE_BY_KEY: dict[str, int] = {}


def get_zveno_api_key() -> str:
    """Извлекает API ключ ZvenoAI из переменных окружения."""
    return (os.getenv("ZVENO_API_KEY") or "").strip()


def _is_enabled(flag_name: str, default: str = "1") -> bool:
    raw = str(os.getenv(flag_name, default)).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, str(default))).strip())
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except Exception:
        return int(default)


def _env_csv(name: str, default: str = "") -> list[str]:
    raw = str(os.getenv(name, default) or "").strip()
    return [item.strip() for item in raw.split(",") if item.strip()]


def _build_zveno_image_provider_preferences() -> dict | None:
    if not _is_enabled("ZVENO_IMAGE_PROVIDER_ENABLED", "1"):
        return None
    provider: dict = {}
    only = _env_csv("ZVENO_IMAGE_PROVIDER_ONLY")
    order = _env_csv("ZVENO_IMAGE_PROVIDER_ORDER")
    # Раньше дефолт был google-ai-studio — из‑за этого 502 на gemini-3.1-flash-image-preview
    # (у preview единственный endpoint как раз google-ai-studio). Ignore только если явно задан в env.
    ignore = _env_csv("ZVENO_IMAGE_PROVIDER_IGNORE", "")
    if only:
        provider["only"] = only
    if order:
        provider["order"] = order
    if ignore:
        provider["ignore"] = ignore
    provider["allow_fallbacks"] = _is_enabled("ZVENO_IMAGE_PROVIDER_ALLOW_FALLBACKS", "1")
    return provider or None


def _mask_key_preview(key: str) -> str:
    if not key:
        return "n/a"
    if len(key) <= 8:
        return f"{key[:2]}***"
    return f"{key[:4]}...{key[-4:]}"


def _bot_db_path() -> str:
    raw = (os.getenv("DATABASE_URL") or "").strip()
    if raw.startswith("sqlite+aiosqlite:///"):
        path = raw.replace("sqlite+aiosqlite:///", "", 1)
        if not path.startswith("/"):
            path = "/" + path
        return path
    if raw.startswith("sqlite:///"):
        path = raw.replace("sqlite:///", "", 1)
        if not path.startswith("/"):
            path = "/" + path
        return path
    return os.getenv("BOT_DB_PATH", "/app/data/bot.db")


async def _load_active_zveno_keys_from_db() -> list[str]:
    db_path = _bot_db_path()
    keys: list[str] = []
    try:
        async with aiosqlite.connect(db_path) as conn:
            # priority column optional in old schemas.
            try:
                async with conn.execute(
                    "SELECT token FROM api_keys WHERE is_active=1 ORDER BY COALESCE(priority, 0) DESC, id ASC"
                ) as cur:
                    rows = await cur.fetchall()
            except Exception:
                async with conn.execute("SELECT token FROM api_keys WHERE is_active=1 ORDER BY id ASC") as cur:
                    rows = await cur.fetchall()
            for row in rows:
                token = (row[0] or "").strip()
                if token and token not in keys:
                    keys.append(token)
    except Exception as exc:
        logger.warning("[ZVENO] failed to read api_keys from db %s: %s", db_path, exc)
    return keys


async def get_zveno_api_keys_cached() -> list[str]:
    global _ZVENO_KEYS_CACHE, _ZVENO_KEYS_CACHE_TS
    cache_ttl = max(3.0, _env_float("ZVENO_KEYS_CACHE_TTL_SECONDS", 15.0))
    now = time.time()
    async with _ZVENO_CACHE_LOCK:
        if _ZVENO_KEYS_CACHE and (now - _ZVENO_KEYS_CACHE_TS) < cache_ttl:
            return list(_ZVENO_KEYS_CACHE)
        keys = await _load_active_zveno_keys_from_db()
        # Fallback to env key for bootstrap and recovery.
        env_key = get_zveno_api_key()
        if env_key and env_key not in keys:
            keys.insert(0, env_key)
        _ZVENO_KEYS_CACHE = keys
        _ZVENO_KEYS_CACHE_TS = now
        return list(keys)


async def acquire_zveno_key_slot(wait_seconds: float | None = None) -> str | None:
    keys = await get_zveno_api_keys_cached()
    if not keys:
        return None
    per_key_limit = max(1, _env_int("ZVENO_MAX_CONCURRENT_PER_KEY", 20))
    wait_for = max(0.5, wait_seconds if wait_seconds is not None else _env_float("ZVENO_KEY_WAIT_SECONDS", 4.0))
    sleep_step = max(0.03, _env_float("ZVENO_KEY_WAIT_POLL_SECONDS", 0.06))
    deadline = asyncio.get_running_loop().time() + wait_for
    while True:
        async with _ZVENO_KEY_SLOT_LOCK:
            for key in keys:
                _ZVENO_ACTIVE_BY_KEY.setdefault(key, 0)
            available = [k for k in keys if _ZVENO_ACTIVE_BY_KEY.get(k, 0) < per_key_limit]
            if available:
                picked = min(available, key=lambda k: _ZVENO_ACTIVE_BY_KEY.get(k, 0))
                _ZVENO_ACTIVE_BY_KEY[picked] = _ZVENO_ACTIVE_BY_KEY.get(picked, 0) + 1
                return picked
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(sleep_step)


async def release_zveno_key_slot(key: str | None) -> None:
    if not key:
        return
    async with _ZVENO_KEY_SLOT_LOCK:
        if key in _ZVENO_ACTIVE_BY_KEY:
            _ZVENO_ACTIVE_BY_KEY[key] = max(0, _ZVENO_ACTIVE_BY_KEY[key] - 1)


def _provider_rub_from_zveno_payload(usage: dict | None, full_response: dict | None) -> float | None:
    """Если Zveno вернул стоимость в ответе — используем её как total_rub (точнее оценки по токенам)."""
    blobs: list[dict] = []
    if isinstance(usage, dict):
        blobs.append(usage)
    if isinstance(full_response, dict):
        blobs.append(full_response)
        nested = full_response.get("usage")
        if isinstance(nested, dict) and nested is not usage:
            blobs.append(nested)
    for b in blobs:
        for key in ("total_cost_rub", "cost_rub", "total_cost", "cost", "billed_rub", "zveno_cost_rub"):
            val = b.get(key)
            if val is None:
                continue
            try:
                x = float(val)
                if x >= 0:
                    return x
            except (TypeError, ValueError):
                continue
    return None


async def record_zveno_usage(
    *,
    api_key: str,
    model: str,
    usage: dict | None,
    latency_ms: int,
    status: str,
    error_text: str = "",
    full_response: dict | None = None,
    user_id: int | None = None,
) -> None:
    db_path = _bot_db_path()
    raw_prompt_tokens = int((usage or {}).get("prompt_tokens") or 0)
    completion_tokens = int((usage or {}).get("completion_tokens") or 0)
    prompt_tokens = raw_prompt_tokens
    total_tokens = int((usage or {}).get("total_tokens") or (prompt_tokens + completion_tokens))
    # Фиксированная ставка за 1 API-вызов (из env или 6.3 ₽ по умолчанию).
    flat_rate = _env_float("ZVENO_FLAT_RATE_PER_CALL_RUB", 6.3)
    provider_rub = _provider_rub_from_zveno_payload(usage, full_response)
    if provider_rub is not None:
        total_rub = float(provider_rub)
    else:
        total_rub = flat_rate
    input_rub = total_rub
    output_rub = 0.0
    key_preview = _mask_key_preview(api_key)
    try:
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS zveno_usage_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    user_id INTEGER,
                    key_id INTEGER,
                    key_preview TEXT,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ok',
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    input_rub REAL NOT NULL DEFAULT 0,
                    output_rub REAL NOT NULL DEFAULT 0,
                    total_rub REAL NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    error_text TEXT
                )
                """
            )
            try:
                async with conn.execute("PRAGMA table_info(zveno_usage_log)") as cur:
                    cols = await cur.fetchall()
                col_names = {str(r[1]) for r in (cols or [])}
                if "user_id" not in col_names:
                    await conn.execute("ALTER TABLE zveno_usage_log ADD COLUMN user_id INTEGER")
            except Exception:
                pass
            key_id = None
            try:
                async with conn.execute("SELECT id FROM api_keys WHERE token=? LIMIT 1", (api_key,)) as cur:
                    row = await cur.fetchone()
                    if row:
                        key_id = int(row[0])
            except Exception:
                key_id = None
            await conn.execute(
                """
                INSERT INTO zveno_usage_log
                (user_id, key_id, key_preview, model, status, prompt_tokens, completion_tokens, total_tokens,
                 input_rub, output_rub, total_rub, latency_ms, error_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(user_id) if user_id is not None else None,
                    key_id,
                    key_preview,
                    str(model or ""),
                    str(status or "ok"),
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    float(input_rub),
                    float(output_rub),
                    float(total_rub),
                    int(latency_ms),
                    (error_text or "")[:500],
                ),
            )
            await conn.commit()
    except Exception as exc:
        logger.warning("[ZVENO] failed to persist usage stats: %s", exc)


def _extract_chat_content_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("text", "output_text"):
                txt = str(part.get("text") or "").strip()
                if txt:
                    chunks.append(txt)
        return "\n".join(chunks).strip()
    return ""


async def enrich_prompt_with_zveno(
    session: aiohttp.ClientSession,
    base_prompt: str,
    images_bytes: list[bytes],
    aspect_ratio: str,
    category: str,
) -> tuple[str, bool]:
    """
    Обогащает финальный промпт через Gemini 3.1 Pro (ZvenoAI), сохраняя fallback на исходный текст.
    """
    # Prompt-opt полностью отключен: в image-запрос отправляем только готовый промпт + фото.
    return base_prompt, False

    api_key = await acquire_zveno_key_slot()
    if not api_key:
        logger.info("[ZVENO] Нет доступных активных ключей, используем исходный промпт.")
        return base_prompt, False

    endpoint = (os.getenv("ZVENO_BASE_URL") or "https://api.zveno.ai/v1").rstrip("/") + "/chat/completions"
    model = (os.getenv("ZVENO_MODEL") or "google/gemini-3.1-pro-preview").strip()
    timeout_sec = int(str(os.getenv("ZVENO_TIMEOUT_SECONDS", "40")).strip() or "40")

    try:
        started_ts = time.time()
        limited_images = (images_bytes or [])[:2]
        content_parts: list[dict] = [
            {
                "type": "text",
                "text": (
                    "You are a prompt optimizer for photorealistic fashion product generation.\n"
                    "Task: rewrite and optimize the prompt while preserving ALL user/admin constraints.\n"
                    "Rules:\n"
                    "1) Keep every hard constraint and prohibited action.\n"
                    "2) Keep exact intent for identity/background/garment fidelity.\n"
                    "3) If images are attached, account for them in wording.\n"
                    "4) Return ONLY the final optimized prompt text, no markdown, no explanations.\n"
                    f"Category: {category or 'unknown'}\n"
                    f"Aspect ratio: {aspect_ratio or '1:1'}\n\n"
                    "SOURCE PROMPT:\n"
                    f"{base_prompt}"
                ),
            }
        ]

        for raw_img in limited_images:
            prep_bytes, mime = prepare_image_for_upload(raw_img, "image/jpeg")
            b64 = base64.b64encode(prep_bytes).decode("ascii")
            content_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                }
            )

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content_parts}],
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        async with session.post(endpoint, headers=headers, json=payload, timeout=timeout_sec) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning("[ZVENO] HTTP %s: %s", resp.status, body[:500])
                await record_zveno_usage(
                    api_key=api_key,
                    model=model,
                    usage=None,
                    latency_ms=int((time.time() - started_ts) * 1000),
                    status=f"http_{resp.status}",
                    error_text=body[:300],
                )
                return base_prompt, False
            data = await resp.json()

        choices = data.get("choices") or []
        if not choices:
            logger.warning("[ZVENO] Empty choices in response.")
            return base_prompt, False

        message = choices[0].get("message") or {}
        rewritten = _extract_chat_content_text(message.get("content"))
        if not rewritten:
            logger.warning("[ZVENO] Empty rewritten prompt, fallback to original.")
            await record_zveno_usage(
                api_key=api_key,
                model=model,
                usage=data.get("usage") if isinstance(data, dict) else None,
                latency_ms=int((time.time() - started_ts) * 1000),
                status="empty_rewrite",
                error_text="empty rewritten content",
                full_response=data if isinstance(data, dict) else None,
            )
            return base_prompt, False

        await record_zveno_usage(
            api_key=api_key,
            model=model,
            usage=data.get("usage") if isinstance(data, dict) else None,
            latency_ms=int((time.time() - started_ts) * 1000),
            status="ok",
            full_response=data if isinstance(data, dict) else None,
        )
        logger.info("[ZVENO] Prompt optimized: %d -> %d chars", len(base_prompt), len(rewritten))
        return rewritten, True
    except Exception as exc:
        await record_zveno_usage(
            api_key=api_key,
            model=model,
            usage=None,
            latency_ms=0,
            status="exception",
            error_text=str(exc),
        )
        logger.warning("[ZVENO] Prompt optimization failed: %s", exc)
        return base_prompt, False
    finally:
        await release_zveno_key_slot(api_key)


def _resolve_zveno_image_model(model_name: str | None) -> str:
    """Модель для генерации изображения в Zveno (формат vendor/model)."""
    default_raw = (os.getenv("ZVENO_IMAGE_MODEL") or "google/gemini-3-pro-image-preview").strip()
    mn = (model_name or "").strip()
    if not mn or "vertex" in mn.lower():
        return default_raw if "/" in default_raw else f"google/{default_raw.removeprefix('google/')}"
    if "/" in mn:
        return mn
    if mn.startswith("google/"):
        return mn
    return f"google/{mn}"


def _is_hard_generation_category(category: str | None) -> bool:
    cat = (category or "").strip().lower()
    return cat in {"own", "own_variant"}


def _pick_zveno_image_model(model_name: str | None, generation_meta: dict | None, quality: str = "HD") -> str:
    # Явно заданная модель имеет приоритет.
    if (model_name or "").strip():
        return _resolve_zveno_image_model(model_name)
    if str(quality).lower() == "4k":
        # Все 4K → nano banana pro (детальная модель). Нативный 4K (3712×4608)
        # обеспечивает image_size="4K" + чтение последнего кадра из ответа.
        pro = (os.getenv("ZVENO_IMAGE_MODEL_PRO") or "google/gemini-3-pro-image-preview").strip()
        return _resolve_zveno_image_model(pro)
    model = os.getenv("ZVENO_IMAGE_MODEL_CHEAP") or "google/gemini-3.1-flash-image-preview"
    return _resolve_zveno_image_model(model)


def prepare_image_for_upload(img_bytes: bytes, fallback_mime_type: str = "image/jpeg") -> tuple[bytes, str]:
    """
    Подготавливает изображение к загрузке: проверяет формат и сжимает до 1536px,
    если это необходимо для ускорения передачи данных.
    """
    try:
        from PIL import Image
        image = Image.open(io.BytesIO(img_bytes))
        fmt = (image.format or "").upper()
        width, height = image.size

        if width > MAX_IMAGE_DIM or height > MAX_IMAGE_DIM:
            scale = min(MAX_IMAGE_DIM / width, MAX_IMAGE_DIM / height, 1.0)
            new_size = (int(width * scale), int(height * scale))
            image = image.convert("RGB").resize(new_size, Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True)
            return buffer.getvalue(), "image/jpeg"

        if fmt in {"JPEG", "JPG"}: return img_bytes, "image/jpeg"
        if fmt == "PNG": return img_bytes, "image/png"
        if fmt == "WEBP": return img_bytes, "image/webp"

        image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True)
        return buffer.getvalue(), "image/jpeg"
    except Exception as exc:
        logger.debug("[image] Ошибка подготовки изображения, используем оригинал: %s", exc)
        return img_bytes, fallback_mime_type


def compress_output_image(img_bytes: bytes) -> bytes:
    """Сжимает итоговое изображение перед отправкой в Telegram."""
    try:
        from PIL import Image
        image = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        width, height = image.size
        if width <= MAX_IMAGE_DIM and height <= MAX_IMAGE_DIM:
            return img_bytes

        scale = min(MAX_IMAGE_DIM / width, MAX_IMAGE_DIM / height, 1.0)
        new_size = (int(width * scale), int(height * scale))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True)
        return buffer.getvalue()
    except Exception as exc:
        logger.debug("[image] Ошибка финального сжатия: %s", exc)
        return img_bytes


def _build_zveno_generation_suffix(current_prompt: str, n_images: int, generation_meta: dict | None) -> str:
    """Текстовые инструкции к промпту (дополнение к user-сообщению для image-модели)."""
    generation_meta = generation_meta or {}
    prompt_low = (current_prompt or "").lower()
    cat_from_meta = str(generation_meta.get("category", "") or "")
    model_id = str(generation_meta.get("model_id", "") or "")

    universal_override = (
        " Preserve original details and patterns. "
        "ORIENTATION: Match the front or back view of the uploaded garment naturally. Do not force a front view if a back view is provided."
    )
    universal_negative = "low quality, blurry, text, watermark, signature, letters, deformed, distorted, interface, crop"

    if cat_from_meta == "collage":
        return ""

    if n_images <= 0:
        return (
            "\n\nGenerate exactly ONE photorealistic image from the instructions. "
            "No collage, no split view, no watermarks, no UI overlays."
        )

    if n_images == 1:
        if cat_from_meta == "whitebg":
            return (
                "\n\nCRITICAL OVERRIDE FOR 1 PHOTO: "
                "Put the EXACT item from the photo on a pure white (#FFFFFF) background. "
                "This is strictly a background removal and lighting adjustment task. "
                "Create a 1:1 exact copy of the garment, preserving its current state exactly, just with soft professional studio lighting."
                + universal_override
                + f"\nAvoid: {universal_negative}, added details, extra seams, extra pockets, invented features."
            )
        if cat_from_meta == "storefront":
            return (
                "\n\nSTOREFRONT (1 PRODUCT PHOTO): "
                "Use the uploaded garment image as the ONLY product source — keep its design, colors, logos, and proportions identical. "
                "Build the final storefront / showcase shot described in the prompt text only (scene, lighting, layout). "
                "Do not invent a different garment; integrate THIS exact item into the scene."
                + universal_override
                + f"\nAvoid: {universal_negative}, bad anatomy, humans, hands, distorted product, wrong colors, extra items."
            )
        return (
            "\n\nCRITICAL OVERRIDE: "
            "Place the original garment exactly as it is into the scene described in the prompt. "
            "Generate a realistic background based ONLY on the text prompt. "
            + universal_override
            + f"\nAvoid: {universal_negative}, bad anatomy, logo, writing."
        )

    user_context = prompt_low.split("additional details:")[-1] if "additional details:" in prompt_low else prompt_low[-250:]
    footwear_keywords = [
        "shoes", "footwear", "sneakers", "boots", "обувь", "кроссовки", "кеды", "туфли", "ботинки",
    ]
    storefront_keywords = ["storefront", "showcase", "hanger", "product-in-scene"]
    is_footwear = any(word in user_context for word in footwear_keywords)
    is_storefront = any(word in user_context for word in storefront_keywords)
    is_own_bg = cat_from_meta == "own_variant"

    if is_footwear:
        return (
            "\n\nFOOTWEAR OVERRIDE: Replace the original footwear with the item from Photo 2. Keep the pants and background intact."
            + universal_override
            + f"\nAvoid: {universal_negative}, change pants, change jeans, alter background, morphing."
        )
    if cat_from_meta == "own":
        return (
            "\n\nCRITICAL MAPPING:\n"
            "Photo 1 (SCENE_AND_MODEL_REFERENCE) = first attached image.\n"
            "Photo 2 (CLOTHING_ITEM_TO_WEAR) = second attached image."
            + universal_override
            + f"\nAvoid: {universal_negative}."
        )
    if is_own_bg:
        return (
            "\n\nCRITICAL — TWO INPUT IMAGES (PHOTO 1 = BACKGROUND REFERENCE, PHOTO 2 = PRODUCT — ONLY SOURCE OF TRUTH FOR THE PRODUCT):\n"
            "First attached image = BACKGROUND / scene reference.\n"
            "Second attached image = PRODUCT (garment) — preserve design, colors, logos, texture 1:1.\n"
            "The output MUST be a new composite integrating the product into the background; "
            "it MUST NOT be an unchanged copy of the background photo alone."
            + universal_override
            + f"\nAvoid: {universal_negative}, identical copy of background frame, passthrough, missing garment, wrong product."
        )
    if is_storefront:
        if model_id == "13":
            return (
                "\n\nSTOREFRONT OVERRIDE (MODEL 13): "
                "Replace the main clothing with the garment from Photo 2. "
                "IMPORTANT: Keep the background and the human hand holding the item intact. "
                "Blend the new garment naturally behind the hand."
                + universal_override
                + f"\nAvoid: {universal_negative}, change background, modified environment, missing parts, deformed."
            )
        return (
            "\n\nSTOREFRONT OVERRIDE: "
            "Replace the existing clothing in the scene with the garment from Photo 2. "
            "Keep the original background and props intact. Strictly product only, no humans."
            + universal_override
            + f"\nAvoid: {universal_negative}, bad anatomy, logo, writing, messy scene."
        )
    return (
        "\n\nMODEL OVERRIDE: The new garment (Photo 2) must be worn naturally by the person in Photo 1. "
        "Change the shape of the original clothes to match the new garment. "
        "The background, face, and pose must remain the same as Photo 1."
        + universal_override
        + f"\nAvoid: {universal_negative}, multiple garments, floating items, cloned clothes, split garment, bad anatomy."
    )


def _zveno_extract_image_ref(data: dict) -> str | None:
    choices = data.get("choices") or []
    if not choices:
        return None
    msg = (choices[0] or {}).get("message") or {}
    imgs = msg.get("images")
    if isinstance(imgs, list) and imgs:
        # Gemini отдаёт промежуточные кадры рендера, а финал в полном разрешении — ПОСЛЕДНИЙ.
        # Для 1K приходит одна картинка, так что imgs[-1] == imgs[0] — поведение не меняется.
        el = imgs[-1]
        if isinstance(el, dict):
            u = el.get("url") or el.get("image_url")
            if isinstance(u, dict):
                u = u.get("url")
            if u:
                return str(u)
            b64 = el.get("b64_json")
            if b64:
                return "data:image/png;base64," + str(b64)
    content = msg.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            iu = part.get("image_url")
            if isinstance(iu, dict) and iu.get("url"):
                return str(iu["url"])
            t = str(part.get("type") or "").lower()
            if t in ("output_image", "image") and isinstance(part.get("url"), str):
                return str(part["url"])
    if isinstance(content, str):
        import re
        m = re.search(r"\((https?://[^)\s]+\.(?:png|jpg|jpeg|webp))\)", content, re.I)
        if m:
            return m.group(1)
    return None


def _zveno_extract_native_finish_reason(data: dict | None) -> str:
    """Пробуем достать provider-native reason из разных вариантов payload."""
    if not isinstance(data, dict):
        return ""
    candidates: list[str] = []
    for key in ("native_finish_reason", "provider_finish_reason", "finish_reason_native"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            candidates.append(val.strip())
    choices = data.get("choices")
    if isinstance(choices, list):
        for ch in choices[:2]:
            if not isinstance(ch, dict):
                continue
            for key in ("native_finish_reason", "provider_finish_reason", "finish_reason_native"):
                val = ch.get(key)
                if isinstance(val, str) and val.strip():
                    candidates.append(val.strip())
            msg = ch.get("message")
            if isinstance(msg, dict):
                for key in ("native_finish_reason", "provider_finish_reason", "finish_reason_native"):
                    val = msg.get(key)
                    if isinstance(val, str) and val.strip():
                        candidates.append(val.strip())
    return candidates[0] if candidates else ""


def _zveno_extract_finish_reasons(data: dict | None) -> list[str]:
    if not isinstance(data, dict):
        return []
    reasons: list[str] = []
    for key in ("finish_reason", "finishReason", "native_finish_reason", "provider_finish_reason", "finish_reason_native"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            reasons.append(val.strip())
    choices = data.get("choices")
    if isinstance(choices, list):
        for ch in choices[:3]:
            if not isinstance(ch, dict):
                continue
            for key in ("finish_reason", "finishReason", "native_finish_reason", "provider_finish_reason", "finish_reason_native"):
                val = ch.get(key)
                if isinstance(val, str) and val.strip():
                    reasons.append(val.strip())
            msg = ch.get("message")
            if isinstance(msg, dict):
                for key in ("finish_reason", "finishReason", "native_finish_reason", "provider_finish_reason", "finish_reason_native"):
                    val = msg.get(key)
                    if isinstance(val, str) and val.strip():
                        reasons.append(val.strip())
    out: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        key = reason.upper()
        if key not in seen:
            seen.add(key)
            out.append(reason)
    return out


def _zveno_no_image_summary(data: dict | None) -> str:
    if not isinstance(data, dict):
        return "payload_type=non_dict"
    choices = data.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    msg = choice.get("message") if isinstance(choice, dict) else {}
    if not isinstance(msg, dict):
        msg = {}
    content = msg.get("content")
    images = msg.get("images")
    content_len = len(content) if isinstance(content, (list, str)) else 0
    images_count = len(images) if isinstance(images, list) else 0
    reasons = ",".join(_zveno_extract_finish_reasons(data)) or "n/a"
    code = str(data.get("code") or "n/a")
    message = str(data.get("message") or "")[:240].replace("\n", " ")
    refusal = str(msg.get("refusal") or "")[:240].replace("\n", " ")
    return (
        f"code={code}; reasons={reasons}; content_type={type(content).__name__}; "
        f"content_len={content_len}; images_count={images_count}; "
        f"message={message or 'n/a'}; refusal={refusal or 'n/a'}"
    )


def _zveno_response_error_text(data: dict | None) -> str:
    if not isinstance(data, dict):
        return ""
    parts: list[str] = []
    for key in ("code", "message", "status"):
        val = data.get(key)
        if val:
            parts.append(str(val))
    return " ".join(parts)


def _is_location_unsupported_response(data: dict | None) -> bool:
    err = _zveno_response_error_text(data).lower()
    return "user location is not supported" in err or "failed_precondition" in err


def _is_image_safety_reason(reason: str) -> bool:
    upper_reason = (reason or "").upper()
    return "IMAGE_SAFETY" in upper_reason


def _is_safety_no_image_response(data: dict | None) -> bool:
    return any("SAFETY" in reason.upper() or "PROHIBITED" in reason.upper() for reason in _zveno_extract_finish_reasons(data))


async def _zveno_fetch_image_bytes(session: aiohttp.ClientSession, ref: str) -> bytes:
    if ref.startswith("data:"):
        comma = ref.find(",")
        if comma == -1:
            return b""
        meta, b64part = ref[:comma], ref[comma + 1 :]
        if ";base64" in meta:
            return base64.b64decode(b64part)
        return base64.b64decode(b64part)
    async with session.get(ref, timeout=aiohttp.ClientTimeout(total=120)) as r:
        r.raise_for_status()
        return await r.read()


async def _zveno_image_chat_request(
    session: aiohttp.ClientSession,
    api_key: str,
    model: str,
    user_text: str,
    images_bytes: list[bytes],
    mime_type: str,
    aspect_ratio: str,
    image_labels: list[str] | None = None,
    image_size: str | None = None,
) -> tuple[bytes, dict]:
    endpoint = (os.getenv("ZVENO_BASE_URL") or "https://api.zveno.ai/v1").rstrip("/") + "/chat/completions"
    timeout_sec = max(45, _env_int("ZVENO_IMAGE_TIMEOUT_SECONDS", 60))
    parts: list[dict] = [{"type": "text", "text": user_text}]
    for idx, raw in enumerate(images_bytes[:8]):
        if image_labels and idx < len(image_labels):
            parts.append({"type": "text", "text": image_labels[idx]})
        prep_bytes, pm = prepare_image_for_upload(raw, mime_type)
        b64 = base64.b64encode(prep_bytes).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{pm};base64,{b64}"}})

    ar = aspect_ratio if aspect_ratio in ("1:1", "16:9", "9:16", "4:3", "3:4", "4:5", "5:4", "3:2", "2:3", "21:9") else "1:1"
    # В проде нам нужен image-only output, чтобы модель не уходила в текстовый ответ.
    response_modalities_env = (os.getenv("ZVENO_IMAGE_RESPONSE_MODALITIES") or "image,text").strip().lower()
    if response_modalities_env in ("image,text", "text,image"):
        response_modalities = ["image", "text"]
    elif response_modalities_env == "text":
        response_modalities = ["text"]
    else:
        response_modalities = ["image"]

    image_config: dict = {"aspect_ratio": ar}
    if image_size:
        # Нативный размер модели (напр. "4K"). Финал приходит последним кадром в images.
        image_config["image_size"] = image_size
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": parts}],
        "modalities": response_modalities,
        "max_completion_tokens": max(2048, _env_int("ZVENO_IMAGE_MAX_COMPLETION_TOKENS", 8192)),
        "image_config": image_config,
    }
    provider_preferences = _build_zveno_image_provider_preferences()
    if provider_preferences:
        payload["provider"] = provider_preferences

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    retries = max(0, _env_int("ZVENO_IMAGE_RETRIES", 0))
    empty_image_retries = max(0, _env_int("ZVENO_IMAGE_EMPTY_RETRIES", 0))
    location_retries = max(0, _env_int("ZVENO_IMAGE_LOCATION_RETRIES", 0))
    retry_delay = max(0.2, _env_float("ZVENO_IMAGE_RETRY_DELAY_SECONDS", 1.2))
    attempt = 0
    data: dict | None = None
    while True:
        attempt += 1
        try:
            async with session.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout_sec),
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    if resp.status in (429, 500, 502, 503, 504) and attempt <= retries + 1:
                        logger.warning("[ZVENO] HTTP %s, paid retry %s/%s (model=%s)", resp.status, attempt, retries + 1, model)
                        await asyncio.sleep(retry_delay * attempt)
                        continue
                    err_text = f"Zveno image API HTTP {resp.status}: {body[:1200]}"
                    if is_zveno_insufficient_credits_error(err_text):
                        raise Exception(f"zveno_insufficient_credits: {err_text}")
                    raise Exception(err_text)
                try:
                    data = json.loads(body)
                except Exception as exc:
                    raise Exception(f"Zveno image API: invalid JSON ({exc}): {body[:600]}") from exc
                # Иногда провайдер отдаёт 200 OK, но без image-блока.
                # Делаем ограниченный повтор без изменения входного промпта/фото.
                ref = _zveno_extract_image_ref(data)
                if not ref:
                    if _is_safety_no_image_response(data):
                        break
                    if _is_location_unsupported_response(data):
                        if attempt <= location_retries:
                            logger.warning(
                                "[ZVENO] location unsupported, paid retry %s/%s same model=%s provider=%s summary=%s",
                                attempt,
                                location_retries,
                                model,
                                json.dumps(provider_preferences or {}, ensure_ascii=False),
                                _zveno_no_image_summary(data),
                            )
                            await asyncio.sleep(retry_delay * attempt)
                            continue
                        break
                    if attempt <= empty_image_retries + 1:
                        logger.warning(
                            "[ZVENO] empty image in response, paid retry %s/%s model=%s summary=%s",
                            attempt,
                            empty_image_retries + 1,
                            model,
                            _zveno_no_image_summary(data),
                        )
                        await asyncio.sleep(retry_delay * attempt)
                        continue
            break
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt <= retries + 1:
                logger.warning("[ZVENO] transient error, paid retry %s/%s: %s", attempt, retries + 1, exc)
                await asyncio.sleep(retry_delay * attempt)
                continue
            raise
    if data is None:
        raise Exception("Zveno image API: empty response payload")

    ref = _zveno_extract_image_ref(data)
    if not ref:
        summary = _zveno_no_image_summary(data)
        if _is_location_unsupported_response(data):
            raise Exception(f"zveno_location_unsupported:{summary}")
        native_reason = _zveno_extract_native_finish_reason(data)
        if _is_image_safety_reason(native_reason) or _is_safety_no_image_response(data):
            raise Exception(f"zveno_image_safety_blocked:{summary}")
        raise Exception(f"zveno_empty_image:{summary}")
    img_bytes = await _zveno_fetch_image_bytes(session, ref)
    if not img_bytes:
        raise Exception("Zveno image API: empty image payload")
    return img_bytes, data


async def upscale_image_vertex(session: aiohttp.ClientSession, api_key: str, image_bytes: bytes,
                               prompt: str = None, aspect_ratio: str = "1:1") -> bytes | None:
    """Усиление детализации / 4K через второй запрос к Zveno image."""
    if not _is_enabled("ZVENO_IMAGE_UPSCALE", "1"):
        return None
    logger.info("[ZVENO Upscale] Второй проход по изображению...")
    try:
        up_model = _resolve_zveno_image_model(
            (
                os.getenv("ZVENO_UPSCALE_MODEL")
                or os.getenv("ZVENO_IMAGE_MODEL_CHEAP")
                or "google/gemini-3.1-flash-image-preview"
            )
        )
        if prompt and ("model" in prompt.lower() or "person" in prompt.lower() or "face" in prompt.lower()):
            txt = (
                "Upscale and refine this image to maximum commercial quality and resolution. "
                "Preserve faces, pose, clothing, and background exactly — no new objects, no redesign. "
                "Output one image only."
            )
        else:
            txt = (
                "Upscale this product image to maximum commercial sharpness and resolution. "
                "Preserve the product and composition exactly — no new elements, no people if none were present. "
                "Output one image only."
            )
        up_started = time.time()
        ar = aspect_ratio if aspect_ratio in ("1:1", "16:9", "9:16", "4:3", "3:4", "4:5", "5:4", "3:2", "2:3", "21:9") else "1:1"
        out, up_data = await _zveno_image_chat_request(
            session, api_key, up_model, txt, [image_bytes], "image/jpeg", ar,
        )
        up_usage = up_data.get("usage") if isinstance(up_data, dict) else None
        up_cost = _provider_rub_from_zveno_payload(up_usage, up_data)
        logger.info(
            "[ZVENO Upscale] Готово за %.1fs, model=%s, cost_rub=%s, tokens=%s",
            time.time() - up_started, up_model,
            f"{up_cost:.2f}" if up_cost is not None else "flat",
            up_usage.get("total_tokens") if up_usage else "?",
        )
        try:
            await record_zveno_usage(
                api_key=api_key,
                model=up_model,
                usage=up_usage,
                latency_ms=int((time.time() - up_started) * 1000),
                status="ok",
                full_response=up_data if isinstance(up_data, dict) else None,
            )
        except Exception:
            pass
        return out
    except Exception as exc:
        logger.error("[ZVENO Upscale] Ошибка: %s", exc)
        return None


def _ensure_4k_resolution(image_bytes: bytes) -> bytes:
    """
    Гарантирует повышенное разрешение для режима 4K.
    Если длинная сторона меньше 3840, делаем high-quality upscale (Lanczos).
    """
    target_long_side = max(2560, _env_int("ZVENO_4K_LONG_SIDE", 3840))
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        long_side = max(w, h)
        if long_side >= target_long_side:
            return image_bytes

        scale = target_long_side / float(long_side)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        up = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        up.save(buf, format="JPEG", quality=96, optimize=True)
        return buf.getvalue()
    except Exception as exc:
        logger.warning("[ZVENO 4K] local upscale fallback failed: %s", exc)
        return image_bytes


def _local_upscale_4k(image_bytes: bytes) -> bytes:
    """
    Локальный высококачественный upscale до 4K без API-вызовов.
    Использует OpenCV (из rembg) + Pillow для многопроходного улучшения:
      1. Двухступенчатый Lanczos resize (для лучшей интерполяции)
      2. Bilateral filter для деноиза без размытия границ
      3. Unsharp mask для резкости деталей
      4. CLAHE для локального контраста (текстуры ткани)
    """
    target_long_side = max(2560, _env_int("ZVENO_4K_LONG_SIDE", 3840))
    jpeg_quality = _env_int("ZVENO_4K_JPEG_QUALITY", 96)

    try:
        import cv2
        import numpy as np
    except ImportError:
        logger.warning("[LOCAL_UPSCALE] opencv/numpy not available, falling back to Pillow Lanczos")
        return _ensure_4k_resolution(image_bytes)

    try:
        # Декодируем изображение
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            logger.warning("[LOCAL_UPSCALE] cv2.imdecode вернул None")
            return _ensure_4k_resolution(image_bytes)

        h, w = img.shape[:2]
        long_side = max(w, h)
        if long_side >= target_long_side:
            logger.info("[LOCAL_UPSCALE] Изображение уже %dx%d, upscale не нужен", w, h)
            return image_bytes

        scale = target_long_side / float(long_side)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))

        logger.info("[LOCAL_UPSCALE] %dx%d → %dx%d (scale %.2fx)", w, h, new_w, new_h, scale)

        # --- Шаг 1: Двухступенчатый resize для лучшего качества ---
        if scale > 2.5:
            mid_w = int(round(w * (scale ** 0.5)))
            mid_h = int(round(h * (scale ** 0.5)))
            img = cv2.resize(img, (mid_w, mid_h), interpolation=cv2.INTER_LANCZOS4)
            logger.info("[LOCAL_UPSCALE] Промежуточный шаг: %dx%d", mid_w, mid_h)

        upscaled = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)

        # --- Шаг 2: Bilateral filter — убираем шум, сохраняем границы ---
        upscaled = cv2.bilateralFilter(upscaled, d=5, sigmaColor=40, sigmaSpace=40)

        # --- Шаг 3: Unsharp mask — восстанавливаем резкость ---
        gaussian = cv2.GaussianBlur(upscaled, (0, 0), sigmaX=2.0)
        sharpness_amount = 1.5
        upscaled = cv2.addWeighted(upscaled, 1.0 + sharpness_amount, gaussian, -sharpness_amount, 0)
        upscaled = np.clip(upscaled, 0, 255).astype(np.uint8)

        # --- Шаг 4: CLAHE на L-канале (локальный контраст для текстур) ---
        lab = cv2.cvtColor(upscaled, cv2.COLOR_BGR2LAB)
        l_channel, a_channel, b_channel = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
        l_channel = clahe.apply(l_channel)
        lab = cv2.merge([l_channel, a_channel, b_channel])
        upscaled = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

        # Кодируем результат
        # PNG, а не JPEG: апскейл и без того интерполирует, добавлять поверх
        # JPEG-артефакты нельзя. Признак ошибки — файл ~1.8–2.5 МБ вместо 10–20 МБ.
        # ZVENO_4K_JPEG_QUALITY оставлен для отката на JPEG через ZVENO_4K_FORMAT=jpeg.
        if (os.getenv("ZVENO_4K_FORMAT") or "png").lower() == "jpeg":
            encode_params = [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality, cv2.IMWRITE_JPEG_OPTIMIZE, 1]
            success, buf = cv2.imencode(".jpg", upscaled, encode_params)
        else:
            success, buf = cv2.imencode(".png", upscaled, [cv2.IMWRITE_PNG_COMPRESSION, 6])
        if not success:
            logger.warning("[LOCAL_UPSCALE] cv2.imencode не удалось")
            return _ensure_4k_resolution(image_bytes)

        result = buf.tobytes()
        logger.info(
            "[LOCAL_UPSCALE] Готово: %dx%d, размер %.1f KB → %.1f KB",
            new_w, new_h,
            len(image_bytes) / 1024, len(result) / 1024,
        )
        return result

    except Exception as exc:
        logger.error("[LOCAL_UPSCALE] Ошибка: %s", exc, exc_info=True)
        return _ensure_4k_resolution(image_bytes)


async def generate_image(
        api_key: str, prompt: str, images_bytes: Optional[list[bytes]] = None,
        aspect_ratio: str = "1:1", quality: str = "HD", key_id: int = 0,
        db_instance=None, generation_meta: dict = None, mime_type: str = "image/jpeg",
        model_name: str | None = None, ref_image_bytes: bytes | None = None,
) -> Optional[str]:
    """
    Генерация изображения через ZvenoAI (OpenAI-совместимый chat/completions + modalities image).
    См. https://zveno.ai/docs/api-reference/chat-completions
    """
    z_key = await acquire_zveno_key_slot()
    if not z_key:
        raise Exception("zveno_keys_busy")

    images = images_bytes if isinstance(images_bytes, list) else [images_bytes] if images_bytes else []
    images = [img for img in images if img]
    if ref_image_bytes:
        images.append(ref_image_bytes)

    asp = str(aspect_ratio).lower().replace("x", ":").strip()
    if asp == "4:5":
        asp = "3:4"
    elif asp == "5:4":
        asp = "4:3"
    elif asp not in ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9"]:
        asp = "1:1"

    # 4K → pro-модель (качественная), 1K → flash-модель (дешёвая/быстрая).
    image_model = _pick_zveno_image_model(model_name, generation_meta, quality=str(quality))
    zveno_user_id: int | None = None
    if isinstance(generation_meta, dict) and generation_meta.get("user_id") is not None:
        try:
            zveno_user_id = int(generation_meta.get("user_id"))
        except Exception:
            zveno_user_id = None

    async with aiohttp.ClientSession() as session:
        try:
            t0 = time.time()
            category_for_zveno = ""
            if isinstance(generation_meta, dict):
                category_for_zveno = str(generation_meta.get("category") or "")
            prompt, zveno_used = await enrich_prompt_with_zveno(
                session=session,
                base_prompt=prompt,
                images_bytes=images,
                aspect_ratio=asp,
                category=category_for_zveno,
            )
            if generation_meta is not None:
                generation_meta["zveno_prompt_enhanced"] = zveno_used
                generation_meta["zveno_key_preview"] = _mask_key_preview(z_key)
                generation_meta["kie_key_preview"] = generation_meta["zveno_key_preview"]
            if _is_enabled("LOG_PROMPT_PREVIEW", "0"):
                logger.info("[PROMPT] len=%s sha1=%s", len(prompt), hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12])
                logger.info("[PROMPT] preview=%s", prompt[:700].replace("\n", " "))

            # Не добавляем суффиксы — используем только промпт из админки.
            full_user_text = prompt.strip()
            logger.info("[ZVENO] Генерация изображения model=%s images=%s", image_model, len(images))

            img_labels = None
            if category_for_zveno == "collage" and len(images) >= 3:
                img_labels = [
                    "[Photo 1 — Reference model photo]",
                    "[Photo 2 — Product front view photo]",
                    "[Photo 3 — Product back view photo]",
                ]

            gen_started = time.time()
            try:
                final_image_bytes, z_data = await _zveno_image_chat_request(
                    session, z_key, image_model, full_user_text, images, mime_type, asp,
                    image_labels=img_labels,
                    image_size=("4K" if str(quality).lower() == "4k" else None),
                )
            except Exception as first_exc:
                first_err = str(first_exc or "")
                err_l = first_err.lower()
                is_safety = "zveno_image_safety_blocked" in err_l
                is_location = "zveno_location_unsupported" in err_l or "user location is not supported" in err_l
                is_empty_image = "zveno_empty_image" in err_l or "no image in response" in err_l or "empty image payload" in err_l
                if not (is_safety or is_empty_image or is_location):
                    raise
                reason = "LOCATION_UNSUPPORTED" if is_location else ("EMPTY_IMAGE" if is_empty_image else "IMAGE_SAFETY")
                logger.warning(
                    "[ZVENO] %s on %s, fallback disabled, ask user to retry: %s",
                    reason,
                    image_model,
                    first_err[:600],
                )
                if is_location:
                    raise Exception("image_generation_provider_location_unsupported") from first_exc
                if is_safety:
                    raise Exception("image_generation_safety_blocked") from first_exc
                raise Exception("image_generation_empty_image") from first_exc
            gen_usage = z_data.get("usage") if isinstance(z_data, dict) else None
            gen_cost = _provider_rub_from_zveno_payload(gen_usage, z_data)
            logger.info(
                "[ZVENO] Генерация: model=%s, cost_rub=%s, tokens=%s, time=%.1fs",
                image_model,
                f"{gen_cost:.2f}" if gen_cost is not None else "flat",
                gen_usage.get("total_tokens") if gen_usage else "?",
                time.time() - gen_started,
            )
            await record_zveno_usage(
                api_key=z_key,
                model=image_model,
                usage=gen_usage,
                latency_ms=int((time.time() - gen_started) * 1000),
                status="ok",
                full_response=z_data if isinstance(z_data, dict) else None,
                user_id=zveno_user_id,
            )

            # 4K включается только если UI/хендлеры передали quality=="4k".
            # Глобальный запрет (app_settings.disable_4k) форсит 1k ещё до вызова generate_image.
            is_4k = str(quality).lower() == "4k"
            if is_4k:
                logger.info("[ZVENO] 4K: локальный upscale до %spx по длинной стороне...",
                            _env_int("ZVENO_4K_LONG_SIDE", 3840))
                final_image_bytes = _local_upscale_4k(final_image_bytes)

            if generation_meta is not None:
                generation_meta["model_used"] = image_model
                generation_meta["upscaled"] = is_4k

            os.makedirs(os.path.join(BASE_DIR, "data", "history"), exist_ok=True)
            # 4K-апскейл отдаёт PNG — расширение определяем по сигнатуре файла
            _ext = "png" if final_image_bytes[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
            file_name = f"result_{uuid.uuid4().hex}.{_ext}"
            file_path = os.path.join(BASE_DIR, "data", "history", file_name)
            with open(file_path, "wb") as f:
                f.write(final_image_bytes)

            logger.info("[ZVENO] Генерация завершена за %.1fs → %s", time.time() - t0, file_path)
            return file_path

        except Exception as e:
            logger.error("[ZVENO] Критическая ошибка: %s", e)
            try:
                await record_zveno_usage(
                    api_key=z_key,
                    model=image_model,
                    usage=None,
                    latency_ms=0,
                    status="exception",
                    error_text=str(e)[:500],
                    user_id=zveno_user_id,
                )
            except Exception:
                pass
            raise e
        finally:
            await release_zveno_key_slot(z_key)


def is_fatal_key_error(exc: BaseException) -> bool:
    """Ошибки ключа/доступа — ключ в админке можно деактивировать."""
    sl = str(exc).lower()
    for needle in (
        "401", "403", "invalid api", "incorrect api", "unauthorized", "forbidden",
        "api key", "authentication", "неверный ключ", "недействителен",
    ):
        if needle in sl:
            return True
    return False


def is_proxy_error(exc: BaseException) -> bool:
    """Сетевые/временные сбои (для логирования api_key_errors), не считаем фатальными для ключа."""
    if is_fatal_key_error(exc):
        return False
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, aiohttp.ServerTimeoutError)):
        return True
    if isinstance(exc, aiohttp.ClientError):
        return True
    sl = str(exc).lower()
    for needle in (
        "503", "502", "504", "timeout", "timed out", "connection", "network", "proxy",
        "temporarily unavailable", "eof", "reset", "broken pipe",
    ):
        if needle in sl:
            return True
    return False


def is_zveno_insufficient_credits_error(exc: BaseException | str) -> bool:
    sl = str(exc).lower()
    return (
        "insufficient credits" in sl
        or "openrouter.ai/settings/credits" in sl
        or '"code":402' in sl
        or "code 402" in sl
    )


async def generate_content_with_image(
    prompt: str,
    image_bytes: bytes | None = None,
) -> tuple[str, bytes | None]:
    """
    Утилита/тест: одна картинка по промпту (опционально с входным изображением) через Zveno image API.
    """
    z_key = await acquire_zveno_key_slot()
    if not z_key:
        raise RuntimeError("zveno_keys_busy")
    model = _resolve_zveno_image_model(None)
    images = [image_bytes] if image_bytes else []
    try:
        async with aiohttp.ClientSession() as session:
            img_out, data = await _zveno_image_chat_request(
                session,
                z_key,
                model,
                (prompt or "").strip(),
                images,
                "image/jpeg",
                "1:1",
            )
            choices = data.get("choices") or []
            msg = (choices[0] or {}).get("message") or {} if choices else {}
            txt = _extract_chat_content_text(msg.get("content"))
            return txt, img_out
    finally:
        await release_zveno_key_slot(z_key)


def generate_clothing_mask_sync(image_bytes: bytes) -> bytes:
    """
    Синхронная функция для генерации пустой маски.
    Используется в качестве заглушки, если другие части кода требуют этот метод.
    """
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(image_bytes))
        # Создаем полностью белую маску
        mask = Image.new("L", img.size, 255)
        buf = io.BytesIO()
        mask.save(buf, "JPEG")
        return buf.getvalue()
    except Exception as e:
        logger.error(f"Error in sync mask: {e}")
        return image_bytes


async def get_task_info(task_id: str) -> dict:
    """Заглушка: асинхронных taskId у Zveno нет, генерация — один запрос chat/completions."""
    return {"taskId": task_id, "status": "n/a", "message": "Zveno: нет polling по taskId."}
