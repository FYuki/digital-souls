# backend

digital-souls の自作バックエンド（FastAPI）。

- Ollama（gemma4:e4b）への接続
- キャラクター（`characters/`）のロード
- `GET /` のヘルスチェック
- `POST /chat` のチャット応答 API
- `POST /characters/{character_id}/conversations` のスレッド作成 API
- `GET /characters/{character_id}/conversations` の利用中スレッド一覧 API
- `GET /characters/{character_id}/conversations/archived` のアーカイブ済み一覧 API
- スレッド単位の履歴取得・アーカイブ・復元・物理削除 API

アーカイブは短期会話履歴を保持したまま通常利用から外す操作であり、物理削除では
対象 conversation とその全 turn だけを SQLite から削除する。削除後、この短期会話履歴は
復元できない。SQLite 接続では `secure_delete` を有効にし、物理削除後の WAL 後処理に
失敗した場合は本文を含まない再試行情報を保存して Backend 起動時に再試行する。
アーカイブと物理削除のどちらも RAG 長期記憶は変更せず、その閲覧・訂正・物理削除と
Chroma 同期削除は Wave 2 で実装する。既存の backup、snapshot、ファイルシステム上の
複製からの消去は保証しない。

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
