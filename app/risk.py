"""リスク制御と建玉管理。

- 許可シンボル・クールダウン・建玉上限・キルスイッチ
- 現物ボット前提の建玉トラッキング（買いでエントリー、売りで決済）
  * 現物は空売り不可 → 保有していないシンボルの売りは見送る
  * 同一シンボルの重ね买い（ピラミッディング）は既定で禁止

状態はプロセス内メモリで保持する（単一プロセス運用が前提）。
キルスイッチはファイルにも永続化し、再起動をまたいで停止状態を保つ。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings

logger = logging.getLogger("risk")

_KILLSWITCH_FILE = Path(__file__).resolve().parent.parent / "logs" / "killswitch.on"


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""


@dataclass
class RiskManager:
    # (symbol, action) -> 直近発注の epoch 秒（クールダウン用）
    _last_order_ts: dict[tuple[str, str], float] = field(default_factory=dict)
    # symbol -> 保有base数量（>0 のときエントリー中）
    _positions: dict[str, float] = field(default_factory=dict)

    # ---- キルスイッチ ----
    def is_killed(self) -> bool:
        return _KILLSWITCH_FILE.exists()

    def set_kill(self, on: bool) -> None:
        _KILLSWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        if on:
            _KILLSWITCH_FILE.touch()
            logger.warning("キルスイッチ ON：以降の発注を停止します")
        else:
            _KILLSWITCH_FILE.unlink(missing_ok=True)
            logger.warning("キルスイッチ OFF：発注を再開します")

    # ---- 建玉参照 ----
    def get_position(self, symbol: str) -> float | None:
        return self._positions.get(symbol)

    @property
    def open_count(self) -> int:
        return len(self._positions)

    # ---- 発注可否の判定 ----
    def check(self, symbol: str, action: str, now: float | None = None) -> RiskDecision:
        now = time.time() if now is None else now

        if self.is_killed():
            return RiskDecision(False, "キルスイッチON")

        if symbol not in settings.allowed_symbols:
            return RiskDecision(False, f"許可外シンボル: {symbol}")

        last = self._last_order_ts.get((symbol, action))
        if last is not None and (now - last) < settings.order_cooldown_sec:
            wait = settings.order_cooldown_sec - (now - last)
            return RiskDecision(False, f"クールダウン中（あと{wait:.0f}秒）")

        if action == "buy":
            if symbol in self._positions:
                return RiskDecision(False, "既に建玉あり（重ね買い禁止）")
            if self.open_count >= settings.max_open_positions:
                return RiskDecision(False, f"建玉上限({settings.max_open_positions})に到達")
        elif action == "sell":
            if symbol not in self._positions:
                return RiskDecision(False, "建玉なし（現物は空売り不可）")

        return RiskDecision(True)

    # ---- 発注確定後の状態更新 ----
    def mark_ordered(self, symbol: str, action: str, now: float | None = None) -> None:
        """クールダウン起点を記録（発注を試みたら必ず呼ぶ）。"""
        now = time.time() if now is None else now
        self._last_order_ts[(symbol, action)] = now

    def open_position(self, symbol: str, base_qty: float) -> None:
        if base_qty and base_qty > 0:
            self._positions[symbol] = base_qty

    def close_position(self, symbol: str) -> None:
        self._positions.pop(symbol, None)


risk_manager = RiskManager()
