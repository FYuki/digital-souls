# backend

digital-souls の自作バックエンド（FastAPI）。

- Ollama（gemma4:e4b）への接続
- キャラクター（`characters/`）のロード
- `GET /` のヘルスチェック
- `POST /chat` のチャット応答 API

実装は [#10](https://github.com/FYuki/digital-souls/issues/10) から開始する。

## セットアップ

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 起動

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload
```
