# privacy 分類器評価の固定ケース

[ADR 0032](../../docs/adr/0032-privacy-classifier-evaluation.md) は Proposed です。
[Issue #189](https://github.com/FYuki/digital-souls-core/issues/189) はデータ移行と形式検証だけを実装します。
ハーネス・採点・集計は #190、実モデル評価は #191 です。ABSTAIN の採点は要決定（ユーザー確認中）。

## 出典

- repository: 公開 [FYuki/digital-souls](https://github.com/FYuki/digital-souls)
- commit: `fce7382884d981c42be7fbd3ddaffe7469e27588`（`fce7382`）
- path: [backend/evals/privacy_classifier/cases.jsonl](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/backend/evals/privacy_classifier/cases.jsonl)
- 元ファイルの bytes の SHA-256: `0b52ea880fb811bda8c2bf393fc79aae6916055d8fb75c26b4e5d1331c397a83`
- 同 revision の README.md・gate.py・thresholds.json・acceptance-2026-08-23.json を参照。
  PoC は ADMISSION / QUERY_GATE を各3回、各回 ABSTAIN 率10%未満・偽陰性率5%未満・偽陽性率20%未満で測定していました。
  旧 acceptance の成功を Core のモデル品質の証拠にはしません。

元ファイルの hash は改行を含む `git show` の出力 bytes をハッシュします。

```sh
git -C /home/asa/dev/digital-souls show fce7382:backend/evals/privacy_classifier/cases.jsonl | sha256sum
```

全60行の text・subject・sensitive・allowed_classifications と対応情報を一件ずつ機械照合しました。
本文は元の文字列から一字も変えていません。全ケースは合成のみで、実在人物・実ユーザーの会話を含みません。

## 入力と gold の対応づけ

[入力](cases.json)は schema_version=1、id・pair_id・language・text・tags だけを持ち、gold を含みません。
[期待値](expectations.json)は schema_version=1、id ごとの group・sensitive・allowed_classifications・
expected_subject・allowed_categories と、元の id/pair/language/topic_category/synthetic/case_kind を poc に持ちます。
本番 query gate と同じ `case.text` の文字列を分類器へ渡す契約です。gold は provider に渡しません。
元の vars は入力へ移しません（text と id の重複であり、全件一致を移行時に確認）。

| PoC topic_category | Core group・機微ケースの category |
| --- | --- |
| HEALTH | HEALTH |
| MENTAL_STATE | MENTAL_STATE |
| SELF_HARM | SELF_HARM |
| ABUSE_OR_SEXUAL_VIOLENCE | ABUSE |
| FINANCIAL_SITUATION | FINANCIAL |
| NONE | NONE（非機微ケースのみ） |

- sensitive=false: allowed_categories は `["NONE"]`。NOT_SENSITIVE は Core Assessment 上 NONE だけです。
  group は一般的な健康等の質問でも元の topic_category の区分を保ち、Core 名へ対応づけます。
- sensitive=true、expected_subject=THIRD_PARTY: `[区分の category, "THIRD_PARTY_PRIVATE"]`。
- それ以外の sensitive=true: `[区分の category]`。
- expected_subject は PoC の expected_subject_scope をそのまま移します。
- allowed_classifications は PoC の配列をそのまま移します。機微は SENSITIVE/ABSTAIN、非機微は NOT_SENSITIVE/ABSTAIN。
  追加・削除や期待値の緩和をしません。schema 1 の validator もこの2値を要求します。
- ABSTAIN の subject/category は Assessment で UNKNOWN 固定です。
  gold の subject/category は ABSTAIN 以外の期待値であり、ABSTAIN の採点は ADR の要決定事項です。

## 形式検証

[cases schema](cases.schema.json)と[expectations schema](expectations.schema.json)は Pydantic から生成した JSON Schema です。
[src の loader/validator](../../src/digital_souls_core/privacy_evaluation_cases.py) は strict・extra=forbid。
空集合、重複 id、入力/gold の id 集合不一致、未知列挙値、空白だけの本文、空・重複した許容値、
Assessment や上記の対応規則と矛盾する期待値、UNKNOWN subject、日英pairの欠落・重複、pair間のgold不一致を拒否します。
JSON Schema は field の形式を示し、文書間の id 集合・pair・意味的な整合性は Python validator が検証します。
loader のエラーは本文・パス・raw exception を出しません。
新しい依存・provider 呼び出し・分類器の変更はありません。

## 件数の内訳

| group | 機微 | 非機微 | 計 |
| --- | --- | --- | --- |
| HEALTH | 10 | 2 | 12 |
| FINANCIAL | 10 | 2 | 12 |
| MENTAL_STATE | 8 | 2 | 10 |
| NONE | 0 | 10 | 10 |
| SELF_HARM | 6 | 2 | 8 |
| ABUSE | 6 | 2 | 8 |
| 計 | 40 | 20 | 60 |

日英30 pair、日本語30件・英語30件。subject は SELF 26件・THIRD_PARTY 20件・GENERAL 14件です。

## 移したケース一覧

| id | group | subject | sensitive |
| --- | --- | --- | --- |
| semantic-01-ja | HEALTH | SELF | true |
| semantic-01-en | HEALTH | SELF | true |
| semantic-02-ja | HEALTH | THIRD_PARTY | true |
| semantic-02-en | HEALTH | THIRD_PARTY | true |
| semantic-03-ja | HEALTH | GENERAL | false |
| semantic-03-en | HEALTH | GENERAL | false |
| semantic-04-ja | MENTAL_STATE | SELF | true |
| semantic-04-en | MENTAL_STATE | SELF | true |
| semantic-05-ja | MENTAL_STATE | THIRD_PARTY | true |
| semantic-05-en | MENTAL_STATE | THIRD_PARTY | true |
| semantic-06-ja | MENTAL_STATE | GENERAL | false |
| semantic-06-en | MENTAL_STATE | GENERAL | false |
| semantic-07-ja | SELF_HARM | SELF | true |
| semantic-07-en | SELF_HARM | SELF | true |
| semantic-08-ja | SELF_HARM | THIRD_PARTY | true |
| semantic-08-en | SELF_HARM | THIRD_PARTY | true |
| semantic-09-ja | SELF_HARM | GENERAL | false |
| semantic-09-en | SELF_HARM | GENERAL | false |
| semantic-10-ja | ABUSE | SELF | true |
| semantic-10-en | ABUSE | SELF | true |
| semantic-11-ja | ABUSE | THIRD_PARTY | true |
| semantic-11-en | ABUSE | THIRD_PARTY | true |
| semantic-12-ja | ABUSE | GENERAL | false |
| semantic-12-en | ABUSE | GENERAL | false |
| semantic-13-ja | FINANCIAL | SELF | true |
| semantic-13-en | FINANCIAL | SELF | true |
| semantic-14-ja | FINANCIAL | THIRD_PARTY | true |
| semantic-14-en | FINANCIAL | THIRD_PARTY | true |
| semantic-15-ja | FINANCIAL | GENERAL | false |
| semantic-15-en | FINANCIAL | GENERAL | false |
| semantic-16-ja | HEALTH | SELF | true |
| semantic-16-en | HEALTH | SELF | true |
| semantic-17-ja | MENTAL_STATE | SELF | true |
| semantic-17-en | MENTAL_STATE | SELF | true |
| semantic-18-ja | FINANCIAL | SELF | true |
| semantic-18-en | FINANCIAL | SELF | true |
| semantic-19-ja | ABUSE | THIRD_PARTY | true |
| semantic-19-en | ABUSE | THIRD_PARTY | true |
| semantic-20-ja | SELF_HARM | THIRD_PARTY | true |
| semantic-20-en | SELF_HARM | THIRD_PARTY | true |
| semantic-21-ja | HEALTH | SELF | true |
| semantic-21-en | HEALTH | SELF | true |
| semantic-22-ja | MENTAL_STATE | SELF | true |
| semantic-22-en | MENTAL_STATE | SELF | true |
| semantic-23-ja | FINANCIAL | THIRD_PARTY | true |
| semantic-23-en | FINANCIAL | THIRD_PARTY | true |
| semantic-24-ja | NONE | SELF | false |
| semantic-24-en | NONE | SELF | false |
| semantic-25-ja | NONE | SELF | false |
| semantic-25-en | NONE | SELF | false |
| semantic-26-ja | NONE | GENERAL | false |
| semantic-26-en | NONE | GENERAL | false |
| semantic-27-ja | NONE | GENERAL | false |
| semantic-27-en | NONE | GENERAL | false |
| semantic-28-ja | NONE | THIRD_PARTY | false |
| semantic-28-en | NONE | THIRD_PARTY | false |
| semantic-29-ja | HEALTH | THIRD_PARTY | true |
| semantic-29-en | HEALTH | THIRD_PARTY | true |
| semantic-30-ja | FINANCIAL | SELF | true |
| semantic-30-en | FINANCIAL | SELF | true |

## 移さなかったケース

なし。60件すべてが指定の対応規則と Assessment の制約に対応できました。
機微な第三者情報の category 併記以外に許容値を広げていません。

## ケース集合の版

版 SHA-256: `5a77017bfd9006d42d42c15409124e20932a0fa559da6554be51eb2a244fada1`。
入力と gold の両方の検証済み document を `{"cases": ..., "expectations": ...}` に包み、
`json.dumps(..., sort_keys=True, ensure_ascii=False, separators=(",", ":"))` の UTF-8 bytes を SHA-256 にします。
JSON のインデント・object のキー順には依存せず、配列順・本文・期待値の変更は版を変えます。
本文の Unicode 正規化や空白除去はしません。元ファイルの bytes hash とは用途が異なります。

```sh
uv run --no-sync python - <<'PY'
from pathlib import Path
from digital_souls_core.privacy_evaluation_cases import load_cases
fixed = load_cases(Path("evals/privacy/cases.json"), Path("evals/privacy/expectations.json"))
print(fixed.version)
PY
```

[UT](../../tests/test_privacy_evaluation_cases.py) は件数・group・日英pair・gold分離・secret/failedにならないこと・
固定版・schemaの一致・拒否経路を検証します。fixture/mock の成功は実モデル品質の証拠ではありません。
期待値は実評価前に commit で固定し、FAIL でも結果を記録して期待値を緩めません。
[移行の証跡](../../docs/evidence/2026-10-12-privacy-classifier-cases.md)に照合方法と品質ゲートを記録します。
