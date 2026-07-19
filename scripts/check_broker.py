"""取引所への接続チェック / 極小テスト注文（Phase 2/3 の準備）。

.env の EXCHANGE_ID / 鍵 / TRADING_MODE を使って接続し、残高・価格の取得を確認する。
--live-order を付けると、本番と同じ発注ロジック（app/broker.py）で
最小サイズの 買い→売り を実際に出す（LIVE では実資金が動くので注意）。

使い方:
  source .venv/bin/activate
  python scripts/check_broker.py --symbol XRP/JPY                    # 接続・残高・価格の確認のみ
  python scripts/check_broker.py --live-order --symbol XRP/JPY --quote 300  # 実テスト注文(¥300)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# プロジェクトルートを import path に追加（scripts/ から単体実行できるように）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=(settings.allowed_symbols[0] if settings.allowed_symbols else "BTC/JPY"))
    parser.add_argument("--live-order", action="store_true", help="最小サイズで実際に買い→売りを試す")
    parser.add_argument("--quote", type=float, default=None, help="1回の発注額(quote建て)。未指定なら .env の ORDER_QUOTE_AMOUNT")
    args = parser.parse_args()

    quote = args.quote if args.quote is not None else settings.order_quote_amount
    print(f"mode={settings.trading_mode} exchange={settings.exchange_id} symbol={args.symbol} quote={quote}")
    if settings.trading_mode == "DRY_RUN":
        print("DRY_RUN では取引所に接続しません。.env を TESTNET/LIVE にして鍵を設定してください。")
        return 1

    import ccxt

    ex = getattr(ccxt, settings.exchange_id)(
        {"apiKey": settings.exchange_api_key, "secret": settings.exchange_api_secret, "enableRateLimit": True}
    )
    if settings.trading_mode == "TESTNET":
        ex.set_sandbox_mode(True)
    ex.load_markets()

    ticker = ex.fetch_ticker(args.symbol)
    print(f"価格 last={ticker['last']}")
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

    # 本番と同じ発注ロジックで 買い→売り（app/broker.py）
    from app.broker import broker

    px = float(ticker["last"])
    print(f"[live-order] 買い cost≈{quote} ...")
    buy = broker.buy(args.symbol, quote, px)
    print("  ", buy.get("summary"))
    filled = buy.get("filled_base")
    if not filled:
        print("約定数量が取得できず売りをスキップ。取引所の約定履歴を確認してください。")
        return 0
    time.sleep(2)
    print("[live-order] 売り（買った分を決済） ...")
    sell = broker.sell(args.symbol, filled, px)
    print("  ", sell.get("summary"))
    print("OK: テスト注文（買い→売り）完了。bitbankの約定履歴を確認してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
