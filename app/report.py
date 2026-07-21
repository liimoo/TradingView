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


def build_positions() -> dict:
    """現在の建玉・含み損益・証拠金・残高をまとめる。"""
    from .risk import risk_manager

    out: dict = {
        "mode": settings.trading_mode,
        "generated": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "positions": [],
        "balance": {},
        "margin_status": {},
    }
    for sym, p in list(risk_manager._positions.items()):
        px = None
        if broker.has_exchange:
            try:
                px = broker.ticker(sym)
            except Exception:  # noqa: BLE001
                px = None
        upnl = None
        if px and p.entry_price:
            sign = 1 if p.side == "long" else -1
            upnl = sign * (px - p.entry_price) * (p.base_qty or 0)
        out["positions"].append(
            {"symbol": sym, "side": p.side, "base": p.base_qty, "entry": p.entry_price, "price": px, "upnl": upnl}
        )
    if broker.has_exchange:
        try:
            bal = broker.balance()
            out["balance"] = {k: v for k, v in bal.get("free", {}).items() if v}
        except Exception as exc:  # noqa: BLE001
            out["balance_error"] = str(exc)
        try:
            out["margin_status"] = broker.margin_status()
        except Exception:  # noqa: BLE001
            pass
    return out


_POS_STYLE = (
    "body{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;margin:1.2rem;color:#111;background:#fafafa}"
    "h1{font-size:1.3rem}h2{font-size:1.05rem;margin-top:1.4rem}"
    "table{border-collapse:collapse;width:100%;margin:.4rem 0;font-size:.9rem;background:#fff}"
    "th,td{border:1px solid #ddd;padding:.4rem .55rem;text-align:right}th{background:#f0f0f0}"
    "td.l,th.l{text-align:left}.pos{color:#0a8f3c;font-weight:bold}.neg{color:#d33;font-weight:bold}.muted{color:#888}"
)


def render_positions_html(data: dict) -> str:
    esc = html.escape
    parts = [
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>建玉状況</title><style>{_POS_STYLE}</style></head><body>",
        f"<h1>建玉状況 <span class='muted'>({esc(data.get('mode',''))})</span></h1>",
        f"<p class='muted'>{esc(data.get('generated',''))}</p>",
    ]
    poss = data.get("positions") or []
    parts.append("<h2>現在の建玉</h2>")
    if not poss:
        parts.append("<p>建玉なし（フラット）</p>")
    else:
        parts.append(
            "<table><tr><th class='l'>銘柄</th><th class='l'>方向</th><th>数量</th><th>取得単価</th>"
            "<th>現在値</th><th>含み損益</th></tr>"
        )
        for p in poss:
            side = "ロング🟩" if p["side"] == "long" else "ショート🟦"
            up = p.get("upnl")
            upcell = "-" if up is None else f"<span class='{'pos' if up>=0 else 'neg'}'>{_yen(up)}</span>"
            parts.append(
                f"<tr><td class='l'>{esc(p['symbol'])}</td><td class='l'>{side}</td>"
                f"<td>{p['base']}</td><td>{p['entry']}</td><td>{p.get('price') if p.get('price') is not None else '-'}</td>"
                f"<td>{upcell}</td></tr>"
            )
        parts.append("</table><p class='muted'>※含み損益は概算（現在値ベース）。手数料・金利は含みません</p>")

    ms = data.get("margin_status") or {}
    if ms:
        parts.append("<h2>信用の証拠金状況</h2><table>")
        parts.append(f"<tr><td class='l'>保証金率</td><td>{ms.get('total_margin_balance_percentage') or '-'} %</td></tr>")
        parts.append(f"<tr><td class='l'>ロスカット率</td><td>{ms.get('losscut_percentage') or '-'} %</td></tr>")
        parts.append(f"<tr><td class='l'>追証率</td><td>{ms.get('margin_call_percentage') or '-'} %</td></tr>")
        parts.append(f"<tr><td class='l'>評価損益</td><td>{_yen(float(ms.get('margin_position_profit_loss') or 0))}</td></tr>")
        parts.append("</table><p class='muted'>※ロスカット率に近づくと強制決済。建玉が無い時は「-」</p>")

    bal = data.get("balance") or {}
    if bal:
        parts.append("<h2>残高</h2><table><tr><th class='l'>通貨</th><th>数量</th></tr>")
        for k, v in bal.items():
            parts.append(f"<tr><td class='l'>{esc(str(k))}</td><td>{v}</td></tr>")
        parts.append("</table>")
    if data.get("balance_error"):
        parts.append(f"<p class='neg'>残高取得エラー: {esc(data['balance_error'])}</p>")

    parts.append("<p class='muted'>操作は「操作パネル」から行えます</p>")
    parts.append("</body></html>")
    return "".join(parts)


def _reason_label(reason: str | None) -> str:
    return {"stop_loss": "損切り", "take_profit": "利確", "rsi_signal": "RSI70", "entry": ""}.get(reason or "", reason or "")


def _fmt_hold(sec) -> str:
    if not sec or sec < 0:
        return "-"
    m = int(sec // 60)
    if m < 60:
        return f"{m}分"
    return f"{m // 60}時間{m % 60}分"


def _build_roundtrips(trades: list, reason_map: dict, rsi_map: dict | None = None) -> tuple[list, list]:
    """約定を買い→売りでFIFOペアリングし、往復（クローズ済み）と未決済lotを返す。"""
    rsi_map = rsi_map or {}
    trades = sorted(trades, key=lambda t: t.get("timestamp") or 0)
    lots: list[dict] = []  # 未決済の買いlot（FIFO）
    rts: list[dict] = []
    for t in trades:
        side = t.get("side")
        amt = float(t.get("amount") or 0)
        price = float(t.get("price") or 0)
        ts = t.get("timestamp")
        if amt <= 0:
            continue
        if side == "buy":
            lots.append({"qty": amt, "price": price, "time": ts, "order": t.get("order")})
        elif side == "sell":
            remaining = amt
            reason = reason_map.get(str(t.get("order"))) if t.get("order") is not None else None
            while remaining > 1e-12 and lots:
                lot = lots[0]
                m = min(remaining, lot["qty"])
                rts.append(
                    {
                        "entry_ts": lot["time"],
                        "entry_price": lot["price"],
                        "entry_rsi": rsi_map.get(str(lot.get("order"))),
                        "exit_ts": ts,
                        "exit_price": price,
                        "qty": m,
                        "pnl": (price - lot["price"]) * m,
                        "pnl_pct": ((price / lot["price"] - 1) * 100) if lot["price"] else 0.0,
                        "hold_sec": ((ts - lot["time"]) / 1000) if (ts and lot["time"]) else None,
                        "reason": reason,
                    }
                )
                lot["qty"] -= m
                remaining -= m
                if lot["qty"] <= 1e-12:
                    lots.pop(0)
    return rts, lots


def _rt_summary(rts: list) -> dict:
    n = len(rts)
    wins = sum(1 for r in rts if r["pnl"] > 0)
    total = sum(r["pnl"] for r in rts)
    return {
        "count": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": (wins / n * 100) if n else 0.0,
        "total_pnl": total,
        "avg_pnl": (total / n) if n else 0.0,
    }


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

    # サーバ側ログから order_id -> 理由 / 買いのRSI を作る（注記用・再デプロイで消える点に注意）
    jentries = journal.read_trades(1000)
    reason_map = {
        str(e.get("order_id")): e.get("reason") for e in jentries if e.get("order_id") and e.get("reason")
    }
    rsi_map = {
        str(e.get("order_id")): e.get("rsi")
        for e in jentries
        if e.get("order_id") and e.get("action") == "buy" and e.get("rsi") is not None
    }

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
        rts, open_lots = _build_roundtrips(trades, reason_map, rsi_map)
        out["symbols"][sym]["roundtrips"] = rts[-50:]
        out["symbols"][sym]["rt_summary"] = _rt_summary(rts)
        out["symbols"][sym]["open_lots"] = len(open_lots)
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

        # 往復トレード台帳（買い→売りペア）
        rt = s.get("rt_summary") or {}
        rts = s.get("roundtrips") or []
        if rt.get("count"):
            tot = rt["total_pnl"]
            tcls = "pos" if tot >= 0 else "neg"
            parts.append("<div class='card'>")
            parts.append(
                f"往復トレード <b>{rt['count']}</b>回　勝ち {rt['wins']} / 負け {rt['losses']}　勝率 <b>{rt['win_rate']:.0f}%</b><br>"
            )
            parts.append(f"合計損益 <b class='{tcls}'>{_yen(tot)}</b>　1回平均 {_yen(rt['avg_pnl'])}")
            if s.get("open_lots"):
                parts.append(f"　<span class='muted'>(未決済 {s['open_lots']}件)</span>")
            parts.append("</div>")
        if rts:
            parts.append(
                "<table><tr><th>#</th><th class='l'>エントリー(JST)</th><th>取得単価</th><th>取得RSI</th>"
                "<th class='l'>決済(JST)</th><th>決済単価</th><th>数量</th><th>損益</th><th>損益%</th><th>保有</th><th class='l'>理由</th></tr>"
            )
            total = len(rts)
            for idx, r in enumerate(reversed(rts)):
                num = total - idx
                pcls = "pos" if r["pnl"] >= 0 else "neg"
                parts.append(
                    f"<tr><td>{num}</td>"
                    f"<td class='l'>{esc(_fmt_ts(r['entry_ts'], True) if r['entry_ts'] else '')}</td><td>{r['entry_price']}</td>"
                    f"<td>{r['entry_rsi'] if r.get('entry_rsi') is not None else '-'}</td>"
                    f"<td class='l'>{esc(_fmt_ts(r['exit_ts'], True) if r['exit_ts'] else '')}</td><td>{r['exit_price']}</td>"
                    f"<td>{r['qty']}</td>"
                    f"<td class='{pcls}'>{_yen(r['pnl'])}</td><td class='{pcls}'>{r['pnl_pct']:+.2f}%</td>"
                    f"<td>{esc(_fmt_hold(r['hold_sec']))}</td><td class='l'>{esc(_reason_label(r['reason']))}</td></tr>"
                )
            parts.append("</table>")

        parts.append("<details><summary class='muted'>個別約定の明細を表示</summary>")
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
        parts.append("</details>")

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
