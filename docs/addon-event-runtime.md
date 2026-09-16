# Addon Eventの設定・利用・検証

## 実装と境界

Epic #187 / #155 / #190 の実装は `backend/app/addon_events/` に置く。
`EventRuntime` は既存 `ExecutionGate` と同じ接続を共有し、`EventSourcePort` を実装する。
`ToolRuntime` の開始・終了に連動し、FastAPIでは `app.state.event_source` からCore内のconsumerが利用する。
LLM・会話・TTSはEventの取得・配信経路から呼び出さない。

[要件指示書](epic-187-addon-event-requirements.md)と
[復旧ADR](decisions/addon-event-recovery-2026-09.md)の要求を具体化した運用契約である。
通知の個別保存・表示・設定は #158 / #191、会話への報告は #365 のconsumerが所有する。
2026-09-16の[通知要件](epic-183-notification-requirements.md)により、通知レコードの集約は行わない。

## Sourceの登録

管理者が作成したJSONファイルのパスを `DS_MCP_EVENT_CONFIG` に設定する。
未設定かつ既存Event DBがなければEvent runtimeを作らない。
接続自体は既存の `DS_MCP_CONFIG` / Addon管理機能で登録する。

```json
{
  "version": 1,
  "sources": [
    {
      "id": "task-events",
      "connection_id": "registered-addon",
      "character_id": "miori",
      "history": {
        "kind": "tool",
        "ref": "event_history",
        "definition_digest": "sha256:実際のnative定義のdigestに置換"
      },
      "snapshot": {
        "kind": "tool",
        "ref": "event_snapshot",
        "definition_digest": "sha256:実際のnative定義のdigestに置換"
      },
      "arguments": {},
      "limits": {"poll_seconds": 60}
    }
  ]
}
```

例のdigestは説明用であり、そのままでは設定検証を通らない。
Coreが確認したactive Capability Snapshotから、Toolの `native_definition` 全体、
Resourceの定義全体を既存の `app.external_mcp.models.digest` でハッシュする。
説明文・schema・annotationの変更も固定した定義との不一致となり、再確認まで取得・配信を拒否する。
Event profileの存在だけではgrant、trust、sharing、bindingの許可を追加しない。

- 最大32 source、1 connectionにつき1 source。source内のconsumerは共通の通常取得を利用する。
- `history` / `snapshot` の `kind` は `tool` または `resource`。
  Toolは信頼されたread分類と、既存承認設定の「自律・通常操作」の継続許可が必要。
  未承認・取消し時は取得/配信を拒否し、バックグラウンドで確認待ちを作らない。
  未信頼readOnlyHint、unknown/write、確認必須操作は使えない。
- Tool履歴は固定 `arguments` に `cursor` と `limit` を追加する。snapshotは固定引数だけを渡す。
- Resource履歴はdiscoveryで許可済みのURIへ `cursor` / `limit` のqueryを追加する。
  外部から返されたnext URIへ追従しない。Resourceの固定引数、fragment、queryキー衝突は拒否する。
  snapshotはdiscoveryで許可済みのURIをそのまま読む。
- `user_id` / `binding_id` は任意。bindingは管理設定の対象IDであり、会話で発行した一時IDではない。
  毎回現在のBindingResolverで解決し、対象の固定引数とsourceの `arguments` の一致を確認する。
- `wake_resource` はdiscoveryで確認できるResource URI。MCP SDKの標準
  `subscriptions/listen` / Resource更新を使用する。非対応・切断時も定期取得を継続する。
  独自SSE/WSや独自MCP通知メソッドは追加しない。
- EventへmappingできないMCPはsource登録せず、通常のTool/Resourceとして使用できる。
- sourceの接続・所有者・対象・固定定義を変えた際は、既存登録へ自動再リンクしない。
  旧consumerを明示解除し、新しいsource IDで再登録する。単なる再接続は同じ登録を使用する。
  limits変更は可能。設定から外れたsourceはbufferを破棄して停止するが、consumer進捗を処理済み・解除済みにはしない。

## 公開結果契約 v1

機械可読な契約は [event-history.schema.json](../contracts/addon/event-history.schema.json)。
Toolは `structuredContent`、Resourceは1つのtext content内JSONで返す。
1応答は最大4MiB。入力要求・Tool error・壊れた結果を正常ページに変換しない。

snapshotは「最新状態」と「その状態に整合する確定済み末尾」を同一応答で返す。

```json
{"epoch":"era1","cursor":"era1~10","position":10,"state":{"status":"current","count":10}}
```

履歴は指定cursorの**直後**から返す。識別子・cursor・epochは最大256文字の
`[A-Za-z0-9_.:~-]`、positionは0以上の連続整数（最大2^53-1）。
cursorは提供元が解釈し、Coreは位置との対応を推測して生成しない。

```json
{
  "epoch":"era1",
  "next_cursor":"era1~11",
  "position":11,
  "events":[{
    "position":11,
    "cursor":"era1~11",
    "id":"event-11",
    "type":"task.completed",
    "occurred_at":"2026-09-16T00:00:00+00:00",
    "metadata":{"task_ref":"task-11","status":"succeeded"}
  }],
  "more":false,
  "gap":false
}
```

提供元の責務は次のとおり。

1. 同一epoch内のpositionを連続・不変とし、Event IDを一意・不変にする。
   削除・再作成等で番号を再利用する際はepochを変える。
2. ページ末尾cursor/positionを最後のEventと一致させる。空ページで `more:true` を返さない。
   1ページの新規positionは要求limit以下。完全一致の重複やstale行を含める場合も行数はlimitの2倍以下。
3. cursorが期限切れ・無効、または途中履歴を復元できない場合は `gap:true` を返す。
   epoch変更も欠落として扱う。任意の古いcursorを黙って最新へ読み替えない。
4. snapshotに安全な再開位置を提示できない場合は成功結果を返さない。
   snapshotは失われた過去の完了Event・報告済み状態の代用品ではない。

Coreはposition順に整列し、完全一致の重複を除き、取得済み位置以前のstale行を配送しない。
同じpositionの異なる内容、ID競合、連番欠落、確認できないcursorはsource停止となる。
buffer保持期間内のID競合もtransaction全体を拒否する。
IDキーはepochと提供元Event IDのハッシュ。保持範囲を越える再配送の冪等性はconsumerがこのキーで管理する。

Event内側のtype・時刻・metadata等が不正でも、安全な外側position/cursorを確認できた場合は、
不正行を理由付きgapにして後続へ進む。外側が壊れている場合は進めない。
許可metadataは `resource_ref/task_ref/rule_ref/execution_ref/revision/result_kind/status/count` のみ。
本文・任意payload・endpoint・secretは投影しない。secret登録変更後は配信時にも再検査する。

## Consumerの利用手順

Core内のサービスから、登録済みsourceと利用者のExecutionContextを使う。
`session_id` は `event:{source_id}`。会話を生成するIDではなく、再試行でも変わらない予算の単位である。
利用者・character・bindingは登録後に別のものへ差し替えられない。

```python
consumer = await events.subscribe(source_id, "notification-storage", context)
delivery = await events.read(source_id, consumer, context)
# delivery.eventsをキーで冪等に保存する。
# delivery.gapsは処理成功と分けて記録する。
# delivery.snapshotは初期/現在状態。過去Eventや会話報告を作り直さない。
await events.acknowledge(
    source_id, consumer, delivery.receipt, context,
    accept_gap=bool(delivery.gaps),
)
# 本当に購読を終了する操作でのみ呼ぶ。
await events.unsubscribe(source_id, consumer, context)
```

- 初回subscribeはその時点のsnapshot末尾を開始位置にする。後から追加したconsumerにも独立した開始境界を作る。
  同じconsumer IDの再登録は保存済み位置を維持する。
- `read` は配信候補とreceiptを返すだけで位置を進めない。
  consumer保存完了後にackする。保存済みreceiptは再起動後も有効だが、新しいread・ack後の古いreceiptは拒否する。
- `Delivery.recovery` はbuffer除去後の再取得範囲を示す。再取得は対象consumerだけの履歴読み出しであり、
  共通bufferへ再挿入せず保持期限を延長しない。
- 復元不能なら `Delivery.gaps` とsnapshot/安全な再開位置を提示する。
  gapがあるreceiptは `accept_gap=True` なしでackできない。
  latest gapの有限metadataと累計をconsumer進捗とは別に保存する。
- 下流で保存してackする前に停止した場合は再配送する。下流の保存/集約/報告とackの間に分散transactionはない。
  下流がキーの一意制約などで冪等性を保証する。外部副作用・端末表示のexactly-onceは保証しない。
- 遅いconsumerのack待ちで共通取得を停止しない。replayと通常取得はsourceごとに有限callを直列化し、
  共通接続のtimeout・利用制限を適用する。
- 認可・grant・現在のdefinition/接続世代・sharing・binding・egressは取得、buffer配信、ackの直前にも確認する。
  buffer配信にもGateの共有利用制限を適用する。解除は権限失効後でも同じconsumer所有者が実行できる。

## 保持・障害・終了

保存先はruntimeのデータルート内 `addon-events/events.sqlite3`。
SQLiteの単一transactionでbufferとingestion cursorを更新し、同期書込みを行う。
processごとの排他leaseで同じDBへの二重取得を拒否する。

| 設定 | 初期値 |
|---|---|
| 通常poll | 60秒/source |
| 保持期限 | 取得時刻から72時間（これ以上には設定できない） |
| buffer件数 | 10,000件/source |
| buffer容量 | 16MiB/source（投影JSON・cursor・管理用概算128byte/Eventを含む） |
| ページ | 最大128新規Event |
| Event投影 | 最大16KiB |
| retry | 初回5秒、指数backoff、最大300秒 |
| consumer登録 | 合計1,024件 |

bufferの上限超過は古い位置から除去する。期限は起動時・定期取得・配信前に適用する。
Backend停止中はDBファイルを能動削除できないが、再起動後に期限切れEventを配信しない。
物理DBにはindex・SQLiteページ等の固定管理領域もあるため、上表のbyte上限はファイル総容量ではない。

wake-upは集約して最短1秒（poll設定が1秒未満ならその間隔）で取得する。
共有Gateのglobal/connection/session制限と有限loop制限を継承する。
利用制限・unavailable・通信障害はbackoffし、wake stormで回避しない。
履歴欠落時のsnapshotが未確定なら元cursorを保持して再確認し、安全な位置を確認後に自動再開する。
外側契約不正や登録不一致による停止は管理側で原因修正後に `resume(source_id)` する。
resumeでもcursorは飛ばさない。

最後のconsumerを明示解除すると取得とwake購読を停止する。
画面を閉じる・consumerの処理が遅い・端末がオフラインであることは解除ではない。
Backend終了時はworker/wakeをcancelして待ち、consumer位置を保存したままDB/leaseを閉じる。
通常診断はsourceハッシュ、状態、固定reason、件数、容量、gap累計のみを返す。

## 検証

標準CIのunit/module suiteに含む。次のコマンドでも対象検証を実行できる。

```sh
python -m pytest backend/tests/unit/test_addon_events_contracts.py backend/tests/module/test_addon_events_conformance.py
```

- fixture `backend/tests/fixtures/addon_events/server.py` はCoreをimportせず別processで起動する。
  標準MCPのstdio/Streamable HTTP、Tool/Resource両経路、標準wake通知を実通信で検証する。
- 初回/後発開始境界、共有取得、独立ack、再起動/再接続、重複・stale・順序変更、
  不正Event、安全な次位置の有無、履歴期限切れ・epoch変更、snapshot未確定を確認する。
- 仮想時計で72時間境界・件数/byte制限、再取得、通知欠落、backoffを検証する。
  保存transaction途中の例外とOS process強制終了でcursorだけが進まないことを確認する。
- 現在のgrant/定義/binding/利用制限、配信時の秘密値再検査、unavailable、
  healthy操作集合の境界、最後の解除、shutdown、通常log/DBへの非漏えいを確認する。
  外部MCPはdegradedを広告しないため、このケースだけRegistryのhealthy集合境界へ障害を注入する。
- 本番Addon、通知UI/30日保持、会話・TTSの受入はこの適合試験の対象外である。
