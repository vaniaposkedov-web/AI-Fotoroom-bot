"""Загрузка бинарных данных фото из Telegram (file_id) или с диска (data/...)."""
from __future__ import annotations

import io
import logging
import os
from typing import Optional

from aiogram import Bot

logger = logging.getLogger(__name__)


async def telegram_file_id_to_bytes(bot: Bot, file_id: str, *, base_dir: str) -> Optional[bytes]:
    """Скачивает файл по Telegram file_id или читает локальный путь вида data/... относительно base_dir."""
    if not file_id:
        return None
    fid = str(file_id).strip()
    if fid.startswith("data/"):
        local_path = os.path.normpath(os.path.join(base_dir, fid.replace("/", os.sep)))
        if not local_path.startswith(os.path.normpath(base_dir)):
            logger.error("[telegram_images] path escape blocked: %s", fid)
            return None
        if os.path.isfile(local_path):
            with open(local_path, "rb") as f:
                return f.read()
        return None
    try:
        f_info = await bot.get_file(fid)
        buf = io.BytesIO()
        await bot.download_file(f_info.file_path, buf)
        return buf.getvalue()
    except Exception as exc:
        logger.error("[telegram_images] download failed file_id=%s err=%s", fid[:24], exc)
        return None


async def telegram_file_ids_to_bytes_list(bot: Bot, file_ids: list, *, base_dir: str) -> list[bytes]:
    """Параллельная загрузка; порядок соответствует непустым file_ids."""
    import asyncio

    ids = [fid for fid in file_ids if fid]
    if not ids:
        return []
    chunks = await asyncio.gather(*[telegram_file_id_to_bytes(bot, fid, base_dir=base_dir) for fid in ids])
    return [c for c in chunks if c]
