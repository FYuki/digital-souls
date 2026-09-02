# CCV3 Character Bookのruntime解釈契約（2026-08）

状態: **ACTIVE**。

本ADRは、CCV3 `data.character_book`を静的なCharacter Loreとしてruntimeで解釈する境界、
matching、selection、prompt挿入、token accountingを定める。
Character Card全体と既存prompt合成の基本契約は
`character-card-v3-prompt-builder-2026-07.md`を引き続き適用し、本ADRと競合する
Character Loreの追加順序と削減順だけは本ADRを優先する。

## 背景

`description`、`personality`、`scenario`、`system_prompt`、`mes_example`はCharacter Coreとして
常にpromptへ入る。世界観や由来等の静的知識までCoreへ含めると、通常の実用回答にも世界観表現が
常時影響しやすい。

runtime人格定義のSource of Truthを`characters/{id}/{id}.card.json`のまま維持しつつ、
CCV3標準の`data.character_book`に定義したCharacter Loreだけを会話内容に応じて条件付きで
投入する。`personality.md`や`world.md`等の設計資料はruntimeから直接読まない。

Character LoreはCharacter Cardに定義された静的知識であり、会話や経験から形成するRAG長期記憶とは
別責務とする。Character LoreをChromaへ登録せず、RAG検索結果としても扱わない。

## 対応範囲

MVPではembedded Character Bookである`data.character_book`だけを対象とする。
standaloneの`spec: lorebook_v3` import/exportは対象外とする。

### Bookモデル

| CCV3 field | 内部型 | 必須 | runtime既定・制約 |
|---|---|---|---|
| `name` | `str \| None` | いいえ | promptへ入れない |
| `description` | `str \| None` | いいえ | promptへ入れない |
| `scan_depth` | `int \| None` | いいえ | 非負整数。省略時は`1`として解釈する |
| `token_budget` | `int \| None` | いいえ | 非負整数。省略時はBook固有上限なし |
| `recursive_scanning` | `bool \| None` | いいえ | 保持する。MVPでは省略、`false`、`true`のいずれでも再帰scanしない |
| `extensions` | JSON object | はい | 意味を変えずに保持する |
| `entries` | Entryの配列 | はい | 空配列を許可する |
| 未知field | JSON object内の値 | - | `extra_fields`へ保持する |

`character_book`自体がない場合は内部表現を`None`とし、Character Core、RAG、履歴、
`post_history_instructions`、現在発言の既存挙動を変えない。

### Entryモデル

| CCV3 field | 内部型 | 必須 | runtime既定・制約 |
|---|---|---|---|
| `keys` | `tuple[str, ...]` | はい | 空配列を許可する。空文字keyはmatchしない |
| `content` | `str` | はい | 空白除去後に空ならpromptへ入れない |
| `extensions` | JSON object | はい | 意味を変えずに保持する |
| `enabled` | `bool` | はい | `false`は常に非match |
| `insertion_order` | `int` | はい | signed integer。prompt配置順とpriority省略時の保持優先度に使う |
| `use_regex` | `bool` | はい | `false`のみMVP対応。`true`は保持するが非選択 |
| `case_sensitive` | `bool \| None` | いいえ | 省略時は`false` |
| `constant` | `bool \| None` | いいえ | 省略時は`false`。`true`はkeyに依存せずmatchする |
| `name` | `str \| None` | いいえ | promptとlogへ入れない |
| `priority` | `int \| None` | いいえ | signed integer。低い値からbudgetで除外する |
| `id` | `int \| str \| None` | いいえ | boolを整数として受理しない。promptとlogへ入れない |
| `comment` | `str \| None` | いいえ | promptへ入れない |
| `selective` | `bool \| None` | いいえ | 省略時は`false` |
| `secondary_keys` | `tuple[str, ...] \| None` | いいえ | `selective=true`時だけ利用する |
| `position` | `before_char \| after_char \| None` | いいえ | 省略時は`after_char` |
| 未知field | JSON object内の値 | - | `extra_fields`へ保持する |

CCV3が`number`と表現する順序・budget関連fieldは、runtimeのmessage数・token数と決定論的な
比較へ利用するため整数だけを受理する。Pythonの`bool`は`int`のsubclassだが、数値fieldでは
明示的に拒否する。`scan_depth`と`token_budget`は負数を拒否し、`0`を許可する。

## validationと互換性

既知fieldの欠落、型不正、制約違反、未知の`position`値は、カード内のpathを含む
`CharacterCardValidationError`でカード全体をrejectする。例えばEntry 2の`keys`不正は
`data.character_book.entries[2].keys`をエラーから識別可能にする。不正Entryだけを黙ってskipしない。

一方、次を理由にカード全体をrejectしない。

- BookまたはEntryの未知field
- `extensions`内の未知field
- 正規CCV3値だがMVP非対応である`use_regex=true`
- `recursive_scanning=true`
- Decoratorを含む`content`

未知fieldはBookとEntryそれぞれの`extra_fields`へlosslessに保持する。MVP非対応機能も型付きfieldまたは
raw `content`として保持し、runtime上の採用可否とデータ保持を分ける。

## scan context

scan対象は、現在のuser原文と、同一`character_id`・`conversation_id`に属するprompt投入可能な
privacy処理済み履歴だけとする。RAG本文、Character Core、過去に選択したCharacter Lore、
別conversationの履歴はscanしない。

messageは新しい順に次のように並べる。

1. 現在のuser発言
2. 直前turnのassistant発言
3. 直前turnのuser発言
4. さらに前のturnを同様にassistant、userの順

`scan_depth`はturn数ではなくuser/assistant個別のmessage数である。現在user発言を1 messageとして
数える。`scan_depth=0`はmessageをscanしない。省略時の`1`では現在user発言だけをscanする。
履歴は`MaskedHistory`と同じ保存済みsanitized本文を使い、保存されていない原文を再取得しない。
利用可能なprompt履歴が`scan_depth`より少ない場合は、利用可能な範囲だけをscanする。

## literal matching

文字列は次の順で比較用viewへ変換する。原文とkey自体は変更・上書きしない。

1. messageとkeyへUnicode NFKC正規化を適用する
2. `case_sensitive=false`の場合だけ双方へUnicode `casefold()`を適用する
3. 各message内でliteral substring検索する

messageは個別に検索し、隣接messageを連結して境界をまたぐmatchを作らない。改行を含む同一messageは
そのまま1つの文字列として検索する。空文字keyは、すべての文字列に含まれるものとして扱わず、
常に非matchとする。

### Entry activation

判定優先順は次のとおりとする。

1. `enabled=false`なら非選択
2. `content.strip()`が空なら非選択
3. Decoratorを含むならMVP非対応として非選択
4. `use_regex=true`ならMVP非対応として非選択
5. `constant=true`なら選択候補
6. `keys`のいずれかがscan contextへmatchしなければ非選択
7. `selective=true`の場合、さらに`secondary_keys`のいずれかがmatchした場合だけ選択候補
8. `selective=false`または省略時はprimary key matchだけで選択候補

`selective=true`かつ`secondary_keys`が欠落、空配列、または空文字だけの場合は非matchとする。
`use_regex=true`ではCCV3上`constant`が無視されるため、`constant=true`との併用でも選択しない。

同一Entryで複数key、複数messageがmatchしても、カード内の配列indexをEntry identityとして1件へ
dedupeする。異なるEntryが同じ`id`を持っていても統合しない。

### MVP非対応機能

`use_regex=true`は、timeoutを持たない正規表現実行による応答遅延を避けるためMVPでは評価しない。
カードとEntryはloadできるが、該当Entryをmatchさせない。

`recursive_scanning=true`は保持するが、選択済みEntryの`content`を新しいscan sourceとして使わない。
初期のliteral/constant matchingは通常どおり行う。

Decoratorは解釈もpromptへのraw投入も行わない。`content`内に、行頭の空白を除いた後で`@@`または
`@@@`から始まる行が1行でもあれば、そのEntryをMVP非対応として非選択にする。これにより未解釈の
制御文字列がLLMへ渡ることを防ぐ。raw `content`はdomain modelへ保持する。

## selectionとBook token budget

matchingを通過した全Entryを候補とし、Bookの`token_budget`を次のように適用する。

- Lore本文を途中で切断しない
- Entryごとに独立した`system` messageとしてrenderする
- message本文は`## キャラクターLore\n{content.strip()}`とする
- `name`、`id`、`comment`、keys、match理由はpromptへ含めない
- `before_char`と`after_char`を合わせた全Lore messageを、productionと同じ`TokenCounter`で計測する
- `token_budget`省略時はBook固有の削減を行わない
- `token_budget=0`では全候補を除外する

上限を超える間、次の除外keyが小さいEntryを1件ずつ除外し、残った集合を再計測する。

```text
effective_priority = priority if priority is present else insertion_order
removal_key = (effective_priority, insertion_order, source_index)
```

したがって、低priorityから除外し、同priorityでは低い`insertion_order`、さらにカード配列の前方を
先に除外する。priorityはbudget超過時の保持優先度だけに使い、prompt配置順には使わない。

budget適用後のEntryはpositionごとに分け、各position内を次のkeyで昇順に並べる。

```text
prompt_order_key = (insertion_order, source_index)
```

## selector出力と診断metadata

selectorは、PromptBuilderが本文を利用する型付きの選択結果と、本文を含まない診断metadataを返す。

選択Entryには少なくとも次を保持する。

- source index
- content
- resolved position
- insertion order
- effective priority
- activation kind（`constant`、`primary`、`selective`）
- token budgetで再除外するための決定論的removal key

application logや診断metadataへEntry本文、keys、secondary keys、regex pattern、`name`、`id`、
`comment`を記録しない。記録可能なのはsource index、reason code、position、token数、選択・省略件数とする。

reason codeは少なくとも次を区別できる境界を用意する。

- disabled
- empty content
- unsupported decorator
- unsupported regex
- primary key miss
- secondary key miss
- selected constant
- selected primary
- selected selective
- omitted by lore budget
- omitted by total budget

## prompt合成とRAG境界

PromptBuilderだけが最終message列を作り、順序を次へ固定する。

```text
before_char Character Lore
Character Core
after_char Character Lore
RAG required instruction / RAG Memory
Conversation History
post_history_instructions
Current User
```

`position`省略時は`after_char`とする。Loreが0件の場合はLore messageを1件も生成せず、
`character_book`がない既存カードと同じmessage列にする。

Character Loreは`RagContext`や`RagItem`へ変換せず、`PromptBuildInput`、`TokenBudget`、
`PromptUsage`へ独立した`character_lore`領域を持たせる。`PromptUsage`にはLore token数と
省略Entry数を含める。RAGの`memory_reference`をLoreへ付けない。

Book固有budgetを通過したLoreがPromptBuilderの`character_lore`個別上限を超える場合も、同じ
removal keyでEntry単位に削減する。通常のruntimeでは個別上限を入力context上限とし、Bookの
`token_budget`を実質的なLore固有上限とする。

### total budget

Character Core、現在user発言、直前のcompleted 1往復、既存のRAG required instructionを必須領域とする。
全体上限を超えた場合、任意領域を次の順で削減する。

1. RAG Memory item
2. 古いConversation History
3. Character Loreをremoval key順にEntry単位で削減
4. `post_history_instructions`

全任意領域を除外しても必須領域が全体上限を超える場合は、従来どおり
`PromptInputLimitError(region="total")`を返す。Character CoreとLoreを同じ必須領域へまとめない。

## runtime wiring

現行のCharacter loader境界は`CharacterPrompt`だけを返すため、runtime contextをCharacter Coreと
Character Bookの両方を持つ型へ拡張する。ChatServiceは同じCharacter Card load結果からCoreとBookを
取得し、RAGとは独立にLore selectorを呼ぶ。

HTTPと移行前WebSocketは共通ChatServiceを通るため、この共有runtime境界へ一度だけ統合する。
transport固有routerでmatchingやprompt挿入を行わない。LiveKit transportもConversation Coreの
共通応答生成へ接続する際に同じ境界を利用し、transport内へLore責務を持たせない。

selectorへ渡す履歴とPromptBuilderへ渡す履歴は同じprivacy処理済みsourceから生成する。
selectorの`scan_depth`適用は、PromptBuilderがhistory token budgetで古い履歴を落とす前に行い、
Lore matchingをRAG有効化やprompt全体budgetへ依存させない。

## fixtureとテスト境界

光織固有の本文や応答品質へ依存しない合成Character Card fixtureを使用する。fixtureは少なくとも
次を含む。

- Character Bookなし
- 空Book
- literal match / miss
- NFKC、case sensitive / insensitive、message境界
- disabled、constant、selective hit / miss
- scan depth 0、1、複数message、利用可能履歴より大きい値
- 複数keyによる同一Entry matchのdedupe
- priority、insertion order、source indexのtie-break
- Lore budget 0、境界一致、1 Entry超過、複数Entry超過
- before / after / position省略
- unsupported regex、recursive scanning、Decorator
- malformed既知fieldとpath付きerror
- Book / Entryの未知field保持
- Lore + RAG + history + post-history
- total budget削減順
- HTTP / WebSocketの共通runtime経路

loader、normalization、matching、selection、PromptBuilderの決定論的処理は`backend/tests/unit/`で検証する。
loaderから共通ChatService、PromptBuilderまでの横断挙動は`backend/tests/module/`の結合テストで検証する。
外部サービスへ接続しない横断テストをインテグレーションテストとは呼ばない。

unit/module testでは本文をapplication logへ出さず、fake token counterで選択と境界値を固定する。
本機構だけを理由にOllamaやChromaへ実接続するテストは追加しない。

## 非スコープ

- 光織固有のCharacter Core、Lore本文、keys、budgetのチューニング
- `personality.md`や`world.md`のruntime参照
- MarkdownからCCV3への自動変換
- regex matching
- recursive scanning
- Decorator解釈
- semantic matching、embedding、ChromaによるLore検索
- Character LoreのRAG登録
- キャラクター間で共有する動的World State
- Reflectionからの人格成長

## 関連

- GitHub Epic #117
- GitHub Issue #118〜#122
- `docs/decisions/character-card-v3-prompt-builder-2026-07.md`
- `docs/decisions/wave2-memory-formation-retrieval-2026-08.md`
