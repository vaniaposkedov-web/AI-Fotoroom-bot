import asyncio
import time
import json
import os
import aiohttp
from google.oauth2 import service_account, credentials as user_credentials
import google.auth.transport.requests

MODEL_NAME = "gemini-3-pro-image-preview"
REGION = "us-central1"  # Исправлено: картинки не работают в global

REQUESTS_PER_KEY = 10
DELAY_BETWEEN_REQUESTS = 6.0  # Равномерно: 1 запрос в 6 секунд

stats = {"200_OK": 0, "429_LIMIT": 0, "403_404_ERRORS": 0, "OTHER_ERRORS": 0}


def load_keys():
    keys = []
    keys_dir = "/app/data/keys"
    if not os.path.exists(keys_dir):
        return keys

    for filename in os.listdir(keys_dir):
        if filename.endswith(".json"):
            filepath = os.path.join(keys_dir, filename)
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                project_id = data.get("project_id") or data.get("quota_project_id")
                if data.get("type") == "service_account":
                    creds = service_account.Credentials.from_service_account_file(filepath, scopes=[
                        "https://www.googleapis.com/auth/cloud-platform"])
                else:
                    creds = user_credentials.Credentials.from_authorized_user_file(filepath, scopes=[
                        "https://www.googleapis.com/auth/cloud-platform"])
                keys.append({"project_id": project_id, "creds": creds})
    return keys


def get_token(creds):
    request = google.auth.transport.requests.Request()
    creds.refresh(request)
    return creds.token


async def fire_request(session, key_data, task_id):
    project_id = key_data["project_id"]
    try:
        token = await asyncio.to_thread(get_token, key_data["creds"])
    except Exception as e:
        print(f"[{task_id}] Ошибка токена {project_id}: {e}")
        stats["OTHER_ERRORS"] += 1
        return

    url = f"https://{REGION}-aiplatform.googleapis.com/v1/projects/{project_id}/locations/{REGION}/publishers/google/models/{MODEL_NAME}:generateContent"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    payload = {
        "contents": [{"role": "user", "parts": [{"text": "A simple red apple on a white table, 1k resolution"}]}],
        "generationConfig": {"responseModalities": ["IMAGE"], "temperature": 0.1}
    }

    start_time = time.time()
    try:
        async with session.post(url, headers=headers, json=payload, timeout=60) as response:
            elapsed = time.time() - start_time
            if response.status == 200:
                print(f"[{task_id}] ✅ УСПЕХ ({project_id}) - {elapsed:.1f} сек")
                stats["200_OK"] += 1
            elif response.status == 429:
                print(f"[{task_id}] ⚠️ ЛИМИТ 429 ({project_id}) - {elapsed:.1f} сек")
                stats["429_LIMIT"] += 1
            elif response.status in (403, 404):
                print(f"[{task_id}] ❌ БЛОК {response.status} ({project_id})")
                stats["403_404_ERRORS"] += 1
            else:
                print(f"[{task_id}] ❓ ОШИБКА {response.status} ({project_id})")
                stats["OTHER_ERRORS"] += 1
    except asyncio.TimeoutError:
        print(f"[{task_id}] ⏱ ТАЙМАУТ ({project_id})")
        stats["OTHER_ERRORS"] += 1
    except Exception as e:
        print(f"[{task_id}] 💥 СБОЙ ({project_id}): {e}")
        stats["OTHER_ERRORS"] += 1


async def worker(session, key_data, key_index):
    project_id = key_data["project_id"]
    print(f"▶️ [{project_id}] Старт: {REQUESTS_PER_KEY} запросов с паузой {DELAY_BETWEEN_REQUESTS} сек.")

    for i in range(1, REQUESTS_PER_KEY + 1):
        task_id = f"Key{key_index}-Req{i}"
        await fire_request(session, key_data, task_id)
        if i < REQUESTS_PER_KEY:
            await asyncio.sleep(DELAY_BETWEEN_REQUESTS)

    print(f"⏹ [{project_id}] Завершил работу.")


async def main():
    print("=" * 60)
    print("РАВНОМЕРНЫЙ НАГРУЗОЧНЫЙ ТЕСТ VERTEX AI")
    print("=" * 60)

    keys = load_keys()
    if not keys:
        print("Ключи не найдены в /app/data/keys!")
        return

    print(f"Найдено ключей: {len(keys)}")
    print(f"План: {REQUESTS_PER_KEY} запросов на ключ, 1 запрос каждые {DELAY_BETWEEN_REQUESTS} сек.")
    print(f"Теоретический максимум пула: ~{len(keys) * int(60 / DELAY_BETWEEN_REQUESTS)} фото/мин\n")

    start_test = time.time()
    async with aiohttp.ClientSession() as session:
        tasks = [worker(session, key, idx + 1) for idx, key in enumerate(keys)]
        await asyncio.gather(*tasks)

    total_time = time.time() - start_test

    print("\n" + "=" * 60)
    print("ИТОГИ ТЕСТА")
    print("=" * 60)
    print(f"Общее время: {total_time:.1f} сек")
    print(f"Успешных (200): {stats['200_OK']}")
    print(f"Упоров в лимит (429): {stats['429_LIMIT']}")
    print(f"Ошибок доступа (403/404): {stats['403_404_ERRORS']}")
    print(f"Других сбоев: {stats['OTHER_ERRORS']}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())