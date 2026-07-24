"""厚めの追加テスト（DRY_RUN前提）。

安全装置（信用の損切り/利確判定）・入力バリデーション・設定ヘルパ・
損益集計・エンドポイントの網羅を、既存の test_webhook.py に上乗せする。
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("TRADING_MODE", "DRY_RUN")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("ALLOWED_SYMBOLS", "BTCUSDT,ETHUSDT,BTC/JPY,XRP/JPY,SOL/JPY")
os.environ.setdefault("SYMBOL_MAP", "XRPUSDT=XRP/JPY,SOLUSDT=SOL/JPY")
os.environ.setdefault("MARGIN_SYMBOLS", "SOL/JPY")
os.environ.setdefault("MARGIN_CAPABLE", "SOL/JPY,XRP/JPY,ETH/JPY,BTC/JPY")
os.environ.setdefault("WEBHOOK_SYNC", "true")
os.environ.setdefault("ORDER_COOLDOWN_SEC", "0")
os.environ.setdefault("MAX_OPEN_POSITIONS", "1")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)
SECRET = "test-secret"


# ======================================================================
# 安全装置：信用の損切り/利確 判定（ロング/ショート両方）
# ======================================================================

def test_margin_exit_long_stop():
    from app.monitor import margin_exit_decision

    # ロング: -5%到達で損切り
    d = margin_exit_decision("long", entry=100.0, px=95.0, sl=0.05, tp=0.05)
    assert d and d[0] == "stop_loss"


def test_margin_exit_long_take_profit():
    from app.monitor import margin_exit_decision

    # ロング: +5%到達で利確
    d = margin_exit_decision("long", entry=100.0, px=105.0, sl=0.05, tp=0.05)
    assert d and d[0] == "take_profit"


def test_margin_exit_long_hold():
    from app.monitor import margin_exit_decision

    # ロング: ±2%は何もしない
    assert margin_exit_decision("long", 100.0, 102.0, 0.05, 0.05) is None
    assert margin_exit_decision("long", 100.0, 98.0, 0.05, 0.05) is None


def test_margin_exit_short_stop_is_price_up():
    from app.monitor import margin_exit_decision

    # ショート: 値上がり+5%が損切り（ロングと逆）
    d = margin_exit_decision("short", entry=100.0, px=105.0, sl=0.05, tp=0.05)
    assert d and d[0] == "stop_loss"


def test_margin_exit_short_take_profit_is_price_down():
    from app.monitor import margin_exit_decision

    # ショート: 値下がり-5%が利確
    d = margin_exit_decision("short", entry=100.0, px=95.0, sl=0.05, tp=0.05)
    assert d and d[0] == "take_profit"


def test_margin_exit_short_hold():
    from app.monitor import margin_exit_decision

    assert margin_exit_decision("short", 100.0, 102.0, 0.05, 0.05) is None
    assert margin_exit_decision("short", 100.0, 98.0, 0.05, 0.05) is None


def test_margin_exit_disabled_when_pct_zero():
    from app.monitor import margin_exit_decision

    # sl=tp=0 なら常に None（安全装置OFF時に誤発火しない）
    assert margin_exit_decision("long", 100.0, 50.0, 0.0, 0.0) is None
    assert margin_exit_decision("short", 100.0, 200.0, 0.0, 0.0) is None


def test_margin_exit_bad_inputs():
    from app.monitor import margin_exit_decision

    assert margin_exit_decision("long", 0.0, 100.0, 0.05, 0.05) is None
    assert margin_exit_decision("long", 100.0, 0.0, 0.05, 0.05) is None


def test_should_stop_boundaries():
    from app.monitor import should_stop, should_take_profit

    # 境界値ちょうど（<=, >=）で発火する
    assert should_stop(100.0, 95.0, 0.05) is True
    assert should_stop(100.0, 95.01, 0.05) is False
    assert should_take_profit(100.0, 105.0, 0.05) is True
    assert should_take_profit(100.0, 104.99, 0.05) is False
    # 無効入力は False
    assert should_stop(0.0, 95.0, 0.05) is False
    assert should_take_profit(100.0, 105.0, 0.0) is False


# ======================================================================
# 入力バリデーション（{{plot_0}}等が未変換でも取引を止めない堅牢化）
# ======================================================================

def test_signal_nonnumeric_rsi_becomes_none():
    from app.models import Signal

    s = Signal(secret="x", action="sell", symbol="XRPUSDT", price="{{close}}", rsi="{{plot_0}}")
    assert s.price is None and s.rsi is None


def test_signal_numeric_strings_parsed():
    from app.models import Signal

    s = Signal(secret="x", action="buy", symbol="XRPUSDT", price="184.9", rsi="68.53")
    assert s.price == 184.9 and s.rsi == 68.53


def test_signal_comma_number_parsed():
    from app.models import Signal

    # 桁区切りカンマ入りでも数値化できる
    s = Signal(secret="x", action="buy", symbol="BTCUSDT", price="10,858,627")
    assert s.price == 10858627.0


def test_signal_symbol_uppercased():
    from app.models import Signal

    s = Signal(secret="x", action="buy", symbol=" xrpusdt ")
    assert s.symbol == "XRPUSDT"


def test_signal_bad_action_rejected():
    import pytest
    from pydantic import ValidationError
    from app.models import Signal

    with pytest.raises(ValidationError):
        Signal(secret="x", action="hold", symbol="BTCUSDT")


# ======================================================================
# 設定ヘルパ（変換・信用判定・サイズ計算・引用符除去）
# ======================================================================

def test_split_symbols_strips_quotes_and_spaces():
    from app.config import _split_symbols

    assert _split_symbols(' "BTC/JPY", ETH/JPY ,') == ["BTC/JPY", "ETH/JPY"]
    assert _split_symbols("") == []


def test_resolve_symbol():
    from app.config import settings

    assert settings.resolve_symbol("XRPUSDT") == "XRP/JPY"  # マップ変換
    assert settings.resolve_symbol("unknownusdt") == "UNKNOWNUSDT"  # 未登録は大文字化


def test_is_margin_requires_both_lists():
    from app.config import settings

    # SOL/JPY は margin_symbols かつ margin_capable → 信用対象
    assert settings.is_margin("SOL/JPY") is True
    # BTC/JPY は capable だが margin_symbols に無い → 現物扱い
    assert settings.is_margin("BTC/JPY") is False


def test_effective_margin_symbols_is_intersection():
    from app.config import settings

    eff = settings.effective_margin_symbols()
    assert "SOL/JPY" in eff
    assert all(s in settings.margin_capable for s in eff)


def test_sized_quote_variants():
    from app.config import sized_quote

    # pct基準: min(総資産×pct, 使える現金)
    assert sized_quote(0.10, 1_000_000, 500_000, 1000) == 100_000
    # 現金が足りなければ現金でキャップ
    assert sized_quote(0.10, 1_000_000, 30_000, 1000) == 30_000
    # pct=0 は固定額
    assert sized_quote(0.0, 1_000_000, 500_000, 1000) == 1000
    # 負にならない
    assert sized_quote(0.10, 1_000_000, -5, 1000) == 0.0


# ======================================================================
# 損益集計（部分約定・複数lot・エラー銘柄のCSV）
# ======================================================================

def test_realized_events_partial_fills():
    from app.report import _realized_events

    # 100で2枚買い、120で1枚・130で1枚売り = (120-100)+(130-100)=50
    trades = [
        {"side": "buy", "amount": 2, "price": 100, "timestamp": 1000},
        {"side": "sell", "amount": 1, "price": 120, "timestamp": 2000},
        {"side": "sell", "amount": 1, "price": 130, "timestamp": 3000},
    ]
    assert round(sum(e["pnl"] for e in _realized_events(trades)), 6) == 50.0


def test_roundtrips_partial_fill_split():
    from app.report import _build_roundtrips

    # 1回の売りが2つの買いlotに分割されて2往復になる
    trades = [
        {"side": "buy", "amount": 1, "price": 100, "timestamp": 1000},
        {"side": "buy", "amount": 1, "price": 110, "timestamp": 1500},
        {"side": "sell", "amount": 2, "price": 120, "timestamp": 2000},
    ]
    rts, open_lots = _build_roundtrips(trades, {})
    assert len(rts) == 2 and open_lots == []
    assert round(sum(r["pnl"] for r in rts), 6) == round((120 - 100) + (120 - 110), 6)
    assert all(r["side"] == "long" for r in rts)


def test_roundtrips_ignores_zero_and_negative():
    from app.report import _build_roundtrips

    trades = [
        {"side": "buy", "amount": 0, "price": 100, "timestamp": 1000},
        {"side": "buy", "amount": 1, "price": 0, "timestamp": 1100},
        {"side": "buy", "amount": 1, "price": 100, "timestamp": 1200},
    ]
    rts, open_lots = _build_roundtrips(trades, {})
    # 有効な買いは1件だけ→未決済lot1、往復0
    assert rts == [] and len(open_lots) == 1


def test_tax_csv_with_error_symbol():
    from app.report import build_tax_csv

    data = {
        "symbols": {
            "XRP/JPY": {"realized": 100.0, "fee": 10.0, "closes": 3, "trades": 6},
            "BTC/JPY": {"error": "boom"},
        },
        "total_realized": 100.0,
        "total_fee": 10.0,
        "closes": 3,
    }
    csv = build_tax_csv(data)
    assert "XRP/JPY,100.00,10.00,90.00,3,6" in csv
    assert "BTC/JPY,ERROR" in csv
    assert "TOTAL,100.00,10.00,90.00,3," in csv


def test_rt_summary_counts():
    from app.report import _rt_summary

    rts = [{"pnl": 10}, {"pnl": -5}, {"pnl": 20}]
    s = _rt_summary(rts)
    assert s["count"] == 3 and s["wins"] == 2 and s["losses"] == 1
    assert round(s["total_pnl"], 6) == 25.0


# ======================================================================
# エンドポイント（/tax の各形式・認証、/positions JSON）
# ======================================================================

def test_tax_requires_secret():
    assert client.get("/tax").status_code == 401


def test_tax_json_shape():
    r = client.get(f"/tax?secret={SECRET}&format=json")
    assert r.status_code == 200
    d = r.json()
    assert "year" in d and "total_realized" in d and "symbols" in d


def test_tax_csv_download():
    r = client.get(f"/tax?secret={SECRET}&format=csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
    assert r.text.splitlines()[0].startswith("symbol,realized_pnl_jpy")


def test_tax_html_has_disclaimer():
    r = client.get(f"/tax?secret={SECRET}")
    assert r.status_code == 200
    # 免責と税の基礎情報が載っている
    assert "確定申告そのものには使えません" in r.text
    assert "雑所得" in r.text


def test_positions_json_shape():
    r = client.get(f"/positions?secret={SECRET}&format=json")
    assert r.status_code == 200
    assert "tracked" in r.json()


def test_health_reports_margin_lists():
    d = client.get("/health").json()
    assert "margin_active" in d and "allowed_symbols" in d
