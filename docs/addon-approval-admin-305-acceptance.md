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
  And 次の利用時に再確認する
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

接続CRUD再実装、Tool個別permission、音声返答での承認、停止済み操作の自動再実行、長時間runtime本体、dogfoodデプロイは対象外。
接続のON/OFF、Autonomy Target、privacy/Egress、Core保護、snapshot、budget、stopは既存契約を維持する。

## 検証と証跡

2026-09-11に以下を確認した。自動回帰・制御fixture・独立MCPへの実接続を区別する。

- Backendの関連unit/module回帰110件が成功。音声入力待ち中の許可取消テストを加えた続行module全6件も成功。
- Frontend unit 802件、module 136件、承認UIのmocked E2E 6件が成功。
- Svelte/TypeScript、mypy（284ファイル）、ruff、Frontend buildが成功。
- 実接続受入1件が108秒で成功。テキスト続行・停止後の回答で再実行しないこと・次の1回で消費・同じLiveKit音声会話の続行とブラウザ音声再生・会話外設定の範囲分離を確認した。

実接続のrun IDは `ccdbe7fa-e1d2-460c-855d-2da596e0be66`、実装commitは `c2b4e8e012dfddb9849d66fdd6d5b26bd79da033`（開始時にtracked変更なし）。
[環境と結果](artifacts/addon-approval-admin-305/runtime-manifest.json)、[ブラウザ結果](artifacts/addon-approval-admin-305/browser-public.json)、[受入項目](artifacts/addon-approval-admin-305/admin-action-evidence.json)、[実操作3回の適用記録](artifacts/addon-approval-admin-305/action-state.json)を保存した。以後のモバイルmocked E2Eセレクタ修正は製品コードを変更していない。

最初の実接続試行は、LLMが指定値に説明文を付けて書き込んだためファイル完全一致で失敗した。要求文で検証用の正確な内容を指定し直して再試行し、成功した。完全一致や実行回数の判定は緩めていない。

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

```bash
backend/.venv/bin/python -m pytest backend/tests/module/test_addon_approval_admin.py backend/tests/module/test_addon_action_continuation.py backend/tests/unit/test_addon_approval_reset.py backend/tests/unit/test_livekit_action_confirmation.py -q
ACCEPTANCE_INFERENCE_ENV=/path/to/private/backend.env \
  backend/.venv/bin/python scripts/acceptance_tool_use.py --addon-action --grep 管理画面
```

main向けPRは最新差分のCI成功、CodeRabbitレビューと指摘対応まで完了させる。mainへのマージはユーザーが行う。
