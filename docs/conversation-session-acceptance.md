# Conversation Session受入記録（#319 / #327）

## 判定

受入は進行中。M1〜M7の基礎実装はEpicへ統合したが、#279との全体統合、#291のthread revision・Episode抽出予約、および本書の未検証項目は残っている。親IssueやM8の完了を意味しない。

## 対象と環境

- 実装: `epic/319-conversation-session`、M7統合commit `75e597077476a2f5d782bb2133bd18de98ed6a4e`。
- 最初の混在会話試験: M7作業commit `c2b94a74a9d214485ebed67c6c32b540c7afae06`。追加specは証跡採取時点ではworking tree。両commitの製品コードは同じ。
- 作業場所: `/tmp/digital-souls-327`、Profile `integration-voice`、環境ID `test`。
- データ: `frontend/test-results/runtime-data/integration-voice`。dogfoodのFrontend・Backend・会話DBは使用しない。
- Backend / Frontendはテストの環境管理から起動・終了。Ollama、Whisper、VOICEVOX、LiveKitは既存の共通サービスに接続し、所有・停止しない。
- Ollama `gemma4:e4b`、Whisper、VOICEVOX、LiveKit server `v1.9.7`、Playwright Chromium。
- 入力は固定WAVをAudioWorkletから実MediaStreamへ流す自動試験とUIへのテキスト入力。実STT、LLM、TTS、LiveKit通信、ブラウザ再生を通す。人の生マイク発話・聴感確認は未実施。

## 実行と初回証跡

`INFERENCE_TARGET_*`を確認済みローカルサービスに設定し、`LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`を実行プロセスへ渡す。秘密値は文書・artifactへ記録しない。今回の公開推論設定は `backend/.env.example` から取得した。最初の設定不足による起動失敗は `INFERENCE_TARGET_CHAT is required` であり、会話機能の成功として数えない。

```bash
cd frontend
npm run test:integration:voice -- --grep 'マイクボタン操作でOFFからSTANDBYへ遷移する'
npm run test:integration:voice -- --grep '実音声応答のuser発話'
npm run test:integration:voice -- conversation-session.spec.ts
```

初回の3実行はそれぞれ1件成功。混在試験は2026-09-11 09:28 UTC開始、47.8秒、skip / flaky / failureは0。各実行で環境のteardown完了を確認した。

[初回の機械可読証跡](artifacts/conversation-session-initial-real-2026-09-11.json)には固定入力由来のイベント相関、再生sample / packet数、履歴件数だけを残し、会話本文・秘密値は含めない。

混在試験で確認したこと:

- 音声→テキスト→音声が同じsession・Conversationで完了し、token取得は1回。
- 履歴件数は1→2→3、turn IDは3つ、最初のturn IDを保持し、テキスト入力の保存は1回。
- テキスト回答にもブラウザ再生完了があり、288,960 samples / 301 packetsを観測。
- テキスト回答の `source_utterance_ids` は空で、STT由来の入力に偽装しない。
- テキスト送信受理後に本文が空になり、focusが外れる。

文脈を参照する質問を送ったが、自然言語の回答内容の意味的正しさを自動判定した試験ではない。Coreの履歴コンテキスト投入はM3のmodule検証で別に確認している。

## 追加の実接続試験

[focusとミュートの証跡](artifacts/conversation-session-focus-real-2026-09-11.json): 4件成功、skip / flaky / failureは0、5.5分、teardown完了。focus中に固定音声の全区間を流してもCore入力は0件で、Enter受理後の次の音声を同じsessionへ保存した。クリック送信の手動ミュートON/OFFと、focusだけでは実TTS再生が止まらないことを確認した。混在会話も再実行して成功した。

[割り込み・別スレッドの初回証跡](artifacts/conversation-session-controls-first-real-2026-09-11.json): 生成中・再生中のtext割り込み2件は成功。旧回答のBE終端は一つで、新回答を同じsessionで再生した。旧回答の再生完了への誤帰属もなかった。別スレッド試験1件は、新規スレッド選択の非同期完了前にlocalStorageのIDを読んだため失敗した。選択完了を待つようspecを修正して再検証する。この初回失敗を成功件数へ含めない。

[別スレッドの再検証証跡](artifacts/conversation-session-routing-real-2026-09-11.json): 修正後1件成功（1.1分）、teardown完了。Aの回答再生はBを表示しても継続し、A/Bの履歴は各1件へ独立して保存された。Bだけが `/chat` を使い、Voice Sessionのtoken取得は全体で1回。Aに戻った後のtext送信でも切り替えミュートを保持し、AのTTS回答を再生できた。#279全体の未読バッジ・背景再生設定まで統合した試験ではない。

## 全音声スイートの回帰

commit `64d4150b437dcd7925d3670be82aacb46068c07c` と同じspec・製品コードで `npm run test:integration:voice -- --workers=1` を実行した。
[全音声スイートの証跡](artifacts/conversation-session-full-voice-real-2026-09-11.json) は14件成功（3.7分）、skip / flaky / failureは0、teardown完了。

- 新規9件: 生成中/再生中text割り込み、A/B送信、focus中の音声抑止と送信後再開、Enter/クリック×manual mute ON/OFFの4組合せとfocus中のTTS継続、音声→text→音声。
- 既存5件: マイク開始、VAD終了後のsession継続、通常音声の表示、同一sessionでの3往復、ラベル付き実音声のbarge-in。
- barge-inはBEの最終take-turn判定を確認し、固定音声開始からlocal stopの上限982.4ms、cancel確認の上限1,010.4msだった。既存assertionの3,000ms / 3,500msを変更せず成功した。1試行の結果であり、#150の100試行・p95の達成を新たに証明したものではない。
- textだけを投入するcontrols試験ではSTTを呼ぶ必要がない。Whisper実接続の処理結果はfocus後の音声・混在・既存音声試験で確認した。

## 通常textと実transport

[通常チャット](artifacts/conversation-session-text-real-2026-09-11.json) は `npm run test:integration:text -- chat.spec.ts` で1件成功（10.2秒）、teardown完了。Voice Sessionを開始せず実Backend・Ollamaの応答を表示した。

[実transport](artifacts/conversation-session-transport-real-2026-09-11.json) は `npm run test:integration:livekit -- --reporter=list,json --output=test-results/playwright-artifacts/transport-327` で2件成功（3.7秒）、teardown完了。実microphone PCMの送達と、明示的な一時切断から同じsession・generation 1への再接続を確認した。LLM/STT/TTS会話試験やネットワーク障害注入の代用にはしない。

この診断ページ `/voice/livekit` はViteの開発モードでのみ有効。今回のBackendは独立 `integration-transport-327-dev` data rootで起動し、別途所有するVite開発サーバーを5174番で起動した。`DS_PROFILE_REPORT`と`DS_BACKEND_ORIGIN`を同じtest Backendへ向け、`LIVEKIT_TEST_FRONTEND_URL=http://localhost:5174`を指定した。終了時にViteと所有Frontend/Backendを終了し、共有LiveKitは停止しなかった。

調整時の失敗もartifactへ記録した。runnerが正常readiness 204を200だけで判定した待機、製品buildへ接続して診断ページがなかった2件、Playwrightの既定出力先によるtest runtime identityの消失、回答前のprobeへ再生graph 1個を期待していた2件を区別する。data rootは新設し、`playwright.integration-livekit.config.ts`の出力先をruntime dataと分離した。`room.ts`はprobeやresponse IDのないtrackを回答graphへ接続しない既存仕様なので、その期待値を0へ整合した。実回答の再生成功は全音声スイートのsample/packet観測で別に確認しており、性能閾値は変更していない。

## 専用ネットワーク障害の初回診断

`f6d78373a9882174f2a966215d7bb424ddb41243` で `run_pilot.py --run-id conversation-327-reconnect-20260911a --inference-env backend/.env.example --livekit-env <専用一時env> --fault-bridge --network-fault --control-probe --trials 3 --scheduled-fixture` を実行した。専用の `ds-voice-quality-fault` Compose projectと新しいキーを使用し、通常サービスへ障害を入れなかった。

[初回の再接続証跡](artifacts/conversation-session-reconnect-first-real-2026-09-11.json) は失敗。2秒の切断がcontrolへ作用し、復旧後の同一sessionで次の音声回答を168,960 samples / 176 packetsまで完全再生できたが、control復旧上限5,064.7ms、audio復旧上限5,187.3msで、診断の3,000msゲートを超えた。次回答の成功を使って診断全体を成功へ読み替えない。

transport failure、音声出力区間の重複、packet証拠の欠測、出力経路のfailureは0。SDKの測定版を確認し、session終了、fault時計process終了、test環境teardown、専用LiveKitとnetworkの削除を確認した。これは1sessionの診断であり、100試行のp95の回帰有無までは結論しない。#319開始前のcommit `051115fa445e432274fa6b07e9b53a647fc429c1` を別worktreeに展開し、同じ条件で比較中。閾値は変更しない。

[開始前の版の比較試行](artifacts/conversation-session-reconnect-baseline-2026-09-11.json) は成功した。control復旧上限2,356.4ms、audio復旧上限2,476.4msで、次回答の完全再生・終了・専用環境の片付けも成功。各版1試行だけなので、#319の回帰とも既存の揺らぎとも断定しない。現行版の追加試行と、遅延が生じた状態同期・復旧probeを調査する。初回の5.19秒は除外せず残す。

[現行版の追加試行](artifacts/conversation-session-reconnect-repeat-real-2026-09-11.json) は製品コード変更なしで成功し、control復旧上限2,556.8ms、audio復旧上限2,686.6ms、次回答の完全再生を確認した。初回の遅延を単発の成功で解決済みにせず、既存 `run_reconnect_cohort.py` の100session集計へ進む。全試行と失敗を保持し、既存の10秒以内成功率99%以上・p95 3,000ms以下・重複0の条件を変更しない。

## 条件と検証の対応

| 条件 | 現在の証拠 | 残作業 |
|---|---|---|
| 共通入力・同一履歴・textのTTS | M3 module、実混在会話 | #291と統合したrevision / 抽出予約 |
| 同一スレッドの送信・session維持 | M5 unit/module/mock E2E、実混在会話、実A/B送信 | #279全体と統合した再検証 |
| focus抑止・Enter/クリック・manual mute | M6 unit/module/mock E2E、実接続で4組合せ成功 | #279全体と統合した再検証 |
| 送信失敗・不明結果・再接続照合・冪等性 | M2/M4/M5のunit/module/mock E2E | 実接続で確認した範囲の記録 |
| text優先・遅延STT / preview / 旧response排除 | M7 Core + bridge + 受付台帳module、FE unit/mock E2E、実生成中/再生中text割り込み | 障害注入の実接続範囲の記録 |
| BEの相槌・take-turn判断 | Core回帰、実take-turn / barge-in回帰 | 相槌コホートの実接続範囲の記録 |
| 通常text・通常voice・再接続 | 全voice 14件、通常text 1件、実transport 2件成功。障害後の次回答も完了 | 専用network障害の初回復旧上限5.19秒。開始前の版と比較中 |
| #279との状態・マイク・履歴分離 | M5/M6接点の基礎実装 | #279全体は未統合 |
| Desktop共通client | M4実装、契約文書 | 統合後の引き継ぎ最終確認 |
| main向けレビュー | 子PRのEpic向けCI成功 | 最終main差分のCI、CodeRabbitと指摘修正 |

## 競合条件の検証範囲

`frontend/src/InputBar.unit.test.ts`と`frontend/src/lib/InputBar.unit.test.ts`は、IME変換中のEnter、失敗時の本文/focus保持、送信確認中から受理確定への遷移、スレッド別下書きと遅着結果を検証する。実OSのIME操作の受入ではない。

受理台帳・結果照合・再送・ACK欠落の競合はM2/M4/M5のunit/module/モックE2Eで再現している。遅延STT・previewとtext優先の競合はM7のCore/bridge検証に対応する。実サービス側では通常の受理後再開、生成/再生へのtext割り込み、focus中の実音声抑止、別スレッドへの送信、実network障害後の会話継続を確認し、全競合パターンを実networkで再現したものとは区別する。

## 契約の引き継ぎ

#317は [共通clientの利用方法](decisions/conversation-session-client-2026-09.md) と [Voice Sessionテキスト入力契約](decisions/conversation-session-text-input-2026-09.md) を参照する。Desktop shell、UI、OS統合は本Epicに含めない。mainへのマージはユーザーが実施する。

## Privacy終端のレビュー修正

main向けドラフトPR #338のCodeRabbit指摘から、直接テキストがprivacyで省略された場合のwire通知欠落を確認した。Coreは`response_started`より前に省略へ終端し、従来serializerはSpeechのutterance破棄だけを通知するため、Text-only入力ではブラウザへ省略結果が届かなかった。

Epic統合commit `6b052700ee561b651d68013144cb72278902c5c2`（PR #339）は本文を含まない`response_privacy_skipped`を追加し、Appの履歴再取得、生成/再生終了、一時本文と遅着開始/deltaの除去を実装する。実SQLite/Core/serializerのSpeech・Text検証、開始前後のApp検証、共通投影、別応答維持、Room再生停止、schema/送信元の検証が通過した。CodeRabbitの再レビューでブロッカーなし、末尾空行の軽微な指摘も修正した。実際の保存拒否ルールによるブラウザ受入は、統合後に追加specを実行して記録する。

## #291固定commitとのローカル互換性

[ローカル接続検証](artifacts/conversation-session-memory-local-2026-09-11.json)は#319側`b416b363b1dc2a4ecc6cb49a1d7a108058b598bb`と、既存#291実装`6aee764cd702c5b947f5f7b4613f7e4c4c75c7cc`を独立worktreeで組み合わせた結果。音声→Text→音声の3 turnは実SQLiteで1つの永続予約へ合流し、thread revision=6、各turn revision=2となった。同じText入力IDの再送で追加更新はなく、新しいqueue instanceからrevision=6と3 turnすべてのsnapshotを取得できた。

共通履歴/Privacy module 3件と既存保存入口・queueの競合/回復38件が成功。抽出LLMの実接続試験ではなく、#291の公開Epicへの取り込みと最終runtime受入も未完了のため、M3とM8はopenを維持する。
