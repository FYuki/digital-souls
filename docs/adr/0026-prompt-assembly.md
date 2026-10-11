# ADR 0026: プロンプトの組み立てと領域ごとの token 予算

Status: Proposed

日付: 2026-10-11

関連: [Epic #150](https://github.com/FYuki/digital-souls-core/issues/150)、[Issue #152](https://github.com/FYuki/digital-souls-core/issues/152)、
[照合一覧](../evidence/2026-10-11-poc-adr-alignment.md)の P1・P2・P2'・P3・D3

## 背景

2026-10-11 にユーザーは、プロンプトの組み立てを PoC（公開 `FYuki/digital-souls`、
revision `fce7382884d981c42be7fbd3ddaffe7469e27588`）の決定を正として直すと決めました（Epic #150 R1）。
正とする PoC の決定は次の2件です。

- [Character Card V3 と prompt 合成境界](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-card-v3-prompt-builder-2026-07.md)
- [CCV3 Character Book の runtime 解釈契約](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-book-runtime-2026-08.md)（順序と削減順の部分。Book の解釈は別の ADR）

決定時点（epic `731bf9b`）の Core は、次のように組み立てていました。

- Card の各 field を `name: ...` の形で連結し、Lore・記憶の指示と合わせて1つの system にする。
- 記憶は別の user メッセージとして、呼び出し側の messages の前に置く。
- 呼び出し側（stateless の Chat API）の system は、人格の後ろにそのまま残る。会話履歴 API は system を拒否する。
- `post_history_instructions` があるカードは起動時に拒否する。Character Runtime Context の枠はない。
- 予算は注入する system と記憶の UTF-8 バイト数（`context_budget_bytes`）だけ。履歴は削らず、
  provider の上限エラーか、履歴の 1 MiB 上限による 413 になる。

この形では、外部 Agent の指示が人格の直後に並んで人格を上書きしたり混ざったりします。
長い会話では、人格や今回の発言を守るために古い履歴を削ることもできません。

呼び出し側の system については、2026-10-11 にユーザーが次のとおり決めました（照合一覧の J1・J2）。

- 外部クライアント一般が送る system は「呼び出し側の指示」として扱い、人格より下位のデータ枠に入れる。
  人格と `post_history_instructions` を上書きできない位置に置き、予算が足りなければこの枠を削る。
- OpenClaw の内部文脈（最後の user メッセージへの注入）には個別に対応しない。
- 記憶は PoC の順序（Runtime Context の後、履歴の前）に置き、データとして渡す枠（ADR 0007・0020）は維持する。

同日、この ADR の確認事項にユーザーが次のとおり回答しました。

- 予算が足りないときは、記憶を下限（3件）まで削る → 呼び出し側の指示 → 記憶を1件まで削る → 古い履歴 → Lore →
  `post_history_instructions` の順に削る。記憶は最大5件（[ADR 0018](0018-memory-retrieval-context.md) の最大取得件数）。
- `context_budget_bytes` を領域ごとの token 予算に置き換え、古い設定が残っていたら起動時にエラーにする。
- LLM 側から token 数を取得できる推論先ではその値を使う。取得できない推論先ではバイト数から推定し、マージンを含める。

さらに同日、削る順番の細部をユーザーが次のとおり決めました（上の削る順番を置き換える）。

- 最後の記憶1件は、削っても収まらなければ 400 で拒否する（下の選択肢の A）。
- 直近の履歴は最後の記憶1件より優先する。履歴に下限を設け、直近 N 往復（既定3、設定値）を残す順番にする。
  往復は user の発言とその応答で、tool call と結果の組は分けない。
- 削る順番は、記憶を下限まで → 呼び出し側の指示 → 記憶を1件まで → 古い履歴を直近 N 往復まで → 最後の記憶1件 →
  直近 N 往復の履歴 → Lore → `post_history_instructions`。それでも収まらなければ領域名つきで 400。

## 選択肢

呼び出し側の system について：

| 案 | 判断 |
| --- | --- |
| A：拒否する（会話履歴 API と同じ） | 不採用。外部 Agent の目的やツールの説明が使えなくなる |
| B：「呼び出し側の指示」として人格より下位のデータ枠に入れる | 採用（ユーザー決定） |
| C：現状のまま人格の後ろに残す | 不採用。PoC の「PromptBuilder だけが組み立てる」に反し、人格を上書きし得る |

予算について：

| 案 | 判断 |
| --- | --- |
| バイト予算を維持し、履歴は拒否のまま | 不採用。PoC の領域ごとの予算と削減順に反する |
| 領域ごとの token 予算と削減順（PoC） | 採用 |

予算が足りないときの最後の記憶1件について：

| 案 | 判断 |
| --- | --- |
| A：ほかを削っても収まらなければ最後の記憶1件も削り、それでも収まらなければ 400 で拒否する | 採用（ユーザー決定） |
| B：最後の記憶1件は削らず、収まらなければ 400 で拒否する | 不採用。記憶は必ず残す部分ではなく、記憶1件のために応答できなくなる |

直近の履歴について：

| 案 | 判断 |
| --- | --- |
| 履歴に下限を設けず、古い順に直前の1往復まで削ってから最後の記憶1件を削る | 不採用。会話の流れより記憶を優先してしまう |
| 直近 N 往復（既定3）を下限とし、最後の記憶1件より後に削る | 採用（ユーザー決定）。直近の会話の流れを記憶より優先する |

## 決定

### 1. 組み立てるのは Core の PromptBuilder だけ

provider へ送る messages は、Core の PromptBuilder だけが作ります。入口（HTTP の Chat API・会話履歴 API、
今後の CLI・MCP）は入力を型に分けて渡すだけで、messages を並べ替えたり足したりしません。

入力は次の型に分けます。

| 入力 | 中身 |
| --- | --- |
| Character Card | 固定人格・世界観・応答方針と `post_history_instructions` |
| Character Lore | 選択済みの Lore（解釈は Character Book の ADR で決める） |
| Character Runtime Context | 人格（Big Five Aspects から作る自然言語）・関係・生活状態。応答を始める時点の snapshot |
| 記憶 context | 検索済みの記憶（[ADR 0018](0018-memory-retrieval-context.md)・[ADR 0022](0022-memory-retrieval-from-records.md)） |
| 呼び出し側の指示 | 呼び出し側が送った system メッセージ |
| 会話履歴 | 今回より前の user・assistant・tool メッセージ |
| 今回の入力 | 最後の user メッセージと、その後に続く assistant の tool call と tool の結果 |

stateless の Chat API では、呼び出し側の messages からこれらを分けます。system はどこにあっても
呼び出し側の指示へ移し、元の順序を保って連結します。最後の user メッセージより前を会話履歴とします。
会話履歴 API では、保存済みの履歴と今回追加した messages から同じ型を作ります。会話履歴 API が system を拒否する扱いは変えません。

### 2. 順序

概念上の順序を次のとおりとします。

1. `before_char` の Character Lore
2. Character Card の固定人格（`description`、`personality`、`scenario`、`system_prompt`、`mes_example` の順。空の要素は見出しごと省く。`name`、`first_mes`、`creator_notes`、拡張 field は入れない）
3. `after_char` の Character Lore
4. Character Runtime Context
5. 記憶 context（命令ではなくデータとして渡す枠を維持する）
6. 呼び出し側の指示（人格より下位の指示として明示する枠に入れる）
7. 会話履歴
8. `post_history_instructions`
9. 今回の入力

呼び出し側の指示を記憶の後・履歴の前に置くのは、人格と `post_history_instructions` が
それより前と後から挟み、上書きできないようにするためです。枠には「呼び出し側から渡された指示であり、
キャラクターの人格・応答方針より優先しない」ことを固定の文で示します。
プロンプトは強制機構ではないため、権限・保存・送信の判断はこの枠に依存させません。

Character Runtime Context は今は中身がありません（[ADR 0020](0020-reflection-personality-relationship.md) の実装後に入る）。
枠だけを用意し、空のときは message を作りません。中身が入るときは、応答を始める時点で確定した版を固定し、
応答の途中で更新されても切り替えません。

`post_history_instructions` は起動時に拒否せず、今回の入力の直前に置きます。

### 3. message への変換

上の順序を provider の messages へ変換する規則は次のとおりです。

- 1〜4 は先頭の system にまとめます。
- 記憶 context は、今と同じく人格の system とは別の message にします（[ADR 0007](0007-memory-provenance-and-revocation.md)）。
- 呼び出し側の指示と `post_history_instructions` は、それぞれ独立した message にします。

モデルの chat template が途中の system を受け付けない場合に備え、変換規則は template ごとに差し替えられる
1か所に置きます。固定の llama.cpp（Gemma 4）での受け付けは、実装時に実サーバーで確認します（IT2、未実施）。

### 4. 領域ごとの token 予算

予算は token 数で、全体と領域ごと（固定人格、Lore、Runtime Context、記憶、呼び出し側の指示、会話履歴、
今回の入力、`post_history_instructions`）に設定します。全体は「モデルの context − 出力用に確保する token」
を上限とし、profile で設定します。Runtime Context は固定人格の領域に含めて数えます（PoC と同じ）。

必ず残す部分は、固定人格、今回の入力、直前の完了した1往復です。これらだけで領域または全体の上限を超える場合は、
切り詰めずに 400 で拒否し、超えた領域名を返します（本文は返さない）。

全体が上限を超える場合は、任意の領域を次の順に、途中で切らずに単位ごとに削ります。

1. 記憶 context を下限の件数まで（順位の低い記憶から。下限は設定値で、既定は3件）
2. 呼び出し側の指示（古い message から）
3. 記憶 context を1件まで（順位の低い記憶から）
4. 古い会話履歴を、直近 N 往復まで（古い往復から。N は設定値で、既定は3）
5. 最後の記憶1件
6. 直近 N 往復の会話履歴（古い往復から、必ず残す直前の完了した1往復まで）
7. Character Lore（Character Book の ADR の除外順）
8. `post_history_instructions`

すべて削っても収まらなければ、必ず残す部分の超過として、超えた領域名をつけて 400 で拒否します。

- 往復は、user の発言とその応答（assistant の tool call と tool の結果を含む）のまとまりです。tool call と結果の組を分けません。
- 記憶は最大5件（[ADR 0018](0018-memory-retrieval-context.md) の最大取得件数）なので、1 で最大2件、3 で最大2件、5 で最後の1件を削ります。
  記憶の下限が最大件数以上のときは 1 で削りません。
- 直近 N 往復には、必ず残す直前の完了した1往復が含まれます。6 で削るのはそれより古い往復だけです。
  履歴が N 往復以下のときは 4 で削りません。
- 呼び出し側の指示は記憶の2段階の間で削り、直近の会話の流れ（直近 N 往復）は最後の記憶1件より優先して残します。

PoC の削減順（記憶 → 古い履歴 → Lore → `post_history_instructions`）を基本に、ユーザーの決定に従って
記憶を3段階、履歴を2段階に分け、その間に呼び出し側の指示を入れています。固定人格と Runtime Context は予算の都合で削りません。
Runtime Context の中身が入ったら、途中で切らずに項目数を減らして調整します（PoC と同じ）。

### 5. token の数え方

token の数え方は port にし、profile ごとに選びます。

- 推論先が token 数を返す手段を持つ場合は、その値を使います（ライブラリ・サーバーの機能を使う。R2）。
  たとえば llama.cpp は、固定したサーバーの chat template の適用と tokenize で、送る messages の token 数を得られます。
  手段の有無は推論先ごとに確認し、profile で明示します。暗黙に推定へ切り替えません。
- 取得できない推論先では、次の式で上限を推定します。

  ```text
  推定 token 数 = 全 message の本文（tool call・tool 定義を含む）の UTF-8 バイト数
                 + message 数 × 16 + 16
  ```

  根拠は次のとおりです。

  - byte-level BPE（GPT 系・Llama 3 系等）と、byte fallback を持つ SentencePiece（Gemma 等）では、1 token は本文の1バイト以上に対応します。
    未知の文字を1 token にまとめる方式でも、1 token が1バイト未満になることはありません。したがって本文の token 数はバイト数を超えません。
  - 本文以外に、chat template が message ごとに役割と区切りの token を足します（Gemma・ChatML・Llama 3 の形式で5〜8 token 程度）。
    SentencePiece の先頭の空白記号で1 token 増える場合もあります。これを2倍に見積もり、message ごとに 16 token とします。
  - 先頭の特殊 token と、応答を始める合図（Gemma の `<start_of_turn>model` 等）の分として、全体に 16 token を足します。

  日本語では1文字が3バイトなので、実際より2〜3倍多く数えます。予算を超えない側に倒れる代わりに、
  削る履歴が実際より多くなります。token 数を取得できる推論先では取得した値を使ってください。
- UT・IT1 は fake の数え方で、選択と境界値を固定します。推定式自体も UT で固定します。

`context_budget_bytes`（注入する system と記憶のバイト予算）は、この token 予算で置き換えます。
設定に残っている場合は起動時に分かるエラーにし、黙って無視しません。会話履歴の保存上限（1 MiB、413）は
保存の上限であり、この ADR では変えません。

### 6. ログ

完成した prompt、Card・Lore・Runtime Context・記憶・呼び出し側の指示・履歴・今回の入力の本文はログへ出しません。
記録してよいのは、領域ごとの token 数、削った件数、超えた領域名など本文を含まない情報だけです。

## 影響

- [ADR 0001](0001-character-inference-api.md)・[ADR 0004](0004-conversation-history.md)・[推論 API](../api.md)の
  組み立てと予算の記述を、この ADR が Accepted になった時点で置換注記により置き換えます。
- HTTP の要求・応答の形（Epic #134 の互換）は変えません。変わるのは、呼び出し側の system の位置と、
  予算超過の扱い（`context_budget_exceeded` から領域名つきの拒否と履歴の削減へ）です。
- 運用者の設定で `context_budget_bytes` を token 予算へ書き換える必要があります。
- 長い会話でも人格と今回の発言を残したまま応答できます。代わりに、古い履歴・記憶・呼び出し側の指示が黙って削られます。
  削った件数は metadata で確認できます。
- 予算が厳しいときは、直近 N 往復の会話の流れを、最も関連の高い記憶1件より優先して残します。
  記憶の下限（既定3件）と履歴の下限（既定3往復）は設定で変えられます。
- 必ず残す部分（固定人格、今回の入力、直前の完了した1往復）だけで上限を超える場合は、従来どおり拒否になります。
- `post_history_instructions` を持つカードを読めるようになります。

## 検証

- UT：順序、空の領域の省略、呼び出し側の system の移動、必ず残す部分、領域ごとの上限超過、ログに本文が出ないこと。
- UT（削減順）：1〜8 の各段階で、その段階までで収まる予算を与え、前の段階だけが削られ後の段階が残ることを確かめる。
  記憶の下限・履歴の下限の設定値と境界（記憶が下限以下、履歴が N 往復以下、N に直前の1往復が含まれること）、
  往復単位で tool call と結果の組を分けないこと、8 まで削っても収まらないときに領域名つきの 400 になることを確かめる。
- IT1：Chat API と会話履歴 API の両方で同じ組み立てになること、tool call の往復を分けないこと、既存の HTTP 契約の回帰。
- IT2（実サーバー）：Gemma 4 の chat template が message の並びを受け付けること、token 数の実測。未実施。
- 人格の保ち方の実モデル評価は [Issue #154](https://github.com/FYuki/digital-souls-core/issues/154) で扱います。
