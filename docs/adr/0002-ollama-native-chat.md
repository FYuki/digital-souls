# ADR 0002: Ollama native Chat経路の明示

- Status: Proposed
- Date: 2026-10-03

一部を[ADR 0027](0027-tool-use-and-activity-runtime.md)のツール利用と実行基盤の決定で置き換えます（2026-10-11 ユーザー承認。Core が tool 実行を追加しないことを前提とした記述。Ollama 経路の制約は維持）。

## 背景

既存のLiteLLM 1.77.7では、Ollamaのtemplate文字列によるtools能力判定が
gemma4:12bのnative toolsを認識せず、JSON形式への暗黙変換に進むことを確認した。
またthinkingで出力上限に達した応答の`done_reason=length`が`stop`へ変換された。
HTTP 200や空のcontentだけを正常なテキスト応答の証拠にはできない。

## 提案

LiteLLMを1.103.2、対応するOpenAI SDKを2.54.0に固定し、lockを更新する。
Ollamaには`ollama_chat/`のみを使用し、旧`ollama/`経路は送信前に拒否する。
固定SDKのnative tools直接転送と終了理由変換をmock HTTPで検証する。
Coreはtool実行、独自Ollama通信、モデルfallbackを追加しない。

管理者の固定profileに任意の`ollama_think: boolean | null`を追加する。
false/trueはnative APIの`think`へ転送し、nullまたは省略時は送らずモデル既定値に従う。
他providerへの設定、文字列や数値によるboolean指定は起動時に拒否する。
他providerでは未指定だけを許可し、明示的なnullも設定の誤りとして拒否する。
callerはthinking設定を上書きできず、要求途中のprofile変更も行わない。

SDKは依然`tool_choice`を黙って削除するため、Ollamaでは値によらず送信前に拒否する。
`allowed_parameters`への登録でもこの制限は解除しない。toolsを提示した場合の選択は
モデルに委ねられ、必ず呼び出す保証はない。streamingは引き続き検証済みのOpenAI
native Chat経路のみとし、Ollamaへ拡張しない。

SDK更新時の既存OpenAI stream timeout回帰にも対応する。新SDKの例外変換は
元のHTTPX例外をPythonの`__context__`に保持するため、既存の型による有界探索に
この経路を追加する。例外本文・生成文・URLからの分類や公開はしない。

独立レビューで確認した契約不整合も修正する。SDKの思考内容等の拡張fieldは
message/deltaの公開field許可リストで除外し、非streamの返却messageをそのまま再送できる
入出力契約とする。空の最終contentとlength終了理由は維持し、思考本文は保存・公開しない。
streamのroute probeにはモデル名だけでなく実要求のtools・reasoning・api_base等を渡す。
GPT-5.4以降等のfunction toolsによるResponsesへの切替も拒否する。SDK実呼出の第2判定と
Core guardを照合し、endpointの管理者設定順も同じ条件で検証する。

## 代替案

| 選択肢 | 判断と理由 |
| --- | --- |
| 旧SDKのままOllama固有の変換をCoreで補修する | 不採用。providerごとの通信・変換の保守がCoreへ入り、SDKとの二重管理になる。 |
| Ollamaを独自HTTP adapterで実装する | 不採用。native toolsの問題は回避できるが、認証・エラー・stream所有権の保守範囲が増える。 |
| thinkingを一律無効化し、未対応parameterを黙って削除する | 不採用。管理者が選ぶモデル既定動作とcallerの要求を変更し、契約違反を隠す。 |
| 固定SDKを更新し、検証済み経路と公開fieldに限定する | 採用提案。既存ライブラリを使い、未対応経路は送信前に拒否して外向き契約を検証できる。 |

## 影響

native toolsと終了理由をSDK経由で保持でき、Coreがtool実行やfallbackを持たない構成を維持できる。
一方、依存更新には追加パッケージとOpenAI SDKのmajor更新を伴うため、lockと既存契約の回帰検証が必要になる。
旧ollama経路、Ollama tool_choice、非Ollamaへのollama_think明示設定は起動時または送信前に拒否する。
既存の該当設定は管理者が修正する必要があり、黙った互換処理は行わない。
思考等のSDK拡張fieldは公開応答から除外するため、それらを履歴として要求するモデルはこの契約の対象外となる。
新しいprovider能力やSDK route変更は、固定依存の更新と追加検証なしには対応済みと扱わない。

## 制約と検証

LiteLLM依存追加はlockに含め、既存のOpenAI streaming・close・cancel・Responses拒否と
全品質ゲートを再実行する。native toolsのIDはSDKが生成する場合があり、引数のJSON表記は
Ollamaのobject形式との変換により空白が変わりうる。意味とID・結果の関連を検証する。
物理context上限、稼働時context設定、注入文脈byte budgetは別の値として扱う。
thinking無効化は出力を必ず保証する機構でも、人格品質の証明でもない。

## 関連Issue・PR・証跡

- [Issue #9: 受入条件](https://github.com/FYuki/digital-souls-core/issues/9)
- [作業PR #10: 実装・独立レビュー修正](https://github.com/FYuki/digital-souls-core/pull/10)
- [main向けPR #11: レビューと統合](https://github.com/FYuki/digital-souls-core/pull/11)
- [revision付き検証証跡](../evidence/2026-10-03-ollama-native-chat.md)
- [修正後headの実モデル機能再試験](https://github.com/FYuki/digital-souls-core/pull/10#issuecomment-5965415841)

## 一次資料

- [LiteLLM 1.103.2 release](https://github.com/BerriAI/litellm/releases/tag/v1.103.2)
- [固定版Ollama Chat変換](https://github.com/BerriAI/litellm/blob/v1.103.2/litellm/llms/ollama/chat/transformation.py)
- [LiteLLM Ollama公式資料](https://docs.litellm.ai/docs/providers/ollama)
