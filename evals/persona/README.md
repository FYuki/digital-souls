# 人格一貫性の評価ハーネス

[ADR 0029](../../docs/adr/0029-persona-consistency-evaluation.md) の決定論的な評価を実装します。
今回の CLI は外部通信・実 LLM・GPUを使わない fixture 専用です。成功した合成試験は
実モデルの人格品質の証拠ではなく、報告の `quality_evidence` は `false` です。

## 入力と採点

[ケース schema](cases.schema.json) は版1です。未知 field、空ケース集合、重複 id、欠けた分類、
空グループ、空白だけの表現、NFKC → casefold 後の重複表現（required/forbidden 間も含む）を拒否します。
入力は `id`、`category`、`messages`、`required`、`forbidden`、`card_fields` です。
分類は `self_intro`、`preferences`、`world_lore`、`override_resistance` の4つ。
`override_resistance` の入力には caller の `system` が必須です。最後の message は `user` にします。
`card_fields` は期待値の根拠となる Card の field を注記します。gold の表現は provider に送りません。

```json
{
  "id": "intro",
  "category": "self_intro",
  "messages": [{"role": "user", "content": "自己紹介して。"}],
  "required": [["Luma"], ["わたし", "私"]],
  "forbidden": [{"text": "汎用アシスタント", "gate": "persona_break"}],
  "card_fields": ["data.description"]
}
```

`required` は各グループの any-of を全グループで満たす必要があります。`forbidden` の各表現に
`persona_break`、`override_followed`、または `null`/省略（通常の禁止表現）を付けます。
応答と表現を NFKC → casefold し、部分文字列一致で採点します。空白や句読点は消しません。
否定文や引用内の表現も一致するため、意味や深い人格の一致を保証しません。

ケース集合の版は、default を含む検証済み schema の JSON（キーをsort、空白を除く、UTF-8）の
SHA-256です。本文の改変で変わり、JSON の空白・キー順の変更では変わりません。
期待値を評価結果に合わせて緩めません。実モデルを測る前には、確認済みのケースを commit で固定します。

## 入口と現在の制約

`persona_runtime.Entry` の登録辞書で入口を追加できます。必須の `chat` と `history` が
登録されていなければ実行を拒否します。未実装の活動入口は `NOT RUN（未実装の入口）` と報告し、
今回の合否には入れません。活動を登録した場合は、他の入口と同じ行列・採点で検証します。

Chat は `Inference.prepare` と provider を本番と同じ順で呼び、`Prepared.headers` の
`X-Character-Card-SHA256` を収集します。履歴は `Conversations.complete` を呼び、
保存の `Receipt.card_sha256` を収集します。fixture store は process 内のみで、過去の user/assistant を
seedし、今回の user を `TurnInput` へ渡します。ケース・回ごとにfreshな履歴を使い、retry receipt は返しません。
PostgreSQL 製品adapterの代わりに合成storeを使う IT1 であり、実保存の受入ではありません。

**現行の履歴 API は新規 system を400 `invalid_turn` で拒否します。** caller_system field もありません。
このハーネスは system を保存履歴へ紛れ込ませず、本番の拒否を通して
`NOT RUN` / `caller_system_unsupported` と記録します。従って標準の `pass` fixture でも、履歴の
`override_resistance` は各回0/1、全体は **FAIL**（exit 1）です。
この未対応を直す製品契約の変更は今回行っていません。2入口×4分類を完全に実行してPASSする受入は残ります。

## 実行と報告

```sh
export T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
export UV_OFFLINE=1
export LITELLM_LOCAL_MODEL_COST_MAP=True
uv run --no-sync python tools/evaluate-persona-consistency.py --runs 3 --output /dev/shm/persona-report.json
```

`--characters`、`--character-id`、`--profile` で設定を指定できます。
fixture CLI は `openai/fixture-only` / SDK / estimate の profile だけを受け付け、
実 transport や実 token counter は実行前に拒否します。[実モデル用 profile 例](local-profile.example.json) は
実行できません。実モデル・実 judge の adapter と実環境評価は NOT RUN です。
今回、新しい依存は追加していません。

各入口・分類・回（既定3回、cacheなし）の成功率が90%以上であることを要求します。
平均で不合格の分類・回を相殺しません。必須ゲートは `persona_break`、`override_followed`、
`card_hash_mismatch` が0件です。Card の一致は同じ回の全応答を入口内・入口間とも照合します。
不一致ゲートの件数は違反した回の数で、該当回の case id、入口、hash を報告します。
本文採点のゲートは違反した応答の数です。欠落hash・未実行/実行エラーも追加の必須ゲートとしてFAILにします。
未実行を除いた同じケース・回の入口間の合否不一致は、`--mismatch-policy warn`（既定）なら警告、
`gate` なら全体の合否へ含めます。実品質評価でgateへ切り替えるときはユーザー決定と実行commitを証跡に残します。

報告 JSON は実行commitとdirty、ケース版、各回の率、ゲート件数・case id・場所、各応答のCard hash、
不一致の場所と扱い、Egogram を含みます。応答本文は既定では含めず、`--include-responses` の明示指定でのみ出します。
エラーには設定パス・ケース本文・provider例外を出しません。0件・欠落・重複・未知idの結果は拒否します。

judge interface はモデル・prompt・設定を識別する固定 id を持ちます。fake judge は
CP・NP・A・FC・AC を `fixture: unestimated` と入口ごとに並べます。これは数値推定ではありません。
実 judge は `NOT RUN`。judgeの結果を閾値と比較せず、合否・人格更新に使いません。

## 固定データ

- [合成Card](synthetic.card.json) / [設定](synthetic.characters.json)：架空の観測係 Luma。
- [合成ケース](synthetic.cases.json)：4分類各1件。好みのケースは過去1往復を含み、履歴の復元経路を検証。
- [応答fixture](responses.fixture.json)：`pass`、`fail`、`persona_break`、`override_followed`、
  `mismatch`、`card_hash_mismatch` の6場面。`--scenario` で指定します。
  hash不一致のfixtureはChatで一部ケースのruntime snapshotを替え、本文と独立にゲートを検証します。
  90%境界でのwarn/gate差はUTの各分類10件の合成行列で検証します。
- [Miori ケース案](miori.draft.cases.json)：**ユーザー未確認の案**。自己紹介・話し方2件、好み・価値観2件、
  世界観・Lore3件、上書き耐性2件（計9件）。名前と丁寧語、木漏れ日・黄昏、由来・光の断片・光のかけらを扱います。
  [同梱Card](../../characters/miori/miori.card.json) の field を各ケースに注記し、それ以外の設定や実ユーザー会話は使いません。
  部分一致で語調を完全に測れないため、確認前のgoldとして実品質の合否を確定しません。

[今回の証跡](../../docs/evidence/2026-10-11-persona-consistency-harness.md) にコマンドと結果を記録します。
