"""発注ラッパ。ccxt 経由で取引所へ、または DRY_RUN で擬似発注。

現物ボット前提:
  - 買い: 金額(quote)指定でエントリー
  - 売り: 保有している base 数量を成行で決済

取引所への全アクセスは self._lock で直列化する（bitbank等は nonce の増加が必要で、
webhook発注・監視ループ・レポートが同時に叩くと衝突するため）。

将来の日本株対応（kabuステーションAPI）は、同じ interface の別実装を追加して差し替える。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from .config import settings

logger = logging.getLogger("broker")


class OrderResult(dict):
    """発注結果。status/summary/filled_base/filled_price を持つ。"""


class Broker:
    def __init__(self) -> None:
        self.mode = settings.trading_mode
        self._lock = threading.Lock()
        self._exchange = None
        # LIVE/TESTNET は発注に必須。DRY_RUNでも鍵があれば参照(残高/約定/価格)用に構築。
        if self.mode in {"TESTNET", "LIVE"} or (settings.exchange_api_key and settings.exchange_api_secret):
            try:
                self._exchange = self._build_exchange()
            except Exception as exc:  # noqa: BLE001
                logger.warning("取引所の初期化に失敗: %s", exc)

    @property
    def has_exchange(self) -> bool:
        return self._exchange is not None

    # ---------- 取引所初期化 ----------
    def _build_exchange(self):
        import ccxt

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

    # ---------- 参照系（すべてロックで直列化） ----------
    def ticker(self, symbol: str) -> float:
        with self._lock:
            return float(self._exchange.fetch_ticker(symbol)["last"])

    def balance(self) -> dict:
        with self._lock:
            return self._exchange.fetch_balance()

    def my_trades(self, symbol: str, limit: int = 200) -> list:
        with self._lock:
            return self._exchange.fetch_my_trades(symbol, limit=limit)

    # ---------- 逆指値（stop）・注文管理 ----------
    def place_stop_sell(self, symbol: str, base_amount: float, trigger_price: float) -> dict:
        """トリガー価格に達したら成行売りする逆指値注文を置く（bitbank: type='stop'）。"""
        ex = self._exchange
        with self._lock:
            trig = float(ex.price_to_precision(symbol, trigger_price))
            order = ex.create_order(symbol, "stop", "sell", base_amount, None, {"trigger_price": trig})
        logger.info("逆指値set: %s sell stop trigger=%s id=%s", symbol, trig, order.get("id"))
        return order

    def cancel(self, symbol: str, order_id) -> None:
        with self._lock:
            self._exchange.cancel_order(order_id, symbol)

    def fetch_order(self, symbol: str, order_id) -> dict:
        with self._lock:
            return self._exchange.fetch_order(order_id, symbol)

    def open_orders(self, symbol: str) -> list:
        with self._lock:
            return self._exchange.fetch_open_orders(symbol)

    def market_min_amount(self, symbol: str) -> float:
        try:
            return float(self._exchange.market(symbol).get("limits", {}).get("amount", {}).get("min") or 0)
        except Exception:  # noqa: BLE001
            return 0.0

    def _price_for(self, symbol: str, price: Optional[float]) -> float:
        px = price
        if (px is None or px <= 0) and self._exchange is not None:
            px = self.ticker(symbol)
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
            return OrderResult(status="dry_run", summary=summary, filled_base=filled, filled_price=px, order=None)

        ex = self._exchange
        with self._lock:
            if ex.has.get("createMarketBuyOrderWithCost"):
                order = ex.create_market_buy_order_with_cost(symbol, quote_amount)
            else:
                px = self._price_for(symbol, price)
                amount = float(ex.amount_to_precision(symbol, quote_amount / px))
                order = ex.create_order(symbol, "market", "buy", amount, None, {})
        filled = order.get("filled") or order.get("amount")
        filled_price = order.get("average") or order.get("price") or (price if price and price > 0 else None)
        summary = f"[{self.mode}] 買い成功: {symbol} cost≈{quote_amount} filled={filled}@{filled_price} id={order.get('id')}"
        logger.info(summary)
        return OrderResult(status="ok", summary=summary, filled_base=filled, filled_price=filled_price, order=order)

    # ---------- 売り（保有 base を決済） ----------
    def sell(self, symbol: str, base_amount: float, price: Optional[float]) -> OrderResult:
        if self.mode == "DRY_RUN" or self._exchange is None:
            summary = f"[DRY_RUN] 本来発注: sell {symbol} 数量≈{base_amount} base（保有分を決済）"
            logger.info(summary)
            return OrderResult(status="dry_run", summary=summary, filled_base=base_amount, filled_price=price, order=None)

        ex = self._exchange
        with self._lock:
            amount = float(ex.amount_to_precision(symbol, base_amount))
            order = ex.create_order(symbol, "market", "sell", amount, None, {})
        filled = order.get("filled") or order.get("amount")
        summary = f"[{self.mode}] 売り成功: {symbol} amount={amount} filled={filled} id={order.get('id')}"
        logger.info(summary)
        return OrderResult(status="ok", summary=summary, filled_base=filled, filled_price=order.get("average"), order=order)


broker = Broker()
