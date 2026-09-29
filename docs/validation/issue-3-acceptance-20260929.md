# Epic #3 / Issue #541 dev受入記録（2026-09-29）

## 判定

**全体はINCONCLUSIVE（受入未完了）**。実MCP接続・LiveKit独立音声診断・自動回帰を実施した。共有推論サービスへ接続できないため、変更前後の会話性能と#540の過大推定0件は未確認。実マイク・聴感はユーザー確認待ち。mocked E2Eや独立音声診断を未実施項目の代用にしない。

## 版・条件・安全境界

- 変更前の対象：main `9e8697a638d4f941dac399e1bdf50690ede04ef7`。
- 変更後の対象：Epic先端 `5d10ab490be8c7a2ec5b13bcaf9fc9a3386be429`（#537〜#540統合済み）。着手時HEADとリモートEpic先端の一致、指定された5ファイルの存在、clean worktreeを確認した。
- 作業ブランチ：`codex/3-541-measurement-acceptance`。WSL2 Linux `6.6.87.2-microsoft-standard-WSL2`、Docker Engine 29.6.0。ユーザー指定のUbuntu dev worktreeで実行。
- dev用LiveKit：`infra/livekit/compose.yaml`のv1.9.7、独立project `digital-souls-dev-livekit`、`:7880`。鍵はshell環境変数から渡し、ファイル・証跡には保存しない。各実行のEXIT trapで`down`し、container削除を確認。
- Playwrightは`DS_ENVIRONMENT_ID=test`、suite専用`frontend/test-results/runtime-data/`を使用。MCPは一時test directoryを使用。dogfoodのdata・専用port・systemdは操作していない。共有サービスに対してはreadinessのGETだけを実施し、起動・停止・設定変更・モデル操作は行っていない。
- 新規venvへrequirementsとrequirements-dev、Frontendへlockfileに従い`npm ci`を実行した。既存の`backend/.env`から推論系設定だけをプロセス環境へ読み、data root・LiveKit鍵を引き継いでいない。

## 実施結果

| 検証 | 区分・結果 |
|---|---|
| Backend unit・module | 実施済み：最終再実行6,777件成功・1件skip。初回の時間比率テスト失敗は末尾に記録 |
| Frontend unit | 実施済み：1,021件成功 |
| Frontend module | 実施済み：188件成功 |
| mocked E2E | 実施済み：58件成功。実接続・実マイクの証跡には含めない |
| 比較集計・pilot回帰 | 実施済み：119件成功 |
| 実MCP | 実施済み：2件成功、認証proxyの3件は選択対象外。公開`@modelcontextprotocol/server-filesystem`・`server-everything`各2026.8.31。protocol 2025-11-25。Filesystemは14 toolをdiscoveryしreadと領域外拒否、Everythingは13 tool・7 resource・4 promptをdiscoveryしecho／resource read・切断後のtransport失敗を既存Execution Gate経由で確認 |
| dev LiveKit readiness | 実施済み：HTTP 200 |
| 実LiveKit独立音声診断 | 実施済み：`probe_native_audio_source.py`。直接入力で入力前packet 0、20ms後1、追加音声後51。decode 48,960 sample、render／完了51 packet、出力時計の全sample通過を確認。`production_playback_verified=false`、`source_pcm_offset_verified=false`を維持 |
| `test:integration:voice` | 起動試行2回。初回はTarget未指定のprofile失敗。既存推論設定を渡した再試行は`external service is not ready: ollama`でreadiness失敗。テスト本体はNOT_RUN。共有VOICEVOX・WhisperのGETも接続失敗 |
| 前後比較：本文開始・実再生開始・CPU／メモリ | NOT_RUN：共有推論依存へ接続できず、両版とも固定音声の会話試行数0。値は欠測。回帰有無はINCONCLUSIVE |
| #540の`playback_estimate_delta_*` | NOT_RUN：上記の会話経路が起動できず観測数0。過大推定件数・短い側の分布は欠測。0件合格とは判定しない |
| 実マイク・スピーカー聴感 | NOT_RUN（ユーザー確認待ち） |

同日のGETはOllama `/api/tags`、VOICEVOX `/version`、Whisper `/health/ready`がすべてHTTP応答なし（curl 000）。既存設定もlocalhostを指定していた。別distributionの起動・共有サービスの復旧は委譲範囲外であり行わなかった。

一次ログはdevローカルの`/tmp/issue541-{backend-tests,backend-retest,privacy-retest,fe-unit,fe-module,mocked,mcp,report-tests,native-source,voice-2}.log`。スイート証跡は`frontend/test-results/{mocked-e2e,integration-voice}/`。再実行でこれらの場所を上書きし得るため、共有用には数値と状態だけの[要約](../artifacts/issue-541-acceptance-20260929.json)を保存する。会話本文・native payloadは転記しない。

## 前後比較の再実行

共有サービスが通常の管理手順で利用可能になった後、固定条件で両版を順番に実行する。物理マイクは不要。現在の計測CLIは`LIVEKIT_KEYS`をshellから受け取れるため、LiveKit用の秘密ファイルを作る必要はない。

1. 比較前に旧版`9e8697a`と新版`5d10ab4`の独立worktreeを用意する。旧版の`run_pilot.py`は鍵ファイルを必須とするため、本PRの同ファイルのshell鍵対応だけを両測定worktreeへ適用してコミットする。製品コードの差分がそれぞれの対象版からないことを確認し、計測用commitと元の製品commitを併記する。pilotは未コミットworktreeを拒否する。
2. [dev手順](../development-environment.md#ubuntu-devの初期セットアップ)でshellに鍵を生成し、独立Compose projectを起動する。終了時の`down`をtrapへ登録する。共有サービスのGETが失敗する場合は計測を開始しない。
3. 両版で同じ既存推論設定ファイル、Card・prompt・モデル／量子化・options・入力／出力上限、同じhost・Chromium・Profile・音声fixtureを使う。`--disable-thinking`等の条件変更は加えない。同時推論を避ける。解決済みTarget設定と使用モデル版をローカルで照合し、秘密値を除いた条件・差分を記録する。
4. 各worktreeで以下を実行する。`ISSUE541_RUN_ID`は`issue541-before-01`／`issue541-after-01`等の新規名、`ISSUE541_INFERENCE_ENV`は既存推論設定のpath。通常応答の正式比較は準備5回＋100独立試行。失敗・欠測も保持し、準備時間を別集計する。

```bash
backend/.venv/bin/python scripts/voice_quality/run_pilot.py \
  --run-id "$ISSUE541_RUN_ID" --inference-env "$ISSUE541_INFERENCE_ENV" \
  --controlled --scheduled-fixture
```

5. 各runの`trial-manifest.json`、`runtime-data/voice-metrics/controlled-trace.jsonl`、`inference-runtime.jsonl`と解決済みProfileを保存する。既存`app.livekit_pilot_report`でfixture・source相関・出力時計・全予定試行を検査する（[既存計測手順](../../scripts/voice_quality/README.md)）。`run_pilot.py`、固定音声spec、`preparation-probe.ts`は比較対象2版で同一だったことを`git diff`で確認した。その後、本PRのworktreeで比較する。出力先は新規ファイルに限る。

```bash
backend/.venv/bin/python scripts/voice_quality/compare_core_acceptance.py \
  --before "$ISSUE541_BEFORE_RUN_DIR" --after "$ISSUE541_AFTER_RUN_DIR" \
  --output /tmp/issue541-comparison-new.json
```

比較CLIは本文開始を同じBE時計の`stt_completed → first_text_delta`、実再生開始を同じbrowser時計の固定PCM発話末尾下限→`first_playback`として集計する。両区間は起点が違い、相互に差し引かない。CPUは所有Backendの累積CPU差／observer時間差、メモリは離散観測のcontainer最大使用量。負荷の集計範囲はwarm-upを含むrun全体の観測窓であり、応答単体の負荷とは扱わない。共有GPUは当該会話専用の負荷ではない。準備時間は既存`preparation-probe.ts`の観測を別集計する。

このCLIの`NUMERICAL_COMPARISON`は数値が比較可能という意味だけで、品質全体の合格ではない。未記録試行・失敗・欠測・coverageを併記し、[共通受入条件](../voice-quality-350-423-424-requirements.md)の100独立試行・p95 **2,000ms以下**を変更しない。少数pilot・未達・準備失敗を除外した数値だけで達成扱いにしない。条件の一致・既存reporterの検証・停止／再接続等の必要な回帰とユーザー確認を揃えて最終判定する。現時点の値・回帰判定は未取得。

## #540の再実行とd

上のshell環境で`npm --prefix frontend run test:integration:voice`を実行し、通常応答と割込の実会話経路を確認する。計測イベント保存には`VOICE_MEASUREMENT_KIND=controlled_baseline`と`VOICE_CONTROLLED_TRACE_PATH`をtest専用data root内の絶対pathへ指定する（既存quality runnerも同じtraceを保存する）。起動・終了はsuiteの所有run reportと独立LiveKit projectに限定する。

```bash
backend/.venv/bin/python scripts/voice_quality/compare_core_acceptance.py \
  --delta-trace "$ISSUE541_TRACE" --output /tmp/issue541-delta-new.json
```

`value = FE観測区間番号 − BE推定区間番号`。負値が過大推定であり、**負値0件**を必要条件とする。`completed / stopped / output_stop`別に観測分母・分布を残し、種別欠測と全体0件を成功にしない。正値は短い側の区間数差として分布のみを記録し、msへ換算しない。イベント重複・非数・不正値は集計を拒否する。

今回dを調整する実測根拠は得られていないため、既定300msを維持する。[#540記録への追記](voice-downlink-delay-540-20260928.md)を参照。

## ユーザー確認手順・結果欄

[dev起動手順](../development-environment.md)に従い`DS_ENVIRONMENT_ID=dev`と専用data rootで起動する。共有依存が利用可能であることを確認し、合成内容で実マイクとスピーカーを使う。会話原文を本記録に貼らず、結果・現象・時刻だけを残す。

| 操作と確認対象 | 結果 |
|---|---|
| 初回の音声開始：準備中表示→待受→実マイク入力、応答冒頭の欠け・不自然な無音がないか | NOT_RUN（ユーザー確認待ち） |
| 同一sessionで3往復：後続応答の冒頭・終端・音質、重複再生や詰まりがないか | NOT_RUN（ユーザー確認待ち） |
| 長めの応答中に割込：停止の聴感、旧音声の残留、次応答の復帰、履歴が聞いた範囲より先に進んでいないか | NOT_RUN（ユーザー確認待ち） |
| 準備中取消・終了／再開：マイク・音声の残留がないか。問題は#507既知制約との関係を個別調査する | NOT_RUN（ユーザー確認待ち） |

確認者・実施日時・端末／browser・入出力device・実行版：未記入。終了時は所有devアプリと独立dev LiveKitだけを停止する。

## #422引渡し・現行文書照合

[引渡し資料](issue-422-core-handoff-20260929.md)にRunner port、Target・Execution Gate・履歴規則、#538／#539 fixture、既知制約をまとめた。`docs/system-architecture.md`のBE再生推定とRunnerの責務は対象コードと一致。`CONTEXT.md`のdの説明は「計測から仮置き」と誤読できるため、実測未確定の仮置きであることを明記した。ADRへ進捗や作業branchを追記していない。

## 自動検査の実行入口と初回失敗

```bash
DS_ENVIRONMENT_ID=test backend/.venv/bin/python -m pytest backend/tests/unit backend/tests/module -q
npm --prefix frontend run test:unit
npm --prefix frontend run test:module
npm --prefix frontend run test:e2e:mocked
npm --prefix frontend run check
RUN_MCP_REAL_SERVICE_TESTS=true MCP_REAL_SERVER_ROOT=/tmp/issue541-mcp \
  DS_ENVIRONMENT_ID=test backend/.venv/bin/python -m pytest \
  backend/tests/integration/test_external_mcp_real_servers_integration.py -k published -q -s --tb=no
```

MCP依存は`npm install --prefix /tmp/issue541-mcp @modelcontextprotocol/server-filesystem@2026.8.31 @modelcontextprotocol/server-everything@2026.8.31`で準備した。認証proxy試験は本Issueの「1件以上の実接続」対象には選択していない。

Backend初回は6,762件成功・1件skip・1件失敗。`test_should_scan_long_email_non_match_with_linear_growth`で、10,000文字の中央値16.29msに対して20,000文字60.70msとなり、3倍未満の既存条件を超えた。privacy scanner全体の単独再実行は101件成功。初回は他の検証・image準備も併行していたが、負荷が原因と断定はしない。製品コード・判定閾値を変更せず、他の重い検証を終了して全体再実行した。最終結果は上表に記録する。

Frontend型検査はerrors／warningsともに0。変更したPython計測ツール・テストのruff、`git diff --check`、追加・更新した検証文書の相対リンク検査も成功。実サービス不足の未実施理由はこれらの成功と区別する。
