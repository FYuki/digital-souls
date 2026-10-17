# Issue #198: 外部回答評価経路（実送信 NOT RUN）

正本：[Issue #198](https://github.com/FYuki/digital-souls-core/issues/198)、
[Epic #126](https://github.com/FYuki/digital-souls-core/issues/126)。2026-10-12 に読み取り照合。

## revision・境界

- 作業場所：`198-luna-answer-path`、branch `feature/198-luna-answer-path`。
- 起点：`5919bcfbe3e803e5a6ad3423c4166cdeeeb74158`。対象実装は本証跡を含む commit。
- 第1ラウンドのゲートは commit 前の変更済み作業ツリーで実行。本証跡追加後に文書検査・差分検査を再実行。
  第2ラウンドは末尾に記録し、追加 commit 後に指定ゲートを実行する。
- Python 3.12.3、uv 0.8.22、Node 24.19.0、promptfoo 0.117.2。依存・lock 変更なし。
- PostgreSQL 18 は既存 digest 固定の使い捨てコンテナ、network none、公開 port なし、Unix socket。
- 実 GPT-6 Luna・外部送信・実ローカルモデル・実 embedding サーバー利用：**NOT RUN**。
  既存 chat/embedding サービスの起動・停止・変更なし。外部用試験は偽 Provider/SDK、embedding は偽応答。
- push・PR 作成・merge・remote CI：**NOT RUN**。実測と SPEC 同期は #199。

## 設計

`external_model` と別型 `ExternalAnswerProfile` を追加。git 管理外かつリポジトリ外の絶対パスを要求し、
symlink の解決先も検査する。enabled と embedding.enabled、chat.external_send_allowed が必須。
chat は本番 Profile の sdk と OpenAI 互換 provider に限定し、model の既定値は置かない。
本番 Profile・LiteLLMProvider・privacy のコードと、既存 AnswerProfile のローカル検査は変更しない。

JSON の `api_key_env` / 任意 `api_base_env` は名前だけ。名前の形式・プロセス制御名との衝突を検査し、
runner は指名した値だけを転送する。Python はその値を OPENAI_API_KEY / OPENAI_BASE_URL へ写し、
終了時に復元する。指名した元変数は SDK 実行中に外して、別の SDK 設定としての利用を防ぐ。
未指定の資格情報・proxy・dotenv は引き継がない。dry-run 以外の実行 flag/profile 欠落は NOT RUN（exit 3）、
値欠落は dispatch 前に FAIL。dry-run は credentials を転送せず、キーを必要としない。

既存 complete_answer を共有し、本番 Inference.prepare の external 認可（scanner、LocalClassifier、
PrivacyPolicy）と直前の inference.check を通す。評価 operator grant に external を加えるのは
external_model のみ。分類器 provider の応答だけを synthetic NOT_SENSITIVE とする既存条件は維持。
embedding は LocalEmbedding、分類器 profile は管理 loopback の llamacpp_chat。
Node network_guard と fixture の IP 拒否は維持する。第2ラウンドでは依頼範囲外だった
local_model の追加ネットワーク制限を撤去し、既存の挙動へ戻した。

external の manifest/identity には `classifier_reason="baseline-parity-no-classifier-export"` を記録します。
比較基準の gemma4-12b 評価（[2026-10-10 証跡](2026-10-10-semantic-real-model-answer-bge-m3.json)）も synthetic で条件をそろえるためです。
分類器は外部モデルへ送らずローカルのまま維持し、実分類器での受入は Issue #128 の範囲です。
fixture/local_model の report にはこのフィールドを追加しません。

外部 report に provider / model / transport / external_send=true / cache=false / run_count を追加する。
既存 runs、commit、quality_evidence 等の意味・fixture/local_model のフィールドは維持。
接続 URL・環境変数名/値・キー・ケース本文・query・回答全文は公開 report に含めない。
SDK の stdout/stderr は provider 内で捕捉し、runner の公開失敗理由も固定識別子だけに限定する。

`--mode external_model --profile <絶対パス> --dry-run` は実行 flag なしで進む。
`--dry-run` と `--execute-external-model` の同時指定は profile 読み込み前に引数エラー（FAIL、送信なし）。
`--dry-run` は本番 PG 登録・LocalEmbedding・prepare・撤回検査を通し、送信しない Provider で
dispatch を計数する。1回分を runs 倍し、入力は既存 ByteTokenCounter の UTF-8 byte 推定。
出力は CLI の仮定値、単価は100万 token あたりの任意 CLI 値のみ。通貨換算しない。
quality_evidence=false / external_send=false / status=DRY RUN は合格判定ではない。

JS 試験内の合成計数による出力例（実モデル見積もりではない）：

```json
{
  "status": "DRY RUN",
  "quality_evidence": false,
  "external_send": false,
  "runs": 3,
  "estimate": {
    "calls_per_run": 60,
    "calls": 180,
    "input_tokens": 30000,
    "token_estimator": "utf8-byte-estimate",
    "output_tokens_per_call_assumption": 100,
    "output_tokens_assumption": 18000,
    "input_cost": 0.06,
    "output_cost": 0.072,
    "total_cost": 0.132
  }
}
```

上記は入力単価2・出力単価4を同じ通貨で明示した計算試験。単価未指定時は金額を出さない。

## 受入条件と試験

試験名は [Python](../../core/tests/test_semantic_answer_external.py)、
[PG](../../core/tests/test_postgres_semantic_answer.py)、[JS](../../core/tools/semantic-answer.test.mjs) を参照。

- a：`test_external_dispatch_checks_production_policy_before_provider[permission/grant]`。
- b：同試験の `[None/scanner/classifier/revoke]`。authorize/check の呼出順と Provider 0回を観測。
- c：`test_external_provider_public_output_redacts_sdk_stdout_stderr_and_exception`、
  JS `external reports remain body-free and disclose only approved execution identity`、
  `external missing credential fails before execution with secret-free stdout stderr report`。
- d：`test_external_identity_retains_local_adapters_and_has_no_secrets`、PG
  `test_external_promptfoo_provider_uses_production_litellm_with_only_local_auxiliaries`。
- e：`test_external_profile_fail_closed`、`test_external_profile_requires_values_without_disclosing_them`、
  `test_repository_profile_cannot_enable_external_sending`、`test_provider_external_opt_in_is_required_before_transport`、
  `test_dry_run_uses_prepared_payload_token_estimator_and_never_keeps_body`、PG
  `test_external_dry_run_prepares_all_cases_with_local_embedding_without_answer_transport`、
  JS `external_model requires its own execution flag and profile (exit 3)` と dry-run 計数・費用試験。
- f：JS `external runner forwards only nominated credentials and never in dry-run`。
- 回帰：`test_external_profile_never_relaxes_local_validation`、既存 fixture/local_model の試験を変更せず実行。
  JS の fixture manifest フィールド集合を照合。期待値・閾値・prompt・gold・ケース・採点は変更なし。

## 第1ラウンドの品質ゲート（commit 前の作業ツリー）

```sh
T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
```

- `uv sync --locked` / `uv lock --check`：PASS、80 packages の lock。
- `.github/workflows/api.yml` の対象一覧で ruff check / ruff format --check：PASS、format 160 files。
- `uv run mypy`：PASS、160 source files。新規 Python は src/tests 配下で既存一覧に含まれる。
- `uv run pytest -m ut -q`：PASS **1222件**。
- `uv run pytest -m it1 -q`：PASS **993件**。
- `uv build --no-build-isolation`：PASS、sdist/wheel の2成果物。
- `bash tools/test-postgres.sh`：PASS **446件**。
- `cd evals/semantic && npm ci --no-audit --no-fund`：PASS、575 packages。
- `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs`：PASS **64件**。
- `bash tools/evaluate-semantic-answer.sh --runs 3`：PASS **62ケース×3回**、fixture、quality_evidence=false。
  report は起点 commit・dirty=true の作業ツリーを記録。raw/private 一時領域は終了時に削除。
- `node tools/check-docs.mjs` / `git diff --check`：PASS。
- SKIP/xfail/TODO/cancelled：**0件**。marker の対象外（deselected）は SKIP ではない。
- 外部実送信、実ローカルモデル/embedding、IT2/ST、remote CI：**NOT RUN**。

初回 PostgreSQL は追加 dry-run 試験が **1 FAIL / 444 PASS**。試験用4次元 vector と
LocalEmbedding profile の1024次元が不一致だった。偽応答をゼロで拡張して内積を維持し、
対象試験1件 PASS、その後全446件 PASS。製品の検索設定やケース vector は変更していない。
初回の追加 JS report 試験も撤回済み null 回答へセンチネルを付けた試験の誤りで1件 FAIL。
null を保持するよう修正し、全64件 PASS を再確認。実モデル品質の合格には扱わない。

## 第2ラウンドの修正と検証

起点は `69349a9`。classifier 理由の外部専用固定値、local_model の追加ネットワーク制限撤去、
実行 flag 不要の dry-run と同時指定拒否だけを修正した。期待値・閾値・プロンプト・gold・ケース・判定は不変。

- UT `test_external_identity_retains_local_adapters_and_has_no_secrets`：外部 identity の固定理由。
- UT `test_classifier_reason_is_absent_from_existing_identity[fixture/local_model]`：既存 identity への非追加。
- UT `test_fixture_network_guard_denies_ip_without_opening_network`：fixture の拒否を維持。
  local_model の追加制限に対応する試験だけ撤去。
- JS `external dry-run runs without execution flag and never forwards credentials or execution permission`：
  合成 bridge による CLI の DRY RUN と転送なし。実モデル/embedding を呼ばない。
- JS `external dry-run with execution flag fails before profile or credentials are read`：同時指定 FAIL（exit 1）。
- JS `external_model requires its own execution flag and profile (exit 3)`：必要情報欠落の NOT RUN。

追加 commit 後に指定の全品質ゲートを実行し、件数・結果・commit SHA は実装担当の完了報告へ記録する。
実送信・実モデル・IT2/ST は NOT RUN。push・PR・merge・Issue 操作は行わない。
