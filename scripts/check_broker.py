"""取引所への接続チェック（Phase 2 の準備）。

.env の EXCHANGE_ID / 鍵 / TRADING_MODE を使って接続し、
残高・価格の取得を確認する。--live-order を付けると最小サイズで実際に
買い→売りのテスト注文を出す（TESTNET 推奨。LIVE では実資金が動くので注意）。

使い方:
  source .venv/bin/activate
  python scripts/check_broker.py                 # 接続・残高・価格の確認のみ
  python scripts/check_broker.py --symbol BTC/USDT
  python scripts/check_broker.py --live-order --symbol BTC/USDT   # 実テスト注文
"""
from __future__ import annotations

import argparse
import sys
import time

from app.config import settings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=(settings.allowed_symbols[0] if settings.allowed_symbols else "BTC/USDT"))
    parser.add_argument("--live-order", action="store_true", help="最小サイズで実際に買い→売りを試す")
    args = parser.parse_args()

    print(f"mode={settings.trading_mode} exchange={settings.exchange_id} symbol={args.symbol}")
    if settings.trading_mode == "DRY_RUN":
        print("DRY_RUN では取引所に接続しません。.env を TESTNET にして鍵を設定してください。")
        return 1

    import ccxt

    ex = getattr(ccxt, settings.exchange_id)(
        {"apiKey": settings.exchange_api_key, "secret": settings.exchange_api_secret, "enableRateLimit": True}
    )
    if settings.trading_mode == "TESTNET":
        ex.set_sandbox_mode(True)
    ex.load_markets()

    # 価格
    ticker = ex.fetch_ticker(args.symbol)
    print(f"価格 last={ticker['last']}")

    # 残高（認証確認）
    try:
        bal = ex.fetch_balance()
        free = {k: v for k, v in bal.get("free", {}).items() if v}
        print(f"残高(free)={free}")
    except Exception as exc:  # noqa: BLE001
        print(f"残高取得に失敗（鍵/権限を確認）: {type(exc).__name__}: {exc}")
        return 2

    if not args.live_order:
        print("OK: 接続と参照は成功。実注文を試すには --live-order を付けてください。")
        return 0

    # 実テスト注文：最小サイズで買い→少し待って売り
    quote = max(settings.order_quote_amount, 5)  # 取引所の最小コスト以上に
    print(f"[live-order] 成行買い cost≈{quote} ...")
    buy = ex.create_market_buy_order_with_cost(args.symbol, quote)
    filled = buy.get("filled") or buy.get("amount")
    print(f"  買い約定 id={buy.get('id')} filled={filled}")
    time.sleep(2)
    if filled:
        amount = float(ex.amount_to_precision(args.symbol, filled))
        print(f"[live-order] 成行売り amount={amount} ...")
        sell = ex.create_order(args.symbol, "market", "sell", amount, None, {})
        print(f"  売り約定 id={sell.get('id')} filled={sell.get('filled') or sell.get('amount')}")
    print("OK: テスト注文完了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
