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
    """約定をFIFOでペアリングし、往復（クローズ済み）と未決済lotを返す。

    ロング(買い→売り)・ショート(売り→買い)の両方に対応。未決済lotは全て同じ方向を持ち、
    反対売買が来ると古いlotから決済していく（余りが出ればドテンして新規建て）。
    """
    rsi_map = rsi_map or {}
    trades = sorted(trades, key=lambda t: t.get("timestamp") or 0)
    lots: list[dict] = []  # 未決済lot。dir: +1ロング / -1ショート
    rts: list[dict] = []
    for t in trades:
        side = t.get("side")
        amt = float(t.get("amount") or 0)
        price = float(t.get("price") or 0)
        ts = t.get("timestamp")
        if amt <= 0 or price <= 0:
            continue
        tdir = 1 if side == "buy" else -1  # 約定の方向
        remaining = amt
        reason = reason_map.get(str(t.get("order"))) if t.get("order") is not None else None
        # 反対方向のlotがあれば決済（FIFOペアリング）
        while remaining > 1e-12 and lots and lots[0]["dir"] != tdir:
            lot = lots[0]
            m = min(remaining, lot["qty"])
            ldir = lot["dir"]  # 建玉の方向（ロング/ショート）
            # ロング: (決済-取得)*数量 / ショート: (取得-決済)*数量 → *ldir で統一
            pnl = (price - lot["price"]) * m * ldir
            pnl_pct = ((price / lot["price"] - 1) * 100 * ldir) if lot["price"] else 0.0
            rts.append(
                {
                    "side": "long" if ldir == 1 else "short",
                    "entry_ts": lot["time"],
                    "entry_price": lot["price"],
                    "entry_rsi": rsi_map.get(str(lot.get("order"))),
                    "exit_ts": ts,
                    "exit_price": price,
                    "qty": m,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "hold_sec": ((ts - lot["time"]) / 1000) if (ts and lot["time"]) else None,
                    "reason": reason,
                }
            )
            lot["qty"] -= m
            remaining -= m
            if lot["qty"] <= 1e-12:
                lots.pop(0)
        # 残り（同方向 or 決済しきった後のドテン）は新規建てとしてlotに積む
        if remaining > 1e-12:
            lots.append({"dir": tdir, "qty": remaining, "price": price, "time": ts, "order": t.get("order")})
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
        net_base = buy_base - sell_base  # 未決済の建玉(base)。+ならロング超過 / −ならショート超過
        net_jpy = sell_cost - buy_cost - fee_jpy  # 売り金額−買い金額−手数料（建玉ゼロ時のみ純損益）
        # 現在値で建玉を時価評価し、実現＋含みの総合損益を出す（未決済分を net_jpy に足し戻す）
        last_price = open_value = mtm_pnl = None
        if trades and abs(net_base) > 1e-12:
            try:
                last_price = broker.ticker(sym)
                open_value = net_base * last_price
                mtm_pnl = net_jpy + open_value
            except Exception:  # noqa: BLE001
                pass
        out["symbols"][sym] = {
            "trades": len(trades),
            "buy_base": buy_base,
            "buy_cost": buy_cost,
            "sell_base": sell_base,
            "sell_cost": sell_cost,
            "fee_jpy": fee_jpy,
            "net_jpy": net_jpy,
            "net_base": net_base,
            "last_price": last_price,
            "open_value": open_value,  # 未決済建玉の現在値(JPY)。ロングは+資産/ショートは−負債
            "mtm_pnl": mtm_pnl,  # 実現＋含みの総合損益。建玉ゼロなら None（net_jpy をそのまま使う）
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

    # 全銘柄の総合損益（実現＋含み）を合計してトップに表示
    syms = data.get("symbols") or {}
    grand = 0.0
    grand_ok = True
    for s in syms.values():
        if s.get("error") or not s.get("trades"):
            continue
        if abs(s.get("net_base") or 0) > 1e-12:
            if s.get("mtm_pnl") is None:
                grand_ok = False  # 建玉ありなのに現在値が取れず時価評価できない
            else:
                grand += s["mtm_pnl"]
        else:
            grand += s.get("net_jpy") or 0.0
    if syms:
        gcls = "pos" if grand >= 0 else "neg"
        note = "" if grand_ok else " <span class='muted'>(一部の建玉は現在値未取得)</span>"
        parts.append(
            f"<div class='card'><b>総合損益（実現＋含み・全銘柄）: "
            f"<span class='{gcls}'>{_yen(grand)}</span></b>{note}"
            f"<br><span class='muted'>建玉は現在値で時価評価。未決済分の元手は損益に含めません（手数料・金利は概算）</span></div>"
        )

    for sym, s in syms.items():
        parts.append(f"<h2>{esc(sym)}</h2>")
        if s.get("error"):
            parts.append(f"<p class='neg'>取得エラー: {esc(s['error'])}</p>")
            continue
        # 取引ゼロの銘柄は1行だけにして見やすくする（全銘柄で体裁を揃える）
        if not s.get("trades"):
            parts.append("<div class='card'><span class='muted'>約定件数: 0（この銘柄はまだ取引なし）</span></div>")
            continue
        parts.append("<div class='card'>")
        parts.append(f"約定件数: <b>{s['trades']}</b>　")
        parts.append(f"買い: {_yen(s['buy_cost'])} ({s['buy_base']:.4f})　")
        parts.append(f"売り: {_yen(s['sell_cost'])} ({s['sell_base']:.4f})　")
        parts.append(f"手数料(JPY): {_yen(s['fee_jpy'])}<br>")
        has_pos = abs(s.get("net_base") or 0) > 1e-12
        if has_pos:
            side_jp = "ロング" if s["net_base"] > 0 else "ショート"
            parts.append(f"未決済建玉: <b>{abs(s['net_base']):.4f}</b> base（{side_jp}）")
            if s.get("open_value") is not None:
                parts.append(f"　現在値 ≈ {_yen(abs(s['open_value']))}")
            parts.append("<br>")
        if has_pos and s.get("mtm_pnl") is not None:
            mtm = s["mtm_pnl"]
            mcls = "pos" if mtm >= 0 else "neg"
            parts.append(f"総合損益(実現＋含み・概算): <b class='{mcls}'>{_yen(mtm)}</b>")
        elif has_pos:
            parts.append("<span class='muted'>総合損益: 現在値が取得できず時価評価できません（往復トレードの実現損益は下記）</span>")
        else:
            net = s["net_jpy"]
            cls = "pos" if net >= 0 else "neg"
            parts.append(f"実現損益(概算): <b class='{cls}'>{_yen(net)}</b>")
        parts.append("</div>")

        # 往復トレード台帳（買い→売りペア）。取引のある全銘柄で体裁を揃えて必ず表示
        rt = s.get("rt_summary") or {}
        rts = s.get("roundtrips") or []
        cnt = rt.get("count") or 0
        parts.append("<div class='card'>")
        if cnt:
            tot = rt["total_pnl"]
            tcls = "pos" if tot >= 0 else "neg"
            parts.append(
                f"往復トレード <b>{cnt}</b>回　勝ち {rt['wins']} / 負け {rt['losses']}　勝率 <b>{rt['win_rate']:.0f}%</b><br>"
            )
            parts.append(f"合計損益 <b class='{tcls}'>{_yen(tot)}</b>　1回平均 {_yen(rt['avg_pnl'])}")
        else:
            parts.append(
                "往復トレード <b>0</b>回　"
                "<span class='muted'>（決済が完了した往復はまだありません）</span>"
            )
        if s.get("open_lots"):
            parts.append(f"　<span class='muted'>(未決済 {s['open_lots']}件)</span>")
        parts.append("</div>")
        if rts:
            parts.append(
                "<table><tr><th>#</th><th class='l'>方向</th><th class='l'>エントリー(JST)</th><th>取得単価</th><th>取得RSI</th>"
                "<th class='l'>決済(JST)</th><th>決済単価</th><th>数量</th><th>損益</th><th>損益%</th><th>保有</th><th class='l'>理由</th></tr>"
            )
            total = len(rts)
            for idx, r in enumerate(reversed(rts)):
                num = total - idx
                pcls = "pos" if r["pnl"] >= 0 else "neg"
                side_jp = "ロング🟩" if r.get("side") == "long" else "ショート🟦"
                parts.append(
                    f"<tr><td>{num}</td><td class='l'>{side_jp}</td>"
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


# ============================================================
# 年間損益サマリー（確定申告の“把握・目安”用。税務アドバイスではない）
# ============================================================

def _ts_year(ts) -> int | None:
    """ミリ秒タイムスタンプ→JSTの暦年。"""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts / 1000, JST).year
    except Exception:  # noqa: BLE001
        return None


def _realized_events(trades: list) -> list:
    """符号付きポジションを時系列で追跡し、決済(ポジション減少)ごとに実現損益を返す。

    ロング(買い→売り)・ショート(売り→買い)の両方に対応した移動平均法ベースの概算。
    各イベント: {ts, pnl, qty}。pnlは手数料を含まない値差益（手数料は別途集計）。
    """
    trades = sorted(trades, key=lambda t: t.get("timestamp") or 0)
    pos = 0.0   # +ロング / −ショート の符号付き数量
    avg = 0.0   # 平均取得(売建)単価
    events: list[dict] = []
    for t in trades:
        amt = float(t.get("amount") or 0)
        price = float(t.get("price") or 0)
        ts = t.get("timestamp")
        if amt <= 0 or price <= 0:
            continue
        signed = amt if t.get("side") == "buy" else -amt
        while abs(signed) > 1e-12:
            if pos == 0 or (pos > 0) == (signed > 0):
                # 同方向 → 建て増し（平均単価を更新）
                new_abs = abs(pos) + abs(signed)
                avg = (avg * abs(pos) + price * abs(signed)) / new_abs
                pos += signed
                signed = 0.0
            else:
                # 反対方向 → 決済（実現損益を確定）
                closing = min(abs(signed), abs(pos))
                direction = 1.0 if pos > 0 else -1.0
                pnl = (price - avg) * closing * direction
                events.append({"ts": ts, "pnl": pnl, "qty": closing})
                pos -= direction * closing
                signed += direction * closing
                if abs(pos) < 1e-12:
                    pos = 0.0
                    avg = 0.0
                # signed が残っていればループ継続＝ドテン（残りは新規建て）
    return events


def build_tax_summary(year: int | None = None) -> dict:
    """暦年(1〜12月)の実現損益をJPYで集計する。確定申告の把握・目安用。"""
    now = datetime.now(JST)
    year = year or now.year
    out: dict = {
        "mode": settings.trading_mode,
        "year": year,
        "generated": now.strftime("%Y-%m-%d %H:%M:%S JST"),
        "symbols": {},
        "total_realized": 0.0,
        "total_fee": 0.0,
        "closes": 0,
    }
    if not broker.has_exchange:
        out["note"] = "取引所へ接続できません（DRY_RUNで鍵未設定など）。集計不可。"
        return out
    for sym in settings.allowed_symbols:
        try:
            trades = broker.my_trades(sym, limit=1000)
        except Exception as exc:  # noqa: BLE001
            out["symbols"][sym] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        events = _realized_events(trades)
        realized = sum(e["pnl"] for e in events if _ts_year(e["ts"]) == year)
        closes = sum(1 for e in events if _ts_year(e["ts"]) == year)
        fee_jpy = 0.0
        trades_in_year = 0
        for t in trades:
            if _ts_year(t.get("timestamp")) != year:
                continue
            trades_in_year += 1
            fee = t.get("fee") or {}
            if fee.get("currency") == "JPY":
                fee_jpy += float(fee.get("cost") or 0)
        out["symbols"][sym] = {
            "realized": realized,
            "fee": fee_jpy,
            "closes": closes,
            "trades": trades_in_year,
        }
        out["total_realized"] += realized
        out["total_fee"] += fee_jpy
        out["closes"] += closes
    out["net_estimate"] = out["total_realized"] - out["total_fee"]
    return out


# 日本の暗号資産税の基礎（一般情報。税務アドバイスではない）
TAX_NOTES = [
    "暗号資産の利益は原則「雑所得」＝総合課税（給与などと合算、所得税率5〜45%＋住民税約10%）。",
    "重要：暗号資産の信用（証拠金）取引は、FXと違い申告分離ではなく総合課税の雑所得扱い。",
    "給与所得者は、暗号資産などの利益が年20万円を超えると確定申告が必要（他の条件もあり）。",
    "課税のタイミングは「売った時・別の通貨に換えた時」などの実現時。含み益は対象外。",
    "計算方法は総平均法（個人の既定）または移動平均法（届出が必要）。",
]


def build_tax_csv(data: dict) -> str:
    """年間損益サマリーをCSV文字列で返す（税理士・税務ソフトへの受け渡し用）。"""
    lines = ["symbol,realized_pnl_jpy,fee_jpy,net_jpy,closes,trades"]
    for sym, s in (data.get("symbols") or {}).items():
        if s.get("error"):
            lines.append(f"{sym},ERROR,,,,")
            continue
        r = s.get("realized", 0.0)
        f = s.get("fee", 0.0)
        lines.append(f"{sym},{r:.2f},{f:.2f},{r - f:.2f},{s.get('closes', 0)},{s.get('trades', 0)}")
    tr = data.get("total_realized", 0.0)
    tf = data.get("total_fee", 0.0)
    lines.append(f"TOTAL,{tr:.2f},{tf:.2f},{tr - tf:.2f},{data.get('closes', 0)},")
    return "\n".join(lines) + "\n"


def render_tax_html(data: dict, secret: str = "") -> str:
    esc = html.escape
    parts = [
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>年間損益サマリー</title><style>",
        "body{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;margin:1.2rem;color:#111;background:#fafafa}",
        "h1{font-size:1.3rem}h2{font-size:1.05rem;margin-top:1.4rem}",
        "table{border-collapse:collapse;width:100%;margin:.4rem 0;font-size:.9rem;background:#fff}",
        "th,td{border:1px solid #ddd;padding:.4rem .6rem;text-align:right}th{background:#f0f0f0}",
        "td.l,th.l{text-align:left}.pos{color:#0a0}.neg{color:#c00}.muted{color:#888}",
        ".card{background:#fff;border:1px solid #e2e2e2;border-radius:8px;padding:.9rem 1.1rem;margin:.6rem 0}",
        ".big{font-size:1.6rem;font-weight:700}.warn{background:#fff7e6;border:1px solid #ffd591}",
        ".tot{background:#f6ffed;border:1px solid #b7eb8f}",
        "a.btn{display:inline-block;margin:.3rem 0;padding:.4rem .8rem;background:#1677ff;color:#fff;border-radius:6px;text-decoration:none;font-size:.85rem}",
        "</style></head><body>",
        f"<h1>年間損益サマリー <span class='muted'>{esc(str(data.get('year','')))}年 ({esc(data.get('mode',''))})</span></h1>",
        f"<p class='muted'>生成: {esc(data.get('generated',''))}</p>",
    ]
    if data.get("note"):
        parts.append(f"<div class='card'>{esc(data['note'])}</div>")

    # 免責（最重要）
    parts.append(
        "<div class='card warn'><b>⚠️ これは把握・目安用の概算です。確定申告そのものには使えません。</b><br>"
        "<span class='muted'>移動平均ベースの概算で、手数料・金利の一部や取引所外の入出金・報酬は含みません。"
        "正確な申告は国税庁の案内・税理士、または暗号資産専用の税計算サービス(Cryptact等)をご利用ください。</span></div>"
    )

    tr = data.get("total_realized", 0.0)
    tf = data.get("total_fee", 0.0)
    net = data.get("net_estimate", tr - tf)
    ncls = "pos" if net >= 0 else "neg"
    parts.append("<div class='card tot'>")
    parts.append(f"<div>{esc(str(data.get('year','')))}年の実現損益（概算）</div>")
    parts.append(f"<div class='big {ncls}'>{_yen(net)}</div>")
    parts.append(
        f"<div class='muted'>値差益 {_yen(tr)} − 手数料 {_yen(tf)}　/　決済回数 {data.get('closes',0)}</div>"
    )
    parts.append("</div>")

    # 20万円ラインの目安
    if net > 200000:
        parts.append(
            "<div class='card warn'>実現損益が <b>20万円</b> を超えています。給与所得者の場合、"
            "確定申告が必要になる可能性が高いです（他の所得と合わせて要確認）。</div>"
        )
    elif net > 0:
        parts.append(
            "<div class='card'><span class='muted'>実現損益は20万円以下です。給与所得者なら申告不要のケースもありますが、"
            "住民税の申告や他の副収入との合算など条件次第です。必ずご自身で確認を。</span></div>"
        )

    if secret:
        parts.append(f"<a class='btn' href='/tax?secret={esc(secret)}&format=csv'>CSVをダウンロード</a>")

    # 銘柄別
    parts.append("<h2>銘柄別の内訳</h2>")
    parts.append(
        "<table><tr><th class='l'>銘柄</th><th>実現損益</th><th>手数料</th><th>差引</th><th>決済回数</th><th>約定数</th></tr>"
    )
    for sym, s in (data.get("symbols") or {}).items():
        if s.get("error"):
            parts.append(f"<tr><td class='l'>{esc(sym)}</td><td class='neg' colspan='5'>{esc(s['error'])}</td></tr>")
            continue
        r = s.get("realized", 0.0)
        f = s.get("fee", 0.0)
        n = r - f
        rc = "pos" if r >= 0 else "neg"
        nc = "pos" if n >= 0 else "neg"
        parts.append(
            f"<tr><td class='l'>{esc(sym)}</td>"
            f"<td class='{rc}'>{_yen(r)}</td><td>{_yen(f)}</td>"
            f"<td class='{nc}'>{_yen(n)}</td>"
            f"<td>{s.get('closes',0)}</td><td>{s.get('trades',0)}</td></tr>"
        )
    parts.append("</table>")

    # 税の基礎知識
    parts.append("<h2>日本の暗号資産税の基礎（一般情報・要確認）</h2>")
    parts.append(
        "<div class='card'><p class='muted' style='margin-top:0'>※以下は一般的な内容です。"
        "最新・正確な扱いは国税庁の案内か税理士でご確認ください。</p><ul>"
    )
    for note in TAX_NOTES:
        parts.append(f"<li>{esc(note)}</li>")
    parts.append("</ul></div>")

    parts.append("</body></html>")
    return "".join(parts)
