import os
from dataclasses import dataclass
from typing import List

from dotenv import load_dotenv


@dataclass
class Settings:
    bot_token: str
    old_bot_token: str | None
    vk_bot_token: str | None
    database_url: str
    admin_ids: list[int]
    gcp_project_id: str
    gcp_region: str
    google_application_credentials: str | None


def load_settings(require_bot_token: bool = True) -> Settings:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    old_bot_token = os.getenv("OLD_BOT_TOKEN", "").strip() or None
    vk_bot_token = os.getenv("VK_BOT_TOKEN", "").strip() or None
    database_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/bot.db").strip()
    gcp_project_id = os.getenv("GCP_PROJECT_ID", "project-f6da0b43-8d6e-48cf-984").strip()
    gcp_region = os.getenv("GCP_REGION", "us-central1").strip()
    google_application_credentials = (
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip() or None
    )

    admin_ids_env = os.getenv("ADMIN_IDS", "").strip()
    admin_ids: List[int] = []
    if admin_ids_env:
        parts = [part.strip() for part in admin_ids_env.replace(";", ",").split(",")]
        for part in parts:
            if not part:
                continue
            try:
                admin_ids.append(int(part))
            except ValueError:
                continue

    if require_bot_token and not bot_token:
        raise RuntimeError("BOT_TOKEN is required in .env")
    if not gcp_project_id:
        raise RuntimeError("GCP_PROJECT_ID is required in .env")
    if not gcp_region:
        raise RuntimeError("GCP_REGION is required in .env")

    return Settings(
        bot_token=bot_token,
        old_bot_token=old_bot_token,
        vk_bot_token=vk_bot_token,
        database_url=database_url,
        admin_ids=admin_ids,
        gcp_project_id=gcp_project_id,
        gcp_region=gcp_region,
        google_application_credentials=google_application_credentials,
    )
