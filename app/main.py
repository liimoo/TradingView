"""TradingView Webhook を受けて、リスク制御→発注→Discord通知を行う中継サーバ。

起動:
  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from .broker import broker
from .config import settings
from .models import Signal
from .notifier import notify
from .risk import risk_manager
from .security import verify_secret

# ---- logging ----
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_LOG_DIR / "server.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

# 同一足の二重POSTを弾くための直近シグナル記憶
_recent_keys: deque[str] = deque(maxlen=200)


@asynccontextmanager
async def lifespan(app: FastAPI):
    problems = settings.validate()
    for p in problems:
        logger.warning("設定警告: %s", p)
    logger.info(
        "起動: mode=%s exchange=%s allowed=%s order_quote=%s",
        settings.trading_mode,
        settings.exchange_id,
        settings.allowed_symbols,
        settings.order_quote_amount,
    )
    yield


app = FastAPI(title="TradingView RSI 中継サーバ", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "mode": settings.trading_mode,
        "killed": risk_manager.is_killed(),
        "open_positions": risk_manager.open_count,
        "positions": risk_manager._positions,
    }


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    # TradingViewは text/plain で送ることがあるため、生ボディをJSONとして読む
    raw = await request.body()
    try:
        signal = Signal.model_validate_json(raw)
    except ValidationError as exc:
        logger.warning("不正なペイロード: %s", exc)
        return JSONResponse(status_code=422, content={"error": "invalid payload"})

    # 1) 認証
    if not verify_secret(signal.secret, settings.webhook_secret):
        logger.warning("シークレット不一致（symbol=%s action=%s）", signal.symbol, signal.action)
        await notify(f"⚠️ 不正なWebhook（secret不一致）: {signal.action} {signal.symbol}")
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    # 2) 二重POST排除（同じ足の同じサイン）
    key = f"{signal.symbol}|{signal.action}|{signal.bar_time or ''}"
    if signal.bar_time and key in _recent_keys:
        logger.info("重複シグナルを無視: %s", key)
        return JSONResponse(status_code=200, content={"status": "duplicate_ignored"})
    _recent_keys.append(key)

    logger.info("シグナル受信: %s %s price=%s rsi=%s tf=%s",
                signal.action, signal.symbol, signal.price, signal.rsi, signal.tf)

    # 3) リスク判定
    decision = risk_manager.check(signal.symbol, signal.action)
    if not decision.allowed:
        msg = f"⏸️ 発注見送り [{decision.reason}] {signal.action} {signal.symbol} (rsi={signal.rsi})"
        logger.info(msg)
        await notify(msg)
        return JSONResponse(status_code=200, content={"status": "skipped", "reason": decision.reason})

    # 4) 発注（DRY_RUN/TESTNET/LIVE はモードで分岐）
    risk_manager.mark_ordered(signal.symbol, signal.action)  # クールダウン起点
    try:
        if signal.action == "buy":
            result = broker.buy(signal.symbol, settings.order_quote_amount, signal.price)
        else:  # sell = 保有分の決済
            held = risk_manager.get_position(signal.symbol)
            result = broker.sell(signal.symbol, held, signal.price)
    except Exception as exc:  # noqa: BLE001
        logger.exception("発注エラー")
        await notify(f"❌ 発注エラー: {signal.action} {signal.symbol}: {exc}")
        return JSONResponse(status_code=502, content={"status": "order_error", "detail": str(exc)})

    # 建玉トラッキング更新
    if result.get("status") in {"ok", "dry_run"}:
        if signal.action == "buy":
            risk_manager.open_position(signal.symbol, result.get("filled_base"))
        else:
            risk_manager.close_position(signal.symbol)

    emoji = "🟢" if signal.action == "buy" else "🔴"
    await notify(
        f"{emoji} {signal.action.upper()} {signal.symbol} "
        f"rsi={signal.rsi} price={signal.price}\n{result.get('summary')}"
    )
    return JSONResponse(status_code=200, content={"status": result.get("status"), "summary": result.get("summary")})


class KillswitchBody(BaseModel):
    secret: str
    on: bool


@app.post("/killswitch")
async def killswitch(body: KillswitchBody) -> JSONResponse:
    if not verify_secret(body.secret, settings.webhook_secret):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    risk_manager.set_kill(body.on)
    await notify(f"🛑 キルスイッチ {'ON（発注停止）' if body.on else 'OFF（発注再開）'}")
    return JSONResponse(status_code=200, content={"killed": body.on})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
