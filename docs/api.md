# キャラクター推論API v0.1

このAPIは人格文脈付きLLMを呼ぶstatelessな推論経路です。単独のチャットUI、配信フロントエンド、
外部Agentのいずれも同じAPIを利用できます。このAPIではAgentループ、tool実行、記憶の保存、
自動技能学習は行いません。Coreはtool実行ループを持ちません。Core全体の範囲は
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
設定とカードは起動時に一度読み込みます。進行中の要求へ設定更新は入りません。

## 対応する小範囲

- textのみの`messages`（system/user/assistant/tool、1件以上・件数上限なし）。callerのsystemは
  人格systemの後に置きます（[ADR 0026](adr/0026-prompt-assembly.md)で「呼び出し側の指示」の枠へ移すことが決定済み、実装前）。人格はLLMへの指示であり、セキュリティ境界ではありません。
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

組み立ての順序、呼び出し側の system の扱い、予算は[ADR 0026](adr/0026-prompt-assembly.md)で置き換えることが決まっています（2026-10-11 承認、実装前）。
以下は現在の実装の挙動です。

元Mioriカードを変更せず、name/description/personality/scenario/system_prompt/mes_exampleを
systemに組み立てます。Loreは最新user textに対する非再帰の単純key一致で、selective時には
secondary keyも必要です。insertion_order順です。これは汎用CCv3実装ではありません。
対応しないregex/constant/case-sensitive/配置/scan modeは起動時に拒否します。
first_mes、音声extension、creator_notesはモデルへ注入しません。

カードの`token_budget`は、この版ではモデル別tokenizerを使わないため強制しません。
注入system全体をUTF-8の`context_budget_bytes`で制限し、超過は切り捨てずエラーにします。
これは物理モデルのcontext windowではありません。metadataの物理token上限は未確認のnullです。
caller履歴の総token数を保証せず、providerの上限エラーはprovider失敗として返します。

`ContextSource`は固定したCharacter（ID/version/profile）と`AccessScope`を受け取ります。
初期実装は空で保存・検索・技能学習をしません。scopeはサーバー固定のlocal-operator/local/
local-privateであり、callerによるsubject/client/audienceの指定は拒否します。
将来、公開配信や複数利用者の記憶を扱うには、認証済みsubjectと信頼できるclient用途・audienceを
確立し、検索と外部送信の両境界で認可する実装が必要です。同じキャラクターIDだけで記憶を共有しません。

## 起動

Python 3.12.3、uv 0.8.22で`uv sync --frozen`を実行してください。
サンプル[設定](../examples/characters.json)は実モデル候補の書式例であり、利用資格や能力は未確認です。
`external_send_allowed=false`・許可parameterなしなので、そのままでは送信しません。
管理者が正当な利用経路・既存認証・対応モデルを確認した後だけ、git管理外の`*.local.json`で
profile/model/送信policy/能力を設定し、providerが規定する環境変数で認証を渡してください。
APIキーをJSON、prompt、リポジトリへ保存しないでください。サブスク契約をAPI資格と推測しません。

```sh
CORE_CHARACTER_CONFIG=examples/characters.json uv run --no-sync uvicorn digital_souls_core.api:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

環境変数なしならキャラクター0件で起動します。無課金のfake/SDK mock検証は`uv run --no-sync pytest`。
fakeの定型返答は人格品質や実モデル動作の証明ではありません。
限定した実モデル試験の範囲は[検証証跡](evidence/2026-10-03-ollama-native-chat.md)を参照してください。
