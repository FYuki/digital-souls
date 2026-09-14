# Irodori-TTS差し替え要件（#329）

## 状態・目的

2026-09-13の要件整理で合意した実行用指示書。epic上で共有サービス、CCV選択、
LiveKit接続を実装中。実音声受入、モデルの本採用を完了した記録ではない。
実装の運用・設定は[共有Irodoriサービス](../infra/irodori/README.md)を参照する。
進捗・依存・完了条件は[Epic #329](https://github.com/FYuki/digital-souls/issues/329)、
設計理由は[TTSと参照音声のADR](decisions/tts-engine-reference-voice-2026-09.md)、
用語は[用語集](glossary.md)を参照する。

VOICEVOXからIrodori-TTSへ設定で差し替えられるようにする。既存の
「LLM deltaを文・節で分割 → 区間ごとに合成 → 完成した区間からLiveKitへ送信・再生」
を再利用し、全文の生成・合成完了を待つ方式へ戻さない。

## 対象範囲と制約

- 作者提供のIrodori-TTS-ServerをUbuntu-dogfood上の共通GPU推論サービスとして導入し、
  Ubuntu-devからもHTTPで利用する。コード・モデルのrevisionを記録する。
- 初期評価モデルは `Aratako/Irodori-TTS-v4.1-Small` の非量子化版。本採用は実測後にユーザーが判断する。
- CCVで `voicevox` / `irodori` と各エンジンの声を管理する。
  接続先URL・タイムアウト等はBE configに置き、環境固有の接続先をCCVへ埋め込まない。
- 既存VOICEVOX CCVを引き続き利用可能にし、整数speaker IDとIrodoriのvoice IDをそれぞれ検証する。
  未知のエンジン、不正な声設定、参照音声の欠落は原因を識別できるエラーにする。
- 固定の検証用参照音声を登録し、保存先・voice ID・再起動後の再利用方法を整備する。
  本採用する声の選定は独立した関連[Issue #330](https://github.com/FYuki/digital-souls/issues/330)が扱う。
- 区間単位の通常APIを基本とし、返却音声を既存のLiveKit用PCMへ正規化する。
  response ID・sequence・本文範囲、順序、重複防止、キュー上限を維持する。
- dev/testは共通推論サービスのprocessを所有・停止しない。会話履歴、SQLite、Chroma、
  data root、設定、既存の音声・モデル・backupを保護し、環境ごとの分離を維持する。
  [環境分離ADR](decisions/local-dogfood-environment-2026-08.md)と[テスト方針](testing-policy.md)を適用する。

## 観測可能な動作の受入条件

以下は今回の合意を実装・検証へ渡す条件であり、実行済みテストではない。
「会話の再開始」は新しいConversation Sessionの開始であり、永続スレッドの作り直しではない。

```gherkin
Feature: Irodoriの利用準備と設定の一貫性

  Scenario: 起動時の準備を終えてから音声会話を利用可能にする
    Given Irodoriサービスが起動しモデル準備が完了していない
    When Irodoriを使用する音声会話を開始しようとする
    Then モデル読み込みとウォームアップ合成の両方が成功するまで開始できない
    And 準備失敗を利用可能として扱わない
    And 初回準備時間は通常会話の待ち時間とは別に記録される

  Scenario: 設定変更を新しいSessionから反映する
    Given 会話Sessionがエンジンと声を選択して開始している
    When CCVのエンジンまたは声を変更する
    Then 実行中のSessionと同じSessionの一時切断からの再接続では変更前の設定を使う
    And Sessionを終了して新しく開始すると変更後の設定を使う
    And VOICEVOXへの明示的な切り戻しも同じタイミングで反映する

  Scenario: voice IDの参照先を保持する
    Given voice IDに固定の参照音声が登録されている
    When 声を変更するため別の参照音声を登録する
    Then 既存IDの参照音声を上書きせず新しいvoice IDとして登録する
    And 既存IDと音声の対応を保持して比較と切り戻しに利用できる
    And サービス再起動後も同じIDから同じ参照音声を利用できる

Feature: 共通推論サービスの混雑と失敗

  Scenario: 待機中のdogfood要求を優先する
    Given 合成が実行中でdevとdogfoodの有効な要求が待機している
    When 次に処理する要求を選ぶ
    Then 待機中のdogfood要求を優先する
    And 優先順位だけを理由に実行中の合成を中断しない
    And 待機件数と実行中件数と待機時間には上限がある

  Scenario: TTSが応答の途中で失敗する
    Given 応答の文章生成と区間単位の音声合成が進んでいる
    When TTSがタイムアウトまたは接続失敗または不正音声または合成失敗になる
    Then その応答を失敗として終了し残りの文章生成と音声再生を停止する
    And 失敗を表示し文章だけで続きを生成しない
    And 別エンジンへ自動切り替えしない
    And 次の利用者発話から再び応答を試みられる

  Scenario: キャンセル後の遅延音声を採用しない
    Given 割り込みまたはSession終了または切断により旧応答が無効になっている
    When 旧応答の合成結果が遅れて到着する
    Then その音声を配信または再生しない
    And サーバー側推論の実停止とHTTP待機取消と結果破棄を区別して検証する
```

上限の具体値は未確定であり、実測に基づく設定案を実行側へ委ねる。
既存のキュー上限に加え、サーバー側推論を即時停止できない場合も要求を無制限に積まない。
失敗済み応答の停止は会話Session全体の終了を要求するものではない。

## 実測・実接続受入・モデルの判断

[音声品質計測](voice-quality-measurement.md)の指標・clock・集計・既存基準を適用する。
TTFAは「利用者発話終了からクライアントのキャラクター音声再生開始まで」とし、
p95の既存上限2000msを自動的に緩和しない。

- サービス起動から準備成功までを初回準備時間として記録する。
  通常会話のTTFAは準備済み状態で測り、コールド時の準備時間と混在させない。
- TTS処理開始から最初の区間の合成完了までを記録し、STT・LLM・TTS・送信／再生待ちを区別する。
- モデル・コードrevision、参照音声、試行数、条件、p50／p95、失敗・欠測を残す。
  devの競合なし測定とdev／dogfoodの同時利用を分け、競合条件を記録する。
- 同等条件のVOICEVOX比較、LLM・STT同時稼働時のVRAM・負荷、
  キャンセル後の後続会話への影響を検証する。
- 固定の検証用参照音声で読み間違い、声の一貫性、区間間の無音・途切れ・自然さを試聴する。
- 実Backend・STT・LLM・Irodori・LiveKit・ブラウザ再生を通して、
  連続会話、全文完成前の区間再生、割り込み、再接続、VOICEVOX回帰の証跡を残す。
  モック、health、ファイル生成だけを実会話受入の代替にしない。
- 非量子化版の最終採用は実測結果を提示してユーザーが判断し、判断で必要になった対応を完了する。
  量子化版への変更は必要性と比較結果を示して別途判断する。未達・未実測を明記する。

検証には#330で選定されepicへ統合された `miori-b3-4221` を使用する。
[光織の音声設定](../characters/miori/voice.md)と参照合成用metadataを正本にする。
2026-09-13のユーザー指示により#330は単独で閉じず、#329と同時にクローズする。
モデルの最終判断は#329の完了条件に含める。

## 実行側への委任と検証事項

| 区分 | 内容・制約 |
|---|---|
| 実装判断 | adapter、設定schemaの具体形、登録・保存・起動手順、優先順位の実現箇所は既存境界を調査して設計する |
| 実測に基づく設定案 | 待機件数・実行中件数・タイムアウトの具体値。上限とdogfood優先を満たし、後続会話への影響を記録する |
| 未検証の外部能力 | 採用revisionでの推論実停止、モデル準備確認、再起動後の参照音声再利用。HTTP取消だけで実停止を断定しない |
| ユーザーの判断 | 実測後のモデル最終採用。#330における本採用音声の選定とは別 |
| 実装・受入の進捗 | 本文書は設計記録。個々の実装、試聴、性能達成は#329の証跡で確認する |

要件判断の確認元はmain `dfeaf12`の
[CCV loader](../backend/app/characters/loader.py)、
[Conversation Core](../backend/app/conversation_core/session.py)、
[区間分割](../backend/app/conversation_core/segmentation.py)、
[LiveKit接続](../backend/app/livekit_transport/production.py)。
上記commitの確認結果と、後続のepic実装の状態を区別する。外部API仕様は採用revisionで再確認する。

## スコープ外・引き継ぎ

本採用音声の生成・選定、独自Voice Design／試聴／アップロード／声管理UI、SSE追加実装、
新しい文分割、LoRA／Speaker Inversion、動的感情・韻律制御、STT差し替え、
LiveKitやConversation Sessionの再設計、自動フォールバック、品質基準の緩和は含めない。

実装順序と子Issueの管理は#329を正本とする。worktreeを用い、作業PRをCI成功後にepicへ統合する。
main向け差分はCodeRabbitレビュー・指摘修正を行い、mainへのマージはユーザーが実施する。

## 実GPU検証記録

[2026-09の実GPU検証](irodori-tts-validation-2026-09.md)に、固定設定の100試行・VOICEVOX比較・共有サービスの取消と実障害回復・実会話診断を記録する。IrodoriのTTFA目標未達、音質受入とモデル最終判断の未完了を区別する。
