# Issue #3 共通Core境界の基点と検証（2026-09-26）

## 固定基点

- 実装worktreeの基点：`d61703fd51b8a82901a04880d29058a68a6a97cc`（`origin/epic/3-character-core-boundary`、PR #519の文書を含む）。比較した`origin/main`は`1e95fb2aa5b28af9dc75b338fb9e5ba252e84762`。
- dev Profileの定義：`environments/profiles/dev.json`。Backend／Frontendはmanaged、Ollama／VOICEVOX／LiveKit／Whisperはexternal、Chromaはin_process。
- 既存devの実利用記録：`/home/asa/.local/state/digital-souls/dev/runtime/dev/environment-run.json`と同階層の`resolved-profile.json`を読み取り確認。2026-09-25の実行は`dev`／`DS_ENVIRONMENT_ID=dev`、データrootは`/home/asa/.local/state/digital-souls/dev`、終了済み（status `completed`、phase `complete`）。この記録は本変更の実接続結果ではない。
- 既存checkoutの`backend/.env`から秘密値を除いたchat設定を確認：`INFERENCE_TARGET_CHAT=ollama/gemma4:e4b`、入力上限7168、出力上限1024。現worktreeの実行環境変数にはこれらの値を設定していない。`INFERENCE_TARGET_VISION`は未設定。推論Targetの暗黙fallbackは追加しない。

## 適合・回帰

本変更のコードを対象に、FEなしの`invoke_text`、未対応の主体・公開出力の早期拒否、音声応答識別子の伝播、既存HTTP／Conversation Coreの主要回帰を実行する。結果と実行方法を以下に記録する。unit・moduleはworktreeから実行し、固定版が一致する既存frontend依存を参照した。

| 検証 | 状態 |
|---|---|
| Python型検査・変更差分検査 | 実施済み。型検査は変更した7ファイルで成功。Python lintと`git diff --check`も成功 |
| HTTP／音声Core主要unit・module | 実施済み。Backend unit・module全体で6687件成功、1件スキップ。音声IDの追加試験後、関連unit 102件成功 |
| frontend unit・module | 実施済み。両suite成功 |
| mocked E2E | 実施済み。独立test-mocked Profileで58件成功。実サービス・実マイクの証跡には含めない |
| 実Ollamaの短い推論 | 実施済み。既存Inference Routerから`gemma4:e4b`へ1件送信し、非空の応答と正常終了を確認 |
| 実Webテキスト会話 | 実施済み。独立`integration-text`環境で1件成功し、実Backend・Ollamaの応答がブラウザへ表示された |
| 実画面共有・Vision | 実施済み。任意のVision Targetを`ollama/gemma4:e4b`へ明示した再実行で1件成功。初回は未設定のため認識完了に到達しなかった。設定変更はtest実行環境のみ |
| devの実Tool・音声サービス接続 | `NOT_RUN`。LiveKitは現在到達不能。終了済みdev実行のreportを本変更の証跡に流用しない |
| 実マイク・聴感 | `NOT_RUN`。利用者の検証結果を待つ |
| 変更前後の本文開始・実再生開始と処理負荷 | `NOT_RUN`。固定条件の実サービス測定が必要 |

実行：`PYTHONPATH=/tmp/issue3-test-deps:backend /home/asa/dev/digital-souls/backend/.venv/bin/python -m pytest -q backend/tests/unit backend/tests/module`（使用したvenvはmain checkoutの同一依存環境）、`npm --prefix frontend run test:unit`、`npm --prefix frontend run test:module`、`npm --prefix frontend run test:e2e:mocked`。実Webは`npm --prefix frontend run test:integration:text`、画面共有は同コマンドに`-- screen-perception.spec.ts`を付け、実行環境へ文書のVision Target設定を追加した。`wasmtime==36.0.0`のみworktree外の一時targetに補い、リポジトリや共有venvは変更していない。`/api/tags`等へのHTTP 200はモデルの実応答や実音声の証跡とは分ける。

既存#507のLiveKit取消・終了問題と、本変更で発生した回帰は分けて追跡する。mocked E2E、readiness、run reportは実音声の品質・取消受入の証跡としない。
