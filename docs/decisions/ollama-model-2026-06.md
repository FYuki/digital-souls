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
