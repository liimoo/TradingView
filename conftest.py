"""pytest 共通設定。

- プロジェクトルートを import path に含める（このファイルの存在で `from app.main import app` が解決）。
- テスト用の環境変数を、どのテストモジュールが最初に app を import しても
  同じ値になるよう、collection より前のここで確定させる（ファイル順依存の不具合を防ぐ）。
"""
import os

_TEST_ENV = {
    "TRADING_MODE": "DRY_RUN",
    "WEBHOOK_SECRET": "test-secret",
    "ALLOWED_SYMBOLS": "BTCUSDT,ETHUSDT,BTC/JPY,XRP/JPY,SOL/JPY",
    "SYMBOL_MAP": "XRPUSDT=XRP/JPY,SOLUSDT=SOL/JPY",
    "MARGIN_SYMBOLS": "SOL/JPY",
    "MARGIN_CAPABLE": "SOL/JPY,XRP/JPY,ETH/JPY,BTC/JPY",  # テスト用にSOLも信用可扱い
    "WEBHOOK_SYNC": "true",  # テストは同期処理で応答内容を検証
    "ORDER_COOLDOWN_SEC": "0",
    "MAX_OPEN_POSITIONS": "1",
    "MAX_DAILY_LOSS_JPY": "2000",
    "MAX_DAILY_LOSS_PCT": "0.08",
}
for _k, _v in _TEST_ENV.items():
    os.environ.setdefault(_k, _v)
