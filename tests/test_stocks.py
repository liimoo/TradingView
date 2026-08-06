"""株モニター（通知のみ）の頭脳部分のテスト。

ネットワークを使わない純粋関数（ゾーン判定・日次レポート文の組み立て・ユニバース定義）を検証する。
Yahooからのデータ取得はネットワーク依存なのでここでは対象外。
"""
from __future__ import annotations

import pytest

from app.config import settings
from app import stocks


@pytest.fixture(autouse=True)
def _restore():
    orig = (settings.pz_entry, settings.pz_exit, settings.stocks_near_rsi)
    yield
    settings.pz_entry, settings.pz_exit, settings.stocks_near_rsi = orig


# ---- ゾーン判定 ----

def test_signal_buy_zone():
    settings.pz_entry, settings.pz_exit, settings.stocks_near_rsi = 30, 55, 35
    z = stocks.signal_of(close=110, sma200=100, rsi4=28)  # 200日線上 & RSI<30
    assert z["above_sma"] and z["buy"] and not z["near"] and not z["exit_zone"]


def test_signal_near_zone():
    settings.pz_entry, settings.pz_exit, settings.stocks_near_rsi = 30, 55, 35
    z = stocks.signal_of(close=110, sma200=100, rsi4=33)  # 30〜35＝もうすぐ
    assert z["above_sma"] and not z["buy"] and z["near"]


def test_signal_below_sma_no_buy():
    settings.pz_entry, settings.pz_exit, settings.stocks_near_rsi = 30, 55, 35
    z = stocks.signal_of(close=90, sma200=100, rsi4=20)  # RSI低くても200日線下なら買わない
    assert z["above_sma"] is False and not z["buy"] and not z["near"]


def test_signal_exit_zone():
    settings.pz_entry, settings.pz_exit, settings.stocks_near_rsi = 30, 55, 35
    z = stocks.signal_of(close=120, sma200=100, rsi4=60)  # RSI>55＝利確ゾーン
    assert z["exit_zone"]


def test_signal_none_inputs():
    z = stocks.signal_of(None, 100, 20)
    assert z["above_sma"] is None and not z["buy"]


# ---- 日次レポート文 ----

def _row(ticker, name, rsi4, above=True, buy=False, near=False, exit_zone=False, error=None):
    r = {"ticker": ticker, "name": name, "group": "米ETF", "rsi4": rsi4,
         "above_sma": above, "buy": buy, "near": near, "exit_zone": exit_zone}
    if error:
        r["error"] = error
    return r


def test_digest_with_buy_signals():
    rows = [
        _row("SPY", "S&P500", 28, buy=True),
        _row("QQQ", "Nasdaq100", 33, near=True),
        _row("DIA", "NYダウ", 45),
    ]
    txt = stocks.build_digest(rows, "2026-08-07")
    assert "買いゾーン 1件" in txt
    assert "SPY" in txt and "RSI28" in txt
    assert "もうすぐ買い" in txt and "QQQ" in txt
    assert "監視 3銘柄" in txt


def test_digest_no_buy_shows_closest():
    # 買いゼロなら「最も近い」を出す（生存＋状況の通知になる）
    rows = [
        _row("SPY", "S&P500", 48),
        _row("QQQ", "Nasdaq100", 40),  # 200日線上で最小RSI→最も近い
    ]
    txt = stocks.build_digest(rows, "2026-08-07")
    assert "買いシグナルなし" in txt
    assert "最も近い: QQQ" in txt and "RSI40" in txt


def test_digest_counts_errors():
    rows = [
        _row("SPY", "S&P500", 28, buy=True),
        _row("XXX", "取得不能", None, error="404"),
    ]
    txt = stocks.build_digest(rows, "2026-08-07")
    assert "監視 1銘柄" in txt and "取得失敗 1" in txt


def test_digest_exit_zone_listed():
    rows = [_row("GLD", "金", 60, exit_zone=True)]
    txt = stocks.build_digest(rows, "2026-08-07")
    assert "利確ゾーン" in txt and "GLD" in txt


# ---- ユニバース定義 ----

def test_universe_wellformed():
    assert len(stocks.UNIVERSE) >= 20
    for t in stocks.UNIVERSE:
        assert len(t) == 3 and all(isinstance(x, str) and x for x in t)
    tickers = [t[0] for t in stocks.UNIVERSE]
    assert len(tickers) == len(set(tickers))  # 重複なし
    assert any(t.endswith(".T") for t in tickers)  # 日本株を含む
