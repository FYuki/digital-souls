# #358 移設前比較用ハーネス

アプリの基準は `07b8b1ee638ca222afe61983ebaf17c6edaaf1f7`。
比較用の固定音声起点・停止観測・匿名集計だけを移設後とそろえ、
`VOICE_QUALITY_INPUT_AUTHORITY=frontend` を明示する。
旧FEのcontrollerテスト用hookと待機表示の観測を維持する。
BE移設・protocol 2.0のアプリ実装はこのworktreeへ入れない。

[対応記録](../artifacts/voice-backend-358-baseline-harness.json)に基準treeとハーネスの出典を残した。
backend、frontend/src、contracts、environments、characters、infraは基準commitと同一。
frontend/publicは基準commitのGit treeには存在せず、存在しないtreeのhashを補完しない。
package-lockが同一であることを確認し、node_modulesは既存のインストールを参照する。

## 局所検証

- 旧版の相槌・take-turn・VAD集計: 62件成功。
- Svelte: 0 errors / 0 warnings、E2E TypeScript成功。
- 旧VADテストの動的importはCLIと同じscripts/voice_quality検索パスを指定した。
- 実サービスでの移設前測定は未実施。これらの検査を性能比較の成功へ読み替えない。

実行時は本体の#358測定と同じ固定fixture・推論設定・モデルdigest・専用Profileを使用し、
新しいrun-idを付ける。共有推論が503/504の状態やGPU条件が不一致の間は比較を成立させない。
失敗・中止済みrunを削除せず、別runの成功試行で差し替えない。
