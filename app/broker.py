"""発注ラッパ。ccxt 経由で取引所へ、または DRY_RUN で擬似発注。

現物ボット前提:
  - 買い: 金額(quote)指定でエントリー（createMarketBuyOrderWithCost を優先）
  - 売り: 保有している base 数量を成行で決済

将来の日本株対応（三菱UFJ eスマート kabuステーションAPI）は、
この Broker と同じ interface を持つ別実装を追加して差し替える。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .config import settings

logger = logging.getLogger("broker")


class OrderResult(dict):
    """発注結果。status/summary/filled_base を持つ。"""


class Broker:
    def __init__(self) -> None:
        self.mode = settings.trading_mode
        self._exchange = None
        if self.mode in {"TESTNET", "LIVE"}:
            self._exchange = self._build_exchange()

    # ---------- 取引所初期化 ----------
    def _build_exchange(self):
        import ccxt  # 遅延import（DRY_RUNではccxt無しでも動く）

        if not hasattr(ccxt, settings.exchange_id):
            raise ValueError(f"未知の EXCHANGE_ID: {settings.exchange_id}")
        exchange = getattr(ccxt, settings.exchange_id)(
            {
                "apiKey": settings.exchange_api_key,
                "secret": settings.exchange_api_secret,
                "enableRateLimit": True,
            }
        )
        if self.mode == "TESTNET":
            try:
                exchange.set_sandbox_mode(True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s はサンドボックス非対応の可能性: %s", settings.exchange_id, exc)
        exchange.load_markets()
        return exchange

    def _price_for(self, symbol: str, price: Optional[float]) -> float:
        px = price
        if (px is None or px <= 0) and self._exchange is not None:
            px = float(self._exchange.fetch_ticker(symbol)["last"])
        if not px or px <= 0:
            raise ValueError("価格が取得できず数量を計算できません")
        return px

    # ---------- 買い（金額指定でエントリー） ----------
    def buy(self, symbol: str, quote_amount: float, price: Optional[float]) -> OrderResult:
        if self.mode == "DRY_RUN" or self._exchange is None:
            px = price if (price and price > 0) else None
            filled = round(quote_amount / px, 10) if px else None
            summary = (
                f"[DRY_RUN] 本来発注: buy {symbol} 金額≈{quote_amount}"
                + (f" (≈{filled} base)" if filled else "")
            )
            logger.info(summary)
            return OrderResult(status="dry_run", summary=summary, filled_base=filled, order=None)

        ex = self._exchange
        if ex.has.get("createMarketBuyOrderWithCost"):
            order = ex.create_market_buy_order_with_cost(symbol, quote_amount)
        else:
            # cost指定非対応の取引所は base 数量に換算して成行買い
            amount = float(ex.amount_to_precision(symbol, quote_amount / self._price_for(symbol, price)))
            order = ex.create_order(symbol, "market", "buy", amount, None, {})
        filled = order.get("filled") or order.get("amount")
        summary = f"[{self.mode}] 買い成功: {symbol} cost≈{quote_amount} filled={filled} id={order.get('id')}"
        logger.info(summary)
        return OrderResult(status="ok", summary=summary, filled_base=filled, order=order)

    # ---------- 売り（保有 base を決済） ----------
    def sell(self, symbol: str, base_amount: float, price: Optional[float]) -> OrderResult:
        if self.mode == "DRY_RUN" or self._exchange is None:
            summary = f"[DRY_RUN] 本来発注: sell {symbol} 数量≈{base_amount} base（保有分を決済）"
            logger.info(summary)
            return OrderResult(status="dry_run", summary=summary, filled_base=base_amount, order=None)

        ex = self._exchange
        amount = float(ex.amount_to_precision(symbol, base_amount))
        order = ex.create_order(symbol, "market", "sell", amount, None, {})
        filled = order.get("filled") or order.get("amount")
        summary = f"[{self.mode}] 売り成功: {symbol} amount={amount} filled={filled} id={order.get('id')}"
        logger.info(summary)
        return OrderResult(status="ok", summary=summary, filled_base=filled, order=order)


broker = Broker()
