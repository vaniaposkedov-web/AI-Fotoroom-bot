"""Video Generation client."""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import time
from typing import Optional

import aiohttp
from PIL import Image

logger = logging.getLogger(__name__)


class VideoError(Exception):
    """Base video generation error with category for user-facing messages."""
    def __init__(self, message: str, category: str = "unknown"):
        super().__init__(message)
        self.category = category


class VideoRateLimitError(VideoError):
    def __init__(self, message: str = "rate_limited"):
        super().__init__(message, category="rate_limit")


class VideoSubmitError(VideoError):
    def __init__(self, message: str):
        super().__init__(message, category="submit_failed")


class VideoTimeoutError(VideoError):
    def __init__(self, message: str = "poll_timeout"):
        super().__init__(message, category="timeout")


class VideoGenerationFailed(VideoError):
    def __init__(self, message: str):
        cat = "unknown"
        msg_lower = message.lower()
        if "safety" in msg_lower or "blocked" in msg_lower or "content" in msg_lower:
            cat = "safety"
        elif "429" in message or "rate" in msg_lower:
            cat = "rate_limit"
        super().__init__(message, category=cat)


class VideoBillingError(VideoError):
    """Provider rejected the job for lack of funds (HTTP 402 / insufficient funds)."""
    def __init__(self, message: str):
        super().__init__(message, category="insufficient_funds")


def _is_retryable_video_failure(err_str: str) -> bool:
    """Whether a *failed* video job is worth resubmitting (transient upstream hiccup).

    Excludes permanent rejections (safety/content/invalid input) and billing,
    where a retry would only waste time and credits.
    """
    s = (err_str or "").lower()
    if any(t in s for t in (
        "safety", "blocked", "content policy", "nsfw",
        "invalid", "unsupported", "insufficient",
    )):
        return False
    return any(t in s for t in (
        "429", "rate", "http failed", "timeout", "timed out", "temporar",
        "internal", "500", "502", "503", "504", "unavailable", "try again", "failed",
    ))

ZVENO_BASE_URL = (os.getenv("ZVENO_BASE_URL") or "https://api.zveno.ai").rstrip("/")
# The video endpoints below always append "/v1/...". If ZVENO_BASE_URL already ends
# with "/v1" (as it does in some envs), strip it so we never build ".../v1/v1/videos" (404).
if ZVENO_BASE_URL.endswith("/v1"):
    ZVENO_BASE_URL = ZVENO_BASE_URL[:-3]
VIDEO_MODEL = os.getenv("ZVENO_VIDEO_MODEL", "kwaivgI/kling-2.1-standard")
VIDEO_RESOLUTION = os.getenv("ZVENO_VIDEO_RESOLUTION", "720p")
VIDEO_POLL_INTERVAL = float(os.getenv("ZVENO_VIDEO_POLL_INTERVAL", "8"))
VIDEO_POLL_TIMEOUT = float(os.getenv("ZVENO_VIDEO_POLL_TIMEOUT", "600"))
VIDEO_MAX_PROMPT_LEN = 2500


async def submit_video_job(
    api_key: str,
    prompt: str,
    duration: int = 5,
    frame_images: list[dict] | None = None,
    aspect_ratio: str = "9:16",
) -> dict:
    """POST /v1/videos — submit async video generation job.

    Returns raw JSON response with keys: id, polling_url, status.
    """
    endpoint = f"{ZVENO_BASE_URL}/v1/videos"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    payload: dict = {
        "model": VIDEO_MODEL,
        "prompt": prompt,
        "duration": duration,
        "resolution": VIDEO_RESOLUTION,
        "aspect_ratio": aspect_ratio,
    }
    if frame_images:
        payload["frame_images"] = frame_images

    logger.info("[VIDEO] submit model=%s duration=%ss resolution=%s aspect=%s frames=%s prompt_len=%d prompt_preview=%.100s",
                VIDEO_MODEL, duration, VIDEO_RESOLUTION, aspect_ratio,
                len(frame_images or []), len(prompt), repr(prompt[:100]))

    timeout = aiohttp.ClientTimeout(total=60)
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(endpoint, headers=headers, json=payload) as resp:
                body = await resp.json()
                if resp.status in (200, 201, 202):
                    logger.info("[VIDEO] job submitted: id=%s status=%s", body.get("id"), body.get("status"))
                    return body

                err = body.get("error", {})
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                logger.error("[VIDEO] submit failed HTTP %s (attempt %s/%s): %s",
                             resp.status, attempt, max_retries, msg)

                if resp.status == 429 and attempt < max_retries:
                    wait = 15 * attempt
                    logger.info("[VIDEO] rate limited, retrying in %ss...", wait)
                    await asyncio.sleep(wait)
                    continue

                if resp.status == 429:
                    raise VideoRateLimitError(msg)
                if resp.status == 402 or "insufficient funds" in msg.lower() or "insufficient credits" in msg.lower():
                    raise VideoBillingError(msg)
                raise VideoSubmitError(msg)


async def poll_video_job(api_key: str, job_id: str, on_progress=None) -> dict:
    """Poll GET /v1/videos/{id} until terminal status (completed / failed).

    on_progress(elapsed_sec, status) — optional async callback for progress updates.
    """
    endpoint = f"{ZVENO_BASE_URL}/v1/videos/{job_id}"
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = aiohttp.ClientTimeout(total=30)

    deadline = time.time() + VIDEO_POLL_TIMEOUT
    start_time = time.time()
    last_status = "pending"
    poll_count = 0

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while time.time() < deadline:
            try:
                async with session.get(endpoint, headers=headers) as resp:
                    body = await resp.json()
                    status = body.get("status", "unknown")
                    if status != last_status:
                        logger.info("[VIDEO] poll id=%s status=%s", job_id, status)
                        last_status = status
                    if status in ("completed", "failed"):
                        return body
            except Exception as exc:
                logger.warning("[VIDEO] poll error for %s: %s", job_id, exc)

            poll_count += 1
            elapsed = time.time() - start_time
            if on_progress and poll_count % 2 == 0:
                try:
                    await on_progress(elapsed, last_status)
                except Exception:
                    pass
            await asyncio.sleep(VIDEO_POLL_INTERVAL)

    raise VideoTimeoutError("video_poll_timeout")


async def download_video(api_key: str, job_id: str, save_path: str) -> str:
    """GET /v1/videos/{id}/content — download MP4 to save_path."""
    endpoint = f"{ZVENO_BASE_URL}/v1/videos/{job_id}/content"
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = aiohttp.ClientTimeout(total=120)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(endpoint, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"video_download_failed: HTTP {resp.status} {text[:300]}")
            with open(save_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1 << 20):
                    f.write(chunk)
    logger.info("[VIDEO] downloaded %s -> %s", job_id, save_path)
    return save_path


VIDEO_MIN_PX = 300
VIDEO_MAX_PX = 2048


def _resize_image_bytes(img_bytes: bytes) -> bytes:
    """Resize image so width and height are within [300, 2048] range. Returns JPEG bytes."""
    img = Image.open(io.BytesIO(img_bytes))
    w, h = img.size

    if VIDEO_MIN_PX <= w <= VIDEO_MAX_PX and VIDEO_MIN_PX <= h <= VIDEO_MAX_PX:
        if img.format == "JPEG":
            return img_bytes
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=92)
        return buf.getvalue()

    scale = 1.0
    if w > VIDEO_MAX_PX or h > VIDEO_MAX_PX:
        scale = min(VIDEO_MAX_PX / w, VIDEO_MAX_PX / h)
    if w * scale < VIDEO_MIN_PX or h * scale < VIDEO_MIN_PX:
        scale = max(VIDEO_MIN_PX / w, VIDEO_MIN_PX / h)

    new_w = max(VIDEO_MIN_PX, min(VIDEO_MAX_PX, int(w * scale)))
    new_h = max(VIDEO_MIN_PX, min(VIDEO_MAX_PX, int(h * scale)))

    img = img.convert("RGB").resize((new_w, new_h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    logger.info("[VIDEO] resized image %dx%d -> %dx%d", w, h, new_w, new_h)
    return buf.getvalue()


def _crop_to_aspect(img_bytes: bytes, aspect_ratio: str) -> bytes:
    """Center-crop image to the target W:H aspect.

    Kling image-to-video derives the OUTPUT aspect from the input frame and ignores
    the API ``aspect_ratio`` field, so to honor the user's chosen format (e.g. 9:16,
    4:5) we must conform the input frame itself.
    """
    try:
        tw, th = aspect_ratio.split(":")
        target = float(tw) / float(th)
    except Exception:
        return img_bytes
    if target <= 0:
        return img_bytes

    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    cur = w / h
    if abs(cur - target) < 0.01:
        return img_bytes  # already correct shape

    if cur > target:
        new_w = max(1, int(round(h * target)))
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = max(1, int(round(w / target)))
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    logger.info("[VIDEO] cropped input %dx%d -> %dx%d for aspect %s", w, h, img.size[0], img.size[1], aspect_ratio)
    return buf.getvalue()


def build_frame_images_from_bytes(
    images: list[bytes],
    labels: list[str] | None = None,
) -> list[dict]:
    """Build frame_images payload for Zveno Video API from raw image bytes.

    labels: list of frame_type values, e.g. ["first_frame", "last_frame"].
    If None, defaults to ["first_frame"] for 1 image or ["first_frame", "last_frame"] for 2.
    """
    if not labels:
        if len(images) == 1:
            labels = ["first_frame"]
        else:
            labels = ["first_frame", "last_frame"]

    result = []
    for i, img_bytes in enumerate(images):
        if i >= len(labels):
            break
        resized = _resize_image_bytes(img_bytes)
        b64 = base64.b64encode(resized).decode("ascii")
        result.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            "frame_type": labels[i],
        })
    return result


async def generate_video(
    api_key: str,
    prompt: str,
    duration: int = 5,
    image_bytes: bytes | None = None,
    aspect_ratio: str = "9:16",
    on_progress=None,
) -> tuple[str, dict]:
    """Full pipeline: submit → poll → download. Returns (video_path, job_data)."""
    frame_images = None
    if image_bytes:
        image_bytes = _crop_to_aspect(image_bytes, aspect_ratio)
        frame_images = build_frame_images_from_bytes([image_bytes], ["first_frame"])

    max_retries_on_429 = int(os.getenv("VIDEO_429_RETRIES", "2"))
    for attempt in range(1, max_retries_on_429 + 1):
        job = await submit_video_job(api_key, prompt, duration, frame_images, aspect_ratio)
        job_id = job.get("id")
        if not job_id:
            raise VideoSubmitError("video_submit_no_id")

        result = await poll_video_job(api_key, job_id, on_progress=on_progress)
        status = result.get("status")
        if status == "completed":
            break

        err = result.get("error", "unknown error")
        err_str = str(err)
        if attempt < max_retries_on_429 and _is_retryable_video_failure(err_str):
            wait = 10 * attempt
            logger.warning("[VIDEO] job %s failed (%.160s), retrying in %ss (attempt %s/%s)...",
                           job_id, err_str, wait, attempt, max_retries_on_429)
            await asyncio.sleep(wait)
            continue
        logger.error("[VIDEO] job %s failed permanently: %s | full=%r", job_id, err_str, result)
        raise VideoGenerationFailed(err_str)

    os.makedirs("/tmp/video_gen", exist_ok=True)
    save_path = f"/tmp/video_gen/{job_id}.mp4"
    await download_video(api_key, job_id, save_path)
    return save_path, result
