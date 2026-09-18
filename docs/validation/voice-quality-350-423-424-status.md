# #350・#423・#424の既存証跡と残受入

2026-09-19 JSTに、移管元Issue本文、保存済み検証記録、PRの統合状態を照合した対応表。
[共通要件](../voice-quality-350-423-424-requirements.md)を変更せず、実装済み機能の再実装と未受入の見落としを避ける。
「過去確認」は記載版・条件での証拠であり、現在の設定・改善後版の受入成功を意味しない。
試験ファイルへの参照は実行先の案内であり、その存在を実行済みの証明にしない。

## 統合状態と今回の実施順

- Irodori導入は[PR #375](https://github.com/FYuki/digital-souls/pull/375)、BE音声判断移設は
  [PR #408](https://github.com/FYuki/digital-souls/pull/408)でmainへ統合済み。
  元Issue #329/#330/#358/#393〜#398の未チェック欄は履歴として保持する。
- 最初に原因別の製品修正と計測欠測修正を行い、固定版の正式測定を別PRで実施する。
  過去の100試行と今回の小規模診断を合算しない。
- 空履歴・記憶参照なしの100独立試行で、利用者発話終了→実再生開始のp95 2,000ms以下を受入確認する。
  その達成後に、同じ入力・モデル・声で固定テスト記憶の参照だけを追加測定する。
  記憶参照の速度・結果には受入閾値を置かず、記憶形成・長い履歴の影響は調べない。
- 実機マイク・聴感、更新／切り戻し、機能競合、レビューは独立した残条件として扱う。
  空状態の速度評価を理由に省略しない。mainマージはユーザーが実施する。

## #329の条件と#423への対応

条件は[元Issue #329](https://github.com/FYuki/digital-souls/issues/329)の対応・受入欄の順に対応する。
[Irodori検証記録](../irodori-tts-validation-2026-09.md)の過去値はspeed 1.00、
現在の[採用CCV](../../characters/miori/miori.card.json)はspeed 1.02である。

| 元条件 | 確認済みの証跡・範囲 | 残条件・担当 |
|---|---|---|
| 1. dev/dogfood共通GPUサービス | 実GPU共有サービスと実会話の記録、#375のmain統合 | 今回の実利用image・有効設定を正式測定ごとに固定。#423 |
| 2. 準備成功前の開始禁止・再起動後参照 | 検証記録の「共有サービスの準備と実合成」、worker回復記録 | 開始準備・失敗表示・手動再試行の現行版検証は#350と共通。準備時間とTTFAを分離 |
| 3. voice ID固定・旧音声保持 | [採用資産](../../characters/miori/voice.md)とWAV/hash、変更済みIDの拒否unit | 原本を維持。今後の声変更は別ID。音質合格とは別 |
| 4. Session内の声固定・再開始で変更 | [Irodori client試験](../../backend/tests/unit/test_irodori_client.py)にfactoryでの固定検証 | 実Session再接続中の設定保持・次Sessionの設定反映の通し証跡は#423の残件 |
| 5. dogfood優先・待機／実行上限 | [実優先処理・HTTP取消](irodori-329/real-priority-cancel.json)、[service unit](../../backend/tests/unit/test_irodori_service.py) | 実行中を横取りしないことは過去確認。全混雑条件の後続会話影響は#423 |
| 6. TTS失敗で全応答終了・次発話回復 | client unitの失敗後応答、[実worker期限・回復](irodori-329/real-worker-deadline.json) | ブラウザの失敗表示・旧出力停止から次の会話までの実接続は#423。worker回復だけで代用しない |
| 7. CCV voiceから参照音声利用 | 採用CCV・登録済みvoiceを用いた実合成・実会話 | 導入済み。声の再選定を条件へ戻さない |
| 8. CCV設定とBE接続・timeout | 元Issueで確認済み、client/service unit、#375 | 既存契約を維持する回帰確認 |
| 9. VOICEVOX互換・明示切り戻し | 元Issueで確認済み、過去のVOICEVOX比較100件 | 現行FE/BE切り戻しは#424の更新手順と区別。自動fallbackへ変更しない |
| 10. LLM全文を待たず区間再生 | 実会話と先頭TTS計測、client unitのsegment streaming | 現行版でも継続。分割間の自然さ・gapは#423 |
| 11. 割込・切断・合成失敗の旧音声排除 | [割込100件](irodori-329/take-turn-100-report.json)、[旧出力100件](irodori-329/stale-output-100-report.json)、限定再接続 | 過去の割込証拠を維持。現行設定の切断／失敗／再接続は#350/#424と共通で確認 |
| 12. 暗黙の声・engine切替なし | client unitにHTTP取消・fallbackなし、固定voice検証 | 障害時のブラウザ通し確認は#423の残件 |
| 13. 実スタックの連続会話・割込・再接続 | [実スタック集計](irodori-329/real-stack-diagnostics.json)の3発話／3割込／1障害注入等 | 記録済み。少数診断を現在の100件品質受入に代用しない |
| 14. TTFA・TTS内訳・比較・共存・音質 | speed 1.00のIrodori/VOICEVOX各100件と共有GPU観測 | speed 1.02の第1段階正式測定、品質・試聴は#423。既存p95 2708.4msは未達 |
| 15. ユーザーのモデル最終判断 | 2026-09-14のdev設定採用記録。Irodori / miori-b3-4221 / speed 1.02を採用 | 採用の撤回・再選定はしない。全音質条件の受入とは分ける |
| 16. 検査・CIと実接続の区別 | #375/#387の統合、各検証記録に対象版と試験範囲 | 今回の変更版のCIと実接続を個別に記録。main向け差分レビューを別途実施 |

## #330の条件と#423への対応

[声選定記録](../miori-voice-selection-2026-09-13.md)と
[音声metadata](../../characters/miori/assets/voice/miori-b3-4221.json)を正本とする。

| 元条件 | 対応 |
|---|---|
| 1. キャラクター設定と声の方向 | 選定記録に既存CCV確認とユーザーによる方向決定を保存済み |
| 2. 公式Gradio UI | 未構築。ユーザー指示で公式ServerのVoice Design APIによる生成・会話上の試聴へ変更した記録あり。未実施UIを完了へ書き換えず、新たな構築条件にも戻さない |
| 3. 非量子化Smallによる複数候補 | 47候補の条件を[selection.json](../artifacts/miori-voice-330-2026-09-13/selection.json)へ保存済み |
| 4. 比較音声・caption・revision・seed保存 | 同記録と採用WAV/hashに保存済み。不採用原WAVの保存範囲も選定記録に明記 |
| 5. ユーザーの試聴・採用 | B-3/seed 4221と、その後のspeed 1.02採用を記録済み。AIの代理承認ではない |
| 6. 別文・分割の声質／自然さ／読み | 実合成素材あり。分割間にユーザー指摘、固定無音追加は未採用。#423で読み・gap・声質・人の試聴を継続 |
| 7. 保存先・voice ID・CCV引渡し | 採用資産、共有サービス実合成、現行CCVへの引渡し済み。原音声を変更しない |

## #393〜#398の条件と#424への対応

| 子Issue | 導入済み／既存証跡 | 残受入 |
|---|---|---|
| #393 M1 | [移設契約](../voice-backend-migration-contract.md)、[責務ADR](../decisions/voice-backend-authority-2026-09.md)、旧基準commitと設定の棚卸し | 今回の改善版・実利用設定を新測定の基準として固定。過去の基準と混同しない |
| #394 M2 | BE連続PCM/VAD、欠落破棄・上限・reset・session分離をmain反映。[実VAD module](../../backend/tests/module/test_backend_voice_input.py)等と固定fixture証跡 | 短発話／文中休止／雑音の品質と全境界coverage。文中分割2/100の未達 |
| #395 M3 | STT/Core/preview/確定判断/入力世代へ接続。[text優先module](../../backend/tests/module/test_conversation_text_priority.py)、過去take-turn100件の遅延 | 相槌判定・旧応答継続・全PCM相関、音声/text競合、遅延結果、実再生prefixの実接続 |
| #396 M4 | FEの会話用VAD依存解除、ACK入力認可・共通clientをmain反映。固定音声3往復等の限定確認 | focus/mute/thread switch、同一／別Session送信、再生観測、話し直し案内の通し確認 |
| #397 M5 | 既存reporter・時計相関・分母・回帰式をBEへ対応。前後5条件各100件、計1000件を収集済み | 通常VAD境界・相槌PCM・pause・再接続欠測を解消し、既存集計で再確認。新評価基盤は作らない |
| #398 M6 | [検証記録](voice-backend-vad-358.md)後半に実測・未達。旧version拒否、開始失敗cleanup、履歴Repository往復は限定確認 | 一括更新／切り戻し通し、残る実接続、人の実マイク／聴感、未完了差分レビュー。Desktop完成・dogfood配備は対象外 |

## #358の機能受入11項目と検証受入6項目

以下の番号は[親Issueの受入条件](https://github.com/FYuki/digital-souls/issues/358)の各節内の順。
詳細・元測定hashは[移設検証記録](voice-backend-vad-358.md)を参照する。

| 機能 | 証拠の範囲／残件 |
|---|---|
| F1 FE VADなしの3往復 | 固定WAVの実ブラウザ3往復は過去確認。改善後版と人の実発声を区別 |
| F2 BEの判断権 | schema/実装・実VAD moduleに対応。FE表示変更で判断が変わらない範囲は機能回帰で維持 |
| F3 短発話・言い淀み・雑音・長無音／長発話 | 区間処理の導入済みと品質を区別。文中休止2%と境界欠測は残件 |
| F4 相槌継続・take-turn・判定不能 | 過去の相槌判定不能20件と出力継続欠測を保持。take-turn遅延4指標は過去条件で確認。#350と共通 |
| F5 生成中／生成後の割込・遅延結果 | 局所試験と固定音声の一部証拠あり。両phase・取消不能provider・後発responseの通し対応が残る |
| F6 音声/text/focus/mute/thread切替 | [実会話suite](../../frontend/integration/voice/conversation-session.spec.ts)と[controls suite](../../frontend/integration/voice/conversation-session-controls.spec.ts)を実行先とする。今回版で全ケース完了の証拠はまだない |
| F7 text即時停止・先行音声破棄・確定履歴保持 | text優先moduleとcontrols suiteに対応。実接続の完了記録を追加する |
| F8 再接続・track交換・順序逆転・世代 | bridge/moduleに導入済み。実再接続の遅延・coverage・後続再生が未達。track等の競合を100件成功率と混同しない |
| F9 stale出力排除・中断履歴prefix | 過去の旧出力監査と履歴試験を維持。改善後版の取消／complete競合と実再生prefixを確認 |
| F10 protocol・準備／capacity／TTS失敗・過大buffer | 旧client拒否、開始失敗cleanup等は過去確認。全障害の実スタック一括合格ではない |
| F11 通常text・Tool・画面知覚・privacy・履歴・旧WS | 既存境界を保持。各機能の試験と実接続範囲を対応づけ、音声pilotだけで回帰なしとしない |
| Q1 全試験層と実BE VAD | #412のCI成功と過去実測あり。今回の変更版のunit/module/mocked E2E/実接続を記録 |
| Q2 既存fixture・集計の再利用 | 既存音声試験・reporterで継続。不足競合はそこへ追加 |
| Q3 固定版・全分母・内訳・resource | 前後1000件に記録あり。今回の速度改善前後は別系列、準備・初回・継続を分離 |
| Q4 絶対目標・比較式・既存未達 | TTFA 2000ms、再接続p95 3000ms、文中分割1%等を維持。#350既存未達と移設回帰を分離 |
| Q5 固定音声と人の受入の区別 | 固定音声の証拠あり。人の実マイク・聴感は未実施 |
| Q6 ADR/schema/実装/計測/client/更新手順の整合 | 文書と実装をmain反映済み。更新・切り戻しの通し実行と引渡し確認が残る |

## 更新・切り戻しとレビューの実行境界

[一括更新・切り戻し手順](../voice-backend-rollout.md)は未実施。
旧版→改善版→旧版で、同じ新規test data rootの会話履歴を保持し、FE/BEを一組で切り替える。
各phaseで会話終了・所有アプリ停止・再読み込み・旧client拒否・履歴継続を観測する。
[履歴Repository往復](../artifacts/voice-backend-358-history-roundtrip.json)だけではこの条件を満たさない。

2026-09-19 JSTに#408のGitHub reviewとコメントを再確認した。
CodeRabbitの差分reviewは取得できず、[手動依頼へのrate limit応答](https://github.com/FYuki/digital-souls/pull/408#issuecomment-5673455895)と
[automatic review skip](https://github.com/FYuki/digital-souls/pull/408#issuecomment-5673452338)が残っている。
main統合済みをレビュー済みとは扱わず、未レビュー範囲を実行可能な単位へ分けたレビューと必要修正を#424で続ける。

## 短い相槌の限定診断

[匿名診断結果](../artifacts/voice-quality-350-short-backchannel-source.json)は固定fixtureを直接STTへ渡した切り分け。
ブラウザ収録PCM・正式100件・人のラベル妥当性判断の代用ではない。
23/33の生成segmentは無音確率0.7881/0.6104で、既存0.6上限により除外された。
除外前の認識も予定の相槌とは一致せず、無音controlにも認識segmentが生じた。
無音除外の撤廃や曖昧な反応の一律相槌化は行わない。元fixture・ラベル・失敗分母を維持し、
実収録の確認と必要な人の試聴を#350/#424で続ける。

## 割り込み診断のfixture番号指定と集計の整合

#408の残レビューとして、VAD/PCM/割り込みreporterと匿名cohort検証を照合した。
runnerは小規模診断で `fixture_indices` を記録するが、相槌・take-turn reporterは
常に原catalogの先頭N件と照合していた。3番・1番を指定した2件の記録が
`labeled fixture coverage or order mismatch` で拒否されることを回帰テストで再現した。

両reporterは、元catalogと記録済み番号列から選択順を解決する。
10件以下・整数・1〜100かつcatalog内・重複なしを確認し、実trialのSHAと順序も照合する。
省略時の先頭N件、原catalogのhash確認、全試行分母、匿名化、時計の相関、
100件以上の合格条件は維持する。選択診断のためのcatalog編集・trial付け替えは不要になる。

2026-09-19 JST、CLI入口から原catalogのhash保持・schema検証済み出力までを含め、
以下の関連283テストが成功した（既存Starlette非推奨警告1件）。
これは集計処理の検証であり、実ブラウザで新しい発話試験を実施した証拠ではない。
過去の測定結果は再計算・上書きしていない。全#408差分レビューの完了は主張しない。

```sh
cd backend
.venv/bin/python -m pytest tests/unit/test_voice_quality_take_turn_report.py tests/unit/test_voice_quality_backchannel_report.py tests/unit/test_voice_quality_cohort_report_validation.py tests/unit/test_voice_quality_backend_vad_report.py tests/unit/test_voice_quality_vad_report.py tests/unit/test_voice_quality_stt_pcm_report.py tests/unit/test_voice_quality_pilot.py -q
```
