"""環境変数(.env)から設定を読み込む。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()  # プロジェクト直下の .env を読む

VALID_MODES = {"DRY_RUN", "TESTNET", "LIVE"}


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _split_symbols(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def _parse_symbol_map(raw: str) -> dict[str, str]:
    """"XRPUSDT=XRP/JPY,BTCUSDT=BTC/JPY" 形式を dict に。"""
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        out[k.strip().upper()] = v.strip().upper()
    return out


@dataclass
class Settings:
    trading_mode: str = field(default_factory=lambda: _get("TRADING_MODE", "DRY_RUN").upper())
    webhook_secret: str = field(default_factory=lambda: _get("WEBHOOK_SECRET"))
    discord_webhook_url: str = field(default_factory=lambda: _get("DISCORD_WEBHOOK_URL"))

    exchange_id: str = field(default_factory=lambda: _get("EXCHANGE_ID", "bybit"))
    exchange_api_key: str = field(default_factory=lambda: _get("EXCHANGE_API_KEY"))
    exchange_api_secret: str = field(default_factory=lambda: _get("EXCHANGE_API_SECRET"))

    order_quote_amount: float = field(default_factory=lambda: float(_get("ORDER_QUOTE_AMOUNT", "1000")))
    max_open_positions: int = field(default_factory=lambda: int(_get("MAX_OPEN_POSITIONS", "1")))
    order_cooldown_sec: int = field(default_factory=lambda: int(_get("ORDER_COOLDOWN_SEC", "60")))
    # 損切り: 取得単価から この割合 下落したら自動で成行決済（0=無効）。例 0.05 = 5%
    stop_loss_pct: float = field(default_factory=lambda: float(_get("STOP_LOSS_PCT", "0")))
    # 利確: 取得単価から この割合 上昇したら自動で成行決済（0=無効）。例 0.05 = 5%
    take_profit_pct: float = field(default_factory=lambda: float(_get("TAKE_PROFIT_PCT", "0")))
    # 価格監視ループの間隔（秒）
    monitor_interval_sec: int = field(default_factory=lambda: int(_get("MONITOR_INTERVAL_SEC", "60")))
    # 取引時間帯(JST)。"8-24"で8:00〜24:00のみ新規買い可。空=24時間（制限なし）
    trading_hours: str = field(default_factory=lambda: _get("TRADING_HOURS", ""))
    # 1日の実現損失がこの額(JPY)を超えたら、その日は新規買いを停止（0=無効）
    max_daily_loss_jpy: float = field(default_factory=lambda: float(_get("MAX_DAILY_LOSS_JPY", "0")))
    # 1日の新規エントリー回数の上限（0=無効）
    max_trades_per_day: int = field(default_factory=lambda: int(_get("MAX_TRADES_PER_DAY", "0")))
    allowed_symbols: list[str] = field(default_factory=lambda: _split_symbols(_get("ALLOWED_SYMBOLS", "")))
    symbol_map: dict = field(default_factory=lambda: _parse_symbol_map(_get("SYMBOL_MAP", "")))

    host: str = field(default_factory=lambda: _get("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_get("PORT", "8000")))

    def validate(self) -> list[str]:
        """起動時の設定チェック。問題点のリストを返す（空なら健全）。"""
        problems: list[str] = []
        if self.trading_mode not in VALID_MODES:
            problems.append(f"TRADING_MODE は {VALID_MODES} のいずれか。現在: {self.trading_mode!r}")
        if not self.webhook_secret or self.webhook_secret == "change-me-to-a-long-random-string":
            problems.append("WEBHOOK_SECRET が未設定/初期値のままです。長いランダム文字列に変更してください。")
        if self.trading_mode in {"TESTNET", "LIVE"} and (not self.exchange_api_key or not self.exchange_api_secret):
            problems.append(f"{self.trading_mode} には EXCHANGE_API_KEY / EXCHANGE_API_SECRET が必要です。")
        if not self.allowed_symbols:
            problems.append("ALLOWED_SYMBOLS が空です。少なくとも1つ許可シンボルを設定してください。")
        return problems

    def resolve_symbol(self, raw: str) -> str:
        """TVの銘柄表記を取引所ペアへ変換（未登録ならそのまま大文字化して返す）。"""
        return self.symbol_map.get(raw.upper(), raw.upper())


settings = Settings()
