# TradingView RSI 自動売買 中継サーバ

TradingView の RSI（売られすぎ/買われすぎ）Webhook を受け取り、
**リスク制御 → 発注 → Discord通知** を行う中継サーバです。

```
TradingView(Pine) --webhook JSON--> このサーバ --ccxt--> 取引所(検証:テストネット / 本番)
                                        └--> Discord通知
```

進め方は **DRY_RUN(通知のみ) → TESTNET(ペーパー) → LIVE(少額実売買)** の3段階。

## セットアップ（Mac）

```bash
cd ~/Documents/TradingView
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env を編集：WEBHOOK_SECRET を長いランダム文字列に、DISCORD_WEBHOOK_URL を設定
```

`WEBHOOK_SECRET` 用のランダム文字列生成例:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 起動

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
# もしくは: python -m app.main
```

## テスト（DRY_RUN のユニットテスト）

```bash
source .venv/bin/activate
pytest -q
```

## 疎通確認（Phase 1）

別ターミナルで（`YOUR_SECRET` は .env の WEBHOOK_SECRET）:
```bash
# 正常系（DRY_RUNなので発注はされず、Discordに通知が飛ぶ）
curl -s -X POST http://localhost:8000/webhook \
  -H 'Content-Type: application/json' \
  -d '{"secret":"YOUR_SECRET","action":"buy","symbol":"BTC/JPY","tf":"60","price":"9000000","rsi":"25","bar_time":"1"}'

# シークレット不一致 → 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8000/webhook \
  -H 'Content-Type: application/json' -d '{"secret":"wrong","action":"buy","symbol":"BTC/JPY"}'

# ヘルスチェック
curl -s http://localhost:8000/health

# キルスイッチ（発注停止/再開）
curl -s -X POST http://localhost:8000/killswitch \
  -H 'Content-Type: application/json' -d '{"secret":"YOUR_SECRET","on":true}'
```

## TradingView 側の設定

1. `pine/rsi_signal.pine` を TradingView の Pine エディタに貼り付け → チャートに追加。
2. インジケータの設定で「共有シークレット」を `.env` の `WEBHOOK_SECRET` と一致させる。
3. アラート作成：条件＝「Any alert() function call」、通知先＝Webhook URL に
   `https://<公開URL>/webhook` を指定。
4. 開発中の公開URLは Cloudflare Tunnel か ngrok で作る:
   ```bash
   cloudflared tunnel --url http://localhost:8000
   # または: ngrok http 8000
   ```
   ※ TradingView の Webhook はインターネットから到達できるURLが必要。Essential以上のプランが前提。

## モード切替（.env の TRADING_MODE）

| モード   | 挙動                                   | フェーズ |
|----------|----------------------------------------|----------|
| DRY_RUN  | 発注せずログ＆通知のみ                  | Phase 1  |
| TESTNET  | テストネットへ実発注（`EXCHANGE_ID=bybit`）| Phase 2  |
| LIVE     | 本番取引所へ実発注（`EXCHANGE_ID=bitbank`・極小サイズ推奨） | Phase 3  |

TESTNET/LIVE では `EXCHANGE_ID` / `EXCHANGE_API_KEY` / `EXCHANGE_API_SECRET` が必要。

### 本番取引所：bitbank（アルトコイン対応）
アルトコインを板取引で自動売買するため、本番は **bitbank**（`EXCHANGE_ID=bitbank`）を使う。
bitbank はアルトの板取引が国内最大級で ccxt 正式対応。`ALLOWED_SYMBOLS` に扱いたい
JPYペア（例 `XRP/JPY,SOL/JPY,DOGE/JPY,ADA/JPY`）を列挙する。

- API鍵は bitbank の「API」設定で発行。**出金権限は付与しない**（発注・参照のみ）。
- 検証(Phase 2)は bybit テストネットで行い、本番直前に `EXCHANGE_ID` と鍵を bitbank に差し替える。
  ※ bybit と bitbank ではシンボル表記が異なるため、`ALLOWED_SYMBOLS` もその段階で本番用へ戻す。

## Phase 2：bybitテストネットで自動発注を検証

日本の取引所にはテストネットが無いため、検証は bybit テストネットで行う。

1. bybit テストネットに登録し、APIキーを発行：https://testnet.bybit.com/
   （本番bybitとは別サイト。テスト資金はサイト内の Faucet で入手）
2. `.env` を検証用に切替：
   ```
   TRADING_MODE=TESTNET
   EXCHANGE_ID=bybit
   EXCHANGE_API_KEY=（テストネットの鍵）
   EXCHANGE_API_SECRET=（テストネットの秘密鍵）
   ALLOWED_SYMBOLS=BTC/USDT,ETH/USDT   # bybitの表記(USDT建て)に合わせる
   ORDER_QUOTE_AMOUNT=20               # 最小コスト(5 USDT)以上・小さめに
   ```
3. 接続チェック（参照のみ）：
   ```bash
   source .venv/bin/activate
   python scripts/check_broker.py --symbol BTC/USDT
   ```
4. 実テスト注文（最小サイズで買い→売り）：
   ```bash
   python scripts/check_broker.py --live-order --symbol BTC/USDT
   ```
5. サーバ経由の検証：`uvicorn app.main:app ...` を起動し、TradingViewのアラート
   （またはcurl）で buy→sell を流し、テストネット画面の約定履歴と `/health` の
   `positions` が一致することを確認する。

問題なく回れば **Phase 3**：`.env` を `TRADING_MODE=LIVE` / `EXCHANGE_ID=bitbank` /
bitbankの本番鍵 / `ALLOWED_SYMBOLS` をJPYペアへ戻し、**極小サイズ**で本番へ。

## 安全上の注意
- 必ず DRY_RUN → TESTNET → 少額LIVE の順で。いきなり本番にしない。
- APIキーは**出金権限を付与しない**。可能なら**IP制限**を設定。
- `.env` は Git 管理外（`.gitignore` 済）。シークレットを公開しない。
- 逆張りは強トレンドで連続逆行し得るため、建玉上限・クールダウン・キルスイッチを必ず使う。

## 将来：日本株（三菱UFJ eスマート証券）への展開
`app/broker.py` と同じ interface で kabuステーションAPI 実装を追加し、発注先を差し替える。
kabuステーションは Windows 専用のため、Mac 上の Windows 仮想環境かクラウドWindowsが必要。
