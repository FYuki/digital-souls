# ADR 0028: Character Book の解釈と Lore の選択

Status: Accepted

日付: 2026-10-11

Accepted日: 2026-10-11（ユーザーの承認「承認する」も同日。承認対象は PR #160 の head `c1693425fd62966d2977d051aa5d2087a82e1c9d` の本文）

関連: [Epic #150](https://github.com/FYuki/digital-souls-core/issues/150)、[Issue #153](https://github.com/FYuki/digital-souls-core/issues/153)、
[照合一覧](../evidence/2026-10-11-poc-adr-alignment.md)の P5、[ADR 0026](0026-prompt-assembly.md)

## 背景

2026-10-11 にユーザーは、Character Book の扱いを PoC（公開 `FYuki/digital-souls`、revision `fce7382884d981c42be7fbd3ddaffe7469e27588`）の決定
[CCV3 Character Book の runtime 解釈契約](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-book-runtime-2026-08.md)
（以下「PoC の Book ADR」）を正として直すと決めました（Epic #150 R1）。

決定時点（epic `2b1f936`）の Core は、Lore を最新の user 発言との単純な key 一致だけで選び、`insertion_order` の順に人格の system へ連結していました。
`constant`、`case_sensitive`、`use_regex`、`before_char`、`scan_depth` が 1 以外、`recursive_scanning` が false 以外のカードは起動時に拒否し、
`token_budget` と `priority` は使っていませんでした（[推論 API「文脈・プライバシー」](../api.md#文脈プライバシー)）。

プロンプトの組み立ての順序と領域ごとの token 予算は [ADR 0026](0026-prompt-assembly.md)（Accepted）で決まっています。
ADR 0026 は、Lore を削る順番（削減順の7番目）の中での除外順を、この ADR に委ねています。

## 決定

PoC の Book ADR の次の節を、Core でもそのまま採用します。各節の詳細は PoC の Book ADR を正本とし、この ADR では Core での読み替えだけを書きます。

| PoC の Book ADR の節 | 採用する内容 |
| --- | --- |
| 対応範囲、Book モデル、Entry モデル | embedded の `data.character_book` だけを対象にする。各 field の型・既定値・制約。数値 field は整数だけを受理し、bool を拒否する |
| validation と互換性 | 既知 field の不正はカード内の path を含むエラーでカード全体を拒否する。未知 field、`extensions` 内の未知 field、`use_regex=true`、`recursive_scanning=true`、Decorator を含む `content` はカードを拒否せず、データとして保持する |
| scan context | 今回の user 発言と、同じ会話の履歴を新しい順に並べる。`scan_depth` は user・assistant を別々に数える message 数で、今回の発言を1と数える。省略時は1、0 は scan しない |
| literal matching | NFKC 正規化、`case_sensitive=false` のときだけ casefold、message ごとの部分一致。空の key は一致しない |
| Entry activation | 判定の優先順（無効 → 空の本文 → Decorator → regex → `constant` → primary key → `selective` の secondary key）。同じ Entry は1件にまとめる |
| MVP 非対応機能 | `use_regex=true` は選ばない。`recursive_scanning` は保持するが再帰しない。Decorator を含む Entry は選ばない |
| selection と Book token budget | 本文を途中で切らない。`token_budget` を超える間、`removal_key = (effective_priority, insertion_order, source_index)` の小さい Entry から除外する。`effective_priority` は `priority`、なければ `insertion_order`。`token_budget=0` は全除外、省略時は Book での削減なし |
| selector 出力と診断 metadata | 本文・keys・`name`・`id`・`comment` をログと metadata に出さない。記録してよいのは source index、理由コード、position、token 数、件数 |

Core での読み替えは次のとおりです。

1. **scan する履歴**：会話履歴 API では、保存済みの privacy 処理済み履歴を使います。stateless の Chat API では、ADR 0026 で「会話履歴」に分けた
   呼び出し側の messages を使います。呼び出し側の指示（system）、記憶 context、Card、選んだ Lore の本文は scan しません。
   `scan_depth` の適用は、ADR 0026 の予算で古い履歴を削る**前**に行います。
2. **配置**：`before_char` の Lore は Card の前、`after_char`（省略時を含む）の Lore は Card の後に置きます（ADR 0026 §2 の 1・3）。
   各 position の中は `(insertion_order, source_index)` の昇順です。各 Entry は `## キャラクターLore` の見出しをつけて、ADR 0026 §3 のとおり先頭の system にまとめます。
   Lore が0件なら何も足さず、Book がないカードと同じ messages にします。
3. **予算**：Book の `token_budget` と ADR 0026 の Lore 領域の上限は、どちらも ADR 0026 §5 の token の数え方（profile ごと）で数えます。
   全体の予算が足りないときは、ADR 0026 の削減順の7番目で、上と同じ `removal_key` の小さい Entry から1件ずつ削ります。
   token 数を推論先から取得できない profile でも、Book の `token_budget` は ADR 0026 の推定式で数えて適用します
   （2026-10-11 ユーザー決定。下の選択肢の表を参照）。
4. **記憶との分離**：Lore は記憶 context に入れず、記憶の参照名も付けません（PoC と同じ）。
5. **起動時の拒否の見直し**：`constant`、`case_sensitive`、`position`、`scan_depth`、`priority`、`token_budget` を理由にした起動時の拒否をやめます。
   `use_regex=true` と `recursive_scanning=true` のカードも拒否せず、上の非対応の扱いにします。

## 選択肢

| 案 | 判断 |
| --- | --- |
| 現在の簡略な実装を維持し、非対応の field を起動時に拒否する | 不採用。PoC を正とする決定（R1）に反し、標準的なカードを読めない |
| PoC の Book ADR を採用し、Core の組み立て（ADR 0026）に合わせて読み替える | 採用案 |
| regex・再帰 scan・Decorator まで対応する | 不採用。PoC でも MVP の範囲外（regex の timeout がない実行による遅延、未解釈の制御文字列の混入を避ける） |

token 数を推論先から取得できない profile での Book の `token_budget`：

| 案 | 判断 |
| --- | --- |
| A：推定式のまま `token_budget` を適用する | 採用（2026-10-11 ユーザー決定、原文「推奨案でよい」）。ADR 0026 の数え方と一貫し、予算を超えない側に倒れる |
| B：推定式の profile では `token_budget` を適用せず、ADR 0026 の Lore 領域と全体の予算だけで削る | 不採用。profile によって Book の予算の意味が変わり、カードに書かれた上限を守らない場合が生じる |

## 影響

- 標準的な CCV3 の Character Book を、起動時に拒否されずに読めます。[推論 API](../api.md) の「汎用 CCv3 実装ではない」という記述と、非対応の field の拒否を書き換えます。
- `scan_depth` が2以上なら、直前の会話に出た語でも Lore が選ばれます。
- Book の `token_budget` と `priority` が効くようになり、一致した Lore が多いときは優先度の低い Entry が外れます。
  token 数を推論先から取得できない profile では、ADR 0026 の推定式が実際より2〜3倍多く数えるため、実際より多くの Entry が外れます。
- 同梱の Miori カード（`scan_depth=1`、`token_budget=700`、全 Entry が `after_char`、`constant` なし）は、一致する Entry の選択と並びが変わりません。
  ただし、多くの Entry に同時に一致し 700 token を超える場合は、優先度の低い Entry が外れます。token 数を取得できない profile では、
  推定式が実際の2〜3倍に数えるため、この外れ方が実際より起きやすくなります（案 A の採用による）。

## 検証

- UT：PoC の Book ADR「fixture とテスト境界」の項目（Book なし、空の Book、一致・不一致、NFKC、大文字小文字、message の境界、無効、constant、selective、
  scan_depth の 0・1・複数・履歴より大きい値、同じ Entry の重複、priority・insertion_order・source_index の順、budget 0・境界・超過、before・after・省略、
  regex・再帰・Decorator、path つきのエラー、未知 field の保持、Lore と記憶と履歴と post_history の組み合わせ、全体予算での削減順）を合成カードで確かめる。
- 回帰：同梱の Miori カードで、代表的な質問に対して選ばれる Entry と並びが現在の実装と同じであること。
- IT1：Chat API と会話履歴 API で同じ Lore が選ばれること。
- ログ・metadata に Lore の本文と keys が出ないこと。
