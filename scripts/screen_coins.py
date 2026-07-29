"""パワーゾーン対象の銘柄を"厳選"するスクリーニングツール。

bitbankのJPY現物ペアを全部拾い、各銘柄を単独でパワーゾーン・バックテストして
ランク付けし、採用推奨/検討/除外(悪玉)/判定不能 に分類する。
定期的に回して「悪玉を外し、良い銘柄を残す」ためのフロー。

使い方: python scripts/screen_coins.py
出力の末尾に、そのまま ALLOWED_SYMBOLS に貼れる文字列を印字する。

注意: これは過去データの成績で並べる"目安"。過去の勝者が未来も勝つ保証はない
（リセンシー/生存バイアス）。頻繁に入れ替えず、数ヶ月に一度の見直し推奨。
"""
from __future__ import annotations

import sys

import ccxt

from backtest_powerzones import (  # 同じロジックを再利用
    FEE_PER_SIDE, RSI_LEN, SMA_LEN, backtest, fetch_daily, rsi_wilder, sma, stats,
)

# パワーゾーンのパラメータ（本番と揃える）
ENTRY, SCALE, EXIT = 30, 25, 55

# 分類のしきい値（透明に）
MIN_TRADES = 20        # これ未満は「判定不能」（新しすぎ/取引が少ない）
STRONG_TOTAL = 100.0   # 総リターンがこれ以上で「採用推奨」
TAIL_WORST = -40.0     # 1トレードの最悪がこれ未満なら尾リスク注意
TAIL_DD = -50.0        # 最大DDがこれ未満なら尾リスク注意


def classify(st: dict, worst: float) -> tuple[str, str]:
    """(tier, note) を返す。tier: strong/ok/exclude/insufficient。"""
    if st["n"] < MIN_TRADES:
        return "insufficient", f"取引{st['n']}回(<{MIN_TRADES})"
    if st["avg"] <= 0 or st["total"] < 0:
        return "exclude", "平均マイナス=悪玉"
    tail = " ⚠尾リスク大" if (worst < TAIL_WORST or st["maxdd"] < TAIL_DD) else ""
    if st["total"] >= STRONG_TOTAL:
        return "strong", "採用推奨" + tail
    return "ok", "検討(弱め)" + tail


def main() -> None:
    bb = ccxt.bitbank()
    bn = ccxt.binance({"enableRateLimit": True})
    markets = bb.load_markets()
    jpy = sorted(s for s, v in markets.items()
                 if v.get("quote") == "JPY" and v.get("spot", True) and v.get("active", True))
    print(f"bitbankのJPY現物ペア: {len(jpy)}銘柄。各銘柄を検証中...", file=sys.stderr)

    rows = []
    nodata = []
    for pair in jpy:
        base = pair.split("/")[0]
        try:
            o = fetch_daily(bn, base + "/USDT")
        except Exception:  # noqa: BLE001
            o = None
        if not o or len(o) < SMA_LEN + RSI_LEN + 5:
            nodata.append(base)
            continue
        closes = [c[4] for c in o]
        tr = backtest(closes, sma(closes, SMA_LEN), rsi_wilder(closes, RSI_LEN), ENTRY, SCALE, EXIT)
        st = stats(tr)
        worst = min(tr) * 100 if tr else 0.0
        tier, note = classify(st, worst)
        rows.append({"base": base, "st": st, "worst": worst, "tier": tier, "note": note})

    rows.sort(key=lambda r: r["st"]["total"], reverse=True)

    labels = {"strong": "🟢採用推奨", "ok": "🟡検討", "exclude": "🔴除外(悪玉)", "insufficient": "⚪判定不能"}
    print("\n=== 銘柄スクリーニング（単独パワーゾーン・手数料込み・過去データ） ===")
    print(f"{'銘柄':<8}{'回数':>5}{'勝率':>6}{'平均':>7}{'総ﾘﾀｰﾝ':>9}{'最悪':>7}{'最大DD':>7}  区分")
    print("-" * 70)
    for r in rows:
        st = r["st"]
        print(f"{r['base']:<8}{st['n']:>5}{st['wr']:>5.0f}%{st['avg']:>6.1f}%"
              f"{st['total']:>8.0f}%{r['worst']:>6.0f}%{st['maxdd']:>6.0f}%  "
              f"{labels[r['tier']]} {r['note']}")
    if nodata:
        print(f"\n⚪ データ源なし（Binance日足なし・対象外）: {', '.join(nodata)}")

    strong = [r["base"] for r in rows if r["tier"] == "strong"]
    ok = [r["base"] for r in rows if r["tier"] == "ok"]

    def to_symbols(bases):
        return ",".join(f"{b}/JPY" for b in bases)

    print("\n" + "=" * 70)
    print(f"【推奨A：採用推奨のみ】{len(strong)}銘柄（堅実・DD低め）")
    print("ALLOWED_SYMBOLS=" + to_symbols(strong))
    print(f"\n【推奨B：採用推奨＋検討】{len(strong) + len(ok)}銘柄（広め・機会多め）")
    print("ALLOWED_SYMBOLS=" + to_symbols(strong + ok))
    print("\n※これは過去データの目安。ポートフォリオ全体のDDは別途バックテストで要確認。")


if __name__ == "__main__":
    main()
