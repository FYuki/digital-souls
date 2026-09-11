# #305 確認キュー・承認設定管理の受入

## 目的・合意

[#305](https://github.com/FYuki/digital-souls/issues/305)の管理UIを、#185の承認正本と#242のAddon管理画面へ接続する。
承認単位は接続 × 通常／ハイリスク操作群 × 対話中／会話外。キャラクターやTool別permissionを追加しない。
2026-09-11の要件対話で次を合意した。

- 保存済み承認・拒否を未承認へ戻せる。
- 有効な待機中の要求は管理画面からでも続行する。停止・期限終了後は将来利用にだけ効く。
- 未承認へ戻すと、同じ承認範囲の未使用単回許可と待機要求への予約も取り消す。開始済み操作は中断・巻き戻ししない。

## 受入条件

```gherkin
Scenario: 管理画面で有効な待機中の要求へ回答する
  Given テキストまたはLiveKitの操作が承認を待っている
  When 管理画面を開いて一度承認する
  Then 画面切替だけでは元の待機を終了しない
  And 元の要求の呼び出し1回分だけ許可して続行する
  And 同時回答や続行の再送で操作を多重実行しない

Scenario: 待機終了後の承認は将来利用にだけ適用する
  Given 元の要求は待機終了し未回答キューに残っている
  When 管理画面から一度承認する
  Then 元操作の実行や活動の自動再開は行わない
  And 同じ接続・操作群・実行場面の将来の呼び出しを最大1回だけ許可する
  And その回数と承認範囲を表示する

Scenario: 未承認へ戻す
  Given 同じ承認範囲に保存済み承認または会話外拒否がある
  And 将来利用用の単回許可または待機要求への予約が残っている
  When 管理画面で未承認へ戻す
  Then 保存済み設定と未使用許可を同じ処理で解除する
  And 会話側の古い待機も終了し、次の依頼では新しい確認を行う
  And 古い回答の再送から許可を復活させない
  And 別接続・操作群・実行場面へ変更を波及させない
  And 実行開始済みの操作を中断・巻き戻ししない

Scenario: 古い接続へ承認を流用しない
  Given 一覧取得後に接続が削除または異なるidentityで再登録された
  When 古い要求へ回答するか古い接続設定を保存する
  Then 変更を拒否し状態の再取得を案内する
```

## 実装側の判断とスコープ外

未回答キューを既定とし、回答済みの閲覧とカーソルによる追加取得を提供する。待機期限は削除期限にしない。
設定変更は常に承認・未承認、および会話外だけ永続拒否を提供する。単回許可は要求IDで重複排除したキュー回答から発行する。
画面配置、文言、APIと保存処理の構成、ページ分割は実装側への委任に基づく判断。

管理APIも既存のloopback限定運用を前提とする。[環境分離ADR](decisions/local-dogfood-environment-2026-08.md)と[インフラ方針](infrastructure-policy.md)のとおり、LAN公開・TLS・認証は別判断であり、認証なしのLAN公開は許容しない。管理者ロールや新しい認証方式は本Issueで導入しない。

接続CRUD再実装、Tool個別permission、音声返答での承認、停止済み操作の自動再実行、長時間runtime本体、dogfoodデプロイは対象外。
接続のON/OFF、Autonomy Target、privacy/Egress、Core保護、snapshot、budget、stopは既存契約を維持する。

## 検証と証跡

2026-09-11に以下を確認した。自動回帰・制御fixture・独立MCPへの実接続を区別する。

- Backendの承認Store、管理API、続行、生成用文脈を含む[関連回帰111件](artifacts/addon-approval-admin-305/reset-wait-regression.txt)が成功。予約取消し後に古い要求IDを再利用する不具合を再現し、新しい依頼では新規確認を作ることを検証した。
- Frontend [unit 802件](artifacts/addon-approval-admin-305/frontend-unit.txt)、[module 136件](artifacts/addon-approval-admin-305/frontend-module.txt)、3択すべてを含む承認UIの[mocked E2E 8件](artifacts/addon-approval-admin-305/e2e-review.txt)が成功。
- Svelte/TypeScript、mypy、ruff、[Frontend build](artifacts/addon-approval-admin-305/frontend-build.txt)が成功。mypyの一次ログは[子PRのBackend CI](https://github.com/FYuki/digital-souls/actions/runs/34560720485/job/103142755430)を参照する。
- 最終修正後の実接続受入2件が169秒で成功。テキスト続行・停止後の回答で再実行しないこと・次の1回で消費・同じLiveKit音声会話の続行とブラウザ音声再生・会話外設定の範囲分離を確認した。予約取消し後は新しい依頼で新規確認を作り、未承認の書き込みがないことも確認した。

実接続のrun IDは `869ff82c-2fb5-44e9-9115-4de7650a844b`、実装commitは `fefe06adad9a31e85d1b7d82e62ec388c4c0ead6`（開始時にtracked変更なし）。
[環境と結果](artifacts/addon-approval-admin-305/runtime-manifest.json)、[ブラウザ結果](artifacts/addon-approval-admin-305/browser-public.json)、[承認・続行の受入項目](artifacts/addon-approval-admin-305/admin-action-evidence.json)、[予約取消しの受入項目](artifacts/addon-approval-admin-305/reset-wait-evidence.json)、[実操作3回の適用記録](artifacts/addon-approval-admin-305/action-state.json)を保存した。以後の証跡・文書更新は製品コードを変更していない。VOICEVOXは稼働中コンテナから不変image IDを取得した。

実接続の途中試行では、指定値への説明文付加、完了表現「上書きしました」の判定漏れ、実行後に過去の承認案内を反復する返答、事前読み取りだけで書き換えを予告する返答で失敗した。検証用の操作名・引数を明確にし、完了表現を補足し、固定の過去承認案内を生成用文脈で区別したうえで再検証した。ファイル完全一致、実行回数、完了返答、音声再生の判定は維持している。最終runの成功はこの合成シナリオの受入であり、任意の自然言語依頼に対するLLM応答品質全般を保証するものではない。

| 要件 | 検証箇所 |
|---|---|
| 4範囲の設定・拒否解除・単回残数・identity・秘密非表示・キュー全件到達 | `backend/tests/module/test_addon_approval_admin.py` |
| 未使用予約取り消しと消費の競合・回答再送 | `backend/tests/unit/test_addon_approval_reset.py` |
| 明示回答・元会話続行・重複続行 | `backend/tests/module/test_addon_action_continuation.py` |
| 音声要求の主体照合とCore入力直前の有効性再検証 | `backend/tests/unit/test_livekit_action_confirmation.py` |
| UIの3択・二重クリック・続行再試行・設定範囲・安全なテキスト表示 | `frontend/src/lib/AddonApprovals.module.test.ts` |
| 管理APIの続行と実HTTPプロキシ期限 | `frontend/src/vite-proxy.module.test.ts` |
| 独立MCPとテキスト・LiveKitの管理画面承認・停止後単回承認 | `frontend/integration/addon-action/conversation.spec.ts` |

独立MCPの受入では公開Filesystem server、実LLM、実Whisper、実VOICEVOX、実LiveKitとブラウザ再生を使う。
音声入力だけVOICEVOX合成発話をマイクMediaStreamへ流す。物理マイクや人の発話品質の検証とは区別する。
会話外の待機・競合の決定論的検証は制御fixtureであり、ブラウザによる実会話検証とは別に扱う。
テストは一時data rootとテスト所有Backend・Frontend・LiveKitを使用する。共有推論サービスは停止しない。
実行ログは公開用に端末の色指定・ローカルworktree pathだけ除いて保存した。
VOICEVOXの`/version`応答が`latest`の場合、不変なビルド識別子にはならない。受入時に`ACCEPTANCE_VOICEVOX_CONTAINER`と、別WSL distributionなら`ACCEPTANCE_VOICEVOX_WSL_DISTRIBUTION`を指定し、稼働中コンテナの不変image IDを追加採取する。Dockerを読み取れない環境ではIDを推測せず、取得できたAPI版だけを記録する。

```bash
backend/.venv/bin/python -m pytest backend/tests/module/test_addon_approval_admin.py backend/tests/module/test_addon_action_continuation.py backend/tests/unit/test_addon_approval_reset.py backend/tests/unit/test_livekit_action_confirmation.py -q
ACCEPTANCE_VOICEVOX_CONTAINER=digital-souls-voicevox \
ACCEPTANCE_VOICEVOX_WSL_DISTRIBUTION=Ubuntu-dogfood \
ACCEPTANCE_INFERENCE_ENV=/path/to/private/backend.env \
  backend/.venv/bin/python scripts/acceptance_tool_use.py --addon-action --grep 管理画面
```

## レビュー対応

[子PR #314](https://github.com/FYuki/digital-souls/pull/314)のCodeRabbit全差分レビューで8件の指摘を受けた。
[修正PR #316](https://github.com/FYuki/digital-souls/pull/316)で索引、承認設定の一括read transaction、不正UUIDの409応答、実行ログへの参照、可変mypy件数の除去、VOICEVOX image ID採取、管理UI全3択のE2Eを対応した。
[追加修正PR #318](https://github.com/FYuki/digital-souls/pull/318)では、Storeが取り消した要求IDを会話処理へ通知し、未実行の待機だけを解放する。開始済み処理や返答ownerは中断しない。
キャラクター別permissionへの変更はIssue #305と合意済み契約に矛盾するため、[根拠を示してCodeRabbitが撤回](https://github.com/FYuki/digital-souls/pull/314#discussion_r3985926734)した。保存承認の共有と、要求への単回予約が別要求へ流用されないことを追加検証した。

main向けPRは最新差分のCI成功、CodeRabbitレビューと指摘対応まで完了させる。mainへのマージはユーザーが行う。
