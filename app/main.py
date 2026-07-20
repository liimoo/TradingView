"""TradingView Webhook を受けて、リスク制御→発注→Discord通知を行う中継サーバ。

起動:
  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ValidationError

from . import journal, monitor
from .broker import broker
from .config import settings
from .models import Signal
from .notifier import notify
from .report import build_report, render_html
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
        "起動: mode=%s exchange=%s allowed=%s order_quote=%s stop_loss=%s",
        settings.trading_mode,
        settings.exchange_id,
        settings.allowed_symbols,
        settings.order_quote_amount,
        settings.stop_loss_pct,
    )
    # 起動時に建玉を復元 → 損切り監視ループを開始
    await monitor.reconstruct_positions()
    task = asyncio.create_task(monitor.exit_monitor_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="TradingView RSI 中継サーバ", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "mode": settings.trading_mode,
        "killed": risk_manager.is_killed(),
        "open_positions": risk_manager.open_count,
        "positions": {s: {"base": p.base_qty, "entry": p.entry_price} for s, p in risk_manager._positions.items()},
        "stop_loss_pct": settings.stop_loss_pct,
        "take_profit_pct": settings.take_profit_pct,
        "day_pnl": round(risk_manager.day_pnl, 2),
        "day_entries": risk_manager.day_entries,
        "daily_block": risk_manager.daily_block_reason(),
    }


@app.get("/report")
async def report(secret: str = "", format: str = "html"):
    """取引記録＆集計。ブラウザで /report?secret=... を開く（URLは他人に共有しない）。"""
    if not verify_secret(secret, settings.webhook_secret):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    data = await asyncio.to_thread(build_report)
    if format == "json":
        return JSONResponse(data)
    return HTMLResponse(render_html(data))


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

    # TradingViewの銘柄表記(例 XRPUSDT)を取引所ペア(例 XRP/JPY)へ変換
    symbol = settings.resolve_symbol(signal.symbol)
    logger.info("シグナル受信: %s %s→%s price=%s rsi=%s tf=%s",
                signal.action, signal.symbol, symbol, signal.price, signal.rsi, signal.tf)

    # 3) リスク判定
    decision = risk_manager.check(symbol, signal.action)
    if not decision.allowed:
        msg = f"⏸️ 発注見送り [{decision.reason}] {signal.action} {symbol} (rsi={signal.rsi})"
        logger.info(msg)
        await notify(msg)
        return JSONResponse(status_code=200, content={"status": "skipped", "reason": decision.reason})

    # 4) 発注（DRY_RUN/TESTNET/LIVE はモードで分岐）
    risk_manager.mark_ordered(symbol, signal.action)  # クールダウン起点
    try:
        if signal.action == "buy":
            result = await asyncio.to_thread(broker.buy, symbol, settings.order_quote_amount, signal.price)
        else:  # sell = 保有分の決済
            held = risk_manager.get_position(symbol)
            result = await asyncio.to_thread(broker.sell, symbol, held.base_qty, signal.price)
    except Exception as exc:  # noqa: BLE001
        logger.exception("発注エラー")
        await notify(f"❌ 発注エラー: {signal.action} {symbol}: {exc}")
        return JSONResponse(status_code=502, content={"status": "order_error", "detail": str(exc)})

    # 建玉トラッキング更新＋取引記録
    if result.get("status") in {"ok", "dry_run"}:
        order = result.get("order") or {}
        journal.record_trade(
            {
                "mode": settings.trading_mode,
                "action": signal.action,
                "symbol": symbol,
                "quote": settings.order_quote_amount if signal.action == "buy" else None,
                "filled_base": result.get("filled_base"),
                "price": signal.price,
                "rsi": signal.rsi,
                "order_id": order.get("id"),
                "status": result.get("status"),
                "reason": ("rsi_signal" if signal.action == "sell" else None),
            }
        )
        if signal.action == "buy":
            entry_price = result.get("filled_price") or signal.price or 0.0
            risk_manager.open_position(symbol, result.get("filled_base"), entry_price)
            risk_manager.record_entry()
        else:
            exit_price = result.get("filled_price") or signal.price or 0.0
            if held and held.entry_price and exit_price:
                risk_manager.record_close((exit_price - held.entry_price) * (held.base_qty or 0))
            risk_manager.close_position(symbol)

    emoji = "🟢" if signal.action == "buy" else "🔴"
    await notify(
        f"{emoji} {signal.action.upper()} {symbol} "
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
