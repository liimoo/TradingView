"""暗号資産モメンタムのペーパー検証（実発注なし）の頭脳部分のテスト。

ネットワークを使わない純粋関数（起点からの仮想資産シミュレーション・月次レポート文）を検証する。
"""
from __future__ import annotations

import pytest

from app.config import settings
from app import crypto_momentum as cp


@pytest.fixture(autouse=True)
def _restore():
    orig = (settings.crypto_paper_capital, settings.crypto_mom_top,
            settings.crypto_mom_lookback, settings.crypto_mom_rebal)
    yield
    (settings.crypto_paper_capital, settings.crypto_mom_top,
     settings.crypto_mom_lookback, settings.crypto_mom_rebal) = orig


def _series(base_ms, closes):
    """日足 (ts_list, closes) を作る。ts は1日=86400000ms刻み。"""
    ts = [base_ms + i * 86_400_000 for i in range(len(closes))]
    return ts, closes


def test_simulate_uptrend_gains():
    # 1銘柄・SMA5より上・上昇率プラス→保有し、その後さらに上昇→仮想資産が増える
    settings.crypto_mom_top = 1
    closes = [100] * 5 + [110, 120, 130, 140, 150]  # 上昇トレンド
    data = {"A/JPY": _series(0, closes)}
    start_ms = 0 + 7 * 86_400_000  # index7(=130)から開始
    res = cp.simulate(data, start_ms, 100000, top_n=1, look=3, rebal=30, sma_len=5)
    assert res["value"] > 100000            # 起点130→150で増える
    assert res["ret"] > 0
    assert res["holdings"] and res["holdings"][0]["sym"] == "A/JPY"


def test_simulate_downtrend_goes_cash():
    # 200日線ならぬSMA5を下回り下落中→eligibleなし→ノーポジ（現金）＝資産は元本のまま
    settings.crypto_mom_top = 1
    closes = [200, 190, 180, 170, 160, 150, 140, 130, 120, 110]  # 一貫下落
    data = {"A/JPY": _series(0, closes)}
    start_ms = 6 * 86_400_000
    res = cp.simulate(data, start_ms, 100000, top_n=1, look=3, rebal=30, sma_len=5)
    assert res["holdings"] == []
    assert res["value"] == pytest.approx(100000)   # 現金退避＝増減なし


def test_simulate_picks_top_n_by_momentum():
    settings.crypto_mom_top = 1
    up_fast = [100] * 5 + [110, 130, 160, 200, 260]   # 強い上昇
    up_slow = [100] * 5 + [101, 102, 103, 104, 105]   # 弱い上昇
    data = {"FAST/JPY": _series(0, up_fast), "SLOW/JPY": _series(0, up_slow)}
    start_ms = 7 * 86_400_000
    res = cp.simulate(data, start_ms, 100000, top_n=1, look=3, rebal=30, sma_len=5)
    assert [h["sym"] for h in res["holdings"]] == ["FAST/JPY"]   # 上昇率が高い方を選ぶ


def test_simulate_empty_universe():
    res = cp.simulate({}, 0, 100000, top_n=5, look=90, rebal=30)
    assert res["value"] == 100000 and res["holdings"] == [] and res["curve"] == []


def test_paper_digest_text():
    settings.crypto_paper_capital = 100000
    res = {"value": 123456, "ret": 0.23456, "bh_ret": 0.10,
           "holdings": [{"sym": "BTC/JPY", "weight": 0.5, "mom": 0.4},
                        {"sym": "ETH/JPY", "weight": 0.5, "mom": 0.3}]}
    txt = cp.build_paper_digest(res, universe_n=20, today="2026-08-20")
    assert "ペーパー検証" in txt
    assert "¥100,000 → ¥123,456" in txt and "+23.5%" in txt
    assert "BTC/JPY" in txt and "監視 20銘柄" in txt


def test_paper_digest_no_holdings():
    res = {"value": 100000, "ret": 0.0, "bh_ret": -0.2, "holdings": []}
    txt = cp.build_paper_digest(res, universe_n=20, today="2026-08-20")
    assert "ノーポジション" in txt and "現金退避" in txt
