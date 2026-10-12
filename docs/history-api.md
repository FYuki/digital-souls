# 会話履歴API

通常の`create_app()`と既存`/v1/chat/completions`、`/v1/character/completions`はstatelessです。
履歴保存・復元は[ADR 0004](adr/0004-conversation-history.md)の明示的な別経路です。

## 有効化と信頼境界

組込み利用者が`create_app(inference, history_store=store, history_policy=policy)`へ
HistoryStoreと信頼されたHistoryPolicyを注入します。保存adapterは
`digital_souls_core.postgres_history.PostgresHistory`です。[接続手順](postgresql.md)を参照してください。policyなしでは全操作を既定拒否しますが、
同じscopeの削除は可能です。HTTPからpolicyを設定したり、安全と自己申告したりする機能はありません。
単一のローカル利用者がloopback上で使用します。既存Host/Origin境界を適用します。

秘密・機微情報のprivacy検査は[privacyガイド](privacy.md)に従って明示的に有効化できます。
分類器の実モデル精度は未評価です。合成テスト以外の私的実会話を自動取り込みしないでください。

## 経路

共通prefixは`/v1/characters/{character_id}/conversations`です。

| Method | Path | 動作 |
| --- | --- | --- |
| POST | prefix | 空の会話を作成し201。本文は不要 |
| GET | prefix | このcharacter/scopeの`conversation_ids`を作成順に取得 |
| GET | prefix/{conversation_id} | `conversation_id`・`revision`・順序付き`messages`を復元 |
| DELETE | prefix/{conversation_id} | 会話と全turn/receiptを削除。存在しないIDにも204 |
| POST | prefix/{conversation_id}/turn-deletions | 選択した往復だけ、または選択した往復以降を削除して200 |
| POST | prefix/{conversation_id}/completions | 新規入力を追加して推論し、完了した往復を原子的保存 |

completion本文の例（合成データ）:

```json
{
  "request_id": "synthetic-turn-1",
  "expected_revision": 0,
  "messages": [{"role": "user", "content": "合成テストの挨拶"}],
  "stream": false
}
```

`messages`には新しいuserまたはtoolのみを1件以上渡します。件数上限はありません。
systemを含む入力は400 `system_message_unsupported`で拒否します（文字列・text配列、streamとも同じ）。
それ以外の不正なturnは400 `invalid_turn`、schema不正は400 `invalid_request`です。
拒否に要求本文を含めず、system拒否はprivacy判定・履歴読み取り・推論より前に行います。
過去ログを再送しません。保存済み履歴と新規入力の合計にも件数上限はありません。
入力`content`は文字列、または`[{"type":"text","text":"..."}]`の配列です。
入口で各textを`\n`で連結し、1要素ならそのtextそのものになります。
要素は`type`・`text`だけを許可し、typeは`text`のみ、textは厳密な文字列です。
text以外のtype、未知キー（cache_control等）、空配列、非object要素、非文字列text、
配列以外の非文字列contentは400です。未知部分を黙って捨てません。
privacyスキャン・保存・推論送信・既存1 MiBバイト検査は正規化した文字列へ適用します。
復元と応答のtext contentも文字列で、assistant tool callのnullは従来どおりです。
保存済み履歴やprovider応答の解析で配列を受理する変更ではありません。
tools/tool_choice/temperature/max_tokens/max_completion_tokensの条件は既存APIと同じです。
request_idは1〜64文字の英数字・`_`・`-`、期待revisionは0以上の整数です。
成功は`conversation_id`、`request_id`、確定`revision`、公開assistant `message`、
`finish_reason`（stop/tool_calls）を返します。OpenAIのresponse envelopeとは異なります。
成功応答（stream・非stream）の `X-Character-Card-SHA256` headerは、その保存済み応答の
組み立てに使ったCardファイルのSHA-256です。`turns.card_sha256` にassistant応答を含む
turn単位で保存し、receiptの `card_sha256` へ復元します。会話復元の `turns` 配列はrevision順の
`{"turn_revision": 1, "card_sha256": "..."}` metadataを返します。会話ID一覧から各会話を
復元して確認できます。空の会話は `turns: []`、削除したturnのmetadataも同時に消えます。
このmetadataはproviderのmessagesへ含めません。要求・completion本文・fingerprintは変えません。
再送と同時要求の後着は、保存済みreceiptのハッシュを返します。再起動後にCardが変わっても
現在値で上書きしません。旧turnのハッシュは不明なNULL（復元時はJSONのnull）のままとし、
そのreceiptの再送ではheaderを省略します。現在のCardから補完せず、再推論もしません。

tool_callsを受け取ったら外部Agentが実行し、次のrequest IDで対応する全tool resultを渡します。
Coreは実行しません。不正な往復は400です。入力・設定が同じrequest IDの再送は同じreceiptを返し、
再推論しません。fingerprintはcontent正規化後に計算するため、同じtextを文字列と
配列で再送しても同一入力です（例：`"one\ntwo"`と2要素のtext配列）。stream等の他項目は
従来どおりfingerprintに含みます。異なる入力のID再利用、期待revisionのずれは409です。
同時要求は推論コストが重複し得ます。409後は復元して次の入力を判断してください。

## streamと失敗

`stream: true`は上流streamをバッファし、完了・policy許可・commit後に
SSEの`event: completed`（dataは上記成功オブジェクト）と`data: [DONE]`を返します。
逐次token配送ではありません。provider完了後、保存直前にcancelの受付点を設けます。
ASGIの`http.disconnect`をそこで観測済みなら保存せず、PostgreSQL commit区間はawaitなしで実行します。
実際のネットワーク切断時刻とASGI通知の到着時刻は同一とは限りません。
commit後に到着した通知や配送失敗では保存済みreceiptが残ります。
保存完了後に配送が途切れた場合は同じ本文とrequest IDで再試行してください。
stream値も再試行fingerprintに含みます。復旧時に切り替えないでください。

履歴のバイト上限は1 MiBです。入力・推論payload・streamバッファ・保存後の履歴を
JSONのUTF-8バイト数で検査し、超過は切り捨てず413（`history_limit`）で拒否します。
[ADR 0026](adr/0026-prompt-assembly.md)の `prompt_budget` を共通のPromptBuilderで適用し、
送信する古い履歴は往復単位で削減します。tool callと結果を分けず、直前の完了した1往復を保護します。
固定人格・今回の入力・保護する往復だけで超過する場合は400 `prompt_budget_exceeded`
（messageは領域名だけ）です。保存済み履歴と1 MiB保存上限は維持します。
`context_budget_bytes` または `prompt_budget.caller_instructions` が残る運用設定は起動時エラーです。
組み立て・token推定・既定予算は[推論API](api.md#文脈プライバシー)を参照してください。
推論先の物理context制限も引き続き適用します。
会話streamのtool call indexは非負の整数（boolean不可）で、新しいindexは0から連続する必要があります。
既存indexへの後続deltaは受け付け、tool call数の固定上限は設けません。
policy欠落・判定不能・拒否は403、不明会話は404、不正上流応答は502です。
保存失敗は成功やDONEとして返しません。エラー本文へ入力・秘密値を反映しません。
削除は進行中推論を自動キャンセルしませんが、その後の保存を拒否します。

policyには保存前、復元、送信前、完了後の再確認があります。削除はpolicy撤回後にも
使えます。既定は保存履歴の同一会話内での直接再利用です。正本の明示登録・参照検索は
[記憶API](memory.md)で利用でき、構造化抽出・自動形成は未実装です。既定では接続しません。
意味検索は正本のEpisode・Semanticと有効なFactを使います
（[ADR 0022](adr/0022-memory-retrieval-from-records.md)、
[SPEC §2.4](../SPEC.md#24-検索と会話での利用)）。実モデル品質は未検証です。


## 同時再試行と初回DB作成

同一requestの同時推論で別の応答が得られても、先に保存したreceiptを維持します。
後から完了した要求も、実際に返す先行receiptを現在のread policyで再確認します。
許可が撤回されていれば403とし、保存済みの内容を別の応答へ書き換えません。

初回schemaのDDLとversion設定は単一transactionです。初期化途中にprocessが終了しても、
PostgreSQLのrollback後に再初期化できます。未知のversionや部分schemaは
自動修復・削除せず拒否します。既存データを識別せず上書きする移行は行いません。


## 明示的な記憶対象とthread操作

[ADR 0006](adr/0006-conversation-memory-controls.md)の操作表を適用します。
completionに`memory_excluded_indices: [0]`を渡すと、今回の`messages`内のindex 0を
履歴に保存したまま記憶対象外にします。indexは0始まりで重複・範囲外を拒否し、件数上限はありません。
省略時は空配列で、旧receipt fingerprint互換を維持します。再送で指定を変えると409です。
このmetadataはモデルへ送信しません。自然文だけから指定期間や操作を推測しません。

`PATCH /v1/characters/{character_id}/conversations/{conversation_id}`へ
`{"expected_revision": 1, "private_mode": true}`または`{"expected_revision": 1, "archived": true}`を渡します。
両fieldの同時指定も可能です。少なくとも一方が必要で、revision不一致は409です。
変更もrevisionを進めるため、旧revisionで進行中の推論は保存できません。
通信失敗後はGETで現在の状態を確認してください。PATCHにrequest IDによるreceiptはありません。

通常一覧はarchiveを除外します。`?include_archived=true`で含められます。
archive中もID指定のGET・DELETEと記憶sourceの有効性は維持します。
private中は全sourceを利用不可にし、その間に保存したturnは解除後も自動採用しません。
除外履歴に依存し得る後続assistant/toolも対象外です。新user発話は明示指定に従います。

GETレスポンスには`private_mode`、`archived`、message順の`memory_sources`を追加します。
各sourceは`turn_revision`、そのturn内の`message_index`、`eligible`、`stated_at`を持ちます。
`stated_at`はturnを保存したUTC時刻のISO 8601文字列（例：`2024-02-29T12:34:56.123456+00:00`）です。
同じturn内のuser・tool・assistant messageには同じ保存値を対応させます。移行前のturnは
不明として`null`を返し、現在時刻で補完しません。GET・PATCHのsnapshotで同じ値を返します。
Message本文やモデル向けmessagesの形式には日時を加えません。
conversation IDとtrusted scope/characterと組み合わせて参照します。現在のconversation revisionと
過去のturn revisionは異なり得ます。eligibleはsource条件だけで、privacyや候補採用の許可ではありません。

DELETEは本文・receiptを消し、内容なしのsource削除通知を同じtransactionで残します。
trusted consumer向けPython portの`deletions(binding)`と`acknowledge_deletion(binding,event_id)`で処理します。
scope違いの通知は取得・ackできません。通知のHTTP ackは公開しません。
同じPostgreSQL schemaにある派生memory本文も同一transactionで消去します。再構成の完了とは別です。
source参照自体は削除直後から無効です。

PostgreSQL schema v1〜v6からv7へ段階的に原子的移行します。schemaとversionを同じtransactionで
更新し、途中失敗はrollbackして再実行できます。履歴・receipt・fingerprintは保持し、旧turnの
日時はNULLのままです。v5→v6で旧逐語3表を削除し、確認状態・通知・正本表は保持します。
v6→v7でnullableな `turns.card_sha256` を追加し、既存turnはNULLのまま補完しません。
[移行の詳細](postgresql.md#初期化と復旧境界)を参照してください。未知versionは引き続き拒否します。
過去発話への後付け除外変更は未実装で、指定はcompletionの入力配列に対して行います。

trusted構築コードから`PostgresHistory(database, clock=clock)`へ、timezone付きdatetimeを返す時計を注入できます。
既定は保存時の現在UTC時刻です。新規turnごとに一度取得してUTCへ正規化し、timezoneなしの値は
拒否します。再送・read・thread操作では取り直しません。HTTP入力から時計や日時は指定できません。
正本の `Citation` は元turnのrevision・message index・epoch・話者・文字範囲を参照し、
逐語原文や抽出時刻を複製しません。`PostgresMemoryRecords` に時計を注入する引数はありません。
正本のcreated_at・last_user_mentioned_atはtrusted callerが記録に渡す値です。


### Stage3の撤回処理

private化は、そのthread由来の既存memoryを削除する製品仕様として承認済みです。
複数sourceの正本は直ちに利用停止し、保存文・構造化本文を消去します。
形成・再生成は未実装で、残る適格sourceから再生成できない期間を許容します。
解除による旧memoryの自動復活は行いません。PATCHはepoch更新・派生本文消去・別のmemory撤回通知を
原子的に確定します。正本・依存結果の撤回と未実装範囲は[記憶API説明](memory.md)を参照してください。


## 保存拒否の確認

現在のuser発話のcontentだけを既存scannerのno_history・no_memoryで検査します。
assistantの引用・説明、tool結果、過去履歴は新しい確認の対象にしません。
検出だけでprivate化や履歴削除は行いません。非stream応答とSSE completedのdataに、
検出時だけ次の内容なしの信号を追加します（合成例）。

```json
{
  "memory_confirmation": {
    "sources": [{"turn_revision": 1, "message_index": 0}],
    "private_mode": {
      "method": "POST",
      "path": "/v1/characters/synthetic/conversations/synthetic-id/memory-confirmations"
    },
    "delete_history": {
      "method": "DELETE",
      "path": "/v1/characters/synthetic/conversations/synthetic-id"
    },
    "delete_turns": {
      "method": "POST",
      "path": "/v1/characters/synthetic/conversations/synthetic-id/turn-deletions",
      "scopes": ["selected", "following"]
    }
  }
}
```

本文・検出語は信号へ含めません。モデルpayloadとrequest fingerprintにも確認metadataを
加えません。GETおよび操作後snapshotのmemory_confirmations配列で未回答の参照を復元できます。
未回答のsourceはeligible=falseで、正本登録・検索・送信直前の出典再検証でも拒否します。
未回答には期限がなく、再起動・再送で解除しません。保留に依存し得るassistant/toolも除外します。

`POST prefix/{conversation_id}/memory-confirmations`へ次を渡します。

```json
{
  "expected_revision": 1,
  "turn_revision": 1,
  "message_index": 0,
  "accept_private_mode": false
}
```

expected_revisionは現在の会話、turn_revisionは対象発話の保存revisionです。
1回につき1発話へ回答し、成功で会話revisionを進めたsnapshotを返します。
古いrevision、存在しない対象、回答済み対象は409で、部分更新しません。
旧revisionで進行中の推論commitも拒否します。認可は既存controlsと同じpolicy・Bindingです。

trueは既存private化を行い、epoch更新・そのスレッド由来のmemory本文消去・撤回通知を
回答と同じtransactionで確定します。private解除後も旧memoryと受入発話は復活しません。
falseは対象発話の保留だけを解除します。明示除外・private状態・他の未回答は維持し、
通常のprivacy審査を引き続き適用します。どちらも履歴本文・stated_at・source識別を変更しません。
履歴削除はユーザーが削除範囲を選び、案内されたAPIを明示的に呼ぶ別操作です。
`delete_history`は会話全体のDELETE、`delete_turns`は往復単位のPOSTを案内します。
`delete_turns.scopes`の`selected`は選択した往復だけ、`following`はその往復とそれ以降を
対象にします。往復の選び方とtool対応による範囲の拡張は[往復単位の明示削除](#往復単位の明示削除)を
参照してください。信号は案内だけで、AIやCoreが履歴を自動削除することはありません。

PostgreSQL v3では独立したturnのmemory_confirmation列へ検出indexと回答を保存します。
旧turnの確認状態は空で、過去本文を再走査しません。旧NULL日時、既知日時、履歴、receipt、
fingerprintを保持します。同じrequestの再送では回答後も元receiptの信号を返し、
再推論・再保留しません。回答の通信失敗後はGETで未回答一覧と現在revisionを確認してください。

## 往復単位の明示削除

GETの`memory_sources[].turn_revision`で保存済み往復を選び、
`POST prefix/{conversation_id}/turn-deletions`へ次を渡します。

```json
{
  "expected_revision": 3,
  "turn_revision": 2,
  "scope": "selected"
}
```

`selected`は選択した往復だけ、`following`はその往復とそれ以降の保存済み往復を対象にします。
会話全体の削除は既存DELETEです。自然文の検出から削除を実行しません。
assistantの構造化`tool_calls[].id`とtool roleの`tool_call_id`の対応が往復をまたぐ場合は、
call側・result側の両方向へ連鎖をたどり、対応する往復をまとめて削除します。
`following`でも対応のために選択より前の往復を含む場合があります。本文やtool名の一致では拡張しません。

成功は本文なしの`conversation_id`、更新後の`revision`、実際に削除した`turn_revisions`配列です。
削除は会話revisionを1進めます。残す往復は再採番せず、日時・確認状態・private・明示除外・
receiptを保持します。部分削除後も会話を継続でき、後続の適格な往復から記憶を形成できます。
不正入力は400、未知・別Bindingの会話は404、revision不一致・存在しない／削除済み往復は409です。
同意撤回後も既存DELETEと同じtrusted Bindingで削除できます。

対象turn/receiptを物理削除し、`turn_tombstones`にはBinding・会話・request ID・元turn revisionだけを
残します。本文・fingerprintは残しません。同じrequest IDは元入力の再送でも別入力での再利用でも
`request_deleted`の409となり、再推論・保存しません。append内でも検査します。
会話全体DELETEではこの印も消えます。削除前のsnapshotから進行中の推論をcommitしようとすると、
JSON・streamとも既存のrevision検証で409となり、新receiptは保存しません。

専用`turn_deletions` outboxは正確な対象revision集合を永続化します。
trusted Python portの`turn_deletions(binding)`は`TurnDeletion(event_id, conversation_id, turn_revisions)`を返し、
`acknowledge_turn_deletion(binding, event_id)`で処理後にackします。別Bindingから取得・ackできず、
重複ackは無作用です。HTTPのackはありません。既存SourceDeletionの`through_revision`は会話全体の
削除だけに使い、その形と意味を維持します。

PostgreSQL v4で削除済み印と往復通知表を追加します。PostgreSQL v1〜v3は版別に既存schemaを
検証して移行します。DDL・version更新は原子的で、旧本文・日時（NULLを含む）・確認状態を変更しません。
削除のrevision更新・印・対象派生本文のNULL化・通知・履歴DELETEも同一transactionです。
途中失敗は全更新をrollbackし、再試行できます。


## 形成drainのlocalhost HTTP入口

[Issue #208](https://github.com/FYuki/digital-souls-core/issues/208)の形成台帳を
trusted起動コードが `create_app(..., formation=formation)` へ注入した場合だけ、
`POST /v1/memory/formation/drain` を追加します。未注入ではroute/OpenAPIとも存在せず404です。
注入するformation・history_store・history_policy・Inference.privacyは同じstore/policyを共有し、
不一致や履歴未注入は起動時に拒否します。予約の有効化は `open_storage(..., formation_policy=policy)`
または `PostgresHistory(..., formation_policy=policy)` で明示します。設定ファイルの存在だけで有効になりません。

path・上限入力・件数応答の具体形は#208の**仮置き・要確認**です。
bodyは `{}` または `{"max_jobs": 5}` です。`max_jobs` は厳密な正の整数で、
起動時の `FormationConfig.max_jobs` を増やせません。未知field・Binding自己申告・bool・文字列・
上限超過は本文なしの400です。応答例は
`{"status":"success","succeeded":1,"failed":0,"excluded":0}` です。
完了したdrainはHTTP 200で、1件以上の試行失敗は `status:"failure"` にします。
未接続processorは503 `formation_processor_unavailable`、DB障害は既存の本文なしstorage errorです。
結果にjob ID・会話本文・prompt・LLM出力・例外文字列を含めません。

既存 `LocalHTTPBoundary` を必ず通り、loopback以外のHostを400、同一Origin以外を403で拒否します。
Originなしのtimer/CLI clientは利用可能です。Coreはloopbackにbindする単独利用者用で、
この境界は認証ではありません。今後、別インフラの画面接続用の認証を導入する際は、drainを
**管理者向けの経路**として扱います。認証・timer同梱・MCPは#208の範囲外です。
Python API・設定の既定値と仮置き判断は[記憶の形成job](memory.md#形成jobの台帳と有限drainissue-208)を参照してください。
