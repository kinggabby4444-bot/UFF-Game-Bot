import os
import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web

from telegram import Update

import main as game_bot_module
import ads_main as ads_bot_module

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", 10000))

BASE_URL = os.environ.get("RENDER_EXTERNAL_URL")
if not BASE_URL:
    raise RuntimeError(
        "RENDER_EXTERNAL_URL is not set. This is auto-provided by Render "
        "on deploy - if running locally, set it manually to your public URL."
    )

GAME_WEBHOOK_PATH = "/webhook/game"
ADS_WEBHOOK_PATH = "/webhook/ads"


class BotRunner:
    """Runs a PTB Application in its own thread with its own event loop,
    so blocking (synchronous psycopg2) database calls inside its handlers
    only ever block THIS bot's thread - never the shared aiohttp server
    that answers Telegram's webhook requests and Render's health checks."""

    def __init__(self, name, build_app_fn, webhook_path, max_db_workers=8):
        self.name = name
        self.build_app_fn = build_app_fn
        self.webhook_path = webhook_path
        self.max_db_workers = max_db_workers
        self.loop = None
        self.app = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        # Cap concurrent asyncio.to_thread() workers to the DB pool size,
        # so under a burst of messages, extra DB calls queue and wait
        # for a free connection instead of all firing at once and
        # exhausting the pool (PoolError). Without this, to_thread's
        # default executor allows far more concurrent threads than the
        # pool has connections.
        executor = ThreadPoolExecutor(max_workers=self.max_db_workers)
        self.loop.set_default_executor(executor)
        try:
            self.app = self.build_app_fn()
            self.loop.run_until_complete(self._startup())
        except Exception:
            logger.exception(f"{self.name}: failed to start")
        finally:
            self.ready.set()
        self.loop.run_forever()

    async def _startup(self):
        await self.app.initialize()
        await self.app.bot.set_webhook(
            url=BASE_URL + self.webhook_path,
            allowed_updates=Update.ALL_TYPES,
        )
        await self.app.start()
        logger.info(f"{self.name}: initialized, webhook set, started.")

    def start(self):
        self.thread.start()
        self.ready.wait(timeout=60)

    def submit_update(self, data):
        if self.app is None or self.loop is None:
            logger.error(f"{self.name}: not ready, dropping update")
            return
        update = Update.de_json(data, self.app.bot)
        asyncio.run_coroutine_threadsafe(self.app.process_update(update), self.loop)

    def stop(self):
        if self.app is None or self.loop is None:
            return
        async def _shutdown():
            await self.app.stop()
            await self.app.shutdown()
        try:
            fut = asyncio.run_coroutine_threadsafe(_shutdown(), self.loop)
            fut.result(timeout=30)
        except Exception:
            logger.exception(f"{self.name}: error during shutdown")
        self.loop.call_soon_threadsafe(self.loop.stop)


GAME_DB_WORKERS = int(os.environ.get("GAME_DB_POOL_MAX", "10"))
ADS_DB_WORKERS = int(os.environ.get("ADS_DB_POOL_MAX", "5"))

game_runner = BotRunner("GAME BOT", game_bot_module.main, GAME_WEBHOOK_PATH, max_db_workers=GAME_DB_WORKERS)
ads_runner = BotRunner("ADS BOT", ads_bot_module.main, ADS_WEBHOOK_PATH, max_db_workers=ADS_DB_WORKERS)


async def health(request):
    return web.Response(text="OK")


async def game_webhook(request):
    try:
        data = await request.json()
        game_runner.submit_update(data)
    except Exception:
        logger.exception("Failed to submit update to game bot")
    return web.Response(text="OK")


async def ads_webhook(request):
    try:
        data = await request.json()
        ads_runner.submit_update(data)
    except Exception:
        logger.exception("Failed to submit update to ads bot")
    return web.Response(text="OK")


async def on_startup(app):
    await asyncio.to_thread(game_runner.start)
    await asyncio.to_thread(ads_runner.start)


async def on_cleanup(app):
    await asyncio.to_thread(game_runner.stop)
    await asyncio.to_thread(ads_runner.stop)


def build_web_app():
    web_app = web.Application()
    web_app.router.add_get("/health", health)
    web_app.router.add_post(GAME_WEBHOOK_PATH, game_webhook)
    web_app.router.add_post(ADS_WEBHOOK_PATH, ads_webhook)
    web_app.on_startup.append(on_startup)
    web_app.on_cleanup.append(on_cleanup)
    return web_app


if __name__ == "__main__":
    web.run_app(build_web_app(), host="0.0.0.0", port=PORT)
