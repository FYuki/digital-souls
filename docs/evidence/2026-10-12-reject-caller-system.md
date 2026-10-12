# 呼び出し側 system 拒否の実装・合成検証

実施日: 2026-10-12（JST）。起点: `d860eebfe3b8ae05e18fb7f519bb36abed8812d0`。
作業branch: `feature/177-reject-caller-system`。この証跡を含むcommitが検証対象の実装です。
正本: [ADR 0031](../adr/0031-reject-caller-system.md)。ADR本文は変更していません。
push・PR作成・merge、稼働中llama.cppへの要求、実LLM・GPUによる評価は行っていません。

## HTTP・設定・組み立て

- `/v1/chat/completions`・`/v1/character/completions`はsystemを1件でも含むと400
  `system_message_unsupported`。文字列・text配列、先頭・中間・末尾、streamでも同じです。
  privacy認可・context/記憶検索・token counter生成・provider送信の前に拒否します。
- 会話履歴completionのsystem拒否だけ同じコードへ変更しました。system以外の不正なturnは
  `invalid_turn`、schema不正は`invalid_request`を維持します。systemと不正tool順序が同居する場合も
  system拒否を優先します。エラー本文に要求内容を含めず、streamも通常のJSON HTTP 400です。
- `SplitMessages`・`PromptInput`・`TokenBudgets`・usageからcaller領域を除去しました。
  `split_messages`へsystemが来たら固定文のValueErrorです。順序・削減順はADR 0031の7段階です。
  最新の完了往復と今回の入力・固定人格の保護、tool call/resultの不可分性は維持します。
- `prompt_budget.caller_instructions`はPydanticのextra-forbidで起動時エラーになります。
  設定JSONの残存3件（examplesの2件とevals/semanticの1件）を除去しました。
  `GET /v1/characters/{character_id}`の`prompt_budget`は8キーから7キー（totalと6領域）へ変わり、
  `caller_instructions`を返しません。成功completionのenvelopeは維持します。
- tools/tool_choice、assistant null+tool_calls、tool結果、text配列正規化、非stream/streamをIT1で確認しました。

## TDD・既存試験の期待値変更

最初に`tests/test_reject_caller_system.py`を追加して実行: **FAIL（41 failed, 1 passed）**。
拒否順序、廃止設定、metadataの差分を検出しました。metadata試験のキャラクターID指定漏れも修正しました。
評価の両入口構造検査を先に追加して実行: **FAIL（1 failed）**。
不正tool順序とのsystem拒否優先を追加して実行: **FAIL（4 failed, 2 passed）**。
いずれも後続の実装・修正後にPASSを確認しています。途中のimport撤去漏れ、削減stageの編集ミス、
型注釈・formatチェックにもFAILがあり、修正後に下表の全ゲートを再実行しました。
FAILやcollection errorをPASSとして扱っていません。

| 既存試験 | 期待値の変更と理由 |
| --- | --- |
| `test_prompt_builder.py`: complete_order、empty_regions、usage/repr、error/repr | caller message・regionを除去。残りの領域と本文非漏洩は引き続き検査 |
| 同: system_moves、partition_preserves_other_http_accepted_sequences | system抽出・systemのみ受理を廃止。tool継続の分割は維持し、system拒否は新規UT/IT1へ移した |
| 同: all_eight_reduction_stages → all_seven_reduction_stages | caller削減を除去。合成初期費用22→20、全7段階と無削減境界の合成予算20/18/16/12/11/7/5/4を検査 |
| 同: configurable_memory_floor、optional_region_limits | caller費用・削減件数を除去し、floor境界と残存領域上限を確認 |
| `test_prompt_assembly_api.py`: strict_budget、http_order、chat_keeps_accepted_sequences | 廃止regionのpositive試験を起動時拒否試験へ置換。caller systemの送信順・受理の期待を廃止 |
| `test_character_book.py` / `test_character_book_api.py`: scan_precedes_history_pruning、shared_scan_context | split/API入力からcaller systemを除去。Card/post_history/記憶/toolのscan除外と履歴削減前scanは維持 |
| `test_content_parts_api.py`: openclaw_tool_roundtrip_normalizes_provider_input | systemとCALLER_FRAMEの送信を除去しindexを補正。両Chat API・stream・tools・tool_choice・call/result・text配列を維持 |
| `test_injection_evaluation.py`: scoring、gate、matrix、report、route_policy | caller system応答を採点対象外へ。正規化・canary/persona逸脱の必須採点はmemoryを使用。応答15→12、構造3→6、構造キーはentry/run/id。callerはstructureで両内容ゲートfalse、memoryはtrue、userは参考のまま |
| `test_injection_runtime.py`: CLI/matrix、real fake、discarded_attack、external_content_gate、user fixture | Chat caller応答/canary枠を廃止し構造検査へ。provider/token counter呼出15→12、DB会話作成9→6。応答fixtureから構造ケースを除去。攻撃欠落試験はuser/memoryを維持し、caller受理・別エラー・誤コードは構造検査で確認 |

変更していないprovider/templateのsystem試験は、Coreが生成するsystemや内部契約の試験です。
`evals/persona`のJSONケースに`role: "system"`はありません。人格評価ケース・schema・Cardは変更していません。

## インジェクションのケース版

case schemaは版1→版2。各ケースの`assessment`を必須にし、caller_systemはstructure、user/memoryはresponse。
入力文・canary・persona_break表現は維持しています。caller応答fixtureを除去し、故意の内容違反fixtureはmemoryへ移しました。
`route_policy.kind`を報告し、caller systemは両入口の400 `system_message_unsupported`だけがPASSです。
受理は`override_channel_open`、誤コードや他エラーは`execution_incomplete`。構造probeは実provider・実tokenizerを呼ばずDB接続も不要です。

| ケース集合 | 変更前SHA-256 | 変更後SHA-256 |
| --- | --- | --- |
| synthetic | `36cb7a87d18c1f2fc9fd73b99b0dc03286549103b8a5a23b3bb3f4af4eaeba30` | `39835de3753d6ced2947a23433f7799420939838e96a4b6094a8ffac0fa2e938` |
| Miori | `af45f3e80282356ab9e69d9eee2da59a37277736ece0c24843795d445f442fbf` | `6f1a9eb23f91c60709b994879d5a454f458410037d1e491657fec3241799398d` |

## 必須ゲート

共通環境:

```sh
export T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
```

下表のruff対象は、`src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py
 tools/evaluate-persona-consistency.py tools/evaluate-injection-resistance.py`です。

| コマンド | 最終結果 |
| --- | --- |
| `uv sync --locked` | PASS（80解決、78 installed/audited） |
| `uv lock --check` | PASS（80解決、lock変更なし） |
| `uv run --no-sync ruff check` + 上記対象 | PASS |
| `uv run --no-sync ruff format --check` + 上記対象 | PASS（143 files already formatted） |
| `uv run --no-sync mypy` | PASS（143 source files） |
| `uv run --no-sync pytest -m ut -q` | PASS（987 passed, 1367 deselected） |
| `uv run --no-sync pytest -m it1 -q` | PASS（925 passed, 1429 deselected） |
| `uv build --no-build-isolation` | PASS（sdistとwheel） |
| `bash tools/test-postgres.sh` | PASS（442 passed, 1912 deselected。ネットワークなし・公開portなしの専用PostgreSQL 18コンテナ、実行後削除） |
| `(cd evals/semantic && npm ci --no-audit --no-fund)` | PASS（固定lock、575 packages。依存deprecation警告あり） |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS（59 registered tests、skip/todo/cancelなし） |
| `node tools/check-docs.mjs` | PASS（tracked text / local Markdown targets） |
| `git diff --check` | PASS |
| `uv run --no-sync python tools/evaluate-persona-consistency.py --mode fixture --runs 3 --output /dev/shm/177-persona-report.json` | PASS（応答18件、quality_evidence=false） |
| `uv run --no-sync python tools/evaluate-injection-resistance.py --mode fixture --runs 3 --output /dev/shm/177-injection-report.json` | PASS（応答12件、構造6件、全ゲート0、quality_evidence=false） |

追加のMiori fixture CLI（各response case IDへ合成safe-response-markerを割り当てた一時fixture）:

```sh
uv run --no-sync python tools/evaluate-injection-resistance.py --mode fixture \
  --cases evals/injection/miori.cases.json --fixture /dev/shm/177-miori-responses.fixture.json \
  --runs 3 --output /dev/shm/177-miori-injection-report.json
```

PASS（応答36件、構造18件、全ゲート0）。新規拒否・予算・評価の対象試験141件、評価IT1 48件もPASS。
`grep -rn caller_instructions src tests tools examples evals docs/api.md docs/history-api.md SPEC.md`では、
残存は廃止設定の拒否・metadata非表示を検査する新規試験と、廃止を説明する文書だけです。
pytestのdeselectedはmarkerによる別分類の除外で、skipや0件PASSではありません。

## NOT RUN・制約

- 実LLM・GPU・稼働中llama.cppコンテナでの評価、実運用DB、IT2/ST: **NOT RUN**（監督担当の範囲）。
- ツール結果からのインジェクション耐性評価: **NOT RUN**（ADR 0027の実装後）。tools互換のIT1とは別です。
- push・PR・merge: **NOT RUN**（今回の許可はローカルcommitまで）。
- fixture PASSは実モデルの人格品質・耐性の合格を意味しません。

## 監督側の再実行（2026-10-12）

`093ec79e221ef0bfcc51c4953959e3a19db2a450` で差分を読み、同じ固定ツールで再実行した。uv sync --locked・uv lock --check・ruff・mypy（143 files）・
UT（987 passed）・IT1（925 passed）・uv build・test-postgres（442 passed）・npm ci＋Node（59 tests）・check-docs・diff --check は PASS、SKIP 0。
人格評価とインジェクション評価の CLI（fixture、各3回）は exit 0。実モデルの評価は [インジェクション](2026-10-11-injection-real-model-evaluation.md)（PASS）と
[人格](2026-10-11-persona-real-model-evaluation.md)（FAIL：twilight の1回）の証跡に追記した。
