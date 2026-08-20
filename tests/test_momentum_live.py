"""本番モメンタム戦略の「判断」部分のテスト（発注は伴わない純粋関数）。

実発注(pz._execute)・データ取得はネットワーク/取引所依存なので対象外。
ここではモメンタム上位の選定と、保有→目標の差分（売る/買う）だけを検証する。
"""
from __future__ import annotations

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
