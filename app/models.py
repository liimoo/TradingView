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
        # TVプレースホルダが置換されず空文字で来た場合に備える
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        return v
