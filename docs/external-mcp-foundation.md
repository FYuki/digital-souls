# 外部MCP利用基盤（#104）

`backend/app/external_mcp/`は、登録済み外部MCPのdiscoveryと選択済みoperationの実行を提供する。
会話からのTool選択・LLM向けprojection・binding解決・応答への統合は#182で実装する。
現在の会話画面へMCPを自動接続する設定やHTTP管理APIは追加しない。

## 構成と呼出順

1. 管理側で`Connection.from_manifest()`に`contracts/addon/manifest.schema.json`形式を渡す。
2. `Registry.register()`でconnection instanceを登録する。
3. `ExternalMCPClient.connect()`の内側で`ExecutionGate.attach()`を開く。初回discoveryがstagedになる。
4. `begin_loop(ExecutionContext(...))`で検証済みstagedをactivateし、そのloopのsnapshotと接続世代を固定する。
5. `invoke()`または`read_resource()`へoperation、引数、loop IDを渡す。
6. loop終了時は`end_loop()`、接続終了時はcontext managerを閉じる。

```python
import json
from app.external_mcp import (
    Connection, Registry, ExternalMCPClient, ExecutionGate, ExecutionContext,
)

async def execute_registered_tool(manifest_path, tool_name, arguments):
    connection = Connection.from_manifest(json.loads(manifest_path.read_text()))
    registry = Registry()
    registry.register(connection)
    gate = ExecutionGate(registry)
    client = ExternalMCPClient(connection)
    async with client.connect(), gate.attach(connection.id, client):
        loop = gate.begin_loop(ExecutionContext(character_id="miori", session_id="session-1"))
        try:
            return await gate.invoke(connection.id, tool_name, arguments, loop)
        finally:
            gate.end_loop(loop)
```

この例は単発実行用。継続runtimeではRegistry/GateをCoreが共有し、3層rate limitをconnectionや
sessionごとに別インスタンスへ分散させない。Manifestやloop IDの発行は管理側の責務であり、
外部MCP・LLM・Frontendへ直接公開しない。`CapabilitySource`の実装も信頼するCoreコードに限定する。

## 設定・identity

- `connection.id`をconnection instance IDとして使用する。同一URLでも異なる認証主体は別IDにする。
- 外部stdioとStreamable HTTPを対応範囲とし、HTTP認証は`none`またはBearerの`secret_ref`だけ。
- Bearerは各HTTP送信直前に`SecretResolverPort`から解決する。既定実装は環境変数を参照する。
- URLのuserinfo・query・fragmentは拒否する。localhost以外のHTTPはTLS必須。redirectへ追従しない。
- `expected_identity`のTLS issuer/auth principalの独自照合は未対応として拒否する。HTTPSはHTTP clientの標準証明書検証を利用する。
- endpoint、command/args、認証設定の変更は`relink_required`となり旧connectionを停止する。新しいIDで管理側が再登録する。
- trust・sharing・restrictionの変更も既存登録の上書きではなく再登録する。
- `serverInfo`やserver instructionsはsecurity identityにも上位命令にも使用しない。
- stdioはSDKの最小環境継承を使い、親processの全環境変数を渡さない。引数へcredentialを埋め込まない。
- self-owned、trusted addon metadata、stable operation IDのruntime解釈は#221へ延期する。

## Snapshot

native Tool定義全体、input schema、annotations、`_meta`、Resources、Promptsを保存する。
Toolのdefinition revisionはconnection ID・Tool名・input/output schema digestで構成する。
snapshot revisionはprotocol、native定義、trustとeffective policyを含む内容digestとする。
JSON文字列による不変な保存形式を使い、取得したdocumentの編集はRegistryへ反映されない。

`refresh()`はpaginationを含むdiscovery全体を検証してstagedへ保存する。現在loopへ追加・削除・
schema変更を途中反映せず、次の`begin_loop()`でactivateする。失敗したrefreshは旧snapshotを
残してdegradedとなる。切断時はunavailable、再接続時は再discoveryと新世代を要求し、
古いloopやcached snapshotだけでは実行できない。

JSON Schemaの外部参照は実行時に取得しない。未対応の参照や不正な引数はvalidation失敗となる。
Promptsはdiscovery情報だけを上位へ渡し、取得・注入は行わない。

## 実行制御

- annotation trustの既定はfalse。`readOnlyHint=true`でもunknown・serial・retryなし。
- 明示的に信頼したread-only Toolだけparallelと最大1回のtransport retryを許可する。
- read-only以外は同一connection内で排他的に実行する。readとの重複も防ぎ、writer待機中は新規readを待たせる。
- Resource readとdiscoveryはprotocol上の読取として扱う。
- `deny`、`force_serial`、`disable_retry`、`require_confirmation`だけを安全側制限として適用する。
- binding必須時にvalidatorがない場合と、confirmation必須時に確認Portがない場合は拒否する。
- loopの既定上限は6 calls、同一Tool連続3回、同一引数2回、自動cycle 3回。自動cycle番号はCoreが`auto_cycle`へ渡す。
- retry・入力回答後の再実行も回数へ加算し、予算を暗黙に延長しない。
- rate limit既定は60秒あたりglobal 120、connection 60、character＋session 30。`RateLimits`で変更可能。
- parallel上限はconnectionごとに4。待機中の停止要求をdispatch直前にも検証する。
- `stop(loop)`は新規dispatchを停止する。既に開始した副作用の取消しや巻戻しを保証しない。

## 結果・MRTR・未対応能力

返却値は`execution-envelope.schema.json`の形式で、text、structuredContent、image、resource link、
embedded resource等をnative payloadとして保持する。native payloadは非信頼データであり、通常log、
Frontend、LLMへそのまま公開しない。表示・会話統合のsanitizeは#182の責務となる。
SDKの外部I/O診断は固定文にし、stdio stderrを通常logへ転送しない。SDK外の例外本文も公開しない。
protocol/auth/tool errorや結果整形の失敗をtransport retryへ変換しない。
SDKの接続lifecycleは専用taskが所有する。HTTP接続断でSDK内部のcancel scopeが終了しても
Coreの呼出taskを取り消さず、transport失敗として返す。利用者からのcancelは維持する。

`InputRequiredResult`は`outcome=input_required`と一回限りの`interaction_id`で返す。
上位は許可・user inputを判断し、`resume(interaction_id, input_responses, loop_id)`へ回答する。
元引数、opaqueな`requestState`、同じloop/snapshotを保持し、許可・sharing・binding・停止・予算を
再検証して元requestを再送する。入力round上限は3で、通常budgetがさらに厳しい場合は先にbudgetで止まる。
Sampling callback、roots callback、elicitation callbackによる自動回答は登録しない。

旧版の`execution.taskSupport=required`はsnapshotでunsupportedにする。2026-07-28では必須能力不足
（`-32003`）をunsupportedとして返す。Tasks必須operationだけを止め、同じserverの通常Toolを壊さない。
`TaskTrackerPort`の既定はunsupported。EventSource、ActionRecovery、ConfirmationPolicy、BindingValidatorは
抽象Portであり、後続Issueの機能を実装済みとは扱わない。

## 検証と#152との関係

```text
backend/.venv/bin/python -m pytest backend/tests/unit/test_external_mcp_*.py
backend/.venv/bin/python -m pytest backend/tests/module/test_external_mcp_conformance.py
```

module suiteは`mcp==2.0.0`とCoreをimportしない別processの合成MCPを使う。stdio、Streamable HTTP、
旧版fallback、Bearer、OAuth要求、MRTR、pagination、native結果、refresh、timeoutとprocess終了を検証する。
第三者MCPやdogfoodのcredentialは使用しない。通常CIのBackend module jobで実行される。

#152はPR #219でepicへ反映し、#153、#154、#156、#159を各作業ブランチから
`epic/104-addon-foundation`へのPRで順次統合する。Epic全体の受入後にmain向けPRを作成する。
契約補完（native_definition、resource_uri、input_required、interaction_id）は#152に含む。

自作Addonのlocalhost bind/Origin/Bearer server conformanceは#221の範囲とする。
SDKのTasks適合は[公式拡張仕様](https://tasks.extensions.modelcontextprotocol.io/)を参照する。
