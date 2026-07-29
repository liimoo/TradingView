"""パワーゾーン戦略（サーバ計算・日足・ロングのみ）の頭脳部分のテスト。

指標(SMA/RSI)・判断ロジック(decide)・シグナル評価(evaluate_signal)・データペア変換を検証。
これらが正しければ「バックテストと同じ判断が本番で出る」ことが担保される。
"""
from __future__ import annotations

import pytest

from app.config import settings  # noqa: E402
from app.indicators import rsi_wilder, sma  # noqa: E402
from app import powerzones as pz  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_settings():
    """各テストが settings/建玉 を書き換えても、他テストへ漏らさないよう元に戻す。"""
    orig = (list(settings.allowed_symbols), settings.pz_dynamic_universe,
            dict(settings.pz_data_map), settings.pz_sma_len, settings.pz_rsi_len,
            settings.pz_entry, settings.pz_scale, settings.pz_exit)
    yield
    from app.risk import risk_manager
    (settings.allowed_symbols, settings.pz_dynamic_universe, settings.pz_data_map,
     settings.pz_sma_len, settings.pz_rsi_len, settings.pz_entry,
     settings.pz_scale, settings.pz_exit) = orig
    risk_manager._positions.clear()


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


def test_parse_hours():
    from app.config import _parse_hours
    assert _parse_hours("9,21") == [9, 21]
    assert _parse_hours("21, 9, 9") == [9, 21]  # 重複除去・昇順
    assert _parse_hours("") == [9]              # 空は既定9
    assert _parse_hours("25,9") == [9]          # 範囲外は無視


def test_seconds_until_eval_multi():
    from app.config import settings
    from app import powerzones as pz
    settings.pz_eval_hours = [9, 21]
    s = pz._seconds_until_eval()
    assert 0 < s <= 24 * 3600  # 次の評価時刻まで24h以内
    settings.pz_eval_hours = [9]


def test_active_universe_static_mode():
    from app.config import settings
    from app import powerzones as pz
    from app.risk import risk_manager
    risk_manager._positions.clear()
    settings.pz_dynamic_universe = False
    settings.allowed_symbols = ["BTC/JPY", "ETH/JPY"]
    assert pz.active_universe() == ["BTC/JPY", "ETH/JPY"]


def test_active_universe_dynamic_includes_strong_and_held(monkeypatch):
    from app.config import settings
    from app import powerzones as pz, screener
    from app.risk import risk_manager
    risk_manager._positions.clear()
    settings.pz_dynamic_universe = True
    settings.allowed_symbols = ["BTC/JPY"]
    # スクリーニングは XLM/ADA を採用推奨(100%以上)とする
    monkeypatch.setattr(screener, "get_cached", lambda: {"recommend_a": ["XLM/JPY", "ADA/JPY"]})
    # SOL を保有中（100%割れでも対象に残るべき）
    risk_manager.open_position("SOL/JPY", 1.0, 100.0, side="long")
    uni = pz.active_universe()
    assert "XLM/JPY" in uni and "ADA/JPY" in uni  # 採用推奨
    assert "SOL/JPY" in uni                        # 保有中は残る
    assert "BTC/JPY" not in uni                    # 採用推奨でも保有でもない→対象外
    risk_manager._positions.clear()
    settings.pz_dynamic_universe = False


def test_active_universe_fallback_when_no_screening(monkeypatch):
    from app.config import settings
    from app import powerzones as pz, screener
    from app.risk import risk_manager
    risk_manager._positions.clear()
    settings.pz_dynamic_universe = True
    settings.allowed_symbols = ["BTC/JPY", "ETH/JPY"]
    monkeypatch.setattr(screener, "get_cached", lambda: {})  # 未集計
    uni = pz.active_universe()
    assert set(uni) == {"BTC/JPY", "ETH/JPY"}  # allowed_symbolsにフォールバック
    settings.pz_dynamic_universe = False


# ---- スクリーニングのハイブリッド分類（全期間100%以上＋直近2年） ----

def test_screener_classify_strong_when_recent_ok():
    from app import screener
    st = {"n": 40, "avg": 3.0, "total": 300.0, "worst": -25.0, "maxdd": -30.0}
    tier, _ = screener._classify(st, recent_total=50.0)  # 直近2年プラス
    assert tier == "strong"


def test_screener_classify_recent_bad_excluded():
    from app import screener
    # 全期間は+509%(優秀)でも直近2年がマイナスなら除外（DOGE型）
    st = {"n": 40, "avg": 10.0, "total": 509.0, "worst": -22.0, "maxdd": -49.0}
    tier, note = screener._classify(st, recent_total=-14.0)
    assert tier == "recent_bad" and "最近2年マイナス" in note


def test_screener_classify_weak_and_toxic():
    from app import screener
    assert screener._classify({"n": 40, "avg": 1.0, "total": 50.0, "worst": -20, "maxdd": -20}, 10.0)[0] == "ok"
    assert screener._classify({"n": 40, "avg": -2.0, "total": -60.0, "worst": -25, "maxdd": -68}, -30.0)[0] == "exclude"
    assert screener._classify({"n": 5, "avg": 5.0, "total": 100.0, "worst": -5, "maxdd": -5}, 50.0)[0] == "insufficient"


def test_screener_backtest_returns_ts_and_compound():
    from app import screener
    # i=2で c>SMA かつ RSI<30 → 買い、i=3で RSI>55 → 利確
    closes = [100, 90, 95, 100, 110]
    s = [None, None, 90, 92, 95]      # SMA(擬似)
    r = [None, 28, 28, 60, 40]        # RSI(擬似)
    ts = [1000, 2000, 3000, 4000, 5000]
    trades = screener._backtest(closes, s, r, ts)
    assert len(trades) == 1 and len(trades[0]) == 2 and trades[0][0] == 4000  # (決済ts, ret)
    assert screener._compound([0.1, 0.1]) > 20  # 複利で+21%
