# キャラクター推論API v0.1

このAPIは人格文脈付きLLMを呼ぶstatelessな推論経路です。単独のチャットUI、配信フロントエンド、
外部Agentのいずれも同じAPIを利用できます。このAPIではAgentループ、tool実行、記憶の保存、
自動技能学習は行いません。現在のCoreはtool実行ループを持ちません（会話中のツール利用をCoreが持つことは
[ADR 0027](adr/0027-tool-use-and-activity-runtime.md)で決定済み、実装前）。Core全体の範囲は
[READMEの「製品の範囲」](../README.md#製品の範囲)を参照してください。履歴・記憶は
[会話履歴API](history-api.md)・[記憶API](memory.md)の明示経路で扱います。
この版は**信頼された単一利用者のlocalhost専用**です。認証・tenant隔離・公開配信向けの
アクセス制御は未実装であり、インターネットや共有LANへ公開しないでください。

アプリはHostを`127.0.0.1`、`localhost`、`[::1]`と有効なportに制限し、重複・曖昧なHostを
body解析前に400/invalid_hostで拒否します。Originがある場合はscheme/hostname/portが
Hostと同一の場合だけ許可し、別origin・`null`・重複Originは403/origin_deniedです。
CLI等のOriginなし要求は許可します。別portのブラウザーUIや外部originを許可するCORS設定は
提供しません。same-originのローカルUIか非ブラウザーclientを利用してください。
これらはDNS rebindingやcross-origin要求への防御であり、認証ではありません。
別のローカルプロセスによる要求を識別・認可するものではなく、複数利用者・公開運用は依然未対応です。
実provider有効化前には信頼された単一利用者の端末であることと既存の利用資格を確認してください。

## 入り口

| メソッド・path | 用途 |
| --- | --- |
| POST `/v1/character/completions` | `character_id`で選択 |
| POST `/v1/chat/completions` | `model`に登録済みaliasを指定。同じapplication処理 |
| GET `/v1/models` | aliasの一覧。物理モデル一覧ではない |
| GET `/v1/characters/{character_id}` | version/profile/対応パラメータと設定済みモデル |

```json
{
  "character_id": "miori",
  "messages": [{"role": "user", "content": "こんばんは"}],
  "max_completion_tokens": 256,
  "stream": false
}
```

互換入口では`character_id`を`model`に置き換えます。内部character ID、config version、
inference profile、provider/modelは別の識別子です。返却JSONの`model`はSDKが返す実モデル表記を
保持し、aliasに書き換えません。レスポンスheaderの`X-Character-ID`、
`X-Character-Config-Version`、`X-Inference-Profile`で固定した設定を確認できます。
両completion経路の成功応答（stream・非stream）には `X-Character-Card-SHA256` も付きます。
これは起動時に読み込んだCardファイルの生バイト列のSHA-256（小文字64桁）です。改行・空白を
正規化せず、解析に使った同じバイト列から計算し、起動後にファイルが変わっても値は変えません。
`GET /v1/characters/{character_id}` も `card_sha256` を返します。要求とcompletion本文は変更しません。
Card本文はログ・エラーへ出さず、ハッシュは評価・調査で応答とCardを対応付けるために使います
（[ADR 0029 §6](adr/0029-persona-consistency-evaluation.md#6-card-のハッシュ)）。
設定とカードは起動時に一度読み込みます。進行中の要求へ設定更新は入りません。

## 対応する小範囲

- textのみの`messages`（user/assistant/tool、1件以上・件数上限なし）。
  `role: "system"` が1件でもあれば、文字列・text配列や位置を問わず400
  `system_message_unsupported`で拒否します（[ADR 0031](adr/0031-reject-caller-system.md)）。
  privacy判定・記憶検索・token計数・provider送信より前に拒否し、要求本文をエラーへ含めません。
  stream要求もSSE開始前の通常のJSON HTTPエラーです。developer等の未対応roleは従来どおり400です。
- 各役割の入力`content`は文字列、または`[{"type":"text","text":"..."}]`の配列を
  受け付けます。入口で各textを`\n`で連結し、1要素ならそのtextそのものになります。
  各要素は`type`・`text`だけを持ち、typeは`text`、textは厳密な文字列です。
  text以外のtype（image_url/input_audio/file等）、未知キー（cache_control等）、空配列、
  非object要素、非文字列text、配列以外の非文字列contentは400です。黙って捨てません。
  assistantの`content: null`と`tool_calls`の組合せは従来どおり受理します。
  privacy検査・履歴保存・推論送信・既存バイト検査には正規化した文字列を渡します。
  応答のtext contentは従来どおり文字列で、tool call時のnullも維持します。
  provider応答の解析と保存済み履歴の読み込みには配列受理を広げません。
- function型`tools`（name/description/parameters）、`tool_choice`のauto/none/required/名前指定。
  toolsは指定時1個以上・個数上限なしで、tool名は重複不可。
  tool callのID、名前、arguments文字列とtool結果は改変しません。
  argumentsをJSONとして解釈・実行しません。履歴中のcall IDは一意で、全結果が必要です。
- `stream`、`temperature`（0〜2）、`max_completion_tokens`（1〜32768）。
  旧`max_tokens`も同じ内部`max_completion_tokens`へ正規化します。両方指定は400です。
  上限はCoreの入力上限でありモデル能力ではありません。reasoningモデルでは非表示の推論tokenを
  含む場合があり、出力文字数の保証ではありません。providerへの変換はLiteLLMに任せます。
- 通常のchoices/finish_reason/usageと、messageまたはstreamのdeltaの対応fieldを返します。
  message/deltaはrole/content/tool_calls/tool_call_idに限定し、SDKのreasoning_contentや
  thinking等の拡張fieldは公開・保存しません。思考だけで終わった通常応答のcontentは空文字です。
  返却assistant messageは加工せず、必要なtool結果を続けて次のmessagesへ再送できます。
  streamのusageはproviderが送った場合のみ保持します。Coreでtoken数を捏造しません。

各parameterは管理者がprofileの`allowed_parameters`で確認済みとしたものだけを許可します。
SDKにも`drop_params=False`を指定します。未知能力や未対応parameterは成功扱いにしません。
tools履歴の再送にもtools能力が必要です。`max_tokens`の許可名は正規化後の
`max_completion_tokens`です。認証情報、API endpoint、provider設定は要求から受け取りません。

Responses、multimodal、audio、developer role、JSON mode、stream_options、parallel_tool_calls、
n、seed、logprobs、その他未記載fieldは未対応です。unknown fieldは400、unknown endpointは404。
完全なOpenAI互換ではありません。必須クライアントがResponses専用なら別途実装が必要です。

## Ollama固有の対応範囲

管理者が`ollama_chat/<model>`を設定した場合、非streamのnative Chat APIを利用します。
旧`ollama/<model>`は400/unsupported_provider_routeです。native toolsのIDと引数・結果の
関連を保持しますが、SDKは引数をJSON文字列とobject間で変換するため、空白等のバイト一致は
保証しません。`tool_choice`はSDKが削除してしまうため、auto/none/required/名前指定すべてを
400/unsupported_parameterで送信前に拒否します。黙ったJSON形式fallbackは利用しません。

固定profileの`ollama_think`だけがbooleanまたはnullを受け付けます。false/trueは上流の
`think`へ渡し、省略/nullはモデル既定値です。他providerでは未指定だけを許可し、
明示的なnullを含む設定は起動時エラーです。
callerの`think`、`ollama_think`、`reasoning_effort`は未対応fieldとして400です。
thinkingのみで出力上限に達した場合も、空contentと`finish_reason=length`を保持します。
管理者はモデルの実能力を確認してtools等を許可してください。Ollama streamingは未対応です。
設計理由は[ADR 0002](adr/0002-ollama-native-chat.md)を参照してください。

## streaming・取消・エラー

通常のSDK transportのstreamは、pinned SDKがローカルmetadataで`mode=chat`と解決する
`openai/`のネイティブChat Completions経路だけに制限します。prefixだけでは許可しません。
SDK自身と同じResponses bridge判定に実要求のtools・reasoning・api_base等を渡して送信前に判定し、
`openai/gpt-5-codex`、`openai/o3-pro`、
`openai/codex-mini-latest`などの既知Responsesモデル、`openai/responses/...`、未知modeを拒否します。
このmetadataは経路判定専用で、モデル能力・利用資格や実モデル検証の証明ではありません。
function toolsによってResponsesへ切り替わるGPT-5.4以降等も要求単位で拒否します。
endpoint解決はSDKと同じ管理者の設定順を使い、callerによるendpoint・reasoning指定は許可しません。
SDKのglobal model aliasによる経路変更も拒否します。APIの登録済みcharacter aliasとは別のものです。
LiteLLM 1.103.2のAnthropic等にはHTTP Responseのclose所有権を公開しないiteratorがあるため、
allowlistにstreamがあっても他providerは送信前に400/unsupported_stream_providerで拒否します。
OpenRouter等のOpenAI互換サービスもこの保証範囲には含めません。非streamの他providerは管理者の
能力確認が必要で、実モデル検証済みとは称しません。汎用Provider port自体は差し替え可能です。

運用者が`transport=llamacpp_chat`を指定した場合だけ、別途検証したlocal Chat経路を利用できます。
`model=openai/gemma4-12b`と`api_base=http://127.0.0.1:<port>/v1`が必須です。
このaliasのmetadata未登録は許可しますが、Responses mode、Responses prefix、SDK alias書換えは
非streamでも拒否します。他モデルや互換サービス一般への許可拡大ではありません。
CoreはSDKに固定の公開ダミー認証値を渡し、環境内のクラウドAPIキーを転送しません。
callerのapi_base/transport/api_keyは400です。管理者設定は起動中の要求に混ざりません。
[固定サーバーと運用条件](llamacpp-operations.md)および[ADR 0003](adr/0003-local-llamacpp-provider.md)を参照してください。

SSEの`data: {chunk}`と正常終了時の`data: [DONE]`を返します。最初のchunkを待ってから
HTTP 200を開始するので、それ以前の失敗は通常のHTTPエラーです。途中失敗は
`event: error`＋`data: {"error": ...}`を送り、`[DONE]`を送らず終了します。
OpenAI Python SDKで途中失敗が例外となる契約テストがあります。すべての互換クライアントの
動作保証ではなく、独自clientはerror event・異常EOFを成功として扱わないでください。
初回chunk待機中も含め、切断・取消では上流streamをcloseします。
応答開始前に検出した切断は内部的に499/client_disconnectedとして扱います。
再試行・別モデルfallbackは実装しません。

エラーbodyは`{"error":{"type":"core_error","code":"...","message":"..."}}`です。
不正入力/未対応は400、送信policy拒否は403、unknown characterは404、provider制限は429、
provider失敗は502、timeoutは504。内部例外や入力・キーをerror本文へ反映しません。
本APIは要求/応答の保存・本文ログを行いません。運用時にSDKのdebugや外部callbackを有効化しないでください。

## 文脈・プライバシー

[ADR 0026](adr/0026-prompt-assembly.md)に従い、Chat API・会話履歴 API・回答評価は
同じ `Inference.prepare` と `PromptBuilder` で provider のmessagesを組み立てます。
順序はbefore_char Lore→Card（固定人格）→after_char Lore→Runtime Context→記憶→履歴→
`post_history_instructions`→今回の入力です。Card・Lore・Runtime Contextは先頭systemへまとめ、
記憶は説明文と別のuser message内のJSON配列でデータとして渡します。
post_historyは独立したsystem messageです。callerのsystemは組み立て前に拒否します。
Runtime Contextは現在空であり、人格・関係・生活状態のsnapshotはまだ生成しません。
記憶検索は従来どおり会話IDがある経路だけのopt-inです。stateless Chat APIでは検索しません。
完成したpayloadを再認可し、送信直前にもpolicyの同一性・版と記憶の撤回guardを再検証します。
削った記憶も含む検索結果全体のguardを保持します。

元Mioriカードを変更せず、description/personality/scenario/system_prompt/mes_exampleを
「キャラクター概要」「性格と話し方」「関係と世界観」「応答方針」「会話例」の順に
`## 見出し`とstrip済み本文で組み立てます。空の要素は見出しごと省き、name/first_mes/
音声extension/creator_notesは注入しません。post_historyを持つカードも読み込みます。
embedded の `data.character_book` は [ADR 0028](adr/0028-character-book.md) が採用する
PoC の Book・Entry 契約に従って読み込みます。既知 field の欠落・型不正・制約違反は
カード内 path を含むエラーでカード全体を拒否し、未知 field と extensions は保持します。
整数 field は boolean・小数・文字列を拒否し、scan_depth/token_budget は非負です。

Lore は今回の user 発言と、同じ会話の user・assistant 履歴を新しい順に scan します。
scan_depth は今回の発言を含む message 数（省略時1、0はscanなし）で、履歴の予算削減より前に適用します。
会話履歴 API は保存済みの privacy 処理済み履歴、stateless Chat API は ADR 0026 の分割後の履歴を使います。
system・tool の本文、記憶、Card、選んだ Lore は scan しません。比較は NFKC 正規化後の message ごとの
部分一致で、case_sensitive が true でないときだけ casefold します。空の key は一致しません。
enabled、空の本文、Decorator、regex、constant、primary key、selective の secondary key の順で判定します。
同じ Entry は source index で1件にまとめます。

constant/case_sensitive/position/scan_depth/priority/token_budget を解釈します。
use_regex=true、recursive_scanning=true、Decorator を含むカードも読み込めますが、regex と
行頭の空白除去後に @@ で始まる行を含む Entry は選ばず、再帰 scan は行いません。
standalone Lorebook、regex、再帰、Decorator の実行は対応範囲外です。
各 Lore は `## キャラクターLore` と strip 済み本文を持ち、before_char は Card の前、after_char
（省略時を含む）は後ろへ、各 position 内は `(insertion_order, source_index)` 昇順で配置します。
0件なら Book がないカードと同じ messages です。Lore は記憶データや memory_reference に変換しません。

Book の token_budget は各 Entry を独立した system message にして、その profile の token counter で数えます。
省略時は Book 固有の削減なし、0は全除外です。estimate でも推定式を適用します。
上限超過時は `(effective_priority, insertion_order, source_index)` の小さい Entry から丸ごと除外して再計測し、
priority 省略時の effective_priority は insertion_order です。Lore 領域・全体予算の削減も同じ除外順です。
診断は source index・reason code・position・token数・件数だけで、本文・keys・name・id・comment は含みません。
同梱 Miori の単独質問の選択は従来と同じですが、複数一致が推定700 tokenを超える場合は Entry が外れます
（[合成検証と回帰の証跡](evidence/2026-10-11-character-book.md)）。

運用設定 `prompt_budget` はtotal/character/lore/memory/history/current_input/
post_historyの正の整数です。boolean・文字列・小数は拒否します。
既定は順に28672/16384/8192/16384/24576/16384/4096です。
全体の既定値は32768から出力用4096を予約した値で、領域上限の合計が全体上限になるわけではありません。
`memory_floor`・`history_floor`は正の厳密な整数で、いずれも既定3です。
旧 `context_budget_bytes` または `prompt_budget.caller_instructions` が残る設定は起動時エラーです。
カード内の `token_budget` は運用設定の代わりにはなりません。

profile の `token_counter` は `estimate`（既定）または `llamacpp` を明示します。
`estimate` は `ByteTokenCounter` で、本文のUTF-8バイト数（tool callの名前・arguments、tool定義を含む）
+ message数×16 + 16をtokenの上限として推定します。
`llamacpp` は `transport=llamacpp_chat` だけで許可し、その他のtransportや未知の値は起動時エラーです。
profile の検証済みloopback `api_base` と同じサーバーの `/apply-template` へmessages・tools・tool_choiceを送り、
返ったpromptを `/tokenize`（add_special/parse_special=true、with_pieces=false）へ送ってtoken ID配列の長さを使います。
固定サーバーのChat Completionsと同じtemplate parserを使い、tool定義はtemplateに含めます。
全体のtoken数は、推論へ送る完成したmessages・toolsを変更せず正確に数えます。
領域別では、会話またはtoolsを含む領域を空のuserメッセージで前後から包み、同じ空枠だけ
（toolsなし）を数えた値との差を領域のtoken数にします。履歴がassistantから始まる場合も、
assistantだけ・userを含まない会話を領域単独でtemplateへ送りません。
空枠は計数専用で、推論や保存履歴には加えません。toolsの費用はcurrent_inputに含めます。
空の領域は0、systemだけの領域とBookのtoken_budgetは従来どおりsystemだけで数えます。
領域の上限はこのtemplateによる増分に対して判定し、推定への切替は行いません。
templateの結合やtoken境界の影響で差が負になる場合は0にします。領域の合計と全体は一致しません。
計数はasyncで、空枠の基準値も含め、同一build内の同一messages/toolsの成功結果だけをキャッシュします。
接続失敗・不正な応答・redirectは502 `token_count_failed`、timeoutは504 `token_count_failed`です。
推定へ切り替えず、推論と会話履歴のappendを行いません。cancelは伝播します。
proxy環境変数を無視し、redirectを追跡せず、計数前にも実際の本文と権限を検査します。
本文・template結果・token IDをログやエラーへ出しません。
実サーバーのIT2によるtemplate受入・実token数の照合はNOT RUNです。
[合成試験の証跡](evidence/2026-10-11-llamacpp-token-count.md)を参照してください。
全体超過では、低順位の記憶を下限まで→記憶を1件まで→古い履歴を
直近N往復まで→最後の記憶→直近N往復のうち保護する1往復より古い履歴→Lore→post_historyの順に削ります。
領域超過ではその領域の同じ削減単位を適用します。本文を途中で切らず、tool callと結果の組は分けません。
固定人格（ContextSource拡張を含む）・今回の入力（toolsを含む）・直前の完了した1往復は必ず残し、
これらだけで超過する場合は400 `prompt_budget_exceeded`、messageには領域名だけを返します。
最初のuserより前のassistant等は削除可能な1単位で、完了往復には数えません。
履歴の下限Nは完了した往復の数で、不完了の単位は下限に含めません。
userのない要求や末尾assistantも従来どおり受理します。
履歴の削減は送信payloadだけに適用し、保存済み履歴を削除しません。

領域のtoken数と削減件数は内部の `Prepared.usage` に保持し、HTTPへは出しません。
`GET /v1/characters/{character_id}` のmetadataでは `context_budget_bytes` を廃止し、
`prompt_budget`（totalと6領域のobject、`caller_instructions`を廃止）・`memory_floor`・`history_floor`を返します。
completion成功応答の形は変えません。物理モデルcontext上限のmetadataは未確認のnullのままです。
運用者は推論先contextと出力予約に合う全体上限を設定してください。

`ContextSource`は固定したCharacter（ID/version/profile）と`AccessScope`を受け取ります。
初期実装は空で保存・検索・技能学習をしません。既存の信頼済みmemory/skill拡張文字列は
Cardの応答方針へ追加し、character予算に含めます。Runtime Contextの状態snapshotには使いません。scopeはサーバー固定のlocal-operator/local/
local-privateであり、callerによるsubject/client/audienceの指定は拒否します。
将来、公開配信や複数利用者の記憶を扱うには、認証済みsubjectと信頼できるclient用途・audienceを
確立し、検索と外部送信の両境界で認可する実装が必要です。同じキャラクターIDだけで記憶を共有しません。

## 起動

Python 3.12.3、uv 0.8.22で`uv sync --frozen`を実行してください。
サンプル[設定](../core/examples/characters.json)は実モデル候補の書式例であり、利用資格や能力は未確認です。
`external_send_allowed=false`・許可parameterなしなので、そのままでは送信しません。
管理者が正当な利用経路・既存認証・対応モデルを確認した後だけ、git管理外の`*.local.json`で
profile/model/送信policy/能力を設定し、providerが規定する環境変数で認証を渡してください。
APIキーをJSON、prompt、リポジトリへ保存しないでください。サブスク契約をAPI資格と推測しません。

```sh
cd core
CORE_CHARACTER_CONFIG=examples/characters.json uv run --no-sync uvicorn digital_souls_core.api:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

環境変数なしならキャラクター0件で起動します。無課金のfake/SDK mock検証は`uv run --no-sync pytest`。
fakeの定型返答は人格品質や実モデル動作の証明ではありません。
限定した実モデル試験の範囲は[検証証跡](evidence/2026-10-03-ollama-native-chat.md)を参照してください。
