"""日次パフォーマンス記録（PDCA用・秘密情報なし）。

Renderの公開スナップショット `/snapshot?public=1`（合言葉不要・機微情報を含まない）を取得し、
戦略評価に必要な項目だけを data/perf_log.csv に1行追記する。GitHub Actionsから毎日実行し、
差分をcommitして貯める（＝地合い・ベンチマーク・保有銘柄の時系列＝エクイティ判断の土台）。

記録するのは非機微情報のみ:
  date, regime_up, regime_distance_pct(指数の200日線乖離%), index(等ウェイト指数の水準),
  btc_jpy(ベンチマーク), n_positions, held(保有銘柄), targets(戦略が持ちたい上位)
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
CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "perf_log.csv"
COLUMNS = ["date", "regime_up", "regime_distance_pct", "index",
           "btc_jpy", "n_positions", "held", "targets"]


def _fetch(url: str, tries: int = 3) -> dict:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "perf-log/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (自分のサーバ)
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"[perf_log] 取得失敗({i + 1}/{tries}): {exc}", file=sys.stderr)
    raise last  # type: ignore[misc]


def _num(v, nd: int):
    return round(v, nd) if isinstance(v, (int, float)) else ""


def main() -> int:
    try:
        d = _fetch(URL)
    except Exception:  # noqa: BLE001
        print("[perf_log] スナップショット取得に失敗。今日は記録をスキップします。")
        return 0  # ワークフローは失敗扱いにしない（翌日再試行）

    reg = d.get("regime") or {}
    row = {
        "date": datetime.now(JST).strftime("%Y-%m-%d"),
        "regime_up": reg.get("up", ""),
        "regime_distance_pct": _num(reg.get("distance_pct"), 2),
        "index": _num(reg.get("index"), 5),
        "btc_jpy": _num(d.get("btc_jpy"), 0),
        "n_positions": d.get("n_positions", ""),
        "held": "|".join(d.get("held") or []),
        "targets": "|".join((reg.get("targets") or [])),
    }

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not CSV_PATH.exists()
    # 同じ日付が既にあれば上書き（重複実行対策）
    rows = []
    if not new_file:
        with CSV_PATH.open(encoding="utf-8", newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("date") != row["date"]]
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
