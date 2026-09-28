"""日次リスクレビュー：資産(¥)・NAV・ドローダウン・地合いを1日1回Discordへ送る。

運用額が大きくなったので、下落・退避に能動的に気づけるようにする。
・**私的な¥資産ログ**：毎日Discordに残るので後から見返せる（公開リポには金額を出さない方針は維持）。
・**リスクアラート**：現在ドローダウンが警戒水準(dd_alert_pct)を超える、または地合いオフのとき⚠️。
Renderの既存Discord設定を使う（新しい秘密情報は不要）。NAV/DDは公開perf_log.csvから読む。
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import datetime, timedelta, timezone

import httpx

from . import equity as eq
from .broker import broker
from .config import settings
from .notifier import notify
from .report import build_positions

logger = logging.getLogger("review")
JST = timezone(timedelta(hours=9))
_RAW_PERFLOG = "https://raw.githubusercontent.com/liimoo/TradingView/main/data/perf_log.csv"


def _yen(v) -> str:
    return f"¥{v:,.0f}" if isinstance(v, (int, float)) else "-"


def compose_review(navvals: list[float], regime_up, dist, money: dict,
                   dd_alert_pct: float) -> tuple[str, bool]:
    """レビュー文と警告フラグを組み立てる（純粋関数・テスト対象）。

    regime_up: True/False/None（None=不明＝地合い列が空）。dist: 乖離%（str可）。
    money: {equity, cash, book, upnl}。
    """
    dd = eq.drawdowns(navvals) if navvals else {"max_dd": 0.0, "cur_dd": 0.0}
    thr = dd_alert_pct * 100
    dd_breach = navvals and dd["cur_dd"] <= -thr
    regime_off = regime_up is False  # 明示的にオフの時だけ警告（不明Noneは警告しない）
    warn = bool(dd_breach or regime_off)

    head = "⚠️ リスク警告 / 日次レビュー" if warn else "💰 日次レビュー"
    lines = [head,
             f"総資産 {_yen(money.get('equity'))}"
             f"（評価額 {_yen(money.get('book'))} / 現金 {_yen(money.get('cash'))}）",
             f"含み損益 {_yen(money.get('upnl'))}"]
    if navvals:
        lines.append(f"NAV {navvals[-1]:.1f}（最大DD {dd['max_dd']:.1f}% / 現在DD {dd['cur_dd']:.1f}%）")
    reg = "🟢 オン" if regime_up else ("🔴 オフ（現金退避）" if regime_up is False else "－（不明）")
    dist_txt = f"（指数の200日線乖離 {dist}%）" if dist not in ("", None) else ""
    lines.append(f"地合い {reg}{dist_txt}")
    if dd_breach:
        lines.append(f"※現在ドローダウンが警戒水準(-{thr:.0f}%)に到達。無理せず様子見も選択肢です。")
    if regime_off:
        lines.append("※地合いオフ＝botは現金退避中（新規買いは止まります）。")
    return "\n".join(lines), warn


async def _load_nav() -> tuple[list[float], dict]:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(_RAW_PERFLOG)
        rows = [x for x in csv.DictReader(io.StringIO(r.text)) if x.get("nav")]
        return [float(x["nav"]) for x in rows], (rows[-1] if rows else {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("perf_log取得失敗: %s", exc)
        return [], {}


async def _current_money() -> dict:
    book = upnl = 0.0
    equity = cash = None
    try:
        pos = await asyncio.to_thread(build_positions)
        positions = pos.get("positions") or []
        book = sum(p["price"] * p["base"] for p in positions
                   if p.get("price") and p.get("base"))
        upnl = sum(p["upnl"] for p in positions if p.get("upnl") is not None)
        if broker.has_exchange:
            try:
                equity, cash = await asyncio.to_thread(broker.portfolio)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("資産取得失敗: %s", exc)
    return {"equity": equity, "cash": cash, "book": round(book), "upnl": round(upnl)}


async def build_and_send_review() -> None:
    navvals, last = await _load_nav()
    money = await _current_money()
    ru = last.get("regime_up")
    regime_up = True if str(ru).lower() in ("true", "1") else (False if str(ru).lower() in ("false", "0") else None)
    text, warn = compose_review(navvals, regime_up, last.get("regime_distance_pct", ""),
                                money, settings.dd_alert_pct)
    await notify(text)
    logger.info("日次レビュー送信（warn=%s）", warn)


def _seconds_until(hour: int) -> float:
    now = datetime.now(JST)
    t = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return (t - now).total_seconds()


async def daily_review_loop() -> None:
    if not settings.daily_review_enabled:
        logger.info("日次レビューは無効（DAILY_REVIEW_ENABLED=false）")
        return
    logger.info("日次レビュー起動（毎日 JST %d時・DDアラート -%.0f%%）",
                settings.daily_review_hour, settings.dd_alert_pct * 100)
    while True:
        await asyncio.sleep(_seconds_until(settings.daily_review_hour))
        try:
            await build_and_send_review()
        except Exception:  # noqa: BLE001
            logger.exception("日次レビューでエラー")
        await asyncio.sleep(60)  # 同時刻の二重実行を避ける
