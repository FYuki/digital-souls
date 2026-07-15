# backend

digital-souls の自作バックエンド（FastAPI）。

- Ollama（gemma4:e4b）への接続
- キャラクター（`characters/`）のロード
- `GET /` のヘルスチェック
- `POST /chat` のチャット応答 API

実装は [#10](https://github.com/FYuki/digital-souls/issues/10) から開始する。

## セットアップ

初回、または `backend/requirements.txt` の更新後に、リポジトリルートで実行する。

```bash
scripts/setup-backend.sh
```

`setup-backend.sh` は `backend/.venv` の作成と実行時依存関係のインストールだけを行い、Backend は起動しない。

## 起動

```bash
scripts/start-backend.sh
```

`start-backend.sh` は構築済みの `backend/.venv` を使って Backend だけを foreground で起動する。環境がない場合にセットアップは自動実行されず、`setup-backend.sh` の実行を促すエラーで終了する。Backend プロセス自身が終了した場合は、その終了ステータスが呼び出し元へ伝播する。
