"""ユニットテスト（DRY_RUN前提）。現物ロジック（買いエントリー/売り決済）を検証。"""
from __future__ import annotations

import json
import os

os.environ.setdefault("TRADING_MODE", "DRY_RUN")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("ALLOWED_SYMBOLS", "BTCUSDT,ETHUSDT,BTC/JPY,XRP/JPY,SOL/JPY")
os.environ.setdefault("SYMBOL_MAP", "XRPUSDT=XRP/JPY,SOLUSDT=SOL/JPY")
os.environ.setdefault("MARGIN_SYMBOLS", "SOL/JPY")
os.environ.setdefault("MARGIN_CAPABLE", "SOL/JPY,XRP/JPY,ETH/JPY,BTC/JPY")  # テスト用にSOLも信用可扱い
os.environ.setdefault("WEBHOOK_SYNC", "true")  # テストは同期処理で応答内容を検証
os.environ.setdefault("ORDER_COOLDOWN_SEC", "0")
os.environ.setdefault("MAX_OPEN_POSITIONS", "1")
os.environ.setdefault("MAX_DAILY_LOSS_JPY", "2000")
os.environ.setdefault("MAX_DAILY_LOSS_PCT", "0.08")

from fastapi.testclient import TestClient  # noqa: E402

from datetime import datetime  # noqa: E402

from app.main import app  # noqa: E402
from app.monitor import should_stop, should_take_profit  # noqa: E402
from app.risk import JST, risk_manager, within_trading_hours  # noqa: E402

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
    risk_manager._roll_day()
    risk_manager._day_pnl = 0.0
    risk_manager._day_entries = 0


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


def test_buy_records_entry_price():
    client.post("/webhook", content=_payload(price=1000, bar_time="ep1"))
    pos = risk_manager.get_position("BTCUSDT")
    assert pos is not None and pos.entry_price == 1000  # 取得単価を記録


def test_should_stop():
    assert should_stop(100, 94, 0.05) is True   # -6% → 損切り
    assert should_stop(100, 96, 0.05) is False  # -4% → まだ
    assert should_stop(100, 50, 0) is False     # 0=無効
    assert should_stop(0, 50, 0.05) is False     # 取得単価不明


def test_should_take_profit():
    assert should_take_profit(100, 106, 0.05) is True   # +6% → 利確
    assert should_take_profit(100, 104, 0.05) is False  # +4% → まだ
    assert should_take_profit(100, 200, 0) is False     # 0=無効
    assert should_take_profit(0, 200, 0.05) is False     # 取得単価不明


def test_sized_quote():
    from app.config import sized_quote

    assert sized_quote(0.10, 50000, 45000, 500) == 5000   # 総資産の10%
    assert sized_quote(0.10, 50000, 3000, 500) == 3000    # 現金が10%未満→現金分だけ
    assert sized_quote(0, 50000, 45000, 500) == 500       # 無効→固定額


def test_within_trading_hours():
    assert within_trading_hours("", None) is True  # 空=常に可
    assert within_trading_hours("8-24", datetime(2026, 7, 20, 10, tzinfo=JST)) is True
    assert within_trading_hours("8-24", datetime(2026, 7, 20, 3, tzinfo=JST)) is False
    assert within_trading_hours("22-6", datetime(2026, 7, 20, 23, tzinfo=JST)) is True   # 日跨ぎ
    assert within_trading_hours("22-6", datetime(2026, 7, 20, 12, tzinfo=JST)) is False


def test_daily_loss_blocks_buy():
    risk_manager.record_close(-2500)  # 本日 -¥2500（固定上限¥2000超）
    r = client.post("/webhook", content=_payload(bar_time="dl1"))
    assert r.status_code == 200 and r.json()["status"] == "skipped"


def test_daily_loss_pct():
    # 総資産¥10,000 の8% = ¥800 が上限
    risk_manager.record_close(-900)
    assert risk_manager.daily_block_reason(10000) is not None   # -900 は超過→ブロック
    risk_manager._day_pnl = 0.0
    risk_manager.record_close(-500)
    assert risk_manager.daily_block_reason(10000) is None       # -500 は上限内→OK


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


# ---- パネル/ガイド/設定 ----
def test_guide_public():
    assert client.get("/guide").status_code == 200


def test_config_get_secret():
    assert client.get("/config").status_code == 401
    assert client.get("/config", params={"secret": "test-secret"}).status_code == 200


def test_config_post_applies_and_restores():
    from app.config import settings as _s

    old = _s.stop_loss_pct
    r = client.post("/config", content=json.dumps({"secret": "test-secret", "values": {"stop_loss_pct": "0.03"}}))
    assert r.status_code == 200 and _s.stop_loss_pct == 0.03
    _s.stop_loss_pct = old  # 後片付け


# ---- 信用取引（フリップ） ----
def test_margin_flip_long_then_short():
    r = client.post("/webhook", content=_payload(symbol="SOLUSDT", action="buy", bar_time="m1"))
    assert r.json()["status"] == "dry_run"
    pos = risk_manager.get_position("SOL/JPY")
    assert pos is not None and pos.side == "long"  # 買い→ロング建て
    r2 = client.post("/webhook", content=_payload(symbol="SOLUSDT", action="sell", bar_time="m2"))
    assert r2.json()["status"] == "dry_run"
    pos2 = risk_manager.get_position("SOL/JPY")
    assert pos2 is not None and pos2.side == "short"  # 売り→ショートへ反転


def test_margin_same_direction_skipped():
    client.post("/webhook", content=_payload(symbol="SOLUSDT", action="buy", bar_time="sd1"))
    r = client.post("/webhook", content=_payload(symbol="SOLUSDT", action="buy", bar_time="sd2"))
    assert r.json()["status"] == "skipped"  # 既にロングなので見送り


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


def test_roundtrips_fifo_pnl():
    from app.report import _build_roundtrips, _rt_summary

    trades = [
        {"side": "buy", "amount": 2, "price": 100, "timestamp": 1000},
        {"side": "sell", "amount": 2, "price": 110, "timestamp": 2000, "order": "o1"},
        {"side": "buy", "amount": 1, "price": 100, "timestamp": 3000},
        {"side": "sell", "amount": 1, "price": 90, "timestamp": 4000, "order": "o2"},
    ]
    rts, open_lots = _build_roundtrips(trades, {"o1": "take_profit"})
    assert len(rts) == 2 and not open_lots
    assert abs(rts[0]["pnl"] - 20) < 1e-9        # (110-100)*2
    assert rts[0]["reason"] == "take_profit"
    assert abs(rts[1]["pnl"] + 10) < 1e-9        # (90-100)*1 = -10
    s = _rt_summary(rts)
    assert s["count"] == 2 and s["wins"] == 1 and s["losses"] == 1
    assert abs(s["total_pnl"] - 10) < 1e-9


def test_roundtrips_entry_rsi():
    from app.report import _build_roundtrips

    trades = [
        {"side": "buy", "amount": 1, "price": 100, "timestamp": 1000, "order": "b1"},
        {"side": "sell", "amount": 1, "price": 105, "timestamp": 2000, "order": "s1"},
    ]
    rts, _ = _build_roundtrips(trades, {}, {"b1": 28.5})
    assert rts[0]["entry_rsi"] == 28.5


def test_roundtrips_open_lot():
    from app.report import _build_roundtrips

    # 買ったまま未決済なら往復は0件、open_lots=1
    trades = [{"side": "buy", "amount": 1, "price": 100, "timestamp": 1000}]
    rts, open_lots = _build_roundtrips(trades, {})
    assert rts == [] and len(open_lots) == 1
