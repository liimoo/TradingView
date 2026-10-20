# 引き継ぎ資料（HANDOFF）

> このファイルは、別のAI（Codex等）や新しいエンジニアが**ゼロからこのプロジェクトを引き継ぐ**ための単一入口です。
> まずここを読み、次に `CHANGELOG.md`（いつ・何を・なぜ変えたか）を読めば現状を正しく把握できます。
> 運用の不変ルールは `.claude/skills/trading-ops/SKILL.md`、配備手順は `DEPLOY.md`。
> 最終更新: 2026-10-20

---

## 0. 最上位の目的（これを見失わない）

**bitbank 口座の「総資産（日本円）」を最大化する。** それだけがゴール。

- 指標として NAV（開始=100 の指数）を使う場面があるが、**NAV ≠ 円**。意思決定は常に「円の総資産」で行う。
- 運用者は日本の個人投資家（Mac・非エンジニア）。本番取引所は **bitbank**（現物・JPY建て）。
- 本番サーバは Render.com（$7/月 starter・常時起動）。

---

## 1. 30秒サマリ（今どうなっているか）

- **戦略**: 「**BTC土台60% ＋ モメンタム40%**」。総資産の60%を常にBTCで買い持ち。残り40%を、流動性の高い12銘柄から90日モメンタム上位5（BTCは除外）に配分。地合いが弱気ならモメンタム部だけ現金化（**BTC土台は維持**）。
- **なぜこの戦略か**: 2023–2026のバックテストで、旧「アルト・モメンタム単体」は **−13%** と BTC買い持ち **+96%** に大敗。BTCを厚く持つ方が優位（50%で+42%/70%で+63%）だったため、2026-10-13 に BTC土台へ転換。これは「今後もBTC優位」という相場観への**ベット**であり、アルト大相場なら負ける（だからモメンタムを40%残している）。
- **直近の実数（2026-10-18 認証付き /snapshot で確認）**: 総資産 **¥1,677,415**、うち BTC ≈ **¥1,005,615（60.0%）** ＝ 土台は正しく機能。9月末ピーク ~¥1.9M から約 **−12%**（10月のアルト調整）。暴落やバグではなく設計通りの変動。
- **ライブ稼働中**。実売買が走っている。変更は必ず慎重に。

---

## 2. 戦略の詳細

実装は `app/momentum_live.py`。判断ロジックは純関数に分離され、`tests/test_momentum_live.py` で検証済み。

| 要素 | 内容 | 根拠/定数 |
|---|---|---|
| BTC土台 | 総資産の `crypto_btc_core_pct`(=0.60) を常にBTC現物で保有 | `build_targets_jpy()` が `targets[BTC]=core_pct*assets` を常に設定 |
| モメンタム枠 | 残り(1−core)=40%を上位Nへ。1銘柄= `(MAX_INVEST/top_n) * sleeve_budget` | `MAX_INVEST=0.90`（10%は現金バッファ）、`top_n=CRYPTO_MOM_TOP=5` |
| 銘柄選定 | Binance日足USDTの**90日モメンタム**上位・**200日SMA上**・ロングのみ・損切りなし | `sleeve_candidates()` / `momentum_targets()`、`CRYPTO_MOM_LOOKBACK=90` |
| 地合いフィルター | 対象銘柄の等加重指数が200日SMAを**下回れば**モメンタム部を現金化（**BTC土台は維持**） | `regime_is_up()` / `market_index()`、`CRYPTO_REGIME_FILTER=true` |
| リバランス契機 | 「顔ぶれ(目標銘柄集合)が変わった」or「地合い変化」or「月替わり」。優先度: 地合い>顔ぶれ>月次。**値ブレだけでは売買しない** | `rebalance_reason()` |
| 執行 | 原則 指値(maker)。`MAKER_WAIT_SEC`(=90)秒で未約定なら成行(taker)へ。安全決済(損切り等)は常に成行 | `ORDER_ENTRY_TYPE=limit` |
| 現金ガード | 買い発注は「その時点の現金残高×0.99」を上限にクリップ（大きな買いで資金不足エラー60002を防ぐ） | `rebalance()` 内の買いループ |

- **データ元は Binance日足（USDT建て）**、**執行は bitbank（JPY現物）**。`SYMBOL_MAP` で USDT→JPY を対応付け。
- `core_pct=0` にすれば**純モメンタムへ即戻せる**（土台なし）。

### バックテスト結果（2023-07〜2026-10, 約3.2年）※判断の根拠
| 構成 | 損益 |
|---|---|
| 旧アルト・モメンタム（現行以前） | **−13%** |
| 全アルト等加重 | −36% |
| 薄いアルト除外（12銘柄厳選） | +3% |
| 地合いフィルターOFF | 0% |
| **BTC買い持ち** | **+96%** |
| BTC土台50% / 70% | +42% / +63% |

### リスクの正直な整理（引き継ぐ人が必ず理解すべき点）
- 資産の約6割がBTCの値動きそのもの。**BTCが暴落すれば土台60%分はそのまま下落を受ける**（地合いフィルターはモメンタム部にしか効かない）。
- クリプトは半年で±50%級の変動が普通。方向は誰にも予測できない。これは「クリプトに賭け続けるか」という**リスク許容度の問題**であり、Botの精度の問題ではない。
- 不安（ボラ）を下げたいなら唯一効くレバーは「**クリプトに置く総額を減らす＝現金を厚くする**」。`crypto_btc_core_pct` を下げると上振れ期待も下がる。

---

## 3. アーキテクチャ

FastAPI アプリ（`app/main.py`）。元は TradingView の RSI Webhook 中継として作られたが、**現在ライブ運用中の主役は暗号資産モメンタム/BTC土台**（`STRATEGY=momentum`）。README は歴史的経緯（RSI中継）寄りなので注意。

### 主要ファイル
| ファイル | 役割 |
|---|---|
| `app/main.py` | FastAPI 本体。ルート定義、起動時(lifespan)に各ループを起動 |
| `app/momentum_live.py` | **本番戦略の中核**。目標算出→差分→発注。純関数＋発注ラッパ＋`momentum_loop()` |
| `app/config.py` | 全設定（環境変数→`settings`）。既定値はここ |
| `app/broker.py` | ccxt 経由の取引所連携（残高・発注・時価） |
| `app/risk.py` | リスク管理（キルスイッチ、日次損失上限、重複防止、建玉記録） |
| `app/risk_watch.py` | 毎時の急落監視（保存なし・急落時のみDiscord警告）。`evaluate()` 純関数＋ヒステリシス |
| `app/daily_review.py` | 日次(JST10時)に 資産¥/NAV/DD/地合い を Discord へ。`compose_review()` 純関数 |
| `app/equity.py` | 資産推移の計算/HTML（`drawdowns`/`nav_series`/`build_curve`/`render_equity_html`） |
| `app/powerzones.py` `app/stocks.py` `app/screener.py` | 旧/補助戦略・株モメンタム通知・スクリーニング |
| `app/notifier.py` | Discord 通知 |
| `data/perf_log.csv` | 日次の成績ログ（GitHub Actionsが追記、9/05〜継続） |
| `reviews/REVIEW-*.md` | 週次PDCAレビューの出力 |

### 主なHTTPルート（`app/main.py`）
- 監視/参照: `GET /health` `GET /snapshot` `GET /positions` `GET /equity` `GET /tax` `GET /report` `GET /panel`
- 実行(**要認証・副作用あり**): `POST /momentum/rebalance`（🔀 手動リバランス） `POST /flatten` `POST /killswitch` `POST /webhook`
- 計算系: `GET /equity/backfill`（過去再構成） `GET /review/run`
- `GET /snapshot?public=1` は秘密情報を落とした公開用モードあり。

### 主要な設定値（既定・`app/config.py` / 本番は `render.yaml`）
| env | 既定 | 意味 |
|---|---|---|
| `CRYPTO_BTC_CORE_PCT` | 0.60 | BTC土台比率（0で純モメンタム） |
| `CRYPTO_MOM_TOP` / `CRYPTO_MOM_LOOKBACK` | 5 / 90 | モメンタム保有数 / 測定日数 |
| `CRYPTO_REGIME_FILTER` | true | 地合いフィルター（モメンタム部のみ） |
| `ORDER_SIZE_PCT` | 0.19 | （参考値）1銘柄≒総資産の19%。実配分は `build_targets_jpy` が主 |
| `MAX_INVEST`（コード定数） | 0.90 | 総投資上限（10%現金）。60002対策で0.95→0.90 |
| `ALLOWED_SYMBOLS` | 12銘柄 | `BTC,ETH,SOL,LTC,BCH,BNB,DOGE,AVAX,DOT,ATOM,TRX,XLM /JPY` |
| `MAKER_WAIT_SEC` | 90 | 指値→成行切替の待ち秒数 |
| `DD_ALERT_PCT` / `INTRADAY_DROP_PCT` | 0.10 / 0.08 | 日次DD警告 / 毎時急落警告の閾値 |
| `TRADING_MODE` | DRY_RUN（本番はLIVE） | DRY_RUN=発注しない |
| `EXCHANGE_ID` | （本番 bitbank） | 取引所 |

---

## 4. デプロイと運用（Render）

- **コード変更**: `git push` → Render が**自動デプロイ**。
- **環境変数(env)変更**: `render.yaml` を書いただけでは**ライブに反映されない**。Render ダッシュボードの Environment を直接編集する（または Blueprint 同期）。秘密情報(`sync:false`)は必ずダッシュボードで入力し、GitHubには載せない。
- **評価時刻**: `PZ_EVAL_HOURS=9,21`（JST 朝9時・夜21時）。日足戦略なので判断は同じ、朝の失敗を夜に拾う保険。
- デプロイで戦略が有効化される既定値方式: `crypto_btc_core_pct` はコード既定0.60なので、env未設定でもデプロイで有効になる。

### 円(¥)の総資産を確認する方法（重要）
1. **認証付き /snapshot**（最も確実）: `GET https://tradingview-rsi-relay.onrender.com/snapshot?secret=<WEBHOOK_SECRET>`
   → 総資産/現金/各銘柄の時価(JPY)がJSONで返る。**BTC比率が約60%か**をここで検算する。
2. `/equity?secret=<WEBHOOK_SECRET>` … 資産推移（10/1以降・開始100で再基準化）。
3. ローカル読取専用MCP: `/Users/le_oram/Documents/trading-mcp/server.py`（`bot_snapshot`/`bot_positions`/`bot_tax`。`BOT_BASE_URL`+`BOT_SECRET` をenvで渡す。登録は任意）。

---

## 5. 監視・定期レビュー
- **日次**: `daily_review_loop`（JST10時）が 資産¥/NAV/DD/地合い を Discord 投稿。現在DD ≤ −10% か地合いオフで⚠️アラート。
- **毎時**: `risk_watch_loop` が直近ピークから −8% 下落で⚠️（保存なし・ヒステリシスで連投防止）。
- **週次PDCA**: 手動スキル `.claude/skills/monthly-review/SKILL.md` ＋ GitHub Actions の自動レビュー。出力は `reviews/REVIEW-YYYY-MM-DD.md`（**助言のみ・自動売買変更はしない**）。
- **日次 perf_log**: GitHub Actions が `data/perf_log.csv` に追記。
  - ⚠️ **GitHub ActionsのIPは Binance にブロックされる**ため、cron上でクリプト価格を取る処理は動かない（成績記録は動く）。クリプトデータ取得は Render 側で行う。

---

## 6. 安全・不変ルール（絶対に守る）
- **AIは実発注・送金・売却を代行しない。** 🔀 リバランス / flatten / killswitch など**ライブの売買ボタンは必ず運用者本人が押す**。AIは提案と確認まで。
- **ADA は隔離**（運用対象外）。`ALLOWED_SYMBOLS` 変更で対象外になった銘柄（例: 過去のSUI）は bot が管理しなくなる＝**孤立。手動売却が必要**。
- **秘密情報をコミット/ログ出力しない**: `WEBHOOK_SECRET`、`EXCHANGE_API_KEY/SECRET`、`DISCORD_WEBHOOK_URL` は Render の Environment と各ローカルMCPのenv（`~/.claude.json`、ローカルのみ）で管理。このリポジトリは公開。
  - ⚠️ `WEBHOOK_SECRET` はチャットに複数回貼られ**露出済み**。ローテーション推奨（`openssl rand -hex 24`）。変更時は Render env とローカルMCP両方を更新。
- 変更は**一度に1つ**（因果を切り分ける）。変えたら必ず `CHANGELOG.md` に 変更/理由/期待/レビュー予定 を追記。
- テスト方針: 純関数の単体＋FastAPI `TestClient` 統合。取引所/Discord/価格取得はモックし、**実発注・実通知を絶対に発生させない**。

---

## 7. 既知の落とし穴（ハマりポイント）
- **env はコードpushで反映されない**（§4）。「変えたのに効かない」の9割はこれ。
- **GitHub Actions ⇔ Binance ブロック**（§5）。cronでクリプト価格は取れない。
- **WebFetch の“要約”はCSVの中間行を飛ばすことがある**。perf_log等の**厳密な数値確認は raw/git**（`git show origin/main:data/perf_log.csv`）で行う。要約を鵜呑みにして「データ欠損」と誤読した前例あり。
- **WebFetch は15分キャッシュ**。最新値が欲しいときは `?cb=` で変える。
- **bitbank 60002 = JPY不足**。満額投資で成行フィルの余裕が消えると出る。`MAX_INVEST=0.90`（現金10%）＋買いループの現金ガードで対策済み。
- 「NAV ≠ 円」。NAVは指数。運用者への報告は必ず**円**で。

---

## 8. 未決・引き継ぎTODO
- [ ] `ALLOWED_SYMBOLS`(12銘柄) と `MAKER_WAIT_SEC=90` が **Render Environment に反映済みか**最終確認（`render.yaml` は更新済み）。未反映なら設定。孤立銘柄があれば手動売却。
- [ ] `WEBHOOK_SECRET` のローテーション（露出済みのため推奨）。実施時は Render env ＋ 各ローカルMCP の env を更新。
- [ ] trading-bot MCP（`/Users/le_oram/Documents/trading-mcp/server.py`）の登録は任意（円の恒久参照用）。`claude mcp add --scope user trading-bot -e BOT_BASE_URL=... -e BOT_SECRET='<secret>' -- python3 <path>`。
- [ ] （検討）BTC暴落時の土台保護は**現状なし**。必要なら設計検討。
- [ ] （任意）#3 Google Sheets の私的¥ログ（Discord日次＋MCPと重複のため保留中）。

---

## 9. 情報源（どこを見るか）
| 目的 | ファイル |
|---|---|
| **いつ・何を・なぜ変えたか**（最重要・最初に読む） | `CHANGELOG.md` |
| 運用の不変ルール・作法 | `.claude/skills/trading-ops/SKILL.md` |
| 週次PDCAの手順 | `.claude/skills/monthly-review/SKILL.md` |
| 配備手順 | `DEPLOY.md` |
| リポジトリ規約（Codex向け） | `AGENTS.md` |
| セットアップ/疎通 | `README.md` |
| 本番設定 | `render.yaml` |
| 戦略判断ロジックのテスト（仕様の実例） | `tests/test_momentum_live.py` |
| 長期の運用哲学 | `長さんからの売買ルールと心得.md` |

### ローカル環境（運用者のMac）
- プロジェクト: `/Users/le_oram/Documents/TradingView`
- 自作MCP: `/Users/le_oram/Documents/yahoo-mail-mcp/server.py`（Yahooメール読取専用）、`/Users/le_oram/Documents/trading-mcp/server.py`（bot参照）
- 本番URL: `https://tradingview-rsi-relay.onrender.com`
