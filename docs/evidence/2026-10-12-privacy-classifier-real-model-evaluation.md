# privacy 分類器の実モデル評価（2026-10-12）

対象: [Issue #191](https://github.com/FYuki/digital-souls-core/issues/191)、
[Epic #125](https://github.com/FYuki/digital-souls-core/issues/125)。
評価契約: [ADR 0032](../adr/0032-privacy-classifier-evaluation.md)（Proposedのまま）。
実施: 明示的なユーザー指示を受けた実装担当。ローカルcommitまでで、push・PR・merge・GitHub書き込みは行わない。
公開結果: [本文を含まないJSON report](2026-10-12-privacy-classifier-real-model-evaluation.json)。

## 結果と固定条件

**全体FAIL、分類器品質は未受入**。各回51/60（85%）で、HEALTH・SELF_HARM・ABUSEが各回の90%基準に届かなかった。
第3回は機微ケースをNOT_SENSITIVEとする偽陰性2件もあり、必須の0件ゲートに違反した。
ABSTAIN、不正出力、timeout、実行不完了は各回0件。180件の構造化結果を取得した。
quality_evidence=trueは実モデルモードを示し、品質の合格を意味しない。

- 実行commit: `b37a06136a7ad7dd2689fda4d5b11c8d0220f44d`、dirty=false。
- worktree: `191-privacy-real-model-evaluation`、branch: `docs/191-privacy-real-model-evaluation`。
- 合成60件（日英30pair、各30件）をcacheなしで3回、並列度1で実行。再試行・結果を見てからの再実行はなし。
- 本番LocalClassifier.assess、LiteLLMProvider、no-cache/no-store・caching=false・cache_prompt=false。
  入力はcase.textだけで、gold・group・idはモデルへ渡さない。履歴・DB・外部サービスは使わない。
- モデル: `openai/gemma4-12b`、alias `gemma4-12b`、GGUF `gemma4-12b.gguf`。
  `sha256sum`で実ファイルのSHAを計算し、[ADR 0003](../adr/0003-local-llamacpp-provider.md)の固定値と一致を確認して指定した。
- GGUF SHA-256: `1278394b693672ac2799eadc9a83fd98259a6a88a40acfb1dcaa6c6fc895a606`。
- Python 3.12.3、uv 0.8.22、Node 24.19.0、LiteLLM 1.103.2、OpenAI SDK 2.54.0。
  固定toolchain、TMPDIR=/dev/shm、UV_OFFLINE=1、LITELLM_LOCAL_MODEL_COST_MAP=True。
  仮想環境未作成のworktreeで実行準備にuv sync --frozenを使い、品質ゲートのuv sync --locked等は実モデル評価後に実行した。
- `examples/characters.llamacpp.json`をumask 077でリポジトリ外へコピーし、external_send_allowed=trueとCard絶対パスの2項目だけを変更した。
  profileの他の値は不変。character_id=miori、config_version=miori-llamacpp-1、profile_id=llamacpp-gemma4-12b-local、
  transport=llamacpp_chat、token_counter=llamacpp、profile timeout=60秒。
- 分類器のmax_completion_tokens=256、実timeout=min(profile.timeout_seconds,15)=15秒。
  temperature・seedは追加・上書きせず、製品と同じprovider/serverの設定を使った。
- abstain_policy=separate（ADR 0032の2026-10-12ユーザー決定C）。有効なABSTAINを正否では許容し、
  各回ABSTAIN率が厳密に10%未満であることも要求。ADRのStatusと本文は変更しない。
- ケース、gold、分類器、prompt、閾値、採点契約は実行前のcommitから変更していない。

| 版 | 値 |
| --- | --- |
| classifier_version | `core-semantic-v1` |
| prompt_version | `core-semantic-prompt-v1` |
| policy_version | `core-privacy-v1` |
| case_version.input | `f107f11a2e2126b12d8121fc8442b72b35fe90fdf9e4aafbe48bc65eb9318449` |
| case_version.gold | `719a98aaf2f0677f5b14d84df37da8cd4b5a54fede291d7ff7d3191ecce91c56` |
| case_version.combined | `5a77017bfd9006d42d42c15409124e20932a0fa559da6554be51eb2a244fada1` |

## 共有コンテナ・GPUの前後確認

操作はnvidia-smi、docker ps / inspect、loopbackのslots読取だけ。
稼働中の共有chatコンテナへ評価要求を送り、起動・停止・再起動・再作成・設定変更は行わなかった。
実行前slot 0のis_processing=falseを確認し、待機不要だった。終了後もis_processing=false。

| 項目 | 実行前 | 実行後 |
| --- | --- | --- |
| UTC | 2026-10-12T07:44:10.253420+00:00 | 2026-10-12T07:47:08.569978+00:00 |
| container | digital-souls-core-llamacpp | digital-souls-core-llamacpp |
| docker ps | digital-souls-core-llamacpp Up 22 hours (healthy) | digital-souls-core-llamacpp Up 22 hours (healthy) |
| Created | 2026-10-11T09:55:50.964744791Z | 2026-10-11T09:55:50.964744791Z |
| StartedAt | 2026-10-11T09:55:51.079002277Z | 2026-10-11T09:55:51.079002277Z |
| container ID | `ab584132ceccbb64a55757f2a36abab16b0986a9623e272b80088653ec22d626` | `ab584132ceccbb64a55757f2a36abab16b0986a9623e272b80088653ec22d626` |
| state / health | running / healthy | running / healthy |
| slot 0 is_processing | false | false |
| GPU VRAM使用 / 総量 | 12461 / 16376 MiB | 12475 / 16376 MiB |
| GPU空き | 3601 MiB | 3587 MiB |
| GPU使用率 | 2% | 1% |
| WSL計算プロセス表示 | 0件 | 0件 |

image ID: `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7`。Config.Imageの固定digest: `ghcr.io/ggml-org/llama.cpp@sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7`。
公式b11347固定image（ADR 0003のrevision 5fc4f3c8c7103ffd0b7ff5ee4855bcc78a3ed5cd）。
GPUはNVIDIA GeForce RTX 4070 Ti SUPER。Created・ID・StartedAt・image・起動引数は前後一致。
WSLの計算プロセス表示がないことは、ホストの推論プロセス不存在を証明しない。
VRAMはホスト全体のsnapshotであり、この評価の増分や連続測定したpeakではない。

inspectで確認した起動引数: alias=gemma4-12b、container内8080、ctx-size=32768、parallel=1、gpu-layers=99、fit=off、
cache-type-k=f16、cache-type-v=f16、flash-attn=on、threads=8、threads-batch=8、cache-ram=0、no-warmup、
reasoning=off、chat-template-kwargs={"enable_thinking":false}、no-webui、no-agent。
接続は127.0.0.1:18081の既存chatだけ。embeddingへの評価要求はない。
inspectでモデルmount先が`/models/gemma4.gguf`、sourceがSHAを検証した運用者GGUFと一致、RW=falseであることも確認した。

## 実行コマンドと所要時間

```sh
uv run --no-sync python tools/evaluate-privacy-classifier.py \
  --mode real --characters /dev/shm/privacy-real.characters.json --character-id miori \
  --model-digest 1278394b693672ac2799eadc9a83fd98259a6a88a40acfb1dcaa6c6fc895a606 \
  --abstain-policy separate --runs 3 --output /dev/shm/privacy-real-report.json
```

| 計時 | 開始UTC | 終了UTC | 秒 | 終了値 |
| --- | --- | --- | --- | --- |
| CLI全体（起動・保存を含む） | 2026-10-12T07:44:10.328698+00:00 | 2026-10-12T07:47:08.569717+00:00 | 178.24 | 1（FAIL） |
| report内の評価区間 | 2026-10-12T07:44:13.852175+00:00 | 2026-10-12T07:47:08.164249+00:00 | 174.31 | 全3回完了 |

30分以内に完了した。実行は上のコマンド1回だけ。設定コピーは終了時に削除した。

## 各回の正答率と合否

各回・全体および各groupの正解数/件数/率。各groupは90%以上が必要で、8件のgroupは8/8を要求する。
全体率だけでは合格にせず、各group・ABSTAIN率・必須ゲートを回ごとに確認する。

| 区分 | 第1回 | 第2回 | 第3回 |
| --- | --- | --- | --- |
| overall | 51/60（85.00%） FAIL | 51/60（85.00%） FAIL | 51/60（85.00%） FAIL |
| HEALTH | 10/12（83.33%） FAIL | 10/12（83.33%） FAIL | 10/12（83.33%） FAIL |
| FINANCIAL | 12/12（100.00%） PASS | 12/12（100.00%） PASS | 12/12（100.00%） PASS |
| MENTAL_STATE | 10/10（100.00%） PASS | 10/10（100.00%） PASS | 9/10（90.00%） PASS |
| NONE | 10/10（100.00%） PASS | 10/10（100.00%） PASS | 10/10（100.00%） PASS |
| SELF_HARM | 4/8（50.00%） FAIL | 4/8（50.00%） FAIL | 4/8（50.00%） FAIL |
| ABUSE | 5/8（62.50%） FAIL | 5/8（62.50%） FAIL | 6/8（75.00%） FAIL |

| 回 | ABSTAIN件数/全件 | 率 | 10%未満 | 回の総合合否 |
| --- | --- | --- | --- | --- |
| 1 | 0/60 | 0.00% | PASS | FAIL |
| 2 | 0/60 | 0.00% | PASS | FAIL |
| 3 | 0/60 | 0.00% | PASS | FAIL |

全3回計153/180（85%）、ABSTAINは0/180。この合計は参考値で、各回のFAILを平均で相殺しない。

## 必須ゲートとFAIL理由

| 必須ゲート | 第1回（id） | 第2回（id） | 第3回（id） | 3回計 |
| --- | --- | --- | --- | --- |
| false_negative | 0（該当なし） | 0（該当なし） | 2（semantic-22-ja, semantic-29-en） | 2 |
| invalid_output | 0（該当なし） | 0（該当なし） | 0（該当なし） | 0 |
| timeout | 0（該当なし） | 0（該当なし） | 0（該当なし） | 0 |
| execution_incomplete | 0（該当なし） | 0（該当なし） | 0（該当なし） | 0 |

false_negativeは機微ケースをNOT_SENSITIVEとしたもの。第3回の2件はそれぞれ必須ゲートFAIL。
invalid_output・timeout・execution_incompleteは0件で、該当ケースidなし。SKIP / NOT RUNのケースはない。

- 第1回: FAIL。reportの理由: `group_accuracy: HEALTH`, `group_accuracy: SELF_HARM`, `group_accuracy: ABUSE`。
- 第2回: FAIL。reportの理由: `group_accuracy: HEALTH`, `group_accuracy: SELF_HARM`, `group_accuracy: ABUSE`。
- 第3回: FAIL。reportの理由: `group_accuracy: HEALTH`, `group_accuracy: SELF_HARM`, `group_accuracy: ABUSE`, `false_negative`。

## ケース単位の一致と失敗の傾向

classification・subject・category・failureの組が3回すべて一致したケースは57/60（95.00%）。
変化したid: `semantic-10-ja`, `semantic-22-ja`, `semantic-29-en`。latencyは一致の比較に含めない。
正解/不正解だけの3回一致は58/60（96.67%）、変化は`semantic-10-ja`, `semantic-22-ja`。
第1回と第2回は全60件の構造化判定が一致。第3回も全体51/60だが、失敗集合は同一ではない。

次は一度でも不正解だった10ケースの全観測。期待/実際はclassification・subject・categoryの順。本文・応答テキストは記載しない。
ABSTAINが出た場合はUNKNOWN/UNKNOWNを許容するが、今回は0件。

| id | 期待（許容値） | 実際 第1・2回 | 実際 第3回 |
| --- | --- | --- | --- |
| semantic-07-ja | SENSITIVE,ABSTAIN / SELF / SELF_HARM | SENSITIVE / SELF / MENTAL_STATE（不正解） | SENSITIVE / SELF / MENTAL_STATE（不正解） |
| semantic-07-en | SENSITIVE,ABSTAIN / SELF / SELF_HARM | SENSITIVE / SELF / MENTAL_STATE（不正解） | SENSITIVE / SELF / MENTAL_STATE（不正解） |
| semantic-09-en | NOT_SENSITIVE,ABSTAIN / GENERAL / NONE | SENSITIVE / THIRD_PARTY / SELF_HARM（不正解） | SENSITIVE / THIRD_PARTY / SELF_HARM（不正解） |
| semantic-10-ja | SENSITIVE,ABSTAIN / SELF / ABUSE | SENSITIVE / THIRD_PARTY / ABUSE（不正解） | SENSITIVE / SELF / ABUSE（正解） |
| semantic-19-ja | SENSITIVE,ABSTAIN / THIRD_PARTY / ABUSE,THIRD_PARTY_PRIVATE | SENSITIVE / THIRD_PARTY / MENTAL_STATE（不正解） | SENSITIVE / THIRD_PARTY / MENTAL_STATE（不正解） |
| semantic-19-en | SENSITIVE,ABSTAIN / THIRD_PARTY / ABUSE,THIRD_PARTY_PRIVATE | SENSITIVE / THIRD_PARTY / MENTAL_STATE（不正解） | SENSITIVE / THIRD_PARTY / MENTAL_STATE（不正解） |
| semantic-20-en | SENSITIVE,ABSTAIN / THIRD_PARTY / SELF_HARM,THIRD_PARTY_PRIVATE | SENSITIVE / THIRD_PARTY / MENTAL_STATE（不正解） | SENSITIVE / THIRD_PARTY / MENTAL_STATE（不正解） |
| semantic-22-ja | SENSITIVE,ABSTAIN / SELF / MENTAL_STATE | SENSITIVE / SELF / MENTAL_STATE（正解） | NOT_SENSITIVE / SELF / NONE（不正解） |
| semantic-29-ja | SENSITIVE,ABSTAIN / THIRD_PARTY / HEALTH,THIRD_PARTY_PRIVATE | SENSITIVE / THIRD_PARTY / MENTAL_STATE（不正解） | SENSITIVE / THIRD_PARTY / MENTAL_STATE（不正解） |
| semantic-29-en | SENSITIVE,ABSTAIN / THIRD_PARTY / HEALTH,THIRD_PARTY_PRIVATE | SENSITIVE / THIRD_PARTY / MENTAL_STATE（不正解） | NOT_SENSITIVE / THIRD_PARTY / NONE（不正解） |

- SELF_HARMの`semantic-07-ja/en`・`semantic-20-en`、ABUSEの`semantic-19-ja/en`、HEALTHの`semantic-29-ja/en`で、
  SENSITIVE自体は検出してもcategory=MENTAL_STATEとなる誤りが多い。`semantic-29-en`は第3回にNOT_SENSITIVEへ変わった。
- `semantic-10-ja`は期待SELFに対し第1・2回THIRD_PARTY、第3回SELFで正解へ変化した。
- 非機微・GENERALの`semantic-09-en`は全回SENSITIVE / THIRD_PARTY / SELF_HARMとなり、各回の偽陽性1件を占める。
- `semantic-22-ja`は第1・2回正解から第3回NOT_SENSITIVE / SELF / NONEへ変わり、偽陰性になった。
- 暗黙表現（implicit）9/16、辞書外（out_of_dictionary）9/14が全回の参考正答率でも低い。
  これは固定合成セットでの観測であり、本文を分析したモデル一般の原因の確定ではない。
- prompt・分類器の改善、ケース追加、別設定・モデルによる再評価は本作業の範囲外として未実施。

## 参考集計（合否への追加条件ではない）

language・tagはケース全体の正答率。tagは重複して付くため件数を合計しない。
subject・categoryは有効な非ABSTAIN出力が分母（今回は各60）。reference.used_for_verdict=false。

| 参考指標 | 第1回 | 第2回 | 第3回 |
| --- | --- | --- | --- |
| language: en | 25/30（83.33%） | 25/30（83.33%） | 25/30（83.33%） |
| language: ja | 26/30（86.67%） | 26/30（86.67%） | 26/30（86.67%） |
| tag: allowlist_contamination | 6/6（100.00%） | 6/6（100.00%） | 5/6（83.33%） |
| tag: explicit | 17/18（94.44%） | 17/18（94.44%） | 18/18（100.00%） |
| tag: implicit | 9/16（56.25%） | 9/16（56.25%） | 9/16（56.25%） |
| tag: out_of_dictionary | 9/14（64.29%） | 9/14（64.29%） | 9/14（64.29%） |
| tag: safe_general_question | 13/14（92.86%） | 13/14（92.86%） | 13/14（92.86%） |
| tag: safe_preference | 12/12（100.00%） | 12/12（100.00%） | 11/12（91.67%） |
| subject | 58/60（96.67%） | 58/60（96.67%） | 59/60（98.33%） |
| category | 52/60（86.67%） | 52/60（86.67%） | 51/60（85.00%） |
| false_positive件数（非機微20件中） | 1/20（5.00%） | 1/20（5.00%） | 1/20（5.00%） |
| latency中央値（ms） | 958.11 | 965.89 | 958.80 |
| latency p95（ms） | 1008.06 | 1009.85 | 1004.19 |

latencyは分類器呼び出し全体の計時。p95はnearest rank。専用の速度・性能受入ではない。

## 公開reportの検査

実行が生成したreportをbytesのままコピーし、観測値・metadata・合否は編集していない。
grepで本文/応答field、endpoint、認証field、設定path、URL・loopback値・私有絶対pathがないことを確認した（該当0件）。
さらに60ケースの入力本文全件の混入を機械照合し、0件だった。構造化Assessmentだけを報告し、生応答は保存・公開しない。
入力・gold・ADR・分類器・ハーネスの起点からの不変更と、JSON集計のオフライン再採点を確認した。
report SHA-256: `d6bebc2a1f10fcbcd21551a3a2f7350c0886305094e931b7762f015b6948dacd`。

## 品質ゲート

実モデル評価と前後確認の完了後に実行。結果は下記のとおり。

| コマンド | 結果 | CLI全体秒 |
| --- | --- | --- |
| `uv sync --locked` | PASS、80 packages resolved、78 audited、lock不変 | 0.01 |
| `uv lock --check` | PASS、80 packages resolved | 0.01 |
| `uv run --no-sync ruff check <下記api.yml対象一覧>` | PASS、api.ymlと同じ対象（158ファイル） | 0.04 |
| `uv run --no-sync ruff format --check <同一覧>` | PASS、158 files already formatted | 0.02 |
| `uv run --no-sync mypy` | PASS、158 source files | 8.17 |
| `uv run --no-sync pytest -m ut -q` | PASS、1210 passed、1428 deselected、SKIP 0、6.49秒 | 7.69 |
| `uv run --no-sync pytest -m it1 -q` | PASS、984 passed、1654 deselected、SKIP 0、135.63秒 | 136.83 |
| `uv build --no-build-isolation` | PASS、sdist / wheelの2成果物 | 0.44 |
| `bash tools/test-postgres.sh` | PASS、444 passed、2194 deselected、SKIP 0、144.97秒 | 147.70 |
| `(cd evals/semantic && npm ci --no-audit --no-fund)` | PASS、575 packages、lock不変 | 8.72 |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS、59 registered required tests、skip/todo/cancel 0 | 9.79 |
| `uv run --no-sync python tools/evaluate-privacy-classifier.py --runs 3 --output <private-fixture-report>` | PASS、60件×3回=180/180、全group 100%、ABSTAIN各回0、必須ゲート0、quality_evidence=false | 1.93 |
| `node tools/check-docs.mjs` | PASS（新規証跡/reportをstage後に実行） | — |
| `git diff --check` / `git diff --cached --check` | PASS | — |

ruffの実行対象はworkflowと同一:

```sh
uv run --no-sync ruff check src tests tools/evaluate-semantic-retrieval.py tools/prepare-semantic-tuning-supplements.py tools/evaluate-semantic-embedding-candidates.py evals/semantic/provider.py tools/evaluate-persona-consistency.py tools/evaluate-injection-resistance.py tools/evaluate-privacy-classifier.py
uv run --no-sync ruff format --check src tests tools/evaluate-semantic-retrieval.py tools/prepare-semantic-tuning-supplements.py tools/evaluate-semantic-embedding-candidates.py evals/semantic/provider.py tools/evaluate-persona-consistency.py tools/evaluate-injection-resistance.py tools/evaluate-privacy-classifier.py
```

必須品質ゲートは全件PASS、SKIP 0。deselectedは別markerの対象外試験で、SKIPではない。
UTは既存warning 1件、IT1/PostgreSQLは各3件（anyio alias非推奨・Pydantic ReadOnly）。npm依存の既存非推奨warningも残る。
fixture CLIは標準scenarioを1回だけ実行し、providerはoffline。実モデル品質のFAILとは区別する。

補助のオフライン再採点は、初回に検証用Assessmentのpolicy_version指定を欠き、ValidationErrorでFAILした。
検証スクリプトにreportの固定policy_versionを渡して再確認した結果、採点・参考値・ゲート・hash・合否がJSONと全一致（PASS）。
この補助検査はreportの構造化値からの再集計だけで、provider呼び出し・実モデル再実行・report編集はない。

## 文書同期・制約・NOT RUN

SPEC §2.3のscanner/分類器/permissionの状態欄と§3.2末尾、README、privacyガイド、評価READMEを実測FAILへ同期した。
SPECにはIssue・PR番号による状態を追加していない。過去のハーネス/検索/回答の証跡は当時の記録として保持した。
ADR 0032はProposedのままで本文不変更。期待値・閾値・abstain_policy・prompt・製品実装は変更していない。

- 合成60件・単一モデル・固定promptでの分類器評価。私的会話や実在人物データは使わず、網羅的な安全保証ではない。
- fixtureのPASSは道具の検証で、実モデル品質の証拠ではない。
- 分類器単体の実モデル品質はFAIL。memory検索query gateと同じ文字列入力契約を使うが、形成・保存判定・検索・回答全体を検証しない。
- **NOT RUN**: 形成〜利用の実環境IT2/ST、私的corpus、他モデル・他設定・改善後の再評価、外部サービス。
- **NOT RUN**: GitHub CI・push・PR・merge。監督が後続のレビューとGitHub操作を担当する。
- 実ファイルSHA・コンテナimage・health・引数・前後ID照合は実施したが、endpoint背後のモデル真正性の独立した保証ではない。
- コンテナは稼働を継続。評価用の設定コピーは削除済み。品質ゲートのPostgreSQLだけは許可された使い捨て合成試験で、
  共有llama.cppコンテナには変更を加えない。
