"""株・ETFのRSIパワーゾーン監視（通知のみ・売買しない）。

暗号資産(bitbankで自動売買)とは別に、日本の大型株・ETF（すべてNISA成長投資枠対象）を
毎日チェックし、「200日SMAより上 かつ 4期間RSIが低い（買いゾーン）」の銘柄をDiscordへ
日次レポートする。売買はしない（日本から株のAPI発注は困難なため）＝通知を見て、必要なら
ご自身の証券会社で手動。（税・為替の手間を避けるため対象は日本株に限定。米国は外した。）

・日足データは CNBC の公開チャートJSON（無料・APIキー不要・米国株/ETFも日本株も同一API）から取得。
  （当初Yahooを使ったがデータセンターIPが429で弾かれたためCNBCへ変更。）
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

# 監視ユニバース: (ティッカー, 表示名, グループ)。日本株は .T 表記。
# 日本の大型株・ETFに限定（すべてNISA成長投資枠対象）。手動で買える主要どころを
# セクター横断で網羅。増減したくなったら声をかけてもらえば調整する。
UNIVERSE: list[tuple[str, str, str]] = [
    # 指数ETF（相場全体の基準）
    ("1321.T", "日経225ETF", "指数ETF"),
    ("1306.T", "TOPIX ETF", "指数ETF"),
    # 自動車・機械
    ("7203.T", "トヨタ", "自動車・機械"),
    ("7267.T", "ホンダ", "自動車・機械"),
    ("7269.T", "スズキ", "自動車・機械"),
    ("7270.T", "SUBARU", "自動車・機械"),
    ("6902.T", "デンソー", "自動車・機械"),
    ("6301.T", "コマツ", "自動車・機械"),
    ("6326.T", "クボタ", "自動車・機械"),
    ("7011.T", "三菱重工", "自動車・機械"),
    ("7013.T", "IHI", "自動車・機械"),
    # 電機・精密
    ("6758.T", "ソニー", "電機・精密"),
    ("6501.T", "日立", "電機・精密"),
    ("6503.T", "三菱電機", "電機・精密"),
    ("6752.T", "パナソニック", "電機・精密"),
    ("6702.T", "富士通", "電機・精密"),
    ("6971.T", "京セラ", "電機・精密"),
    ("6762.T", "TDK", "電機・精密"),
    ("6981.T", "村田製作所", "電機・精密"),
    ("6594.T", "ニデック", "電機・精密"),
    ("7751.T", "キヤノン", "電機・精密"),
    ("7741.T", "HOYA", "電機・精密"),
    ("4543.T", "テルモ", "電機・精密"),
    # 半導体・FA
    ("8035.T", "東京エレクトロン", "半導体・FA"),
    ("6857.T", "アドバンテスト", "半導体・FA"),
    ("6146.T", "ディスコ", "半導体・FA"),
    ("6723.T", "ルネサス", "半導体・FA"),
    ("6861.T", "キーエンス", "半導体・FA"),
    ("6954.T", "ファナック", "半導体・FA"),
    ("6273.T", "SMC", "半導体・FA"),
    ("6367.T", "ダイキン", "半導体・FA"),
    # 電線
    ("5803.T", "フジクラ", "電線"),
    ("5802.T", "住友電工", "電線"),
    # 通信・ネット
    ("9432.T", "NTT", "通信・ネット"),
    ("9433.T", "KDDI", "通信・ネット"),
    ("9984.T", "ソフトバンクG", "通信・ネット"),
    ("6098.T", "リクルート", "通信・ネット"),
    ("7974.T", "任天堂", "通信・ネット"),
    ("4755.T", "楽天", "通信・ネット"),
    # 商社
    ("8058.T", "三菱商事", "商社"),
    ("8031.T", "三井物産", "商社"),
    ("8001.T", "伊藤忠", "商社"),
    ("8053.T", "住友商事", "商社"),
    ("8002.T", "丸紅", "商社"),
    # 金融
    ("8306.T", "三菱UFJ", "金融"),
    ("8316.T", "三井住友FG", "金融"),
    ("8411.T", "みずほ", "金融"),
    ("8766.T", "東京海上", "金融"),
    ("8591.T", "オリックス", "金融"),
    ("8604.T", "野村", "金融"),
    # 医薬・化学
    ("4568.T", "第一三共", "医薬・化学"),
    ("4502.T", "武田薬品", "医薬・化学"),
    ("4519.T", "中外製薬", "医薬・化学"),
    ("4523.T", "エーザイ", "医薬・化学"),
    ("4578.T", "大塚HD", "医薬・化学"),
    ("4901.T", "富士フイルム", "医薬・化学"),
    ("4063.T", "信越化学", "医薬・化学"),
    ("3407.T", "旭化成", "医薬・化学"),
    ("3402.T", "東レ", "医薬・化学"),
    ("4452.T", "花王", "医薬・化学"),
    # 食品
    ("2914.T", "JT", "食品"),
    ("2802.T", "味の素", "食品"),
    ("2502.T", "アサヒ", "食品"),
    ("2503.T", "キリン", "食品"),
    # 小売・サービス
    ("9983.T", "ファーストリテイリング", "小売・サービス"),
    ("3382.T", "セブン&アイ", "小売・サービス"),
    ("8267.T", "イオン", "小売・サービス"),
    ("9843.T", "ニトリ", "小売・サービス"),
    ("4661.T", "オリエンタルランド", "小売・サービス"),
    # 運輸・インフラ
    ("9020.T", "JR東日本", "運輸・インフラ"),
    ("9022.T", "JR東海", "運輸・インフラ"),
    ("9101.T", "日本郵船", "運輸・インフラ"),
    ("9104.T", "商船三井", "運輸・インフラ"),
    ("9531.T", "東京ガス", "運輸・インフラ"),
    # 資源・素材
    ("5401.T", "日本製鉄", "資源・素材"),
    ("5108.T", "ブリヂストン", "資源・素材"),
    ("1605.T", "INPEX", "資源・素材"),
    ("5020.T", "ENEOS", "資源・素材"),
    # 不動産
    ("8801.T", "三井不動産", "不動産"),
    ("8802.T", "三菱地所", "不動産"),
    ("1925.T", "大和ハウス", "不動産"),
    ("1928.T", "積水ハウス", "不動産"),
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


# ---- データ取得（CNBC 公開チャートJSON） ----
# 米国株/ETFも日本株(.T)も同一APIで日足約500本を取得できる。APIキー不要・データセンターIPでも可。
# レンジ "1Y" が実際には約2年分の日足を返す（SMA200に十分）。

_CNBC_URL = "https://ts-api.cnbc.com/harmony/app/charts/1Y.json"


def cnbc_symbol(ticker: str) -> str:
    """ユニバースのティッカーをCNBC表記へ。日本株(.T)は "XXXX.T-JP"、米国はそのまま。"""
    return ticker + "-JP" if ticker.endswith(".T") else ticker


def _parse_cnbc(data: dict) -> list[float]:
    bars = (data.get("barData") or {}).get("priceBars") or []
    out = []
    for b in bars:  # 古い順→新しい順。close は文字列なのでfloat化
        c = b.get("close")
        if c in (None, ""):
            continue
        try:
            out.append(float(c))
        except (TypeError, ValueError):
            pass
    return out


async def _fetch_closes(client: httpx.AsyncClient, ticker: str) -> list[float]:
    """日足の終値リストを返す（約2年・500本前後）。失敗なら例外（軽く1回リトライ）。"""
    last_exc: Exception = RuntimeError("unknown")
    for attempt in range(2):
        try:
            r = await client.get(_CNBC_URL, params={"symbol": cnbc_symbol(ticker)})
            r.raise_for_status()
            closes = _parse_cnbc(r.json())
            if not closes:
                raise ValueError("no data")
            return closes
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            await asyncio.sleep(0.5)
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
    async with httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
        for entry in UNIVERSE:
            rows.append(await _evaluate_one(client, entry))
            await asyncio.sleep(0.2)  # レート配慮
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
