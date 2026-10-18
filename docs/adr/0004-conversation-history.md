# ADR 0004: 明示的な会話履歴の永続化

Status: Proposed

一部を[ADR 0036](0036-mvp-api-keys-and-users.md)で置き換えます（2026-10-18 ユーザー承認。localhost の単一利用者に限り認証を持たない記述。Core が発行する key で利用者を決める）。

一部を[ADR 0037](0037-message-level-conversation-history.md)で置き換えます（2026-10-18 ユーザー承認。入力と応答を往復としてまとめる保存、request ID と revision による排他、保存の policy の既定拒否。発言単位で順序だけを持ち、判定せずに保存する。実装は後続の Issue）。

一部を[ADR 0027](0027-tool-use-and-activity-runtime.md)のツール利用と実行基盤の決定で置き換えます（2026-10-11 ユーザー承認。Core に tool 実行ループを追加しない記述。履歴の保存契約は維持）。

一部を[ADR 0021](0021-postgresql-only-storage.md)のPostgreSQL一本化で置き換えます（SQLite adapter、既定保存先（`history.sqlite3`）、ファイル権限、journal/secure_deleteの設定。保存・復元・削除・scope・revision・policyの契約は維持）。

一部を[ADR 0026](0026-prompt-assembly.md)のプロンプトの組み立てで置き換えます（2026-10-11 ユーザー承認。履歴を自動要約・切捨てせず拒否する扱いのうち、推論へ送る履歴。推論時は領域ごとの token 予算で古い履歴を往復単位で削る。会話履歴の保存上限 1 MiB と 413 は維持）。

一部をEpic #134 / [Issue #135](https://github.com/FYuki/digital-souls-core/issues/135)の件数上限撤廃で置き換えます（2026-10-10 ユーザー決定。推論入力256 messageの件数上限を撤廃。履歴1 MiBのバイト上限は維持）。

日付: 2026-10-03

## 背景

ユーザーが承認した順序は「会話履歴の保存 → 機微情報の扱い → 記憶の抽出・検索」です。
このADRは第1段階だけを扱います。汎用Agentは目的とtoolsを渡し、Coreが人格・モデル設定を
所有します。Coreにtool実行ループを追加しません。[ADR 0001](0001-character-inference-api.md)の
stateless APIは互換性を維持します。

PoCの会話schemaとHistorySanitizerを参照しました
（公開digital-souls、revision `fce7382884d981c42be7fbd3ddaffe7469e27588`、
`backend/app/conversation_history/schema.py`、`backend/app/privacy/history_sanitizer.py`、
`backend/app/privacy/contracts.py`）。SQLite、会話単位の境界、判定不能時の保存拒否を参考にし、
UI設定・音声turn・常駐worker・記憶queue・既存DB移行は移植しません。私的会話を参照しません。

## 決定案

Python標準のSQLiteをadapterとし、新規依存やDBサービスを増やしません。
JSONLは原子的な再試行・順序・削除の管理が複雑になり、外部DBは単独利用には過大です。
applicationはHistoryStore portに依存し、SQLiteやHTTPには依存しません。

保存は`create_app`へのstore/policyの明示的注入で有効にした専用conversation経路だけです。
通常起動は経路もDBも作りません。保存policyが欠落・例外・厳密なTrue以外なら拒否します。
第1段階では本番用allow-all、分類器、環境変数による私的会話の自動保存は提供しません。
テスト用policyは合成入力のみを対象にし、秘密判定実装として配布しません。

subject/client/audienceはサーバーのAccessScopeから取得します。会話ID、キャラクターID、
scopeの組をすべてのDB操作で照合し、呼び出しJSONからscopeを受け取りません。
localhost単一利用者限定で、別ローカルプロセスに対する認証やmulti-tenant隔離ではありません。

会話ごとにrevisionを持ち、request IDの一意性と期待revisionを同一transactionで検証します。
request IDは同一入力・同一設定の再送なら保存済みreceiptを返し、違う入力なら409です。
入力・assistant最終応答をまとめて保存し、未完了input・部分応答・例外本文は保存しません。
保存済みtool callの未解決状態は許容し、次の入力は一致する全tool resultを要求します。
callerによるassistant/system挿入や、重複・未知・不足したtool resultを拒否します。

更新は楽観的排他です。同時推論自体は重複し得ますが、保存の二重反映や上書きを拒否します。
削除が先に確定した進行中推論はcommitできず、会話を復活させません。
初回schemaの全DDLとversionも単一transactionとし、途中process停止はrollback後に再初期化します。
未知・旧部分schemaは自動修復しません。未確定推論は再試行可能です。
同時requestの先行receiptを返す場合も、その実際の内容を現在のread policyで再認可します。
provider完了後のcommit直前にcancel受付点を設け、観測済み切断は保存しません。
SQLite commit区間はawaitなしとし、commit後の切断通知や配送失敗ではreceiptを維持します。
ネットワーク上の切断時刻を保証するものではなく、同じrequest IDで復旧します。

会話用streamは上流の完了まで最大1 MiBの可視内容をバッファします。完全なtool引数と
最終文面をpolicyへ渡し、保存確定後に`completed`イベントと`[DONE]`を返します。
途中失敗・キャンセル・終端なし・length終了は保存せず、statelessの逐次SSEは変更しません。
この段階では逐次表示の体験より、保存・秘密判定の境界を小さく確実にする方針です。

保存対象はuser/toolと公開assistant messageのallowlistのみです。system、Lore、ContextSource、
SDK envelope、reasoning/thinkingフィールド、usage、認証情報、例外は保存しません。
可視content内の既知think/analysisタグも拒否します。未知形式の秘密や思考を識別する保証は
なく、信頼されたpolicyが全content・tool引数・resultを許可しない限り保存できません。
第2段階の接続口はHistoryPolicyで、read・export・storeごとに再判定します。
第3段階は別途設計し、今は抽出・検索・自動学習やContextSourceへの自動接続を行いません。

既定保存先は`~/.local/share/digital-souls-core/history.sqlite3`です。Git配下とsymlinkを拒否し、
専用ディレクトリ0700、DB0600、所有者・hardlinkを検証します。Linux/WSLのPOSIX filesystemが
対象で、Windows ACLやネットワークfilesystemを検証済みとはしません。
rollback journalのDELETE modeとsecure_deleteを使用し、WAL・全文検索・バックアップを作りません。
削除は会話のturn・receiptをcascade削除します。バックアップ、SSD、OS snapshot、外部Agentや
送信先モデルが保持するコピーの消去は保証しません。

## 制約と検証

会話履歴は自動要約せず、推論入力256 message・履歴1 MiBの上限で拒否します。
モデルの実token上限は別問題です。巨大履歴のpaging・retention・会話単位以外の削除・暗号化・
DB移行・UI・認証・分類器・記憶は未実装です。保存policyの本番実装前に私的実会話を使いません。

合成データのUT/IT1で保存/復元/一覧/削除、順序、scope隔離、再試行、tool往復、stream失敗・
キャンセル、削除との競合、policy撤回を検証します。GPU・実モデル・人格品質評価は不要です。
APIの詳細は[会話履歴API](../history-api.md)を参照してください。


## 代替案と影響

- JSONLは依存を増やしませんが、順序・再試行receipt・会話単位の削除を原子的に扱う独自処理が必要です。
  SQLiteのtransactionと制約を使い、その実装負担を避けます。
- 外部DBは複数利用者や分散運用に向きますが、この段階の単一利用者にはサービス管理が過大です。
- stateless APIへの自動保存は既存callerの保存意図を変えるため採用しません。
- streamの逐次配送は低遅延ですが、完全なtool引数を保存policyが確認する前に内容を公開します。
  完了までバッファすることで初回表示は遅くなります。断片ごとの増分バイト計測で処理量を抑え、
  完了時と保存前の厳密な全体サイズ検証を維持します。
- 履歴の上限到達時は自動要約・切捨てをせず413を返すため、caller側で新しい会話を作成する必要があります。
  分類器が未実装の間は保存を既定拒否とし、私的会話の自動取り込みを開始しません。

## 参照

- [作業Issue #18](https://github.com/FYuki/digital-souls-core/issues/18)
- [初回work PR #19](https://github.com/FYuki/digital-souls-core/pull/19)
- [epic PR #20](https://github.com/FYuki/digital-souls-core/pull/20)
- [履歴保存・レビュー修正の検証証跡](../evidence/2026-10-03-conversation-history.md)
- [llama.cpp統合の検証証跡](../evidence/2026-10-03-history-llamacpp-integration.md)
- [PoC履歴sanitizer](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/backend/app/privacy/history_sanitizer.py)
