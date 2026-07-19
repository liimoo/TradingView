"""Discord Webhook への通知。

LINE Notify は2025年3月終了のため Discord Webhook を採用。
未設定でもサーバは落とさず、標準ログにフォールバックする。
"""
from __future__ import annotations

import logging

import httpx

from .config import settings

logger = logging.getLogger("notifier")


async def notify(message: str) -> None:
    """Discord にメッセージを送る。失敗しても例外を投げない（発注処理を止めない）。"""
    text = f"[{settings.trading_mode}] {message}"
    if not settings.discord_webhook_url:
        logger.info("(discord未設定) %s", text)
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(settings.discord_webhook_url, json={"content": text[:1900]})
            if resp.status_code >= 300:
                logger.warning("Discord通知失敗 status=%s body=%s", resp.status_code, resp.text[:200])
    except Exception as exc:  # noqa: BLE001 通知失敗は握りつぶす
        logger.warning("Discord通知で例外: %s", exc)
