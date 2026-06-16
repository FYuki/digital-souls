# AIRI Docker方針 (2026-06)

## 決定内容

**AIRIはDockerに含めない。WSL2上で直接起動するサイドカー構成とする。**

---

## 検討経緯

### Docker対象の判断軸

「CI/CDで検証できるものをDockerに含める」という方針を前提とした。

### AIRIのアーキテクチャ調査

moeru-ai/airi のアーキテクチャを調査した結果、以下が判明した。

- AIRIは`server-runtime`（Hono製WebSocketサーバー）を中心とした分散システム
- Web・デスクトップ・Bot等の各クライアントはWebSocket経由でserver-runtimeに接続する設計
- Discord Bot・Minecraft Bot等の外部連携サービスもサイドカー的に接続する構成
- 公式にはDocker非対応（Dockerize対応Issueが存在）

### 人格反映の方式確認

AIRIは**Character Card V3（CCV3）フォーマット**を採用しており、人格・システムプロンプト・
会話例・モデル設定をすべて外部JSONファイルで定義できる。

AIRIのソースコードへの改変は不要であり、カードJSONを渡すだけで光織の人格を反映できる。

### Dockerに含めない判断根拠

- AIRIは外部接続を前提としたサイドカー構造であり、Dockerに入れる必然性がない
- ソースコードの改変が不要なため、フォーク・ビルドの管理コストが発生しない
- Live2D・音声デバイス・WebGPU等のGUI/デバイス依存コンポーネントを内包しており、
  Dockerに入れると設定が複雑になる
- Ollama（`localhost:11434`）との接続も、WSL2上直接起動であれば`localhost`のままシンプルに保てる

### 接続構成

```
WSL2（直接起動）
├─ AIRI server-runtime（WebSocket）
└─ Ollama（HTTP: localhost:11434）

digital-souls コア（WSL2直接起動）
└─ AIRI server-runtimeにWebSocketで接続
    └─ OllamaにHTTPで接続
```

---

## 追加決定事項

**AIRIの導入方式：別途クローン（サブモジュールなし）**

- `git clone --branch vX.Y.Z` でバージョン指定してWSL2上にクローン
- digital-soulsリポジトリにはAIRIのコードを含めない
- 採用バージョンはセットアップスクリプトまたはドキュメントに明記して管理
- AIRIのコードには手を入れない（upstream参照方針）。改修が必要な場合はPRを出す

**採用しなかった方式：サブモジュール**

- AIRIのコードに手を入れない前提ではサブモジュールで縛る必要がない
- サイドカーとして起動するだけであれば過剰

## 保留事項（Phase 3で確認）

- AIRIとdigital-soulsコアの接続ポート番号
- Character Cardのインポート方法（UI操作 / API）
- `postHistoryInstructions`による動的振る舞い調整
- 採用するAIRIの具体的なバージョン番号

---

## 関連

- `docs/decisions/docker-policy-2026-06.md` — Docker全体方針
- `docs/development-environment.md` — 開発環境構成
- `characters/miori/` — 光織人格設定（Character Card作成の参照元）
- `Agent.md` — 技術スタック（AIRI: Phase 3で検証）
