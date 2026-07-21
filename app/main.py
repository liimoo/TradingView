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
from .config import settings, sized_quote
from .models import Signal
from .notifier import notify
from .report import build_report, render_html
from .risk import risk_manager, within_trading_hours
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
        "positions": {
            s: {"side": p.side, "base": p.base_qty, "entry": p.entry_price, "stop": p.stop_order_id}
            for s, p in risk_manager._positions.items()
        },
        "stop_loss_pct": settings.stop_loss_pct,
        "take_profit_pct": settings.take_profit_pct,
        "order_size_pct": settings.order_size_pct,
        "order_quote_amount": settings.order_quote_amount,
        "day_pnl": round(risk_manager.day_pnl, 2),
        "day_entries": risk_manager.day_entries,
        "max_daily_loss_pct": settings.max_daily_loss_pct,
        "daily_block": risk_manager.daily_block_reason(),
        "allowed_symbols": settings.allowed_symbols,
        "margin_symbols": settings.margin_symbols,
        "margin_active": settings.effective_margin_symbols(),  # 実際に信用で動く銘柄(設定∩取引所対応)
        "max_open_positions": settings.max_open_positions,
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


@app.get("/positions")
async def positions_endpoint(secret: str = ""):
    """トラッキング建玉 と bitbankの実信用建玉・証拠金状況を返す（確認用）。"""
    if not verify_secret(secret, settings.webhook_secret):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    out = {
        "tracked": {
            s: {"side": p.side, "base": p.base_qty, "entry": p.entry_price}
            for s, p in risk_manager._positions.items()
        }
    }
    if broker.has_exchange:
        try:
            out["bitbank_margin"] = await asyncio.to_thread(broker.margin_positions)
        except Exception as exc:  # noqa: BLE001
            out["bitbank_margin_error"] = str(exc)
        try:
            out["margin_status"] = await asyncio.to_thread(broker.margin_status)
        except Exception as exc:  # noqa: BLE001
            out["margin_status_error"] = str(exc)
    return JSONResponse(out)


@app.get("/orders")
async def orders(secret: str = ""):
    """取引所の未約定注文（逆指値の確認用）。/orders?secret=..."""
    if not verify_secret(secret, settings.webhook_secret):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    if not broker.has_exchange:
        return JSONResponse({"note": "取引所未接続"})
    out = {}
    for sym in settings.allowed_symbols:
        try:
            oo = await asyncio.to_thread(broker.open_orders, sym)
            out[sym] = [
                {"id": o.get("id"), "type": o.get("type"), "side": o.get("side"),
                 "amount": o.get("amount"), "trigger": o.get("triggerPrice") or o.get("stopPrice"),
                 "price": o.get("price"), "status": o.get("status")}
                for o in oo
            ]
        except Exception as exc:  # noqa: BLE001
            out[sym] = {"error": str(exc)}
    return JSONResponse(out)


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

    # 信用取引の銘柄はロング/ショートのフリップ戦略へ分岐（現物はこの下の従来ロジック）
    if settings.is_margin(symbol):
        return await handle_margin(symbol, signal)

    # 3) リスク判定
    decision = risk_manager.check(symbol, signal.action)
    if not decision.allowed:
        msg = f"⏸️ 発注見送り [{decision.reason}] {signal.action} {symbol} (rsi={signal.rsi})"
        logger.info(msg)
        await notify(msg)
        return JSONResponse(status_code=200, content={"status": "skipped", "reason": decision.reason})

    # 4) 発注（DRY_RUN/TESTNET/LIVE はモードで分岐）
    risk_manager.mark_ordered(symbol, signal.action)  # クールダウン起点
    order_quote = settings.order_quote_amount
    try:
        if signal.action == "buy":
            # 総資産を取得（発注サイズ / デイリー損失上限% の計算用）
            assets = free_jpy = None
            if (settings.order_size_pct > 0 or settings.max_daily_loss_pct > 0) and broker.has_exchange:
                try:
                    assets, free_jpy = await asyncio.to_thread(broker.portfolio)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("資産取得に失敗: %s", exc)
            # デイリー損失上限（総資産%）チェック
            block = risk_manager.daily_block_reason(assets)
            if block:
                logger.info("発注見送り: %s", block)
                await notify(f"⏸️ 発注見送り [{block}] {symbol}")
                return JSONResponse(status_code=200, content={"status": "skipped", "reason": block})
            # 発注額 = 総資産の一定割合（資金が足りなければある分だけ）。未設定なら固定額
            if settings.order_size_pct > 0 and assets:
                order_quote = sized_quote(settings.order_size_pct, assets, free_jpy or 0, settings.order_quote_amount)
            if settings.min_order_jpy > 0 and order_quote < settings.min_order_jpy:
                await notify(f"⏸️ 資金不足で見送り: {symbol}（発注可能額≈¥{order_quote:.0f} < 最小¥{settings.min_order_jpy:.0f}）")
                return JSONResponse(status_code=200, content={"status": "skipped", "reason": "insufficient_funds"})
            result = await asyncio.to_thread(broker.buy, symbol, order_quote, signal.price)
        else:  # sell = 保有分の決済
            held = risk_manager.get_position(symbol)
            # 先に逆指値(stop)をキャンセルしてから成行売り（二重売り防止）
            if held and held.stop_order_id and broker.has_exchange:
                try:
                    await asyncio.to_thread(broker.cancel, symbol, held.stop_order_id)
                    risk_manager.set_stop_order(symbol, None)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("逆指値キャンセル失敗（約定済みの可能性）: %s", exc)
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
                "quote": order_quote if signal.action == "buy" else None,
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
            # bitbankに逆指値(stop)を置く（実発注時のみ・失敗時はサーバ監視がフォールバック）
            if result.get("status") == "ok" and settings.stop_loss_pct > 0 and broker.has_exchange:
                try:
                    stop_price = entry_price * (1 - settings.stop_loss_pct)
                    so = await asyncio.to_thread(broker.place_stop_sell, symbol, result.get("filled_base"), stop_price)
                    risk_manager.set_stop_order(symbol, so.get("id"))
                    await notify(f"🔻 逆指値set: {symbol} stop@{stop_price:.4f} id={so.get('id')}")
                except Exception as exc:  # noqa: BLE001
                    logger.exception("逆指値設定エラー")
                    await notify(f"⚠️ 逆指値の設定に失敗（サーバ監視でカバー）: {symbol}: {exc}")
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


def _skip(reason: str):
    return JSONResponse(status_code=200, content={"status": "skipped", "reason": reason})


async def handle_margin(symbol: str, signal: Signal) -> JSONResponse:
    """信用取引のフリップ戦略。buy→ロング / sell→ショート。反対建玉は決済してから反転。"""
    target_side = "long" if signal.action == "buy" else "short"
    pos = risk_manager.get_position(symbol)

    # 共通チェック（キルスイッチ・許可・クールダウン）
    dec = risk_manager.precheck(symbol, signal.action)
    if not dec.allowed:
        await notify(f"⏸️ 見送り [{dec.reason}] 信用 {signal.action} {symbol}")
        return _skip(dec.reason)

    # 既に同方向なら何もしない
    if pos and pos.side == target_side:
        return _skip(f"既に{target_side}建玉あり")

    # 総資産（サイズ・デイリー損失用）
    assets = free_jpy = None
    if broker.has_exchange:
        try:
            assets, free_jpy = await asyncio.to_thread(broker.portfolio)
        except Exception as exc:  # noqa: BLE001
            logger.warning("資産取得に失敗: %s", exc)

    # エントリーゲート（時間帯・デイリー損失・建玉上限）
    if not within_trading_hours(settings.trading_hours):
        await notify(f"⏸️ 見送り [取引時間外] 信用 {symbol}")
        return _skip("取引時間外")
    block = risk_manager.daily_block_reason(assets)
    if block:
        await notify(f"⏸️ 見送り [{block}] 信用 {symbol}")
        return _skip(block)
    if pos is None and risk_manager.open_count >= settings.max_open_positions:
        await notify(f"⏸️ 見送り [建玉上限({settings.max_open_positions})] 信用 {symbol}")
        return _skip("建玉上限")

    risk_manager.mark_ordered(symbol, signal.action)

    # 発注額と価格
    order_quote = settings.order_quote_amount
    if settings.order_size_pct > 0 and assets:
        order_quote = sized_quote(settings.order_size_pct, assets, free_jpy or 0, settings.order_quote_amount)
    px = signal.price or 0.0
    if (not px or px <= 0) and broker.has_exchange:
        try:
            px = await asyncio.to_thread(broker.ticker, symbol)
        except Exception:  # noqa: BLE001
            px = 0.0

    try:
        # 1) 反対建玉があれば決済
        if pos:
            if pos.stop_order_id and broker.has_exchange:
                try:
                    await asyncio.to_thread(broker.cancel, symbol, pos.stop_order_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("逆指値キャンセル失敗: %s", exc)
            close_side = "sell" if pos.side == "long" else "buy"
            cres = await asyncio.to_thread(broker.margin_order, symbol, close_side, pos.base_qty, pos.side, px)
            exitp = cres.get("filled_price") or px
            if pos.entry_price and exitp:
                sign = 1 if pos.side == "long" else -1
                risk_manager.record_close(sign * (exitp - pos.entry_price) * (pos.base_qty or 0))
            risk_manager.close_position(symbol)
            journal.record_trade({
                "mode": settings.trading_mode, "action": "close", "symbol": symbol, "side": pos.side,
                "filled_base": cres.get("filled_base"), "price": exitp, "reason": "flip",
                "entry_price": pos.entry_price, "order_id": (cres.get("order") or {}).get("id"),
                "status": cres.get("status"),
            })

        # 2) 新規建て
        amount = (order_quote / px) if px else None
        if not amount or amount <= 0:
            await notify(f"❌ 信用: 価格取得できず建てられません {symbol}")
            return JSONResponse(status_code=502, content={"status": "order_error", "detail": "no price"})
        open_side = "buy" if target_side == "long" else "sell"
        ores = await asyncio.to_thread(broker.margin_order, symbol, open_side, amount, target_side, px)
    except Exception as exc:  # noqa: BLE001
        logger.exception("信用発注エラー")
        await notify(f"❌ 信用発注エラー: {symbol}: {exc}")
        return JSONResponse(status_code=502, content={"status": "order_error", "detail": str(exc)})

    if ores.get("status") in {"ok", "dry_run"}:
        entry_price = ores.get("filled_price") or px or 0.0
        risk_manager.open_position(symbol, ores.get("filled_base"), entry_price, side=target_side)
        risk_manager.record_entry()
        journal.record_trade({
            "mode": settings.trading_mode, "action": "open", "symbol": symbol, "side": target_side,
            "quote": order_quote, "filled_base": ores.get("filled_base"), "price": entry_price,
            "rsi": signal.rsi, "order_id": (ores.get("order") or {}).get("id"), "status": ores.get("status"),
        })

    emoji = "🟩" if target_side == "long" else "🟥"
    await notify(f"{emoji} 信用 {target_side.upper()} {symbol} rsi={signal.rsi} price={px}\n{ores.get('summary')}")
    return JSONResponse(status_code=200, content={"status": ores.get("status"), "summary": ores.get("summary")})


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
