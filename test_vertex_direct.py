import os
import json
import time
import base64
import requests
from google.oauth2 import service_account
import google.auth.transport.requests

KEYS_DIR = "data/keys"

def get_test_image_b64():
    img_url = "https://telegram.org/img/t_logo.png"
    resp = requests.get(img_url, timeout=5)
    resp.raise_for_status()
    return base64.b64encode(resp.content).decode("utf-8"), "image/png"

def main():
    print("=== 🚀 СТРЕСС-ТЕСТ VERTEX AI (УМНЫЙ ПОИСК КЛЮЧА БЕЗ ПРОКСИ) ===\n")
    if not os.path.exists(KEYS_DIR):
        print(f"❌ Ошибка: Папка {KEYS_DIR} не найдена.")
        return

    key_files = [f for f in os.listdir(KEYS_DIR) if f.endswith(".json")]
    if not key_files:
        print(f"❌ Ошибка: Ключи не найдены.")
        return

    print("📥 1. Скачиваем тестовую картинку...")
    b64_img, mime_type = get_test_image_b64()
    prompt = "Опиши очень коротко, что изображено на этой картинке? Это тест."

    # ЖЕСТКАЯ БЛОКИРОВКА ПРОКСИ
    session = requests.Session()
    session.trust_env = False

    print("🔍 2. Начинаем перебор ключей...\n")
    
    for key_file in key_files:
        key_path = os.path.join(KEYS_DIR, key_file)
        print(f"▶️ Проверяем ключ: {key_file}")
        
        try:
            with open(key_path, "r", encoding="utf-8") as f:
                key_data = json.load(f)
                project_id = key_data.get("project_id") or key_data.get("quota_project_id")

            credentials = service_account.Credentials.from_service_account_file(
                key_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            request = google.auth.transport.requests.Request()
            credentials.refresh(request)
            token = credentials.token

            print(f"  ✅ Авторизация успешна! Отправляем боевой запрос в США напрямую...")

            region = "us-central1"
            model = "gemini-1.5-flash"
            url = f"https://{region}-aiplatform.googleapis.com/v1/projects/{project_id}/locations/{region}/publishers/google/models/{model}:generateContent"

            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }

            payload = {
                "contents": [{
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        {"inlineData": {"mimeType": mime_type, "data": b64_img}}
                    ]
                }]
            }

            start_time = time.time()
            response = session.post(url, headers=headers, json=payload, timeout=30)
            elapsed_time = time.time() - start_time

            if response.status_code == 200:
                text_result = response.json()["candidates"][0]["content"]["parts"][0]["text"]
                print(f"  🟢 [УСПЕХ] Код 200 OK")
                print(f"  ⏱ [СКОРОСТЬ] Время ответа Гугла: {elapsed_time:.2f} сек")
                print(f"  🤖 [ОТВЕТ]: {text_result.strip()}")
                break  # Тест прошел успешно, выходим из цикла!
                
            elif response.status_code == 429:
                print(f"  ⚠️ [ЛИМИТЫ] Код 429: Перегруз (Rate Limit). Ищем следующий...")
            else:
                print(f"  ❌ [ОШИБКА] Код {response.status_code}: {response.text[:100]}")

        except Exception as e:
            # Ошибка парсинга или авторизации (как было с VictoryTrujillo)
            print(f"  ⚠️ Ключ сломан или пуст: {str(e)[:100]}")
            print("  Переходим к следующему...\n")

if __name__ == "__main__":
    main()
