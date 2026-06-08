"""
Canon bot — aiohttp server entry point.
Handles incoming Bot Framework messages from Azure Bot Service.
"""

from __future__ import annotations

import logging
import os

from aiohttp import web
from botbuilder.core import (
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
    TurnContext,
)
from botbuilder.schema import Activity

from bot.canon_bot import CanonBot

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def create_app() -> web.Application:
    microsoft_app_id = os.environ.get("MICROSOFT_APP_ID", "")
    microsoft_app_password = os.environ.get("MICROSOFT_APP_PASSWORD", "")
    microsoft_app_tenant_id = os.environ.get("MICROSOFT_APP_TENANT_ID", "")

    adapter_settings = BotFrameworkAdapterSettings(
        app_id=microsoft_app_id,
        app_password=microsoft_app_password,
        channel_auth_tenant=microsoft_app_tenant_id,
    )
    adapter = BotFrameworkAdapter(adapter_settings)
    bot = CanonBot()

    async def on_error(context: TurnContext, error: Exception) -> None:
        logger.exception("Bot unhandled error: %s", error)
        await context.send_activity("An error occurred. Please try again.")

    adapter.on_turn_error = on_error

    async def messages(request: web.Request) -> web.Response:
        if "application/json" not in request.headers.get("Content-Type", ""):
            return web.Response(status=415)
        body = await request.json()
        activity = Activity().deserialize(body)
        auth_header = request.headers.get("Authorization", "")
        response = await adapter.process_activity(activity, auth_header, bot.on_turn)
        if response:
            return web.json_response(data=response.body, status=response.status)
        return web.Response(status=201)

    async def healthz(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app = web.Application()
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", healthz)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("BOT_PORT", 3978))
    web.run_app(create_app(), host="0.0.0.0", port=port)
