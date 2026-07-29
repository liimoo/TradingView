"""RSIパワーゾーン戦略（Larry Connors）を暗号資産の日足でバックテストする。

長さんのアドバイス（200日SMA上で4期間RSIの売られすぎを買い、RSI>55で利確、買い増しあり）を
暗号資産に応用した場合に本当に機能するかを、過去データで検証する読み取り専用スクリプト。

データ源: Binance の USDT ペア日足（長期・クリーン。シグナルの形はbitbankのJPYペアとほぼ同一）。
※本来のパワーゾーンは米国ETF(SPY等)で90%超の勝率。暗号資産は未検証なので、これはその確認。

使い方: python scripts/backtest_powerzones.py
"""
from __future__ import annotations

import sys
import time

import ccxt

# ---- 検証パラメータ ----
SYMBOLS = ["BTC/USDT", "ETH/USDT", "XRP/USDT", "SOL/USDT", "DOGE/USDT",
           "LTC/USDT", "LINK/USDT", "DOT/USDT", "ATOM/USDT", "AVAX/USDT"]
SMA_LEN = 200          # 長期トレンドフィルタ（この上でのみ買い）
RSI_LEN = 4            # 4期間RSI
FEE_PER_SIDE = 0.0012  # 片道手数料(bitbank taker 0.12%)。指値makerならほぼ0だが保守的に

# 検証する2パターン（entry, scale, exit）
VARIANTS = {
    "基本(30/25/55)": dict(entry=30, scale=25, exit=55),
    "厳しめ(25/20/55)": dict(entry=25, scale=20, exit=55),
}
SINCE = "2018-01-01T00:00:00Z"


def fetch_daily(ex, symbol: str) -> list:
    """日足OHLCVをページングで全期間取得。"""
    since = ex.parse8601(SINCE)
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
    # 重複除去
    seen = {}
    for c in out:
        seen[c[0]] = c
    return [seen[k] for k in sorted(seen)]


def sma(values: list, length: int) -> list:
    out = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= length:
            s -= values[i - length]
        if i >= length - 1:
            out[i] = s / length
    return out


def rsi_wilder(closes: list, length: int) -> list:
    """ワイルダー平滑のRSI。"""
    out = [None] * len(closes)
    if len(closes) <= length:
        return out
    gains = losses = 0.0
    for i in range(1, length + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag = gains / length
    al = losses / length
    out[length] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(length + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (length - 1) + max(d, 0.0)) / length
        al = (al * (length - 1) + max(-d, 0.0)) / length
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def backtest(closes: list, sma200: list, rsi4: list, entry: float, scale: float, exit_: float,
             stop_pct: float = 0.0, sma_exit: bool = False) -> list:
    """トレードのリストを返す。各要素は net_return(手数料込みの損益率)。

    stop_pct>0: 平均取得から stop_pct 下落(終値ベース)で損切り撤退。
    sma_exit=True: 終値が200日SMAを割ったら撤退（トレンド崩れ）。
    """
    trades = []
    in_pos = False
    entries: list = []   # 取得価格のリスト（買い増し対応）
    for i in range(len(closes)):
        if sma200[i] is None or rsi4[i] is None:
            continue
        c = closes[i]
        if not in_pos:
            if c > sma200[i] and rsi4[i] < entry:   # トレンド上 かつ 売られすぎ
                in_pos = True
                entries = [c]
        else:
            if rsi4[i] < scale and len(entries) == 1:  # さらに売られたら1回だけ買い増し
                entries.append(c)
            avg = sum(entries) / len(entries)
            hit_stop = stop_pct > 0 and c <= avg * (1 - stop_pct)
            hit_sma = sma_exit and c < sma200[i]
            if rsi4[i] > exit_ or hit_stop or hit_sma:   # 利確 or 損切り or トレンド崩れ
                gross = c / avg - 1
                fees = FEE_PER_SIDE * (len(entries) + 1)  # 買い(n回)+売り(1回)
                trades.append(gross - fees)
                in_pos = False
                entries = []
    return trades


def stats(trades: list) -> dict:
    n = len(trades)
    if n == 0:
        return dict(n=0, win=0, wr=0.0, avg=0.0, total=0.0, maxdd=0.0)
    wins = sum(1 for t in trades if t > 0)
    # 複利エクイティで総リターンと最大DD
    eq = 1.0
    peak = 1.0
    maxdd = 0.0
    for t in trades:
        eq *= (1 + t)
        peak = max(peak, eq)
        maxdd = min(maxdd, eq / peak - 1)
    return dict(n=n, win=wins, wr=wins / n * 100, avg=sum(trades) / n * 100,
                total=(eq - 1) * 100, maxdd=maxdd * 100)


def portfolio_sim(data_ts: dict, entry: float, scale: float, exit_: float,
                  frac: float, max_pos: int) -> dict:
    """全銘柄を同時に扱い、1建玉=総資産のfrac、同時max_posまで、で口座全体を回す。

    日足の口座エクイティ曲線から総リターン・年率・最大DDを出す（現実の口座に近い指標）。
    """
    syms = list(data_ts.keys())
    idx = {s: {t: i for i, t in enumerate(data_ts[s][0])} for s in syms}
    all_ts = sorted({t for s in syms for t in data_ts[s][0]})
    cash = 1.0
    pos: dict = {}          # sym -> dict(units, entries, alloc)
    last_px: dict = {}      # sym -> 直近終値（時価評価用）
    curve = []
    for t in all_ts:
        # その日の終値を更新
        for s in syms:
            if t in idx[s]:
                c = data_ts[s][1][idx[s][t]]
                if c:
                    last_px[s] = c
        equity = cash + sum(p["units"] * last_px.get(s, 0) for s, p in pos.items())
        # シグナル処理（その日の終値で）
        for s in syms:
            if t not in idx[s]:
                continue
            i = idx[s][t]
            sm, rs = data_ts[s][2][i], data_ts[s][3][i]
            if sm is None or rs is None:
                continue
            c = data_ts[s][1][i]
            if s in pos:
                p = pos[s]
                if rs < scale and len(p["entries"]) == 1 and cash >= p["alloc"]:
                    add = p["alloc"]
                    p["units"] += add * (1 - FEE_PER_SIDE) / c
                    p["entries"].append(c)
                    cash -= add
                if rs > exit_:
                    cash += p["units"] * c * (1 - FEE_PER_SIDE)
                    del pos[s]
            else:
                if c > sm and rs < entry and len(pos) < max_pos:
                    alloc = frac * equity
                    if cash >= alloc:
                        pos[s] = {"units": alloc * (1 - FEE_PER_SIDE) / c,
                                  "entries": [c], "alloc": alloc}
                        cash -= alloc
        curve.append(cash + sum(p["units"] * last_px.get(s, 0) for s, p in pos.items()))
    # 指標
    peak = curve[0]
    maxdd = 0.0
    for e in curve:
        peak = max(peak, e)
        maxdd = min(maxdd, e / peak - 1)
    years = len(all_ts) / 365.25
    total = curve[-1] - 1
    cagr = (curve[-1]) ** (1 / years) - 1 if years > 0 and curve[-1] > 0 else 0.0
    return dict(total=total * 100, cagr=cagr * 100, maxdd=maxdd * 100, days=len(all_ts))


# リスク管理の比較（基本パターン 30/25/55 に各種の撤退ルールを追加）
RISK_VARIANTS = [
    ("損切りなし(原典)", dict(stop_pct=0.0, sma_exit=False)),
    ("損切り -15%", dict(stop_pct=0.15, sma_exit=False)),
    ("損切り -20%", dict(stop_pct=0.20, sma_exit=False)),
    ("損切り -25%", dict(stop_pct=0.25, sma_exit=False)),
    ("200日線割れ撤退", dict(stop_pct=0.0, sma_exit=True)),
    ("200日割れ+損切-25%", dict(stop_pct=0.25, sma_exit=True)),
]


def main() -> None:
    ex = ccxt.binance({"enableRateLimit": True})
    print("データ取得中（Binance日足・2018〜現在）...\n", file=sys.stderr)
    data = {}
    for sym in SYMBOLS:
        try:
            o = fetch_daily(ex, sym)
            ts = [c[0] for c in o]
            closes = [c[4] for c in o]
            data[sym] = (ts, closes, sma(closes, SMA_LEN), rsi_wilder(closes, RSI_LEN))
            print(f"  {sym}: {len(o)}本", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: 取得失敗 {e}", file=sys.stderr)

    # ---- 各種パターン × リスク管理 の全体成績 ----
    for pname, p in VARIANTS.items():
        print("\n" + "=" * 78)
        print(f"■ エントリー: {pname}  （手数料 片道{FEE_PER_SIDE*100:.2f}%込み・全{len(SYMBOLS)}銘柄合算）")
        print("=" * 78)
        print(f"{'リスク管理':<20}{'トレード':>8}{'勝率':>8}{'平均':>8}{'最悪1回':>9}"
              f"{'平均DD':>9}{'最悪DD':>9}")
        print("-" * 78)
        for rname, rk in RISK_VARIANTS:
            all_trades = []
            dds = []
            for sym in SYMBOLS:
                if sym not in data:
                    continue
                _ts, closes, s, r = data[sym]
                if len(closes) < SMA_LEN + RSI_LEN + 5:
                    continue
                tr = backtest(closes, s, r, p["entry"], p["scale"], p["exit"], **rk)
                all_trades += tr
                if tr:
                    dds.append(stats(tr)["maxdd"])
            st = stats(all_trades)
            worst = min(all_trades) * 100 if all_trades else 0.0
            avg_dd = sum(dds) / len(dds) if dds else 0.0
            worst_dd = min(dds) if dds else 0.0
            print(f"{rname:<20}{st['n']:>8}{st['wr']:>7.1f}%{st['avg']:>7.2f}%"
                  f"{worst:>8.1f}%{avg_dd:>8.1f}%{worst_dd:>8.1f}%")

    # ---- 口座全体シミュレーション（サイズ管理でDDを抑える）----
    print("\n" + "=" * 78)
    print("■ 口座全体シミュレーション（基本30/25/55・損切りなし・全10銘柄を分散運用）")
    print("   1建玉=総資産のX%、同時Y銘柄まで。実際の口座に近いDDを測る。")
    print("=" * 78)
    print(f"{'1建玉サイズ':<12}{'同時上限':>8}{'総リターン':>12}{'年率':>9}{'最大DD':>9}")
    print("-" * 78)
    p = VARIANTS["基本(30/25/55)"]
    for frac, mx in [(0.10, 5), (0.15, 6), (0.20, 6), (0.20, 8), (0.30, 8)]:
        r = portfolio_sim(data, p["entry"], p["scale"], p["exit"], frac, mx)
        print(f"{f'{frac*100:.0f}%':<12}{mx:>8}{r['total']:>11.0f}%{r['cagr']:>8.1f}%{r['maxdd']:>8.1f}%")


if __name__ == "__main__":
    main()
