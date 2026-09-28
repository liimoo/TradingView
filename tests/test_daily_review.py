"""日次リスクレビューの文面組み立て（純粋関数）のテスト。"""
from app.daily_review import compose_review

MONEY = {"equity": 1_890_000, "cash": 95_000, "book": 1_790_000, "upnl": -12_000}


def test_normal_no_warning():
    # 最高値圏(現在DD 0)・地合いオン → 警告なし
    text, warn = compose_review([100, 105, 110], regime_up=True, dist="17.2",
                                money=MONEY, dd_alert_pct=0.10)
    assert warn is False
    assert text.startswith("💰 日次レビュー")
    assert "総資産 ¥1,890,000" in text and "🟢 オン" in text


def test_drawdown_triggers_warning():
    # 110→95 は -13.6% で -10%基準を超える → ⚠️
    text, warn = compose_review([100, 110, 95], regime_up=True, dist="5",
                                money=MONEY, dd_alert_pct=0.10)
    assert warn is True
    assert text.startswith("⚠️ リスク警告")
    assert "警戒水準" in text


def test_regime_off_triggers_warning():
    text, warn = compose_review([100, 101], regime_up=False, dist="-3",
                                money=MONEY, dd_alert_pct=0.10)
    assert warn is True
    assert "🔴 オフ" in text and "現金退避中" in text


def test_regime_unknown_no_warning():
    # 地合い不明(None)＝バックフィル部分など → 警告扱いにしない
    _, warn = compose_review([100, 101], regime_up=None, dist="",
                             money=MONEY, dd_alert_pct=0.10)
    assert warn is False
