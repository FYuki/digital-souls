# Addon契約

#152 / #104 のMCP-first外部能力接続基盤で使うJSON Schema Draft 2020-12契約。

## ファイル

- `manifest.schema.json`: 初期Static Manifest。`connection` / `capabilities` / `core_policy`を分離する。
- `addon-meta.schema.json`: 自作AddonがMCP `_meta` の `io.digital-souls/operation` に公開する拡張metadata。
- `capability-snapshot.schema.json`: discovery結果を検証・正規化したCapability Snapshot。
- `execution-envelope.schema.json`: protocol非依存の実行metadataとnative payload保持境界。
- `fixtures/valid/`: schemaとsemantic policyで受理すべきfixture。
- `fixtures/invalid/`: schemaまたはsemantic policyで拒否すべきfixture。

## 信頼と利用許可

MCP connectionの連携許可と、server自己申告metadataのtrustは別概念とする。

- `connection.grant.scope = validated_snapshot` は、登録済みconnection identityから取得し検証済みとなったactive snapshot内のTool利用を許可する。
- refreshで追加されたToolは検証済みsnapshotに入り、`next_execution_loop`境界でactiveになってから利用可能になる。現在loopへ途中追加しない。
- `connection.trust.annotations` の既定概念はfalse。falseなら`readOnlyHint`等を実行最適化の根拠に使わない。
- `connection.trust.addon_metadata` がtrueでも、自作Addonの登録済みidentity照合と`addon-meta.schema.json`検証に成功したoperationだけをtrusted metadataとして扱う。
- MCP `serverInfo`等の自己申告identityは表示・診断用であり、security identityの正本にしない。

`capability-snapshot.schema.json`では、未信頼annotationを`effective_policy.effect_source = trusted_annotation`として扱うことを拒否する。effect sourceが`unknown`なら、effect=`unknown`、concurrency=`serial`、retry=`none`を強制する。

## Coreによる制限

`core_policy.restrictions`は安全側の制約だけを表す。

- `deny`
- `force_serial`
- `disable_retry`
- `require_confirmation`

Addonの`write/destructive`をCore側で`read`へ書き換えるような、domain意味を安全側に見せかけるoverrideは契約に存在させない。

## 秘密情報

Manifestへ生credentialを保存しない。Bearer認証は`secret_ref`だけを保持し、Connectorが送信直前にsecret store/環境変数から解決する。

自作Streamable HTTP AddonはBearer service tokenを必須とする。外部HTTP MCPの初期対応は`none`または事前設定Bearerまでとし、OAuthは#104 MVPの非スコープとする。

## 初期Execution既定値

```text
max_calls_per_loop = 6
max_consecutive_same_tool = 3
max_identical_call = 2
normal_max_auto_cycles = 3
```

並列化とConnector-level 1 retryは、trust評価後のeffective read-onlyにだけ許可する。state-changing/unknownは同一connection instance内で直列、automatic retryなしとする。

## テスト用データの意図

- `valid/manifest-self-owned.json`: self-owned Streamable HTTP + Bearer `secret_ref` + trusted metadata。
- `invalid/manifest-raw-secret.json`: raw tokenをManifestに含めるため拒否。
- `invalid/manifest-unsafe-override.json`: safetyを緩める未定義overrideのため拒否。
- `valid/snapshot-untrusted-annotation.json`: `readOnlyHint=true`でもannotation未信頼なのでeffective effectはunknown。
- `invalid/snapshot-untrusted-as-read.json`: annotation未信頼なのにtrusted read-onlyとして並列/retryを許可しているため拒否。
- `valid/execution-succeeded.json`: native MCP payloadを保持した成功Execution envelope。

## スキーマで表現しない実行時の不変条件

次はJSON Schemaだけでは十分に表現できないため、Registry/Execution Gateのsemantic validationと#159 conformance testで検証する。

- 登録済みendpoint/auth主体/TLS issuer等と実接続identityの照合。
- self-owned metadata trustを有効にする前のidentity照合。
- Core restrictionがAddonのdomain宣言より安全性を緩めないこと。
- schema digest/revision差分とsnapshot activation境界。
- cached snapshotだけでoffline実行しないこと。
- secret/raw auth error/native payloadを通常log・LLM・Frontendへ露出しないこと。

## 外部MCP向けの契約範囲

#104のruntime対象はexternal stdio / Streamable HTTPとする。self-owned向けschemaは将来互換用であり、runtime・server conformanceは#221で実装する。
Snapshot Toolは`native_definition`へ定義全体を保持できる。Executionの`operation_ref`は`tool_name`または`resource_uri`のいずれか一方とする。MRTRは`outcome=input_required`と`interaction_id`で明示的な上位判断へ渡す。
