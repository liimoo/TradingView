"""時間内リスク監視：現在資産が直近ピークから急落したら即Discord警告（保存なし）。

日次レビュー/日次ログより速く「急落」に気づくためのもの。運用額が大きくなったので追加。
Renderの常駐プロセス内で動くので追加インフラ・コスト不要。**記録(保存)はせず、アラートのみ**
（毎時コミットのようなノイズを出さない）。ピークはプロセス内メモリ（再デプロイでリセット＝許容）。

評価は純粋関数 evaluate() に分離（テスト対象）。ヒステリシス付きで、下落閾値を割った瞬間に1回だけ
警告し、閾値の半分まで回復したら再アーム（＝下がり続けている間の連投を防ぐ）。
"""
from __future__ import annotations

import asyncio
import logging

from .broker import broker
from .config import settings
from .notifier import notify

logger = logging.getLogger("riskwatch")


def evaluate(equity, peak, below: bool, drop_pct: float):
    """急落判定（純粋関数）。

    戻り: (alert, new_peak, new_below, dd)
      dd = ピークからの下落率(0以下)。alert=Trueなら今回警告すべき。
      ヒステリシス: 未警告(below=False)で dd<=-drop_pct なら警告。
      警告済み(below=True)で dd が -drop_pct/2 より上へ回復したら再アーム(below=False)。
    """
    if equity is None:
        return (False, peak, below, None)
    new_peak = equity if (peak is None or equity > peak) else peak
    dd = (equity / new_peak - 1) if new_peak else 0.0
    thr = -abs(drop_pct)
    alert = False
    new_below = below
    if not below and dd <= thr:
        alert = True
        new_below = True
    elif below and dd > thr / 2:
        new_below = False
    return (alert, new_peak, new_below, dd)


_state = {"peak": None, "below": False}


async def risk_watch_loop() -> None:
    if not settings.risk_watch_enabled:
        logger.info("時間内リスク監視は無効（RISK_WATCH_ENABLED=false）")
        return
    logger.info("時間内リスク監視 起動（%d分ごと・直近ピーク-%.0f%%で警告）",
                settings.risk_watch_interval_min, settings.intraday_drop_pct * 100)
    while True:
        await asyncio.sleep(max(1, settings.risk_watch_interval_min) * 60)
        try:
            equity = None
            if broker.has_exchange:
                try:
                    equity, _ = await asyncio.to_thread(broker.portfolio)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("資産取得失敗（この回はスキップ）: %s", exc)
                    continue
            alert, _state["peak"], _state["below"], dd = evaluate(
                equity, _state["peak"], _state["below"], settings.intraday_drop_pct)
            if alert:
                await notify(
                    f"⚠️ 時間内リスクアラート：資産が直近ピークから {dd * 100:.1f}% 下落\n"
                    f"現在 ¥{equity:,.0f} ／ ピーク ¥{_state['peak']:,.0f}\n"
                    f"（急落の可能性。無理に動かず様子見も選択肢です）")
                logger.info("時間内リスクアラート送信 dd=%.1f%%", dd * 100)
        except Exception:  # noqa: BLE001
            logger.exception("時間内リスク監視でエラー")
