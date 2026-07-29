"""環境変数(.env)から設定を読み込む。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # プロジェクト直下の .env を読む

VALID_MODES = {"DRY_RUN", "TESTNET", "LIVE"}

# ブラウザから調整できるパラメータ（キー: 型）
_EDITABLE = {
    "stop_loss_pct": float,
    "take_profit_pct": float,
    "order_size_pct": float,
    "order_quote_amount": float,
    "max_daily_loss_pct": float,
    "max_open_positions": int,
    "order_cooldown_sec": int,
}
_OVERRIDE_FILE = Path(__file__).resolve().parent.parent / "logs" / "overrides.json"


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _split_symbols(raw: str) -> list[str]:
    out = []
    for s in raw.split(","):
        s = s.strip().strip('"').strip("'").strip()  # 引用符も除去（貼り付けミス対策）
        if s:
            out.append(s)
    return out


def _parse_hours(raw: str) -> list[int]:
    """"9,21" → [9, 21]。0〜23の整数のみ、重複除去・昇順。空なら[9]。"""
    out = set()
    for s in (raw or "").split(","):
        s = s.strip()
        if not s:
            continue
        try:
            h = int(s)
        except ValueError:
            continue
        if 0 <= h <= 23:
            out.add(h)
    return sorted(out) or [9]


def sized_quote(pct: float, total_assets: float, free_jpy: float, fixed: float) -> float:
    """発注額を決める。pct>0なら min(総資産×pct, 使える現金)、そうでなければ固定額。"""
    if pct and pct > 0:
        return max(0.0, min(total_assets * pct, free_jpy))
    return fixed


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
    # 発注額を「総資産のこの割合」にする（0=無効で固定額 order_quote_amount を使う）。例 0.10=10%
    order_size_pct: float = field(default_factory=lambda: float(_get("ORDER_SIZE_PCT", "0")))
    # これ未満の発注額になる場合は資金不足として見送り（0=チェックなし）
    min_order_jpy: float = field(default_factory=lambda: float(_get("MIN_ORDER_JPY", "0")))
    max_open_positions: int = field(default_factory=lambda: int(_get("MAX_OPEN_POSITIONS", "1")))
    order_cooldown_sec: int = field(default_factory=lambda: int(_get("ORDER_COOLDOWN_SEC", "60")))
    # エントリー/サイン決済の注文方法。"market"=成行 / "limit"=指値(maker,手数料節約)。
    # 損切り・利確などの安全決済は常に成行（この設定に関係なく）。
    order_entry_type: str = field(default_factory=lambda: _get("ORDER_ENTRY_TYPE", "market").lower())
    # 指値が約定しない時に成行へ切り替えるまでの待ち時間（秒）。order_entry_type=limit時のみ有効。
    maker_wait_sec: int = field(default_factory=lambda: int(_get("MAKER_WAIT_SEC", "25")))
    # 損切り: 取得単価から この割合 下落したら自動で成行決済（0=無効）。例 0.05 = 5%
    stop_loss_pct: float = field(default_factory=lambda: float(_get("STOP_LOSS_PCT", "0")))
    # 利確: 取得単価から この割合 上昇したら自動で成行決済（0=無効）。例 0.05 = 5%
    take_profit_pct: float = field(default_factory=lambda: float(_get("TAKE_PROFIT_PCT", "0")))
    # RSIの判定閾値（TradingView側のアラート設定と一致させる。レポート表示・記録用）。
    rsi_oversold: float = field(default_factory=lambda: float(_get("RSI_OVERSOLD", "30")))
    rsi_overbought: float = field(default_factory=lambda: float(_get("RSI_OVERBOUGHT", "70")))

    # ===== 戦略モード =====
    # "webhook" = 旧: TradingViewのRSIアラートを受けて売買（15分逆張り・ロング+ショート）
    # "powerzones" = 新: サーバが日足でLarry ConnorsのRSIパワーゾーンを計算し売買（ロングのみ）
    strategy: str = field(default_factory=lambda: _get("STRATEGY", "webhook").lower())
    # --- パワーゾーン(日足・ロングのみ)のパラメーター ---
    pz_sma_len: int = field(default_factory=lambda: int(_get("PZ_SMA_LEN", "200")))   # 長期トレンド
    pz_rsi_len: int = field(default_factory=lambda: int(_get("PZ_RSI_LEN", "4")))     # 4期間RSI
    pz_entry: float = field(default_factory=lambda: float(_get("PZ_ENTRY", "30")))    # 買い
    pz_scale: float = field(default_factory=lambda: float(_get("PZ_SCALE", "25")))    # 買い増し
    pz_exit: float = field(default_factory=lambda: float(_get("PZ_EXIT", "55")))      # 利確
    pz_max_positions: int = field(default_factory=lambda: int(_get("PZ_MAX_POSITIONS", "5")))
    # 対象銘柄を動的にするか。true=スクリーニングで総リターン100%以上(採用推奨)の銘柄を自動対象に
    # ＋保有中の銘柄は100%を割っても対象に残す（決済まで管理）。false=allowed_symbols固定。
    pz_dynamic_universe: bool = field(default_factory=lambda: _get("PZ_DYNAMIC_UNIVERSE", "false").lower() in ("1", "true", "yes"))
    # 判定を回す時刻(JST hour)。カンマ区切りで複数可。例 "9" / "9,21"（朝晩2回=保険）。
    # 日足戦略なので同じ日足を見る限り判断は同じ。複数化は主に「朝の失敗を夜に拾う」信頼性向上のため。
    pz_eval_hours: list = field(default_factory=lambda: _parse_hours(_get("PZ_EVAL_HOURS", _get("PZ_EVAL_HOUR", "9"))))
    # シグナル計算に使うOHLCVの取得元(ccxt id)。既定binance(長期・安定、JPYペアとほぼ同形)。
    pz_data_exchange: str = field(default_factory=lambda: _get("PZ_DATA_EXCHANGE", "binance"))
    # bitbank JPYペア → データ取得用ペア の対応（USDT建てで代用）。例 BTC/JPY=BTC/USDT
    pz_data_map: dict = field(default_factory=lambda: _parse_symbol_map(_get("PZ_DATA_MAP", "")))
    # 価格監視ループの間隔（秒）
    monitor_interval_sec: int = field(default_factory=lambda: int(_get("MONITOR_INTERVAL_SEC", "60")))
    # Webhookを同期処理するか（テスト用。本番はFalse=即200返してバックグラウンド処理）
    webhook_sync: bool = field(default_factory=lambda: _get("WEBHOOK_SYNC", "false").lower() in ("1", "true", "yes"))
    # 起動時の「建玉を復元しました」通知を出すか。開発中(頻繁な再デプロイ)は false 推奨。
    notify_restore: bool = field(default_factory=lambda: _get("NOTIFY_RESTORE", "true").lower() in ("1", "true", "yes"))
    # Discord通知の全体スイッチ。false=一切通知しない（開発中の静音用）。本番稼働前にtrueへ戻すこと。
    notify_enabled: bool = field(default_factory=lambda: _get("NOTIFY_ENABLED", "true").lower() in ("1", "true", "yes"))
    # 取引時間帯(JST)。"8-24"で8:00〜24:00のみ新規買い可。空=24時間（制限なし）
    trading_hours: str = field(default_factory=lambda: _get("TRADING_HOURS", ""))
    # 1日の実現損失がこの額(JPY)を超えたら、その日は新規買いを停止（0=無効）
    max_daily_loss_jpy: float = field(default_factory=lambda: float(_get("MAX_DAILY_LOSS_JPY", "0")))
    # 1日の実現損失が「総資産×この割合」を超えたら停止（0=無効。設定時は上のJPYより優先）。例 0.08=8%
    max_daily_loss_pct: float = field(default_factory=lambda: float(_get("MAX_DAILY_LOSS_PCT", "0")))
    # 1日の新規エントリー回数の上限（0=無効）
    max_trades_per_day: int = field(default_factory=lambda: int(_get("MAX_TRADES_PER_DAY", "0")))
    allowed_symbols: list[str] = field(default_factory=lambda: _split_symbols(_get("ALLOWED_SYMBOLS", "")))
    symbol_map: dict = field(default_factory=lambda: _parse_symbol_map(_get("SYMBOL_MAP", "")))
    # 信用取引(ロング+ショート)で扱う銘柄。ここに無い銘柄は現物ロング専用。例 BTC/JPY,ETH/JPY,XRP/JPY
    margin_symbols: list[str] = field(default_factory=lambda: _split_symbols(_get("MARGIN_SYMBOLS", "")))
    # 取引所が信用に対応している銘柄(安全ガード)。bitbankはBTC/ETH/XRP/SOL/DOGEの5銘柄。ここに無い銘柄は信用不可→現物扱い
    margin_capable: list[str] = field(
        default_factory=lambda: _split_symbols(
            _get("MARGIN_CAPABLE", "BTC/JPY,ETH/JPY,XRP/JPY,SOL/JPY,DOGE/JPY")
        )
    )

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

    # ---- ブラウザからの調整（ランタイム上書き） ----
    def editable(self) -> dict:
        return {k: getattr(self, k) for k in _EDITABLE}

    def apply_overrides(self, values: dict, persist: bool = True) -> dict:
        applied = {}
        for k, v in (values or {}).items():
            if k in _EDITABLE and v not in (None, ""):
                try:
                    setattr(self, k, _EDITABLE[k](v))
                    applied[k] = getattr(self, k)
                except (ValueError, TypeError):
                    pass
        if persist and applied:
            try:
                _OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
                _OVERRIDE_FILE.write_text(json.dumps(self.editable()))
            except Exception:  # noqa: BLE001
                pass
        return applied

    def load_overrides(self) -> None:
        try:
            if _OVERRIDE_FILE.exists():
                self.apply_overrides(json.loads(_OVERRIDE_FILE.read_text()), persist=False)
        except Exception:  # noqa: BLE001
            pass

    def is_margin(self, symbol: str) -> bool:
        """信用取引(ロング+ショート)対象か。設定 かつ 取引所が信用対応 の銘柄のみ。"""
        return symbol in self.margin_symbols and symbol in self.margin_capable

    def effective_margin_symbols(self) -> list[str]:
        """実際に信用で動く銘柄（設定∩取引所対応）。"""
        return [s for s in self.margin_symbols if s in self.margin_capable]


settings = Settings()
settings.load_overrides()  # 前回のブラウザ調整を復元（再起動時。再デプロイでは消える）
