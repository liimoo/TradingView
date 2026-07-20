"""取引集計レポート。取引所の約定履歴(fetch_my_trades)からP&Lを集計する。"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from . import journal
from .broker import broker
from .config import settings

logger = logging.getLogger("report")

JST = timezone(timedelta(hours=9))


def _fmt_ts(ms_or_s: float, is_ms: bool) -> str:
    try:
        sec = ms_or_s / 1000 if is_ms else ms_or_s
        return datetime.fromtimestamp(sec, JST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return str(ms_or_s)


def build_report() -> dict:
    """取引所の約定履歴から銘柄ごとの集計を作る。"""
    out: dict = {
        "mode": settings.trading_mode,
        "generated": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "balance": {},
        "symbols": {},
        "journal": journal.read_trades(50),
    }
    if not broker.has_exchange:
        out["note"] = "取引所へ接続できません（DRY_RUNで鍵未設定など）。独自ログ(journal)のみ表示。"
        return out

    try:
        bal = broker.balance()
        out["balance"] = {k: v for k, v in bal.get("free", {}).items() if v}
    except Exception as exc:  # noqa: BLE001
        out["balance_error"] = f"{type(exc).__name__}: {exc}"

    for sym in settings.allowed_symbols:
        try:
            trades = broker.my_trades(sym, limit=200)
        except Exception as exc:  # noqa: BLE001
            out["symbols"][sym] = {"error": f"{type(exc).__name__}: {exc}"}
            continue

        buy_base = buy_cost = sell_base = sell_cost = fee_jpy = 0.0
        rows = []
        for t in trades:
            side = t.get("side")
            amt = float(t.get("amount") or 0)
            cost = float(t.get("cost") or 0)
            fee = t.get("fee") or {}
            if side == "buy":
                buy_base += amt
                buy_cost += cost
            elif side == "sell":
                sell_base += amt
                sell_cost += cost
            if fee.get("currency") == "JPY":
                fee_jpy += float(fee.get("cost") or 0)
            rows.append(
                {
                    "time": _fmt_ts(t.get("timestamp"), True) if t.get("timestamp") else (t.get("datetime") or ""),
                    "side": side,
                    "price": t.get("price"),
                    "amount": amt,
                    "cost": cost,
                    "fee": fee.get("cost"),
                    "fee_ccy": fee.get("currency"),
                }
            )
        out["symbols"][sym] = {
            "trades": len(trades),
            "buy_base": buy_base,
            "buy_cost": buy_cost,
            "sell_base": sell_base,
            "sell_cost": sell_cost,
            "fee_jpy": fee_jpy,
            "net_jpy": sell_cost - buy_cost - fee_jpy,  # 概算の実現損益（フラット時）
            "net_base": buy_base - sell_base,  # 未決済の建玉(base)
            "rows": rows[-30:],
        }
    return out


def _yen(v) -> str:
    try:
        return f"¥{v:,.2f}"
    except Exception:  # noqa: BLE001
        return str(v)


def render_html(data: dict) -> str:
    esc = html.escape
    parts = [
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>取引レポート</title><style>",
        "body{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;margin:1.2rem;color:#111;background:#fafafa}",
        "h1{font-size:1.3rem}h2{font-size:1.05rem;margin-top:1.6rem}",
        "table{border-collapse:collapse;width:100%;margin:.4rem 0;font-size:.85rem;background:#fff}",
        "th,td{border:1px solid #ddd;padding:.35rem .5rem;text-align:right}th{background:#f0f0f0}",
        "td.l,th.l{text-align:left}.pos{color:#0a0}.neg{color:#c00}.muted{color:#888}",
        ".card{background:#fff;border:1px solid #e2e2e2;border-radius:8px;padding:.8rem 1rem;margin:.6rem 0}",
        "</style></head><body>",
        f"<h1>取引レポート <span class='muted'>({esc(data.get('mode',''))})</span></h1>",
        f"<p class='muted'>生成: {esc(data.get('generated',''))}</p>",
    ]
    if data.get("note"):
        parts.append(f"<div class='card'>{esc(data['note'])}</div>")

    bal = data.get("balance") or {}
    if bal:
        parts.append("<h2>残高</h2><table><tr><th class='l'>通貨</th><th>数量</th></tr>")
        for k, v in bal.items():
            parts.append(f"<tr><td class='l'>{esc(str(k))}</td><td>{v}</td></tr>")
        parts.append("</table>")
    if data.get("balance_error"):
        parts.append(f"<p class='neg'>残高取得エラー: {esc(data['balance_error'])}</p>")

    for sym, s in (data.get("symbols") or {}).items():
        parts.append(f"<h2>{esc(sym)}</h2>")
        if s.get("error"):
            parts.append(f"<p class='neg'>取得エラー: {esc(s['error'])}</p>")
            continue
        net = s["net_jpy"]
        cls = "pos" if net >= 0 else "neg"
        parts.append("<div class='card'>")
        parts.append(f"約定件数: <b>{s['trades']}</b>　")
        parts.append(f"買い: {_yen(s['buy_cost'])} ({s['buy_base']:.4f})　")
        parts.append(f"売り: {_yen(s['sell_cost'])} ({s['sell_base']:.4f})　")
        parts.append(f"手数料(JPY): {_yen(s['fee_jpy'])}<br>")
        parts.append(f"未決済建玉: <b>{s['net_base']:.4f}</b> base　")
        parts.append(f"純損益(概算): <b class='{cls}'>{_yen(net)}</b>")
        parts.append("</div>")
        rows = s.get("rows") or []
        if rows:
            parts.append("<table><tr><th class='l'>時刻(JST)</th><th>売買</th><th>価格</th><th>数量</th><th>金額</th><th>手数料</th></tr>")
            for r in reversed(rows):
                sc = "pos" if r["side"] == "sell" else "neg"
                parts.append(
                    f"<tr><td class='l'>{esc(str(r['time']))}</td>"
                    f"<td class='{sc}'>{esc(str(r['side']))}</td>"
                    f"<td>{r['price']}</td><td>{r['amount']}</td><td>{_yen(r['cost'])}</td>"
                    f"<td>{r['fee']} {esc(str(r['fee_ccy'] or ''))}</td></tr>"
                )
            parts.append("</table>")

    jr = data.get("journal") or []
    if jr:
        parts.append("<h2>サーバ側ログ(直近)</h2>")
        parts.append("<table><tr><th class='l'>時刻(JST)</th><th>売買</th><th class='l'>銘柄</th><th>価格</th><th>数量</th><th>RSI</th><th class='l'>状態</th></tr>")
        for e in reversed(jr):
            parts.append(
                f"<tr><td class='l'>{esc(_fmt_ts(e.get('ts',0), False))}</td>"
                f"<td>{esc(str(e.get('action','')))}</td>"
                f"<td class='l'>{esc(str(e.get('symbol','')))}</td>"
                f"<td>{e.get('price')}</td><td>{e.get('filled_base')}</td>"
                f"<td>{e.get('rsi')}</td><td class='l'>{esc(str(e.get('status','')))}</td></tr>"
            )
        parts.append("</table>")

    parts.append("</body></html>")
    return "".join(parts)
