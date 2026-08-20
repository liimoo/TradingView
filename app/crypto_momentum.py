"""暗号資産モメンタムの「ペーパー検証」（実発注ゼロ・本番PowerZonesとは完全別系統）。

本番(bitbank)のパワーゾーン自動売買を切り替える前に、順張り(モメンタム)を紙上で追跡する。
crypto_paper_start を起点(=capital)として「200日線より上・直近look日の上昇率が高い上位N銘柄を
等ウェイトで持ち、rebal日ごとに入れ替える」戦略の仮想資産を、毎日フォワードで再計算する。

・状態は永続化しない：毎回、起点から現在までを価格データで丸ごと再計算する（Render再起動に強い）。
・データ源は本番と同じ（pz._get_data_exchange / pz.data_pair 経由の日足終値）。
・注文は一切出さない。broker/risk には触れない。表示(/paper)と月次通知のみ。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .config import settings
from . import powerzones as pz
from .indicators import sma
from .notifier import notify

logger = logging.getLogger("crypto_paper")
JST = timezone(timedelta(hours=9))


# ---- 純粋関数（ネットワーク不要・テスト対象） ----

def _mom(closes: list, i: int, look: int):
    if i < look or i >= len(closes):
        return None
    base = closes[i - look]
    return None if not base else closes[i] / base - 1


def simulate(data_ts: dict, start_ms: int, capital: float,
             top_n: int, look: int, rebal: int, sma_len: int = 200) -> dict:
    """起点(start_ms)から現在までの仮想資産推移を返す（発注しない・純粋関数）。

    data_ts: {sym: (ts_list, closes_list)}  各銘柄の日足（昇順・長さは銘柄ごとに違ってよい）
    戻り: {curve:[(ms,value)], holdings:[{sym,weight,mom}], buyhold:[(ms,value)],
           value, ret, bh_value, bh_ret, start_ms}
    """
    syms = list(data_ts.keys())
    if not syms:
        return {"curve": [], "holdings": [], "buyhold": [], "value": capital,
                "ret": 0.0, "bh_value": capital, "bh_ret": 0.0, "start_ms": start_ms}
    idx = {s: {t: i for i, t in enumerate(data_ts[s][0])} for s in syms}
    smas = {s: sma(data_ts[s][1], sma_len) for s in syms}
    all_ts = sorted({t for s in syms for t in data_ts[s][0]})
    k0 = next((k for k, t in enumerate(all_ts) if t >= start_ms), len(all_ts) - 1)

    cash = capital
    units: dict = {}
    last: dict = {}
    curve: list = []
    F = 0.0012  # 片道手数料（bitbank taker 相当）
    for k in range(len(all_ts)):
        t = all_ts[k]
        for s in syms:
            if t in idx[s]:
                c = data_ts[s][1][idx[s][t]]
                if c:
                    last[s] = c
        if k < k0:
            continue
        if (k - k0) % rebal == 0:  # 入れ替え日
            cand = []
            for s in syms:
                i = idx[s].get(t)
                if i is None:
                    continue
                c = data_ts[s][1][i]
                sm = smas[s][i]
                m = _mom(data_ts[s][1], i, look)
                if c is None or sm is None or m is None or c <= sm or m <= 0:
                    continue
                cand.append((m, s, c))
            cand.sort(reverse=True)
            pick = {s: c for _, s, c in cand[:top_n]}
            eq = cash + sum(u * last.get(s, 0) for s, u in units.items())
            for s in list(units):  # 保有外を売る
                if s not in pick:
                    cash += units[s] * last.get(s, 0) * (1 - F)
                    del units[s]
            if pick:
                tgt = eq / top_n
                for s, c in pick.items():
                    cur = units.get(s, 0) * last.get(s, c)
                    diff = tgt - cur
                    if diff > 0 and cash > 0:
                        buy = min(diff, cash)
                        units[s] = units.get(s, 0) + buy * (1 - F) / last.get(s, c)
                        cash -= buy
                    elif diff < 0:
                        u = min(-diff / last.get(s, c), units.get(s, 0))
                        cash += u * last.get(s, c) * (1 - F)
                        units[s] -= u
        curve.append((t, cash + sum(u * last.get(s, 0) for s, u in units.items())))

    value = curve[-1][1] if curve else capital
    eq_now = value
    holdings = sorted(
        ({"sym": s, "weight": (u * last.get(s, 0) / eq_now) if eq_now else 0.0,
          "mom": _mom(data_ts[s][1], len(data_ts[s][1]) - 1, look)}
         for s, u in units.items() if u > 0),
        key=lambda h: h["weight"], reverse=True)

    # 買い持ち（起点で等分・比較用）
    base = {}
    for s in syms:
        i = idx[s].get(all_ts[k0])
        if i is not None and data_ts[s][1][i]:
            base[s] = data_ts[s][1][i]
    bh = []
    for k in range(k0, len(all_ts)):
        t = all_ts[k]
        vals = []
        for s in base:
            i = idx[s].get(t)
            if i is not None and data_ts[s][1][i]:
                vals.append(data_ts[s][1][i] / base[s])
        bh.append((t, capital * (sum(vals) / len(vals) if vals else 1.0)))
    bh_value = bh[-1][1] if bh else capital

    return {"curve": curve, "holdings": holdings, "buyhold": bh,
            "value": value, "ret": value / capital - 1,
            "bh_value": bh_value, "bh_ret": bh_value / capital - 1,
            "start_ms": all_ts[k0] if all_ts else start_ms}


def _pct(v) -> str:
    return "-" if v is None else f"{'+' if v >= 0 else ''}{v * 100:.1f}%"


def build_paper_digest(res: dict, universe_n: int, today: str) -> str:
    """月次ペーパー検証レポート文（純粋関数）。"""
    cap = settings.crypto_paper_capital
    L = [f"🧪 暗号資産モメンタム・ペーパー検証（{today}）",
         f"仮想元本 ¥{cap:,.0f} → ¥{res['value']:,.0f}　（{_pct(res['ret'])}）",
         f"　参考: 同期間の買い持ち {_pct(res['bh_ret'])}"]
    if res["holdings"]:
        L.append(f"📊 現在の仮想保有（上位{len(res['holdings'])}・等ウェイト目安）")
        for h in res["holdings"]:
            L.append(f"　・{h['sym']}　上昇率 {_pct(h.get('mom'))}　保有 {h['weight'] * 100:.0f}%")
    else:
        L.append("📊 現在ノーポジション（上昇トレンドの銘柄なし＝現金退避中）")
    L.append("―――")
    L.append(f"監視 {universe_n}銘柄　※実発注なしの検証／本番切替の判断材料。売買はしていません")
    return "\n".join(L)


# ---- データ取得（本番と同じ Binance 日足） ----

def _fetch_ohlc(pair: str, limit: int = 520) -> list:
    """日足の (ms, close) を返す。最後の形成中バーは落とす。"""
    ex = pz._get_data_exchange()
    ohlcv = ex.fetch_ohlcv(pair, "1d", limit=limit)
    closed = ohlcv[:-1] if ohlcv else []
    return [(c[0], c[4]) for c in closed if c[4]]


def _universe() -> list:
    """ペーパー検証の対象（本番の売買許可銘柄）と、そのデータ用ペア。"""
    return [(s, pz.data_pair(s)) for s in settings.allowed_symbols]


async def _gather() -> dict:
    data_ts: dict = {}
    for sym, pair in _universe():
        try:
            oc = await asyncio.to_thread(_fetch_ohlc, pair)
            if len(oc) >= settings.pz_sma_len + settings.crypto_mom_lookback:
                data_ts[sym] = ([o[0] for o in oc], [o[1] for o in oc])
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s (%s) 取得失敗: %s", sym, pair, exc)
        await asyncio.sleep(0.2)
    return data_ts


def _start_ms() -> int:
    d = datetime.strptime(settings.crypto_paper_start, "%Y-%m-%d").replace(tzinfo=JST)
    return int(d.timestamp() * 1000)


async def paper_status() -> dict:
    """/paper 表示用：現在のペーパー検証結果を組み立てる。"""
    data_ts = await _gather()
    res = simulate(data_ts, _start_ms(), settings.crypto_paper_capital,
                   settings.crypto_mom_top, settings.crypto_mom_lookback,
                   settings.crypto_mom_rebal, settings.pz_sma_len)
    res["universe_n"] = len(data_ts)
    return res


async def run_and_notify() -> dict:
    res = await paper_status()
    today = datetime.now(JST).strftime("%Y-%m-%d")
    await notify(build_paper_digest(res, res.get("universe_n", 0), today))
    return {"value": round(res["value"]), "ret": round(res["ret"] * 100, 1),
            "bh_ret": round(res["bh_ret"] * 100, 1), "holdings": len(res["holdings"]),
            "universe": res.get("universe_n", 0)}


def _seconds_until_eval() -> float:
    now = datetime.now(JST)
    secs = []
    for h in settings.stocks_eval_hours or [7]:
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        secs.append((t - now).total_seconds())
    return min(secs)


_last_notify_month: tuple | None = None


async def crypto_paper_loop() -> None:
    """毎日評価し、月が替わったらペーパー検証の月次レポートを通知するループ（実発注なし）。"""
    global _last_notify_month
    if not settings.crypto_paper_enabled:
        logger.info("暗号資産モメンタムのペーパー検証は無効（CRYPTO_PAPER_ENABLED=false）")
        return
    logger.info("暗号資産モメンタム ペーパー検証 起動（起点 %s・仮想¥%s・上位%d・実発注なし）",
                settings.crypto_paper_start, f"{settings.crypto_paper_capital:,.0f}",
                settings.crypto_mom_top)
    while True:
        await asyncio.sleep(_seconds_until_eval())
        now = datetime.now(JST)
        ym = (now.year, now.month)
        if ym != _last_notify_month:
            try:
                summary = await run_and_notify()
                _last_notify_month = ym
                logger.info("暗号資産ペーパー検証 月次通知: %s", summary)
            except Exception:  # noqa: BLE001
                logger.exception("暗号資産ペーパー検証でエラー")
        await asyncio.sleep(60)
