# ADR 0011: 固定SDKによる明示的なローカルembedding接続

Status: Proposed

置換注記（2026-10-08）: embedding 未注入時に部分文字列検索を維持する記述は、[ADR 0022](0022-memory-retrieval-from-records.md) の storage を読まない空結果で置換します。

一部を[ADR 0021](0021-postgresql-only-storage.md)のPostgreSQL一本化で置き換えます（SQLite schemaへの言及。検索設定の識別と保存済みデータを変更しない方針は維持）。

日付: 2026-10-05

## 背景

[ADR 0010](0010-in-process-memory-search.md)はプロセス内embedding portと一時的な順位付けを定め、
実adapterを後続としました。このADRでは、trusted起動コードが明示注入するローカル通信adapterへ
接続範囲を拡張します。ADR 0010の検索認可・出典・撤回・保存境界と、未注入時の部分文字列検索を維持します。
稼働サービスや私的履歴へ自動接続せず、実モデル評価は別の明示的な作業です。

## 選択肢と決定

OpenAI互換の通信を自前実装せず、既存lockの`openai==2.54.0`が提供する
`AsyncOpenAI`の`embeddings.with_raw_response.create`を使います。型付き返答への変換前のJSONを
検証し、bool等がfloatへ変換されることで不正値が受理されないようにします。SDKの更新、新しい依存、
外部API経路は追加しません。
会話providerと共有する`local_sdk.py`に、検証済みloopback接続とrequest単位のclient所有を集約します。

`LocalEmbeddingProfile`と`LocalEmbedding`は`local_embedding.py`へ置きます。
profileの`enabled`は既定でfalseです。設定を読み込んだだけでは起動せず、trusted起動コードが有効化した
profileからadapterを作り、`MemoryService(..., embedding=adapter)`へ注入した場合だけ使用します。
無効なadapterを注入した場合は検索を拒否し、部分文字列検索へfallbackしません。

| 設定 | 契約 |
| --- | --- |
| `profile_id` | 秘密を含まない固定設定名 |
| `api_base` | `http://127.0.0.1:<port>/v1`。数値portを必須とし、userinfo・query・fragmentを拒否 |
| `model` | 接続先で固定したembeddingモデルのalias。既存chat用aliasへ限定しない |
| `model_digest` | 運用者が確認して固定するモデルrevision/digest。adapter自身は実ファイルを検証しない |
| `dimensions` | 返答に要求する1〜4096次元。requestの`dimensions`には送らない |
| `timeout_seconds` | 0より大きく15秒以下。既定15秒 |
| `enabled` | 明示的にtrueにした設定だけが非空入力を送信可能 |

`encoding_format="float"`で文字列batchを送ります。互換serverごとの縮約機能を前提にしないため、
`dimensions`をrequestへ送らず、応答vectorの実際の次元を検証します。modelは返答と完全一致を要求し、
object型、入力と同数のdata、重複・欠落・範囲外のないindex、次元、有限の数値、非ゼロvectorを検証します。
返答行の並びはindexで入力順へ戻します。不正返答を黙って補完したり、別モデルへ再送したりしません。

## 通信と認可の境界

`httpx.AsyncClient(trust_env=False, follow_redirects=False)`をSDKへ注入し、SDKのretryは0にします。
API keyには公開のダミー値を明示し、環境変数のkey・organization・projectを使いません。
`OPENAI_CUSTOM_HEADERS`が非空ならclient構築前に拒否し、SDKの環境header追加による認証情報や
Hostの上書きを防ぎます。proxy環境変数にも依存せず、redirect先や外部fallbackへ本文を送信しません。
clientは呼出しごとに所有し、成功・失敗・cancel時ともshield付きでcloseします。
loopbackの接続先プロセス自身による外部送信やモデルの真正性まで証明する境界ではありません。

本文のmemory/local許可と意味分類は従来どおり`MemoryService`が担当します。adapterのscannerは
追加の決定論的検査であり、`embed`単体をprivacy認可APIとして扱いません。候補全体の認可とsource再検証が
完了してからqueryと候補を1 batchで送信し、await後とdispatch前のguardを維持します。
タイムアウトは内容なしの`memory_embedding_timeout`、その他の失敗は`memory_embedding_failed`とし、
本文・応答・例外文字列を公開エラーへ含めません。MemoryService経由では既存の汎用境界が
タイムアウトも`memory_embedding_failed`として返します。いずれもcancelは伝播します。

## 検索設定の識別と互換性

`EmbeddingSpace`へ任意の`configuration`を追加し、既定値は`in-process`とします。
既存の3引数の構築を維持し、ローカルadapterは正規化endpoint・profile ID・timeout・enabled・adapter/SDK
識別情報をconfigurationへ格納します。model/digest/dimensionsと合わせて検索guardのsnapshotに使い、
途中の設定変更で古い検索結果やcontextを送信しません。`configuration`は秘密を含まない識別用の値です。

この識別は検索専用です。保存済み抽出jobの`_versions()`、Memory本文・ID・source refs/epochs、SQLite schema、
撤回outbox、再構築承認を変更しません。vectorの永続化・共有cache・新DBは追加しません。
`MemoryEmbedding` Protocol自体は任意のPython実装をsandbox化するものではありません。

## 評価と未実施範囲

合成corpus用の評価ハーネスは、既定で偽embeddingを使って指標と報告形式を検証します。
この出力は`quality_evidence=false`とし、実モデル品質の証拠にしません。
実profileを明示した場合だけローカルembeddingを測定できます。embedding呼出し件数を記録し、
全候補が0件で実呼出しがなければlocal modeでも`quality_evidence=false`です。Recall・precision・MRR・空結果を報告し、
閾値でモデルの採用や品質良好を自動判定しません。本文を結果へ複製せず、case/memory IDと集計値を使います。
詳細は[意味検索評価手順](../memory-evaluation.md)に記載します。

llama.cppの`/v1/embeddings`は`none`以外のpoolingを要求します。embedding用途に対応するモデルと
server設定を別途確認する必要があり、既存chat用gemma稼働系で動くとは主張しません。
[llama.cpp公式server仕様](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#post-v1embeddings-openai-compatible-embeddings-api)を参照してください。

実API呼出し、モデル取得、GPU操作、dogfood変更、私的履歴import、検索品質・速度の実測はこのsliceでは
NOT RUNです。PostgreSQL等へのDB変更、永続vector index、常駐処理は後続範囲です。

作業範囲は[Issue #41](https://github.com/FYuki/digital-souls-core/issues/41)で追跡します。

## 参照

- [既存lockと同じOpenAI Python SDK v2.54.0](https://github.com/openai/openai-python/tree/v2.54.0)
- [記憶APIと接続例](../memory.md)
- [無効が既定の合成profile例](../../core/examples/embedding.example.json)


共有clientを使う既存chatでも、LiteLLM側の`OPENAI_ORGANIZATION`またはglobal `organization`が
非空の場合は、SDK clientのorganizationを上書きする前に拒否します。通常の`transport=sdk`経路は
この拒否の対象外です。いずれも環境変数を一時的に書き換えず、私的値をエラーへ含めません。
