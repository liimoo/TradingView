"""約定の独自ログ（logs/trades.jsonl への追記）。

※Renderの無料/Starterのファイルは再デプロイで消えるため、これは補助記録。
  集計の「正」は取引所側の約定履歴（app/report.py の fetch_my_trades）に置く。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("journal")

_TRADES_FILE = Path(__file__).resolve().parent.parent / "logs" / "trades.jsonl"


def record_trade(entry: dict) -> None:
    """1件の約定/擬似約定を追記する。"""
    row = {"ts": time.time(), **entry}
    try:
        _TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _TRADES_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("取引記録の書き込み失敗: %s", exc)


def read_trades(limit: int = 200) -> list[dict]:
    if not _TRADES_FILE.exists():
        return []
    out: list[dict] = []
    try:
        with _TRADES_FILE.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:  # noqa: BLE001
        logger.warning("取引記録の読み込み失敗: %s", exc)
    return out[-limit:]
