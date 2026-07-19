"""共有シークレットの照合（タイミング攻撃に配慮した定数時間比較）。"""
from __future__ import annotations

import hmac


def verify_secret(provided: str, expected: str) -> bool:
    if not expected:
        # サーバ側シークレット未設定は「不許可」（設定漏れで素通りさせない）
        return False
    return hmac.compare_digest(str(provided), str(expected))
