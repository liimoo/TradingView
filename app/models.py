"""TradingView Webhook のペイロードスキーマ。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class Signal(BaseModel):
    """TradingView のアラートメッセージ(JSON)。

    Pine のアラートに貼る例:
      {"secret":"...","action":"buy","symbol":"BTC/JPY","tf":"{{interval}}",
       "price":"{{close}}","rsi":"{{plot_0}}","bar_time":"{{timenow}}"}
    """

    secret: str
    action: Literal["buy", "sell"]
    symbol: str
    tf: Optional[str] = None
    price: Optional[float] = None
    rsi: Optional[float] = None
    # 同一足の二重POST排除に使う識別子（TVの {{timenow}} など）。無くても可。
    bar_time: Optional[str] = Field(default=None)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("price", "rsi", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        # TVプレースホルダ({{plot_0}}等)が置換されず、空文字や非数値のまま
        # 来た場合に備える。数値化できなければ None にして取引自体は通す
        # （rsi/priceはおまけ情報。これのせいでWebhookを弾かないため）。
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip().replace(",", "")
            if s == "":
                return None
            try:
                return float(s)
            except ValueError:
                return None
        return v
