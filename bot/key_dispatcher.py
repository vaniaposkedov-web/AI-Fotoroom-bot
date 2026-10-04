from __future__ import annotations

import asyncio
from typing import Iterable


_KEY_LOCKS: dict[int, asyncio.Lock] = {}
_LOCKS_GUARD = asyncio.Lock()


async def _get_or_create_lock(key_id: int) -> asyncio.Lock:
    async with _LOCKS_GUARD:
        lock = _KEY_LOCKS.get(int(key_id))
        if lock is None:
            lock = asyncio.Lock()
            _KEY_LOCKS[int(key_id)] = lock
        return lock


async def try_acquire_key_lock(key_id: int) -> asyncio.Lock | None:
    lock = await _get_or_create_lock(int(key_id))
    if lock.locked():
        return None
    await lock.acquire()
    return lock


async def acquire_key_lock_with_wait(
    key_id: int,
    *,
    wait_seconds: float = 8.0,
    poll_interval_seconds: float = 0.2,
) -> asyncio.Lock | None:
    deadline = asyncio.get_running_loop().time() + max(0.0, float(wait_seconds))
    while True:
        lock = await try_acquire_key_lock(int(key_id))
        if lock is not None:
            return lock
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(max(0.05, float(poll_interval_seconds)))


def release_key_lock(lock: asyncio.Lock | None) -> None:
    if lock and lock.locked():
        lock.release()


def order_keys_after_last_used(active_keys: Iterable[tuple], last_used_key_id: int | None) -> list[tuple]:
    keys = list(active_keys or [])
    if not keys or last_used_key_id is None:
        return keys
    key_ids = [int(k[0]) for k in keys]
    if int(last_used_key_id) not in key_ids:
        return keys
    idx = key_ids.index(int(last_used_key_id))
    return keys[idx + 1 :] + keys[: idx + 1]
