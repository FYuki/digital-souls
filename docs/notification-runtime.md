# 通知の設定・利用・検証

## 実装と責務

Epic #183 / #158 / #363 / #191 は、共有のEventRuntimeを購読し、会話と別の通知として保存・表示する。
[要件指示書](epic-183-notification-requirements.md)を具体化し、実装は次に置く。

- `app.notifications.contracts`：登録時の担当・対象・Policy、独立参照、有限制限。
- `NotificationStore`：個別通知、ユーザー設定、処理位置、既読の版、保持・継続予算。
- `NotificationRuntime`：独立Event consumerとUI向けAPI。ToolRuntimeの開始・終了に連動する。
- `NotificationReader`：通知IDまたは独立したCore管理参照を解決し、共有Gateで必要時だけ取得する。
- `NotificationCenter`：時系列一覧、通知元・担当・未読の絞り込み、設定、詳細、既読・未読・非表示。

保存・取得ではLLMのToolDecision、会話作成、履歴・RAG・TTSを呼ばない。
DS_NOTIFICATION_CONFIGが構成済みで、InferenceTargetが未設定、または必須推論先の起動probeが接続不可・timeoutとなる場合、同じ本番Backendが通知限定で起動する。通知・連携管理・キャラクター一覧を利用でき、会話・音声・記憶管理など未構成のAPIは503、WebSocketは1013を返す。通知画面にも停止中の機能を表示する。
/health/readyはreadyとmode=notifications_onlyを返し、/health/inferenceは503のままとする。会話再開には設定・接続を復旧してBackendを再起動する。通常起動後のLLM停止も通知workerには影響しない。
部分的・不正なInferenceTarget設定、認証・モデル指定の誤り、通知設定・データ領域・復元状態の異常は起動失敗として扱い、通知限定起動で隠さない。通知設定のない従来の起動契約は変更しない。通知限定起動は会話・記憶DBのschema更新や音声サービス起動を行わない。
その場合の会話Tool routing呼出しは明示的に拒否する。

## 登録設定

`DS_NOTIFICATION_CONFIG`に管理側JSONの絶対パスを設定する。未設定時は登録なしの空一覧となる。
コンテナでは、既存data rootのマウント内など、Backendから読める場所に置く。
接続は既存Addon管理、sourceは[Event設定](addon-event-runtime.md)で登録する。
通知登録ごとにsource取得を複製せず、`notification:<registration id>`を独立consumerとして使う。

以下は単発結果の例。digestは確認済みactive Capability SnapshotのTool native定義を
`app.external_mcp.models.digest`でハッシュした実値へ置き換える。例の文字列のままでは検証を通らない。

~~~json
{
  "version": 1,
  "registrations": [{
    "id": "export-run-1",
    "source_id": "task-events",
    "character_id": "miori",
    "owner_user_id": "local",
    "auth_user_id": null,
    "viewers": [],
    "event_type": "task.completed",
    "kind": "result",
    "decision": "ignore",
    "match": {"task_ref": "task-1", "execution_ref": "run-1"},
    "detail": {
      "kind": "tool",
      "ref": "read_task_result",
      "definition_digest": "sha256:実際のnative定義のdigestに置換"
    },
    "arguments": {},
    "reference_arguments": {
      "task_id": "task_ref",
      "execution_id": "execution_ref",
      "revision": "revision"
    }
  }],
  "limits": {"max_per_user": 10000, "poll_seconds": 5}
}
~~~

- 担当・所有者・公開先・外部認証主体・接続instance・binding・操作定義・Task／実行回・Policyは登録時に固定する。
  変更する場合は新しい登録IDを使う。既存通知や開始済みTaskを新しい対象へ付け替えない。
- `owner_user_id`は既定`local`。HTTP APIも既存UIと同じ単一ローカルユーザーを使い、任意user IDを入力させない。
  内部では閲覧者と接続認証主体を分離し、公開先に含まれていても現在の接続grantを持たないユーザーへ生成・表示しない。
- `binding_id`は管理側の固定対象ID。固定`arguments`はbinding制約と一致させる。
  会話で選択した対象へ追従しない。接続の再登録やbinding変更も取得時に拒否する。
- `kind=monitor`は最新状態を取得し、通知発生時点の本文とは限らない。`kind=result`はTask・実行回のmatchとEventのrevisionを必須にする。
- `reference_arguments`は許可された参照欄を固定のTool引数へ対応づける。Resourceは確認済みURIを固定し、任意URL・引数を受け付けない。
- `decision`は`ignore`（既定）／`state-only`／`notify`。
  ユーザーのsource＋event type設定はignore／notifyを上書きするが、管理metadata専用のstate-onlyを通知へ昇格させない。
- 登録は最大256件、DBに残る登録識別は最大1024件。登録UI・scheduler・依頼参照の寿命管理は後続の担当範囲。

## 個別保存・設定変更・保持

通知と通知consumerの処理位置を同じSQLite transactionで保存した後にEvent receiptをACKする。
保存失敗時は位置も戻り、ACK失敗・再起動では同じ配送を安全に再処理する。
通知の削除後も処理位置を残すため、再配送で期限切れ通知を復活させない。
単発結果は登録・受信者・Task／実行回・revisionのhashでも重複排除する。
この補助識別は登録・受信者ごとに最大1024 revision。超過は通知を増やさず取得不能状態として再確認する。

OFFでも共有sourceと通知consumerは継続する。ONへの変更時には共有sourceが受信済みのepoch・位置を保存し、
consumerが遅れている場合や再起動後でも、その位置以前を通知へ戻さない。
新規購読時のsnapshotは既存状態であり、過去の全件を新着通知にしない。
Backend停止中にまだ受信していない履歴は#187の復旧規則に従う。

通知DBは`DS_DATA_DIR/notifications/notifications.sqlite3`。
WAL・FULL同期とtransactionを使い、本文・成果物・raw endpointを保存しない。
正規化した参照、固定分類、発生時刻、初回保存時刻、期限、通知状態、操作版のみを通知へ持つ。

- 保持は初回保存から既定30日。古い発生時刻でも保存から数える。既読・非表示・再配送では延長しない。
- 件数上限はユーザーごとに既定10,000件。30日平均で約333件／日の範囲を確保し、
  他ユーザーの流量で自分の通知が押し出されない単位とした。
- 本文を除いた参照metadataは数KB以内だが、SQLiteのindex等もあるため、10,000件を容量保証とはしない。
  想定流量に応じて設定可能。最大100,000件／ユーザー、保持は30日以下に制限する。
- 上限超過時は未読を含め、初回保存が古い順に削除する。同時刻は通知ID順で決める。
  元の期限まで「件数上限による履歴不足」を表示する。提供元のEvent欠落とは別に表示する。
- APIアクセス時と毎分の保守処理で期限を除去する。ブラウザを閉じても購読・保存を続ける。
- 通知の削除・非表示はTask取消し、監視解除、依頼参照の削除、会話への取込、報告完了を意味しない。

既存の世代backup形式は会話・人格記憶DBが対象であり、この通知DB・Event DB・Addon設定を含まない。
通知を含む環境移行ではBackend停止下で関連DB・設定を整合した同一時点のものとして保持する。
通知DBだけを消す、旧版へ戻す、Event DBと別時点で復元する操作を通常の再起動と扱わない。
dogfoodへの配備・backup形式拡張はこの実装の実サービス受入とは別に扱う。

## 詳細の提供元契約

Toolは`structuredContent`、Resourceは単一text content内JSONで、次の形式を返す。
通知のEvent metadataに本文を入れない。

~~~json
{
  "state": "available",
  "references": {
    "resource_ref": "artifact-1",
    "task_ref": "task-1",
    "execution_ref": "run-1",
    "revision": "1"
  },
  "text": "この実行の結果本文"
}
~~~

取得不能時のstateは`expired`／`deleted`／`not_found`／`unavailable`／`permission_denied`。
availableでは対象参照の一致を確認する。単発結果のrevision不一致は`revision_mismatch`とし、
別実行の成功や最新Resourceで穴埋めしない。監視のrevisionは更新され得る。

取得結果はsanitize済みtext、出典・担当、revision、取得時刻、状態、`untrusted=true`を返す。
native応答は最大4MiB、表示は既定16KiB、取得は既定15秒以内。
外部本文・URLを操作命令や取得先にせず、取得成功だけでは既読にしない。

共有Gateの現在grant、snapshot、read分類、binding、継続許可とglobal／connection等の予算を毎回使う。
さらに登録時のTask／ruleを安定した範囲にし、既定12取得／分／範囲と60取得／分／担当をDBで共有する。
連打・複数端末・新しいloop・Backend再起動でこの窓はリセットされない。

登録済みreadには、本文を受け付けない`validate_reference_egress`を使う。
値は短いASCII参照token、有限整数、booleanだけとし、ネスト・URL・自由文・secretは拒否する。
呼出側guardが登録対象との一致を確認し、共有Sanitizerも再検査するため、LLMの意味判定を必要としない。
通常の会話Toolや外部writeは従来の`validate_egress`を継続する。
独自ActionPolicyが参照専用判定を構成しない場合も、その既存送信判定を省略しない。

## 独立参照と後続consumer

Coreの依頼側consumerは`NotificationReader.reference(registration_id, event_key, metadata)`で
登録対象と照合したReferenceを作り、通知とは独立して保持できる。
再取得は`read(reference, viewer, purpose="registered_consumer", caller_character_id=登録時担当)`を使う。
異なる担当からの暗黙取得を拒否し、通知IDの引用だけでは参照を新規登録できない。
HTTP APIに任意の参照や取得先を登録する入口は設けない。

この参照の永続化・会話削除・報告状態との接続は#365。
#183では通知削除後の取得契約を試験するが、会話本文の無期限アーカイブや会話導線を追加しない。

## HTTP APIとUI

- `GET /notifications`：時系列一覧、未読数、設定・取得状態、保持不足。`source_id`／`character_id`／`unread`／`hidden`で絞り、
  `limit`は1〜100、`offset`でページングする。未読数は絞り込みから独立する。
- `GET /notifications/{id}/detail`：現在権限で必要時取得。既読を変更しない。
- `PATCH /notifications/{id}`：`state=read|unread|hidden`と操作対象のversion。競合は409で最新を再取得する。
- `PATCH /notifications/preferences/{source}/{type}`：enabledを保存する。
- 正常応答は`Cache-Control: no-store`。非公開の外部例外本文や任意入力のvalidation詳細を返さない。

サイドバーの「通知」から開き、15秒ごとの更新とfocus／online復帰時の再取得を行う。
本文は画面内の一時表示だけとし、詳細を閉じる・別通知を開く・タブを背景へ移す際に破棄する。
「確認して既読にする」または一覧の明示操作だけを既読境界とする。
画面のキャラクター切替・会話選択は通知の担当や取得対象を変更しない。

## 検証

単体試験：`backend/tests/unit/test_notification_store_unit.py`。
独立MCP適合試験：`backend/tests/module/test_notifications_module.py`。
本番起動境界：`backend/tests/module/test_notification_startup_module.py`（LLM未構成・接続停止、誤設定拒否、復元中の起動禁止）。
UIコンポーネント試験：`frontend/src/lib/notifications/notifications.module.test.ts`。

`python scripts/acceptance_notifications.py`は、独立した一時data root、本番app.mainの通知限定lifespan・共有ToolRuntime・HTTP・Vite・ブラウザと
MCP fixtureを起動し、ブラウザ終了中の保存、件数上限、詳細、明示既読、OFF／ON、絞り込み、期限切れを検証する。
LLM・会話・音声サービスを構成せず、API通信を差し替えない。
結果は`docs/artifacts/notification-183/`へ保存する。
提供元は合成データの適合試験用であり、実外部サービスやdogfoodの受入完了を示さない。

2026-09-16の実行証跡・PC／モバイル画面は[検証記録](artifacts/notification-183/README.md)を参照する。
