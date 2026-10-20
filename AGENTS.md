# Repository Guidelines

> **引き継ぎ・現状把握はまず [`HANDOFF.md`](HANDOFF.md) を読むこと**（目的・ライブ戦略・アーキテクチャ・運用の罠・未決TODOを集約）。次に `CHANGELOG.md`。このファイルは開発規約のみ。

## プロジェクト構成

`app/` に FastAPI のルート、モメンタム戦略、リスク管理、ccxt による取引所連携を配置します。`tests/` は pytest、`pine/` は TradingView の Pine スクリプト、`scripts/` は成績記録・バックテストなどの補助ツールです。`data/` に成績データ、`reviews/` に定期レビュー、`docs/` に説明用 HTML を置きます。戦略の現状は `CHANGELOG.md`、配備手順は `DEPLOY.md` を確認してください。

## 開発・テストコマンド

- `python3 -m venv .venv`：仮想環境を作成します。
- `source .venv/bin/activate`：仮想環境を有効にします。
- `pip install -r requirements.txt`：実行・テスト依存を導入します。
- `.env` がなければ `cp .env.example .env`：ローカル設定を準備します。
- `uvicorn app.main:app --host 127.0.0.1 --port 8000`：ローカル API を起動します。
- `pytest -q`：全テストを実行します。
- `pytest -q tests/test_momentum_live.py`：戦略の判断ロジックを検証します。

起動前に `TRADING_MODE=DRY_RUN` と `NOTIFY_ENABLED=false` を明示してください。独立したビルド工程はありません。

## コーディング規約

Python は4スペースでインデントし、関数・変数・モジュールには `snake_case`、クラスには `PascalCase`、定数には `UPPER_SNAKE_CASE` を使います。既存の型注釈と docstring の書式に合わせ、関数は小さく保ちます。判断ロジックは外部通信から分離し、新しい値を返す設計を優先してください。API 境界で入力を検証し、例外を黙殺しないでください。専用の formatter・lint 設定は現状ありません。

## テスト方針

pytest のファイル名は `test_*.py`、関数名は `test_*` とします。不具合修正・機能追加では失敗するテストを先に追加し、実装後に全体を確認してください。純粋関数の単体テストと FastAPI `TestClient` の統合テストを中心に、認証、重複防止、資金不足などの境界条件を含めます。取引所・Discord・価格取得はモックし、実発注や通知を発生させないでください。`conftest.py` の環境変数は既存値を上書きしないため、シェル側の設定にも注意します。カバレッジ目標は80%以上ですが、自動強制する設定はありません。

## コミット・プルリクエスト

履歴には日本語の説明と `chore:` 形式が混在しています。新規コミットは `fix: 現金残高に合わせて注文額を制限` のように `<type>: <説明>` を推奨します。PR には目的、動作の変更、実行したテストと結果、関連 issue を記載し、画面変更には画像を添付してください。戦略変更は理由・期待・レビュー予定を `CHANGELOG.md` に残します。

## 安全・設定

運用変更前に `.agents/skills/trading-ops/SKILL.md` と最新の変更履歴を確認してください。ADA の隔離、固定額サイズ、地合いフィルターを維持し、BTC 土台への適用範囲は現行実装で確認します。AI に実発注・送金・売却を代行させないでください。`.env`、API 鍵、共有シークレットをコミット・ログ出力しないでください。push は Render の自動デプロイにつながります。環境変数の変更はコード配備だけでは反映されず、Render 側の設定反映が必要です。
