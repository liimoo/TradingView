"""価格監視ループ（損切り）と、起動時の建玉復元。

- 損切り: 取得単価から settings.stop_loss_pct 下落したら成行で決済
- 復元: 再デプロイ等で建玉のメモリが消えても、起動時に取引所の残高から建玉を復元
  （取得単価は直近の買い約定から推定）
"""
from __future__ import annotations

import asyncio
import logging

from . import journal
from .broker import broker
from .config import settings
from .notifier import notify
from .risk import risk_manager

logger = logging.getLogger("monitor")


def should_stop(entry_price: float, current_price: float, stop_pct: float) -> bool:
    """取得単価から stop_pct 以上下落していれば True。"""
    if not entry_price or entry_price <= 0 or not stop_pct or stop_pct <= 0:
        return False
    return current_price <= entry_price * (1 - stop_pct)


async def reconstruct_positions() -> None:
    """起動時、取引所残高から建玉を復元する（LIVE/TESTNETのみ）。"""
    if settings.trading_mode not in {"LIVE", "TESTNET"} or not broker.has_exchange:
        return
    try:
        bal = await asyncio.to_thread(broker.balance)
    except Exception as exc:  # noqa: BLE001
        logger.warning("残高取得に失敗（建玉復元スキップ）: %s", exc)
        return
    free = bal.get("free", {}) or {}
    for sym in settings.allowed_symbols:
        base = sym.split("/")[0]
        qty = float(free.get(base) or 0)
        min_amt = broker.market_min_amount(sym)
        if qty <= 0 or qty < max(min_amt, 0):
            continue
        entry = 0.0
        try:
            trades = await asyncio.to_thread(broker.my_trades, sym, 50)
            buys = [t for t in trades if t.get("side") == "buy"]
            if buys:
                entry = float(buys[-1].get("price") or 0)
        except Exception:  # noqa: BLE001
            pass
        if not entry:
            try:
                entry = await asyncio.to_thread(broker.ticker, sym)
            except Exception:  # noqa: BLE001
                entry = 0.0
        risk_manager.open_position(sym, qty, entry)
        logger.warning("起動時に建玉を復元: %s qty=%s entry=%s", sym, qty, entry)
        await notify(f"♻️ 起動時に建玉を復元: {sym} {qty} @ entry≈{entry}")


async def stop_loss_loop() -> None:
    """一定間隔で保有建玉の価格をチェックし、損切り条件で成行決済する。"""
    if settings.trading_mode not in {"LIVE", "TESTNET"} or not broker.has_exchange:
        logger.info("損切り監視は無効（DRY_RUN/取引所なし）")
        return
    if not settings.stop_loss_pct or settings.stop_loss_pct <= 0:
        logger.info("損切り無効（STOP_LOSS_PCT=0）")
        return
    logger.info("損切り監視 開始: %.1f%% / %ds間隔", settings.stop_loss_pct * 100, settings.monitor_interval_sec)
    while True:
        try:
            for sym, pos in list(risk_manager._positions.items()):
                if risk_manager.is_killed():
                    break
                try:
                    px = await asyncio.to_thread(broker.ticker, sym)
                except Exception:  # noqa: BLE001
                    continue
                if not should_stop(pos.entry_price, px, settings.stop_loss_pct):
                    continue
                logger.warning("損切り発動: %s entry=%s now=%s", sym, pos.entry_price, px)
                try:
                    res = await asyncio.to_thread(broker.sell, sym, pos.base_qty, px)
                    risk_manager.close_position(sym)
                    journal.record_trade(
                        {
                            "mode": settings.trading_mode,
                            "action": "sell",
                            "symbol": sym,
                            "filled_base": res.get("filled_base"),
                            "price": px,
                            "reason": "stop_loss",
                            "entry_price": pos.entry_price,
                            "order_id": (res.get("order") or {}).get("id"),
                            "status": res.get("status"),
                        }
                    )
                    await notify(
                        f"🛑 損切り決済: {sym} @ {px}（取得 {pos.entry_price}、-{settings.stop_loss_pct*100:.1f}%以上下落）\n{res.get('summary')}"
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("損切り発注エラー")
                    await notify(f"❌ 損切り発注エラー: {sym}: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("監視ループ例外: %s", exc)
        await asyncio.sleep(settings.monitor_interval_sec)
