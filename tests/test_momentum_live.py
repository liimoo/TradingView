"""本番モメンタム戦略の「判断」部分のテスト（発注は伴わない純粋関数）。

実発注(pz._execute)・データ取得はネットワーク/取引所依存なので対象外。
ここではモメンタム上位の選定と、保有→目標の差分（売る/買う）だけを検証する。
"""
from __future__ import annotations

import pytest

from app import momentum_live as ml


def test_momentum_targets_uptrend_only():
    # SMA5より上＆上昇の2銘柄を上昇率順に、下降の1銘柄は除外
    strong = [100] * 5 + [110, 130, 160, 200, 260]  # 上昇率大
    weak = [100] * 5 + [101, 102, 103, 104, 108]    # 上昇率小
    down = [200, 180, 160, 140, 120, 110, 100, 95, 90, 85]  # 下降トレンド→除外
    data = {"STRONG/JPY": strong, "WEAK/JPY": weak, "DOWN/JPY": down}
    tgt = ml.momentum_targets(data, top_n=5, look=3, sma_len=5)
    assert tgt == ["STRONG/JPY", "WEAK/JPY"]         # 上昇率降順・下降は除外


def test_momentum_targets_top_n_limit():
    data = {f"{c}/JPY": [100] * 5 + [100 + i for i in range(1, 6)]
            for c in ["A", "B", "C"]}
    # 全て同じ形なので順序は問わず、N=2で2つに絞られること
    tgt = ml.momentum_targets(data, top_n=2, look=3, sma_len=5)
    assert len(tgt) == 2


def test_momentum_targets_none_when_all_down():
    data = {"A/JPY": [200, 180, 160, 140, 120, 110, 100, 90, 80, 70]}
    assert ml.momentum_targets(data, top_n=5, look=3, sma_len=5) == []


def test_reconcile():
    held = ["BTC/JPY", "ETH/JPY", "SOL/JPY"]
    target = ["ETH/JPY", "SOL/JPY", "DOGE/JPY"]
    sells, buys = ml.reconcile(held, target)
    assert sells == ["BTC/JPY"]          # 目標から外れた保有→売る
    assert buys == ["DOGE/JPY"]          # 新しく目標入り→買う


def test_reconcile_no_change():
    held = ["BTC/JPY", "ETH/JPY"]
    sells, buys = ml.reconcile(held, ["ETH/JPY", "BTC/JPY"])
    assert sells == [] and buys == []    # 同じ顔ぶれなら売買なし


def test_rebalance_reason():
    H = {"ETH/JPY", "LTC/JPY"}
    # 地合い変化が最優先
    assert ml.rebalance_reason(H, set(), True, False, False) == "地合い悪化→退避"
    assert ml.rebalance_reason(set(), H, True, True, False) == "地合い回復→再開"
    # 顔ぶれが変われば即入替（新しい銘柄が目標入り/外れ）
    assert ml.rebalance_reason(H, {"ETH/JPY", "SOL/JPY"}, False, True, False) == "顔ぶれ変更→即入替"
    assert ml.rebalance_reason({"ETH/JPY"}, {"ETH/JPY", "SOL/JPY"}, False, True, False) == "顔ぶれ変更→即入替"
    # 顔ぶれ同じ・地合い不変 → 発火しない（値ブレだけでは売買しない）
    assert ml.rebalance_reason(H, {"LTC/JPY", "ETH/JPY"}, False, True, False) is None
    # 顔ぶれ同じでも月替わりなら保険で再調整
    assert ml.rebalance_reason(H, H, False, True, True) == "月次の再調整"
    # 地合い変化は顔ぶれ変化より優先
    assert ml.rebalance_reason(H, {"SOL/JPY"}, True, True, False) == "地合い回復→再開"


def test_plan_rebalance_full():
    # A=過大→削り, C=新規→買い, B=目標外→全売り, 目標1銘柄=80円
    plan = ml.plan_rebalance({"A/JPY": 100.0, "B/JPY": 100.0},
                             target=["A/JPY", "C/JPY"], target_quote=80.0, min_order=10.0)
    assert plan["sell_all"] == ["B/JPY"]
    assert plan["trim"] == [("A/JPY", 20.0)]         # 100→80 は 20円削る
    assert plan["buy"] == [("C/JPY", 80.0)]          # 0→80 は 80円買う


def test_plan_rebalance_ignores_small_diffs():
    # 目標80に対し 75/85 は min_order=10未満の差なので触らない
    plan = ml.plan_rebalance({"A/JPY": 75.0, "B/JPY": 85.0},
                             target=["A/JPY", "B/JPY"], target_quote=80.0, min_order=10.0)
    assert plan["sell_all"] == [] and plan["trim"] == [] and plan["buy"] == []


def test_plan_rebalance_new_only():
    plan = ml.plan_rebalance({}, target=["A/JPY", "B/JPY"], target_quote=100.0, min_order=10.0)
    assert plan["sell_all"] == []
    assert plan["buy"] == [("A/JPY", 100.0), ("B/JPY", 100.0)]


# ---- 地合いフィルター ----

def test_market_index_equal_weight():
    # 2銘柄が同率で2倍→指数も2倍(=期首1.0→2.0)
    data = {"A": [100, 150, 200], "B": [10, 15, 20]}
    idx = ml.market_index(data)
    assert idx[0] == pytest.approx(1.0) and idx[-1] == pytest.approx(2.0)


def test_regime_up_uptrend():
    # 上昇一貫→指数は自分のSMAより上→リスクオン(True)
    up = list(range(1, 60))  # 1..59 単調増加
    assert ml.regime_is_up({"A": up, "B": up}, sma_len=20) is True


def test_regime_down_downtrend():
    # 下降一貫→指数はSMAより下→リスクオフ(False)
    dn = list(range(60, 1, -1))  # 60..2 単調減少
    assert ml.regime_is_up({"A": dn, "B": dn}, sma_len=20) is False


def test_regime_insufficient_data_defaults_up():
    # 本数不足なら判定不能→True(通常運用・誤清算しない)
    assert ml.regime_is_up({"A": [1, 2, 3]}, sma_len=200) is True


# ---- 下火→現金退避 の end-to-end（発注はモック） ----

def test_rebalance_regime_down_sells_all_to_cash(monkeypatch):
    """地合いが弱気(指数が200日線↓)なら、目標を空にして全保有を売り現金化する。"""
    import asyncio
    from app.config import settings

    dn = list(range(300, 1, -1))  # 単調減少＝指数もSMA割れ→リスクオフ
    data = {"BTC/JPY": dn, "ETH/JPY": dn}

    monkeypatch.setattr(settings, "crypto_regime_filter", True)
    monkeypatch.setattr(settings, "pz_sma_len", 20)
    monkeypatch.setattr(settings, "crypto_mom_top", 5)
    monkeypatch.setattr(settings, "crypto_mom_lookback", 3)
    monkeypatch.setattr(settings, "order_size_pct", 0.19)
    monkeypatch.setattr(settings, "min_order_jpy", 100)

    monkeypatch.setattr(ml.risk_manager, "is_killed", lambda: False)
    monkeypatch.setattr(ml.risk_manager, "daily_block_reason", lambda *a, **k: None)
    monkeypatch.setattr(ml.risk_manager, "_positions", {})  # held_after 用（売却後は空想定）
    # 現在の建玉（時価）＝2銘柄保有中
    monkeypatch.setattr(ml, "_position_values",
                        lambda: {"BTC/JPY": 190000.0, "ETH/JPY": 190000.0})

    sold, bought = [], []

    async def fake_sell_all(sym):
        sold.append(sym)

    async def fake_trim(sym, q):  # noqa: ARG001
        pass

    async def fake_buy(sym, q):  # noqa: ARG001
        bought.append(sym)  # 下火では呼ばれてはいけない

    async def fake_notify(msg):  # noqa: ARG001
        pass

    monkeypatch.setattr(ml, "_sell_all", fake_sell_all)
    monkeypatch.setattr(ml, "_trim", fake_trim)
    monkeypatch.setattr(ml, "_buy", fake_buy)
    monkeypatch.setattr(ml, "notify", fake_notify)

    summary = asyncio.run(ml.rebalance(data))

    assert summary["regime_down"] is True          # 弱気判定
    assert summary["target"] == []                 # 目標＝現金（保有ゼロ）
    assert set(summary["sell_all"]) == {"BTC/JPY", "ETH/JPY"}
    assert set(sold) == {"BTC/JPY", "ETH/JPY"}     # 全保有を実際に売却
    assert bought == []                            # 買いは一切なし


def test_rebalance_skips_when_killed(monkeypatch):
    """キルスイッチON中はリバランスしない（売買ゼロ）。"""
    import asyncio
    monkeypatch.setattr(ml.risk_manager, "is_killed", lambda: True)

    async def fake_notify(msg):  # noqa: ARG001
        pass

    monkeypatch.setattr(ml, "notify", fake_notify)
    summary = asyncio.run(ml.rebalance({"BTC/JPY": [1, 2, 3]}))
    assert summary == {"skipped": "killed"}
