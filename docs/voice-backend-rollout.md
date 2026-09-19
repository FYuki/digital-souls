# #358 音声入力移設の更新・切り戻し手順

本書はM6の実行手順。配備完了・実マイク受入済みの記録ではない。
[移設契約](voice-backend-migration-contract.md)と[検証記録](validation/voice-backend-vad-358.md)を併読する。

導入はmain反映済みだが、本書の通し実行は[#424](https://github.com/FYuki/digital-souls/issues/424)の残件。
個別証跡と未実施範囲は[残受入対応表](validation/voice-quality-350-423-424-status.md)に整理する。

## 版とデータの境界

FEとBEを同じcommitの一組で切り替える。旧Core 1.1／private v1と新Core 2.0／private v2を混在させない。
旧Sessionの無停止引継ぎは行わず、会話終了、所有アプリ停止、FE／BE更新、ブラウザ再読み込み、新Session開始の順とする。
手動muteは利用者の状態として扱い、切替を理由に意図せずマイクを再開しない。

検証用data rootを一つ新設し、旧版・新版・切り戻しで同じ履歴を使う。
元checkoutやdogfoodの実データを検証用にコピー・削除しない。
履歴DBだけを切替時点のバックアップへ巻き戻すと新版で保存した履歴を失うため、
保存形式が互換な今回の切り戻しでは、同じdata rootをそのまま使用する。
実運用のバックアップ・更新は別の配備依頼と既存の運用入口に従う。

## 検証前の固定事項

- 旧アプリ基準は `07b8b1ee638ca222afe61983ebaf17c6edaaf1f7`。
  比較用ハーネスbranchのcommitと、アプリの基準commitは区別して記録する。
- 新版は検証対象の完全なcommitを固定し、FE／BEのimage名・image IDをrun reportと照合する。
- `DS_PROFILE=integration-voice`、`DS_ENVIRONMENT_ID=test` と新規の専用data rootを使用する。
  各版の起動は専用worktreeで行う。推論設定・モデルdigest・fixture・GPU／network条件をそろえる。
- 既存の共有Ollama／Whisper／VOICEVOX／LiveKitはexternal依存であり、
  アプリの切替から起動・停止・設定変更しない。所有が確認できる検証用LiveKitの操作は別管理する。
- 共有推論の503／504や測定中のGPU条件変化があれば、性能比較を成立したものとして扱わない。
  中止runの記録済み失敗と未記録試行を残し、成功試行で差し替えない。

## 専用Profileでの一括切替

以下は既存Environment CLIの操作順。まだ通しで実施していない。
`DS_DATA_DIR` は新規の検証用絶対pathとして事前に設定し、
各worktree内でその値を維持する。接続設定の秘密値を記録へ転記しない。

1. 旧版worktreeで旧版FE／BEを起動し、固定音声による会話と検証用履歴を作る。
   起動前に同じapp用ポートが別runに占有されていないことを確認する。
2. UIから音声Sessionを終了し、終了APIとnative session記録を照合する。
   再生停止とマイク停止を確認する。単なるpage closeを明示終了の証明にはしない。
3. 起動に使ったrun reportを維持して `environments/status.sh`、
   `environments/down.sh` を実行する。停止対象はそのrunが所有するFE／BEだけとする。
   所有containerの終了とappポート閉鎖を確認する。
4. 新版worktreeへ移り、同じdata root、別のrun／Profile report pathを指定する。
   FE／BEを同じ新版commitで起動する。前runの解決済みProfileを暗黙に使い回さない。
5. ブラウザを再読み込みし、新版の新Sessionを開始する。
   入力ACK後の発話3往復、相槌継続、生成終了後の割り込み、同一Sessionのtext優先、
   mute／focus／再接続の入力境界、実再生済みprefixの履歴を確認する。
6. 旧clientのbootstrapは409で拒否され、Room・Session・runtime・tokenを作らないことを確認する。
   エラー画面からFE VADへ暗黙に戻らない。
7. 手順2〜4を繰り返して旧版FE／BEへ戻す。data rootは変更しない。
   ブラウザ再読み込み、新しい旧版Session、既存履歴の閲覧と追記を確認する。
   新版で確定保存した履歴・中断prefix・privacy除外状態が残ることを検証する。

各phaseの環境変数は次の形で設定する。
`phase` は `before`、`after`、`rollback` のいずれかで、同じreportを上書きしない。

```bash
export DS_PROFILE=integration-voice
export DS_ENVIRONMENT_ID=test
: "${DS_DATA_DIR:?新規の検証用data rootの絶対pathを設定する}"
phase=after
export DS_ENVIRONMENT_RUN_REPORT="$DS_DATA_DIR/runtime/358-$phase/environment-run.json"
export DS_PROFILE_REPORT="$DS_DATA_DIR/runtime/358-$phase/resolved-profile.json"
revision=$(git rev-parse HEAD)
export DS_BACKEND_IMAGE="digital-souls-voice-quality/backend:$revision"
export DS_FRONTEND_IMAGE="digital-souls-voice-quality/frontend:$revision"
scripts/start-all.sh
environments/status.sh
# UIの会話終了を確認した後に、そのphaseの所有アプリを停止する。
environments/down.sh
```

readiness成功は起動確認に限る。固定音声の実サービス試験と人の実マイク・聴感確認を分け、
後者が未実施なら受入完了にしない。
本番のmainへのマージはユーザーが行う。Epicへの統合は対象CI成功後、main向けにはCodeRabbit差分レビューと修正を完了する。

## 保存履歴の互換性検証

次のCLIは既存data rootを受け取らず、自分で作成した一時SQLiteだけを使う。
各版のRepositoryを別プロセスで読み込み、旧版→新版→旧版→新版で読み書きする。

```bash
backend/.venv/bin/python scripts/voice_quality/check_history_compatibility.py   --before-repo /home/asa/dev/digital-souls-worktrees/issue-358-baseline   --output /tmp/ds358-history-new-report.json
```

完了、中断prefix、privacyによる本文除外、手動タイトル、archive、character境界を確認し、
各版の初期化前後の全テーブル行hashを照合する。新版・切り戻し版で追記した履歴も次の版から読む。
[保存済み結果](artifacts/voice-backend-358-history-roundtrip.json)はschema 9のまま全4段階に成功した。
これはRepositoryとSQLiteの互換性確認であり、上の実FE／BE切替の代用ではない。

## 現在の確認範囲

| 項目 | 状態 |
|---|---|
| 履歴保存コード・schemaの移設前との差分 | 変更なしを確認 |
| 新規SQLiteによる旧版／新版の読み書き往復 | 4段階成功 |
| Core 1.1・旧privateの資源作成前拒否 | TestClient＋実SFUで4パターンの作成呼び出し0回と正常版実接続を確認。さらに新FEのversion提示だけを旧契約へ変更し、実HTTPの409・UIのエラー／入力停止・拒否後1秒のマイク取得／VAD読込／新WebSocket 0件を4件確認。旧FE bundle自体の操作は未確認 |
| 接続開始失敗時のSession／Room解放 | 新版FE／BEと実SFUで1件成功。終了API 1回・Room削除・再接続409・マイク取得0回を確認。[証跡](artifacts/voice-backend-358-startup-cleanup-browser.json) |
| FE／BE一括更新・切り戻しの通し実行 | 未実施 |
| #358の前後5条件各100試行 | [検証記録](validation/voice-backend-vad-358.md)後半に計1,000件の予定試行を収集済み。失敗・欠測・品質未達があり受入未合格。中止runも保持 |
| #350・#423・#424の空状態100試行 | 今回の改善版・専用条件での正式測定は未実施。旧1,000件を代用しない |
| 人の実マイク・聴感 | 未実施 |

### ブラウザの旧version拒否（GPU計測を伴わない検証）

[保存済み結果](artifacts/voice-backend-358-bootstrap-browser.json)では、実FE／BEを既存Environment CLIで起動し、bootstrapのversion提示だけを変更した4件が成功した。HTTP応答は実BEのままとし、UIの汎用エラー・入力停止と、拒否後1秒のマイク取得／FE VAD asset要求／新WebSocketが0件であることを確認した。旧FE bundleそのものの実行、FE／BEの一括切替、実音声会話はこの試験に含めない。

新設data rootの会話・turn・memory job／receiptはcleanup後すべて0件。Environmentの所有FE／BEと専用LiveKitを停止し、専用ポート閉鎖を確認した。共有推論サービスはexternalのまま操作していない。初回の起動設定誤り、応答構造・UI文言のテスト期待値誤りも別runとして保持した。起動前image IDはCLI内の再build後の実行image証拠には使わない。
