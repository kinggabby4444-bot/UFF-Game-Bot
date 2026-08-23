import os
import logging
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


async def health(request):
    return web.Response(text="OK")


async def game_webhook(request):
    app = request.app["game_app"]
    data = await request.json()
    update = Update.de_json(data, app.bot)
    await app.process_update(update)
    return web.Response(text="OK")


async def ads_webhook(request):
    app = request.app["ads_app"]
    data = await request.json()
    update = Update.de_json(data, app.bot)
    await app.process_update(update)
    return web.Response(text="OK")


async def on_startup(app):
    game_app = app["game_app"]
    ads_app = app["ads_app"]

    await game_app.initialize()
    await ads_app.initialize()

    await game_app.bot.set_webhook(url=BASE_URL + GAME_WEBHOOK_PATH, allowed_updates=Update.ALL_TYPES)
    await ads_app.bot.set_webhook(url=BASE_URL + ADS_WEBHOOK_PATH, allowed_updates=Update.ALL_TYPES)

    await game_app.start()
    await ads_app.start()

    logger.info("Both bots initialized, webhooks set, and started.")


async def on_cleanup(app):
    game_app = app["game_app"]
    ads_app = app["ads_app"]
    await game_app.stop()
    await ads_app.stop()
    await game_app.shutdown()
    await ads_app.shutdown()


def build_web_app():
    game_app = game_bot_module.main()
    ads_app = ads_bot_module.main()

    web_app = web.Application()
    web_app["game_app"] = game_app
    web_app["ads_app"] = ads_app

    web_app.router.add_get("/health", health)
    web_app.router.add_post(GAME_WEBHOOK_PATH, game_webhook)
    web_app.router.add_post(ADS_WEBHOOK_PATH, ads_webhook)

    web_app.on_startup.append(on_startup)
    web_app.on_cleanup.append(on_cleanup)

    return web_app


if __name__ == "__main__":
    web.run_app(build_web_app(), host="0.0.0.0", port=PORT)
