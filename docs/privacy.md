# 機微情報の検査を明示的に有効にする

この機能は単一ローカル利用者向けです。通常起動や稼働中のサービスへ自動適用しません。
既定のPrivacyPolicyは全permissionを拒否します。分類器はgemma4-12b・合成60件をcacheなし3回評価済みですが、
各回51/60（85%）、HEALTH・SELF_HARM・ABUSEの分類別基準未達と第3回の偽陰性2件によりFAILで、品質未受入です。
ABSTAIN・不正出力・timeout・実行不完了は各回0件です。ABSTAINは[ADR 0032](adr/0032-privacy-classifier-evaluation.md)の決定Cで別集計しました。
各回・各groupと制約は[実モデル証跡](evidence/2026-10-12-privacy-classifier-real-model-evaluation.md)にあります。
形成〜利用の実環境IT2/STは未実施です。
私的会話の取り込み開始を意味しません。[ADR 0005](adr/0005-privacy-boundaries.md)を参照してください。

## 起動側の接続

信頼されたPython起動コードで同じpolicyをInferenceとcreate_appへ渡します。
HTTP callerはpolicy・scope・permissionを指定できません。以下は合成データ用の構成例です。

```python
from digital_souls_core.application import Inference
from digital_souls_core.api import create_app
from digital_souls_core.history import Binding
from digital_souls_core.privacy import PrivacyPolicy
from digital_souls_core.storage import StorageConfig, open_storage

policy = PrivacyPolicy()
# characters/providerは起動側で解決した固定構成。
inference = Inference(characters, provider, privacy=policy)
policy.configure({
    Binding(inference.scope, characters[0].config.character_id):
        frozenset({"history", "local"}),
})
# storage_config: StorageConfig（backend="postgresql"、postgresは必須）
stores = open_storage(storage_config)
app = create_app(inference, history_store=stores.history, history_policy=policy)
```

`local`として扱うのは、既存の検証済み`llamacpp_chat` loopback Profileだけです。
他のSDK経路はモデル名にlocal/Ollamaが含まれてもexternalとして扱います。
既存Profileの`external_send_allowed`は推論能力の許可として従来どおり必要です。
このフラグだけでprivacyのexternal permissionを付与することはありません。

外部送信を許す場合は、既存Providerと固定local Profileから`LocalClassifier`を作成し、
`PrivacyPolicy(classifier)`へ渡します。model digestを起動構成で固定してください。
localとexternalの両permissionが必要です。分類器は同じProvider portを利用し、transportや
外部fallbackを追加しません。proxy環境変数やcloud APIキーは既存local adapterが引き継ぎません。

## 許可の境界

| permission | 検査 | 失敗時 |
|---|---|---|
| history | 全messageの秘密値・直接識別値 | 保存・復元・receipt返却を拒否 |
| local | 実送信payloadの決定論的検査 | ローカル推論を拒否 |
| external | 決定論的検査＋ローカル意味分類 | 外部推論を拒否 |
| memory | 決定論的検査＋ローカル意味分類（source除外は別途必須） | 記憶検索を拒否（形成の保存判定は未実装） |

意味分類のsafe判定だけで候補の根拠・型・有用性・保存先が承認されたことにはなりません。
正本の登録・参照検索は[記憶API説明](memory.md)を参照してください。
登録port自体はprivacy分類や保存価値の判断を行わず、trusted callerが保存判定を済ませる前提です。
構造化形成は未実装で、現行形式の実モデル品質は未受入です。

[ADR 0006](adr/0006-conversation-memory-controls.md)のユーザー操作仕様を優先します。
「記録しないで（指定発話）」は履歴を残し、UI/Agentが`memory_excluded_indices`で指定した発話を
memory対象外にします。自然語だけから期間や操作を推定しません。thread private/archiveは明示PATCHです。
現在userの保存拒否の語は確認の契機とし、確認まで該当発話を形成対象外として永続保留します。
非stream・SSE completedで本文なしのsource参照と操作案内を返します。assistant・tool・過去履歴は
新しい確認を生成しません。回答APIの受入は既存private化と記憶撤回を原子的に実行し、解除でも
旧記憶と受入発話を復活させません。拒否は対象の保留だけを解除し、明示除外・private・他の保留を
維持します。未回答は再起動後も対象外です。自然文・回答から履歴削除を実行しません。
revision排他、再送、移行とHTTP形式は[履歴API](history-api.md#保存拒否の確認)を参照してください。
履歴を残さない操作は会話削除で、派生memory削除の通知を同時に作ります。
同じDBの正本と依存結果の本文を同時に消去します。旧逐語抽出・再構成のconsumerは撤去済みです。
撤回イベントのoutboxは保持しますが、形成ができるまで再生成しない期間を許容します。
secret検出時は引き続きturnを拒否します。stateless応答の事後マスク機能もありません。

保存の許可と外部送信の許可は独立しています。health等の同一会話内履歴は
history permissionで扱い、external/memoryでは意味分類が機微と判定すれば拒否します。
`configure({})`で許可を撤回できます。進行中の分類は失効しますが、既に外部へ送られた内容は回収できません。
policyを差し替えた履歴serviceはfail-closedになり、削除だけはscope照合で引き続き可能です。

## 検出範囲と限界

既知credential prefix、ラベル付きpassword/key/token、PEM、Bearer、email、限定した電話形式、
Luhn整合カード番号、ラベル付き住所/ID等を対象にします。NFKC、casefold、format文字除去、
区切り文字、nested JSONとJSON内文字列を検査し、深さ・項目数・入力byteを制限します。
検出結果はフラグだけで、本文・検出値・spanを返しません。tool構造を含め全文を拒否し、部分置換しません。

任意の無印opaque値、未知vendor形式、難読化、全世界の住所/IDは網羅しません。
たとえば「password: ...」を含む一般的な設定例も保守的に拒否し得ます。
合成corpusの成功は未知の秘密や意味的機微情報を必ず検出できる証明ではありません。
LLMは誤判定し得ます。運用で検出範囲を拡張する際はscanner/prompt/policy versionと評価を更新します。


### 数値表現の扱い

scanner v3はJSON文字列内のdecimal・指数表記をDecimalで桁落ちなく読み、固定小数表記で
検査します。整数部と有効な小数部の数字は別々に検査し、小数部の桁合わせの先行ゼロは
識別子の一部として扱いません。指数展開の範囲はadjusted exponentの-1024から308までで、超過は判定失敗です。
既にbinary floatになった絶対値`1e13`以上の値は元の桁の復元を保証できないため拒否します。
これは秘密の確定判定ではなく、未対応の数値表現としてのfail-closedです。小さな有限floatは
decimal表記へ変換して検査します。非有限値は拒否し、boolean/nullは識別子として扱いません。
元の字句を失う前の全形式を復元できる保証や、任意の難読化検出を追加するものではありません。

## 管理された構造化出力

分類器は固定JSON Schemaをproviderへ渡し、返却値のstrict検証を維持します。未対応・不正出力は拒否し、
制約なしの再試行は行いません。schema・対応契約・SDK versionもprovenanceに含みます。
[ADR 0008](adr/0008-managed-structured-output.md)の分類器側を維持し、逐語抽出器は撤去済みです。
分類器のprovenanceにはmodel/digest、transport・profile ID・正規化loopback endpoint・識別形式versionを持たせます。
http scheme・127.0.0.1・数値port・固定/v1 pathで組み立て、同値なURL表記は同じidentityとします。
profile IDだけの変更も別の処理先と扱い、token/key/headerは保存しません。
userinfo/query/fragmentを含むURLはProfile検証で拒否します。
検索中とdispatch前に分類器設定とpolicyの同一性・世代を照合します。
これは運用者が固定した設定の識別であり、endpoint背後の実プロセスやmodel digestの真正性保証ではありません。
旧抽出jobの設定再承認・再試行APIは撤去済みで、現在のendpointを旧承認へ補完しません。
