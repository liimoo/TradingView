"""時間内リスク監視の判定（純粋関数 evaluate）テスト。"""
from app.risk_watch import evaluate

DROP = 0.08  # -8%で警告


def test_no_alert_when_rising_or_shallow():
    # 初回（peak確立・dd=0）→警告なし
    alert, peak, below, dd = evaluate(1_000_000, None, False, DROP)
    assert alert is False and peak == 1_000_000 and below is False and dd == 0.0
    # 最高値更新→警告なし・peak更新
    alert, peak, below, dd = evaluate(1_100_000, 1_000_000, False, DROP)
    assert alert is False and peak == 1_100_000 and dd == 0.0
    # -5%の浅い下げ（-8%未満）→警告なし
    alert, peak, below, dd = evaluate(950_000, 1_000_000, False, DROP)
    assert alert is False and round(dd, 4) == -0.05


def test_alert_on_breach_once():
    # -8.5%（閾値-8%を明確に超過）→警告、below=True
    alert, peak, below, dd = evaluate(915_000, 1_000_000, False, DROP)
    assert alert is True and below is True and round(dd, 3) == -0.085
    # さらに下げても連投しない（below=Trueのまま・alert=False）
    alert, peak, below, dd = evaluate(900_000, 1_000_000, True, DROP)
    assert alert is False and below is True


def test_rearm_after_recovery():
    # 半分(-4%)より上へ回復→再アーム(below=False)、警告はしない
    alert, peak, below, dd = evaluate(970_000, 1_000_000, True, DROP)
    assert alert is False and below is False
    # 再び-8%を明確に割れ→また警告
    alert, peak, below, dd = evaluate(915_000, 1_000_000, False, DROP)
    assert alert is True and below is True


def test_none_equity_is_safe():
    alert, peak, below, dd = evaluate(None, 1_000_000, False, DROP)
    assert alert is False and peak == 1_000_000 and dd is None
