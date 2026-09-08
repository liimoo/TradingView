"""GitHub Actions から1日1回だけ起動する「ワンショット」実行スクリプト。

常時起動サーバ(Render・$7/月)の代わり。モメンタムは日足戦略で「1日1回動けば十分」なので、
毎朝 JST 7:00 にこのスクリプトを起動して以下を行い、終わったら終了する（＝サーバ不要・無料）。

  1. bitbank残高から現在の建玉を復元（reconstruct_positions）
  2. 暗号資産モメンタム: 地合いを日次判定し、月替わり or 地合い変化ならリバランス（実発注）
  3. 株モメンタム: 月が替わっていれば「今月の上位N＋IN/OUT」をDiscord通知（売買はしない）
  4. 月替わりなら 損益サマリーを Discord へ送る（旧 /report の代わり）
  5. 判定状態(state/cron_state.json)を更新（次回の月替わり/地合い変化の判定に使う。Actionsがcommit）

状態は state/cron_state.json に保存し、GitHub Actions がリポジトリへ commit して次回へ引き継ぐ
（サーバのメモリの代わり）。秘密情報は一切含めない。

環境変数 CRON_JOB で動作を切替（既定 auto）:
  auto      … 上記の通常フロー（毎日の自動実行）
  rebalance … 今すぐ強制リバランス（手動実行用）
  stocks    … 今すぐ株ランキングを送る（手動実行用）
  report    … 今すぐ損益サマリーを送る（手動実行用）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("cron")

JST = timezone(timedelta(hours=9))
_STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "cron_state.json"


def _load_state() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("状態ファイルの読み込みに失敗（初期化します）: %s", exc)
    return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("状態を保存: %s", state)
    except Exception as exc:  # noqa: BLE001
        logger.warning("状態ファイルの書き込みに失敗: %s", exc)


async def _run_momentum(state: dict, ym: str, force: bool) -> None:
    """暗号資産モメンタム: 地合いを日次判定し、月替わり or 地合い変化 or 強制 でリバランス。"""
    from app.config import settings
    from app import momentum_live

    if settings.strategy != "momentum":
        logger.info("STRATEGY=%s のためモメンタムはスキップ", settings.strategy)
        return

    data = await momentum_live._gather()
    if not data:
        logger.warning("データ取得に失敗（この日はモメンタム判定をスキップ）")
        return

    regime_up_now = (not settings.crypto_regime_filter) or \
        momentum_live.regime_is_up(data, settings.pz_sma_len)
    last_ym = state.get("last_rebalance_ym")
    last_regime = state.get("last_regime_up")  # None/true/false

    month_changed = ym != last_ym
    regime_changed = last_regime is not None and regime_up_now != last_regime

    if force or month_changed or regime_changed:
        reason = ("手動" if force else
                  "月次ローテーション" if month_changed else
                  ("地合い回復→再開" if regime_up_now else "地合い悪化→退避"))
        logger.info("モメンタム リバランス実行（理由: %s／地合い %s）",
                    reason, "上向き" if regime_up_now else "弱気")
        await momentum_live.rebalance(data)
        state["last_rebalance_ym"] = ym
    else:
        logger.info("モメンタム: 変化なし（地合い %s）→ リバランス見送り",
                    "上向き" if regime_up_now else "弱気")

    state["last_regime_up"] = regime_up_now


async def _run_stocks(state: dict, ym: str, force: bool) -> None:
    """株モメンタム: 月が替わっていれば（または強制なら）ランキングをDiscord通知。"""
    from app.config import settings
    from app import stocks

    if not settings.stocks_enabled:
        logger.info("STOCKS_ENABLED=false のため株通知はスキップ")
        return
    if not force and ym == state.get("last_stock_ym"):
        logger.info("株モメンタム: 今月は通知済み → スキップ")
        return
    summary = await stocks.run_and_notify()
    state["last_stock_ym"] = ym
    logger.info("株モメンタム 月次通知: %s", summary)


async def _send_pnl_summary() -> None:
    """損益サマリーをDiscordへ（旧 /report の代わり）。取引所の約定履歴から集計。"""
    from app.report import build_report
    from app.notifier import notify

    data = await asyncio.to_thread(build_report, "momentum")
    syms = data.get("symbols", {})
    total_rt = 0.0
    wins = losses = n = 0
    mtm = 0.0
    have_mtm = False
    for s in syms.values():
        rt = s.get("rt_summary") or {}
        total_rt += rt.get("total_pnl") or 0.0
        wins += rt.get("wins") or 0
        losses += rt.get("losses") or 0
        n += rt.get("n") or 0
        if s.get("mtm_pnl") is not None:
            mtm += s["mtm_pnl"]
            have_mtm = True
    wr = (wins / n * 100) if n else 0.0
    lines = ["📊 モメンタム損益サマリー（月次）",
             f"往復トレード {n}回　勝ち {wins} / 負け {losses}　勝率 {wr:.0f}%",
             f"確定損益（合計）: ¥{total_rt:,.0f}"]
    if have_mtm:
        lines.append(f"実現＋含み（現建玉込み）: ¥{mtm:,.0f}")
    lines.append("※詳細な往復明細は必要なら別途出せます（現在は非公開＝口座情報のため）")
    await notify("\n".join(lines))
    logger.info("損益サマリー送信: 確定¥%.0f / mtm¥%.0f", total_rt, mtm if have_mtm else 0.0)


async def main() -> int:
    from app.config import settings
    from app import monitor
    from app.notifier import notify

    job = (os.getenv("CRON_JOB", "auto") or "auto").strip().lower()
    now = datetime.now(JST)
    ym = f"{now.year}-{now.month:02d}"
    logger.info("=== cron_run 開始 job=%s JST=%s strategy=%s mode=%s ===",
                job, now.strftime("%Y-%m-%d %H:%M"), settings.strategy, settings.trading_mode)

    problems = settings.validate()
    for p in problems:
        logger.warning("設定警告: %s", p)

    state = _load_state()

    try:
        # 手動ジョブ（workflow_dispatch）
        if job == "stocks":
            await _run_stocks(state, ym, force=True)
            _save_state(state)
            return 0
        if job == "report":
            await _send_pnl_summary()
            return 0

        # 建玉を bitbank 残高から復元（LIVE/TESTNETのみ）
        await monitor.reconstruct_positions()

        if job == "rebalance":
            await _run_momentum(state, ym, force=True)
            _save_state(state)
            return 0

        # ---- 通常の日次フロー（job=auto） ----
        # 月替わりを検知（P&Lサマリーは株通知の前に判定用として控える）
        month_changed = ym != state.get("last_stock_ym") or ym != state.get("last_rebalance_ym")

        await _run_momentum(state, ym, force=False)
        await _run_stocks(state, ym, force=False)

        # 月が替わっていたら損益サマリーも送る（旧 /report の代替）
        if month_changed:
            try:
                await _send_pnl_summary()
            except Exception:  # noqa: BLE001
                logger.exception("損益サマリーの送信でエラー（続行）")

        _save_state(state)
        logger.info("=== cron_run 正常終了 ===")
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("cron_run で致命的エラー")
        try:
            await notify(f"❌ 日次バッチでエラー: {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
