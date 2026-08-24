# Post-MVPエンハンス計画 再編 (2026-07)

状態: **ACTIVE**（Wave 3の音声会話方針を2026-08-24に更新）

## 概要

MVP（テキスト+音声チャット、RAG基盤）の完了を受け、コードベースを調査し直した結果、
旧ロードマップのPhase 5〜8として積んであったタスク列挙よりも先に着手すべき現状ギャップが
複数見つかった。旧タスクを白紙化し、`docs/roadmap.md` / `docs/enhancement-plan.md` に
Wave 1〜4構成として再計画した。本ファイルはその意思決定の記録。

## 背景: 白紙化の経緯

旧ロードマップのPhase 5（長期記憶RAG）〜Phase 8（常時稼働化）は、AIRIフォーク検討時点
（2026-06）に立てたタスク列挙であり、その後の自作BE/FE実装を経た現状のコードとの対応が
薄くなっていた。改めてコードを調査したところ、以下のギャップが判明した。

- 会話が完全ステートレスで、直前のやりとりすらプロンプトに含まれず多ターン会話が成立しない
- RAGは実装済みだが `RAG_ENABLED=false` がデフォルトで機能していない
- SQLiteが `character` カラムのままで、`docs/decisions/archive/Multi-character-db-2026-06.md` の
  決定事項（全レコードに `character_id` を付与する）と不整合
- LLM/TTSが逐次処理（全文生成待ち→一括合成）で体感遅延が大きい
- 音声チャットがターン形式のままで、双方向・割り込み可能な会話になっていない
- モデル名のハードコード、`ClaudeClient` 未実装、card.jsonの一部フィールド未使用

これらは「長期記憶RAGを仕上げてから配信連携へ」という旧Phase構成では拾いきれない粒度の
問題であり、特に「会話が続かない」「RAGが眠っている」は、旧Phase構成の枠外にある基盤課題
だったため、タスク列挙そのものを見直すことにした。

## 決定事項

### 1. 優先方向: 記憶・会話の質 ＞ 音声リアルタイム性 ＞ ツール・運用

post-MVPの取り組みを、この優先順位でWave 1〜4に再編する。

- 記憶・会話の質（Wave 1: 会話が「続く」、Wave 2: 「覚えている」）を最優先とする。
  光織との関係性・パーソナルAIとしての価値の核はここにあり、音声のリアルタイム性より
  先に成立させるべきと判断した
- 音声のリアルタイム性（Wave 3: 「自然に話せる」）はその次。現状のターン形式でも
  会話としては成立するが、双方向性がないと「自然な対話」体験には届かない
- ツール・運用（Wave 4: 「役に立つ」）は後続。農業日誌等のツール実行基盤や
  常時稼働化・配信連携は、会話の土台が固まってから着手する

### 2. Wave 3: LiveKitを採用し、双方向音声会話への再設計と統合する（2026-08-24更新）

2026-07時点では、WebSocket上で会話状態管理、LLM/TTSの逐次配信、barge-inを実装し、
その結果を計測してからLiveKitへの移行要否を判断する方針だった。

その後、目標を「1 user + 1 characterで、ターン待ちを意識せず割り込み可能な連続音声会話」と
明確化した結果、発話単位PCMと単一WAVを運ぶ既存WebSocketを完成形へ拡張してから
LiveKitへ移す二段階実装は、media delivery、再接続、再生停止を重複実装することになる。
このため、**Wave 3のrealtime media transportとしてLiveKit / WebRTCを採用し、従来のWave 3と
LiveKit対応を1つの実装計画へ統合する**。

LiveKitの責務はRoom、Participant、Track、WebRTC media、再接続に限定する。
FrontendはVADの検出主体として `speech_started` / `speech_stopped` を通知し、Conversation Coreは
そのevent contract、turn-taking、utterance確定、`should_response`、response / cancel lifecycle、
STT、LLM、VOICEVOX、履歴、privacy、記憶の意味論を所有する。LiveKit固有identityと
Conversation Coreの `session_id` / `utterance_id` / `response_id` は分離する。

音声とcontrol eventも分離する。microphoneとCharacter音声はLiveKit AudioTrackで継続配送し、
`speech_started` / `speech_stopped`、response delta、cancel等はtransport非依存contractとして
定義してLiveKitのdata/RPC等へmappingする。`speech_stopped`、utterance確定、
`should_response`、response開始は同一eventにせず、listeningとspeakingが同時に成立できる
状態モデルとする。

現行WebSocket一括pipelineは移行前baselineとして計測するが、採否判断の対象にはしない。
変更前の比較値を残すため#17のbaseline取得を最初に開始し、#13のConversation contractと
Issue #113のLiveKit基盤設計を並行する。以降はresponse世代管理、継続入力、中断履歴、逐次text/audio、
barge-in、再接続・障害回復、自動受入、dogfood受入の順に進める。

FE側の `AudioTransport` 抽象化は、Conversation Coreへtransport固有APIを漏らさない境界として
維持する。ただし、その目的はWave 3でWebSocketとLiveKitの採否を保留することではない。

### 3. RealtimeAgentは全面採用せず、設計を参照する

OpenAI RealtimeAgentやLiveKit AgentsをConversation Coreのruntime、会話状態、履歴・記憶の
正本として全面採用しない。既存のローカルSTT / LLM / VOICEVOX、Character Card、privacy、
キャラクター別記憶の境界を維持し、providerやtransportへ会話意味論を従属させないためである。

一方で、session / input / response / playbackを独立lifecycleとして扱うこと、response IDによる
世代管理、cancel、遅延eventの破棄、speech stopとresponse開始の分離といった設計上の考え方は
参照する。参照した概念もdigital-soulsのtransport非依存contractとして定義し直す。

### 4. 複数キャラクター会話はEpic CとしてWave 3から分離する

複数キャラクター会話はWave 3の初期受入へ含めず、Epic C（#114）で別管理する。
Wave 3は1 user + 1 characterのConversation CoreとLiveKit transportを完成させ、Epic Cはそれを
再利用して次の順で進める。

1. User + 光織 + 葵のテキストグループチャットで、speaker、宛先、発話順、応答調停を確立する。
2. 共有会話eventとCharacter別episodic memoryを分離し、記憶形成・想起を検証する。
3. 確立済みのConversation CoreをLiveKit Roomへ接続し、複数Characterの音声会話へ拡張する。

これにより、複数参加者の会話意味論と記憶モデルを音声transportから独立して先に検証する。
Epic Cは現時点ではEpic Issueだけを作成し、子IssueはWave 3完了後にPhase 1から分割する。

### 5. Issueとの対応と着手順

- #106: Wave 3親Epic。LiveKit採用、1 user + 1 character、Conversation Coreとの責務分離を管理する。
- #17: 旧WebSocket baselineとLiveKit受入目標を定義する。LiveKit採否は再判断しない。
- #13 / #113: transport非依存contractとLiveKit Room・認証・mappingを並行して確定する。
- #107 / #108 / #109: response世代管理、継続microphone/VAD、中断履歴・privacy・記憶整合性を実装する。
- #14 / #15 / #16: transport非依存control event、Character AudioTrack、barge-inへ接続する。
- #110 / #111 / #112: 再接続・回復、自動受入、LiveKit dogfood受入を完了する。
- #114: Wave 3とは別枠で、テキスト会話、Character別記憶、LiveKit音声統合を段階実装する。

推奨着手順は次とする。

```text
#17 baseline開始
  └─ #13 + #113
       └─ #107 / #108 / #109
            └─ #14 → #15 → #16
                 └─ #110・runtime安定化 → #111 → #112
                      └─ #114
```

### 6. 旧Phase → Wave 対応

| 旧Phase | 内容 | 移行先 |
|---|---|---|
| Phase 4（未完了分） | WebSocketの遅延baseline取得・LiveKit対応 | Wave 3（LiveKit採用済み） |
| Phase 5 | 長期記憶（RAG） | Wave 2 |
| Phase 6 | パーソナルAI機能 | Wave 4 |
| Phase 7 | 表現・配信連携 | Wave 4 |
| Phase 8 | 常時稼働化・マルチクライアント対応 | Wave 4 |

Wave 1（会話履歴のプロンプト注入・プロンプト合成の一元設計・設定のenv化）と、
Wave 3の会話状態管理部分は、旧Phaseには対応項目がない新規タスクである。
これらはコード調査で判明した現状ギャップに基づいて追加した。

## 関連

- `docs/roadmap.md` — post-MVPのWave構成と実現する機能を簡潔に記載
- `docs/enhancement-plan.md` — Wave 1〜4の詳細タスク・依存関係・設計方針
- `docs/decisions/archive/Multi-character-db-2026-06.md` — `character_id`統一の元になった初期検討履歴
- `docs/decisions/wave2-memory-formation-retrieval-2026-08.md` — Wave 2の現行記憶・記録モデル
- `docs/decisions/archive/miori-memory-policy-2026-06.md` — 初期検討の履歴ADR
- GitHub Issue #106 — Wave 3親Epicと受入条件
- GitHub Issue #13 — transport非依存の音声session contract
- GitHub Issue #17 — 会話品質指標と現行WebSocket baseline
- GitHub Issue #113 — LiveKit Room・認証・transport基盤
- GitHub Issue #114 — Epic C（複数キャラクター会話・記憶・音声統合）
