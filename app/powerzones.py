"""RSIパワーゾーン戦略（Larry Connors）— サーバ計算・日足・ロングのみ。

長さんのアドバイスに基づく新戦略。TradingView不要で、サーバが日足のOHLCVから
200日SMAと4期間RSIを計算して機械的に売買する。バックテスト(scripts/backtest_powerzones.py)と
同一ロジック。

ルール（すべて日足の確定終値で判定）:
  1. 終値が200日SMAより上（長期上昇トレンド）でのみ買う
  2. 4期間RSIが pz_entry(30) 未満 → 現物を買い（新規）
  3. 保有中に4期間RSIが pz_scale(25) 未満 → 同額を1回だけ買い増し（2倍）
  4. 4期間RSIが pz_exit(55) 超 → 全部売って利確
  ※損切りは置かない（平均回帰では逆効果と検証済み）。リスクはサイズ管理で制御。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import journal
from .broker import broker
from .config import settings, sized_quote
from .indicators import rsi_wilder, sma
from .notifier import notify
from .risk import risk_manager

logger = logging.getLogger("powerzones")
JST = timezone(timedelta(hours=9))


def decide(close, sma200, rsi4, holding: bool, already_scaled: bool) -> str:
    """当日の確定終値での判断を返す: "buy" | "scale" | "sell" | "hold"。

    holding: 既に建玉を持っているか / already_scaled: 既に買い増し済みか
    """
    if close is None or sma200 is None or rsi4 is None:
        return "hold"
    if not holding:
        if close > sma200 and rsi4 < settings.pz_entry:
            return "buy"
        return "hold"
    # 保有中
    if rsi4 > settings.pz_exit:
        return "sell"
    if rsi4 < settings.pz_scale and not already_scaled:
        return "scale"
    return "hold"


def evaluate_signal(closes: list[float]) -> dict:
    """確定した終値リストから、最新足の指標と判断材料を計算する。

    closes は「確定済みの足」だけを渡すこと（形成中の当日足は呼び出し側で除外）。
    戻り: {close, sma200, rsi4}（不足時は None）。
    """
    if not closes:
        return {"close": None, "sma200": None, "rsi4": None}
    s = sma(closes, settings.pz_sma_len)
    r = rsi_wilder(closes, settings.pz_rsi_len)
    return {"close": closes[-1], "sma200": s[-1], "rsi4": r[-1]}


def data_pair(jpy_symbol: str) -> str:
    """JPYペア → シグナル計算用ペア(USDT建て)。マップ優先、無ければ BASE/USDT に変換。"""
    m = settings.pz_data_map.get(jpy_symbol)
    if m:
        return m
    base = jpy_symbol.split("/")[0]
    return f"{base}/USDT"


# ---- 以降はネットワーク/発注を伴う実行部（DRY_RUNでは発注せずログ/通知のみ） ----

_data_ex = None


def _get_data_exchange():
    global _data_ex
    if _data_ex is None:
        import ccxt
        _data_ex = getattr(ccxt, settings.pz_data_exchange)({"enableRateLimit": True})
    return _data_ex


def fetch_closed_closes(pair: str, need: int) -> list[float]:
    """日足の確定終値を need 本以上取得する（最後の形成中の足は除外）。"""
    ex = _get_data_exchange()
    ohlcv = ex.fetch_ohlcv(pair, "1d", limit=need + 5)
    if not ohlcv:
        return []
    # 最後の足は当日の形成中の可能性が高いので落とす
    closed = ohlcv[:-1]
    return [c[4] for c in closed]


async def evaluate_symbol(jpy_symbol: str) -> dict:
    """1銘柄を評価し、必要なら発注する。結果dictを返す。"""
    need = settings.pz_sma_len + settings.pz_rsi_len + 2
    pair = data_pair(jpy_symbol)
    try:
        closes = await asyncio.to_thread(fetch_closed_closes, pair, need)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s データ取得失敗(%s): %s", jpy_symbol, pair, exc)
        return {"symbol": jpy_symbol, "action": "error", "detail": str(exc)}
    if len(closes) < need:
        return {"symbol": jpy_symbol, "action": "insufficient_data", "bars": len(closes)}

    sig = evaluate_signal(closes)
    pos = risk_manager.get_position(jpy_symbol)
    holding = pos is not None and pos.side == "long"
    already_scaled = bool(pos and getattr(pos, "scaled", False))
    action = decide(sig["close"], sig["sma200"], sig["rsi4"], holding, already_scaled)
    ctx = {"symbol": jpy_symbol, "action": action, "rsi4": round(sig["rsi4"], 1) if sig["rsi4"] is not None else None,
           "above_sma": (sig["close"] > sig["sma200"]) if sig["sma200"] else None}

    if action == "hold":
        return ctx
    if action in ("buy", "scale") and not risk_manager.precheck(jpy_symbol, "buy").allowed:
        ctx["action"] = "skipped"
        return ctx
    await _execute(jpy_symbol, action, pos)
    return ctx


async def _execute(jpy_symbol: str, action: str, pos) -> None:
    """buy/scale/sell を現物で執行し、記録・通知する。"""
    px = await asyncio.to_thread(_safe_ticker, jpy_symbol)
    if action in ("buy", "scale"):
        # 建玉サイズ = 総資産の order_size_pct（使える現金でキャップ）
        assets = free = None
        if broker.has_exchange:
            try:
                assets, free = await asyncio.to_thread(broker.portfolio)
            except Exception:  # noqa: BLE001
                pass
        quote = sized_quote(settings.order_size_pct, assets or 0, free or 0, settings.order_quote_amount)
        if quote < settings.min_order_jpy:
            await notify(f"⏸️ 資金不足で見送り: {jpy_symbol}（発注可能額≈¥{quote:.0f}）")
            return
        try:
            res = await asyncio.to_thread(broker.buy, jpy_symbol, quote, px)
        except Exception as exc:  # noqa: BLE001
            await notify(f"❌ 発注エラー: buy {jpy_symbol}: {exc}")
            return
        filled = res.get("filled_base")
        fp = res.get("filled_price") or px
        if action == "buy":
            risk_manager.open_position(jpy_symbol, filled or 0, fp or 0, side="long")
            await notify(f"🟢 パワーゾーン買い {jpy_symbol} @ {fp}（RSI売られすぎ・200日線上）\n{res.get('summary')}")
        else:  # scale
            _apply_scale(jpy_symbol, pos, filled or 0, fp or 0)
            await notify(f"➕ 買い増し {jpy_symbol} @ {fp}（さらに売られRSI<{_n(settings.pz_scale)}）\n{res.get('summary')}")
        journal.record_trade({"mode": settings.trading_mode, "action": action, "symbol": jpy_symbol,
                              "price": fp, "filled_base": filled, "status": res.get("status"),
                              "order_id": (res.get("order") or {}).get("id"), "reason": "powerzones"})
    elif action == "sell":
        qty = pos.base_qty if pos else 0
        try:
            res = await asyncio.to_thread(broker.sell, jpy_symbol, qty, px)
        except Exception as exc:  # noqa: BLE001
            await notify(f"❌ 発注エラー: sell {jpy_symbol}: {exc}")
            return
        if pos and pos.entry_price:
            risk_manager.record_close((px - pos.entry_price) * (qty or 0))
        risk_manager.close_position(jpy_symbol)
        await notify(f"💰 パワーゾーン利確 {jpy_symbol} @ {px}（RSI>{_n(settings.pz_exit)}）\n{res.get('summary')}")
        journal.record_trade({"mode": settings.trading_mode, "action": "sell", "symbol": jpy_symbol,
                              "price": px, "filled_base": res.get("filled_base"), "status": res.get("status"),
                              "order_id": (res.get("order") or {}).get("id"), "reason": "powerzones_exit"})


def _apply_scale(jpy_symbol: str, pos, add_base: float, add_price: float) -> None:
    """買い増し: 数量を合算し、取得単価を加重平均に更新、scaledフラグを立てる。"""
    if not pos:
        return
    old_qty = pos.base_qty or 0
    old_cost = old_qty * (pos.entry_price or 0)
    new_qty = old_qty + (add_base or 0)
    new_avg = (old_cost + (add_base or 0) * (add_price or 0)) / new_qty if new_qty else pos.entry_price
    pos.base_qty = new_qty
    pos.entry_price = new_avg
    pos.scaled = True


def _safe_ticker(symbol: str) -> float:
    try:
        return float(broker.ticker(symbol))
    except Exception:  # noqa: BLE001
        return 0.0


def _n(v) -> str:
    return str(int(v)) if float(v) == int(float(v)) else str(v)


async def evaluate_all() -> list[dict]:
    """全対象銘柄を評価する。"""
    results = []
    for sym in settings.allowed_symbols:
        results.append(await evaluate_symbol(sym))
        await asyncio.sleep(0.5)  # データ取得のレート配慮
    holds = sum(1 for r in results if r.get("action") == "hold")
    acts = [r for r in results if r.get("action") in ("buy", "scale", "sell", "skipped", "error")]
    logger.info("パワーゾーン評価: %d銘柄 hold=%d 行動=%d", len(results), holds, len(acts))
    return results


async def signal_status() -> list[dict]:
    """全銘柄の現在のシグナル状況を返す（発注しない・表示用＝チャートの代替）。"""
    need = settings.pz_sma_len + settings.pz_rsi_len + 2
    out = []
    for sym in settings.allowed_symbols:
        pair = data_pair(sym)
        try:
            closes = await asyncio.to_thread(fetch_closed_closes, pair, need)
        except Exception as exc:  # noqa: BLE001
            out.append({"symbol": sym, "error": str(exc)})
            continue
        if len(closes) < need:
            out.append({"symbol": sym, "note": f"データ不足({len(closes)}本)"})
            continue
        sig = evaluate_signal(closes)
        pos = risk_manager.get_position(sym)
        holding = pos is not None and pos.side == "long"
        already = bool(pos and getattr(pos, "scaled", False))
        out.append({
            "symbol": sym, "pair": pair,
            "rsi4": round(sig["rsi4"], 1) if sig["rsi4"] is not None else None,
            "above_sma": (sig["close"] > sig["sma200"]) if sig["sma200"] else None,
            "holding": holding, "scaled": already,
            "would": decide(sig["close"], sig["sma200"], sig["rsi4"], holding, already),
        })
        await asyncio.sleep(0.3)
    return out


def _seconds_until_eval() -> float:
    """次の評価時刻(複数可)までの秒数。"""
    now = datetime.now(JST)
    hours = settings.pz_eval_hours or [9]
    secs = []
    for h in hours:
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        secs.append((t - now).total_seconds())
    return min(secs)


async def powerzones_loop() -> None:
    """毎日 pz_eval_hours(JST) にパワーゾーンを評価するループ。"""
    if settings.strategy != "powerzones":
        logger.info("パワーゾーン戦略は無効（STRATEGY=%s）", settings.strategy)
        return
    hours = ", ".join(f"{h}:00" for h in settings.pz_eval_hours)
    logger.info("パワーゾーン戦略 起動（評価時刻 JST %s）", hours)
    while True:
        await asyncio.sleep(_seconds_until_eval())
        try:
            await evaluate_all()
        except Exception:  # noqa: BLE001
            logger.exception("パワーゾーン評価でエラー")
        await asyncio.sleep(60)  # 同一時刻での二重評価を避ける
