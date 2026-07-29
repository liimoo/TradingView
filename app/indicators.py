"""テクニカル指標（純関数）。バックテストと本番で同一ロジックを使う。

- sma: 単純移動平均
- rsi_wilder: ワイルダー平滑のRSI（Larry Connorsのパワーゾーンは4期間RSIを使う）

いずれも「終値のリスト」を受け取り、各足に対応する値のリスト（不足期間は None）を返す。
"""
from __future__ import annotations


def sma(values: list[float], length: int) -> list[float | None]:
    """単純移動平均。i番目は values[i-length+1..i] の平均（不足は None）。"""
    out: list[float | None] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= length:
            s -= values[i - length]
        if i >= length - 1:
            out[i] = s / length
    return out


def rsi_wilder(closes: list[float], length: int) -> list[float | None]:
    """ワイルダー平滑のRSI（0〜100）。最初の length 本は None。"""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= length:
        return out
    gains = losses = 0.0
    for i in range(1, length + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain = gains / length
    avg_loss = losses / length
    out[length] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(length + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (length - 1) + max(d, 0.0)) / length
        avg_loss = (avg_loss * (length - 1) + max(-d, 0.0)) / length
        out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out
