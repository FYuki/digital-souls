# システムアーキテクチャ

## 基本思想

`digital-souls` では、AI人格の本体を「表示・配信システム」ではなく、「人格・記憶・判断・ツール実行」に置く。

表示形態は用途に応じて切り替える。

- 日常利用: 静止画UIまたは軽量チャットUI
- 通常の視覚表現: Live2D
- 配信・イベント時: 必要に応じてVRM
- 重い推論: WindowsメインPCまたはCloud VM

## 全体構成

```text
                     User / Viewer
                          │
                          ▼
                  Input Interface
          Chat / Voice / Discord / Web UI
                          │
                          ▼
                 digital-souls Core
                          │
        ┌─────────────────┼─────────────────┐
        │                 │                 │
        ▼                 ▼                 ▼
   Personality         Memory             Tools
 characters/        RAG / DB        Farming / Recipe
        │                 │                 │
        └─────────────────┼─────────────────┘
                          ▼
                  Inference Router
                          │
        ┌─────────────────┼─────────────────┐
        │                 │                 │
        ▼                 ▼                 ▼
   Local LLM         Windows PC        Cloud GPU/VM
 Mac mini/Ollama     Heavy models      Fallback worker
                          │
                          ▼
                 Output Controller
                          │
        ┌─────────────────┼─────────────────┐
        │                 │                 │
        ▼                 ▼                 ▼
   Static Image          Live2D              VRM
  Personal UI       VTube Studio      3tene/Warudo/etc.
```

## AIRIの位置づけ

AIRIは、人格・記憶・エージェント制御のコア候補として扱う。

想定する役割:

* 会話制御
* 人格定義の適用
* 記憶の参照
* ツール呼び出し
* 感情・表情制御の判断
* 外部UIへの出力制御

AIRI本体を直接大きく改造するのではなく、まずは `digital-souls` 側で人格・記憶・ツール定義を管理し、必要に応じてAIRIへ接続する。

## 表示・配信レイヤー

### 基本

* Live2Dを標準の姿とする
* パーソナルAI用途では静止画UIも許容する
* VRMは配信時や3D表現が必要な場合のみ利用する

### Live2D

候補:

* VTube Studio
* OBS連携
* 将来的なAPI制御

### VRM

候補:

* 3tene
* Warudo
* VNyan
* VSeeFace
* Unity + UniVRM
* Three.js + three-vrm

VRMは常用ではなく、配信・イベント用の身体として扱う。

## 推論ルーター

推論処理は用途に応じて振り分ける。

```text
small:
  provider: local
  target: Mac mini / Ollama
  purpose: 日常会話、記録、軽い相談

medium:
  provider: windows
  target: WindowsメインPC
  purpose: 高精度回答、長文推論、重めの処理

large:
  provider: cloud
  target: Cloud GPU/VM
  purpose: Windows未起動時の代替、大規模推論
```

## 記憶・ツール設計

初期ツール候補:

* 農業日誌
* アレンジレシピ管理
* メモ管理
* タスク管理
* 配信ログ
* キャラクター記憶

記憶は人格ごとに分離できるようにする。

```text
characters/
└─ miori/
   ├─ personality.md
   ├─ world.md
   └─ memory-policy.md  # 方針本文と実装設定への案内
```

光織の記憶方針本文は `docs/decisions/miori-memory-policy-2026-06.md`、実装が参照する機械可読な設定値は `backend/app/memory/memory_policy.json` で管理する。
