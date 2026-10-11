# ADR 0025: 常駐Coreの共通runtimeと外部インターフェース

Status: Accepted

一部を[ADR 0027](0027-tool-use-and-activity-runtime.md)のツール利用と実行基盤の決定で置き換えます（2026-10-11 ユーザー承認。「外部 Agent が tool 実行を担当する」の記述。会話中のツール利用は Core が持ち、会話外の活動の Agent が使う MCP の入口を Core に置く。共通 runtime と入口の方針は維持）。

日付: 2026-10-10

Accepted日: 2026-10-10（ユーザーとの合意日も同日）

ユーザーが、単一の常駐Coreへ入口を追加し、CLIは薄いクライアントとするA案を採用した。
採用範囲は本書の「決定」。OpenClaw固有の連携方式は検討依頼であり、
[連携検討](../openclaw-integration-design.md)の提案を採用済みとは扱わない。
関連作業: [Epic #134](https://github.com/FYuki/digital-souls-core/issues/134)。

## 背景

複数のUI・CLI・外部Agentから共通のキャラクターを利用することがCoreの目的である。
現状でも人格文脈・推論の`Inference`、履歴の`Conversations`、記憶・privacyの処理は
HTTP入口から分かれている。HTTP APIが現在の外部入口であり、評価は本体を直接呼んでいる。

ここでいうruntimeはCoreの回答生成と履歴・記憶・privacyを扱うアプリケーション層を指す。
外部Agentのtool実行ループや、LLMを実行する別プロセスをCoreへ移す意味ではない。

## 選択肢

- A: 一つの常駐Coreに外部入口を設け、各入口から共通runtimeを呼ぶ。CLIはCoreのクライアントとする。
- B: 各CLI・サーバーにCore本体を組み込み、それぞれが設定・履歴・記憶処理を持つ。

## 決定

Aを採用する。製品利用ではキャラクター設定、privacy、履歴・記憶の正本への操作、
推論先の設定を常駐Coreに集約し、入口ごとに本体を複製しない。
本体を直接呼ぶ構成は試験・評価で利用する。

- 外部入口はプロトコルの検証・正規化を担当し、共通runtimeを呼ぶ。
- CLIを追加するときはCoreへ接続する薄いクライアントとする。
- HTTP以外の入口も同じアプリケーション境界を利用する。MCPなど具体的な入口の実装は別作業とする。
- Coreは引き続き人格・履歴・記憶・privacyの境界を持ち、外部Agentがtool実行を担当する。

共通の人格を使うことは、別Bindingの履歴・記憶を共有する許可を意味しない。
既存のBinding分離、localhost制限、保存のopt-in、stateless APIの挙動を維持する。
記憶の形成jobの起動をPrivateAgentが担う既存の責務（[ADR 0017](0017-memory-formation-admission.md)）も変更しない。

## 影響

入口を増やしても人格・privacyの振る舞いを共通に保ちやすくなる一方、製品利用には
Coreの常駐が必要になる。推論先のLLM・GPUサービス自体のプロセス統合は要求しない。

今回の変更は方針の記録のみ。CLI・MCP・OpenClaw専用API、新しい入力由来の契約、
記憶形成処理は実装しない。既存コードの大規模な再編も前提としない。

## 関連資料

- [製品の範囲](../../README.md)
- [推論API](../api.md)、[履歴API](../history-api.md)
- [OpenClawとの連携検討](../openclaw-integration-design.md)
- [文書検証の証跡](../evidence/2026-10-10-shared-runtime-design.md)
