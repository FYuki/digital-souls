# 複数入口から同じ人格コアを利用する要件と最終構成（2026-09）

## 状態・適用範囲

**ACTIVE**。2026-09-26に#3の範囲確認で合意した要件と、将来の最終構成を記録する。
本ADRは要件・判断・不変条件を定める。実装・既定有効・dogfood受入を意味しない。
進捗・依存・完了条件は次のIssuesを正本とする。

| 範囲 | Issue |
|---|---|
| 共通Core呼出境界（既存Web・音声） | [#3](https://github.com/FYuki/digital-souls/issues/3) |
| 入口Adapter共通基盤とDiscord Bot | [#516](https://github.com/FYuki/digital-souls/issues/516) |
| ELYTHへの自律参加 | [#517](https://github.com/FYuki/digital-souls/issues/517) |
| 記憶・履歴の公開範囲（優先度低） | [#518](https://github.com/FYuki/digital-souls/issues/518) |
| 推論Runnerの差替え（Pydantic AI） | [#422](https://github.com/FYuki/digital-souls/issues/422) |

自律活動・外部送信・高影響操作は[Character Life共通契約](character-life-memory-personality-autonomy-2026-09.md)の§8〜§11を再利用する。
本ADRはそれらを再定義・緩和しない。

## 背景

キャラクターは自作Web FE（テキスト・音声）だけでなく、Discord、スマホ、CLI、SNS（ELYTH等）からも利用する。
どの入口から話しかけても「同じ子」として応答・行動することを目標とする。
複数の窓口を同じコアへつなぎ、記憶の出所で公開範囲を分けるという考え方は、
[OURIの設計紹介](https://note.com/ouri_chronicles/n/n8ab0737cf9d1)を参考にした。

## 要件

| # | 要件 | 合意内容 |
|---|---|---|
| R1 | 一つの人格コアを複数の入口から使う | 入口ごとに人格・記憶・権限を複製しない。入口の違いは表現手段と能力に限る |
| R2 | 対話型と活動型の入口 | SNS参加は自律で行う。利用開始はオーナーが承認し、その後の個々の行動は§5の制御で保証する |
| R3 | 記憶の公開範囲 | `public`／`private`の2段階。現行記憶は機微情報マスク済みのため全て`public`。`private`は秘密の話をするようになってから追加する（優先度低） |
| R4 | 相手の識別 | MVPは`owner`（ユーザー）と`other`（その他）の2種類。将来SNS等でIDがある相手は個別に識別できる構造にする |
| R5 | 入口をまたいだ一貫性 | ある入口での体験を他の入口の会話で使える（R3の範囲内） |

## 1. 最終構成

以下は目標とする構成であり、現行実装ではない。現行の構成は[アーキテクチャ](../system-architecture.md)を参照する。

```mermaid
flowchart LR
  subgraph Clients["入口"]
    WebFE["自作Web FE"]
    Discord["Discord Bot"]
    Mobile["スマホ（ブラウザ）"]
    CLI["CLI / テストclient"]
  end

  subgraph Adapters["入口Adapter（認証・呼出主体・会話対応・能力宣言）"]
    TextAd["Text adapter"]
    VoiceAd["Voice adapter"]
  end

  Life["Character Life（活動型の起点）"]

  WebFE <--> TextAd
  WebFE <--> VoiceAd
  Discord <--> TextAd
  Mobile <--> TextAd
  Mobile <--> VoiceAd
  CLI <--> TextAd

  VoiceAd <--> STT["STT provider"]
  VoiceAd <--> TTS["TTS provider"]

  TextAd <-->|"入力 / delta・終端・取消"| Core["共通Core（人格コア）"]
  VoiceAd <-->|"確定入力・割込 / delta・実再生結果"| Core
  Life --> Core

  Core <--> State["Card・履歴・記憶（SQLite正本 / Chroma派生）"]
  Core --> Runner["推論Runner port"]
  Core --> Gate["Execution Gate"]
  Gate -.->|"承認要求"| Adapters
  Gate --> MCP["MCP / 外部サービス（ELYTH等）"]
  Core -.->|"自発発話・通知"| Adapters
```

- Voice adapterは、現行のLiveKit transport＋自前の音声制御から、将来LiveKit Agents等へ差し替えられる。
  差替え時にCoreを変更しないため、Coreが受け取るのは確定した入力・割込・取消・実再生済み範囲に限り、
  発話区間検出・ターン判断・再生キューはVoice adapter側が所有する。#3ではLiveKit Agentsを導入しない。
- 推論Runner portは既存Inference Routerを包み、#422でPydantic AIへ差し替える。
- Discordのボイスチャットに対応する場合は2つ目のVoice adapterとし、STT／TTS providerを共有する。

## 2. 入口の分類

| 種類 | 例 | 起点 | 出力先 |
|---|---|---|---|
| 対話型 | Web、Discord、スマホ、CLI | 相手の入力 | 入力元の会話 |
| 活動型 | ELYTHの巡回・投稿・返信 | Character Life | Execution Gate経由の外部サービス |

活動型は会話の配送先ではない。Character LifeがCoreを呼び、外部への作用は必ずExecution Gateを通す。
自律runtimeからMCPを直接呼ばない（Character Life共通契約§11）。

## 3. 呼出主体

Coreへの呼出は次の主体を持つ。

```text
actor = { kind: owner | other | self, platform, external_id? }
```

- `owner`：認証済みのユーザー。Discord等では設定したオーナーIDだけを`owner`とする。
- `other`：それ以外の人間・AI。非信頼入力として扱い、prompt上でも区別する。`other`の発言から形成する記憶の範囲は#516で決める。
- `self`：活動型の呼出でキャラクター自身が起点の場合。
- MVPでは`external_id`を使った個別識別を行わないが、記録できる形にして後からのデータ移行を不要にする。
  個別の相手との関係はCharacter Life共通契約§7のRelationship Stateへ接続する。

## 4. 公開範囲

- 記憶・履歴は`public`／`private`、Coreへの呼出は出力先の公開範囲を持つ。
- 出力先が`public`なら`public`の記憶だけをpromptへ入れる。`private`なら両方を使える。LLMへの指示には頼らない。
- 派生する記憶（Semantic・Reflection・人格変化）は、根拠のうち最も厳しい範囲を引き継ぐ。
- **現行運用**：記憶は長期記憶保存時の機微情報マスクを通しているため、全て`public`とする。
  #3・#516・#517の契約には公開範囲の欄を予約し、`private`の付与・絞り込みは#518で実装する。

## 5. 自律的なSNS参加の制御

利用開始の承認は、該当connectionをAutonomy Targetとして許可する操作とする（Character Life共通契約§8）。
会話中の閲覧限定（#241）と自律活動での許可は別設定であり、会話中の制限を緩めない。
許可後の個々の投稿・返信は自律実行し、次で制御する。

1. 公開の場への出力では`public`の記憶だけを使う（§4）。
2. 送信直前のEgress Privacy Check。secretは常にBLOCK（同§9）。
3. Execution Gateの書き込み許可、回数上限・予算、監査ログ、緊急停止（pause・disable・revoke）。
4. High Impact操作は#185のConfirmationPolicyへ委譲（同§10）。
5. 結果不明の送信を未実行へ変換して再送しない（同§11）。

## 6. 不変条件

- 人格・記憶・履歴・権限・推論設定の正本は一つ。入口・SDK・FEの内部状態を第二の正本にしない。
- 共通CoreはFE・LiveKit・Discord・SNS固有の型へ依存しない。
- 外部への作用はExecution Gateを通す。入口Adapterや活動runtimeが独自の権限判断・Tool loopを持たない。
- 既存のprivacy・Egress・High Impact・結果回復の契約を、入口の追加を理由に緩和しない。

## 7. 対象外・未決事項

- 対象外：スマホのネイティブアプリ（当面は#514のブラウザ経由）、一般公開API、複数オーナー、3段階以上の公開範囲。
- 未決（#516）：Discord Adapterの配置（同一プロセスか認証付きAPIか）、承認UIのない入口で承認が必要な操作の扱い、複数入口からの同時会話。
- 未決（#517）：対象操作の範囲、起動条件と頻度上限、他者の投稿から形成する記憶の範囲。
- 未決（#518）：非公開の入力の指定方法、要約を通じた漏えいの扱い。

## 用語

用語と実装名の対応は[用語集](../glossary.md)の「複数入口・公開範囲」を参照する。
用語集の`Character Core`はCardから組み立てるprompt領域を指す既存語であり、本ADRの「共通Core（人格コア）」とは別である。
