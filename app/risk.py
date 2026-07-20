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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import settings

logger = logging.getLogger("risk")

_KILLSWITCH_FILE = Path(__file__).resolve().parent.parent / "logs" / "killswitch.on"
JST = timezone(timedelta(hours=9))


def within_trading_hours(spec: str, now: datetime | None = None) -> bool:
    """取引時間帯(JST)の判定。spec="8-24" で 8:00<=h<24。空/不正なら常にTrue。"""
    if not spec or "-" not in spec:
        return True
    try:
        a_s, b_s = spec.split("-", 1)
        a, b = int(a_s), int(b_s)
    except ValueError:
        return True
    h = (now or datetime.now(JST)).hour
    if a <= b:
        return a <= h < b
    return h >= a or h < b  # 日をまたぐ指定（例 22-6）


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""


@dataclass
class Position:
    base_qty: float
    entry_price: float
    opened_ts: float
    stop_order_id: str | None = None  # bitbankに置いた逆指値注文のID


@dataclass
class RiskManager:
    # (symbol, action) -> 直近発注の epoch 秒（クールダウン用）
    _last_order_ts: dict[tuple[str, str], float] = field(default_factory=dict)
    # symbol -> 建玉（保有base数量と取得単価）
    _positions: dict[str, Position] = field(default_factory=dict)
    # デイリー集計（JST日付ごとにリセット）
    _day: str = ""
    _day_pnl: float = 0.0
    _day_entries: int = 0

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
    def get_position(self, symbol: str) -> Position | None:
        return self._positions.get(symbol)

    @property
    def open_count(self) -> int:
        return len(self._positions)

    # ---- デイリー集計（JST日付でリセット） ----
    def _roll_day(self) -> None:
        d = datetime.now(JST).strftime("%Y-%m-%d")
        if d != self._day:
            self._day = d
            self._day_pnl = 0.0
            self._day_entries = 0

    def record_entry(self) -> None:
        self._roll_day()
        self._day_entries += 1

    def record_close(self, realized_pnl_jpy: float) -> None:
        self._roll_day()
        self._day_pnl += realized_pnl_jpy

    def daily_block_reason(self) -> str | None:
        self._roll_day()
        if settings.max_daily_loss_jpy > 0 and self._day_pnl <= -settings.max_daily_loss_jpy:
            return f"本日の損失上限¥{settings.max_daily_loss_jpy:.0f}に到達（本日損益¥{self._day_pnl:.0f}）"
        if settings.max_trades_per_day > 0 and self._day_entries >= settings.max_trades_per_day:
            return f"本日の取引回数上限{settings.max_trades_per_day}回に到達"
        return None

    @property
    def day_pnl(self) -> float:
        self._roll_day()
        return self._day_pnl

    @property
    def day_entries(self) -> int:
        self._roll_day()
        return self._day_entries

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
            if not within_trading_hours(settings.trading_hours):
                return RiskDecision(False, "取引時間外")
            block = self.daily_block_reason()
            if block:
                return RiskDecision(False, block)
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

    def open_position(self, symbol: str, base_qty: float, entry_price: float = 0.0,
                      stop_order_id: str | None = None, now: float | None = None) -> None:
        if base_qty and base_qty > 0:
            now = time.time() if now is None else now
            self._positions[symbol] = Position(
                base_qty=base_qty, entry_price=entry_price or 0.0, opened_ts=now, stop_order_id=stop_order_id
            )

    def set_stop_order(self, symbol: str, order_id: str | None) -> None:
        p = self._positions.get(symbol)
        if p:
            p.stop_order_id = order_id

    def close_position(self, symbol: str) -> None:
        self._positions.pop(symbol, None)


risk_manager = RiskManager()
