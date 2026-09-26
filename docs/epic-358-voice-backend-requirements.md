# #358 音声会話判断のBackend集約 要件指示書

## 2026-09-19：後続の実施先

#358の導入作業はmain反映・クローズ済み。残る品質・計測・実接続／移行・レビューは
[#424](https://github.com/FYuki/digital-souls/issues/424)で追跡する。
#350/#423と共通の開始準備・段階評価・PR分割は[共通実行指示書](voice-quality-350-423-424-requirements.md)を参照する。
本書の既存機能契約・原受入条件と当時の状態記録は保持する。

## 状態・目的

採用済み要件。実装・実接続受入は未完了であり、本書のコミットをM1〜M6の完了とは扱わない。
親Issueは[#358](https://github.com/FYuki/digital-souls/issues/358)。
BEが会話としての振る舞いを決定し、Web／Desktopが同じ会話制御を使えるようにする。
FEは音声入出力、ユーザー操作、端末で観測した事実を担当する。

## 根拠と優先関係

- #358本文の責務・既存操作・非スコープを基礎とし、2026-09-14〜15の対話で移行方式、音声欠落、遅延回帰基準を追加合意した。
- 設計判断は[Backend集約ADR](decisions/voice-backend-authority-2026-09.md)、用語は[用語集](../CONTEXT.md)を参照する。
- 文書作業の分岐元は2026-09-15確認のorigin/main `07b8b1ee638ca222afe61983ebaf17c6edaaf1f7`。これは性能baselineの実測済みcommitを意味しない。
- 同commitの`frontend/src/lib/audio/utterance-detector.ts`には`silenceMs=600`、`neuralSilenceMs=700`がある。古いADRの1,400msと区別し、有効設定・モデル・preroll・検証済み挙動はM1で改めて棚卸しする。
- 同mainには#329のIrodori対応が含まれる。既存のSession単位TTS選択・区間合成・取消契約を保持する。Irodoriの採用・声選定・品質改善を#358の前提に追加しない。

M1の棚卸し・protocol・計測条件は[移設契約](voice-backend-migration-contract.md)を参照する。

## 対象範囲と要件

### BEが所有する判断

認証済みmicrophone trackをAudioTransportで連続PCMへ変換し、transport非依存の音声入力処理へ渡す。
BEが発話開始・終了、正式なutterance、入力世代、STT確定、相槌／take-turn、応答開始・保留、生成取消を所有する。
session、音声活動、STT、response、playbackの状態を分離し、VAD終了をSTT確定・応答開始と同一視しない。

無音を含む連続音声をVADへ入力し、STT向けtrim後音声を流用しない。sample rate、channel、frame長、resampling、
短発話の補助根拠、candidate／confirmed／ended／misfire、preroll、resetを棚卸しし、現行の役割を保つ最小構成へ移す。
設定はBEに置き、既定挙動の移設と感度チューニングを混ぜない。モデル推論状態・bufferはsessionごとに分離する。

音声sample／media timelineを用い、到着遅延・jitter・frame未着を利用者の無音へ読み替えない。
復元不能な音声欠落があった発話は破棄し、話し直しを案内する。対象発話の遅延STT／preview結果も不採用にする。
通常の遅延との区別と欠落閾値は実行側へ委ね、採用根拠を記録する。
track終了・交換、mute、再接続、不正音声、長発話・長い無音を扱い、保持時間・PCM byte・待機数の上限と通知を設ける。
VAD初期化失敗を準備完了と表示せず、FE判定への暗黙fallbackをしない。

### 既存Core・STT・TTSへの接続

既存capture、preview、確定STT、共通User Input、相槌分類、LLMの文・節分割、区間TTSを再利用する。
二重区間化・二重STT・二重turnを防ぐ。共有Whisperのcapacity・timeout・service ownershipを維持し、previewを無制限に並列化しない。

生成中と生成完了後の再生中の双方を対象に、BEが実再生通知とresponseを対応づけて停止対象を決める。
FEへの停止通知はprovider内部計算の停止・取消ACKを待たない。取消不能な処理でも旧結果を提示せず、
入力世代とresponse有効性でSTT／preview／LLM／TTSの遅延結果を除外する。
完了と取消の競合、重複event、連続submitで終端・冪等性を維持する。
実行済みToolの副作用は生成取消で取り消されたと扱わない。

### FEに残す責務

マイク権限・取得設定・track、端末エコーキャンセル、manual mute、focus／thread switchによる抑止、
再生キュー、対象検証、実再生範囲・停止・decode失敗の通知、接続エラー、UI表示を維持する。
マイク取得・publishをFE VADのロードと切り離し、正式utterance生成と会話用speech通知の依存を外す。
補助インジケーター用VADを残しても正式判断に使わない。音声session未開始・マイクOFF時の取得・送信を追加しない。

抑止は端末で即時適用し、BEでも入力ゲートを適用する。media／control順序逆転や再開時に旧音声を新発話へ変えない。
focusだけでは応答・再生を中断せず、focus前に終了した発話と未終了captureを区別する。
blur／送信成功でmanual muteやthread switch muteを解除しない。
同じSessionへのtext submitはFEで即時local stopし、BEで先行未確定音声を無効化する。
別スレッドへの通常text送信では元Sessionの入力・応答・再生を取り消さない。
消音と会話中断を分離する。共通clientへDOM／Tauri依存を持ち込まない。

### 履歴・再接続・privacy

生成済み本文、配信済み音声、実再生範囲を分け、中断履歴は既存の連続した実再生済みprefixで保存する。
確定済み入力・保存済み履歴は保持する。再接続で古い音声を再送・途中復元せず、重複発話や意図しないマイク再開を防ぐ。
textの配送ACKと受理結果を分け、結果不明を自動成功・別入力としての自動再送に変えない。
音声・非公開会話本文を通常log／DB／計測artifactへ追加保存しない。上限付き一時bufferと既存privacy処理後の履歴境界を維持する。

## 移行契約

FE／BEを一組で更新・切り戻しし、旧clientとの互換期間は設けない。
更新時の会話終了とブラウザ再読み込みを許容し、旧Sessionの無停止引継ぎは要求しない。
非対応clientはbootstrapで明示拒否する。1 session内の判断主体を1つに限定する。

`contracts/voice-session/voice-session.schema.json`を正本として、生成型、runtime validation、
送信者検証、fixtureを更新する。Core protocolとLiveKit private frameのversionを区別し、番号と具体的な移行手順はM1で固定する。
切り戻しでも保存済み履歴を破棄せず、FE／BEを整合した版へ戻す。データ互換性はM1で確認し、操作手順と証跡はM6で残す。

## 遅延回帰と検証条件

同条件で比較できる指標ごとに、移設前p95をB（ms）、移設後p95をA（ms）とする。

`A > B + max(B × 0.10, 50ms)`

を本Epicで解消する遅延回帰とする。等号は「超える」に含めない。
これは#150のWebSocket baseline比較とは別に追加する移設前後の基準であり、元の絶対目標・比較目標を置き換えない。
誤停止率・見逃し率・失敗率・欠測・CPU／memoryへこの許容幅を流用しない。
#350で許容済みの既存未達と今回の新規回帰を分け、性能最適化全体を取り込まない。

M1で移設前commit、有効設定、モデル、音声fixture／境界、network条件、warm-up、試行数・分母、
再試行／除外規則、時計相関・誤差を事前固定する。既存試験に定義済みの試行条件を再利用する。
利用者発話終了→実再生開始、利用者発話開始→実再生停止の起点をBE検知時刻へ置き換えない。
異なるhostのmonotonic時計を直接減算せず、clock相関、media位置、fixtureの正解境界を使う。
p50／p95と処理内訳、誤停止・見逃し・失敗・欠測、CPU／memory・共有推論への影響を記録する。
比較不能・欠測・未実行は理由と分母を残し、合格扱いにしない。

## 受け入れ条件

以下は解釈差が結果を変える主要シナリオ。網羅的な確認項目と担当は親・子Issueで管理する。

```gherkin
Feature: BEを正本とする音声会話

  Scenario: FEの発話検知を使わず会話する
    Given FE VADとFEのspeech通知を無効にしたclientが音声Sessionに接続している
    When 利用者が通常の音声会話を行う
    Then 実BE VADからSTTと応答生成と実再生まで3往復以上成立する
    And Session開始後の追加操作を必要としない

  Scenario: 復元不能な音声欠落を通知する
    Given 発話取得中に通常の到着遅延とは区別された復元不能な音声欠落がある
    When BEが対象発話を破棄する
    Then 話し直しを案内する
    And 対象発話の遅延STTやpreview結果を新しい入力として採用しない

  Scenario: 相槌では再生を継続する
    Given キャラクターの回答を再生している
    When BEが利用者の発話を相槌と判定する
    Then 旧回答の生成と再生を継続する

  Scenario: 生成完了後の再生にも割り込む
    Given 生成が完了した回答をFEが再生している
    When BEが利用者の発話をtake-turnと判定する
    Then 対象回答の再生を停止して新しい入力へ進む
    And 後発responseを誤停止しない

  Scenario: 同じSessionのテキストを優先する
    Given 先行音声のSTTが未確定で旧回答を再生している
    When 利用者が同じSessionへテキストをsubmitする
    Then FEはBEやproviderの取消完了を待たず即時再生停止する
    And BEは先行未確定音声を無効化してテキストを処理する
    And 確定済み入力と保存済み履歴を保持する

  Scenario: 入力抑止解除で旧音声を復活させない
    Given 入力抑止通知とmediaの到着順が逆転している
    When 入力抑止が解除される
    Then 旧世代の音声を新発話として採用しない
    And 残っているmanual muteやthread switch muteを解除しない

  Scenario: 中断履歴に未再生部分を含めない
    Given 回答音声の一部だけをFEが実再生している
    When 回答が中断される
    Then 既存privacy処理を適用した連続した実再生済みprefixを中断履歴とする
    And 配信済みだが未再生の部分を再生済みと補完しない

  Scenario: 旧clientを新しい音声契約へ接続させない
    Given FEとBEが一組で新しい契約へ更新されている
    When 非対応の旧clientがbootstrapを要求する
    Then 明示的に拒否する
    And FE判断への暗黙fallbackをしない
```

既存unit／module／mocked E2Eを更新し、各段階で担当範囲を検証する。
M6では実LiveKit／Whisper／LLM／VOICEVOX／ブラウザを用い、現在選択可能なTTSの共通契約も回帰確認する。
固定WAVによる自動試験と人の実マイク・聴感確認を分ける。後者が未実施なら人の受入済みと記録しない。
health成功やテスト用speechイベントの注入は実BE VADの受入証拠にしない。

## 子Issueと依存関係

基本順序はM1 → M2 → M3 → M4 → M5 → M6。各段階の局所検証をM5／M6へ先送りしない。
M1は設計・基準確定であり、今回の文書コミットだけで完了しない。M2以降がschema／生成物／runtimeを段階的に整合させる。
旧WebSocket baselineを新契約へ無断で置き換えず、最終的に旧LiveKit clientとの互換期間を設けない。

| 段階 | 子Issue |
|---|---|
| M1 | [#393 [design][voice] BE音声判断移設の現状基準・protocol・計測契約を確定する](https://github.com/FYuki/digital-souls/issues/393) |
| M2 | [#394 [feat][voice] Backendで連続PCMからVAD・発話区間を管理する](https://github.com/FYuki/digital-souls/issues/394) |
| M3 | [#395 [feat][voice] BE発話境界をSTT・Core・割り込み・入力世代へ統合する](https://github.com/FYuki/digital-souls/issues/395) |
| M4 | [#396 [refactor][voice] Frontendの会話用VAD依存を解除し共通入出力clientへ整理する](https://github.com/FYuki/digital-souls/issues/396) |
| M5 | [#397 [test][voice] BE音声判断の計測相関・回帰判定と既存試験を更新する](https://github.com/FYuki/digital-souls/issues/397) |
| M6 | [#398 [test][voice] BE音声判断を実サービス・ブラウザで受け入れ移行手順を整備する](https://github.com/FYuki/digital-souls/issues/398) |

## 制約・非スコープ・委任

[テスト方針](testing-policy.md)に従い、dev／test専用Profile・data rootを使う。
共有Whisper／VOICEVOX／Ollama／Irodoriやdogfoodのサービス・実データをテスト準備／cleanupから停止・破壊しない。

外部Realtime API、ネイティブ音声モデル、STTモデル変更、TTS差し替え・声選定、相槌分類器の全面刷新、
先行duck／pauseの新UX、Desktop本体、Live2D、複数端末・キャラクター調停、自発発話、記憶再設計、
音声永続保存、新しい日本語ベンチマーク・別評価基盤は対象外。
#319の残受入、#279のUI統合、#329の採用、#100／#291の記憶改修を新たな完了待ちにしない。
#317の旧FE判断に関する計画は新契約との対応を明記して引き渡す。

内部クラス名、実行ライブラリ、event名、version番号、buffer／欠落閾値、ファイル分割は、
上記の観測可能な要件を変更しない範囲で実行側へ委ねる。閾値・測定条件・採用根拠はM1と担当Issueへ残す。

作業はworktreeで行い、作業ブランチから`epic/358-voice-backend`へのPRをCI成功後に統合する。
main向け差分はCodeRabbitレビューと必要な修正を行い、mainへのマージはユーザーが実施する。deployは別の明示依頼で扱う。
