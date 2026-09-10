# #185 承認・回復の受入記録

親Issueは[#185](https://github.com/FYuki/digital-souls/issues/185)、最終受入は[#304](https://github.com/FYuki/digital-souls/issues/304)。
仕様は[承認・回復ADR](decisions/addon-action-approval-recovery-2026-09.md)を参照する。

## 状態（2026-09-11）

M1〜M4は各PRの全CI成功後にepicへ統合した。M5の制御MCPによる実プロセス検証は9件成功した。
独立MCPを通すブラウザのテキスト／LiveKit受入は実行中であり、まだ完了扱いにしない。
main向けDraft PRは[#311](https://github.com/FYuki/digital-souls/pull/311)。CodeRabbitの初回全差分レビュー21件へ回答し、修正をM5の[#310](https://github.com/FYuki/digital-souls/pull/310)へ反映した。
関連unit 125件・module 76件・mypyは成功。[対応表](artifacts/addon-action-185/review-disposition.md)に合意を変更するため採用しなかった指摘も記載した。M5統合後の最新差分レビューは未完了。

## 検証境界

| 検証 | 使用する実体 | 証明する範囲 |
|---|---|---|
| 独立MCPと会話 | 公開Filesystem MCP `2026.8.31`、実Ollama/STT/TTS、LiveKit `1.9.7`、Chromium | テキスト・音声から3択承認、実ファイル更新、結果回答・再生 |
| 制御MCPと実プロセス | 別processのPython MCP、外部正本SQLite、Core子process | 確定結果、競合、外部commit後切断、Core保存前終了、安全な保存済み結果取得、cancel状態照会 |
| 自動回帰 | Gate・承認SQLite・会話境界・Frontendテスト。必要箇所の外部応答はfixture | 状態分離、競合、重複回答、単回承認、安全境界、stop、UI・音声承認禁止 |

制御MCPはCoreをimportしないが、本開発のために作ったfixtureであり、独立した公開MCPではない。
同suiteのprivacy scanner/Egress許可は検証用で、semantic privacyの実接続を証明しない。
独立MCPの音声入力はVOICEVOX合成発話をマイクMediaStreamへ流す。STT・LLM・MCP・TTS・LiveKit・再生は差し替えない。
これは物理マイクや人の発話品質の証跡ではない。

## 制御MCPの観測結果

`backend/tests/module/test_addon_action_process_recovery.py` の9件が **80.29秒で成功**。
一次出力は[process-recovery.txt](artifacts/addon-action-185/process-recovery.txt)。

| 条件 | 観測した結果 |
|---|---|
| APPLIED / NO_CHANGE / CONFLICT / FAILED | 各結果を分離し、再接続・同一活動の再評価で新規dispatchしない。CONFLICTは外部の最新状態を保持 |
| 外部commit直後にMCP process終了 | 外部更新は1回、CoreはRESULT_UNKNOWN。回復契約ありなら再接続後にstatusだけでAPPLIEDへ更新 |
| 同じ切断で回復契約なし | RESULT_UNKNOWNを維持し、runtimeを再構成しても再送・新規承認をしない |
| Taskにstop/cancel | 新規更新を停止。cancel要求後も外部がrunningなら停止済みと扱わず、再接続後の外部cancelledだけを確定状態に反映 |
| 外部応答受信後・Core checkpoint前にCore process終了 | SQLite記録はDISPATCHINGのまま。再起動後にstatus不明→保存済み結果replayで回復。外部のchangeは1回のみ |
| 初期60秒の会話外待機 | 実時計で60秒以上待機しDEFERRED。キューを保持。後からの単回承認ではdispatchせず、将来の別活動が1回だけ更新 |

replayは既に受理された依頼の保存済み結果だけを返す契約であり、不明な依頼を新規実行しない。
fixtureの制御ファイルは障害注入側だけが操作する。一般利用者へ公開するToolやCoreの新しい権限経路ではない。

## 要件と自動検証の対応

| 要件 | 検証箇所 |
|---|---|
| 接続 × 操作群 × 実行場面、初期値、拒否の効力、単回競合 | `test_addon_action_policy.py`、`test_addon_action_queue.py` |
| 静的分類＋実引数、外部送信だけでは高影響にしない、未知は安全に緩和しない | `test_addon_action_policy.py` |
| 待機期限・設定・遅い承認・重複回答・stop競合 | `test_addon_action_queue.py`、`test_addon_action_process_recovery.py` |
| 承認後もEgress、Core保護、binding、snapshot、grantを再検査 | `test_addon_action_queue.py`、`test_addon_action_recovery.py`、既存Execution Gate回帰 |
| 画面操作だけで再開、会話/STTでは承認しない、終了済みresponseを再開しない | `test_addon_action_conversation.py`、`test_addon_action_continuation.py`、`test_livekit_action_confirmation.py` |
| 3択、重複クリック、retry、テキスト／LiveKit表示、320px表示 | `ActionConfirmation.module.test.ts`、`e2e/addon-action.spec.ts` |
| 外部結果とruntime retryを分離、確定結果優先、状態不明を保持 | `test_addon_action_dispatch.py`、`test_addon_action_journal.py`、`test_addon_action_recovery.py` |
| 会話外活動の期限終了・次回適用・結果不明の活動再試行 | `test_character_life_actions.py` |
| 読取と変更の結果分類を区別し、外部payloadの自己申告で分類を書き換えない | `test_tool_use.py` |
| Coreが補うbinding引数名を提示し、元schema・値の非露出・明示値の不一致拒否を維持 | `test_tool_use.py` |

上記のファイル名は `backend/tests/unit/`、`backend/tests/module/`、`frontend/` 配下を指す。
実サービス不足をこの自動回帰の成功で補って完了扱いにしない。

## 実接続からの修正と未完了ケース

- 読取結果だけで変更要求を完了扱いにした例を確認した。結果にCoreの操作分類を付け、変更完了の判断と回答の指示を修正した。回答生成時に実在しない承認待ちを履歴から推測しないようにした。
- 音声の対象ラベルから元schemaの必須引数を構成する際、Coreのbinding補完が判断材料に含まれていなかった。引数値はCoreに残し、補完する引数名を提示するよう修正した。利用者が明示した値の不一致は引き続き拒否する。
- 引数名の提示だけでは実LLMが空文字・ラベル・仮のパスを埋めることが分かったため、Coreが発行する選択済みbindingへの参照も提示する。参照はそのbindingの固定引数だけへ解決し、その後に元schema・privacy・影響・承認を検証する。別の参照や通常の不一致値を自動置換しない。関連76件・会話外3件とmypyで検証した。
- これらの関連回帰68件は成功した（[binding-regression.txt](artifacts/addon-action-185/binding-regression.txt)）。実LLMの正確性をこの回帰だけで証明するものではなく、独立MCPの会話受入を別途継続する。
- 初期の試行では対象パス不足、実LLMからの追加質問、承認後の180秒待機上限、音声のTool実行失敗で完走できなかった。失敗した試行は成功証跡に含めない。
- `738d615` の試行（run `0e9a2a96-dea5-4d3a-a6a3-ca5f12a3cc5d`）でも単回承認後のHTTP応答が300秒以内に戻らなかった。外部ファイルは未更新で、送信記録は0件だった。
- 次の非同期診断から、続行APIがViteの一般管理API向け50秒制限で切断されることを特定した。`/continue`だけ通常チャットと同じ180秒へ分類し、回答保存APIは既存の期限を維持した。実HTTPプロキシの回帰は修正前にsocket切断で失敗し、修正後は関連17件が成功した（[proxy-regression.txt](artifacts/addon-action-185/proxy-regression.txt)）。この回帰では待機時間を全経路共通で1/100に短縮し、独立MCP受入とは区別している。
- 再実行入口は、実行commit・開始時の追跡対象ファイル変更の有無・サービスversion・固定metadataの処理段階・終了時の承認／実行状態を保存する。合成会話attachment内の一時pathも公開用に置換する。
- 音声の画面承認後に実更新・音声再生まで到達した試行で、試験側がSTT入力イベントだけを追っていたため再生の対応付けに失敗した。画面承認から発生するCore responseの`source_utterance_ids`で対応付け、同じresponseの完了・実再生sample数を確認するよう修正した。
- 推論contextを合計8192へ統一した試行でも、Action Egress独自の15秒制限でprivacy判定が時間切れになった。既存推論の既定上限に合わせて30秒まで判定を待つ。機微情報・判定不能・timeoutの拒否、再試行なしを維持し、関連40件が成功した（[egress-regression.txt](artifacts/addon-action-185/egress-regression.txt)）。この制御テストを実privacy受入の代わりにはしない。
- その後は読取・テキストの単回承認まで進んだが、次の変更依頼で選択器が承認要否を会話で質問した。対象と引数が揃えばCoreへcallし、Coreが操作群の承認を判定する責務を選択器へ明示した。関連39件が成功した（[routing-approval-regression.txt](artifacts/addon-action-185/routing-approval-regression.txt)）。過去の音声回答に実行予告だけの例もあったため、完了した変更は結果として回答する指示と、実更新後の完了回答を確認する受入を追加した。

## 再実行

worktreeのルートから実行する。Backendの全unitと全moduleは別processに分ける。
実接続は公開MCP依存・Chromium・Dockerと既存の推論サービスを必要とする。

```bash
backend/.venv/bin/python -m pytest backend/tests/module/test_addon_action_process_recovery.py -q
ACCEPTANCE_INFERENCE_ENV=/path/to/private/backend.env \
  backend/.venv/bin/python scripts/acceptance_tool_use.py --addon-action
```

受入scriptは一時data root、テスト所有Backend/Frontend/LiveKitを使用し、終了時に自身のprocessだけを停止する。
dogfoodのデータやサービスを変更しない。実購入・契約・本番データ破壊は行わない。
公開証跡へcredential・raw protocol payload・実会話履歴を含めない。

## 残る受入

- 独立MCPを通すブラウザのテキスト／LiveKitシナリオを完走し、provider/model・音声構成・実行commit・観測結果を保存する。
- M5の最新CIを確認してepicへ統合する。
- main向けPRに対してCodeRabbitの実差分レビューと指摘修正を完了する。mainへのマージはユーザーが行う。
- Addon管理UI（#305）、音声だけの承認、SDK Tasks自体の新規実装、dogfoodデプロイは今回の完了条件に含めない。
