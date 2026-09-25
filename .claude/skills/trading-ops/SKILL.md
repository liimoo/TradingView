---
name: trading-ops
description: bitbankの暗号資産モメンタム自動売買システム(Render/FastAPI)を運用・変更・状況確認・PDCAするときに読む。現状把握の手順(/snapshot・WebFetchのキャッシュ回避)、安全な設定変更(コード=push自動デプロイ/環境変数=手動反映)、絶対に守る不変ルール(実発注はユーザーが押す・ADA隔離・固定額サイズのみ・地合いフィルター)、過去にハマった落とし穴、月次PDCAレビューの型をまとめる。
---

# 運用ランブック（暗号資産モメンタムbot）

このシステムを「運用・変更・状況確認・PDCA」するとき、まずこれを読むこと。毎回ゼロから学び直さない／状態を取り違えないための手順書。

## 1. これは何か（全体像）
- **本番**: bitbankでの暗号資産クロスセクション・モメンタム（順張り・日足・ロング現物のみ）。上位N=5銘柄を保有、200日線上＆上昇率上位、月次ローテーション＋**地合い日次判定**。損切りなし（順位落ち・地合い割れで自動退出）。
- **地合いフィルター**: 対象銘柄の等ウェイト指数が200日線割れの弱気相場では**全て現金へ退避**（`CRYPTO_REGIME_FILTER=true`）。
- **配信**: Render.com（Starter・$7/月・シンガポール・常時起動）／FastAPI＋uvicorn。シグナルデータ源は**Binance日足USDT**（`app/powerzones.py`のfetch）、執行は**bitbank JPY現物**。
- **株モニター**: 日本大型株82銘柄のモメンタムを月次でDiscord通知（**売買はしない**＝SBI等で手動判断用）。データ源はCNBC。
- **戦略の変遷は `CHANGELOG.md` を必ず確認**（いつ・何を・なぜ変えたか）。

## 2. 現状把握のやり方（最初にやること）
- **`GET /snapshot?secret=...`（要合言葉）** … PDCA用の一発サマリー。総資産・**建玉の円評価**・現金・**隔離ADAの時価**・**地合い（指数/200日線/乖離%/リスクオン/上位候補）**・BTCベンチマークを返す。まずこれを見る。
- `GET /health`（公開・合言葉不要） … mode/strategy/killed/建玉(base,entry)/当日損益/主要パラメータ。
- `GET /positions?format=json&secret=...` … 建玉の現在値・含み損益（円換算済み）。
- `GET /tax?secret=...&year=YYYY` … 暦年の実現損益（税・PDCAの確定損益）。
- **⚠️ WebFetchは同一URLを15分キャッシュする。** ライブ値を見るときは必ず `?cb=<適当な変化値>` を付けてキャッシュ回避すること（過去に「反映されてないように見えた」原因はこれ）。
- **合言葉(secret)はURLに載る＝私(AI)のツールで扱わない。** 合言葉が要るページは**ユーザー自身のブラウザで開いてもらい数値を教えてもらう**。合言葉が露出したら**ローテーション推奨**。

## 3. 設定変更の作法（ハマりどころ）
- **コードの変更（app/*.py 等）**: `git push` で **Renderが自動デプロイ**して反映。→ 手動デプロイ不要。
- **環境変数の変更（render.yaml の値）**: **通常/手動デプロイでは反映されない**。Renderの **Environmentタブで直接編集** するか **Blueprintを同期** する必要がある。（過去に発注額¥9,000→¥100,000で1時間ハマった。）
- **即時・一時変更**: `GET /config`（⚙️パラメーター調整ページ）。ただし**再デプロイで消える**ので、恒久化はRender Environment/render.yamlで。
- **push実行**: 環境によりAIのpushが自動モードでブロックされることがある。その場合はユーザーに `! git push origin main` を依頼。コミットはAIが作ってよい。
- **git履歴**: mainに直接コミットする運用（このプロジェクトの慣習）。コミットメッセージ末尾に規定のCo-Authored-By/Claude-Sessionを付ける。

## 4. 絶対に守る不変ルール（invariants）
1. **実発注ボタンはユーザーが押す。AIは代理で約定・送金・売却を実行しない**（パネルの🔀/🧹/flatten/killswitch、Render/bitbankの操作は案内のみ）。
2. **ADA/JPY は `ALLOWED_SYMBOLS` から除外＝隔離**（入金保有ADAをbotに取り込ませない・売らせない）。追加・変更時もADAを混ぜない。
3. **サイズは固定額のみ（`ORDER_SIZE_PCT=0` / `ORDER_QUOTE_AMOUNT`）。%運用は禁止**（隔離ADA・現金で総資産が膨張し、%だと発注額が歪むため）。現在の発注額は¥100,000（上位5＝最大約¥50万）。
4. **地合いフィルターON**（弱気は全現金）。安易に切らない（バックテストで等ウェイト指数フィルターが最良と確認済み）。
5. **株は通知のみ**（日本から株API発注はしない）。
6. **損切りは置かない**（モメンタム/平均回帰とも検証で逆効果。リスクはサイズと地合いと分散で管理）。

## 5. 過去にハマった落とし穴（再発防止）
- **WebFetchの15分キャッシュ** → `?cb=` で回避（§2）。
- **render.yaml env変更が自動反映されない** → Environment編集/Blueprint同期（§3）。
- **Renderのファイルは再デプロイで消える（ephemeral）** → journal(logs/trades.jsonl)・overrides.json・in-memoryのkill switch/建玉/`_last_*`は消える。**成績の正は取引所の約定履歴**（`app/report.py`）。永続化したいデータは外部（GitHub/スプレッドシート等）へ。
- **GitHub ActionsのIPからBinanceは遮断される** → cronで暗号資産momentumのデータ取得(`_gather`)が必ず失敗。**サーバレス($0)化は暗号資産では不可**（Renderが必要）。株通知(CNBC)やbitbank公開API/私有APIはGitHubからでも到達可。
- **サーバ内蔵戦略が有効な間、旧TradingView webhookは無視**（`STRATEGY != "webhook"`）。過去に信用ショート誤発注 → 修正済み。
- **再デプロイでkill switchはOFFに戻る**（in-memory）。

## 6. PDCAレビューの型（月次）
1. **データを見る**: `data/`のパフォーマンスログ（毎日1行）＋ `/snapshot` ＋ `/tax`（確定損益）。
2. **ベンチ比較**: 自分のリターン vs BTC vs 等ウェイト指数（`regime.index`）。“上がった/下がった”ではなく**ベンチ対比**で評価。
3. **戦略の当たり外れ**: 保有した上位N銘柄は実際に強かったか／地合いフィルターは退避で損失を避けられたか／DD（最大下落）。
4. **1回に1変更**: パラメータ実験は**一度に1つだけ**（因果を切り分けるため）。「仮説→変更→レビュー日→結果」を**必ず `CHANGELOG.md` に記録**。
5. **提案は具体的に**: 変えるなら「何を・いくつに・なぜ・いつ結果を見るか」。少額なので過剰最適化を避け、大きく崩れない設計を優先。

## 7. ファイル地図（どこを見るか）
- `app/momentum_live.py` … モメンタム本番（`momentum_targets`/`regime_is_up`/`market_index`/`rebalance`/`momentum_loop`）。純粋関数はテスト済み。
- `app/main.py` … FastAPIルート（/health・/snapshot・/positions・/report(_momentum)・/tax・/panel・/config・/momentum/rebalance・/webhook 等）＋操作パネルUI。
- `app/powerzones.py` … 発注執行`_execute`・Binanceデータ取得（momentumも執行はこれを流用）。
- `app/config.py` … 環境変数→設定。`sized_quote`。
- `app/report.py` … 約定履歴からP&L集計（`build_report`/`build_positions`/`build_tax_summary`）。
- `app/stocks.py` … 日本株82・月次モメンタム通知。
- `render.yaml` … Renderの環境変数（恒久設定の正）。
- `CHANGELOG.md` … 変更履歴・意思決定ログ（最重要・毎回確認）。
- メモリ(`memory/`): `powerzones-migration` / `stocks-monitor` / `tradingview-rsi-autotrade`。

## 8. ユーザーについて
- 日本の個人投資家・非エンジニア・Mac。少額運用。丁寧な日本語で、専門用語は噛み砕く。
- **投資助言はしない**（有資格アドバイザーではない）。事実・税・仕組み・選択肢の整理までに留め、売買判断は本人に委ねる。
