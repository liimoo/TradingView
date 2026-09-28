"""app/equity.py の純粋関数テスト（NAV再構築）。"""
from datetime import datetime, timezone, timedelta

from app import equity

JST = timezone(timedelta(hours=9))


def _ts(date: str) -> float:
    """'YYYY-MM-DD' のJST正午のミリ秒。"""
    dt = datetime.strptime(date, "%Y-%m-%d").replace(hour=12, tzinfo=JST)
    return dt.timestamp() * 1000


def test_nav_series_chains_equal_weight_returns():
    dates = ["2026-01-01", "2026-01-02", "2026-01-03"]
    held = {"2026-01-01": ["A"], "2026-01-02": ["A"], "2026-01-03": ["A", "B"]}
    closes = {
        "A": {"2026-01-01": 100.0, "2026-01-02": 110.0, "2026-01-03": 121.0},
        "B": {"2026-01-02": 50.0, "2026-01-03": 55.0},
    }
    nav = equity.nav_series(dates, held, closes)
    assert [round(x["nav"], 2) for x in nav] == [100.0, 110.0, 121.0]  # +10%/日を連鎖


def test_nav_flat_when_cash():
    dates = ["2026-01-01", "2026-01-02"]
    held = {"2026-01-01": [], "2026-01-02": []}  # ずっと現金
    nav = equity.nav_series(dates, held, {})
    assert [x["nav"] for x in nav] == [100.0, 100.0]  # 据え置き


def test_nav_equal_weight_two_coins():
    dates = ["2026-01-01", "2026-01-02"]
    held = {"2026-01-01": ["A", "B"], "2026-01-02": ["A", "B"]}
    closes = {"A": {"2026-01-01": 100, "2026-01-02": 120},   # +20%
              "B": {"2026-01-01": 100, "2026-01-02": 100}}   # 0%
    nav = equity.nav_series(dates, held, closes)
    assert round(nav[1]["nav"], 3) == 110.0  # 平均+10%


def test_holdings_by_date_replays_trades():
    trades = {"A": [{"timestamp": _ts("2026-01-01"), "side": "buy", "amount": 1.0},
                    {"timestamp": _ts("2026-01-03"), "side": "sell", "amount": 1.0}]}
    dates = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
    closes = {"A": {d: 1000.0 for d in dates}}  # 端数除去に十分な価格
    held = equity.holdings_by_date(trades, dates, closes)
    assert held["2026-01-01"] == ["A"]
    assert held["2026-01-02"] == ["A"]
    assert held["2026-01-03"] == []   # 同日に売却→保有なし
    assert held["2026-01-04"] == []


def test_holdings_dust_excluded():
    trades = {"A": [{"timestamp": _ts("2026-01-01"), "side": "buy", "amount": 0.00001}]}
    dates = ["2026-01-01"]
    closes = {"A": {"2026-01-01": 100.0}}  # 0.00001*100 = ¥0.001 < ¥100 → 端数
    assert equity.holdings_by_date(trades, dates, closes)["2026-01-01"] == []


def test_normalize_index():
    dates = ["2026-01-01", "2026-01-02", "2026-01-03"]
    closes = {"2026-01-01": 100.0, "2026-01-02": 200.0, "2026-01-03": 150.0}
    assert equity.normalize_index(dates, closes) == [100.0, 200.0, 150.0]


def test_drawdowns():
    # 100→120(ピーク)→90 → 最大DD=(90/120-1)=-25%、現在DDも-25%（回復してない）
    d = equity.drawdowns([100, 110, 120, 100, 90])
    assert d["max_dd"] == -25.0 and d["cur_dd"] == -25.0
    # 最高値更新中なら現在DD=0
    d2 = equity.drawdowns([100, 90, 130])
    assert d2["cur_dd"] == 0.0 and d2["max_dd"] == -10.0
    assert equity.drawdowns([]) == {"max_dd": 0.0, "cur_dd": 0.0, "peak": None}


def test_build_curve_shape():
    trades = {"BTC/JPY": [{"timestamp": _ts("2026-01-01"), "side": "buy", "amount": 0.1}]}
    cal = ["2026-01-01", "2026-01-02"]
    closes = {"BTC/JPY": {"2026-01-01": 1_000_000.0, "2026-01-02": 1_100_000.0}}
    d = equity.build_curve(trades, closes, cal)
    assert d["dates"] == cal
    assert len(d["nav"]) == 2 and d["btc_index"][1] == 110.0
    assert d["held_latest"] == ["BTC/JPY"]
