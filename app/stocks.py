"""株・ETFのRSIパワーゾーン監視（通知のみ・売買しない）。

暗号資産(bitbankで自動売買)とは別に、米国ETF/米国株/日本株を毎日チェックし、
「200日SMAより上 かつ 4期間RSIが低い（買いゾーン）」の銘柄をDiscordへ日次レポートする。
売買はしない（日本から株のAPI発注は困難なため）＝通知を見て、必要ならご自身の証券会社で手動。

・日足データは Yahoo Finance の公開JSON（無料・APIキー不要）から取得。
・シグナルのパラメータ(SMA200/RSI4/買い30/利確55)は暗号資産のパワーゾーンと共有(config)。
・pz.evaluate_signal を再利用するので「暗号資産と同じ計算」で株を判定する。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from .config import settings
from . import powerzones as pz
from .notifier import notify

logger = logging.getLogger("stocks")
JST = timezone(timedelta(hours=9))

# 監視ユニバース: (ティッカー, 表示名, グループ)。Yahoo表記（日本株は .T）。
# 手動で買える主要どころ。増減したくなったら声をかけてもらえば調整する。
UNIVERSE: list[tuple[str, str, str]] = [
    # 米国ETF（パワーゾーン本来の土俵＝指数/セクターETFは平均回帰しやすい）
    ("SPY", "S&P500", "米ETF"),
    ("QQQ", "Nasdaq100", "米ETF"),
    ("DIA", "NYダウ", "米ETF"),
    ("IWM", "米小型株", "米ETF"),
    ("VTI", "米国全体", "米ETF"),
    ("XLK", "米テック", "米ETF"),
    ("XLF", "米金融", "米ETF"),
    ("XLE", "米エネルギー", "米ETF"),
    ("XLV", "米ヘルスケア", "米ETF"),
    ("SMH", "半導体", "米ETF"),
    ("GLD", "金", "米ETF"),
    # 米国個別株（大型）
    ("AAPL", "アップル", "米株"),
    ("MSFT", "マイクロソフト", "米株"),
    ("NVDA", "エヌビディア", "米株"),
    ("GOOGL", "グーグル", "米株"),
    ("AMZN", "アマゾン", "米株"),
    ("META", "メタ", "米株"),
    ("TSLA", "テスラ", "米株"),
    # 日本株・ETF
    ("1321.T", "日経225ETF", "日本"),
    ("1306.T", "TOPIX ETF", "日本"),
    ("7203.T", "トヨタ", "日本"),
    ("6758.T", "ソニー", "日本"),
    ("9984.T", "ソフトバンクG", "日本"),
    ("8306.T", "三菱UFJ", "日本"),
    ("6861.T", "キーエンス", "日本"),
]

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


# ---- 純粋関数（ネットワーク不要・テスト対象） ----

def signal_of(close, sma200, rsi4) -> dict:
    """終値/SMA/RSI から売買ゾーンを判定する（発注はしない・表示/通知用）。"""
    if close is None or sma200 is None or rsi4 is None:
        return {"above_sma": None, "buy": False, "near": False, "exit_zone": False}
    above = close > sma200
    buy = above and rsi4 < settings.pz_entry                         # 買いゾーン
    near = above and (not buy) and rsi4 < settings.stocks_near_rsi   # もうすぐ買い
    exit_zone = rsi4 > settings.pz_exit                             # 利確ゾーン（保有中なら）
    return {"above_sma": above, "buy": buy, "near": near, "exit_zone": exit_zone}


def _fmt_rsi(v) -> str:
    return "-" if v is None else f"{v:.0f}"


def build_digest(rows: list[dict], today: str) -> str:
    """評価済みの行から、Discordへ送る日次レポート文を組み立てる（純粋関数）。"""
    ok = [r for r in rows if not r.get("error")]
    buys = [r for r in ok if r.get("buy")]
    nears = [r for r in ok if r.get("near")]
    exits = [r for r in ok if r.get("exit_zone")]
    errs = [r for r in rows if r.get("error")]

    def line(r: dict) -> str:
        return f"　・{r['ticker']} {r['name']} RSI{_fmt_rsi(r.get('rsi4'))}"

    L = [f"📈 株パワーゾーン日次レポート（{today}）"]
    if buys:
        L.append(f"🟢 買いゾーン {len(buys)}件（200日線↑ & RSI<{int(settings.pz_entry)}）")
        L += [line(r) for r in sorted(buys, key=lambda r: r.get("rsi4") or 0)]
    else:
        cand = [r for r in ok if r.get("above_sma") and r.get("rsi4") is not None]
        if cand:
            c = min(cand, key=lambda r: r["rsi4"])
            L.append(f"🟢 買いシグナルなし（最も近い: {c['ticker']} {c['name']} RSI{_fmt_rsi(c['rsi4'])}）")
        else:
            L.append("🟢 買いシグナルなし")
    if nears:
        L.append(f"🟡 もうすぐ買い（RSI<{int(settings.stocks_near_rsi)}）")
        L += [line(r) for r in sorted(nears, key=lambda r: r.get("rsi4") or 0)]
    if exits:
        L.append(f"💰 利確ゾーン（保有中ならRSI>{int(settings.pz_exit)}）")
        L += [line(r) for r in exits]
    tail = f"監視 {len(ok)}銘柄"
    if errs:
        tail += f" / 取得失敗 {len(errs)}"
    L.append("―――")
    L.append(tail + "　※通知のみ・売買はご自身の証券会社で手動判断を")
    return "\n".join(L)


# ---- データ取得（Yahoo Finance 公開JSON） ----
# 米国株/ETFも日本株(.T)も同一APIで日足200本以上を取得できるためYahooを主軸にする。
# データセンターIPだと稀に429を返すので、Cookieセッション＋query1/2フォールバック＋軽いリトライで堅牢化。

_WARMUP_URL = "https://finance.yahoo.com/"


def _parse_chart(data: dict) -> list[float]:
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError("no data")
    quote = (result[0].get("indicators") or {}).get("quote") or [{}]
    return [c for c in (quote[0].get("close") or []) if c is not None]


async def _fetch_closes(client: httpx.AsyncClient, ticker: str) -> list[float]:
    """日足の終値リストを返す（直近2年≈500本）。全経路失敗なら例外。"""
    params = {"range": "2y", "interval": "1d"}
    last_exc: Exception = RuntimeError("unknown")
    for host in ("query1", "query2"):  # 片方が詰まっても他方で拾う
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}"
        try:
            r = await client.get(url, params=params)
            if r.status_code == 429:  # レート制限は一拍おいて次経路へ
                last_exc = RuntimeError("429 too many requests")
                await asyncio.sleep(1.5)
                continue
            r.raise_for_status()
            return _parse_chart(r.json())
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            await asyncio.sleep(0.4)
    raise last_exc


async def _evaluate_one(client: httpx.AsyncClient, entry: tuple[str, str, str]) -> dict:
    ticker, name, group = entry
    need = settings.pz_sma_len + settings.pz_rsi_len + 2
    base = {"ticker": ticker, "name": name, "group": group}
    try:
        closes = await _fetch_closes(client, ticker)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s データ取得失敗: %s", ticker, exc)
        return {**base, "error": str(exc)[:80]}
    if len(closes) < need:
        return {**base, "error": f"データ不足({len(closes)}本)"}
    sig = pz.evaluate_signal(closes)  # 暗号資産と同じSMA200/RSI4計算を再利用
    zone = signal_of(sig["close"], sig["sma200"], sig["rsi4"])
    return {**base,
            "rsi4": round(sig["rsi4"], 1) if sig["rsi4"] is not None else None,
            **zone}


async def evaluate_all() -> list[dict]:
    """全ユニバースを評価して行リストを返す（発注しない）。"""
    rows = []
    async with httpx.AsyncClient(timeout=15, headers=_HEADERS, follow_redirects=True) as client:
        try:  # Cookie取得のウォームアップ（429対策）。失敗しても続行
            await client.get(_WARMUP_URL)
        except Exception:  # noqa: BLE001
            pass
        for entry in UNIVERSE:
            rows.append(await _evaluate_one(client, entry))
            await asyncio.sleep(0.4)  # Yahooへのレート配慮
    return rows


async def run_and_notify() -> dict:
    """評価してDiscordへ日次レポートを送る。集計サマリーを返す。"""
    rows = await evaluate_all()
    today = datetime.now(JST).strftime("%Y-%m-%d")
    await notify(build_digest(rows, today))
    return {
        "buy": sum(1 for r in rows if r.get("buy")),
        "near": sum(1 for r in rows if r.get("near")),
        "exit_zone": sum(1 for r in rows if r.get("exit_zone")),
        "errors": sum(1 for r in rows if r.get("error")),
        "total": len(rows),
    }


async def signal_status() -> list[dict]:
    """表示用に全銘柄の現在シグナルを返す（/stocks ページ用）。"""
    return await evaluate_all()


def _seconds_until_eval() -> float:
    """次の評価時刻(JST hour, 複数可)までの秒数。"""
    now = datetime.now(JST)
    secs = []
    for h in settings.stocks_eval_hours or [7]:
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        secs.append((t - now).total_seconds())
    return min(secs)


async def stocks_loop() -> None:
    """毎日 stocks_eval_hours(JST) に株モニターを回してDiscordへ通知するループ。"""
    if not settings.stocks_enabled:
        logger.info("株モニターは無効（STOCKS_ENABLED=false）")
        return
    hours = ", ".join(f"{h}:00" for h in settings.stocks_eval_hours)
    logger.info("株モニター 起動（%d銘柄・通知 JST %s）", len(UNIVERSE), hours)
    while True:
        await asyncio.sleep(_seconds_until_eval())
        try:
            summary = await run_and_notify()
            logger.info("株モニター通知: %s", summary)
        except Exception:  # noqa: BLE001
            logger.exception("株モニターでエラー")
        await asyncio.sleep(60)  # 同一時刻での二重通知を避ける
