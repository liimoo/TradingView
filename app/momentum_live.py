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

from .config import settings
from . import powerzones as pz
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
    """保有(held)と目標(target)から、売る銘柄・買う銘柄を出す。"""
    sells = [s for s in held if s not in target]
    buys = [s for s in target if s not in held]
    return sells, buys


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


async def rebalance() -> dict:
    """モメンタム上位へ保有を入れ替える（実発注・リスクチェックは pz._execute を流用）。"""
    if risk_manager.is_killed():
        await notify("⏸️ モメンタム: キルスイッチON中のためリバランスを見送りました")
        return {"skipped": "killed"}
    data = await _gather()
    if not data:
        await notify("⚠️ モメンタム: データ取得に失敗（リバランス見送り）")
        return {"skipped": "no_data"}
    target = momentum_targets(data, settings.crypto_mom_top,
                              settings.crypto_mom_lookback, settings.pz_sma_len)
    held = [s for s, p in risk_manager._positions.items() if p.side == "long"]
    sells, buys = reconcile(held, target)

    sold, bought = [], []
    for sym in sells:  # 上位から外れた銘柄を売却
        pos = risk_manager.get_position(sym)
        try:
            await pz._execute(sym, "sell", pos)
            sold.append(sym)
        except Exception:  # noqa: BLE001
            logger.exception("モメンタム売却エラー: %s", sym)

    open_now = len([s for s in held if s not in sells])
    for sym in buys:  # 新しく上位に入った銘柄を買い（枠・リスク上限を尊重）
        if open_now >= settings.max_open_positions:
            break
        if not risk_manager.precheck(sym, "buy", check_allowed=False).allowed:
            continue
        try:
            await pz._execute(sym, "buy", None)
            bought.append(sym)
            open_now += 1
        except Exception:  # noqa: BLE001
            logger.exception("モメンタム買いエラー: %s", sym)

    summary = {"target": target, "sold": sold, "bought": bought,
               "held_after": [s for s, p in risk_manager._positions.items() if p.side == "long"]}
    tgt_txt = "、".join(target) if target else "なし（現金）"
    lines = [f"🔀 モメンタム月次リバランス（{datetime.now(JST):%Y-%m-%d}）",
             f"🎯 今月の上位{settings.crypto_mom_top}: {tgt_txt}"]
    if sold:
        lines.append(f"➖ 売却: {'、'.join(sold)}")
    if bought:
        lines.append(f"➕ 新規買い: {'、'.join(bought)}")
    if not sold and not bought:
        lines.append("＝ 入れ替えなし（保有継続）")
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


async def momentum_loop() -> None:
    """毎日 pz_eval_hours(JST) に評価し、月が替わったらモメンタムのリバランスを実行するループ。"""
    global _last_rebalance_month
    if settings.strategy != "momentum":
        logger.info("モメンタム戦略は無効（STRATEGY=%s）", settings.strategy)
        return
    hours = ", ".join(f"{h}:00" for h in settings.pz_eval_hours)
    logger.info("モメンタム戦略 起動（月次リバランス JST %s頃・上位%d・%d日ごと目安）",
                hours, settings.crypto_mom_top, settings.crypto_mom_rebal)
    while True:
        await asyncio.sleep(_seconds_until_eval())
        now = datetime.now(JST)
        ym = (now.year, now.month)
        if ym != _last_rebalance_month:  # 月が替わった最初の評価でリバランス（月1回）
            try:
                await rebalance()
                _last_rebalance_month = ym
            except Exception:  # noqa: BLE001
                logger.exception("モメンタム リバランスでエラー")
        await asyncio.sleep(60)
