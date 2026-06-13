# インフラ方針

## 基本方針

`digital-souls` のインフラは、常時稼働する軽量サーバーと、必要時のみ使う高性能計算資源に分ける。

## 最終構成

```text
┌────────────────────┐
│ Mac mini            │
│ 常時稼働サーバー      │
├────────────────────┤
│ AIRI               │
│ Ollama             │
│ 軽量LLM             │
│ Whisper（音声入力）  │
│ PostgreSQL         │
│ Qdrant             │
│ Discord/Web UI     │
└─────────┬──────────┘
          │
          │ API / RPC
          │
 ┌────────▼─────────┐
 │ WindowsメインPC   │
 │ RTX搭載           │
 ├──────────────────┤
 │ 大型LLM           │
 │ Whisper（動画編集用）│
 │ ComfyUI           │
 │ 配信処理           │
 └──────────────────┘

          │
          │ Windows未起動時など
          ▼

 ┌──────────────────┐
 │ Cloud GPU / VM    │
 │ 一時的な重処理      │
 └──────────────────┘
```

## Mac miniの役割

Mac miniは将来的な常時稼働サーバーとして扱う。

主な役割:

- AIRIまたはコアエージェントの常時稼働
- 軽量LLMの実行
- 記憶DBの保持
- Whisperによる音声入力処理（常時稼働用途）
- 農業日誌・レシピ管理などの生活支援ツール
- Discord Bot / Web UI
- 推論ルーター
- WindowsメインPCやCloud VMへの処理委譲

## WindowsメインPCの役割

WindowsメインPCは、重いAI処理と配信処理を担当する。

主な役割:

- 大型LLM
- Whisper（動画編集・高負荷処理用途）
- 画像生成
- ComfyUI
- VRM配信
- OBS
- 必要時のみ起動する高性能ワーカー

## Cloud GPU / VMの役割

Cloud GPU / VMは、WindowsメインPCが起動していない場合や、ローカル資源で不足する場合の代替先とする。

主な役割:

- 大型LLM推論
- 一時的なGPU処理
- 重いバッチ処理
- 緊急時の代替実行先

## 初期開発構成

Mac mini調達までは、WindowsメインPCのWSL2を開発環境として使用する。
各サービスはWSL2上に直接インストールして運用する。

```text
WindowsメインPC
├─ VSCode
└─ WSL2 Ubuntu
    ├─ digital-souls 開発環境
    ├─ AIRI
    ├─ Ollama
    ├─ PostgreSQL
    ├─ Qdrant
    └─ Redis
```

## ローカルLLM方針

常時応答用には軽量LLMを利用する。

候補:

- Gemma 4B級
- Qwen 8B級
- Llama 8B級

重い推論や高精度回答は、WindowsメインPCまたはCloud VMへ委譲する。

## インフラ判断

- 常時稼働には省電力・静音性を重視する
- GPU常時稼働は避ける
- Mac miniは人格・記憶・軽量推論の家とする
- WindowsメインPCは高性能な作業場とする
- Cloud VMは必要時の外部ワーカーとする
