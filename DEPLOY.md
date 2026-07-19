# クラウド常設デプロイ手順（Render.com）

中継サーバを Render に置き、**固定URLで24時間稼働**させる。Macやターミナルは不要になる。
コードは GitHub（`liimoo/TradingView`）に上げてあるので、あなたは Render の画面操作だけでOK。

## 手順

### 1. Render にサインアップ
1. https://render.com を開く → 「Get Started」→ **GitHubアカウントでサインアップ**。
2. Render に GitHub リポジトリへのアクセスを許可（`TradingView` リポジトリを選べればOK）。

### 2. Blueprint でデプロイ
1. Render ダッシュボードで「**New +**」→「**Blueprint**」。
2. リポジトリ `liimoo/TradingView` を選択 →「**Connect**」。
3. `render.yaml` が自動で読み込まれ、サービス `tradingview-rsi-relay` が作られる。「**Apply**」。

### 3. 秘密情報（環境変数）を入力
デプロイ時、または サービスの「**Environment**」タブで、以下を入力：

| キー | 値 |
|---|---|
| `WEBHOOK_SECRET` | （あなたの共有シークレット。TradingView側と一致させる） |
| `DISCORD_WEBHOOK_URL` | （あなたの Discord Webhook URL） |

※ `EXCHANGE_API_KEY` / `EXCHANGE_API_SECRET` は Phase 2/3（実発注）で入力。通知だけの今は空でOK。

### 4. デプロイ完了 → URLを確認
- デプロイが「**Live**」になると、上部に固定URLが出る：`https://tradingview-rsi-relay.onrender.com`（末尾は環境で多少変わる）。
- ブラウザで `<そのURL>/health` を開き、`{"status":"ok",...}` が出れば成功。

### 5. TradingView のアラートに設定
- アラートの Webhook URL に **`<そのURL>/webhook`** を入れる。
- これで Mac を閉じていても、RSIシグナルがクラウド経由で Discord に届く。

## プラン（料金）
- `render.yaml` は **無料プラン(free)** で開始する設定。15分アクセスが無いとスリープし、復帰に約1分かかる（初回アラートを取りこぼす可能性あり）。
- **本番で自動売買する時は、Render の設定で `starter`（$7/月）に変更**して常時起動にするのが安全。

## モード切替（Renderダッシュボードの Environment）
- 通知のみ: `TRADING_MODE=DRY_RUN`
- テストネット: `TRADING_MODE=TESTNET` / `EXCHANGE_ID=bybit` ＋ bybit鍵
- 本番: `TRADING_MODE=LIVE` / `EXCHANGE_ID=bitbank` ＋ bitbank鍵 ＋ `ALLOWED_SYMBOLS` をJPYペアへ
- 変更後は「Manual Deploy」または保存で自動再起動。
