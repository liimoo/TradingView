"""株モメンタム監視（通知のみ）の頭脳部分のテスト。

ネットワークを使わない純粋関数（移動平均・上昇率・上位ランキング・月次通知文・ユニバース定義）を
検証する。CNBCからのデータ取得はネットワーク依存なのでここでは対象外。
"""
from __future__ import annotations

import pytest

from app.config import settings
from app import stocks


@pytest.fixture(autouse=True)
def _restore():
    orig = (settings.pz_sma_len, settings.stocks_mom_top, settings.stocks_mom_lookback)
    yield
    settings.pz_sma_len, settings.stocks_mom_top, settings.stocks_mom_lookback = orig


# ---- 指標（移動平均・上昇率・スナップショット） ----

def test_sma_at():
    closes = [1, 2, 3, 4, 5]
    assert stocks._sma_at(closes, 4, 3) == 4.0        # (3+4+5)/3
    assert stocks._sma_at(closes, 1, 3) is None       # 本数不足


def test_momentum_at():
    closes = [100, 110, 120, 130]
    assert stocks.momentum_at(closes, 3, 3) == pytest.approx(0.30)  # 130/100-1
    assert stocks.momentum_at(closes, 1, 3) is None                 # 本数不足


def test_snapshot_eligible_uptrend():
    settings.pz_sma_len = 3
    closes = [100, 90, 95, 130]  # 末尾130 > SMA3(=(90+95+130)/3=105) かつ 上昇率+30%
    s = stocks.snapshot(closes, 3, sma_len=3, look=3)
    assert s["above"] is True and s["eligible"] is True and s["mom"] == pytest.approx(0.30)


def test_snapshot_not_eligible_downtrend():
    settings.pz_sma_len = 3
    closes = [100, 100, 100, 80]  # 末尾80 < SMA3 かつ 下落
    s = stocks.snapshot(closes, 3, sma_len=3, look=3)
    assert s["above"] is False and s["eligible"] is False


# ---- 上位ランキング ----

def _row(ticker, mom, elig=True, prev_mom=None, prev_elig=False):
    return {"ticker": ticker, "name": ticker, "group": "x",
            "mom": mom, "eligible": elig, "prev_mom": prev_mom, "prev_eligible": prev_elig}


def test_top_momentum_orders_and_limits():
    rows = [_row("A", 0.1), _row("B", 0.5), _row("C", 0.3), _row("D", -0.2, elig=False)]
    settings.stocks_mom_top = 2
    top = stocks.top_momentum(rows, 2)
    assert [r["ticker"] for r in top] == ["B", "C"]     # 上昇率降順・eligibleのみ・上位2


def test_top_momentum_prev():
    rows = [_row("A", 0.1, prev_mom=0.9, prev_elig=True), _row("B", 0.5, prev_mom=0.2, prev_elig=True)]
    top_prev = stocks.top_momentum(rows, 2, prev=True)
    assert [r["ticker"] for r in top_prev] == ["A", "B"]  # 先月はA(0.9)が上


# ---- 月次通知文 ----

def test_digest_lists_top_and_changes():
    settings.stocks_mom_top = 2
    settings.stocks_mom_lookback = 126
    rows = [
        _row("7203.T", 0.40, prev_mom=0.10, prev_elig=True),   # 今月2位・先月も上位
        _row("6146.T", 0.80, prev_mom=None, prev_elig=False),  # 今月1位・先月圏外→IN
        _row("9984.T", 0.05, elig=True, prev_mom=0.50, prev_elig=True),  # 先月上位→今月圏外→OUT
    ]
    txt = stocks.build_digest(rows, "2026-08-20")
    assert "モメンタム・ランキング" in txt
    assert "1. 6146.T" in txt and "+80%" in txt           # 1位は最強
    assert "IN ➕" in txt and "6146.T" in txt               # 新規IN
    assert "OUT ➖" in txt and "9984.T" in txt              # 圏外OUT
    assert "監視 3銘柄" in txt


def test_digest_no_eligible():
    settings.stocks_mom_top = 3
    rows = [_row("A", -0.1, elig=False), _row("B", -0.2, elig=False)]
    txt = stocks.build_digest(rows, "2026-08-20")
    assert "該当なし" in txt and "現金推奨" in txt


def test_digest_counts_errors():
    settings.stocks_mom_top = 2
    rows = [_row("A", 0.3), {"ticker": "X", "name": "取得不能", "group": "x", "error": "404"}]
    txt = stocks.build_digest(rows, "2026-08-20")
    assert "監視 1銘柄" in txt and "取得失敗 1" in txt


# ---- データ整形・ユニバース定義 ----

def test_cnbc_symbol_conversion():
    assert stocks.cnbc_symbol("7203.T") == "7203.T-JP"
    assert stocks.cnbc_symbol("1321.T") == "1321.T-JP"
    assert stocks.cnbc_symbol("SPY") == "SPY"


def test_parse_cnbc():
    data = {"barData": {"priceBars": [
        {"close": "100.5"}, {"close": "101.0"}, {"close": ""}, {"close": None}, {"close": "abc"}, {"close": "102"},
    ]}}
    assert stocks._parse_cnbc(data) == [100.5, 101.0, 102.0]
    assert stocks._parse_cnbc({}) == []


def test_universe_wellformed():
    assert len(stocks.UNIVERSE) >= 20
    for t in stocks.UNIVERSE:
        assert len(t) == 3 and all(isinstance(x, str) and x for x in t)
    tickers = [t[0] for t in stocks.UNIVERSE]
    assert len(tickers) == len(set(tickers))          # 重複なし
    assert all(t.endswith(".T") for t in tickers)     # 日本株のみ
