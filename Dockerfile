FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app


RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*


COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt


COPY bot ./bot
COPY vk_bot ./vk_bot
COPY scripts ./scripts

COPY ["WhatsApp Image 2025-11-25 at 00.40.21.jpeg", "./garment_length_guide.jpeg"]
RUN mkdir -p /app/data


ENV DATABASE_URL=sqlite+aiosqlite:///./data/bot.db


CMD ["python", "-m", "bot.main"]
