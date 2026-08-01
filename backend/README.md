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

LLM・Whisper・prompt予算は `backend/.env.example` の環境変数で変更できる。`OLLAMA_CONTEXT_TOKENS` はOllamaの実行時context、`LLM_CONTEXT_TOKEN_LIMIT` はモデル自体の最大context、`OLLAMA_RESPONSE_RESERVE_TOKENS` はassistant応答の予約量である。応答予約量は実行時context未満、実行時contextはモデル最大context以下でなければならない。`ASSISTANT_MAX_GENERATION_TOKENS` は応答予約量の既存契約名で、同時指定時は同値が必要になる。不正値は起動時に設定名を含むエラーとなる。

`WHISPER_MODEL` を変更すると、環境adapterのcache確認・prepareとBackendのfaster-whisper初期化が同じモデルへ切り替わる。Profile経由の起動では、これらの設定が解決済みreportへ記録され、Backendとadapterの双方へ渡される。
