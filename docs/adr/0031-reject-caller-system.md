# ADR 0031: 呼び出し側の system を受け取らない

Status: Proposed

日付: 2026-10-11

関連: [Epic #150](https://github.com/FYuki/digital-souls-core/issues/150)、[Issue #177](https://github.com/FYuki/digital-souls-core/issues/177)、
[ADR 0026](0026-prompt-assembly.md)、[ADR 0030](0030-injection-resistance-evaluation.md)、[ADR 0027](0027-tool-use-and-activity-runtime.md)

## 背景

ADR 0026 は、stateless の Chat API（`/v1/chat/completions`、`/v1/character/completions`）で呼び出し側が送る system を
「呼び出し側の指示」として受け取り、人格より下位であることを固定の文で示す枠に入れると決めました（照合一覧の J1）。
会話履歴 API は、もともと呼び出し側の system を拒否しています（400 `invalid_turn`）。

2026-10-11 の実モデル評価（ADR 0030、[証跡](../evidence/2026-10-11-injection-real-model-evaluation.md)）では、
Chat API の呼び出し側の system から注入した指示に、9件中7件で従いました（canary の出力）。1件は3回とも別人格として名乗りました。
この枠は、固定の Gemma 4 に対して人格の上書きを防げませんでした。一方、記憶を命令ではなくデータとして渡す枠と、
会話履歴 API の system 拒否は PASS でした。

ツールの定義は `tools` パラメータで渡せます（ADR 0027 で互換のため維持）。呼び出し側の system に残る役割は外部 Agent の振る舞いの指示だけで、
人格を上書きする指示と区別できません。

2026-10-11 にユーザーは、Chat API でも呼び出し側の system を受け取らない方針を決めました（原文「受け取らない方針で進めて」）。

## 決定

### 1. 呼び出し側の system を拒否する

stateless の Chat API（`/v1/chat/completions`、`/v1/character/completions`）は、`messages` に `role: "system"` が1件でもあれば、
推論の前に 400 で拒否します。本文の形（文字列、text の配列）によらず拒否し、黙って捨てたり、別の役割に変換したりしません。
`developer` 等の未対応の役割は、従来どおり 400 です。

### 2. エラーの形と会話履歴 API との統一

拒否のエラーコードは、両方の API で同じ `system_message_unsupported` にします（2026-10-11 ユーザー決定）。
会話履歴 API では、system を含む入力の拒否だけをこのコードに変え、それ以外の不正な turn は従来どおり `invalid_turn` のままにします。
エラー本文には要求の内容を含めません。

### 3. 予算と削減順の「呼び出し側の指示」領域

ADR 0026 の入力の型・順序・予算・削減順から、「呼び出し側の指示」の領域をなくします。

- 順序：before_char の Lore → Card → after_char の Lore → Runtime Context → 記憶 context → 会話履歴 → `post_history_instructions` → 今回の入力。
- 削減順：記憶を下限まで → 記憶を1件まで → 古い会話履歴を直近 N 往復まで → 最後の記憶1件 → 直近 N 往復の会話履歴 → Character Lore → `post_history_instructions`。
- 設定の `prompt_budget.caller_instructions` は廃止し、残っていたら起動時に分かるエラーにします（黙って無視しない。`context_budget_bytes` と同じ扱い。2026-10-11 ユーザー決定）。

### 4. `tools` パラメータの互換

`tools`・`tool_choice` と、tool call・tool の結果の往復は従来どおりです（ADR 0001・0027）。ツールの説明は `tools` の
`description` で渡せます。外部 Agent が振る舞いを指示したい場合の受け口は設けません。

### 5. 評価（ADR 0030）の変更

ADR 0030 の「Chat API の呼び出し側の system」の経路は、応答の採点から、構造の検査（system を含む入力が拒否されること。
受理されたら必須ゲート `override_channel_open`）へ移します。両方の入口で同じ構造の検査になります。

## 選択肢

（下の4つの表のうち、エラーコード・設定・移行の3つは 2026-10-11 のユーザー決定。原文「3件とも推奨案でよい」）

| 案 | 判断 |
| --- | --- |
| 呼び出し側の指示の枠を維持し、枠の文を強める | 不採用。プロンプトは強制機構ではなく、実モデルで上書きを防げなかった |
| 呼び出し側の system を記憶と同じデータの枠で渡す | 不採用。外部 Agent の指示を「データ」として渡しても役割が曖昧で、上書きの経路が残る |
| 呼び出し側の system を拒否する | 採用案（ユーザー決定の方針） |
| system を黙って捨てる | 不採用。呼び出し側が気づかず、契約違反を隠す |

エラーコード：

| 案 | 判断 |
| --- | --- |
| A：両 API で `system_message_unsupported` に統一（会話履歴 API は system の場合だけ `invalid_turn` から変わる） | 採用。拒否の理由を呼び出し側が区別でき、両 API で同じ扱いになる |
| B：Chat API だけ新しいコードにし、会話履歴 API は `invalid_turn` のまま | 不採用。同じ理由の拒否が API ごとに違うコードになる |
| C：両 API とも `invalid_turn` | 不採用。Chat API には turn の概念がなく、拒否の理由も区別できない |

設定の `prompt_budget.caller_instructions`：

| 案 | 判断 |
| --- | --- |
| A：廃止し、残っていたら起動時エラー | 採用。黙って無視しない方針（`context_budget_bytes`）と一貫する |
| B：一定期間は読み捨てる | 不採用。設定の誤りに気づけない |

既存のクライアントへの移行：

| 案 | 判断 |
| --- | --- |
| A：移行期間なしで常に 400 | 採用。受け付けを残すと上書きの経路が残る。Core は localhost の単独利用で影響を受けるクライアントが限られる |
| B：運用者の設定で一時的に受け付けを許す | 不採用。上書きの経路を残す設定になる |

## 影響

- 人格の上書きの経路（呼び出し側の system）がなくなります。会話中の user の指示による役割演技は、ADR 0030 の注記のとおり許します。
- system を送る既存のクライアントは、移行期間なしで 400 になります（2026-10-11 ユーザー決定）。OpenAI 互換のチャット UI や Agent の多くは system を送るため、
  移行には呼び出し側の変更（system をやめる、ツールの説明は `tools` へ）が必要です。
- 会話履歴 API の system 拒否のエラーコードが `invalid_turn` から `system_message_unsupported` に変わります。
- ADR 0026 の J1（呼び出し側の指示の枠）と、予算・削減順の該当部分は、この ADR が Accepted になった時点で置換注記により置き換えます。
  推論 API・SPEC・ADR 0030 の評価も同期します。
- 外部の MCP を Core のツール一覧に登録する仕組みは、この ADR の範囲外です（ADR 0027 の実装 Epic の論点）。
