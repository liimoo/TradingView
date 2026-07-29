"""パワーゾーン戦略（サーバ計算・日足・ロングのみ）の頭脳部分のテスト。

指標(SMA/RSI)・判断ロジック(decide)・シグナル評価(evaluate_signal)・データペア変換を検証。
これらが正しければ「バックテストと同じ判断が本番で出る」ことが担保される。
"""
from __future__ import annotations

import os

os.environ.setdefault("STRATEGY", "powerzones")

from app.config import settings  # noqa: E402
from app.indicators import rsi_wilder, sma  # noqa: E402
from app import powerzones as pz  # noqa: E402


# ---- 指標 ----

def test_sma_basic():
    vals = [1, 2, 3, 4, 5]
    out = sma(vals, 3)
    assert out[:2] == [None, None]
    assert out[2] == 2.0 and out[3] == 3.0 and out[4] == 4.0


def test_rsi_all_up_is_100():
    closes = [i for i in range(1, 20)]  # 単調増加
    r = rsi_wilder(closes, 4)
    assert r[-1] == 100.0


def test_rsi_all_down_is_0():
    closes = [i for i in range(20, 1, -1)]  # 単調減少
    r = rsi_wilder(closes, 4)
    assert r[-1] == 0.0


def test_rsi_matches_known_range():
    # 上下混在なら 0〜100 の中間
    closes = [10, 11, 10.5, 11.2, 10.8, 11.5, 11.0, 11.8]
    r = rsi_wilder(closes, 4)
    assert r[-1] is not None and 0 < r[-1] < 100


# ---- 判断ロジック（Larry Connorsのルール）----

def _set(entry=30, scale=25, exit_=55):
    settings.pz_entry, settings.pz_scale, settings.pz_exit = entry, scale, exit_


def test_decide_buy_needs_trend_and_oversold():
    _set()
    # 200日線より上 かつ RSI<30 → 買い
    assert pz.decide(close=110, sma200=100, rsi4=28, holding=False, already_scaled=False) == "buy"
    # RSI<30でも 200日線より下 なら買わない（トレンドフィルタ）
    assert pz.decide(close=90, sma200=100, rsi4=28, holding=False, already_scaled=False) == "hold"
    # 200日線より上でも RSIが高ければ買わない
    assert pz.decide(close=110, sma200=100, rsi4=40, holding=False, already_scaled=False) == "hold"


def test_decide_scale_only_once():
    _set()
    # 保有中 RSI<25 かつ 未買い増し → 買い増し
    assert pz.decide(close=95, sma200=100, rsi4=22, holding=True, already_scaled=False) == "scale"
    # 既に買い増し済みなら hold（1回まで）
    assert pz.decide(close=95, sma200=100, rsi4=22, holding=True, already_scaled=True) == "hold"


def test_decide_sell_on_exit():
    _set()
    # 保有中 RSI>55 → 利確売り（トレンド位置に関係なく）
    assert pz.decide(close=90, sma200=100, rsi4=60, holding=True, already_scaled=True) == "sell"


def test_decide_hold_when_holding_midrange():
    _set()
    assert pz.decide(close=105, sma200=100, rsi4=40, holding=True, already_scaled=False) == "hold"


def test_decide_none_inputs_hold():
    assert pz.decide(None, 100, 20, False, False) == "hold"
    assert pz.decide(100, None, 20, False, False) == "hold"
    assert pz.decide(100, 100, None, False, False) == "hold"


# ---- シグナル評価（終値リスト→最新足の指標）----

def test_evaluate_signal_last_bar():
    settings.pz_sma_len, settings.pz_rsi_len = 5, 4
    closes = [10, 11, 12, 11, 10, 9, 8, 9, 10, 11]
    sig = pz.evaluate_signal(closes)
    assert sig["close"] == closes[-1]
    assert sig["sma200"] is not None and sig["rsi4"] is not None


def test_evaluate_signal_insufficient():
    settings.pz_sma_len, settings.pz_rsi_len = 200, 4
    sig = pz.evaluate_signal([1, 2, 3])
    assert sig["sma200"] is None


# ---- データペア変換 ----

def test_data_pair_default_usdt():
    settings.pz_data_map = {}
    assert pz.data_pair("BTC/JPY") == "BTC/USDT"
    assert pz.data_pair("XRP/JPY") == "XRP/USDT"


def test_data_pair_map_override():
    settings.pz_data_map = {"BTC/JPY": "BTC/USD"}
    assert pz.data_pair("BTC/JPY") == "BTC/USD"
    settings.pz_data_map = {}
