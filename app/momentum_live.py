"""暗号資産クロスセクション・モメンタムの本番実行（順張り・月次リバランス）。

10年バックテスト（Binance日足）で、逆張り(パワーゾーン)より順張り(モメンタム)が
大きく上回ったため導入。STRATEGY=momentum で有効。

設計方針＝「新規リスクを最小化」：
  ・実発注/リスクチェック/通知/記録は、実績のあるPowerZonesの執行 pz._execute をそのまま流用。
  ・この層が足すのは「モメンタム上位N銘柄を選び、月次で保有を入れ替える」判断だけ。
ルール（すべて日足の確定終値・月1リバランス）:
  1. 200日SMAより上 かつ 直近 look日 の上昇率がプラスの銘柄だけが対象
  2. 上昇率の高い順に上位N銘柄を等ウェイトで保有（サイズは既存の order_size_pct 等で決定）
  3. 毎月の入れ替え：上位から外れた銘柄は売却、新しく入った銘柄は買い
  ※損切りは置かない（200日線割れ等でトレンドが崩れれば次のリバランスで自動的に外れる）
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .config import settings, sized_quote
from . import powerzones as pz
from . import journal
from .broker import broker
from .indicators import sma
from .notifier import notify
from .risk import risk_manager

logger = logging.getLogger("momentum")
JST = timezone(timedelta(hours=9))


# ---- 純粋関数（ネットワーク不要・テスト対象） ----

def _mom(closes: list, i: int, look: int):
    if i < look or i >= len(closes):
        return None
    base = closes[i - look]
    return None if not base else closes[i] / base - 1


def momentum_targets(data: dict, top_n: int, look: int, sma_len: int) -> list:
    """現在の確定終値から、モメンタム上位N銘柄(JPYシンボル)を返す。
    対象＝200日線より上 かつ 上昇率>0。上昇率の高い順にN個。"""
    cand = []
    for sym, closes in data.items():
        i = len(closes) - 1
        sm = sma(closes, sma_len)[i] if len(closes) >= sma_len else None
        m = _mom(closes, i, look)
        if sm is None or m is None:
            continue
        if closes[i] > sm and m > 0:
            cand.append((m, sym))
    cand.sort(reverse=True)
    return [s for _, s in cand[:top_n]]


def reconcile(held: list, target: list) -> tuple:
    """保有(held)と目標(target)から、売る銘柄・買う銘柄を出す（新規/退出のみ）。"""
    sells = [s for s in held if s not in target]
    buys = [s for s in target if s not in held]
    return sells, buys


def market_index(data: dict) -> list:
    """全銘柄の等ウェイト正規化指数（地合い判定用）。各銘柄を期首=1に正規化して平均。"""
    if not data:
        return []
    n = min(len(c) for c in data.values() if c)
    if n < 2:
        return []
    out = []
    for i in range(n):
        vals = []
        for c in data.values():
            w = c[-n:]
            if w[0]:
                vals.append(w[i] / w[0])
        out.append(sum(vals) / len(vals) if vals else 1.0)
    return out


def regime_is_up(data: dict, sma_len: int) -> bool:
    """クリプト全体の等ウェイト指数が200日SMAより上か（＝リスクオンの地合いか）。
    判定不能（本数不足）なら True（通常運用）を返し、誤って全清算しないようにする。"""
    idx = market_index(data)
    if len(idx) < sma_len + 1:
        return True
    sm = sma(idx, sma_len)
    return bool(sm[-1] is not None and idx[-1] > sm[-1])


def plan_rebalance(current_values: dict, target: list, target_quote: float,
                   min_order: float) -> dict:
    """各建玉の時価(current_values)と目標(target・1銘柄=target_quote円)から増減プランを出す（純粋関数）。

    戻り: {'sell_all':[sym...], 'trim':[(sym,quote)...], 'buy':[(sym,quote)...]}
      sell_all = 目標から外れた銘柄（全部売る）
      trim     = 目標超過ぶんを売る額 / buy = 目標未達ぶんを買う額（min_order未満の微差は無視）
    """
    sell_all = [s for s in current_values if s not in target]
    trim, buy = [], []
    for s in target:
        diff = target_quote - current_values.get(s, 0.0)
        if diff > min_order:
            buy.append((s, diff))
        elif -diff > min_order:
            trim.append((s, -diff))
    return {"sell_all": sell_all, "trim": trim, "buy": buy}


# ---- 本番実行（発注は pz._execute を流用） ----

async def _gather() -> dict:
    """売買許可銘柄の日足終値を集める（本番と同じデータ源）。"""
    need = settings.pz_sma_len + settings.crypto_mom_lookback + 2
    data: dict = {}
    for sym in settings.allowed_symbols:
        pair = pz.data_pair(sym)
        try:
            closes = await asyncio.to_thread(pz.fetch_closed_closes, pair, need)
            if len(closes) >= need:
                data[sym] = closes
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s (%s) データ取得失敗: %s", sym, pair, exc)
        await asyncio.sleep(0.3)
    return data


def _position_values() -> dict:
    """現在のロング建玉の時価(円)を {sym: value} で返す。"""
    out = {}
    for sym, p in risk_manager._positions.items():
        if p.side != "long" or not p.base_qty:
            continue
        px = pz._safe_ticker(sym)
        if px:
            out[sym] = p.base_qty * px
    return out


async def _sell_all(sym: str) -> None:
    """建玉を全部売る（退出）。"""
    pos = risk_manager.get_position(sym)
    if pos:
        await pz._execute(sym, "sell", pos)


async def _trim(sym: str, quote: float) -> None:
    """建玉を quote円ぶん減らす（目標超過の削り）。"""
    pos = risk_manager.get_position(sym)
    px = await asyncio.to_thread(pz._safe_ticker, sym)
    if not pos or not px:
        return
    qty = min(quote / px, pos.base_qty)
    if qty <= 0:
        return
    try:
        res = await asyncio.to_thread(broker.sell, sym, qty, px)
    except Exception as exc:  # noqa: BLE001
        await notify(f"❌ モメンタム削り売り エラー {sym}: {exc}")
        return
    filled = res.get("filled_base") or qty
    if pos.entry_price:
        risk_manager.record_close((px - pos.entry_price) * filled)
    pos.base_qty = max(0.0, pos.base_qty - filled)
    if pos.base_qty * px < settings.min_order_jpy:
        risk_manager.close_position(sym)
    journal.record_trade({"mode": settings.trading_mode, "action": "sell", "symbol": sym,
                          "price": px, "filled_base": filled, "status": res.get("status"),
                          "order_id": (res.get("order") or {}).get("id"), "reason": "momentum_trim"})
    await notify(f"➖ モメンタム削り {sym} @ {px}（目標20%へ調整・約¥{quote:.0f}）")


async def _buy(sym: str, quote: float) -> None:
    """quote円ぶん買う（新規 or 増し玉）。取得単価は加重平均で更新。"""
    if quote < settings.min_order_jpy:
        return
    px = await asyncio.to_thread(pz._safe_ticker, sym)
    if not px:
        return
    try:
        res = await asyncio.to_thread(broker.buy, sym, quote, px)
    except Exception as exc:  # noqa: BLE001
        await notify(f"❌ モメンタム買い エラー {sym}: {exc}")
        return
    filled = res.get("filled_base") or 0.0
    fp = res.get("filled_price") or px
    pos = risk_manager.get_position(sym)
    if pos and pos.base_qty:  # 増し玉：数量合算・取得単価を加重平均
        new_qty = pos.base_qty + filled
        pos.entry_price = ((pos.base_qty * pos.entry_price) + filled * fp) / new_qty if new_qty else fp
        pos.base_qty = new_qty
        await notify(f"➕ モメンタム増し玉 {sym} @ {fp}（目標20%へ調整・約¥{quote:.0f}）\n{res.get('summary')}")
    else:
        risk_manager.open_position(sym, filled, fp, side="long")
        await notify(f"🟢 モメンタム買い {sym} @ {fp}（200日線上・上昇率上位）\n{res.get('summary')}")
    journal.record_trade({"mode": settings.trading_mode, "action": "buy", "symbol": sym,
                          "price": fp, "filled_base": filled, "status": res.get("status"),
                          "order_id": (res.get("order") or {}).get("id"), "reason": "momentum"})


async def rebalance(data: dict | None = None) -> dict:
    """モメンタム上位へ「継続保有分も含めて」目標へ揃え直す（実発注）。

    退出＝全売り／保有中で超過＝削り／未達＝増し玉／新規＝買い。売り→買いの順で現金を確保。
    実発注は broker、リスク管理は risk_manager を流用。損切りは置かない（順位で自動入替）。
    data を渡すと再取得せずそれを使う（ループ側で日次取得したデータを流用するため）。
    """
    if risk_manager.is_killed():
        await notify("⏸️ モメンタム: キルスイッチON中のためリバランスを見送りました")
        return {"skipped": "killed"}
    if data is None:
        data = await _gather()
    if not data:
        await notify("⚠️ モメンタム: データ取得に失敗（リバランス見送り）")
        return {"skipped": "no_data"}
    target = momentum_targets(data, settings.crypto_mom_top,
                              settings.crypto_mom_lookback, settings.pz_sma_len)

    # 地合いフィルター: クリプト全体が200日線割れの弱気相場なら、全て現金へ退避（target空）。
    regime_down = False
    if settings.crypto_regime_filter and not regime_is_up(data, settings.pz_sma_len):
        regime_down = True
        target = []

    # 1銘柄あたりの目標額 = 総資産 × order_size_pct。
    # ただし総投資は MAX_INVEST(=95%) までに抑え、5%は現金で残す。
    # これをしないと 5銘柄×20%=100% で現金が尽き、最後の買いが bitbank 60002
    # （成行買いが資金上限を超過）で失敗する。手数料と成行時の確保余裕分にも必要。
    MAX_INVEST = 0.95
    assets = free = 0.0
    if broker.has_exchange:
        try:
            assets, free = await asyncio.to_thread(broker.portfolio)
        except Exception:  # noqa: BLE001
            pass
    per = min(settings.order_size_pct, MAX_INVEST / max(1, settings.crypto_mom_top))
    target_quote = sized_quote(per, assets or 0.0, assets or 0.0, settings.order_quote_amount)

    current = _position_values()
    plan = plan_rebalance(current, target, target_quote, settings.min_order_jpy)
    daily_blocked = bool(risk_manager.daily_block_reason())

    # 売り（退出→削り）を先に行い現金を確保、次に買い（増し玉→新規）
    for sym in plan["sell_all"]:
        await _sell_all(sym)
    for sym, q in plan["trim"]:
        await _trim(sym, q)
    if daily_blocked:
        await notify("⏸️ モメンタム: 本日の損失上限に達しているため買いは見送り（売り/削りのみ実施）")
    else:
        for sym, q in plan["buy"]:
            await _buy(sym, q)

    held_after = [s for s, p in risk_manager._positions.items() if p.side == "long"]
    summary = {"target": target, "target_quote": round(target_quote), "regime_down": regime_down,
               "sell_all": plan["sell_all"], "trim": [s for s, _ in plan["trim"]],
               "buy": [s for s, _ in plan["buy"]], "held_after": held_after}
    tgt_txt = "、".join(target) if target else "なし（現金）"
    lines = [f"🔀 モメンタム月次リバランス（{datetime.now(JST):%Y-%m-%d}）"]
    if regime_down:
        lines.append("🛡️ 地合い弱気（クリプト全体が200日線↓）→ 全て現金へ退避")
    lines += [f"🎯 今月の上位{settings.crypto_mom_top}: {tgt_txt}",
              f"（1銘柄の目標 ≈ ¥{target_quote:,.0f}／総資産の{per*100:.0f}%・現金約5%は温存）"]
    if plan["sell_all"]:
        lines.append(f"➖ 退出: {'、'.join(plan['sell_all'])}")
    if plan["trim"]:
        lines.append(f"🔻 削り: {'、'.join(s for s, _ in plan['trim'])}")
    if plan["buy"]:
        lines.append(f"➕ 買い/増し: {'、'.join(s for s, _ in plan['buy'])}")
    if not any((plan["sell_all"], plan["trim"], plan["buy"])):
        lines.append("＝ 調整なし（すでに目標どおり）")
    await notify("\n".join(lines))
    logger.info("モメンタム リバランス: %s", summary)
    return summary


def _seconds_until_eval() -> float:
    now = datetime.now(JST)
    hours = settings.pz_eval_hours or [9]
    secs = []
    for h in hours:
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        secs.append((t - now).total_seconds())
    return min(secs)


_last_rebalance_month: tuple | None = None
_last_regime_up: bool | None = None


async def momentum_loop() -> None:
    """毎日 pz_eval_hours(JST) に評価。地合いは"日次"で見張り、切替時は即リバランス。

    ・上位N銘柄のローテーション＝月次（月替わりで実行）。
    ・地合い（リスクオン/オフ）＝毎日チェックし、変化した日はその場でリバランス
      （例：指数が200日線を回復→翌日には現金から再エントリー／割れ→即現金退避）。
    """
    global _last_rebalance_month, _last_regime_up
    if settings.strategy != "momentum":
        logger.info("モメンタム戦略は無効（STRATEGY=%s）", settings.strategy)
        return
    hours = ", ".join(f"{h}:00" for h in settings.pz_eval_hours)
    logger.info("モメンタム戦略 起動（地合い日次チェック＋月次ローテーション JST %s頃・上位%d）",
                hours, settings.crypto_mom_top)
    while True:
        await asyncio.sleep(_seconds_until_eval())
        now = datetime.now(JST)
        ym = (now.year, now.month)
        try:
            data = await _gather()
            if not data:
                logger.warning("モメンタム: データ取得失敗（この日は判定スキップ）")
                await asyncio.sleep(60)
                continue
            regime_up_now = (not settings.crypto_regime_filter) or regime_is_up(data, settings.pz_sma_len)
            month_changed = ym != _last_rebalance_month
            regime_changed = _last_regime_up is not None and regime_up_now != _last_regime_up
            if month_changed or regime_changed:
                reason = "月次ローテーション" if month_changed else ("地合い回復→再開" if regime_up_now else "地合い悪化→退避")
                logger.info("モメンタム リバランス発火（%s）", reason)
                await rebalance(data)  # 取得済みデータを流用
                _last_rebalance_month = ym
            _last_regime_up = regime_up_now
        except Exception:  # noqa: BLE001
            logger.exception("モメンタム 評価でエラー")
        await asyncio.sleep(60)
