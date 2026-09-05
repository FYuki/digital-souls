# 外部能力としてのAddonと最小接続基盤（ドラフトADR）

- 作成日: 2026-09-05
- 文書段階: 作業ブランチ上の未受入ドラフト
- 状態タグ: 未付与。#152の方式選定・contractレビュー後に`ACTIVE`へ移行する。
- 対象: [#104](https://github.com/FYuki/digital-souls/issues/104)、設計作業 [#152](https://github.com/FYuki/digital-souls/issues/152)

> この文書は合意済みの設計原則と、今後比較・確定する項目を分けて記録する。
> MCP、HTTP API、SSE、WebSocket等の採用、wire schema、SDK、運用数値は未確定である。
> ドラフトの存在を実装完了・接続検証済み・後続実装の着手許可と解釈しない。
> 未受入の間は作業ブランチに保持し、現行実装の説明や既存のACTIVEなADRを置き換えない。

## 1. 背景

Addon基盤は、当初のREST／WebSocket二方式案から、最小接続、read-only Tool利用、
副作用操作、Event配送、会話統合、管理UI等を別Epicにする構成へ整理されている。
今回見直すのは通信方式の先行固定であり、Epicの責務分割や段階導入を取り消さない。

外部記録のCRUD、開発状況の観測、実時間で進むゲームは、通信頻度・副作用・状態寿命が異なる。
「記録ならREST」「ゲームならWS」「MCPを採用すれば安全に再送できる」とは決めない。
先に能力の意味と所有境界を定め、実際の接続先に対するFit/Gapで最小構成を選ぶ。

## 2. 合意済みの決定

### 2.1 Addonは外部能力の単位

AddonはCoreが利用する外部能力の単位とする。すべてを自作サーバーとして新設する必要はない。

既存MCPサーバーや既存APIで必要な観測・操作が提供されている場合は、Coreの共通許可境界と
Connectorを通して直接接続できる。独自の収集・変換・domain処理が必要な場合に、自作Addonを置く。

共通の内部利用IFを持つことと、独自wire protocolを作ることを区別する。
既存protocolへ独自invoke endpointや独自envelopeの実装を強制しない。

### 2.2 CoreとAddon／外部システムの責務

| 領域 | Core | Addon／外部システム |
|---|---|---|
| 共通の知覚 | PC画面認識、音声等 | 外部システム固有の観測を提供 |
| 状況理解 | 知覚・会話・記憶・外部情報の統合 | 根拠となる構造化状態と履歴を提供 |
| 行動判断 | 目標、優先順位、行動選択、停止方針 | 独断で別の戦略目標を選ばない |
| 具体的実行 | 許可された操作を依頼 | API呼出し、経路探索、domain処理、条件再検証 |
| 即時反応 | 許可範囲・方針を設定 | 許可された停止・回避・応戦等を実行 |
| 記録 | action監査、会話、キャラクターの経験 | domain事実・実行結果の正本 |

画面認識をCoreの責務とすることは、画面キャプチャまでBackendと同一processに置く要求ではない。
OS権限が必要な取得処理の別process化は許容する。取得対象、同意、画像保持、外部Vision送信等は
別設計で扱い、本ADRを画面取得・保存・外部送信の許可として使わない。

外部事実の正本が既存サービスにある場合、Addon側に同じ正本をもう一つ作らない。
Coreのaction logは「何を依頼して、どの結果を確認したか」を追跡するmetadataとし、
domain本文の無条件複製やpersona memoryへの自動昇格を行わない。

### 2.3 自作Addonは同一repoでも独立サービス

```text
digital-souls/
├─ backend/app/addons/          Core側の最小接続基盤
├─ addons/development-observer/ 最初の自作Addon（#58）
├─ frontend/
└─ docs/
```

自作Addonは別process・別DB・別runtime設定で独立して起動・停止・testできることとする。
Coreの内部module、repository、DBへの直接import／直接アクセスを連携手段にしない。
連携は選定した公開契約を通し、将来の別repository化を妨げない。

Observerが許可された開発ファイルを観測することは、Core packageへの実行時依存やDB共有とは別である。
参照権限を理由にCore内部へ密結合させない。

## 3. 能力の意味モデル

| 概念 | 意味 | 例 | 必須性 |
|---|---|---|---|
| Query | 状態・記録を取得する | 履歴、Git状態、ゲーム観測 | 必要なものだけ公開 |
| Command | 外部状態の変更を依頼する | 記録訂正、装備変更、移動開始 | QueryだけのAddonには不要 |
| Task | 時間をまたぐ処理を識別・追跡する | 進捗、完了、キャンセル | 任意能力 |
| Event | 起きた出来事を表す | test終了、戦闘、記録更新 | 配送方式とは独立 |

この4概念は、排他的な4つのwire messageを必ず実装する要求ではない。
Task開始はCommand、状態照会はQuery、進捗通知はEventとして表現できる。
Eventは履歴Queryの結果に含まれてもよく、Eventを持つだけでpushやsubscribeを必須にしない。

「睡眠開始」が直ちに受理され、その後の回復状態をQueryで観測できる場合、独立したTask APIを
設けずに成立しうる。Task ID、進捗、結果取得、cancel等が必要な接続先でTask能力を使う。

この意味モデルとMCPのTools／Resourcesは一対一ではない。MCP Toolには読み取りも含まれるため、
Queryを必ずResourceへ変換する必要はない。[S1]

## 4. 3つの層を区別する

```text
Coreの状況理解・行動判断
  ↓
Tool選択／権限／resource binding／副作用policy
  ↓
共通のAddon利用IF
  ↓
Connector（接続protocolへの適合）
  ↓
Transport（選定した通信方式）
  ↓
既存外部サービス または 自作Addon
```

MCPは能力発見やtool呼出し等を定めるprotocolであり、Streamable HTTP等がそのtransportである。
MCPとWSを同じ階層の排他的な選択肢にしない。[S1][S2]

`AddonManager`は明示されたaddon／operationを解決してConnectorへ委譲する薄い境界とする。
LLM、ChatService、PromptBuilder、character、conversation、binding、確認policy、
継続的な行動判断を所有しない。上位はprotocolやSDK固有の型を扱わない。

最初の実装で必要なのは選定した最小構成であり、すべてのConnectorを作ることではない。
任意能力を利用できない場合はunsupportedとして扱い、別の操作で黙って代替しない。

## 5. 操作と観測の契約原則

### 5.1 提供能力と現在の行動候補

Capability Catalogは「何ができる接続先か」、現在の行動候補は「この対象・状態で何が可能か」を示す。
Coreは現在の候補から選択できるが、Addon／外部システムは実行時にも条件を検証する。

候補があることはCoreの実行許可ではない。権限・binding・effect・確認policyを別に適用する。
opaqueな行動参照は、そのまま渡し、自作・解析・修復・別対象用の変造をしない。
単純CRUDでは型付き引数と対象version等で十分な場合があり、動的行動候補を一律要求しない。

### 5.2 状態競合と結果不明（result_unknown）

| 状況 | 対応 |
|---|---|
| 状態変更で候補が失効 | 新しい観測を取得し、Coreが再判断する |
| 送信後に結果を失った | 同じ依頼について、接続先の保証する再送または状態照会を使う |
| 安全な再送・照会がない | 結果不明を維持して報告し、新しい操作として自動再実行しない |
| 実行済み結果が再返却された | 新規実行と区別し、二重操作・二重通知にしない |

通信エラーを一律retryにしない。接続先の再送保証、idempotency key、状態照会、期限を確認する。
共通Errorへ正規化しても、競合等の安全なdomain reasonや再観測の必要性を失わない。
#104の最小read-only Connectorでは自動retryを行わず、副作用の回復実装は#185へ分離する。

### 5.3 情報の正しさと鮮度

現在状態を判断するときは、権限内で、出所・対象・revisionが一致する構造化観測を、
過去の履歴・生成summary・Coreの推測より優先する。単に時刻が新しい情報を常に正としない。

取得時刻、出来事の時刻、対象scope、source revision、取得成否、欠落の有無は区別する。
具体的なfield名・必須性は後続schema設計で決める。鮮度不明は「最新」と表示しない。
外部の構造化データも他のresourceへのアクセス権や上位命令を与える根拠にはならない。

## 6. ニケロワから借りる設計

本節は2026-09-05にユーザーが共有した「NIKE ROYALE — AIエージェント参加ガイド」の
第5〜7、9、13、15〜16、20〜21節に基づく要約である。サーバーへ接続・登録・プレイして
検証したものではなく、対応するMCP version／SDKも未確認である。

| ガイド記載の仕組み | 本設計への対応 |
|---|---|
| Streamable HTTPのMCPでobserve／act／historyを提供 | Query／Commandを既存protocol経由で利用する例 |
| legal_actionsとaction_ref | 現在の行動候補とopaque参照 |
| act結果内のobservation | 行動後観測の再利用、不要な追加Queryの削減 |
| historyのcursor | 認知済みEventの段階取得。push必須ではない |
| ACTION_STALE | 再送ではなく再観測・再判断 |
| 同じaction_refの安全な再送とreplayed | ゲーム側が提供する重複防止契約 |
| response_policyに基づくサーバー自動処理 | Coreの方針に従う外部側の即時反応 |

```text
observe
  ↓ 現在の状態・警告・行動候補
Coreが0個または1個の行動を選ぶ
  ↓
act
  ↓ 結果と行動後のobservation
Coreが次を判断する
```

実時間で進むゲームでも、操作粒度が適切なら上記の要求・応答ループで成立しうる。
「実時間進行」と「高頻度の操作通信が必要」を同一視しない。

action_refの重複防止保証はニケロワ固有であり、MCPそのものの保証や全Addonの前提にしない。
参加コード・Bearer tokenを取得する作業、登録、プレイ、本番を用いた接続testは本ADRの対象外。
ガイド全文や非公開情報をrepositoryへ転載しない。

## 7. Coreの行動セッションとAddon Task

| 項目 | Coreの行動セッション | Addon Task |
|---|---|---|
| 例 | 試合終了まで観測・判断・行動を続ける | 指定地点まで移動する |
| 主な責任 | 目標、判断間隔、推論予算、会話優先度、停止条件 | 進捗、実行状態、結果、可能なcancel |

Coreの新規行動停止、通信リクエストのcancel、外部Taskのcancel、外部世界の時間停止は別である。
外部側で取消できない処理や、操作停止中も進む世界を明示的に扱う。

ゲームの高頻度制御は必要に応じてAddon／ゲームruntimeに置き、LLMを毎frameの制御経路へ
入れることを前提にしない。ただし戦略変更までAddonへ暗黙委譲しない。

これらのruntimeは#104／#152／#58の初期実装へ追加しない。必要になった時点で、
Task実行管理とCoreの継続判断を別の設計・実装作業として扱う。

## 8. Development Observerへの適用

#58はQueryとEventを中心とする。TAKT、Git、test等を観測・sanitize・構造化し、
Observerのdomain logへ保持する。高度な進捗判断やキャラクター発話はCoreに置く。

ログだけでなく、許可されたrepository／revision／pathに限って、現在の実装状況を確認する
読み取り境界を検討する。自由なshellやファイル編集、test実行、Git変更を許可するものではない。
path traversal、symlinkによる越境、secret、サイズ、取得時間、token上限を詳細設計する。

観測にはrepository／branch／commit／worktreeとtest対象revisionの対応を持たせる。
古いrevisionでのtest成功を現在のコードの成功と報告しない。不一致・不明を明示する。

サービス本体は#104後、on-demand会話統合は#105と#182後、Event会話統合は#187と#183後、
管理UIは#184に従って段階導入する。最初から全機能を要求しない。

## 9. 接続方式のFit/Gap（比較案・未選定）

以下は採用構成の候補であり、同じ階層の規格を順位付けした表ではない。
標準の提供範囲と、digital-soulsで追加実装すべき責務を分けて評価する。

| 候補構成 | 比較上の利点 | 検証すべきGap | 初期採用状態 |
|---|---|---|---|
| MCP＋Streamable HTTP | 既存tool発見・呼出し契約を利用可能 | 接続先version／SDK、認証、schema、domain Event／Taskの対応範囲 | 未選定 |
| HTTP API＋必要時Query／polling | 必要な要求・応答から小さく始められる | operation discovery／mapping、polling負荷・鮮度、再送契約 | 未選定 |
| HTTP API＋SSE | 要求とサーバー側イベント配信を分けられる | 認証付き購読、cursor／replay、proxy timeout、欠落検知 | 未選定 |
| WebSocketによる双方向通信 | 継続的双方向交換の候補 | RPC相関、再接続、認証更新、順序・流量・復旧契約 | 未選定 |
| local IPC等 | ローカル配置時の比較対象に追加できる | 別host展開、process管理、認証・隔離・運用負担 | 必要時に比較 |

MCPのprotocolとtransportの分離、HTTP応答のJSON／SSEは[S1][S2]を参照する。
SSEのイベントストリームとWebSocketの双方向通信の仕様は[S3][S4]を参照する。
これらの方式だけで、domain transaction、認可、冪等性、完全replay、任意Taskのcancelが
自動的に保証されるとは扱わない。

### 評価シナリオ

| シナリオ | 必要能力 | 比較する主な条件 |
|---|---|---|
| #58の現在状況問い合わせ | Query | binding対象・revision・鮮度、schema、LLM不使用の接続test |
| 単純な外部記録 | Query＋Command | 確認、対象version、保存完了、重複防止 |
| ニケロワ型ゲーム | Query＋Command、履歴Event | 動的候補、観測再利用、競合、同一依頼の安全な回復 |
| 長時間ゲーム操作 | 任意Task＋Query／Event | 進捗、期限、cancel可能性、停止と外部状態の違い |

### 選定手順

1. #58を最初の利用対象に、必要な認証・schema・配置・応答期限・情報鮮度を定義する。
2. protocolの版、SDKの対応版、接続先が実際に提供する能力を一次資料と限定PoCで確認する。
3. 初期構成の適合と、後続Command／Event／Taskへの拡張限界を比較する。
4. 根拠と採用しない理由を記録し、最小の1構成を選定する。全候補の同時実装をしない。
5. 内部contract、Manifest、Connector mapping、fixture、conformanceを固定してレビューする。

応答時間等は目標値を後から根拠付きで設定する。根拠のないミリ秒値を本ADRで保証しない。
測定時は外部応答、queue待ち、推論、回答開始を分解し、protocolだけを遅延原因と決めつけない。

## 10. 最小基盤の不変条件

#104は明示的なread-only Query呼出しを成立させる最小境界とする。
Command、Task、Eventは意味上の拡張点を識別するが、すべてのruntimeを先行実装しない。

- 内部利用IFと外部wireを分離し、内部contract versionと接続先protocol versionを混同しない。
- 静的Manifestでaddon、operation、接続profile、secret参照、schema、enabledを解決する。
- discoveryを採用しても、発見した能力を無条件に許可しない。
- endpoint、secret、raw error／payloadをLLM、Frontend、通常logへ露出しない。
- 採用方式に応じて接続先制限、認証、timeout、サイズ、形式、schemaを検証する。
- 予期される運用失敗は共通Resultへ正規化し、安全なdomain reasonを保持する。
- 自動retryを行わず、運用上のAddon停止でもCore起動を継続する。
- Manager／ConnectorはLLMを呼ばず、利用タイミング・binding・確認を決めない。

詳細なfield、ID形式、HTTP endpoint、MCP method mappingは未確定である。
内部contractはJSON Schema Draft 2020-12を基準に検証可能にするが、既存protocolへ独自envelopeを
追加することは前提にしない。payload schemaはAddonが定義し、Core境界での検証方法を固定する。

## 11. 安全性・情報境界

認証された接続で届いたデータも、上位命令としては扱わない。外部文書・プロフィール・ログ・
説明文によってtool許可、secret送信先、binding、Core policyを書き換えない。

ユーザー×Addon×resourceの許可と接続資格情報を区別し、接続の再作成をプレイヤーやresourceの
新規作成と結び付けない。識別は接続先の明示的な契約に従う。

#185の高影響操作に対する確認要件は維持する。ゲームの通常操作の事前許可を検討する場合も、
試合・対象・操作範囲・予算を限定した後続設計にし、実ファイル削除・外部公開・購入等の
確認をまとめて省略する根拠にしない。

raw観測は無条件に保存しない。Coreでの利用は現在turn等の必要範囲に限定し、
会話履歴・RAG・通常logへのrawコピーを避ける。観測の正本、sanitizedな一時利用、
metadata監査、人格の経験を分ける。

## 12. Epicの境界

| Issue | 所有する責務 | 今回の変更 |
|---|---|---|
| #104／#152 | 最小接続・選定・内部contract | REST固定を外し、Fit/Gapを先行 |
| #153／#154／#156／#159 | Registry・最初のConnector・Manager・受入 | #152の選定とcontract確定をgateにする |
| #182 | Queryの選択・binding・回答統合 | 接続方式非依存、出所・鮮度を保持 |
| #185 | Commandの確認・実行追跡・回復 | 競合と結果不明を区別 |
| #187／#155／#190 | Event配送・復旧・流量・受入 | WS固定を外し配送方式を選定 |
| #183 | Eventの会話通知・自発発話 | 接続基盤に混ぜない |
| #184 | runtime管理・状態・UI | 最小接続から分離を維持 |
| #186 | 配布・同期・SDK | 最小接続から分離を維持 |
| #58 | 開発観測・sanitize・記録・payload | 共通画面認識はCore、実装参照は限定読み取り |
| 後続設計 | Core行動セッション、Addon Task | 今回の初期基盤には追加しない |

詳細な進捗・作業ブランチ・完了チェックはIssueで管理し、本ADRを進捗表として更新し続けない。

## 13. 未決定事項と受入ゲート

未決定: 初期Connector、protocol／SDKのversion、認証profile、内部schemaの具体field、
Manifestとdiscoveryの照合、応答期限・サイズ上限、Event配送・保持・回復の保証、Taskの必要範囲。

#152の受入には、選定理由、対象version、機械検証可能な最小contract／mappingとfixture、
主要失敗の扱い、秘密情報境界、別process conformance条件のレビューが必要である。
ドラフト作成だけでは#152をcloseせず、#153以降を実装readyとしない。

pure contractはunit test、test-owned別process接続はmodule testとしてCI実行し、
#58等の実Addonとの実接続はintegration testとする。ゲームの本番登録やプレイを自動testに使わない。
受入後にtesting-policyへ確定内容を反映し、system-architectureは現在実装の記述を保つ。
未実装構成を実装済みと記載しない。

## 14. 帰結と採用しない前提

能力と通信を分離することで、単純記録、開発観測、既存MCPゲームを同じ上位利用境界で扱える。
一方、異なる接続先の意味を正規化しすぎない配慮と、権限・鮮度・復旧能力の明示が必要になる。

現時点で採用しない前提は、全AddonのREST＋WS二重実装、全機能のMCP化、独自wireの強制、
全ゲームの高頻度WS制御、全AddonのTask／push必須、通信成功を操作完了と同一視する設計である。
特定方式の将来採用を拒否する決定ではない。

## 参考資料

以下の公開一次資料は2026-09-05に参照した。参照した版をそのまま採用版とはしない。
SDK・接続先との互換性確認は方式選定時に実施する。

- [S1] [MCP Architecture overview（2026-07-28 docs）](https://modelcontextprotocol.io/docs/2026-07-28/learn/architecture): protocol／transport、Tools／Resourcesの説明。
- [S2] [MCP Transports overview（2026-07-28 specification）](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports): transport binding、Streamable HTTPの位置づけ。
- [S3] [WHATWG HTML: Server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html): サーバーからのイベントストリーム。
- [S4] [RFC 6455: The WebSocket Protocol](https://www.rfc-editor.org/info/rfc6455/): 双方向通信とフレームの基礎。
- NIKE ROYALE — AIエージェント参加ガイド: 2026-09-05にユーザーが会話で共有した本文。第5〜7、9、13、15〜16、20〜21節を要約。実サーバー未検証。
- [repository-policy](../repository-policy.md)、[ADR状態の運用](README.md)、[testing-policy](../testing-policy.md)。
