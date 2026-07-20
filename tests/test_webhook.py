"""ユニットテスト（DRY_RUN前提）。現物ロジック（買いエントリー/売り決済）を検証。"""
from __future__ import annotations

import json
import os

os.environ.setdefault("TRADING_MODE", "DRY_RUN")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("ALLOWED_SYMBOLS", "BTCUSDT,ETHUSDT,BTC/JPY,XRP/JPY")
os.environ.setdefault("SYMBOL_MAP", "XRPUSDT=XRP/JPY")
os.environ.setdefault("ORDER_COOLDOWN_SEC", "0")
os.environ.setdefault("MAX_OPEN_POSITIONS", "1")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.risk import risk_manager  # noqa: E402

client = TestClient(app)


def _payload(**over):
    base = {
        "secret": "test-secret",
        "action": "buy",
        "symbol": "BTCUSDT",
        "tf": "60",
        "price": 1000000,
        "rsi": 25.0,
        "bar_time": "t",
    }
    base.update(over)
    return json.dumps(base)


def setup_function(_):
    risk_manager.set_kill(False)
    risk_manager._positions.clear()
    risk_manager._last_order_ts.clear()


# ---- 認証・入力 ----
def test_secret_mismatch_returns_401():
    assert client.post("/webhook", content=_payload(secret="wrong")).status_code == 401


def test_invalid_payload_returns_422():
    assert client.post("/webhook", content='{"action":"buy"}').status_code == 422


def test_disallowed_symbol_skipped():
    r = client.post("/webhook", content=_payload(symbol="DOGEUSDT", bar_time="d1"))
    assert r.status_code == 200 and r.json()["status"] == "skipped"


def test_duplicate_bar_ignored():
    p = _payload(bar_time="dup1")
    assert client.post("/webhook", content=p).json()["status"] == "dry_run"
    assert client.post("/webhook", content=p).json()["status"] == "duplicate_ignored"


# ---- 現物ロジック ----
def test_buy_creates_position():
    r = client.post("/webhook", content=_payload(bar_time="b1"))
    assert r.json()["status"] == "dry_run"
    assert risk_manager.get_position("BTCUSDT") is not None  # 建玉が立つ


def test_buy_then_sell_closes_position():
    assert client.post("/webhook", content=_payload(action="buy", bar_time="c1")).json()["status"] == "dry_run"
    sell = client.post("/webhook", content=_payload(action="sell", bar_time="c2"))
    assert sell.json()["status"] == "dry_run"
    assert risk_manager.get_position("BTCUSDT") is None  # 決済されて建玉が消える


def test_symbol_mapping_buy():
    # TVの "XRPUSDT" が bitbankの "XRP/JPY" に変換されて建玉が立つ
    r = client.post("/webhook", content=_payload(symbol="XRPUSDT", bar_time="map1"))
    assert r.json()["status"] == "dry_run"
    assert risk_manager.get_position("XRP/JPY") is not None
    assert risk_manager.get_position("XRPUSDT") is None


def test_sell_without_position_skipped():
    r = client.post("/webhook", content=_payload(action="sell", bar_time="s1"))
    assert r.json()["status"] == "skipped"  # 現物は空売り不可


def test_second_buy_same_symbol_skipped():
    assert client.post("/webhook", content=_payload(bar_time="m1")).json()["status"] == "dry_run"
    assert client.post("/webhook", content=_payload(bar_time="m2")).json()["status"] == "skipped"  # 重ね買い禁止


def test_max_open_positions_blocks_other_symbol():
    assert client.post("/webhook", content=_payload(symbol="BTCUSDT", bar_time="x1")).json()["status"] == "dry_run"
    r = client.post("/webhook", content=_payload(symbol="ETHUSDT", bar_time="x2"))
    assert r.json()["status"] == "skipped"  # MAX_OPEN_POSITIONS=1 到達


# ---- キルスイッチ・ヘルス ----
def test_killswitch_blocks_order():
    risk_manager.set_kill(True)
    r = client.post("/webhook", content=_payload(bar_time="k1"))
    assert r.json()["status"] == "skipped"


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["mode"] == "DRY_RUN"


# ---- 取引レポート ----
def test_report_requires_secret():
    assert client.get("/report", params={"secret": "wrong"}).status_code == 401


def test_report_html_ok():
    client.post("/webhook", content=_payload(bar_time="rep1"))  # 記録を1件作る
    r = client.get("/report", params={"secret": "test-secret"})
    assert r.status_code == 200 and "取引レポート" in r.text


def test_report_json_ok():
    r = client.get("/report", params={"secret": "test-secret", "format": "json"})
    assert r.status_code == 200 and r.json()["mode"] == "DRY_RUN"
