# frontend

`digital-souls`の自作フロントエンド（Vite + Svelte + TypeScript）。概念と実装名は[用語集](../docs/glossary.md)、FE/BEの責務境界は[アーキテクチャ](../docs/system-architecture.md)を参照する。

## 実装している画面・操作

- テキスト／LiveKit音声会話、同じ実行中Conversation Sessionへのテキスト入力、割り込み・再接続・受理結果の表示。
- キャラクター別スレッドの作成・一覧・名称変更・アーカイブ・復元・削除、キャラクター／スレッドのピン留め、キャラクターの追加・非表示。
- PCサイドバー／compactドロワー、静止画立ち絵、履歴レイアウト、記憶管理、Addon接続管理・操作承認。
- Chrome／Edgeの標準pickerによる単一モニター／ウィンドウ／ブラウザタブの選択と、会話に応じた画面参照。

現在のアバター表示は静止画であり、Live2D／VRMのruntime統合やDesktop版を実装済みとして扱わない。

## 会話とSession

永続スレッドの`conversation_id`と、音声・テキスト入力を扱う実行Sessionの`session_id`は別である。
選択スレッドに実行中Sessionがある場合、テキストはそのSessionへ送る。別スレッドのテキストはHTTP経路で送信し、送信先を表示中スレッドと照合する。
入力の受理、応答生成、再生完了、privacyによる非保存は区別して表示する。

入力欄へのfocusによる音声入力抑止は、手動muteやSession終了と区別する。focusだけで応答再生を停止するのではなく、実際のテキスト送信等に伴う中断と分けて扱う。
送信結果が不明な場合の受理照合・再接続の契約は[混在入力ADR](../docs/decisions/conversation-session-text-input-2026-09.md)と[共通client ADR](../docs/decisions/conversation-session-client-2026-09.md)を参照する。

`src/lib/conversation-session.ts`は選択済みスレッドIDを保存・復元するhelperであり、Speech/Text共通実行clientそのものではない。
音声・入力状態の実装は`src/livekit/`、統合は`src/App.svelte`を参照する。

実装済みの機能と、連続運用・遅延・回復の受入結果は別である。
[混在Session受入](../docs/conversation-session-acceptance.md)、[dev回復記録](../docs/conversation-session-dev-recovery.md)、[連続操作試験](../docs/conversation-session-dev-operations.md)の残課題を参照する。無期限Sessionの保証はない。

## キャラクター・レイアウト・管理

キャラクター切り替えは常設セレクターではなく、対象キャラクターのスレッド選択または新規スレッド作成で行う。
設定ではBackendが検出したキャラクターだけを、表示名とIDを併記した候補から一覧へ追加できる。
立ち絵はカタログが返したURLを使用し、未設定・読込失敗時は共通プレースホルダーへ切り替える。

PCでは立ち絵を履歴の右側または背面へ配置できる。タブレット・モバイルでは背面配置に固定し、履歴範囲を下部50%、75%、100%から選ぶ。
設定はBackend SQLiteへ保存する。モバイルではVisual Viewportに追従して、ソフトウェアキーボードを除く表示領域に入力欄を保つ。
画面確認手順は[Epic #151受入](../docs/epic-151-acceptance.md)を参照する。

記憶管理は`src/lib/MemoryManagement.svelte`、Addon管理は`src/lib/AddonManagement.svelte`を入口とする。
スレッド削除と長期記憶削除は別操作である。接続の有効化、操作の許可、今回の実行確認も同じ状態として扱わない。
管理の操作・制約は[Addon管理](../docs/addon-admin.md)と[操作承認・回復契約](../docs/decisions/addon-action-approval-recovery-2026-09.md)を参照する。

## 画面共有・参照

画面共有は初期OFFで、再読込後に自動復元しない。単一のモニター・ウィンドウ・ブラウザタブを対象として選べる。
希望した種類と実際に選ばれたsurfaceが一致しない場合や、必要な取得条件を満たさない場合は送信しない。

ON中も画像を定期送信せず、発言をBackendが`inspect_screen`と判断した場合、または「現在の画面を参照」を選んだ場合に、新しい静止画を1枚送る。
同じ有効な共有Sessionでは質問ごとのpicker操作は不要である。OFF、対象・会話・character変更、track終了、Backend切断で旧generationを失効させる。
対象名とpreviewはローカルUIだけで扱う。画像や派生会話のcloud送信が必要な場合の同意は、共有対象を選んだ操作とは別に確認する。

実装は[`capture.ts`](src/lib/screen-perception/capture.ts)と[`ScreenCaptureControls.svelte`](src/lib/ScreenCaptureControls.svelte)、Windows実機の確認手順とmetadata-only証跡は[画面共有受入](../docs/screen-perception-browser-acceptance.md)を参照する。

## 起動・検証

通常起動はリポジトリルートの`scripts/start-all.sh`を使うProfile＋Docker経路で行う。
初回の共有サービス・dev用LiveKit・data rootの準備、状態確認・停止は[開発環境](../docs/development-environment.md)に従う。
Frontendだけの互換・単体開発起動は、通常の環境全体起動と区別する。

テスト・型検査・buildの入口はルートの[package.json](../package.json)、依存準備・mocked／実接続／実マイクの区別は[テスト方針](../docs/testing-policy.md)を参照する。
