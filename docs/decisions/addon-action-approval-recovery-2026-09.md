# MCP操作群の承認・確認待機・結果回復 (2026-09)

## 状態・適用範囲

**ACTIVE**。#185の2026-09-11合意を記録する。外部MCPを含む共通契約であり、自作Addon限定ではない。
承認単位・効力についてCharacter Life共通ADRのHigh Impact節を部分改定する。
MCP transport、Tool選択、Autonomy Target、privacy、Core保護境界は変更しない。
実装・受入の進捗は#185と子Issue #300〜#304、後続管理UIは#305を参照する。

## 承認の正本

管理単位は `MCP接続 × 通常/ハイリスク操作群 × 対話中/会話外` とする。
Tool個別・実引数条件ごとのpermissionを作らない。
対話と会話外で承認を流用せず、接続の再連携によるidentity変更にも流用しない。
単なる再接続やCapability更新では接続単位の承認を失効させない。

接続利用許可時は通常操作群を「常に承認」、ハイリスク操作群を未承認とする。
登録時OFFとgrant検証は維持し、初期値の存在だけで接続・会話外利用を有効化しない。
会話外利用には別途Autonomy Target許可を必要とする。

| 選択 | 対話中 | 会話外 |
|---|---|---|
| 常に承認する | 同じ接続・操作群・実行場面の将来利用に保持する | 同左 |
| 一度承認する | Tool呼び出し1回分のみ | 同左 |
| 拒否する | 当該要求のみ。次回は再確認する | 設定変更まで同じ接続・操作群を使用不可 |

単回承認を活動全体へ適用しない。並行呼び出しにも最大1回分だけ適用する。
保存済みのハイリスク操作群の承認が有効なら毎回の確認は要求しない。
影響分類・privacy・Core保護・snapshot・grant・availability・budget・stopの検証は継続する。

## 二段階の影響分類

登録時にstatic classificationをCapability Snapshotへ含め、dispatch直前に解決済みbindingと
実引数を評価する。LLM単独やTool名だけで最終分類しない。
未分類の副作用はハイリスクとし、未知・非信頼annotationで通常操作へ緩和しない。
不可逆削除、force、履歴破壊、権限・security・credential変更、金銭・法的commitment、
大量操作、重要な公開範囲変更、Core自身の保護境界変更を高影響として扱う。
external-sendやpublicという属性だけではハイリスクにしない。

### Coreで確認した外部Toolの分類

実行側へ委ねられた分類ルールの具体化として、管理設定の`core_policy.impact_profiles`を使う。
これは利用者の許可設定ではなく、Core運用側が接続先の操作の意味を確認した分類情報である。
MCP本文やLLM出力から自動登録しない。

- `tool_name`とnative Tool定義全体の`definition_digest`で確認済み定義を特定する。
- `effect`はread/write/external_send/destructive/security/financial。
- `normal_arguments_schema`は通常操作と判定できる引数条件をJSON Schemaで記述する。
  対象数・操作種別・公開範囲等を制限し、意味が異なるToolへ流用しない。
- 定義が変わればunknownへ戻し、normal条件外ならハイリスクとする。
- runtimeのforce・権限変更・大量操作等の検出をprofileで解除しない。
- この分類によってeffective effect、read retry、parallel許可を緩めない。
  未信頼annotationのToolは分類profileがあってもserial・自動retryなしを維持する。

profileがない場合、信頼済みeffective readだけを通常と判断できる。
Toolの意味・引数条件を確認できなければハイリスクのまま利用者へ確認する。
Capability変更は次loopへ反映し、実行中loopの定義へ途中追加しない。

## 確認キューと活動側待機

確認要求と承認設定、元の実行状態を分離する。
会話外活動は初期60秒（設定変更可能）だけ承認を待ち、時間切れなら当該操作を使用不可として進む。
無回答を永続拒否・成功・実行済みへ変換しない。
待機終了後もキューは後から承認できる。60秒はキュー削除・承認失効の期限ではない。

後から承認しても元操作を遅れてdispatchせず、古い活動・responseを自動再開しない。
承認は以後のTool利用時だけに効く。後からの単回承認も将来の呼び出し1回分にする。
表示用preview・要求識別・arguments digestを保持しても、生payloadやsecretを会話履歴・RAG・
通常logへ保存しない。要求表示の対応づけを引数条件ごとのpermissionへ置き換えない。

保存先はruntime data rootの`addon-actions/actions.sqlite3`とする。承認状態と確認要求は
別tableに保存し、回答の重複排除と単回許可の発行・消費をtransactionで制御する。
Core起動時は旧要求の活動側待機を終了するが、要求と保存済み承認は保持する。
会話外の待機時間は`DS_MCP_ACTION_WAIT_SECONDS`で設定する（正の秒数、既定60）。
対話中の要求は既存のTool追加情報待機と同じ600秒を上限とし、画面離脱・stopでも終了する。

`GET /addon-actions/requests`はcharacter/sessionで絞った安全なpreviewを返す。
`POST /addon-actions/requests/{id}/answer`は要求主体と接続identityを確認して回答を保存する。
このAPI自体はdispatchしない。実行側が元loopの有効性を再検証し、再開時にも
binding・停止・grantと送信直前の意味privacyを確認する。MRTRの追加回答も外部送信内容として
privacy・影響判定と要求識別に含め、元引数の通常分類だけで追加回答を許可しない。

## 利用者との対話

今回のMVPはチャット内に3択を表示する。接続・操作群・実行場面と操作内容・対象を示す。
テキストとLiveKit音声会話を対象とし、音声会話中は確認を読み上げ、画面操作で承認する。
音声回答を承認へ変換しない。
Addon管理UIのキュー一覧・後からの承認・保存済み設定の管理は後続#305とする。

## 結果回復・停止

状態競合は最新状態取得とCoreの再判断へ返し、古いopaque参照を別操作へ自動変換しない。
送信後の結果不明は`RESULT_UNKNOWN`として保持し、接続先が保証した同一依頼replayまたは
状態照会だけを利用する。runtime retry/checkpoint復元や再承認を理由に副作用を再送しない。
実行済み結果の再返却と新規操作を区別する。

stopは新規state-changing callを止める。Task cancel送信だけで外部停止済みとは扱わず、
再起動後も外部正本へ状態照会する。未対応機能・照会不能を成功へ変換しない。
共通結果はAPPLIED/NO_CHANGE/DEFERRED/SUPERSEDED/CONFLICT/REJECTED/FAILED/RESULT_UNKNOWNを区別する。

## 受入の境界

実LLM・独立した実MCPを使い、テキストおよび実STT/TTS/LiveKitを通す音声の両経路を検証する。
副作用は破棄可能なテストデータで検証する。
競合・結果不明・replay・Task等は制御されたfixtureを併用し、独立serverの実接続証跡と分ける。
mock、skip、readinessを実接続完了として扱わない。具体的な受入条件・証跡は#304で管理する。
