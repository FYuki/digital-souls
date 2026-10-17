# ADR 0001〜0012 と PoC の決定の照合一覧

日付: 2026-10-11

対象: [Issue #151](https://github.com/FYuki/digital-souls-core/issues/151) / [Epic #150](https://github.com/FYuki/digital-souls-core/issues/150)

起点: `origin/epic/poc-adr-realignment` / `5b9ea26bcd33f0b7eca9b1420610112583b4dbe2`

PoC: 公開 `FYuki/digital-souls-poc` / `fce7382884d981c42be7fbd3ddaffe7469e27588` の `docs/decisions` 直下（39件、すべて ACTIVE）。
以下、PoC の文書は `PoC:<ファイル名>` と書き、リンクは同 revision の固定 URL です。

この文書は照合の記録です。分類 (a)〜(c) と子 Issue への対応付けは提案であり、ユーザーの判断が必要な項目は
「判断が必要な項目」に分けました。ADR の本文はこの作業では変更していません。

## 分類

- **(a)** PoC を正として直す。
- **(b)** Core でユーザーが決めた変更なので維持する。
- **(c)** Core の範囲外。

「ずれなし」は、照合した範囲で PoC と矛盾しないものです。

## 対象外にした PoC の決定と理由

README の「製品の範囲」は、音声・STT・TTS・LiveKit・UI を Core の対象外としています。
Core は入口の音声制御・画面を持たず、確定したテキスト入力だけを受け取るためです。
PoC 側でも [複数入口 ADR §1](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/multi-entry-character-core-2026-09.md#1-最終構成)
で、発話区間の検出・ターン判断・再生キューを Voice adapter 側の所有としています。

| PoC の決定 | 分類 | 理由 |
| --- | --- | --- |
| voice-backend-authority、voice-playback-estimation-speech-services、voice-quality-improvement、voice-session-contract、voice-session-preparation、tts-engine-reference-voice | (c) | 音声・STT・TTS。Core の範囲外 |
| livekit-transport、livekit-sdk-reconnect-build | (c) | LiveKit。Core の範囲外 |
| conversation-session-client、conversation-session-text-input、browser-screen-perception | (c) | FE・クライアントの入力と画面知覚。Core は確定入力だけを受け取る |
| docker-policy、local-dogfood-environment | (c) | PoC の配備・dogfood の方針。Core の Docker 利用（PostgreSQL・llama.cpp）は Core の文書で個別に定める |
| documentation-governance | (c) | PoC の文書運用。Core は CONTRIBUTING が正本 |
| post-mvp-enhancement | (c) | PoC の Wave 計画。Core の進め方は Issue で管理する |
| character-miori、miori-personality-without-fixed-role | (c) | キャラクター本文の調整。Core 同梱の `characters/miori/miori.card.json` は PoC のものと同一（`cmp` で確認） |
| wave2-memory-formation-retrieval、rag-memory-privacy-policy、episode-fact-semantic-boundaries、semantic-memory-lifecycle、memory-personality-terminology | 範囲外（移設済み） | ADR 0015〜0022 で移設済み。Epic #150 の範囲外（記憶モデルの再見直しはしない） |

残りの決定（Card・Book、会話と活動、Character Life、Tool、複数入口、Core 呼出、推論基盤、目標構成、Addon・通知）は
次の一覧で照合しました。

## ずれの一覧

| ID | ずれ | PoC の根拠 | Core の該当箇所 | 分類 | 扱う Issue |
| --- | --- | --- | --- | --- | --- |
| P1 | 呼び出し側の system を人格の後ろにそのまま足す。PoC では PromptBuilder だけがメッセージを組み立てる | [card-v3 §合成順序と入力境界](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-card-v3-prompt-builder-2026-07.md#合成順序と入力境界)、[複数入口 §6](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/multi-entry-character-core-2026-09.md#6-不変条件) | [api.md「対応する小範囲」](../api.md#対応する小範囲)、[application.py](../../core/src/digital_souls_core/application.py) の `prepare`（人格 system → 記憶 → 呼び出し側の messages をそのまま連結）、[ADR 0001](../adr/0001-character-inference-api.md)。会話経路は system を拒否する（[conversations.py](../../core/src/digital_souls_core/conversations.py) の role 検査） | (a) | #152（判断が必要：J1） |
| P1' | Lore の照合と記憶検索は、末尾の `role=user` を今回の発言とみなす。OpenClaw の内部文脈が最後の user になると、それで照合・検索する | 同上（`CurrentUserMessage` を別の型で受け取る） | [application.py](../../core/src/digital_souls_core/application.py) の `user_text`、[OpenClaw 連携検討「確認できたことと制約」](../openclaw-integration-design.md#確認できたことと制約) | (a) | #152（J1） |
| P2 | 組み立ての順序。Core は Card・Lore・記憶の指示を1つの system にまとめ、Runtime Context の枠がない。`post_history_instructions` は起動時に拒否する | [card-v3 §合成順序と入力境界](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-card-v3-prompt-builder-2026-07.md#合成順序と入力境界)、[Book §prompt合成とRAG境界](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-book-runtime-2026-08.md#prompt合成とrag境界)、[会話と活動 §2](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/conversation-activity-agents-2026-09.md#2-共有する正本)（Life State を応答準備時に読む） | [character.py](../../core/src/digital_souls_core/character.py) の `load_characters`、[application.py](../../core/src/digital_souls_core/application.py) の `prepare` | (a) | #152 |
| P2' | Card の書式。Core は `name` を含め、各 field を `key: value` で連結する。PoC は `description` から `mes_example` までを決まった順に置き、空の要素は見出しごと省く | [card-v3 §Character Cardフィールド](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-card-v3-prompt-builder-2026-07.md#character-cardフィールド) | [character.py](../../core/src/digital_souls_core/character.py) の `fields` | (a) | #152 |
| P3 | 領域ごとの token 予算。Core は注入する system のバイト予算だけで、履歴は削らず 413 で拒否する | [card-v3 §token budget](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-card-v3-prompt-builder-2026-07.md#token-budget)、[Book §total budget](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-book-runtime-2026-08.md#total-budget) | [ADR 0001](../adr/0001-character-inference-api.md)、[ADR 0004「制約と検証」](../adr/0004-conversation-history.md#制約と検証)、[api.md「文脈・プライバシー」](../api.md#文脈プライバシー)、`context_budget_bytes`（[character.py](../../core/src/digital_souls_core/character.py)） | (a) | #152 |
| P3' | 推論入力の件数上限（256件）の撤廃 | PoC は件数でなく token 予算で削る | ADR 0004 の置換注記（#135） | (b)・ずれなし | -（件数上限の撤廃は P3 と矛盾しない。履歴 1 MiB の上限は P3 で再検討） |
| P4 | 人格の一貫性の評価がない | [会話と活動 §6](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/conversation-activity-agents-2026-09.md#6-ペルソナ一貫性の評価)、[Character Life §6.2](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-life-memory-personality-autonomy-2026-09.md#62-egogram)、card-v3「決定」（Egogram は表出一貫性の質的評価だけ） | [ADR 0020](../adr/0020-reflection-personality-relationship.md)（Egogram の位置付けは一致）、[ADR 0023](../adr/0023-semantic-evaluation-contract.md)（記憶検索・回答の評価だけ） | (a) | #154（判断が必要：J3） |
| P4' | 応答ごとに使った Card のハッシュを記録しない。PoC は評価・調査で応答と Card を対応付けるために記録する | [会話と活動 §5](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/conversation-activity-agents-2026-09.md#5-character-cardの参照) | [api.md「入り口」](../api.md#入り口)（`X-Character-Config-Version` だけ） | (a) | #154 |
| P5 | Character Book。Core は最新の発言との key 一致だけで、`constant`、`case_sensitive`、`before_char`、`scan_depth`≠1 を起動時に拒否する。`token_budget` と `priority` を使わない | [Book ADR](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-book-runtime-2026-08.md) 全体 | [character.py](../../core/src/digital_souls_core/character.py) の `load_characters`、[api.md「文脈・プライバシー」](../api.md#文脈プライバシー) | (a) | #153 |
| A1 | 会話中のツール判断・Execution Gate・ツール一覧を Core が持たない。Core は「tool 実行ループを持たない」とする | [Tool 利用 §責務・§loop](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/tool-use-foundation-2026-09.md#責務)、[複数入口 §1](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/multi-entry-character-core-2026-09.md#1-最終構成)（推論 Runner を Pydantic AI へ差し替え、#422） | [ADR 0001](../adr/0001-character-inference-api.md)、[ADR 0002「影響」](../adr/0002-ollama-native-chat.md#影響)、[ADR 0003](../adr/0003-local-llamacpp-provider.md)、[ADR 0004「背景」](../adr/0004-conversation-history.md#背景)、[api.md](../api.md) 冒頭、[README「製品の範囲」](../../README.md#製品の範囲)、[ADR 0025「決定」](../adr/0025-shared-runtime-interfaces.md#決定)（Accepted） | (a)。ただし ADR 0025 は (b) と衝突 | #155（判断が必要：J4） |
| A2 | 会話外の活動の実行基盤。PoC は DBOS を Core に組み込み、判断・操作の中身も Core が持つ。Core は実行基盤と判断の一部（Autonomy Target、High Impact の確認、結果の状態）を PrivateAgent に任せる | [Life runtime 選定](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-life-runtime-2026-09.md)、[Character Life §8〜§13](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-life-memory-personality-autonomy-2026-09.md#13-framework-neutral-runtime-boundary)、[会話と活動 §1〜§3](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/conversation-activity-agents-2026-09.md#1-二つのエージェントに分ける) | [ADR 0017 §4](../adr/0017-memory-formation-admission.md#4-会話からの非同期形成)（Accepted）、[ADR 0020 §6・§7](../adr/0020-reflection-personality-relationship.md#6-会話外の活動との境界)（Accepted）、[README「製品の範囲」](../../README.md#製品の範囲)、[CONTEXT](../../CONTEXT.md) の PrivateAgent | (a)。R3・R5・R6 で置き換える。Accepted の ADR と衝突 | #155（判断が必要：J4） |
| A2' | 活動から会話への報告経路。PoC は活動側が会話へ直接話さず、通知・報告の経路で会話エージェントへ渡す | [会話と活動 §3](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/conversation-activity-agents-2026-09.md#3-活動からの報告)、[通知と会話の分離](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/notification-conversation-separation-2026-09.md) | 該当なし（未定義） | (a) | #155（ADR で境界だけ定める） |
| A2'' | Core を Agent から呼ぶ MCP。PoC は「Core 側に MCP サーバー等の汎用の受け口は置かない」とする。R5 は活動の Agent が使うツールを Core の MCP に限定する | [目標構成 §5](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/companion-target-architecture-2026-09.md#5-外部から呼ばれる入口) | [ADR 0025](../adr/0025-shared-runtime-interfaces.md)（MCP 等の入口は別作業）、Epic #150 R5 | (b)（2026-10-11 のユーザー決定 R5 が PoC に優先） | #155 |
| A3 | 複数入口の公開範囲（public / private）と相手の識別（owner / other / self） | [複数入口 §3・§4](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/multi-entry-character-core-2026-09.md#3-呼出主体) | Binding（subject・client・audience・character）のみ。[ADR 0004](../adr/0004-conversation-history.md) | (a) | #77（Epic #150 の範囲外） |
| D1 | 推論先の選び方。PoC は用途別の Target（chat、privacy、memory-extraction、embedding、任意の tool-routing 等）から provider/model を解決する。Core はキャラクターごとの profile と、分類器・embedding の個別 profile | [推論基盤 §1・§2](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/inference-provider-foundation-2026-09.md#1-targetからprovidermodelを直接解決する)、[Tool 利用 §推論と候補](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/tool-use-foundation-2026-09.md#推論と候補) | [ADR 0001](../adr/0001-character-inference-api.md)、[ADR 0003](../adr/0003-local-llamacpp-provider.md)、[ADR 0005](../adr/0005-privacy-boundaries.md)、[ADR 0011](../adr/0011-local-memory-embedding.md) | 迷う（J5） | #155（tool-routing の推論先だけ） |
| D2 | Card の反映時期。PoC は最新の Card を参照し、Session の再開時や Card の更新時に反映する。Core は起動時に一度読み、再起動まで変えない | [会話と活動 §5](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/conversation-activity-agents-2026-09.md#5-character-cardの参照) | [api.md「入り口」](../api.md#入り口)、[ADR 0001](../adr/0001-character-inference-api.md)（immutable snapshot） | 迷う（J6） | - |
| D3 | 記憶 context の置き場所。PoC は Runtime Context の後・履歴の前に置く（RAG required instruction と RAG Memory）。Core は人格の system とは別の user メッセージとして、呼び出し側の messages の前に置く | [Book §prompt合成とRAG境界](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-book-runtime-2026-08.md#prompt合成とrag境界) | [ADR 0007「レビューによる再構築と送信境界の補強」](../adr/0007-memory-provenance-and-revocation.md#レビューによる再構築と送信境界の補強)、[ADR 0020 §6](../adr/0020-reflection-personality-relationship.md#6-会話外の活動との境界)（インジェクション対策は Core が所有、Accepted） | 順序は (a)、データとして扱う枠は (b)（J2） | #152 |

## 照合した Core の決定（ADR ごと）

| ADR | 照合結果 |
| --- | --- |
| 0001 推論 API | P1・P2・P3・P5・A1・D1・D2。Python・FastAPI・LiteLLM の選定はユーザーが了承済みで、仕組みはライブラリを使う方針（R2）とも合うため維持 |
| 0002 Ollama native Chat | A1（「Core は tool 実行を追加しない」）。Ollama 経路の制約はずれなし |
| 0003 llama.cpp | A1（「tools の実行ループは追加しない」）。推論先を llama.cpp にしたのはユーザー決定で (b)（PoC は ollama-model で Ollama）。Docker の設定は (c) |
| 0004 会話履歴 | P3（削らずに拒否）、A1（tool 実行ループを追加しない）。会話経路での system 拒否は P1 の方向と一致。Binding・保存の契約は (b) |
| 0005 privacy | ずれなし。分類器をローカルに限る点は PoC（推論基盤 §7、目標構成 §3）と一致。permission の分け方と Binding は (b) |
| 0006 操作境界 | (b)。ユーザーが指定した操作表 |
| 0007 出典と撤回 | 出典 epoch・撤回は PoC の失効契約と一致。記憶 context をデータとして扱う枠は D3 |
| 0008 構造化出力 | ずれなし（PoC の structured generation と同じ考え方） |
| 0009 一時参照名 | ずれなし |
| 0010・0011 意味検索 | 順位は PoC 互換に改訂済み。Chroma の永続 index の代わりに呼び出し中だけの vector を使うのは、PostgreSQL への一本化（0021）に沿うので (b) |
| 0012 PostgreSQL | (b)。PoC の SQLite＋Chroma に代えて PostgreSQL へ一本化（ADR 0021、Accepted） |

README の「製品の範囲」、api.md、CONTEXT（PrivateAgent）は A1・A2 に該当します。
SPEC には、プロンプトの組み立て・Character Book・人格の一貫性の評価の行がありません。#152〜#154 で追加します。

## 子 Issue への対応付け

| Issue | 扱う項目 |
| --- | --- |
| #152 プロンプトの組み立て | P1、P1'、P2、P2'、P3、D3 |
| #153 Character Book | P5 |
| #154 人格の一貫性の評価 | P4、P4' |
| #155 ツールの利用と実行基盤の ADR | A1、A2、A2'、A2''、D1（tool-routing の推論先） |
| #77（Epic 外） | A3 |

## 判断が必要な項目

| ID | 論点 | 選択肢と推奨 |
| --- | --- | --- |
| J1 | 呼び出し側の system と OpenClaw の内部文脈（P1・P1'） | #152 の着手前に、選択肢と推奨をまとめて確認する |
| J2 | 記憶 context の扱い（D3） | 推奨：位置は PoC の順序（Runtime Context の後・履歴の前）に合わせ、命令ではなくデータとして渡す枠（ADR 0007・0020）は維持する |
| J3 | 人格の一貫性の評価方式と合格基準（P4） | #154 の着手前に確認する |
| J4 | A1・A2 と Accepted の ADR の衝突。ADR 0025「外部 Agent が tool 実行を担当する」、ADR 0017 §4・ADR 0020 §6・§7「PrivateAgent が実行し、実行時の契約を所有する」は、R3〜R6 と一部矛盾する | 推奨：#155 の新しい ADR（Proposed）で該当部分の置換を提案し、Accepted になった時点で各 ADR に置換注記を付ける。Accepted の ADR の置換なので、ユーザーの承認を条件とする |
| J5 | 推論先の選び方（D1） | 推奨：Core の profile 方式を維持する（仕組みの部分で、本人らしさに直接効かない）。会話中のツール判断に使う推論先だけを #155 で定める |
| J6 | Card の反映時期（D2） | 推奨：起動時の snapshot を維持し、P4' のハッシュ記録で応答と Card を対応付ける。再起動なしでの反映は必要になってから扱う |
