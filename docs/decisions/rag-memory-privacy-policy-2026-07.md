# RAG長期記憶の保存判定・機微情報保護方針 (2026-07)

## 状態

採用。

本ADRはMVPで守る不変条件と、利用範囲の拡大時に追加する防御を定める。
完成形の防御をMVPですべて実装することは求めず、後から安全に拡張できる境界を先に固定する。

## 背景

現行実装は、SQLiteへ生の会話本文を保存し、その一部を同じ本文のままChromaへ登録する。
保存可否は語彙・部分文字列中心であり、辞書外の機微情報を見落とす一方、一般質問を
誤検出する可能性がある。また、会話履歴、長期記憶、検索インデックス、失敗時記録の責務が
同じ経路に混在している。

長期パートナー型AIでは、会話を継続できることと、別スレッドでも参照する長期記憶を持つことの
両方が必要である。ただし、ユーザーが保存を希望しても、APIキー、パスワード、秘密鍵などを
長期記憶へ保存してはならない。health情報、心理状態、第三者情報など、文脈依存で機微性が
決まる情報についても、安全側へ倒す必要がある。

## 決定事項

### 1. 長期記憶はpositive allowlist方式にする

禁止語に一致しなかった本文をそのまま保存する方式は採用しない。

長期記憶へ保存できるのは、許可された型へ正規化され、機微情報判定を通過した
`ApprovedMemoryCandidate`だけとする。ユーザーの「覚えて」「保存して」という指示は
記憶する意思の確認にはなるが、policy上の保存許可そのものにはならない。

会話から自動抽出した候補は、ユーザーが確認するまでRAG長期記憶へ保存しない。
明示的な「覚えて」は確認済み候補として扱えるが、絶対禁止、機微情報判定、許可型判定は
省略しない。自動抽出候補を確認前に永続化する必要がある場合も、生本文ではなく、
機微情報検査を通過した構造化候補だけを短いTTL付きで保持する。

MVPで許可候補とする型は、既存用途に合わせて次から開始する。

- `USER_PREFERENCE`
- `AGRICULTURE_LOG`
- `RECIPE`
- `PROJECT_DECISION`
- `WORK_LOG`
- `TASK`
- `STREAMING_NOTE`
- `CHARACTER_SETTING`

許可型の本文に機微情報が混在する場合、MVPでは部分保存せず候補全体を拒否する。

### 2. 保存判定を型付き状態で表現する

単一のboolや「既知signalなし」を保存許可として扱わない。

| 状態 | 意味 | RAG長期記憶 |
|---|---|---|
| `DENY_SENSITIVE` | 機微情報または絶対禁止情報を検出 | 保存しない |
| `DENY_USER_REQUEST` | 保存拒否指示を検出 | 保存しない |
| `ABSTAIN_UNKNOWN` | 判定不能、分類器障害、未知形式 | 保存しない |
| `NOT_MEMORY_WORTHY` | 安全だが長期記憶対象ではない | 保存しない |
| `PENDING_CONFIRMATION` | 許可型だがユーザー確認前 | 保存しない |
| `ALLOW_STRUCTURED` | 許可型へ正規化され、安全確認済み | 保存可能 |

複数の判定がある場合は、次の順で厳しい状態を優先する。

```text
DENY_SENSITIVE
  > DENY_USER_REQUEST
  > ABSTAIN_UNKNOWN
  > NOT_MEMORY_WORTHY
  > PENDING_CONFIRMATION
  > ALLOW_STRUCTURED
```

機微情報分類の結果と保存可否は別の型で表現する。たとえば一般的なhealth質問が
`NOT_SENSITIVE`でも、それだけでRAG保存可能にはならず、通常は`NOT_MEMORY_WORTHY`となる。
第三者のhealth情報が「本人healthではない」と分類されても、第三者情報policyにより
`DENY_SENSITIVE`となる。分類器やparserの障害時も会話応答は継続し、RAG長期記憶への
保存だけを抑止する。

### 3. 絶対保存禁止は会話から上書きできない

次の情報は、ユーザーが「問題ない」「公開用」「自己責任」「保存してよい」と明言しても
RAG長期記憶へ保存しない。

- パスワード、APIキー、access token、session cookie、recovery code
- 秘密鍵、SSH秘密鍵、暗号資産の秘密鍵・seed phrase
- クレジットカード番号、セキュリティコード、銀行口座認証情報
- 政府発行の本人確認番号・本人確認書類情報
- health、妊娠、服薬、治療、生体認証情報
- 自宅住所、正確な現在位置、私用連絡先
- 第三者の非公開情報

組み込みの絶対禁止policyは会話、人格prompt、LLM出力から変更できない。
運用設定は原則として禁止カテゴリを追加できるが、組み込み禁止を緩和できない。

### 4. 決定論的検出と実装交換可能な意味分類器を組み合わせる

機微情報検出は、決定論的scannerと意味分類器のハイブリッド方式にする。
検出処理は会話履歴とRAGで共通化し、検出結果に対する処置だけを保存先別policyへ分離する。

決定論的scannerは、Unicode・空白・区切り文字の正規化後に、少なくとも次を扱う。

- 既知形式のAPIキー・access token
- パスワードを示す周辺表現
- PEM形式の秘密鍵、SSH秘密鍵、暗号資産の秘密鍵・seed phrase
- クレジットカード番号とchecksum、決済認証情報、銀行口座番号
- メールアドレス、電話番号
- 政府発行の本人確認番号・本人確認書類情報
- 正確な住所・位置を示す表現
- 保存拒否指示

文脈依存の次の判定には意味分類器を使用する。

- health、心理状態、自傷・自殺念慮
- 虐待、性的被害
- 暗示的な機微情報
- 第三者の私的情報
- 許可型候補への機微情報混入

意味分類器の実装はinterfaceの背後で交換可能にし、ローカルLLM、spaCy／GiNZA等のparser、
専用分類器を候補とする。MVPで複数方式をすべて実装する必要はない。日英の固定corpusで
比較し、必要な検出品質、過検知、初期化時間、実行時間、メモリ使用量を満たす実装を1つ選ぶ。
ローカルLLMはローカル実行に限り使用してよく、外部APIへ判定本文を送信しない。

意味分類器は、カテゴリ、対象が本人・第三者・一般のいずれか、判定結果、reason code、
実装versionを含む構造化結果を返すだけとし、保存許可や権限判定を行わない。分類器が
`NOT_SENSITIVE`を返しても決定論的scannerの拒否を解除できない。timeout、モデル未ロード、
不正出力、未知カテゴリは`ABSTAIN_UNKNOWN`とする。

共通scannerの結果は、少なくともカテゴリ、本文中の開始・終了位置、確信度、reason code、
recognizer version、policy versionを持つ型付きfindingとする。検出した値そのものをfindingへ
複製しない。開始・終了位置はscannerへ渡した原文上の位置を指し、正規化後文字列との対応付けは
scanner内部だけで扱う。本文と文字位置は現在の処理中だけ保持し、finding、文字位置、
マスク前後の対応をSQLite、application log、例外へ保存しない。

保存先別policyは同じfindingを次のように扱う。

- スレッド履歴: 値を不可逆placeholderへ置換し、安全に置換できない場合は本文を保存しない
- RAG長期記憶: 絶対禁止または機微情報findingが1件でもあれば候補全体を拒否する

RAGでは、秘密値をplaceholderへ置換した本文を保存許可へ昇格させない。周辺文脈や別の未検出値が
残る可能性があるため、許可型候補全体を拒否する。

### 5. 最終判断と権限制御はアプリケーション層で行う

最終的な保存可否は、決定的なpolicy evaluatorが判断する。

- 共通scannerは保存先や処置を選択しない
- 履歴sanitizerはRAG保存可否を返さない
- RAG admission evaluatorは履歴保存可否を返さない
- LLMはwhitelistを変更できない
- LLMは`owner_id`、`character_id`、保存先を選択できない
- 生文字列を直接Chromaへ保存する公開APIを作らない
- repositoryは`ApprovedMemoryCandidate`だけを受け取る
- 保存拒否と権限判定はprompt上の指示に依存しない

policyの優先順位は次のとおりとする。

1. 本ADRの不変条件
2. typed policy schemaとアプリケーションevaluatorが強制する絶対禁止
3. `memory_policy.json`の認識語彙・pattern・閾値・追加禁止設定

`memory_policy.json`は実行時の認識設定のSource of Truthだが、絶対禁止カテゴリを削除したり
許可へ反転したりできない。必須カテゴリや最低限のrecognizerが欠ける設定は起動時に拒否する。

### 6. 会話履歴とRAG長期記憶を分離する

「履歴」と「記憶」を次のように定義する。UI上のスレッドと実装上のconversationは同じ境界を
指し、識別子は既存APIに合わせて`conversation_id`へ統一する。

- **履歴**: 同一スレッドを再開し、過去の発言を表示するための会話記録
- **記憶**: 別スレッドでも参照できる、承認済みのRAG長期記憶

同一スレッドでは、そのスレッドの会話履歴を参照できる。別スレッドの生会話履歴は
直接検索・参照しない。別スレッドで利用できるのは、RAGへ承認保存された記憶だけとする。

長いスレッドの要約を作る場合も、その要約はスレッド内の会話状態として扱い、
自動的にRAG長期記憶へ昇格させない。

用語上の指示は次の意味を基本とする。

- 「覚えて」: RAG長期記憶の候補生成を依頼する
- 「覚えないで」: RAG長期記憶へ保存しない
- 「履歴にも残さないで」: スレッド履歴にも保存しない
- 曖昧な「保存しないで」: 安全側で履歴・RAGの双方へ保存しない

### 7. 保存先ごとに機微情報の扱いを分ける

| 情報 | スレッド履歴 | RAG長期記憶 |
|---|---|---|
| APIキー、パスワード、秘密鍵 | 値をマスクして保存 | 保存禁止 |
| 決済認証、口座番号、政府ID、私用連絡先、正確な住所 | 値をマスクして保存 | 保存禁止 |
| health、心理状態、金融状況、正確な値を含まない生活情報 | 履歴として保持可能 | 保存禁止 |
| 第三者情報 | MVPでは履歴内だけ | 保存禁止 |
| 一般会話 | 保存可能 | 原則保存しない |
| 安全な明示記憶 | 保存可能 | 構造化して保存可能 |
| 判定不能 | 履歴policyに従う | 保存禁止 |

assistant応答が秘密情報を復唱した場合も、履歴保存前に同じマスク処理を適用する。
メールアドレス、電話番号、政府ID、口座番号、正確な住所等の直接識別値もplaceholderへ置換する。
healthや心理状態等の話題全体は、同一スレッドの継続性を失うため一律にはマスクしない。
秘密値を安全にマスクできない場合と、ユーザーが履歴にも残さないよう依頼した場合は、
本文を保存せずmetadataだけを残す。現在の応答生成では受信した原文を一時的に利用できるが、
原文、マスク前本文、原文hashをSQLite、application log、例外へ残さない。
スレッド履歴には削除機能を用意し、バックアップ・ファイル権限・ディスク暗号化の方針を
長期記憶とは別に管理する。

### 8. MVPでは第三者情報をRAG保存禁止にする

MVPでは、第三者の情報をRAG長期記憶へ保存しない。

将来、家族等を登録できるようにする場合も、第三者情報を全面許可せず、設定画面等の
管理経路から登録した人物について、人物別・カテゴリ別に許可する。

- 会話だけでwhitelist登録を完了しない
- 氏名、電話番号、住所を登録の必須項目にしない
- health、認証、金融等の絶対禁止はwhitelistでも解除しない
- 登録解除時に、その人物に由来する記憶を削除できるようにする
- `character_id`ごとにwhitelistと記憶を分離する

### 9. SQLiteを正本、Chromaを派生インデックスとする

SQLiteには承認済み記憶の構造、状態、policy version、削除・訂正情報を保存する。
Chromaには、SQLiteの承認済み記憶から生成した検索用documentとembeddingだけを登録する。

```text
conversations
conversation_turns
approved_memories
memory_index_outbox
```

`conversation_id`がUI上のthread IDに相当する。`conversations`と`conversation_turns`は
同一conversationの再開・表示に使う履歴であり、RAG検索対象にはしない。

`approved_memories`は少なくとも次を持つ。

```text
id
character_id
memory_type
normalized_text
structured_value
source_conversation_id
policy_version
status
created_at
effective_at
expires_at
```

Chromaには生会話を入れず、次だけを保存する。

- SQLiteの`memory_id`
- 承認済み`normalized_text`
- embedding
- `character_id`
- `memory_type`
- `policy_version`
- 検索に必要な有効日時metadata

Chromaは削除・破損してもSQLiteから再構築できるものとし、正本として扱わない。

### 10. 最小構成のtransactional outboxを使用する

承認済み記憶のSQLite保存とChroma登録予定の作成を、同じSQLite transactionで行う。

```text
BEGIN
  approved_memoriesへINSERT
  memory_index_outboxへPENDINGをINSERT
COMMIT
```

MVPのoutboxは次に限定する。

```text
id
memory_id
operation: UPSERT / DELETE
status: PENDING / COMPLETED / FAILED
attempt_count
last_error_code
created_at
updated_at
```

単一workerが`PENDING`と`FAILED`を再試行する。Chromaへの書き込みは、同じ`memory_id`で
再実行しても安全な冪等`upsert`にする。分散lease、指数backoff、dead-letter管理画面は
MVPでは実装しない。

outboxや失敗ログへ本文・embeddingを複製しない。SQLite自体への保存に失敗した場合も、
生本文をfallbackファイルへ退避しない。記憶を失うことを、未審査・重複本文を残すことより
優先する。

### 11. ingestion時と取得時の双方に境界を置く

MVPのingestion時には、次を順に行う。

1. 履歴と共通の決定論的scanner
2. 保存拒否指示の検出
3. 許可型`MemoryCandidate`の抽出
4. 共通scannerと意味分類による候補全体の機微情報再検査
5. 必要なユーザー確認
6. アプリケーションpolicy evaluator
7. `ALLOW_STRUCTURED`だけ`approved_memories`・outboxへ保存

MVPの取得時には、次を行う。

1. `character_id`で分離されたChroma collectionを検索
2. 取得した`memory_id`をSQLiteで確認
3. `active`、TTL、policy versionを検証
4. SQLiteの`normalized_text`へ共通の決定論的絶対禁止scannerを再適用
5. 不正・失効・旧policy・絶対禁止findingのある記憶をLLM promptへ渡さない

MVPでは、検索結果全件をローカルLLMで意味再分類することまでは求めない。
外部文書の取り込み、複数ユーザー、家族whitelist、cloud vector DBへの移行時に、
検索前の細粒度権限filterと検索後の意味再分類を追加する。

### 12. 本番ログはmetadata-onlyとする

監査と障害調査のため、次を記録してよい。

- trace ID、admission ID、memory ID、outbox job ID
- 判定stage、decision、reason code
- recognizer、model、prompt、policyのversion
- 取得したmemory ID
- tool名、処理結果、latency、retry状態

次は本番ログへ記録しない。

- prompt全文、生会話、取得chunk本文、最終出力全文
- APIキー、password、秘密鍵等の検出値
- tool引数・結果内の秘密情報
- 原文hash、embedding、parser frame全体

再現性は、実ユーザー本文の全文ログではなく、固定した合成テストcorpus、case ID、
model artifact、prompt version、policy version、実行環境manifestで確保する。

### 13. 意味分類器の品質は実測で確認する

ローカルLLMやparserを使用できることと、十分な検出品質があることは分けて扱う。
MVPでは大規模な統計的証明は求めず、日英の固定conformance corpusを実モデルで実行する。

最低限、次を確認する。

- 「問題ない」「自己責任」「保存して」と言われても絶対禁止を保存しない
- APIキー、password、秘密鍵の形式違い・空白挿入・コードブロック
- 同じ共通scanner結果が、履歴ではマスク、RAGでは候補全体拒否になること
- マスク不能時に履歴本文が保存されないこと
- userとassistantの双方に同じscanner・sanitizerを適用すること
- health、第三者情報、辞書外の病名・薬剤
- 許可型への機微情報混入
- 一般質問・説明・安全な好みの過検知
- 選択した分類器のtimeout、未ロード、不正出力
- `DENY`・`UNKNOWN`で`approved_memories`、outbox、embedding、Chromaへの副作用がない
- 履歴保存可能なhealth情報がRAGへ昇格せず、同一conversationの履歴には残る
- 他スレッドの生履歴を参照しない
- ログへ原文が出ない

model、prompt、policyのいずれかを変更した場合は、このcorpusを再実行する。
複数ユーザー化、cloud利用、外部文書RAG等へ進む前に、カテゴリ別recall、precision、
false-negative rate、false-positive rateを含む本格評価へ拡張する。

### 14. 既存テストデータは移行せず削除する

既存SQLite会話履歴と既存Chroma collectionはテスト用データであるため、新schemaへ移行しない。

実装切り替え時に次を行う。

1. 旧SQLite・Chromaへの書き込みを停止
2. 既存`conversations.db`を削除
3. 既存Chromaデータを削除
4. 旧`failed-memories.jsonl`が存在する場合は削除
5. 新schemaを空状態から作成
6. 新しい`approved_memories`だけをChromaへ登録

旧SQLiteの生会話からChromaを再構築しない。

## MVPで実装しない項目

次は必要性を認識するが、MVPへ含めない。

- 全言語・全PII形式の網羅
- 大規模な統計的accuracy証明
- 複数ユーザー向けRBAC・ABAC
- 家族whitelistの設定UIと同意管理
- 検索結果全件のLLM再分類
- 全出力への意味分類rail
- cloud LLM・cloud embedding向けgateway
- 自動的な全記憶再分類
- 分散outbox worker、lease、指数backoff、dead-letter UI
- 監査・policy管理画面
- 別スレッドの生会話を対象にした横断検索

## 後続実装を開始する条件

| 条件 | 追加する防御 |
|---|---|
| 複数ユーザー化 | `owner_id`単位の検索前権限filter |
| 家族whitelist | 人物・カテゴリ単位の許可と削除 |
| cloud LLM利用 | outbound prompt filterとlocal強制routing |
| cloud embedding利用 | embedding前の再検査と送信policy |
| 外部文書RAG | ingestion・retrieval双方のchunk検査 |
| 公開配信モード | 強化output filter |
| policy大幅変更 | 既存memoryの再分類または全再構築 |
| worker増加 | lease、backoff、dead-letter付きoutbox |
| セキュリティ保証が必要 | 大規模評価corpusと統計レポート |

## 採用しなかった案

### 禁止語・正規表現だけで本文保存を判定する

辞書外の病名・薬剤・暗示表現を検出できず、一般質問を誤検出しやすいため採用しない。

### LLMだけで保存可否を決める

非決定的であり、prompt injection、モデル変更、障害時のfallbackによってpolicyを迂回し得るため
採用しない。

### Chromaを記憶の正本とする

構造化された状態管理、訂正・削除、transaction、policy version管理に向かず、
再構築可能な派生インデックスとして扱う方が安全なため採用しない。

### 失敗本文をJSONLへ退避する

失敗記録が別の未管理な個人情報ストアになるため採用しない。再試行はSQLite outboxで行い、
ログにはmetadataだけを残す。

### 本番監査ログへprompt・chunk・出力全文を保存する

再現性は高まるが、監査ログが機微情報の複製先になるため採用しない。合成corpusとversion情報で
再現性を確保する。

## 影響・トレードオフ

- 安全側の誤検出では、保存されるはずの記憶が保存されないことがある
- 誤検出時も会話をブロックしないため、通常の対話品質への影響は限定できる
- 生会話ではなく構造化記憶を保存するため、抽出・正規化処理が増える
- scannerを共有しつつ保存先別policyを分離するため、finding型とversionの互換性管理が必要になる
- SQLiteとChromaの二層同期が必要になるが、outboxにより障害から回復できる
- スレッド履歴と長期記憶の削除・UIを別々に扱う必要がある
- Chromaを再構築可能にすることで、policy変更やデータ破損への対応が容易になる

## 関連

- `docs/decisions/miori-memory-policy-2026-06.md` — 光織の長期記憶方針
- `docs/decisions/Multi-character-db-2026-06.md` — キャラクターごとの記憶分離
- `docs/system-architecture.md` — SQLite・Chromaを含むシステム構成
- `docs/testing-policy.md` — 単体・結合・実接続テストの区分
- `backend/app/memory/memory_policy.json` — 認識語彙・pattern・閾値・追加禁止設定の実行時Source of Truth
