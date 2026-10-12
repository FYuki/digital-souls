# プロンプト組み立ての共通経路と予算（2026-10-11）

対象：[ADR 0026](../adr/0026-prompt-assembly.md) のA1レビュー修正とA2接続・設定・HTTP結合試験。
開始revisionは `839aa957892702f590dd84c1acfc2e4b36f3bd1e`、branchは `feature/152-prompt-assembly`。
指定worktreeだけで製品を変更し、push・PR・mergeは行っていない。
実装・文書commit：`76371c9a0c5f80b749c12db85c443a42fe06462f`。本証跡はその後の文書commitとして保存する。

## 変更ファイル（26件）

```text
SPEC.md
docs/api.md
docs/history-api.md
evals/semantic/characters.json
examples/characters.json
examples/characters.llamacpp.json
src/digital_souls_core/api.py
src/digital_souls_core/application.py
src/digital_souls_core/character.py
src/digital_souls_core/memory.py
src/digital_souls_core/prompt_builder.py
src/digital_souls_core/semantic_answer_runtime.py
src/digital_souls_core/semantic_evaluation_runtime.py
tests/memory_confirmation_contracts.py
tests/postgres_record_support.py
tests/support.py
tests/test_api.py
tests/test_content_parts_api.py
tests/test_contracts.py
tests/test_llamacpp_provider.py
tests/test_ollama_provider.py
tests/test_postgres_history_privacy.py
tests/test_postgres_semantic_answer.py
tests/test_prompt_assembly_api.py
tests/test_prompt_builder.py
docs/evidence/2026-10-11-prompt-assembly.md
```

## 変更

- Cardを型のまま保持し、PromptBuilderだけがprovider messagesへ変換する。日本語の5見出しとstrip済み本文を使用し、空要素・nameを省く。
- 最初のuser以前のassistant等を1つの削除可能な不完了単位とする。userのない要求・末尾assistantもHTTPの既存受理範囲を維持する。
- Chat API・会話履歴API・回答評価が共通のprepareを使用する。systemはどの位置からも呼び出し側指示へ移す。会話履歴APIのsystem拒否は維持する。
- MemoryContextは順位順のJSON object列と従来のJSON配列textを同時に返す。削減後もJSON配列を保ち、itemの途中を切らない。削減前の全候補を閉じ込めたvalid()、policy同一性、版・scope・検索設定のguardを保持する。記憶を全削減してもguardを外さない。
- Lore.matchesとBookの起動時制限は維持する。選択したLoreのinsertion_orderをafter_charで渡す。
- 完成したpayloadを従来のprivacy.authorizeで再検査し、送信直前の再検証も維持する。
- PromptBudgetExceededは400 `prompt_budget_exceeded`。messageは領域名だけ。PromptUsageはPreparedに保持し、HTTPへは出さない。
- strictな正の整数のprompt_budgetとmemory_floor/history_floorを設定へ追加する。旧context_budget_bytesはextra-forbidによる起動時エラー。post_historyカードは型として読み込む。
- 回答評価のcontext_empty・dispatch_memory_idsは、予算で削減した後の送信itemだけを観測する。撤回検証は全検索候補のまま。

## ADRに明記されていない実装判断

既存ContextSourceはmemory/skill用の信頼済み拡張で、製品側の現在の実装はEmptyContextのみ。
既存のsystem指示としての意味を保つためCardのsystem_prompt（応答方針）へ追加し、character予算へ含める。
人格・関係・生活状態のsnapshotではないのでRuntime Contextへ置かない。新しい保存・検索機能は加えていない。

記憶の検索は従来どおりconversation_idがある経路だけ。stateless Chat APIの検索対象範囲は広げない。
itemsを持たない既存の独自GuardedContext sourceは、そのtext全体を1つの削減単位として扱う。
JSON object itemかどうかは信頼済みserializerが明示し、PromptBuilder側で本文を解釈し直さない。

履歴下限は完了往復の件数。不完了単位（先頭assistant等）は下限Nにも必須の直前往復にも数えない。
これにより先頭assistantのために最後の記憶を先に捨てることを避ける。
userがない要求は非system部分を削除可能な1履歴単位として扱い、今回の入力は空。
最後のuser以降に通常のassistantがある既存の受理形も、今回の入力へそのまま含める。

既定予算はtotal/character/lore/memory/caller_instructions/history/current_input/post_historyの順に
28672/16384/8192/16384/8192/24576/16384/4096。全体は32768 contextから出力4096を予約した値。
旧examplesの固定文脈16 KiBの目安をcharacter・memoryに残し、Lore/外部指示には8 KiB、post_historyには4 KiBを与えた。
履歴には24 KiBを与えて既存の短い会話・件数上限撤廃fixtureを維持する。領域上限の合計は全体上限ではない。
ByteTokenCounterは日本語を多く見積もるため、長い履歴は実token数より早く削減され得る。
意味回答評価の設定は旧64 KiBの領域上限を残し、全体は同じ28672で制限する。
物理contextの実測・出力要求に応じた自動調整は行わず、運用者がprofileに合う予算を設定する。

`rg -n context_budget_bytes .` とJSON設定の検索を実行し、使用中のJSON設定3件をすべて移行した。
残る一致は廃止の説明・拒否/metadata absenceの試験・履歴として保持するADR/過去の証跡。

## 既存試験の期待値の変更

| 試験 | 変更 | 理由 |
| --- | --- | --- |
| test_prompt_builder.py: complete_order / empty_regions / lore_order | `description: ...` 等から日本語見出しへ | PoCとADRのCard書式を一致させる |
| test_prompt_builder.py: invalid_partition | systemのみ、先頭assistant、末尾assistantの3ケースを拒否集合から外し、別の受理回帰へ移す | HTTPが既に受理する形を拒否しない |
| test_contracts.py: context_lore_and_byte_budget → context_lore_and_token_budget | byte予算1・`byte budget`からcharacter token予算1・領域名`character`へ | 旧設定/byte予算の廃止 |
| test_api.py: empty_stream_errors_and_capability_metadata | context_budget_bytesからprompt_budget.total=28672と各floor=3、旧キーなしへ | metadataの設定schema変更 |
| test_content_parts_api.py: openclaw_tool_roundtrip_normalizes_provider_input（8 variants） | 正規化system本文にCALLER_FRAMEを付加 | caller指示の位置と意味を明示する。partsの正規化やtool往復の期待は維持 |
| test_postgres_semantic_answer.py: production_inference_dispatch_and_publication | `system_prompt:`から`## 応答方針`へ | Card書式の変更 |

support.py、memory_confirmation_contracts.py、postgres_record_support.py、llamacpp/ollama provider試験、
postgres_history_privacy.pyのCharacter生成は、平坦化済み文字列からCharacterCardを渡すように変更した。
これらはfixtureの型変更であり、privacy・provider契約の受入期待を緩めていない。
既存の記憶JSON配列の試験期待は変更していない。

## HTTP応答の形

completion成功JSON/SSE・headersの形は維持する。領域usage/削減件数はHTTPへ追加しない。
`GET /v1/characters/{character_id}`だけは旧context_budget_bytesを削除し、prompt_budget object（8整数）と
memory_floor/history_floorの整数を追加する。物理context上限のmetadataはnullのまま。
予算超過は従来のcore_error envelopeでcodeがprompt_budget_exceeded、messageが領域名となる。

## TDDと途中のFAIL

以下のFAILは修正前の観測であり、PASSへ読み替えていない。

- `uv run --no-sync pytest tests/test_prompt_builder.py -q`：A1修正前は8 failed / 52 passed。見出し・strip・先頭assistant・既存受理形を追加してから実装した。
- `uv run --no-sync pytest tests/test_prompt_assembly_api.py -q`：fixtureのNone設定をexclude_noneで修正後、A2接続前は13 failed / 50 passed。post_history拒否、旧byte設定の受理、未導入token設定が原因。
- JSON item試験単独：MemoryItemのJSON形式指定導入前は1 failed。その後、既存IT1で検出したJSON配列の退行を修正した。
- 完了往復下限の先頭assistant試験単独：修正前は1 failed。下限を履歴の全単位数から完了往復数へ修正した。
- PostgreSQLのanswer_observation_tracks_only_memory_items_in_final_payload単独：修正前は1 failed / 1 passed。全記憶削減時の回答評価metadataを修正した。
- 途中のIT1全体：9 failed / 724 passed（caller枠の期待値8件、記憶JSON配列の退行1件）。前者は仕様どおり期待値を更新、後者は実装を修正して既存期待を維持した。
- 初期ruffはformat前の長い行、初期mypyは評価runtimeの旧Character文字列でFAIL。整形と型移行後に下表の最終確認を実行した。

## 最終ゲート

環境：Python 3.12、uv 0.8.22、Node 24.19.0。各コマンドに次を設定した。

```sh
export T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
export UV_OFFLINE=1
export npm_config_offline=true
```

依存は手元のcacheのみ。固定PostgreSQL 18 imageの存在をdocker image inspectで確認してから起動した。
使い捨てcontainerは公開portなし・network none・合成データのみで、終了時に削除した。
llama.cppや他のcontainerは操作していない。pytestのdeselectedは他markerの除外でありskipではない。

| コマンド | 最終結果 |
| --- | --- |
| `uv sync --locked` | PASS（80 resolved / 78 audited、lock変更なし） |
| `uv lock --check` | PASS |
| `uv run --no-sync ruff check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py` | PASS |
| `uv run --no-sync ruff format --check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py` | PASS（120 files） |
| `uv run --no-sync mypy` | PASS（120 source files） |
| `uv run --no-sync pytest -m ut -q` | PASS（812 passed / 1171 deselected） |
| `uv run --no-sync pytest -m it1 -q` | PASS（739 passed / 1244 deselected） |
| `uv build --no-build-isolation` | PASS（sdist / wheel） |
| `bash tools/test-postgres.sh` | PASS（432 passed / 1551 deselected） |
| `(cd evals/semantic && npm ci --no-audit --no-fund)` | PASS（575 packages、offline cache、既存依存のdeprecated警告あり） |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS（59 required tests、skip/TODO/cancelなし） |
| `node tools/check-docs.mjs` | PASS |
| `git diff --check` | PASS |

pytestには既存Starlette/anyio deprecationとPydantic ReadOnly警告がある。必須試験のSKIPは0。
本文なしの合成回答評価追加確認：`bash tools/evaluate-semantic-answer.sh --runs 3 --output /dev/shm/prompt-assembly-answer-1791704651728.json`、
PASS（62ケース×3回、cacheなし、実モデル品質の証拠ではない）。実モデル品質の証明ではない。

## NOT RUNと後続の範囲

実LLM・GPU・外部通信、llama.cpp/Gemma 4の実chat templateによるIT2、tokenize実測、人格の実モデル評価・STはNOT RUN。
この版はByteTokenCounter固定。Runtime Contextの状態生成、Book解釈/選択の拡張は今回の実装対象外。
未確認の物理context上限を確認済みとしていない。push・PR・mergeは実行していない。

## 監督側の再実行（2026-10-11）

`f43f403c15ab4da2b7c8865a7b5cbdcc83e443e7` で、差分を読んだうえで同じ固定ツール・`TMPDIR=/dev/shm` により再実行した。
`uv sync --locked`、`uv lock --check`、ruff check / format --check、mypy（120 files）、UT（812 passed）、IT1（739 passed）、
`uv build --no-build-isolation`、`bash tools/test-postgres.sh`（432 passed）、`npm ci`（offline）、Node 必須試験（59 tests）、
`node tools/check-docs.mjs`、`git diff --check` はすべて PASS。SKIP は0件。合成回答評価の再実行は NOT RUN。
