"""TradingView Webhook を受けて、リスク制御→発注→Discord通知を行う中継サーバ。

起動:
  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import html as html_lib
import logging
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ValidationError

from . import journal, monitor
from .broker import broker
from .config import settings, sized_quote
from .models import Signal
from .notifier import notify
from .report import build_positions, build_report, render_html, render_positions_html
from .risk import risk_manager, within_trading_hours
from .security import verify_secret

# ---- logging ----
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_LOG_DIR / "server.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

# 同一足の二重POSTを弾くための直近シグナル記憶
_recent_keys: deque[str] = deque(maxlen=200)


@asynccontextmanager
async def lifespan(app: FastAPI):
    problems = settings.validate()
    for p in problems:
        logger.warning("設定警告: %s", p)
    logger.info(
        "起動: mode=%s exchange=%s allowed=%s order_quote=%s stop_loss=%s",
        settings.trading_mode,
        settings.exchange_id,
        settings.allowed_symbols,
        settings.order_quote_amount,
        settings.stop_loss_pct,
    )
    # 起動時に建玉を復元 → 損切り監視ループを開始
    await monitor.reconstruct_positions()
    task = asyncio.create_task(monitor.exit_monitor_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="TradingView RSI 中継サーバ", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "mode": settings.trading_mode,
        "killed": risk_manager.is_killed(),
        "open_positions": risk_manager.open_count,
        "positions": {
            s: {"side": p.side, "base": p.base_qty, "entry": p.entry_price, "stop": p.stop_order_id}
            for s, p in risk_manager._positions.items()
        },
        "stop_loss_pct": settings.stop_loss_pct,
        "take_profit_pct": settings.take_profit_pct,
        "order_size_pct": settings.order_size_pct,
        "order_quote_amount": settings.order_quote_amount,
        "day_pnl": round(risk_manager.day_pnl, 2),
        "day_entries": risk_manager.day_entries,
        "max_daily_loss_pct": settings.max_daily_loss_pct,
        "daily_block": risk_manager.daily_block_reason(),
        "allowed_symbols": settings.allowed_symbols,
        "margin_symbols": settings.margin_symbols,
        "margin_active": settings.effective_margin_symbols(),  # 実際に信用で動く銘柄(設定∩取引所対応)
        "max_open_positions": settings.max_open_positions,
    }


@app.get("/report")
async def report(secret: str = "", format: str = "html"):
    """取引記録＆集計。ブラウザで /report?secret=... を開く（URLは他人に共有しない）。"""
    if not verify_secret(secret, settings.webhook_secret):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    data = await asyncio.to_thread(build_report)
    if format == "json":
        return JSONResponse(data)
    return HTMLResponse(render_html(data))


@app.get("/positions")
async def positions_endpoint(secret: str = "", format: str = "html"):
    """現在の建玉・含み損益・証拠金・残高。?format=json で生データ（bitbank実建玉含む）。"""
    if not verify_secret(secret, settings.webhook_secret):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    if format == "json":
        out = {
            "tracked": {
                s: {"side": p.side, "base": p.base_qty, "entry": p.entry_price}
                for s, p in risk_manager._positions.items()
            }
        }
        if broker.has_exchange:
            try:
                out["bitbank_margin"] = await asyncio.to_thread(broker.margin_positions)
            except Exception as exc:  # noqa: BLE001
                out["bitbank_margin_error"] = str(exc)
            try:
                out["margin_status"] = await asyncio.to_thread(broker.margin_status)
            except Exception as exc:  # noqa: BLE001
                out["margin_status_error"] = str(exc)
        return JSONResponse(out)
    data = await asyncio.to_thread(build_positions)
    return HTMLResponse(render_positions_html(data))


def _render_panel(secret: str) -> str:
    tmpl = """<!doctype html><html lang='ja'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>操作パネル</title><style>
body{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;margin:1rem;background:#f6f7f9;color:#111}
h1{font-size:1.2rem}.card{background:#fff;border:1px solid #e2e2e2;border-radius:10px;padding:1rem;margin:.7rem 0}
button{font-size:1rem;padding:.7rem 1rem;border-radius:8px;border:0;color:#fff;cursor:pointer;width:100%;margin:.3rem 0}
.red{background:#d33}.orange{background:#e08600}.green{background:#0a8f3c}.gray{background:#666}
a{color:#0a6ed1}.mono{font-family:ui-monospace,monospace;font-size:.85rem;white-space:pre-wrap;word-break:break-all}
.muted{color:#888;font-size:.85rem}
</style></head><body>
<h1>🎛️ 操作パネル</h1>
<div class='card' id='status'>読み込み中...</div>
<div class='card'>
  <button class='red' onclick="flatten()">🧹 全建玉を今すぐクローズ（flatten）</button>
  <button class='orange' onclick="kill(true)">🛑 緊急停止（新規発注を止める）</button>
  <button class='green' onclick="kill(false)">▶ 発注を再開</button>
</div>
<div class='card'>
  <div class='muted'>詳しく見る・設定</div>
  <a href='/report?secret=__S__' target='_blank'>📊 損益レポート</a> ／
  <a href='/positions?secret=__S__' target='_blank'>🔻 建玉・信用状況</a><br>
  <a href='/config?secret=__S__' target='_blank'>⚙️ パラメーター調整</a> ／
  <a href='/guide' target='_blank'>📖 通知の見方</a> ／
  <a href='/health' target='_blank'>🩺 稼働状況</a>
</div>
<div class='card mono' id='log'></div>
<script>
const S="__S__";
function log(m){document.getElementById('log').textContent=(new Date().toLocaleTimeString())+" "+m+"\\n"+document.getElementById('log').textContent;}
async function refresh(){
  try{const r=await fetch('/health');const d=await r.json();
    let pos=Object.entries(d.positions||{}).map(([k,v])=>k+": "+v.side+" "+v.base+" @"+v.entry).join("\\n")||"（建玉なし）";
    document.getElementById('status').innerHTML="<b>状態: "+d.mode+"</b>　本日損益: ¥"+d.day_pnl+(d.killed?" 　<b style='color:#d33'>停止中</b>":"")+"<br><div class='mono'>"+pos+"</div>";
  }catch(e){document.getElementById('status').textContent="取得失敗: "+e;}
}
async function flatten(){
  if(!confirm("本当に全部の建玉をクローズしますか？"))return;
  log("flatten実行中...");
  try{const r=await fetch('/flatten',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({secret:S})});
    const d=await r.json();log("flatten結果: "+JSON.stringify(d));refresh();}catch(e){log("失敗: "+e);}
}
async function kill(on){
  log((on?"緊急停止":"再開")+"実行中...");
  try{const r=await fetch('/killswitch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({secret:S,on:on})});
    const d=await r.json();log("結果: "+JSON.stringify(d));refresh();}catch(e){log("失敗: "+e);}
}
refresh();setInterval(refresh,15000);
</script></body></html>"""
    return tmpl.replace("__S__", secret)


@app.get("/panel")
async def panel(secret: str = ""):
    """ブラウザで押せる操作パネル（状態表示＋クローズ/緊急停止ボタン）。"""
    if not verify_secret(secret, settings.webhook_secret):
        return HTMLResponse("<h3>unauthorized（URLに ?secret=... が必要です）</h3>", status_code=401)
    return HTMLResponse(_render_panel(secret))


_GUIDE_ROWS = [
    ("🟢 BUY {銘柄}", "現物の買い（ロング建て）。RSIが30を割った"),
    ("🔵 SELL {銘柄}", "現物の売り（ロングを決済）。RSIが70を超えた"),
    ("🟩 現物ロング {銘柄}", "信用対象銘柄(XRP/ETH)を現物でロング建て（RSI30割れ）"),
    ("🟦 信用ショート {銘柄}", "信用で新規ショート建て（RSI70超え）"),
    ("💰 利確決済", "取得単価から+5%（または反対シグナル）で利益確定"),
    ("😖 損切り決済 / 😖 逆指値約定", "取得単価から-5%で損切り。ショートは値上がりで損切り"),
    ("🔻 逆指値set", "現物銘柄で、買いと同時にbitbankへ逆指値(自動損切り)を設置"),
    ("⏸️ 発注見送り [理由]", "安全機能で発注しなかった。理由＝許可外銘柄/建玉なし/建玉上限/クールダウン中/取引時間外/本日の損失上限/既に同方向/資金不足 など。多くは正常動作"),
    ("♻️ 建玉を復元", "サーバ再起動時に、保有中の建玉を自動で復元した"),
    ("🧹 全建玉クローズ", "操作パネルのクローズボタンを実行した"),
    ("🛑 キルスイッチ ON/OFF", "緊急停止/再開を実行した"),
    ("❌ 発注エラー", "注文が失敗。要チェック（残高不足・API一時エラーなど。続くなら相談を）"),
    ("⚠️ 逆指値の設定に失敗", "逆指値が置けずサーバ監視の損切りに切替。頻発するなら相談を"),
]


@app.get("/guide")
async def guide():
    """Discord通知の見方（解説ページ・合言葉不要）。"""
    rows = "".join(
        f"<tr><td class='l' style='white-space:nowrap'>{html_lib.escape(k)}</td><td class='l'>{html_lib.escape(v)}</td></tr>"
        for k, v in _GUIDE_ROWS
    )
    page = (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>通知の見方</title><style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;margin:1.2rem;background:#fafafa;color:#111}"
        "h1{font-size:1.2rem}table{border-collapse:collapse;width:100%;background:#fff;font-size:.9rem}"
        "th,td{border:1px solid #ddd;padding:.5rem .6rem;text-align:left;vertical-align:top}th{background:#f0f0f0}"
        ".muted{color:#888}</style></head><body>"
        "<h1>📖 Discord通知の見方</h1>"
        "<p class='muted'>基本、❌ と ⚠️ 以外は「見るだけ」でOKです。</p>"
        "<table><tr><th>通知</th><th>意味</th></tr>" + rows + "</table>"
        "</body></html>"
    )
    return HTMLResponse(page)


_CONFIG_FIELDS = [
    ("stop_loss_pct", "損切り幅（0.05 = 5%）"),
    ("take_profit_pct", "利確幅（0.05 = 5%）"),
    ("order_size_pct", "発注サイズ＝総資産の割合（0.10 = 10%。0で下の固定額）"),
    ("max_daily_loss_pct", "デイリー損失上限＝総資産の割合（0.08 = 8%）"),
    ("max_open_positions", "同時に持てる建玉数"),
    ("order_quote_amount", "固定発注額（円）※発注サイズ%が0の時のみ使用"),
    ("order_cooldown_sec", "連続発注クールダウン（秒）"),
]


def _render_config(secret: str) -> str:
    cur = settings.editable()
    rows = "".join(
        f"<div class='row'><label>{html_lib.escape(lbl)}</label>"
        f"<input id='{k}' value='{html_lib.escape(str(cur.get(k)))}'></div>"
        for k, lbl in _CONFIG_FIELDS
    )
    keys_js = ",".join(f"'{k}'" for k, _ in _CONFIG_FIELDS)
    tmpl = """<!doctype html><html lang='ja'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>パラメーター調整</title><style>
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;margin:1rem;background:#f6f7f9;color:#111}
h1{font-size:1.2rem}.card{background:#fff;border:1px solid #e2e2e2;border-radius:10px;padding:1rem;margin:.7rem 0}
.row{margin:.6rem 0}label{display:block;font-size:.9rem;margin-bottom:.2rem}
input{font-size:1rem;padding:.5rem;width:100%;box-sizing:border-box;border:1px solid #ccc;border-radius:6px}
button{font-size:1rem;padding:.7rem 1rem;border-radius:8px;border:0;color:#fff;background:#0a8f3c;cursor:pointer;width:100%}
.muted{color:#888;font-size:.85rem}.mono{font-family:ui-monospace,monospace;font-size:.85rem}
</style></head><body>
<h1>⚙️ パラメーター調整</h1>
<div class='card'>__ROWS__
  <button onclick='save()'>保存して即反映</button>
</div>
<div class='card muted'>⚠️ ここでの変更は<b>すぐ反映</b>されます（次のシグナルから有効）。ただし<b>Renderの再デプロイ時にはRenderの設定値へ戻ります</b>。恒久的に変えるならRenderのEnvironmentを変更してください。空欄の項目は変更しません。</div>
<div class='card mono' id='log'></div>
<script>
const S="__S__"; const KEYS=[__KEYS__];
function log(m){document.getElementById('log').textContent=m;}
async function save(){
  const v={}; KEYS.forEach(k=>{const el=document.getElementById(k); if(el && el.value!=='') v[k]=el.value;});
  try{const r=await fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({secret:S,values:v})});
    const d=await r.json(); log('反映しました: '+JSON.stringify(d.applied||d));}catch(e){log('失敗: '+e);}
}
</script></body></html>"""
    return tmpl.replace("__ROWS__", rows).replace("__KEYS__", keys_js).replace("__S__", secret)


class ConfigBody(BaseModel):
    secret: str
    values: dict = {}


@app.get("/config")
async def config_get(secret: str = ""):
    """パラメーター調整ページ（ブラウザで編集）。"""
    if not verify_secret(secret, settings.webhook_secret):
        return HTMLResponse("<h3>unauthorized（URLに ?secret=... が必要です）</h3>", status_code=401)
    return HTMLResponse(_render_config(secret))


@app.post("/config")
async def config_post(body: ConfigBody):
    if not verify_secret(body.secret, settings.webhook_secret):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    applied = settings.apply_overrides(body.values)
    if applied:
        await notify(f"⚙️ 設定変更: {applied}")
    return JSONResponse({"applied": applied, "current": settings.editable()})


@app.get("/orders")
async def orders(secret: str = ""):
    """取引所の未約定注文（逆指値の確認用）。/orders?secret=..."""
    if not verify_secret(secret, settings.webhook_secret):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    if not broker.has_exchange:
        return JSONResponse({"note": "取引所未接続"})
    out = {}
    for sym in settings.allowed_symbols:
        try:
            oo = await asyncio.to_thread(broker.open_orders, sym)
            out[sym] = [
                {"id": o.get("id"), "type": o.get("type"), "side": o.get("side"),
                 "amount": o.get("amount"), "trigger": o.get("triggerPrice") or o.get("stopPrice"),
                 "price": o.get("price"), "status": o.get("status")}
                for o in oo
            ]
        except Exception as exc:  # noqa: BLE001
            out[sym] = {"error": str(exc)}
    return JSONResponse(out)


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    # TradingViewは text/plain で送ることがあるため、生ボディをJSONとして読む
    raw = await request.body()
    try:
        signal = Signal.model_validate_json(raw)
    except ValidationError as exc:
        logger.warning("不正なペイロード: %s", exc)
        return JSONResponse(status_code=422, content={"error": "invalid payload"})

    # 1) 認証
    if not verify_secret(signal.secret, settings.webhook_secret):
        logger.warning("シークレット不一致（symbol=%s action=%s）", signal.symbol, signal.action)
        await notify(f"⚠️ 不正なWebhook（secret不一致）: {signal.action} {signal.symbol}")
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    # 2) 二重POST排除（同じ足の同じサイン）
    key = f"{signal.symbol}|{signal.action}|{signal.bar_time or ''}"
    if signal.bar_time and key in _recent_keys:
        logger.info("重複シグナルを無視: %s", key)
        return JSONResponse(status_code=200, content={"status": "duplicate_ignored"})
    _recent_keys.append(key)

    # TradingViewの銘柄表記(例 XRPUSDT)を取引所ペア(例 XRP/JPY)へ変換
    symbol = settings.resolve_symbol(signal.symbol)
    logger.info("シグナル受信: %s %s→%s price=%s rsi=%s tf=%s",
                signal.action, signal.symbol, symbol, signal.price, signal.rsi, signal.tf)

    # TradingViewのタイムアウト回避: 即200を返し、実際の売買はバックグラウンドで処理
    if settings.webhook_sync:  # テスト時のみ同期
        result = await _process_signal(symbol, signal)
        return JSONResponse(status_code=200, content=result or {"status": "processed"})
    asyncio.create_task(_process_signal(symbol, signal))
    return JSONResponse(status_code=200, content={"status": "accepted"})


_process_lock = asyncio.Lock()


async def _process_signal(symbol: str, signal: Signal) -> dict:
    """実際の売買処理（バックグラウンド・1件ずつ直列化）。"""
    async with _process_lock:
        try:
            if settings.is_margin(symbol):
                return await handle_margin(symbol, signal)
            return await _handle_spot(symbol, signal)
        except Exception as exc:  # noqa: BLE001
            logger.exception("シグナル処理エラー")
            await notify(f"❌ 処理エラー: {signal.action} {symbol}: {exc}")
            return {"status": "error", "detail": str(exc)}


def _skip(reason: str) -> dict:
    return {"status": "skipped", "reason": reason}


async def _handle_spot(symbol: str, signal: Signal) -> dict:
    """現物ロング専用の処理。"""
    decision = risk_manager.check(symbol, signal.action)
    if not decision.allowed:
        logger.info("見送り: %s", decision.reason)
        await notify(f"⏸️ 発注見送り [{decision.reason}] {signal.action} {symbol} (rsi={signal.rsi})")
        return _skip(decision.reason)

    risk_manager.mark_ordered(symbol, signal.action)
    order_quote = settings.order_quote_amount
    held = None
    # 価格は取引所(JPY)から取得（signal.priceはUSD建てなので使わない）
    px = 0.0
    if broker.has_exchange:
        try:
            px = await asyncio.to_thread(broker.ticker, symbol)
        except Exception:  # noqa: BLE001
            px = 0.0
    if not px:
        px = signal.price or 0.0
    try:
        if signal.action == "buy":
            assets = free_jpy = None
            if (settings.order_size_pct > 0 or settings.max_daily_loss_pct > 0) and broker.has_exchange:
                try:
                    assets, free_jpy = await asyncio.to_thread(broker.portfolio)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("資産取得に失敗: %s", exc)
            block = risk_manager.daily_block_reason(assets)
            if block:
                await notify(f"⏸️ 発注見送り [{block}] {symbol}")
                return _skip(block)
            if settings.order_size_pct > 0 and assets:
                order_quote = sized_quote(settings.order_size_pct, assets, free_jpy or 0, settings.order_quote_amount)
            if settings.min_order_jpy > 0 and order_quote < settings.min_order_jpy:
                await notify(f"⏸️ 資金不足で見送り: {symbol}（発注可能額≈¥{order_quote:.0f}）")
                return _skip("insufficient_funds")
            result = await asyncio.to_thread(broker.buy, symbol, order_quote, px)
        else:  # sell = 保有分の決済
            held = risk_manager.get_position(symbol)
            if held and held.stop_order_id and broker.has_exchange:
                try:
                    await asyncio.to_thread(broker.cancel, symbol, held.stop_order_id)
                    risk_manager.set_stop_order(symbol, None)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("逆指値キャンセル失敗（約定済みの可能性）: %s", exc)
            result = await asyncio.to_thread(broker.sell, symbol, held.base_qty, px)
    except Exception as exc:  # noqa: BLE001
        logger.exception("発注エラー")
        await notify(f"❌ 発注エラー: {signal.action} {symbol}: {exc}")
        return {"status": "order_error", "detail": str(exc)}

    if result.get("status") in {"ok", "dry_run"}:
        order = result.get("order") or {}
        journal.record_trade(
            {
                "mode": settings.trading_mode, "action": signal.action, "symbol": symbol,
                "quote": order_quote if signal.action == "buy" else None, "filled_base": result.get("filled_base"),
                "price": result.get("filled_price") or px, "rsi": signal.rsi, "order_id": order.get("id"),
                "status": result.get("status"), "reason": ("rsi_signal" if signal.action == "sell" else None),
            }
        )
        if signal.action == "buy":
            entry_price = result.get("filled_price") or px or 0.0
            risk_manager.open_position(symbol, result.get("filled_base"), entry_price)
            risk_manager.record_entry()
            if result.get("status") == "ok" and settings.stop_loss_pct > 0 and broker.has_exchange:
                try:
                    stop_price = entry_price * (1 - settings.stop_loss_pct)
                    so = await asyncio.to_thread(broker.place_stop_sell, symbol, result.get("filled_base"), stop_price)
                    risk_manager.set_stop_order(symbol, so.get("id"))
                    await notify(f"🔻 逆指値set: {symbol} stop@{stop_price:.4f} id={so.get('id')}")
                except Exception as exc:  # noqa: BLE001
                    logger.exception("逆指値設定エラー")
                    await notify(f"⚠️ 逆指値の設定に失敗（サーバ監視でカバー）: {symbol}: {exc}")
        else:
            exit_price = result.get("filled_price") or px or 0.0
            if held and held.entry_price and exit_price:
                risk_manager.record_close((exit_price - held.entry_price) * (held.base_qty or 0))
            risk_manager.close_position(symbol)

    emoji = "🟢" if signal.action == "buy" else "🔵"
    await notify(f"{emoji} {signal.action.upper()} {symbol} rsi={signal.rsi} price={signal.price}\n{result.get('summary')}")
    return {"status": result.get("status"), "summary": result.get("summary")}


async def handle_margin(symbol: str, signal: Signal) -> dict:
    """信用取引のフリップ戦略。buy→ロング / sell→ショート。反対建玉は決済してから反転。"""
    target_side = "long" if signal.action == "buy" else "short"
    pos = risk_manager.get_position(symbol)

    # 共通チェック（キルスイッチ・許可・クールダウン）
    dec = risk_manager.precheck(symbol, signal.action)
    if not dec.allowed:
        await notify(f"⏸️ 見送り [{dec.reason}] 信用 {signal.action} {symbol}")
        return _skip(dec.reason)

    # 既に同方向なら何もしない
    if pos and pos.side == target_side:
        return _skip(f"既に{target_side}建玉あり")

    # 総資産（サイズ・デイリー損失用）
    assets = free_jpy = None
    if broker.has_exchange:
        try:
            assets, free_jpy = await asyncio.to_thread(broker.portfolio)
        except Exception as exc:  # noqa: BLE001
            logger.warning("資産取得に失敗: %s", exc)

    # エントリーゲート（時間帯・デイリー損失・建玉上限）
    if not within_trading_hours(settings.trading_hours):
        await notify(f"⏸️ 見送り [取引時間外] 信用 {symbol}")
        return _skip("取引時間外")
    block = risk_manager.daily_block_reason(assets)
    if block:
        await notify(f"⏸️ 見送り [{block}] 信用 {symbol}")
        return _skip(block)
    if pos is None and risk_manager.open_count >= settings.max_open_positions:
        await notify(f"⏸️ 見送り [建玉上限({settings.max_open_positions})] 信用 {symbol}")
        return _skip("建玉上限")

    risk_manager.mark_ordered(symbol, signal.action)

    # 発注額と価格
    order_quote = settings.order_quote_amount
    if settings.order_size_pct > 0 and assets:
        order_quote = sized_quote(settings.order_size_pct, assets, free_jpy or 0, settings.order_quote_amount)
    # 価格は取引所(JPY)から取得。signal.priceはUSD建てで別物なので使わない
    px = 0.0
    if broker.has_exchange:
        try:
            px = await asyncio.to_thread(broker.ticker, symbol)
        except Exception:  # noqa: BLE001
            px = 0.0
    if not px:  # DRY_RUN等のフォールバック
        px = signal.price or 0.0

    try:
        # 1) 反対建玉があれば決済（ロング=現物売り / ショート=信用買い戻し）
        if pos:
            if pos.side == "long":
                if pos.stop_order_id and broker.has_exchange:
                    try:
                        await asyncio.to_thread(broker.cancel, symbol, pos.stop_order_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("逆指値キャンセル失敗: %s", exc)
                cres = await asyncio.to_thread(broker.sell, symbol, pos.base_qty, px)
            else:  # short → 信用で買い戻し
                cres = await asyncio.to_thread(broker.margin_order, symbol, "buy", pos.base_qty, "short", px)
            exitp = cres.get("filled_price") or px
            if pos.entry_price and exitp:
                sign = 1 if pos.side == "long" else -1
                risk_manager.record_close(sign * (exitp - pos.entry_price) * (pos.base_qty or 0))
            risk_manager.close_position(symbol)
            journal.record_trade({
                "mode": settings.trading_mode, "action": "close", "symbol": symbol, "side": pos.side,
                "filled_base": cres.get("filled_base"), "price": exitp, "reason": "flip",
                "entry_price": pos.entry_price, "order_id": (cres.get("order") or {}).get("id"),
                "status": cres.get("status"),
            })

        # 2) 新規建て（ロング=現物buy / ショート=信用sell）
        if target_side == "long":
            ores = await asyncio.to_thread(broker.buy, symbol, order_quote, px)
        else:
            amount = (order_quote / px) if px else None
            if not amount or amount <= 0:
                await notify(f"❌ 信用: 価格取得できず建てられません {symbol}")
                return {"status": "order_error", "detail": "no price"}
            ores = await asyncio.to_thread(broker.margin_order, symbol, "sell", amount, "short", px)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ハイブリッド発注エラー")
        await notify(f"❌ 発注エラー: {symbol}: {exc}")
        return {"status": "order_error", "detail": str(exc)}

    if ores.get("status") in {"ok", "dry_run"}:
        entry_price = ores.get("filled_price") or px or 0.0
        risk_manager.open_position(symbol, ores.get("filled_base"), entry_price, side=target_side)
        risk_manager.record_entry()
        journal.record_trade({
            "mode": settings.trading_mode, "action": "open", "symbol": symbol, "side": target_side,
            "quote": order_quote, "filled_base": ores.get("filled_base"), "price": entry_price,
            "rsi": signal.rsi, "order_id": (ores.get("order") or {}).get("id"), "status": ores.get("status"),
        })

    if target_side == "long":
        emoji, label = "🟩", "現物ロング"
    else:
        emoji, label = "🟦", "信用ショート"
    await notify(f"{emoji} {label} {symbol} rsi={signal.rsi} price={px}\n{ores.get('summary')}")
    return {"status": ores.get("status"), "summary": ores.get("summary")}


class SecretBody(BaseModel):
    secret: str


@app.post("/flatten")
async def flatten(body: SecretBody):
    """全建玉を決済してフラットにする（緊急用・テスト後始末用）。"""
    if not verify_secret(body.secret, settings.webhook_secret):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    results = []
    for sym, pos in list(risk_manager._positions.items()):
        px = pos.entry_price or 0.0
        if broker.has_exchange:
            try:
                px = await asyncio.to_thread(broker.ticker, sym)
            except Exception:  # noqa: BLE001
                pass
        try:
            if pos.side == "long":
                if pos.stop_order_id and broker.has_exchange:
                    try:
                        await asyncio.to_thread(broker.cancel, sym, pos.stop_order_id)
                    except Exception:  # noqa: BLE001
                        pass
                res = await asyncio.to_thread(broker.sell, sym, pos.base_qty, px)
            else:
                res = await asyncio.to_thread(broker.margin_order, sym, "buy", pos.base_qty, "short", px)
            if pos.entry_price:
                sign = 1 if pos.side == "long" else -1
                risk_manager.record_close(sign * (px - pos.entry_price) * (pos.base_qty or 0))
            risk_manager.close_position(sym)
            results.append({"symbol": sym, "side": pos.side, "summary": res.get("summary")})
        except Exception as exc:  # noqa: BLE001
            results.append({"symbol": sym, "error": str(exc)})
    await notify(f"🧹 全建玉クローズ: {len(results)}件")
    return JSONResponse(content={"closed": results})


class KillswitchBody(BaseModel):
    secret: str
    on: bool


@app.post("/killswitch")
async def killswitch(body: KillswitchBody) -> JSONResponse:
    if not verify_secret(body.secret, settings.webhook_secret):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    risk_manager.set_kill(body.on)
    await notify(f"🛑 キルスイッチ {'ON（発注停止）' if body.on else 'OFF（発注再開）'}")
    return JSONResponse(status_code=200, content={"killed": body.on})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
