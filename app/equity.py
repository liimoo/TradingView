"""資産推移（NAV指数）の再構築とページ描画。

「資産推移」を **入出金の影響を受けない NAV指数（start=100）** で表す。
NAVは「その日に保有していた銘柄の等ウェイト日次リターン」を毎日つないだもの＝戦略の実質リターン。
買い増し・入金でNAVは動かず、価格の動きと"どの銘柄を持っていたか"だけで決まる。
→ 純資産額（円）を出さずに、資産の伸び／地合い退避の効果を可視化できる。

過去分は取引所の約定履歴（保有銘柄をその日ごとに復元）＋bitbank公開日足（価格）から再構築し、
[[perf_log]] が今後分を同じ指標で継ぎ足す。ベンチマークとしてBTCを同じ100基準で重ねる。

このモジュールの計算部は純粋関数（ネットワーク不要・テスト対象）。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
_DUST_JPY = 100.0  # これ未満の評価額の建玉は「保有なし」とみなす（端数除去）


def ts_to_date(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, JST).strftime("%Y-%m-%d")


def holdings_by_date(trades_by_symbol: dict, dates: list[str],
                     closes: dict[str, dict[str, float]]) -> dict[str, list[str]]:
    """各日付で「保有していた銘柄」を、約定履歴を再生して求める（純粋関数）。

    trades_by_symbol: {sym: [{"timestamp":ms,"side":"buy"/"sell","amount":float}, ...]}
    dates:            対象日(昇順・"YYYY-MM-DD")
    closes:           {sym: {date: close}} 端数判定に使う
    戻り: {date: [保有していたsym...]}
    """
    # 各銘柄の (日付, 累積base) を作る
    events: dict[str, list[tuple[str, float]]] = {}
    for sym, trades in trades_by_symbol.items():
        cum = 0.0
        seq = []
        for t in sorted(trades, key=lambda x: x.get("timestamp") or 0):
            amt = float(t.get("amount") or 0)
            cum += amt if t.get("side") == "buy" else -amt
            seq.append((ts_to_date(t.get("timestamp") or 0), max(0.0, cum)))
        events[sym] = seq

    out: dict[str, list[str]] = {}
    for d in dates:
        held = []
        for sym, seq in events.items():
            base = 0.0
            for edate, ecum in seq:
                if edate <= d:
                    base = ecum
                else:
                    break
            close = (closes.get(sym) or {}).get(d)
            if base > 1e-12 and (close is None or base * close >= _DUST_JPY):
                held.append(sym)
        out[d] = held
    return out


def nav_series(dates: list[str], held_by_date: dict[str, list[str]],
               closes: dict[str, dict[str, float]], start: float = 100.0) -> list[dict]:
    """保有銘柄の等ウェイト日次リターンを連鎖したNAV指数（純粋関数）。

    各日 d のリターン＝「前日保有していた銘柄」の (close[d]/close[d-1]-1) の平均。
    現金（前日保有なし）はリターン0でNAV据え置き。start=100。
    戻り: [{"date","nav","ret_pct","held":[...]}]
    """
    nav = start
    out = []
    for i, d in enumerate(dates):
        if i == 0:
            out.append({"date": d, "nav": round(nav, 4), "ret_pct": 0.0,
                        "held": held_by_date.get(d, [])})
            continue
        prev = dates[i - 1]
        held_prev = held_by_date.get(prev, [])
        rets = []
        for s in held_prev:
            cp = (closes.get(s) or {}).get(prev)
            cn = (closes.get(s) or {}).get(d)
            if cp and cn:
                rets.append(cn / cp - 1)
        r = sum(rets) / len(rets) if rets else 0.0
        nav *= (1 + r)
        out.append({"date": d, "nav": round(nav, 4), "ret_pct": round(r * 100, 3),
                    "held": held_by_date.get(d, [])})
    return out


def normalize_index(dates: list[str], closes_for_symbol: dict[str, float],
                    start: float = 100.0) -> list[float | None]:
    """1銘柄の終値系列を start 基準の指数に正規化（ベンチマーク用・純粋関数）。"""
    base = None
    for d in dates:
        if closes_for_symbol.get(d):
            base = closes_for_symbol[d]
            break
    out: list[float | None] = []
    for d in dates:
        c = closes_for_symbol.get(d)
        out.append(round(start * c / base, 4) if (c and base) else None)
    return out


def build_curve(trades_by_symbol: dict, closes: dict[str, dict[str, float]],
                calendar: list[str], btc_symbol: str = "BTC/JPY") -> dict:
    """再構築の総合。calendar(昇順日付)上でNAVとBTCベンチを作る（純粋関数）。"""
    held = holdings_by_date(trades_by_symbol, calendar, closes)
    nav = nav_series(calendar, held, closes)
    btc_idx = normalize_index(calendar, closes.get(btc_symbol) or {})
    return {"dates": calendar, "nav": nav, "btc_index": btc_idx,
            "held_latest": held.get(calendar[-1], []) if calendar else []}


# ---- 描画（純粋：data -> HTML文字列） ----

def _poly(points: list[tuple[float, float]]) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)


def render_equity_html(data: dict, generated: str = "", note: str = "") -> str:
    """NAV(戦略)とBTC(ベンチ)を start=100 で重ねた折れ線ページ（純粋関数）。"""
    import html as _html

    dates = data.get("dates") or []
    nav = data.get("nav") or []
    btc = data.get("btc_index") or []
    navvals = [p["nav"] for p in nav]

    css = ("body{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;"
           "margin:1.2rem;color:#111;background:#fafafa}h1{font-size:1.3rem}"
           ".muted{color:#888}.big{font-size:1.6rem;font-weight:800}"
           ".pos{color:#0a8f3c}.neg{color:#d33}.card{background:#fff;border:1px solid #e2e2e2;"
           "border-radius:12px;padding:1rem;max-width:820px;margin:.6rem 0}"
           ".lg{display:inline-block;width:12px;height:12px;border-radius:2px;margin:0 .3rem 0 .8rem;vertical-align:middle}")
    head = ("<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>資産推移(NAV)</title><style>{css}</style></head><body>"
            "<h1>📈 資産推移（NAV指数・start=100）</h1>"
            "<p class='muted'>NAV＝保有銘柄の等ウェイト日次リターンを連鎖した指数。"
            "<b>入出金・発注額の変更の影響を受けない</b>ので、純粋な戦略の伸びが見える"
            "（純資産額そのものは表示しません）。灰=BTC(買い持ち)ベンチ。</p>")

    if len(navvals) < 2:
        return head + "<div class='card'>データがまだ足りません（約定履歴が2日分以上たまると表示されます）。</div></body></html>"

    cur = navvals[-1]
    ret = cur - 100.0
    btc_last = next((v for v in reversed(btc) if v is not None), None)
    btc_ret = (btc_last - 100.0) if btc_last is not None else None
    rcls = "pos" if ret >= 0 else "neg"

    # --- SVG 折れ線 ---
    W, H = 760, 320
    padL, padR, padT, padB = 48, 16, 16, 34
    plotW, plotH = W - padL - padR, H - padT - padB
    allv = [v for v in navvals] + [v for v in btc if v is not None] + [100.0]
    ymin, ymax = min(allv), max(allv)
    if ymax - ymin < 1e-9:
        ymin, ymax = ymin - 1, ymax + 1
    pad = (ymax - ymin) * 0.08
    ymin, ymax = ymin - pad, ymax + pad
    n = len(dates)

    def X(i):
        return padL + (plotW * i / (n - 1))

    def Y(v):
        return padT + plotH * (1 - (v - ymin) / (ymax - ymin))

    nav_pts = _poly([(X(i), Y(v)) for i, v in enumerate(navvals)])
    btc_pts = _poly([(X(i), Y(v)) for i, v in enumerate(btc) if v is not None])
    y100 = Y(100.0)
    # 軸ラベル
    ylabels = "".join(
        f"<text x='{padL - 6}' y='{Y(v) + 3:.1f}' text-anchor='end' font-size='10' fill='#888'>{v:.0f}</text>"
        for v in (ymin + pad, 100.0, ymax - pad))
    xidx = [0, n // 2, n - 1]
    xlabels = "".join(
        f"<text x='{X(i):.1f}' y='{H - 8}' text-anchor='middle' font-size='10' fill='#888'>{dates[i][5:]}</text>"
        for i in xidx)
    svg = (
        f"<svg width='100%' viewBox='0 0 {W} {H}' style='max-width:{W}px'>"
        f"<line x1='{padL}' y1='{y100:.1f}' x2='{W - padR}' y2='{y100:.1f}' stroke='#ccc' stroke-dasharray='4 3'/>"
        f"{ylabels}{xlabels}"
        + (f"<polyline points='{btc_pts}' fill='none' stroke='#9aa0aa' stroke-width='1.5'/>" if btc_pts else "")
        + f"<polyline points='{nav_pts}' fill='none' stroke='#0a8f3c' stroke-width='2.2'/>"
        "</svg>"
    )

    esc = _html.escape
    held = "、".join(data.get("held_latest") or []) or "なし（現金）"
    btc_txt = (f"<span class='muted'>／ BTC買い持ち {('+' if btc_ret >= 0 else '')}{btc_ret:.1f}%</span>"
               if btc_ret is not None else "")
    body = (
        "<div class='card'>"
        f"<div>NAV <span class='big {rcls}'>{cur:.1f}</span> "
        f"<span class='{rcls}'>({'+' if ret >= 0 else ''}{ret:.1f}%)</span> {btc_txt}</div>"
        f"<div class='muted'>期間 {esc(dates[0])} 〜 {esc(dates[-1])}（{n}日）／ 現在の保有: {esc(held)}</div>"
        f"{svg}"
        "<div><span class='lg' style='background:#0a8f3c'></span>NAV（戦略）"
        "<span class='lg' style='background:#9aa0aa'></span>BTC（買い持ち・ベンチ）</div>"
        "</div>"
        f"<p class='muted'>{esc(note)}</p>"
        f"<p class='muted'>生成: {esc(generated)}　データ源: bitbank公開日足＋約定履歴。"
        "NAVは価格変動と保有銘柄のみで動く（入出金の影響を除く）。</p>"
    )
    return head + body + "</body></html>"
