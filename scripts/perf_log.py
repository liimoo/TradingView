"""日次パフォーマンス記録（PDCA用・秘密情報なし）。

Renderの公開スナップショット `/snapshot?public=1`（合言葉不要・機微情報を含まない）を取得し、
戦略評価に必要な項目だけを data/perf_log.csv に1行追記する。GitHub Actionsから毎日実行し、
差分をcommitして貯める（＝地合い・ベンチマーク・保有・資産の伸びの時系列＝PDCAの土台）。

記録するのは非機微情報のみ:
  date, regime_up, regime_distance_pct(指数の200日線乖離%), index(等ウェイト指数の水準),
  btc_jpy(ベンチマーク), n_positions, held(保有銘柄), targets(戦略が持ちたい上位),
  nav(資産指数=保有銘柄の等ウェイト日次リターンを連鎖。start=100。**純資産額そのものは出さない**),
  book_ret_pct(当日のbookの騰落率%)
※総資産・現金・ADAなどの残高は記録しない（公開リポのため）。

標準ライブラリのみ（pip不要）。
"""
from __future__ import annotations

import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
URL = os.getenv("SNAPSHOT_URL",
                "https://tradingview-rsi-relay.onrender.com/snapshot?public=1")
BACKFILL_URL = os.getenv("BACKFILL_URL",
                         "https://tradingview-rsi-relay.onrender.com/equity/backfill?public=1")
CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "perf_log.csv"
COLUMNS = ["date", "regime_up", "regime_distance_pct", "index", "btc_jpy",
           "n_positions", "held", "targets", "nav", "book_ret_pct"]
NAV_START = 100.0


def _fetch_json(url: str, tries: int = 3, timeout: int = 30):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "perf-log/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (自分のサーバ/公開API)
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"[perf_log] 取得失敗({i + 1}/{tries}) {url}: {exc}", file=sys.stderr)
    raise last  # type: ignore[misc]


def _bitbank_pair(symbol: str) -> str:
    """'BTC/JPY' -> 'btc_jpy'。BCHはbitbankでは 'bcc'。"""
    base = symbol.split("/")[0].upper()
    base = "bcc" if base == "BCH" else base.lower()
    return f"{base}_jpy"


def _last2_closes(pair: str):
    """bitbank公開日足の直近2本の終値 (前日, 当日) を返す。取れなければ None。"""
    now = datetime.now(JST)
    closes: list[float] = []
    for yr in (now.year - 1, now.year):  # 年初でも2本揃うよう前年も見る
        try:
            d = _fetch_json(f"https://public.bitbank.cc/{pair}/candlestick/1day/{yr}", tries=2)
            ohlcv = d["data"]["candlestick"][0]["ohlcv"]
            closes += [float(row[3]) for row in ohlcv]
        except Exception:  # noqa: BLE001
            continue
    if len(closes) < 2:
        return None
    return closes[-2], closes[-1]


def _book_return(held: list[str]) -> float | None:
    """保有銘柄の等ウェイト日次リターン（0.01=+1%）。現金(保有なし)は0。取得全滅はNone。"""
    if not held:
        return 0.0
    rets = []
    for sym in held:
        c = _last2_closes(_bitbank_pair(sym))
        if c and c[0]:
            rets.append(c[1] / c[0] - 1)
    if not rets:
        return None
    return sum(rets) / len(rets)


def _num(v, nd: int):
    return round(v, nd) if isinstance(v, (int, float)) else ""


def _read_rows() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _prev_nav(rows: list[dict], today: str) -> float:
    for r in reversed(rows):
        if r.get("date", "") < today:
            try:
                return float(r.get("nav") or NAV_START)
            except (TypeError, ValueError):
                return NAV_START
    return NAV_START


def main() -> int:
    try:
        d = _fetch_json(URL, tries=3, timeout=60)
    except Exception:  # noqa: BLE001
        print("[perf_log] スナップショット取得に失敗。今日は記録をスキップします。")
        return 0  # ワークフローは失敗扱いにしない（翌日再試行）

    today = datetime.now(JST).strftime("%Y-%m-%d")
    reg = d.get("regime") or {}
    held = d.get("held") or []

    rows = _read_rows()
    # 履歴がまだ薄い間（初回や、今日分しか無い状態）は過去NAVをサーバから取り込む。
    # 再構築は19銘柄ぶん順次取得で重いので、読み取りは長めに待つ。以後(履歴が入れば)追記のみ。
    if len(rows) < 3:
        try:
            bf = _fetch_json(BACKFILL_URL, tries=3, timeout=180).get("rows") or []
            if bf:
                rows = bf  # 過去(8月〜)からの連続NAVで置き換え。今日分は下で再計算・上書き
                print(f"[perf_log] 過去バックフィルを取り込み: {len(bf)}日分")
        except Exception as exc:  # noqa: BLE001
            print(f"[perf_log] バックフィル取得失敗（過去なしで開始）: {exc}", file=sys.stderr)
    prev_nav = _prev_nav(rows, today)
    ret = _book_return(held)  # None=価格取得全滅
    nav = prev_nav * (1 + ret) if ret is not None else prev_nav

    row = {
        "date": today,
        "regime_up": reg.get("up", ""),
        "regime_distance_pct": _num(reg.get("distance_pct"), 2),
        "index": _num(reg.get("index"), 5),
        "btc_jpy": _num(d.get("btc_jpy"), 0),
        "n_positions": d.get("n_positions", ""),
        "held": "|".join(held),
        "targets": "|".join(reg.get("targets") or []),
        "nav": _num(nav, 3),
        "book_ret_pct": _num(ret * 100, 3) if ret is not None else "",
    }

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows = [r for r in rows if r.get("date") != today]  # 同日重複は上書き
    rows.append(row)
    rows.sort(key=lambda r: r.get("date", ""))
    with CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    print(f"[perf_log] 記録: {row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
