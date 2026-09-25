# CI安定性・テストリファクタリング 改修棚卸し（2026-09-25）

本書は、CI（`.github/workflows/ci.yml`）の不安定要因と、テストのリファクタリング・安定化の改修候補を棚卸しした調査記録である。
現在の機能説明や設計判断の正本ではない。個々の改修範囲・完了条件はGitHub Issueへ切り出して管理する
（[リポジトリ運用方針](../repository-policy.md)「文書ごとの責務」）。

## 1. 調査範囲と方法

| 項目 | 内容 |
|---|---|
| 対象commit | `main` `3f82389`（2026-09-25） |
| CI履歴 | `ci.yml`の完了済みrun直近300件（2026-09-13〜2026-09-25）をGitHub Actions APIで取得 |
| 失敗ログ | 失敗19件のログ末尾と、再実行で成功した9件のうち5件のattempt 1をjob・stepで確認 |
| ローカル実測 | Claude Code on the webのクラウドコンテナ（Ubuntu 24.04、4 CPU、WSLではない）。Python 3.12 venv、`npm ci --prefix frontend`後、`pytest backend/tests/unit`と`backend/tests/module`を`--durations`付きで実行。開発標準のWSL2 Ubuntu（`AGENTS.md`「環境」）やCIの`ubuntu-latest`とは絶対値が異なるため、比率で読む |
| 重複分析 | テスト関数のAST正規化比較、3-gram類似度、per-test coverage context（`coverage run`の`dynamic_context = test_function`）と実行時間の突合（7章） |
| 未実施 | Frontend unit/module、mocked E2E、Docker image buildのローカル実行。反復実行による再現率測定 |

## 2. 観測結果の要約

### 2.1 CI run結果（直近300件）

| 結論 | 件数 | 備考 |
|---|---:|---|
| success | 219 | うち9件はattempt 2（再実行）で成功 |
| cancelled | 62 | 大半は`concurrency.cancel-in-progress`による後続pushでの取消（想定動作） |
| failure | 19 | 内訳は2.2 |

### 2.2 失敗・再実行の分類

| 分類 | 件数 | 代表run | 内容 | 性質 |
|---|---:|---|---|---|
| A. image build：外部archive取得失敗 | 失敗5 | 36077570020（main、9/25）、34863702086、34860423389、34867920356、34871637876 | `backend/Dockerfile:4`の`prepare.py`が`codeload.github.com`／`chromium.googlesource.com`からarchiveを取得して失敗。503を確認したのは前3件で、後2件は同じstepの`exit code: 1`までを確認 | **外部依存のフレーキー** |
| B. image build：LiveKit FFI検証のRust panic | 失敗3 | 35049250449、34954343226、34887311350 | `FfiClient`初期化・disposeで`panic in a function that cannot unwind`（exit 134） | フレーキー。9/16に`verify.py`へ分離後、観測範囲内で再発なし |
| C. image build失敗→再実行成功（原因ログ未確認） | 再実行成功3 | 34917550861、34868789130、34862702911 | 同じ`Build application images`stepのみ失敗、他jobは成功 | A/Bと同系統と推定（未確認） |
| D. GitHub Actions側障害 | 失敗5＋再実行成功1 | 35429707558ほか（9/19 05:40〜08:00）、再実行成功35430879258 | 全4jobが開始2秒で失敗、ログ404。同じ時間帯の再実行成功4件（35429409944、35427074494、35425919705、35425851639）も同じ原因と推定（未確認） | リポジトリ外要因。対処対象外 |
| E. backend時間切れ | 再実行成功1 | 35042773999（attempt 1） | module testsが旧`timeout-minutes: 15`で取消 | 実行時間の増加。現在25分へ延長済み |
| F. テストのタイミング依存 | 失敗1 | 34865733809 | `test_episodic_formation.py::test_scheduler_failure_diagnostic_excludes_exception_content` が`TimeoutError` | **フレーキー候補** |
| G. テストの環境依存 | 失敗1 | 35440598325 | `test_environment_image_preparation.py`がrunner上のwhisper endpoint状態に依存 | `3409662`で隔離済み |
| H. 変更起因の正当な失敗 | 失敗4 | 34910001913、34909570091、34860744499、34844407361 | PRブランチ上のassertion失敗（後続commitで修正） | 不安定性ではない |

**結論**：テスト本体のフレーキーは少数（F）で、CI不安定の主因は`container-contracts`jobの**Docker image build（外部archive取得とネイティブSDKビルド）**である。
次点は**backend jobの実行時間の長さ**で、時間切れと再実行コストの両方を大きくしている。

### 2.3 実行時間

| 対象 | 実測 |
|---|---|
| CI全体（成功したpush run） | 11.6〜15.3分。大半が13〜15分 |
| CI `container-contracts` | image buildだけで約4.5分。Rust SDKを毎回ソースビルドする（`backend/Dockerfile:6-14`） |
| CI backend module tests | 約8分（9/14）→約9.5分（9/19）→`ci.yml:42`のコメントでは15分時点で93% |
| ローカル unit | 4,653件、92秒 |
| ローカル module | 2,030件、485秒。**上位60件だけで269秒（56%）** |

module testsの遅いファイル（上位60件内の合計）:

| ファイル | 秒 | 主因 |
|---|---:|---|
| `test_character_life.py` | 90.7 | 実DBOSのcron・polling。1件あたり約4秒のテストが多数 |
| `test_addon_action_process_recovery.py` | 81.2 | `test_real_sixty_second_wait_...`（L226）が実時間60秒待ち（`60 <= elapsed < 70`を検証） |
| `test_character_life_recovery.py` | 25.4 | 実DBOS再起動・recovery |
| `test_voice_input_vad_parity.py` | 22.7 | VADの実音声処理 |
| `test_episodic_runtime.py` | 16.4 | lifespan全体の起動 |
| `test_environment_signal_entrypoints.py` | 15.3 | 実process＋signal |

## 3. 不安定・保守性の要因（コード上の根拠）

| ID | 要因 | 根拠 |
|---|---|---|
| R1 | 外部archive取得にretryがない | `scripts/voice_quality/native_sdk_experiment/prepare.py:63`で`urlopen(url, timeout=60)`を1回だけ実行 |
| R2 | image buildにlayer cacheがない | `ci.yml:36-37`が`docker compose build`を素で実行。Rust SDKのフルビルドと外部取得を毎run実施 |
| R3 | ネイティブSDK検証がbuild中に走る | `backend/Dockerfile:29`の`verify.py`。9/16の分離後に再発はないが、監視が必要 |
| R4 | backendが単一processで直列実行 | `backend/requirements-dev.txt`に`pytest-xdist`／`pytest-timeout`がない。CIはunit→moduleを直列実行 |
| R5 | 実時間の待機・短い固定timeoutに依存 | `timeout=0.x`等が54箇所。`time.sleep`／短い`asyncio.sleep`を含むファイルは上位が`unit/test_livekit_runtime_audio.py`(16)、`module/test_character_life.py`(15)、`module/test_environment_process.py`(9)、`unit/test_irodori_service.py`(8)、`unit/test_whisper_client.py`(7) |
| R6 | 1件のhangがjob全体のtimeoutになる | per-test timeoutがないため、hang時は25分待ってから取消され、失敗テスト名も残らない |
| R7 | subprocessが`PATH`上の`python3`を使う | `test_backend_startup_contract.py`、`test_dogfood_*.py`等。venvの外で実行すると`ModuleNotFoundError: jsonschema`となり、暗黙に「system pythonへ依存を導入済み」を前提にしている |
| R8 | 暗黙のOS・Git依存 | dogfood系テスト239件が`fakeroot`コマンドを前提にしている（ローカルでは`FileNotFoundError`）。音声品質テストは全Git履歴を要求（`ci.yml:48`）。IPv6不可の環境で2件が`OSError: [Errno 97]` |
| R9 | E2Eの固定待機 | `frontend/e2e/voice-chat.spec.ts:442,476`の`page.waitForTimeout(1_000)`（「追加要求が来ない」ことの確認） |
| R10 | Frontendテストの実タイマー | fake timerを使わず`setTimeout`を使う6ファイル（`AudioRecorder.unit.test.ts`、`notifications.module.test.ts`、`voice-history/controller.unit.test.ts`等） |
| R11 | 失敗証跡がCIに残らない | `ci.yml`にPlaywright結果・JUnit XMLのartifact uploadがない。フレーキー率を後から集計できない |
| R12 | テスト規模 | Backend unit 238ファイル／約6.8万行、module 121ファイル／約4.5万行。`*_test_support.py`が22本あり、fixture共有の境界が分散 |

## 4. 改修候補（優先度順）

制約：[テスト方針](../testing-policy.md)に従い、テストのskip・無効化・隔離、実接続試験のmock代替、履歴・依存不足をskipで回避すること（同方針「実行入口」末尾）は行わない。

### P0：CI不安定の主因を除去する（image build）

| # | 改修 | 対象 | 期待効果 |
|---|---|---|---|
| 1 | `prepare.py`の取得に有限回retry（指数backoff、5xx・接続断のみ）を追加。検証済みdigest照合は維持 | `prepare.py:63` | 分類Aの解消 |
| 2 | image buildへBuildKit cacheを導入（`docker/setup-buildx-action`＋`cache-from/to: type=gha`、またはsdk-build stageの成果物をGHCRへ固定digestで保存） | `ci.yml:36`、`backend/Dockerfile` | 外部取得・Rustビルドの回数削減。約4.5分の短縮 |
| 3 | 外部archiveのミラー化を検討（GitHub Release asset等の自前保存＋digest固定） | `prepare.py` SOURCES | 外部可用性への依存を除去（2で不十分な場合） |
| 4 | FFI検証panicの再発監視。再発時はbuild時検証を独立jobのsmoke testへ移す | `backend/Dockerfile:29` | 分類B |

### P1：実行時間とhangの影響を抑える（backend）

| # | 改修 | 対象 | 期待効果 |
|---|---|---|---|
| 5 | `pytest-timeout`導入（既定値は例：120秒。長時間テストは個別mark） | `requirements-dev.txt`、`pytest.ini` | hang時に即座にテスト名付きで失敗させる（R6） |
| 6 | `pytest-xdist`による並列化の可否を調査。port・一時dir・環境変数・`os.fork`（`test_restore_crash_recovery.py`）の衝突を洗い出してから導入する | `backend/tests` | module 8分の短縮 |
| 7 | CI backend jobをunit／moduleの2job、またはmoduleをshard分割 | `ci.yml` | wall-clockと再実行単位の縮小 |
| 8 | 60秒実待機テストをclock注入へ置換。実時間の検証が必須なら専用markで分離し、実行条件を文書化する | `test_addon_action_process_recovery.py:226` | 約60秒の短縮 |
| 9 | 実DBOSのpolling間隔・停止待ちをテスト用設定で短縮（production設定は変えない。7.3参照） | `test_character_life*.py`、`backend/app/character_life/runtime.py:129,303` | 実測120.8秒→77.8秒（60件全件合格のまま） |
| 9a | VAD parityのNode起動を全fixtureで1回にまとめる（7.3参照） | `test_voice_input_vad_parity.py:69` | 実測72秒の大半（Node起動1回1.6秒×43件） |

### P2：テストの決定性を上げる（リファクタリング）

| # | 改修 | 対象 | 期待効果 |
|---|---|---|---|
| 10 | 短い固定timeout・sleepを、条件待ち（Event／Condition／poll with deadline）とfake clockへ置換。R5の上位ファイルから着手 | R5の一覧、`test_episodic_formation.py:305` | 分類Fの予防 |
| 11 | subprocessの起動を`sys.executable`へ統一し、system pythonへの暗黙依存を除去 | R7の対象ファイル | venv外・別環境での再現性 |
| 12 | `fakeroot`・IPv6・完全なGit履歴などの前提を、共通fixtureで冒頭に明示検査する（不足時はskipではなく、原因を示すsetup errorにする） | R8 | 失敗原因の即時特定 |
| 13 | E2Eの`waitForTimeout`を、route呼出記録の短時間`expect.poll`による否定確認へ置換 | `voice-chat.spec.ts:442,476` | 固定1秒待ちの除去 |
| 14 | Frontendの実タイマー使用テストを`vi.useFakeTimers`へ移行 | R10の6ファイル | 決定性の向上 |
| 15 | `*_test_support.py`（22本）とharnessの重複を整理し、fixtureの所有範囲をテスト層ごとに揃える | `backend/tests/*_test_support.py` | 保守性の向上 |

### P3：計測・可視化

| # | 改修 | 対象 | 期待効果 |
|---|---|---|---|
| 16 | pytestのJUnit XML・`--durations`とPlaywright結果を`actions/upload-artifact`で保存（失敗時は必須） | `ci.yml` | フレーキー率・遅延の継続計測 |
| 17 | 再実行で成功したrunの記録運用（Issueまたは定期集計） | 運用 | 再発の早期検知 |

## 5. 推奨する着手順

1. P0-1、P0-2（image build）：直近mainの失敗原因であり、変更範囲も小さい。
2. P1-5（per-test timeout）→P3-16（証跡保存）：以降の改修効果を測る土台にする。
3. P1-8、P1-9、P1-6/7：実行時間短縮。xdistは衝突調査の結果で可否を判断する。
4. P2：分類Fの予防と保守性。ファイル単位の小PRに分ける。

## 6. 未確認事項

- 分類C（3件）と、分類Dと推定した再実行成功4件の個別ログ。ログ末尾を未取得のため、A/Bと同系統というのは推定。
- Frontend unit/moduleとmocked E2Eのローカル反復実行による再現率。直近300件のCIでは、これらのjob失敗は分類D（全job同時失敗）以外に観測されていない。
- 300件より前の履歴。

## 7. 重複テストと長時間実行の分析（追補）

### 7.1 方法

| 観点 | 方法 | 注意 |
|---|---|---|
| 字面の重複 | テスト関数本体をASTで正規化し、完全一致・リテラルのみ相違を検出。ファイル跨ぎは3-gram Jaccard 0.6以上 | 対象3,592関数 |
| 意味的な重複 | 各テスト関数が通過した`backend/app`・`environments`等の行集合を記録し、「固有行が0」かつ「より速い別テストが全行を包含」する遅いテスト（0.5秒以上）を抽出 | 行が同じでもassertionが異なれば重複ではない。候補は全件本文を読んで判定した |
| 対象外 | subprocessで別processを起動するテスト等、app行を記録しない1,007関数（計測下139秒） | 本手法では重複判定できない |

計測時、8箇所のテストがプロセス全体の`sqlite3.connect`を差し替えており、coverage自身のSQLite書込みまで失敗した
（例：`test_backup_restore_sqlite_coordination.py:266`、`test_runtime_startup_order.py:77`、`test_restore_intent_startup.py:86`）。
テストは合格しているが、モジュール属性経由でグローバルを差し替えるため、同一processで動く他の処理へ影響し得る。差し替え対象を注入点へ限定することを改修候補に加える（7.4 #D5）。

### 7.2 結果：純粋な重複は少ない

| 分類 | 件数 | 判定 | 時間への影響 |
|---|---:|---|---|
| 本文完全一致 | 2組 | **重複**。`test_livekit_bootstrap_api.py:313`と`test_runtime_contract.py:707`（同じ`client`fixtureで`GET /`を検証）、`test_environment_adapters.py:657`と`:739`（本文同一、名前のみ相違） | ほぼ0 |
| リテラルのみ相違 | 31組68関数 | 重複ではない（入力値が異なる）。`pytest.mark.parametrize`への統合候補 | 0（件数不変） |
| ファイル跨ぎ高類似 | 3組 | `test_dogfood_deploy.py:1716`と`test_dogfood_rollback.py:410`など。対象コマンドが異なり重複ではない | 0 |
| coverage包含（遅いテスト） | 12件 | 本文確認の結果、**実質重複は1件**：`test_episodic_retrieval.py:162`（4ケース）。実Chroma索引・月精度・content versionの確認は`:307`と重なり、固有なのはNarrativeContextの表示ラベル対応のみ | 数秒 |
| 同上（非重複と判定） | 11件 | 例：`test_notifications_module.py:352`と`:83`は再起動後の反復ONとOFF中の非遡及で別の振る舞い。`test_irodori_service.py:157`と`:262`は無応答と部分応答で別の障害 | — |
| CIジョブ間の重複 | 1件 | voice-session生成物の照合を`ci.yml:86`とbackend unit `test_voice_session_contract.py:422`の両方で実行 | 約2.5秒 |

**結論**：テストコードの重複を解消しても、実行時間はほとんど短縮しない。
長時間実行の主因は重複ではなく、**テストごとに繰り返される重い準備・後始末と実時間待ち**である（7.3）。

### 7.3 長時間実行の実因（実測）

| 対象 | 実測（通常実行） | 原因 | 対策案 | 効果（実測／推定） |
|---|---:|---|---|---|
| `test_voice_input_vad_parity.py` | 72秒（43件） | 各ケースで`node frontend/scripts/voice-vad-parity.mjs`を起動。1回1.6秒のうち大半はesbuild変換とONNX／WASM読込で、Python側の処理は0.06秒 | 全fixtureを1回のNode起動で処理するsession fixtureへ変更。比較内容は変えない | 推定60秒以上 |
| `test_character_life*.py`（3ファイル60件） | 120.8秒 | 1テスト約4秒の大半が`DBOS.destroy()`。(1)`workflow_completion_timeout_sec=10`（`runtime.py:303`）では稼働workflow 0件でも最初に1秒sleep、(2)SQLite通知listenerの既定polling 1秒で終了待ちが発生 | テスト用にpolling間隔と停止待ちを注入可能にする。production既定値は維持 | **実測77.8秒（-43秒、60件全件合格）** |
| `test_addon_action_process_recovery.py:226` | 約62秒 | `autonomous_wait_seconds == 60`を実時間で待ち、`60 <= elapsed < 70`を検証 | clock注入で待機を短縮。実時間の検証が必要なら専用markで分離 | 推定約60秒 |
| `test_episodic_retrieval.py:162` | 数秒 | 4ケースとも実Chromaで索引・検索 | ラベル対応はunitで検証し、実Chroma経路は1ケースへ集約 | 推定数秒 |

module testsのローカル実測は485秒。上表の対策で約160秒（約35%）の短縮が見込める（DBOSのみ実測、他は推定）。

計測に使った測定専用プラグイン（DBOS設定の差し替え）はリポジトリへ入れていない。
DBOS 2.31.0では、`DBOSConfig`に渡した`runtimeConfig`の`notification_listener_polling_interval_sec`が反映されなかった。
生成後の内部設定へ値を入れた場合に限り効果を確認したため、実装時には公開APIでの設定方法を先に確認する。

### 7.4 改修候補（重複・長時間）

| # | 改修 | 優先 |
|---|---|---|
| D1 | VAD parityのNode起動を1回にまとめる | 高 |
| D2 | Character LifeのDBOS停止待ち・pollingをテスト用に注入可能にする | 高 |
| D3 | 60秒実待機テストをclock注入へ変更（4章 P1-8と同じ） | 高 |
| D4 | 完全一致2組を1件ずつに統合し、`test_episodic_retrieval.py:162`のラベル対応をunit化 | 低 |
| D5 | `sqlite3.connect`のグローバル差し替え8箇所を、注入された接続factoryの差し替えへ変更 | 中（隔離性） |
| D6 | リテラル違い31組を`parametrize`へ統合 | 低（保守性のみ） |
| D7 | voice-session生成物の照合をCIのどちらか一方へ寄せる | 低 |
