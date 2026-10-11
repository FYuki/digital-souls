# ADR 0003: Coreのローカルllama.cpp Chat接続

- Status: Proposed
- Date: 2026-10-03

一部を[ADR 0027](0027-tool-use-and-activity-runtime.md)のツール利用と実行基盤の決定で置き換えます（2026-10-11 ユーザー承認。tools の実行ループを追加しない記述。llama.cpp の接続契約は維持）。

一部を[Issue #147](https://github.com/FYuki/digital-souls-core/issues/147)のcontext拡張で置き換えます（2026-10-10 ユーザー決定。context 4096を32768へ変更し、KV型はK/Vともf16を維持。[実測証跡](../evidence/2026-10-10-llamacpp-context.md)）。

## 背景

ユーザーは限定比較を受け、Core用の推論先をllama.cppへ切り替える方針を選択した。
比較ではwarm生成速度は同程度でTTFTに差があったが、背景GPU負荷、KV型、Flash Attention、
cache、計時境界を完全には揃えていない。一般的な性能優位の証明として扱わない。
既存Ollamaのモデル・unitを残し、他クライアントの接続先を一括変更しない。

## 選択肢と提案

| 選択肢 | 判断 |
| --- | --- |
| Coreがllama.cpp HTTP/SSEを独自実装する | 不採用。既存SDKと別の認証・stream所有権を保守する必要がある。 |
| 既知OpenAIモデル名に偽装して既存stream guardを通す | 不採用。実モデルと能力を誤表示し、route判定を迂回する。 |
| 運用者profileで検証済みlocal Chat transportを明示する | 採用提案。既存LiteLLM/OpenAI SDK経路を使い、実サーバーと外向き契約を検証する。 |

`transport=llamacpp_chat`には固定alias `openai/gemma4-12b`と運用者`api_base`が必要。
最初は`http://127.0.0.1:<port>/v1`だけを許可し、他モデル、remote、URL内認証、query、fragmentを拒否する。
通常のSDK transportにapi_base指定は追加しない。callerのHTTP契約にendpointやtransportは追加しない。
endpointは起動時snapshotに固定し、クラウド認証を転送しないためSDKには公開のダミー文字列を渡す。
これは認証情報の新設でも、ローカルAPIの認証機構でもない。

llama.cpp transportはnative OpenAI Chat SDKのstream所有権を使用する。一般SDK transportの
「metadataでchatと確認できるもののみ」という制限は維持する。local transportに限り、
検証済みaliasのmetadata未登録を許可するが、実要求のResponses bridge判定、alias書換え拒否、
prefix拒否は非streamも含め必須とする。新モデルや他の互換サーバーを自動的に許可しない。

## 固定するサーバー契約

公式image b11347 / revision `5fc4f3c8c7103ffd0b7ff5ee4855bcc78a3ed5cd`をdigestで固定する。
検証済みGemma 4 Q4_K_Mのmodel SHAは
`1278394b693672ac2799eadc9a83fd98259a6a88a40acfb1dcaa6c6fc895a606`。
GGUF内chat templateを使用し、reasoning off / enable_thinking=false、context 4096、slot 1とする。
text-only運用ではmmprojをマウントしない。物理context上限やCore注入byte budgetとは区別する。
toolsの実行ループは追加せず、native tool call・ID・引数・結果だけを往復する。

## 影響と制約

loopback URLだけでは環境proxy経由の外部送信を防げないため、ローカルprofile専用の
リクエスト所有HTTPX/AsyncOpenAIクライアントをLiteLLMへ渡す。`trust_env=False`と
`follow_redirects=False`を明示し、SDK共有clientと環境変数は変更しない。
streamのEOF・途中終了・cancel・timeoutでも、応答を閉じた後に専用clientを閉じる。
接続poolはリクエスト間で共有しない。この小さいローカル用途では境界と所有を優先する。

Dockerは非root、read-only filesystem/model、capabilityなし、loopback公開、restart=noを既定とする。
新規host systemd unit、認証情報、LAN公開、原本ACL変更を追加しない。
自動再起動はGPUメモリ占有と待受の自動復帰を伴うため、別途ユーザーが判断する。
Ollamaと同時ロードしない起動手順は、既存Ollamaサービスの停止を前提とする。
既存Ollamaクライアントへの影響があるので停止は自動化せず、rollbackでunitを再開できる状態を保つ。

人格品質、multimodal、他モデル、他クライアントの移行、外部配信は対象外。
この実装はPR #11のOllama SDK修正から別branchで進め、PR #11のheadは変更しない。

## 関連資料

- [API契約](../api.md)
- [運用・rollback](../llamacpp-operations.md)
- [既存Ollama SDK修正 PR #11](https://github.com/FYuki/digital-souls-core/pull/11)
- [Issue #13: 採用範囲と受入条件](https://github.com/FYuki/digital-souls-core/issues/13)
- [検証証跡](../evidence/2026-10-03-llamacpp-core.md)
- [llama.cpp固定revision](https://github.com/ggml-org/llama.cpp/tree/5fc4f3c8c7103ffd0b7ff5ee4855bcc78a3ed5cd)
