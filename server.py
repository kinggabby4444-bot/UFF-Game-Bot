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
# One access-log line per webhook/health ping buries every real log line.
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

PORT = int(os.environ.get("PORT", 10000))

BASE_URL = os.environ.get("RENDER_EXTERNAL_URL")
if not BASE_URL:
    raise RuntimeError(
        "RENDER_EXTERNAL_URL is not set. This is auto-provided by Render "
        "on deploy - if running locally, set it manually to your public URL."
    )

GAME_WEBHOOK_PATH = "/webhook/game"
ADS_WEBHOOK_PATH = "/webhook/ads"

# How many updates one bot processes at the same time. Extra updates wait in the
# event loop (cheap) instead of all fighting for database connections at once.
MAX_CONCURRENT_UPDATES = int(os.environ.get("UPDATE_CONCURRENCY", "16"))
# Worker threads for the blocking (psycopg2) calls each bot makes via asyncio.to_thread.
BOT_THREADS = int(os.environ.get("BOT_THREADS", "24"))


class BotRunner:
    """Runs a PTB Application in its own thread with its own event loop,
    so blocking (synchronous psycopg2) database calls inside its handlers
    only ever block THIS bot's thread - never the shared aiohttp server
    that answers Telegram's webhook requests and Render's health checks."""

    def __init__(self, name, build_app_fn, webhook_path):
        self.name = name
        self.build_app_fn = build_app_fn
        self.webhook_path = webhook_path
        self.loop = None
        self.app = None
        self._sem = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        # Futures from run_coroutine_threadsafe() must be kept referenced
        # until they finish - otherwise they can be garbage-collected while
        # still pending (harmless "Task was destroyed but it is pending"
        # warning) and any exception raised inside process_update() is
        # silently lost instead of being logged.
        self._pending_futures = set()

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        # Enough worker threads that waiting for a database slot never starves
        # the rest of this bot's asyncio.to_thread() calls.
        self.loop.set_default_executor(
            ThreadPoolExecutor(max_workers=BOT_THREADS, thread_name_prefix=self.name.replace(" ", "_"))
        )
        try:
            self.app = self.build_app_fn()
            self.loop.run_until_complete(self._startup())
        except Exception:
            logger.exception(f"{self.name}: failed to start")
        finally:
            self.ready.set()
        self.loop.run_forever()

    async def _startup(self):
        self._sem = asyncio.Semaphore(MAX_CONCURRENT_UPDATES)
        await self.app.initialize()
        await self.app.bot.set_webhook(
            url=BASE_URL + self.webhook_path,
            allowed_updates=Update.ALL_TYPES,
        )
        await self.app.start()
        logger.info(f"{self.name}: initialized, webhook set, started.")

    async def _process(self, update):
        async with self._sem:
            await self.app.process_update(update)

    def start(self):
        self.thread.start()
        self.ready.wait(timeout=60)

    def submit_update(self, data):
        if self.app is None or self.loop is None or self._sem is None:
            logger.error(f"{self.name}: not ready, dropping update")
            return
        update = Update.de_json(data, self.app.bot)
        future = asyncio.run_coroutine_threadsafe(self._process(update), self.loop)
        self._pending_futures.add(future)
        future.add_done_callback(self._on_update_done)

    def _on_update_done(self, future):
        self._pending_futures.discard(future)
        try:
            future.result()
        except Exception:
            logger.exception(f"{self.name}: unhandled error while processing an update")

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


game_runner = BotRunner("GAME BOT", game_bot_module.main, GAME_WEBHOOK_PATH)
ads_runner = BotRunner("ADS BOT", ads_bot_module.main, ADS_WEBHOOK_PATH)


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