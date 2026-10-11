# llama.cpp token計数の合成検証（2026-10-11）

対象はADR 0026 §5のprofileごとの計数選択と、Chat API / 会話履歴 APIの予算削減。
実LLM・GPU・稼働中のllama.cppは使わず、指定worktreeだけを変更した。
branch: `feature/152-llamacpp-token-count`、起点: `f0d9fb7391a7431c454ce99e08ef2f2efb8fd0e2`。

## 固定APIの根拠

確認revision: `5fc4f3c8c7103ffd0b7ff5ee4855bcc78a3ed5cd`。

- [tools/server/README.md](https://github.com/ggml-org/llama.cpp/blob/5fc4f3c8c7103ffd0b7ff5ee4855bcc78a3ed5cd/tools/server/README.md) の POST `/apply-template` / `/tokenize`：messagesからpromptを返し、contentからtokens配列を返す。
- [tools/server/server.cpp](https://github.com/ggml-org/llama.cpp/blob/5fc4f3c8c7103ffd0b7ff5ee4855bcc78a3ed5cd/tools/server/server.cpp) L281–283：両POST routeの登録。
- [tools/server/server-context.cpp](https://github.com/ggml-org/llama.cpp/blob/5fc4f3c8c7103ffd0b7ff5ee4855bcc78a3ed5cd/tools/server/server-context.cpp) L5060–5127：`post_apply_template`がChat Completionsと同じ`oaicompat_chat_params_parse`を呼び、`post_tokenize`がtoken ID配列を返す。
- [tools/server/server-common.cpp](https://github.com/ggml-org/llama.cpp/blob/5fc4f3c8c7103ffd0b7ff5ee4855bcc78a3ed5cd/tools/server/server-common.cpp) L1224–1243 / L1314–1322：tools・tool_choiceを読み、server templateへ渡す。toolsはJinjaが必要。
- server-context.cpp L5533–5548：入力token数の別経路も同じparserを使い、`tokenize_mixed(..., true, true)`。これに合わせてadd_special=true、parse_special=true、with_pieces=falseを明示。

サーバーのtemplate設定を使い、Coreからtemplateを上書きしない。toolsとtool_choiceをtemplateへ渡せるため、tool定義の推定加算はしない。
接続先は既存のmanaged_loopback_endpointで検証したapi_baseから末尾/v1だけを除いたroot。

## 設計とTDD

- profile.token_counterはestimate（既定）/llamacpp。後者はllamacpp_chatだけで許可。
- TokenCounterはintまたはAwaitable[int]を返せるportとし、PromptBuilder.buildをasync化。同期のByteTokenCounterと既存の削減アルゴリズムを維持し、HTTP待ちがイベントループを塞がない。
- 同一build内のmessages/toolsのcanonical JSONのSHA-256をkeyに成功した計数だけを保存（tool_choiceはcounter作成時に固定）。全体・各領域・削減後に共通のkeyを使い、別buildへ本文や結果を持ち越さない。
- template適用とtokenizeの一組ごとにHTTP clientを所有し、既存SDKのcancel対応cleanupを共用。trust_env=false、follow_redirects=false、retryなし。
- tokenizer送信前にも組み立て済み本文のprivacyを検査し、template応答後の権限失効をtokenize前に拒否。
- 接続失敗・timeout・不正JSON/shape/負token ID/boolean・redirectをfail-closedとし、本文なしのCoreErrorへ変換。timeoutは504、その他は502、codeはtoken_count_failed。cancelは伝播。
- 先行テスト：新adapterが存在しない段階のtest_token_count.pyはModuleNotFoundErrorでFAIL。
- privacyの先行回帰：`pytest tests/test_token_count.py -k 'private_card or revoked' -q`は4 FAIL。本文の事前検査と失効guardを追加してPASS。
- 途中のasync UT移行ではawaitの属性参照2件、fixtureの履歴slice1件がFAIL。修正後の対象試験はPASS。途中lintの長行もformat後に修正。
- UT：設定、toolを含むcache key、8段階のasync/sync counter一致、既存PromptBuilder UTの全受入条件。
- PostgreSQLの初回ゲートは431 PASS / 1 FAIL。追加したtoken_counterの既定値が旧SDK receiptのfingerprintを変える回帰を検出。既存のestimateでは新fieldをhashへ含めず、llamacppへの変更は含める。先行IT1の旧固定hash照合も1 FAIL / 2 PASSを確認してから修正。
- IT1：ローカルASGI fixtureでtemplate/tokenizeの往復と両APIの履歴削減。MockTransportで両endpointの障害、redirect拒否、proxy環境変数の無視、cancelとclient終了を決定的に検証。外部socketなし。

## ゲート

固定uv 0.8.22 / Node 24.19.0、Python 3.12。全コマンドは指定worktreeで実行。

```sh
export T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
```

最終結果（対象lint/formatはsrc、tests、tools/evaluate-semantic-retrieval.py、evals/semantic/provider.py）。

| コマンド | 結果 |
| --- | --- |
| `uv sync --locked` | PASS |
| `uv lock --check` | PASS |
| `uv run --no-sync ruff check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py` | PASS |
| `uv run --no-sync ruff format --check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py` | PASS、123 files |
| `uv run --no-sync mypy` | PASS、123 files |
| `uv run --no-sync pytest -m ut -q` | PASS、826 passed / 1203 deselected |
| `uv run --no-sync pytest -m it1 -q` | PASS、771 passed / 1258 deselected |
| `uv build --no-build-isolation` | PASS、sdist/wheel |
| `bash tools/test-postgres.sh` | 初回FAIL（1 failed / 431 passed）、修正後PASS（432 passed / 1597 deselected） |
| `(cd evals/semantic && npm ci --no-audit --no-fund)` | PASS、575 packages |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS、59 required tests |
| `node tools/check-docs.mjs` | PASS |
| `git diff --check` | PASS |

最終必須ゲートのFAIL・SKIPはなし。markerによるdeselectedは別suiteへの分類で、SKIPではない。
Pythonの既存Starlette/Pydantic警告、npmの固定依存のdeprecated警告は出た。lockfileは変更していない。
PostgreSQLはネットワークなし・公開portなし・合成データだけの新規コンテナを作り、各試行後に削除。
他のコンテナは操作していない。

変更ファイル：

- src/digital_souls_core/application.py、character.py、conversations.py、errors.py、local_sdk.py、prompt_builder.py、token_count.py。
- tests/fixture_server.py、test_history_llamacpp.py、test_prompt_builder.py、test_token_count.py。
- docs/api.md、docs/llamacpp-operations.md、本証跡。
- examples/characters.llamacpp.json。

`CoreError`はerrors.pyへ移し、application.pyからの既存importを明示re-exportで維持。
SPECは変更していない。

## NOT RUN

- IT2：実サーバーのGemma chat templateによる全region・tools・末尾assistant等の受入と実token数の照合。
- ST / 実モデル品質評価、実GPU計数・速度測定。
- 既存コンテナの変更、push / PR / merge。

fixtureのtoken IDと数は合成であり、実モデル品質やtemplate受入の証拠にはしない。
