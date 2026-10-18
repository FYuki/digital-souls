# 人格一貫性の評価ハーネス

[ADR 0029](../../../docs/adr/0029-persona-consistency-evaluation.md) の決定論的な評価を実装します。
既定の CLI は外部通信・実 LLM・GPUを使わない fixture モードです。成功した合成試験は
実モデルの人格品質の証拠ではなく、報告の `quality_evidence` は `false` です。

## 入力と採点

[ケース schema](cases.schema.json) は版1です。未知 field、空ケース集合、重複 id、欠けた分類、
空グループ、空白だけの表現、NFKC → casefold 後の重複表現（required/forbidden 間も含む）を拒否します。
入力は `id`、`category`、`messages`、`required`、`forbidden`、`card_fields` です。
分類は `self_intro`、`preferences`、`world_lore` の3つ。
ケースの会話は `user` / `assistant` とし、最後の message は `user` にします。
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
`persona_break`、または `null`/省略（通常の禁止表現）を付けます。
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

2026-10-11 ユーザー決定により、人格の上書きへの耐性（`override_resistance`）、
上書き指示に従った表現のゲート（`override_followed`）、会話履歴 API の system 拒否の構造検査・
ゲート（`override_channel_open`）をこのハーネスから外しました。
system を受け付ける入口だけへケースを適用する規則・対象外集計も撤去しています。
これらは [Issue #165](https://github.com/FYuki/digital-souls-core/issues/165) の
インジェクション対策の評価へ移します。製品 API の system 拒否仕様は維持します。
標準の `pass` fixture は、Chat・履歴の3分類すべてが各回100%、全体 **PASS**（exit 0）です。
ケース集合の各分類は1件以上必須で、空集合・0件の試験をPASSにしません。

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
fixture モードは `openai/fixture-only` / SDK / estimate の profile だけを受け付け、
実 transport や実 token counter は実行前に拒否します。`--profile` は fixture のみの override です。
今回、新しい依存は追加していません。

### 実モデルでの実行（運用者・監督担当のみ）

`--mode real` は `--characters`、`--character-id`、`--cases` の明示指定を要求します。
本番の `LiteLLMProvider`、`Inference`、`Conversations` を使い、設定の transport・token counter・
prompt budget・Card/Lore を変更せずに読み込みます。llama.cpp の準備・起動は
[運用手順](../../../docs/llamacpp-operations.md)を参照してください。実環境への送信は承認された担当だけが行います。

設定は [本番と同じ設定例](../../examples/characters.llamacpp.json)をリポジトリ外へコピーして作ります。
例は `external_send_allowed=false` のままです。承認済みの運用者がコピー先だけを `true` にし、
Card の参照を絶対パスに変更します。profile 単体の [例](local-profile.example.json) も送信禁止が既定です。
以下はこの worktree の root で実行する例です（設定生成だけではモデルへ送信しません）。

```sh
export T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
export UV_OFFLINE=1
export LITELLM_LOCAL_MODEL_COST_MAP=True
umask 077
python3 - <<'PY'
import json
from pathlib import Path
root = Path.cwd()
settings = json.loads((root / "examples/characters.llamacpp.json").read_text())
settings[0]["card_path"] = str(root / "characters/miori/miori.card.json")
settings[0]["profile"]["external_send_allowed"] = True
Path("/dev/shm/persona-real.characters.json").write_text(json.dumps(settings))
PY
bash tools/with-test-postgres.sh uv run --no-sync python tools/evaluate-persona-consistency.py \
  --mode real --characters /dev/shm/persona-real.characters.json --character-id miori \
  --cases evals/persona/miori.cases.json --runs 3 \
  --max-completion-tokens 1024 --temperature 0.7 --mismatch-policy warn \
  --output /dev/shm/persona-real-report.json
```

生成設定は `--max-completion-tokens`（別名 `--max-tokens` と同時指定不可）と `--temperature` を
指定できます。両入口とも本番の入力検証と profile の `allowed_parameters` を通します。
指定しない値はモデル/server の既定値です。報告の `generation_settings` に明示指定値を残し、
profile 識別子・モデル・transport・token counter・timeout・許可パラメータ・prompt budget も記録します。
設定パス、endpoint、認証情報は報告しません。

履歴保存には製品の `PostgresHistory` を使います。既存の `with-test-postgres.sh` が
ネットワークなし・公開ポートなしの専用 PostgreSQL コンテナを作り、終了時に削除します。
CLI はその専用 Unix socket と合成 DB/user 名だけを受け付け、ランダムな評価専用 schema を使います。
既存 DB の DSN 指定はありません。ケース・回ごとに新しい coordinator と会話を作り、合成の過去履歴を seed して
`Conversations.complete` へ今回の user を渡し、成功・失敗とも会話を削除します。
実データ・運用 DB・記憶検索・人格の可変状態は使いません。この保存方法を選んだ理由は、
fixture の部分的な合成 store に実応答を保存する代わりに、製品の保存契約と Card metadata を通すためです。

LiteLLM の応答 cache は読み書きとも無効、llama.cpp の生成リクエストも `cache_prompt=false` です。
既定3回で、既存 receipt の再利用・自動再試行はしません。接続失敗・timeout・拒否・途中終了・保存失敗は
本文を含まない `evaluation_failed` として記録し、`execution_incomplete` の必須ゲートを FAIL にします。
入力や保存先設定自体が不正なら、内容を含まない CLI エラーで exit 1 になります。

実モードの報告は `mode: real`、`quality_evidence: true` を記録します。これは実モデルを測定したことを表し、
品質の PASS は別に `status`・分類ごとの率・必須ゲートで判断します。commit/dirty・ケース版 hash・Card hash・
warn/gate・各回の結果は fixture と同じ形式です。応答本文は `--include-responses` のときだけ出します。
Egogram は fake judge のままで、`real_judge: NOT RUN` と報告し合否には使いません。
活動入口・実 judge・実モデルの品質受入は未実施です。UT/IT1 の実モード経路には fake provider と
fake token counter を注入するため、これらの PASS を実品質の証拠にしません。

各入口・分類・回（既定3回、cacheなし）の成功率が90%以上であることを要求します。
平均で不合格の分類・回を相殺しません。必須ゲートは `persona_break`、
`card_hash_mismatch`、`card_hash_missing`、`execution_incomplete` が0件です。Card の一致は同じ回の全応答を入口内・入口間とも照合します。
不一致ゲートの件数は違反した回の数で、該当回の case id、入口、hash を報告します。
本文採点のゲートは違反した応答の数です。欠落hash・未実行/実行エラーも追加の必須ゲートとしてFAILにします。
比較する全入口で実行済みの同じケース・回だけで入口間の合否不一致を計算します。
不一致は、`--mismatch-policy warn`（既定）なら警告、
`gate` なら全体の合否へ含めます。実品質評価でgateへ切り替えるときはユーザー決定と実行commitを証跡に残します。

報告 JSON は実行commitとdirty、ケース版、各回の率、ゲート件数・case id・場所、各応答のCard hash、
不一致の場所と扱い、Egogram を含みます。応答本文は既定では含めず、`--include-responses` の明示指定でのみ出します。
エラーには設定パス・ケース本文・provider例外を出しません。0件・欠落・重複・未知idの結果は拒否します。

judge interface はモデル・prompt・設定を識別する固定 id を持ちます。fake judge は
CP・NP・A・FC・AC を `fixture: unestimated` と入口ごとに並べます。これは数値推定ではありません。
実 judge は `NOT RUN`。judgeの結果を閾値と比較せず、合否・人格更新に使いません。

## 固定データ

- [合成Card](synthetic.card.json) / [設定](synthetic.characters.json)：架空の観測係 Luma。
- [合成ケース](synthetic.cases.json)：3分類各1件。好みのケースは過去1往復を含み、履歴の復元経路を検証。
- [応答fixture](responses.fixture.json)：`pass`、`fail`、`persona_break`、
  `mismatch`、`card_hash_mismatch`、`card_hash_missing`、`execution_incomplete` の7場面。`--scenario` で指定します。
  hash不一致のfixtureはChatで一部ケースのruntime snapshotを替え、本文と独立にゲートを検証します。
  hash欠落・実行不完了のfixtureは合成観測結果だけを変更し、各必須ゲートを検証します。
  90%境界でのwarn/gate差はUTの各分類10件の合成行列とIT1のCLIで検証します。
  標準CLIのPASS / exit 0と各違反のFAIL / exit 1を、CI対象のUT・IT1で固定しています。
- [固定 Miori ケース](miori.cases.json)：2026-10-11 ユーザー決定を反映した固定集合。
  自己紹介・話し方2件（自己紹介と挨拶）、好み・価値観2件、世界観・Lore3件（計7件）。
  名前と丁寧語、木漏れ日・黄昏、光の現象が寄り集まって生まれた由来・光の断片・光のかけらを扱います。
  [同梱Card](../../characters/miori/miori.card.json) の field を各ケースに注記します。
  必須ゲート `persona_break` の表現は全ケース共通で「私は言語モデル」「AIアシスタントです」
  「大規模言語モデルとして」の3つに限定します。単独の「AI」「アシスタント」は禁止しません。
  2026-10-11 のユーザー決定で、`miori-name` の丁寧語の条件を「です／といいます／と申します」のいずれかに広げました
  （実モデルの応答が「光織といいます」の形で名乗り、人格に沿っていたため。[証跡](../../../docs/evidence/2026-10-11-persona-real-model-evaluation.md)）。
  同日のユーザー決定で Card の概要・由来を「存在」に修正し、`miori-origin` の required を
  `[["光"], ["生まれ", "寄り集ま"]]` に固定しました。「AI」のグループは外しました。
  [同日先行の実モデル評価](../../../docs/evidence/2026-10-11-persona-real-model-evaluation.md)は
  この変更前の「光」と「AI」を必須とする条件で測定した結果であり、新版の品質評価ではありません。
  [変更の証跡](../../../docs/evidence/2026-10-11-miori-origin-lore.md)に新旧 Card・ケース版を記録します。
  固定ケースを `--cases` で指定した報告の `case_version` に集合のSHA-256、`cases_draft: false` を記録します。
  標準CLIはLumaの合成fixtureを使います。Mioriの fixture 評価には対応するCard設定・応答fixtureを指定し、
  実モデル評価には上記の real モードを使います。
  新版の Card・ケースでの実モデルのMiori品質は未評価です。

## 今後の課題

運用開始後に、望ましい QA の組（質問と望ましい応答の例）を追加し、一致傾向の評価を加えます。
含んだら不合格の表現は最低限に保ちます。この追加評価は未実装です。

[今回の証跡](../../../docs/evidence/2026-10-11-persona-consistency-harness.md) にコマンドと結果を記録します。
[実モード追加の合成検証](../../../docs/evidence/2026-10-11-persona-real-mode.md) は別の証跡として記録します。

[インジェクション耐性の独立したハーネス](../injection/README.md) で system・user・記憶の攻撃と履歴 system 拒否を評価します。
