"""銘柄スクリーニングをサーバ側で定期実行し、結果をキャッシュする。

bitbankのJPY現物ペアを単独パワーゾーン検証してランク付けし、採用/除外を分類する。
重い処理（全銘柄の全履歴を取得）なので、裏で週1回＋起動時に集計してキャッシュし、
/screen ページはキャッシュを即表示するだけにする。scripts/screen_coins.py と同じロジック。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import settings
from .indicators import rsi_wilder, sma

logger = logging.getLogger("screener")
JST = timezone(timedelta(hours=9))

# パワーゾーンのパラメータ（本番と揃える）
SMA_LEN, RSI_LEN = 200, 4
ENTRY, SCALE, EXIT = 30, 25, 55
FEE_PER_SIDE = 0.0012
MIN_TRADES = 20
STRONG_TOTAL = 100.0
TAIL_WORST = -40.0
TAIL_DD = -50.0
RECENT_DAYS = 730  # 直近この日数(=2年)のトレードが合計マイナスなら「最近ダメ」で除外

_CACHE_FILE = Path(__file__).resolve().parent.parent / "logs" / "screen_cache.json"
_cache: dict = {}
_refreshing = False


# ---- バックテスト（単独銘柄・パワーゾーン。scriptと同じ） ----

def _backtest(closes, sma200, rsi4, ts) -> list:
    """(決済タイムスタンプ, 損益率) のリストを返す。"""
    trades, in_pos, entries = [], False, []
    for i in range(len(closes)):
        if sma200[i] is None or rsi4[i] is None:
            continue
        c = closes[i]
        if not in_pos:
            if c > sma200[i] and rsi4[i] < ENTRY:
                in_pos, entries = True, [c]
        else:
            if rsi4[i] < SCALE and len(entries) == 1:
                entries.append(c)
            if rsi4[i] > EXIT:
                avg = sum(entries) / len(entries)
                trades.append((ts[i], c / avg - 1 - FEE_PER_SIDE * (len(entries) + 1)))
                in_pos, entries = False, []
    return trades


def _compound(rets: list) -> float:
    eq = 1.0
    for r in rets:
        eq *= (1 + r)
    return (eq - 1) * 100


def _stats(trades: list) -> dict:
    n = len(trades)
    if n == 0:
        return dict(n=0, wr=0.0, avg=0.0, total=0.0, maxdd=0.0, worst=0.0)
    wins = sum(1 for t in trades if t > 0)
    eq = peak = 1.0
    maxdd = 0.0
    for t in trades:
        eq *= (1 + t)
        peak = max(peak, eq)
        maxdd = min(maxdd, eq / peak - 1)
    return dict(n=n, wr=wins / n * 100, avg=sum(trades) / n * 100,
                total=(eq - 1) * 100, maxdd=maxdd * 100, worst=min(trades) * 100)


def _classify(st: dict, recent_total: float) -> tuple[str, str]:
    if st["n"] < MIN_TRADES:
        return "insufficient", f"取引{st['n']}回"
    if st["avg"] <= 0 or st["total"] < 0:
        return "exclude", "平均マイナス=悪玉"
    tail = " ⚠尾リスク大" if (st["worst"] < TAIL_WORST or st["maxdd"] < TAIL_DD) else ""
    if st["total"] >= STRONG_TOTAL:
        if recent_total < 0:  # 全期間は100%以上だが直近2年がマイナス→最近ダメで除外
            return "recent_bad", f"最近2年マイナス({recent_total:.0f}%)→除外"
        return "strong", "採用推奨" + tail
    return "ok", "検討(弱め)" + tail


def _fetch_daily(ex, symbol: str) -> list:
    since = ex.parse8601("2018-01-01T00:00:00Z")
    out: list = []
    while True:
        batch = ex.fetch_ohlcv(symbol, "1d", since=since, limit=1000)
        if not batch:
            break
        out += batch
        if len(batch) < 1000:
            break
        since = batch[-1][0] + 86_400_000
        time.sleep(ex.rateLimit / 1000)
    return out


def refresh() -> dict:
    """全銘柄を検証してキャッシュを更新する（重い・スレッドで呼ぶこと）。"""
    global _cache, _refreshing
    if _refreshing:
        return _cache
    _refreshing = True
    try:
        import ccxt
        bb = ccxt.bitbank()
        bn = getattr(ccxt, settings.pz_data_exchange)({"enableRateLimit": True})
        markets = bb.load_markets()
        jpy = sorted(s for s, v in markets.items()
                     if v.get("quote") == "JPY" and v.get("spot", True) and v.get("active", True))
        cutoff_ms = int((datetime.now(timezone.utc).timestamp() - RECENT_DAYS * 86400) * 1000)
        rows, nodata = [], []
        for pair in jpy:
            base = pair.split("/")[0]
            try:
                o = _fetch_daily(bn, base + "/USDT")
            except Exception:  # noqa: BLE001
                o = None
            if not o or len(o) < SMA_LEN + RSI_LEN + 5:
                nodata.append(base)
                continue
            closes = [c[4] for c in o]
            ts = [c[0] for c in o]
            trades = _backtest(closes, sma(closes, SMA_LEN), rsi_wilder(closes, RSI_LEN), ts)
            st = _stats([r for _, r in trades])
            recent = _compound([r for t, r in trades if t >= cutoff_ms])  # 直近2年の損益
            tier, note = _classify(st, recent)
            rows.append({"base": base, "tier": tier, "note": note, "recent": recent, **st})
        rows.sort(key=lambda r: r["total"], reverse=True)
        strong = [r["base"] for r in rows if r["tier"] == "strong"]
        ok = [r["base"] for r in rows if r["tier"] == "ok"]
        _cache = {
            "generated": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
            "rows": rows, "nodata": nodata,
            "recommend_a": [b + "/JPY" for b in strong],
            "recommend_b": [b + "/JPY" for b in strong + ok],
        }
        try:
            _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_FILE.write_text(json.dumps(_cache, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass
        logger.info("スクリーニング更新: %d銘柄", len(rows))
        return _cache
    finally:
        _refreshing = False


def get_cached() -> dict:
    """キャッシュを返す（メモリ→ファイルの順で読む）。空なら {}。"""
    global _cache
    if _cache:
        return _cache
    try:
        if _CACHE_FILE.exists():
            _cache = json.loads(_CACHE_FILE.read_text())
    except Exception:  # noqa: BLE001
        pass
    return _cache


def is_refreshing() -> bool:
    return _refreshing


async def screener_loop() -> None:
    """起動時に(キャッシュが無ければ)集計し、以降は週1回更新する。"""
    if settings.strategy != "powerzones":
        return
    if not get_cached():
        try:
            await asyncio.to_thread(refresh)
        except Exception:  # noqa: BLE001
            logger.exception("初回スクリーニング失敗")
    while True:
        await asyncio.sleep(7 * 24 * 3600)  # 週1回
        try:
            await asyncio.to_thread(refresh)
        except Exception:  # noqa: BLE001
            logger.exception("定期スクリーニング失敗")
