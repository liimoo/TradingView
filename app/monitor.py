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


def should_take_profit(entry_price: float, current_price: float, tp_pct: float) -> bool:
    """取得単価から tp_pct 以上上昇していれば True。"""
    if not entry_price or entry_price <= 0 or not tp_pct or tp_pct <= 0:
        return False
    return current_price >= entry_price * (1 + tp_pct)


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
        if settings.is_margin(sym):
            continue  # 信用は現物残高に出ないので下で別途復元
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
        # 逆指値(stop)の復元: 既存があれば採用、無ければ再設定
        if settings.stop_loss_pct > 0:
            try:
                oo = await asyncio.to_thread(broker.open_orders, sym)
                stop = next((o for o in oo if o.get("side") == "sell"), None)
                if stop:
                    risk_manager.set_stop_order(sym, stop.get("id"))
                    logger.warning("逆指値を復元(既存): %s id=%s", sym, stop.get("id"))
                else:
                    sp = entry * (1 - settings.stop_loss_pct)
                    so = await asyncio.to_thread(broker.place_stop_sell, sym, qty, sp)
                    risk_manager.set_stop_order(sym, so.get("id"))
                    logger.warning("逆指値を再設定: %s stop@%s id=%s", sym, sp, so.get("id"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("逆指値の復元/再設定に失敗: %s", exc)

    # 信用建玉(ロング/ショート)の復元
    if settings.margin_symbols:
        try:
            mpos = await asyncio.to_thread(broker.margin_positions)
        except Exception as exc:  # noqa: BLE001
            mpos = []
            logger.warning("信用建玉の取得に失敗: %s", exc)
        for p in mpos:
            pair = (p.get("pair") or "").upper().replace("_", "/")
            if pair not in settings.margin_symbols:
                continue
            side = p.get("position_side")
            qty = float(p.get("open_amount") or 0)
            entry = float(p.get("average_price") or 0)
            if qty > 0 and side in ("long", "short"):
                risk_manager.open_position(pair, qty, entry, side=side)
                logger.warning("信用建玉を復元: %s %s qty=%s entry=%s", pair, side, qty, entry)
                await notify(f"♻️ 信用建玉を復元: {pair} {side} {qty} @ {entry}")


async def _do_market_close(sym, pos, px, reason, emoji, label) -> None:
    """成行で建玉を決済し、記録・通知する。"""
    try:
        res = await asyncio.to_thread(broker.sell, sym, pos.base_qty, px)
        if pos.entry_price:
            risk_manager.record_close((px - pos.entry_price) * (pos.base_qty or 0))
        risk_manager.close_position(sym)
        journal.record_trade(
            {
                "mode": settings.trading_mode,
                "action": "sell",
                "symbol": sym,
                "filled_base": res.get("filled_base"),
                "price": px,
                "reason": reason,
                "entry_price": pos.entry_price,
                "order_id": (res.get("order") or {}).get("id"),
                "status": res.get("status"),
            }
        )
        await notify(f"{emoji} {label}決済: {sym} @ {px}（取得 {pos.entry_price}）\n{res.get('summary')}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("決済発注エラー")
        await notify(f"❌ 決済発注エラー: {sym}: {exc}")


async def _reconcile_stop(sym, pos) -> str:
    """逆指値注文の状態を確認。約定→クローズ処理して'closed'、消滅→'gone'、
    有効→'open'、逆指値なし→'none' を返す。"""
    if not pos.stop_order_id:
        return "none"
    try:
        o = await asyncio.to_thread(broker.fetch_order, sym, pos.stop_order_id)
    except Exception:  # noqa: BLE001
        return "open"  # 照会失敗時はまだ有効とみなす（誤クローズ防止）
    st = (o or {}).get("status")
    if st in ("closed", "filled"):
        fill = float(o.get("average") or o.get("price") or pos.entry_price * (1 - settings.stop_loss_pct))
        qty = float(o.get("filled") or pos.base_qty or 0)
        if pos.entry_price:
            risk_manager.record_close((fill - pos.entry_price) * qty)
        risk_manager.close_position(sym)
        journal.record_trade(
            {
                "mode": settings.trading_mode,
                "action": "sell",
                "symbol": sym,
                "filled_base": qty,
                "price": fill,
                "reason": "stop_loss",
                "entry_price": pos.entry_price,
                "order_id": pos.stop_order_id,
                "status": "ok",
            }
        )
        await notify(f"🛑 逆指値約定: {sym} @ {fill}（取得 {pos.entry_price}）")
        return "closed"
    if st in ("canceled", "cancelled", "rejected", "expired"):
        risk_manager.set_stop_order(sym, None)  # フォールバックのサーバ監視へ
        return "gone"
    return "open"


async def _handle_margin_exit(sym, pos, sl: float, tp: float) -> None:
    """信用建玉(ロング/ショート)の損切り/利確をサーバ監視で判定し成行決済する。"""
    try:
        px = await asyncio.to_thread(broker.ticker, sym)
    except Exception:  # noqa: BLE001
        return
    entry = pos.entry_price
    if not entry or entry <= 0:
        return
    reason = emoji = label = None
    if pos.side == "long":
        if sl > 0 and px <= entry * (1 - sl):
            reason, emoji, label = "stop_loss", "🛑", f"損切り(-{sl*100:.1f}%)"
        elif tp > 0 and px >= entry * (1 + tp):
            reason, emoji, label = "take_profit", "💰", f"利確(+{tp*100:.1f}%)"
    else:  # short: 値上がりが損、値下がりが利益
        if sl > 0 and px >= entry * (1 + sl):
            reason, emoji, label = "stop_loss", "🛑", f"損切り(+{sl*100:.1f}%上昇)"
        elif tp > 0 and px <= entry * (1 - tp):
            reason, emoji, label = "take_profit", "💰", f"利確(-{tp*100:.1f}%下落)"
    if reason is None:
        return
    close_side = "sell" if pos.side == "long" else "buy"
    try:
        cres = await asyncio.to_thread(broker.margin_order, sym, close_side, pos.base_qty, pos.side, px)
        sign = 1 if pos.side == "long" else -1
        risk_manager.record_close(sign * (px - entry) * (pos.base_qty or 0))
        risk_manager.close_position(sym)
        journal.record_trade({
            "mode": settings.trading_mode, "action": "close", "symbol": sym, "side": pos.side,
            "filled_base": cres.get("filled_base"), "price": px, "reason": reason,
            "entry_price": entry, "order_id": (cres.get("order") or {}).get("id"), "status": cres.get("status"),
        })
        await notify(f"{emoji} 信用{pos.side.upper()} {label}決済: {sym} @ {px}（取得 {entry}）\n{cres.get('summary')}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("信用決済エラー")
        await notify(f"❌ 信用決済エラー: {sym}: {exc}")


async def exit_monitor_loop() -> None:
    """逆指値の約定監視＋利確(サーバ主導)＋逆指値が無い時の損切りフォールバック。"""
    if settings.trading_mode not in {"LIVE", "TESTNET"} or not broker.has_exchange:
        logger.info("決済監視は無効（DRY_RUN/取引所なし）")
        return
    sl, tp = settings.stop_loss_pct, settings.take_profit_pct
    if (not sl or sl <= 0) and (not tp or tp <= 0):
        logger.info("損切り/利確とも無効")
        return
    logger.info("決済監視 開始: 逆指値-%.1f%% / 利確+%.1f%% / %ds間隔",
                sl * 100, tp * 100, settings.monitor_interval_sec)
    while True:
        try:
            for sym, pos in list(risk_manager._positions.items()):
                if risk_manager.is_killed():
                    break
                # 信用建玉はロング/ショート両対応の別処理へ
                if settings.is_margin(sym):
                    await _handle_margin_exit(sym, pos, sl, tp)
                    continue
                # 1) 逆指値(native stop)の約定/消滅を照合
                if await _reconcile_stop(sym, pos) == "closed":
                    continue
                # 2) 価格取得
                try:
                    px = await asyncio.to_thread(broker.ticker, sym)
                except Exception:  # noqa: BLE001
                    continue
                # 3) 利確（サーバ主導）: 逆指値をキャンセルしてから成行売り
                if should_take_profit(pos.entry_price, px, tp):
                    if pos.stop_order_id:
                        try:
                            await asyncio.to_thread(broker.cancel, sym, pos.stop_order_id)
                            risk_manager.set_stop_order(sym, None)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("利確前の逆指値キャンセル失敗: %s", exc)
                    await _do_market_close(sym, pos, px, "take_profit", "💰", f"利確(+{tp*100:.1f}%)")
                    continue
                # 4) フォールバック損切り（逆指値が無い時だけサーバが売る）
                if pos.stop_order_id is None and should_stop(pos.entry_price, px, sl):
                    await _do_market_close(sym, pos, px, "stop_loss", "🛑", f"損切り(-{sl*100:.1f}%)")
        except Exception as exc:  # noqa: BLE001
            logger.warning("監視ループ例外: %s", exc)
        await asyncio.sleep(settings.monitor_interval_sec)
