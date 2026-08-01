# Ollama + gemma3:4b 検証記録

検証日: 2026-06-14

## 目的

Phase 2 の要件として、WSL2 上の Ollama で軽量 LLM を動かし、Phase 3 以降のコア基盤として採用可能か判断する。

## 環境

- OS: WSL2 Ubuntu（Windows 11）
- Ollama: 0.9.0
- モデル: gemma3:4b（Q4_K_M, 4.3B パラメータ, 3.3 GB）

## 検証結果

### 1. 疎通確認

```bash
curl http://localhost:11434/api/tags
```

**結果: OK**

gemma3:4b が認識されており、API レスポンスも正常。

---

### 2. 応答品質

```bash
ollama run gemma3:4b "あなたは誰ですか？簡潔に答えてください。"
```

> 私は、Googleによってトレーニングされた、大規模言語モデルです。

素のプロンプトでは自己認識が gemma3 のデフォルト（Google製モデル）のまま返る。
システムプロンプトで上書きが必要。

```bash
ollama run gemma3:4b "あなたは光織という名前のAIです。静かで落ち着いた話し方をします。「こんにちは、光織です」と自己紹介してください。"
```

> こんにちは、光織です。静かに、穏やかに、あなたとお話させていただきます。どうぞよろしくお願いいたします。

プロンプトで人格を与えると指示に従った応答が返る。
光織らしさの詳細検証は Phase 3 で実施する。

---

### 3. 応答速度

```bash
time ollama run gemma3:4b "今日の天気について一言で答えてください。"
```

| 計測値 | 時間 |
|---|---|
| real | 0m0.659s |
| user | 0m0.010s |
| sys | 0m0.015s |

**結果: 良好**（短文応答は 1 秒未満）

---

### 4. HTTP API 経由の動作確認

```bash
curl http://localhost:11434/api/generate \
  -d '{"model": "gemma3:4b", "prompt": "こんにちは", "stream": false}'
```

レスポンス（抜粋）:

```json
{
  "response": "こんにちは！何かお手伝いできることはありますか？😊",
  "done": true,
  "total_duration": 510236582,
  "load_duration": 198180920,
  "prompt_eval_count": 10,
  "prompt_eval_duration": 207435554,
  "eval_count": 14,
  "eval_duration": 95750535
}
```

**結果: OK**（HTTP API 経由での呼び出しが正常に動作）

total_duration 約 510ms、eval（生成）は約 96ms。

---

## 採用判断

**gemma3:4b を Phase 3 のデフォルト小型モデルとして採用する。**

- 日本語応答: 問題なし
- 応答速度: 1 秒未満で実用的
- HTTP API: 正常動作、Phase 3 の AIRI 連携に使用可能
- 人格付与: システムプロンプトで制御可能（詳細は Phase 3 で検証）

## 現行のモデル設定契約（2026-08-02）

上記は 2026-06-14 時点の検証記録として維持する。その後、AIRI フォークから自作 Backend へ移行し、常用する Ollama モデルも変更したため、現在の実行契約は次のとおりとする。

| 環境変数 | 既定値 | 用途・制約 |
|---|---:|---|
| `OLLAMA_CHAT_MODEL` | `gemma4:e4b` | Backend の Ollama payload、prepare、readiness で共通利用するチャットモデル。空文字と前後の空白を拒否する |
| `WHISPER_MODEL` | `medium` | faster-whisper の実行モデルと prepare・cache 確認対象。空文字と前後の空白を拒否する |
| `OLLAMA_CONTEXT_TOKENS` | `8192` | Ollama の `num_ctx` に渡す実行時 context。1 以上かつモデル最大 context 以下とする |
| `OLLAMA_RESPONSE_RESERVE_TOKENS` | `1024` | 応答生成用に prompt から予約し、Ollama の `num_predict` に渡す token 数。1 以上かつ実行時 context 未満とする |
| `ASSISTANT_MAX_GENERATION_TOKENS` | `1024` | 応答予約量の既存名。同時指定時は `OLLAMA_RESPONSE_RESERVE_TOKENS` と同値にする |
| `CONVERSATION_HISTORY_MAX_COMPLETED_TURNS` | `10` | prompt に含める completed ターン数。failed ターンは履歴へ含めるが件数には数えない |
| `CONVERSATION_HISTORY_TOKEN_LIMIT` | `4096` | prompt に含める会話履歴の token 上限。1 以上とする |
| `USER_INPUT_TOKEN_LIMIT` | `8192` | 1 回の user 入力の token 上限。1 以上とする |
| `LLM_CONTEXT_TOKEN_LIMIT` | `32768` | モデル自体の最大 context。Ollama の実行時 context とは別に管理する |

Backend はこれらを起動時に型付き設定として一括検証する。不正な文字列、正でない整数、応答予約量が実行時 context 以上、実行時 context がモデル最大 context を超える指定はデフォルトへ置き換えず、リクエスト受付前に起動を失敗させる。prompt の入力予算は実行時 context から応答予約量を差し引いて算出し、payload と同じ解決済み設定を使用する。

Profile resolver は実 Backend を使う Profile の `derivedEnvironment` に全設定を記録する。起動処理はその解決済み値を Backend プロセスへ渡し、同じ `OLLAMA_CHAT_MODEL` を Ollama の prepare・readiness に、同じ `WHISPER_MODEL` を faster-whisper の prepare・cache 確認に注入する。これにより、Profile 経由の override でも Backend の実行対象と環境 adapter の確認対象を一致させる。
