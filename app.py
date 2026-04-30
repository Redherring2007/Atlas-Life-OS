from __future__ import annotations

import asyncio
import logging

import uvicorn

from bot import build_application
from config import config
from mini_app import app as web_app


logger = logging.getLogger("atlas_life_os")


async def main() -> None:
    telegram_app = build_application()
    await telegram_app.initialize()
    await telegram_app.start()
    if telegram_app.updater:
        await telegram_app.updater.start_polling(allowed_updates=None)
    logger.info("Telegram bot started")

    server = uvicorn.Server(
        uvicorn.Config(
            web_app,
            host="0.0.0.0",
            port=config.port,
            log_level="info",
        )
    )

    try:
        await server.serve()
    finally:
        if telegram_app.updater:
            await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
