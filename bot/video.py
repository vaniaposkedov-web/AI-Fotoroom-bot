import asyncio
import base64
import io
import logging
import os
import time
import uuid
from typing import Optional

import aiohttp

from bot.gemini import (
    acquire_zveno_key_slot,
    release_zveno_key_slot,
    get_zveno_api_keys_cached,
    record_zveno_usage,
)

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ZVENO_VIDEO_MODEL = os.getenv("ZVENO_VIDEO_MODEL", "kwaivgI/kling-2.1-master").strip()
ZVENO_VIDEO_POLL_INTERVAL = int(os.getenv("ZVENO_VIDEO_POLL_INTERVAL", "7"))
ZVENO_VIDEO_TIMEOUT = int(os.getenv("ZVENO_VIDEO_TIMEOUT", "300"))


def _video_base_url() -> str:
    return (os.getenv("ZVENO_BASE_URL") or "https://api.zveno.ai/v1").rstrip("/")


def _resolution_for_quality(quality: str) -> str:
    """1K → 720p, 4K → 1080p."""
    if quality and quality.lower() in ("4k", "4К"):
        return "1080p"
    return "720p"


async def generate_video(
    *,
    image_bytes: bytes,
    prompt: str,
    duration: int = 5,
    quality: str = "1k",
    model: str | None = None,
    aspect_ratio: str = "16:9",
    db_instance=None,
    user_id: int | None = None,
) -> Optional[str]:
    """
    Generate video from image via Zveno Video API.
    Returns path to saved MP4 or None on failure.
    """
    api_key = await acquire_zveno_key_slot()
    if not api_key:
        keys = await get_zveno_api_keys_cached()
        if keys:
            api_key = keys[0]
        else:
            logger.error("[VIDEO] No Zveno API keys available")
            return None

    base = _video_base_url()
    chosen_model = (model or ZVENO_VIDEO_MODEL).strip()
    resolution = _resolution_for_quality(quality)

    img_b64 = base64.b64encode(image_bytes).decode("utf-8")
    img_data_url = f"data:image/jpeg;base64,{img_b64}"

    payload = {
        "model": chosen_model,
        "prompt": prompt,
        "duration": duration,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "frame_images": [
            {
                "type": "image_url",
                "image_url": {"url": img_data_url},
                "frame_type": "first_frame",
            }
        ],
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    t0 = time.time()
    job_id = None

    try:
        async with aiohttp.ClientSession() as session:
            # 1. Submit
            async with session.post(
                f"{base}/videos",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status not in (200, 201, 202):
                    body = await resp.text()
                    logger.error("[VIDEO] Submit failed %s: %s", resp.status, body[:500])
                    await release_zveno_key_slot(api_key)
                    return None
                job_data = await resp.json()

            job_id = job_data.get("id")
            polling_url = job_data.get("polling_url") or f"{base}/videos/{job_id}"
            status = job_data.get("status", "pending")
            logger.info("[VIDEO] Job submitted: id=%s status=%s model=%s", job_id, status, chosen_model)

            # 2. Poll
            deadline = time.time() + ZVENO_VIDEO_TIMEOUT
            while status in ("pending", "in_progress"):
                if time.time() > deadline:
                    logger.error("[VIDEO] Timeout waiting for job %s", job_id)
                    await release_zveno_key_slot(api_key)
                    return None
                await asyncio.sleep(ZVENO_VIDEO_POLL_INTERVAL)
                async with session.get(
                    polling_url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as poll_resp:
                    if poll_resp.status != 200:
                        logger.warning("[VIDEO] Poll status=%s for job %s", poll_resp.status, job_id)
                        continue
                    job_data = await poll_resp.json()
                    status = job_data.get("status", status)
                    logger.debug("[VIDEO] Poll job=%s status=%s", job_id, status)

            if status != "completed":
                err = job_data.get("error", "unknown")
                logger.error("[VIDEO] Job %s failed: %s", job_id, err)
                latency_ms = int((time.time() - t0) * 1000)
                await record_zveno_usage(
                    api_key=api_key, model=chosen_model,
                    usage=job_data.get("usage"), latency_ms=latency_ms,
                    status="error", error_text=str(err)[:500],
                    user_id=user_id,
                )
                await release_zveno_key_slot(api_key)
                return None

            # 3. Download
            unsigned_urls = job_data.get("unsigned_urls", [])
            if not unsigned_urls:
                content_url = f"{base}/videos/{job_id}/content"
            else:
                content_url = unsigned_urls[0]

            output_dir = os.path.join(BASE_DIR, "data", "history")
            os.makedirs(output_dir, exist_ok=True)
            out_path = os.path.join(output_dir, f"video_{uuid.uuid4().hex[:12]}.mp4")

            async with session.get(
                content_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as dl_resp:
                if dl_resp.status not in (200, 206):
                    logger.error("[VIDEO] Download failed %s for job %s", dl_resp.status, job_id)
                    await release_zveno_key_slot(api_key)
                    return None
                with open(out_path, "wb") as f:
                    async for chunk in dl_resp.content.iter_chunked(1 << 20):
                        f.write(chunk)

            latency_ms = int((time.time() - t0) * 1000)
            usage = job_data.get("usage")
            cost = None
            if isinstance(usage, dict):
                cost = usage.get("cost")

            await record_zveno_usage(
                api_key=api_key, model=chosen_model,
                usage=usage, latency_ms=latency_ms,
                status="ok", user_id=user_id,
            )

            logger.info(
                "[VIDEO] Job %s completed. cost=%s latency=%dms path=%s",
                job_id, cost, latency_ms, out_path,
            )
            await release_zveno_key_slot(api_key)
            return out_path

    except Exception as exc:
        latency_ms = int((time.time() - t0) * 1000)
        logger.exception("[VIDEO] Exception during video generation job=%s", job_id)
        try:
            await record_zveno_usage(
                api_key=api_key, model=chosen_model,
                usage=None, latency_ms=latency_ms,
                status="error", error_text=str(exc)[:500],
                user_id=user_id,
            )
        except Exception:
            pass
        await release_zveno_key_slot(api_key)
        return None
