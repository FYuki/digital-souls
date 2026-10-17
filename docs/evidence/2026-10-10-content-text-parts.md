# completion 入力の text 配列正規化（2026-10-10）

対象は [Issue #136](https://github.com/FYuki/digital-souls-core/issues/136)、Epic #134。
作業branchは `fix/136-content-text-parts`、起点は `35c20182f0aa3acfe2bcf49c73ddd7eb3d9eb022`。
実装・試験・API文書の検証対象revisionは `3d824c9a94373121e4ce1c935e3be92100665b15`。
本証跡はその次の文書のみのcommitに含める。push・PR作成・mergeは実施していない。

## 入力境界と影響範囲

[contracts.py](../../core/src/digital_souls_core/contracts.py)の `InputMessage` は `Message` の
検証前に入力dictのcontentだけを正規化する。空でないtext部分配列を厳密検証し、
textを改変せず `\n` で連結する。1要素ではそのtextをそのまま使う。
`TextPart` は `type: "text"` と文字列の `text` だけを許可し、未知キーや他形式を拒否する。
呼出元dictは変更しない。文字列とassistant tool callのnullは従来の検証へそのまま渡す。

適用箇所は `CompletionInput.messages`（chat/characterが継承）と
[history.py](../../core/src/digital_souls_core/history.py)の `TurnInput.messages` のみ。
実行時のmessageは従来どおり `Message` で、contentは文字列または許可されたnullになる。
validation JSON Schemaにも受理する配列を記載するため、schema専用の動的モデルを使う。
これは入力の記述だけで、実行時の正規化・検証後の型を広げない。

共有 `Message` 自体は変更しない。provider adapterの応答解析、
`Conversations._visible` の応答検証、PostgreSQLの保存済み履歴・receipt読み込みは従来どおり。
共有モデルと保存対象provider応答が配列を拒否することもUTで確認した。
応答のtext contentは文字列のまま。assistant tool callのnullも維持する。
履歴completionの新規入力は既存のuser/tool制限を維持する（system/assistantの履歴再送を許可しない）。

正規化はprivacyスキャン、履歴1 MiB検査、推論送信、保存、fingerprint計算より前に完了する。
認証、記憶形成、systemの扱い、件数・バイト上限、context_budget_bytesは変更していない。

## fingerprint と試験

`request_fingerprint` のアルゴリズムは変更しない。`TurnInput.model_dump()` が正規化後の
文字列を出すので、同じtextを文字列・1要素配列・複数要素配列で表現しても同じfingerprintになる。
例：`"one\ntwo"` と `[{"type":"text","text":"one"},{"type":"text","text":"two"}]`。
同じrequest_id、revision、stream等で文字列へ置き換えて再送しても既存receiptを返し、再推論しない。
textを変えれば409。stream等の他項目は従来どおりfingerprintに含む。

追加した試験は次のとおり（すべて合成入力）。

- [test_content_parts.py](../../core/tests/test_content_parts.py)：UT 53件。
  単一/複数/空文字を含む部分の連結、全役割、4入力契約、null+tool_calls、18拒否形式、
  fingerprint、共有モデル/provider応答の配列拒否、入力JSON Schema。
- [test_content_parts_api.py](../../core/tests/test_content_parts_api.py)：IT1 64件。
  chat/character×stream有無のOpenClaw形のtool往復、12 tools、tool_choice、max_completion_tokens、
  system/user/tool配列のprovider送信、HTTP 400（3経路×18形式）、mock storeの保存・復元・再送。
- [test_postgres_content_parts.py](../../core/tests/test_postgres_content_parts.py)：PostgreSQL 10件。
  stream有無の保存・再open・tool配列往復・文字列再送、保存/記憶拒否の3指示×文字列/配列の同一判定、
  秘密の文字列/配列についてHTTP 403、provider/classifier未送信、DB本文とreceiptが未保存。

保存拒否発話の既存動作は履歴の自動削除ではなく、形成対象外・確認保留であり、これを維持している。

### TDD の RED と GREEN

実装前に上記UT/IT1（Schema試験を除く）を実行し、**25 FAIL・91 PASS**。
配列正規化/全役割/null+tool結果/fingerprintはPydantic検証失敗、HTTP受理・保存は400でFAILした。
PostgreSQL追加試験も実装前に **6 FAIL・4 PASS**。
配列の保存・拒否指示は検証/400で失敗し、秘密配列は期待403に対し400だった。
文字列の対照ケースはPASS。実装後は追加UT/IT1 117件・PostgreSQL 10件がPASSした。

既存 [test_contracts.py](../../core/tests/test_contracts.py)の `test_invalid_contract` から
正しいtext配列を拒否するparameterを1件除いた。新仕様で受理するためであり、
受理・正規化の試験と不正配列18形式の拒否試験へ置き換えた。他の既存試験は削除していない。

## 文書と全品質ゲート

[API](../api.md)と[履歴API](../history-api.md)へ入力形式、連結、拒否条件、null、応答文字列、
入力に限定する境界とfingerprintの挙動を反映した。
[SPEC](../../SPEC.md)にはmessages.contentの型・配列拒否を規定する該当記載がなく、変更不要。

環境はCPython 3.12.3、uv 0.8.22、Node 24.19.0。実行前に以下を順番どおり設定した。

```sh
export T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
```

| コマンド | 最終結果 | 件数・内容 |
| --- | --- | --- |
| `uv sync --locked` | PASS | lockどおり78 packages |
| `uv lock --check` | PASS | resolved 80 packages、lock変更なし |
| `uv run --no-sync ruff check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py` | PASS | 指定全対象 |
| `uv run --no-sync ruff format --check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py` | PASS | 117 files |
| `uv run --no-sync mypy` | PASS | 117 source files |
| `uv run --no-sync pytest -m ut -q` | PASS | 750 passed、1100 deselected |
| `uv run --no-sync pytest -m it1 -q` | PASS | 670 passed、1180 deselected |
| `uv build --no-build-isolation -o /dev/shm/dsc136-dist` | PASS | sdist・wheelの2成果物 |
| `bash tools/test-postgres.sh`（`timeout 1800`で上限） | PASS（再実行） | 430 passed、1420 deselected、74.18秒 |
| `(cd evals/semantic && npm ci --no-audit --no-fund)` | PASS | 575 packages、lock変更なし |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS | 59 registered required tests |
| `node tools/check-docs.mjs` | PASS | 本証跡stage後にも再確認 |
| `git diff --check` | PASS | 本証跡stage後にも再確認 |

必須ゲートにskip/xfail/TODO/cancelledはない。deselectedはmarkerによる他分類の除外。
PostgreSQLは既存runnerのdigest固定PostgreSQL 18、network none・公開portなし、Unix socket。
実LLM、実OpenClaw接続、IT2/STは **NOT RUN**。mock合格を実モデル品質として扱わない。

## 初回 PostgreSQL FAIL と制約

最初の全体実行は **392 PASS・38 ERROR（FAIL）**、70.59秒。
専用DBが途中で接続を閉じ、後続fixture作成はrecovery mode/接続拒否になった。
コンテナは既存runnerが削除するため原因は未確定。OOM等を確認済み原因とはしていない。
他のゲート終了後、コード・fixture・資源上限を変更せず同じ全体コマンドを再実行し、430件PASS。
最初のFAILを合格に読み替えず、最終ゲートを再実行結果として記載した。
依存由来のDeprecationWarning/UserWarningとnpmのdeprecated警告もあったが、設定変更はしていない。
