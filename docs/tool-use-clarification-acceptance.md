# 会話中の確認Q&AとMCP実行の受入

## 対象

実行前にCoreが不足条件を質問し、利用者の追加回答を使ってMCPを実行する経路を検証する。
MCPが操作開始後に返す`inputRequests`への応答（MRTR）とは区別する。

- 原要求と、最大4往復の確認質問・回答を同じ会話のTool loop内に保持する。
- 通常の会話履歴が切り詰められても、確認への回答をツール選択と引数生成へ渡す。
- 追加回答のキーワードも候補の絞り込みに使う。質問への回答は追加許可として扱わない。
- 完了・停止・期限切れ・別依頼への切替で不要な確認状態を破棄する。
- 4回確認しても条件を確定できなければ、同じ質問を無期限に続けず、その確認を終了する。
- MCP側の追加質問、Binding選択、既存Execution Gateの許可・副作用制御は維持する。

## 回帰テスト

`backend/tests/unit/test_tool_use_clarification.py`で、2往復の確認後の実行、履歴の切り詰め、
別会話・停止後への非伝播、確認回数の上限を検証する。
既存のTool suiteにはMCP側の追加質問とresume、停止・期限切れ・Bindingの回帰がある。

```bash
python -m pytest backend/tests/unit/test_tool_use*.py -q
python -m pytest backend/tests/module/test_chat.py -q
```

## 実接続

会話のツール選択には`INFERENCE_TARGET_TOOL_ROUTING`が必要であり、
`INFERENCE_TARGET_CHARACTER_LIFE`やMCP接続の有効化だけでは会話利用は有効にならない。
設定例は`backend/.env.example`を参照する。

`ELYTH_API_KEY`を実行環境へ安全に設定し、ローカルOllamaの`gemma4:e4b`と
`nomic-embed-text:latest`を用意する。通常pytestは個人の`.env`を読み込まない。

```bash
RUN_TOOL_USE_ELYTH_REAL_TESTS=true \
python -m pytest backend/tests/integration/test_tool_use_elyth_clarification_integration.py -q -s
```

実行時は既存fixtureが一時testデータへ分離する。公開投稿の`search_post`だけを許可し、
次の流れを、ToolService単独と通常のHTTP会話・履歴保存の両方で確認する。

1. キーワード未指定で検索を依頼し、質問を待つ。外部操作はまだ行わない。
2. まだキーワードを決めていないと回答し、もう一度質問を待つ。
3. 具体的な検索語を回答し、追加質問を繰り返さず実検索へ進む。
4. 取得成功の出典を確認し、HTTPでは最終回答と3ターンの保存まで検証する。

ELYTH側の機能未有効化や接続障害は成功扱いしない。実MCP・実LLMはmockへ置き換えない。
キー・投稿本文は証跡へ出力せず、終了時にテスト所有のMCP接続とアプリを閉じる。
ブラウザ音声・人格からの自律的な検索テーマ形成は、このケースの受入対象に含めない。

## 2026-09-08の検証結果

- Tool関連unit 69件、Chat module 33件、テスト分類5件が成功。
- 実Ollama `gemma4:e4b`と実ELYTHによる上記2ケースが260.54秒で成功。
- HTTPケースでは画面と同じstatus pollingを行い、推論待ちを利用者離脱と誤判定しない。
- mypy（254ファイル）・Ruffが成功。devのFrontend／Backend／ready gateは停止状態を維持。
- 途中で人物の興味を任せる入力は検索器が即座に検索を選ぶ場合もあったため、
  実接続の再確認ケースは「まだ条件未決定」と明示する。人格からテーマを生成する品質とは分ける。

[実接続と関連回帰の証跡](test-evidence/mcp-qa/real-and-regression.log)
