import asyncio
import logging
from pathlib import Path

from bot.gemini import generate_content_with_image


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

OUTPUT_PATH = Path(__file__).resolve().parent / "test_output.jpg"
TEST_PROMPT = "Реалистичный кот-астронавт на Марсе, 4k, кинематографичное освещение"


async def main() -> None:
    logger.info("Отправляю тестовый промпт в Vertex AI")
    generated_text, image_bytes = await generate_content_with_image(TEST_PROMPT)

    if image_bytes:
        OUTPUT_PATH.write_bytes(image_bytes)
        logger.info("Тест успешен. Изображение сохранено в %s", OUTPUT_PATH)
        if generated_text:
            logger.info("Текстовый ответ модели: %s", generated_text)
        return

    logger.error("Генерация не вернула изображение")


if __name__ == "__main__":
    asyncio.run(main())
