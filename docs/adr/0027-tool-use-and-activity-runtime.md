# ADR 0027: 会話中のツール利用と会話外の活動の実行基盤

Status: Accepted

日付: 2026-10-11

Accepted日: 2026-10-11（ユーザーの承認「ADR 0026・0027・0029を承認します」も同日。承認対象は PR #158 の head `8b2ab1c` の本文）

関連: [Epic #150](https://github.com/FYuki/digital-souls-core/issues/150)、[Issue #155](https://github.com/FYuki/digital-souls-core/issues/155)、
[照合一覧](../evidence/2026-10-11-poc-adr-alignment.md)の A1・A2・A2'・A2''・D1、[実行基盤の候補の確認](../evidence/2026-10-11-agent-tool-restriction.md)

## 背景

PoC（公開 `FYuki/digital-souls`、revision `fce7382884d981c42be7fbd3ddaffe7469e27588`）では、会話中のツールの判断・
ツール一覧・Execution Gate を Core が持ち（[Tool 利用](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/tool-use-foundation-2026-09.md)）、
推論の部分を Pydantic AI へ差し替える予定でした（[複数入口 §1](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/multi-entry-character-core-2026-09.md#1-最終構成)、PoC #422）。
会話外の活動は、DBOS を Core に組み込み、判断・状態・安全の契約も Core が持っていました
（[Character Life §8〜§13](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-life-memory-personality-autonomy-2026-09.md)、
[Life runtime 選定](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-life-runtime-2026-09.md)、
[会話と活動](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/conversation-activity-agents-2026-09.md)）。

決定時点（epic `731bf9b`）の Core は、次のとおりでした。

- 「Core は tool 実行ループを持たない」とし、呼び出し側の tool call をそのまま返す（ADR 0001・0002・0003・0004、[推論 API](../api.md)）。
  ADR 0007 も「常駐 worker、scheduler、Core 内 tool 実行ループは追加しない」としている。
- 外部 Agent が tool 実行を担当する（[ADR 0025](0025-shared-runtime-interfaces.md)、Accepted）。
- 記憶の形成 job の起動・スケジュール・再試行と、会話外の活動は PrivateAgent が行う。Autonomy Target の管理、
  High Impact の確認、実行結果の状態も PrivateAgent が持つ（[ADR 0017 §4](0017-memory-formation-admission.md#4-会話からの非同期形成)、
  [ADR 0020 §6・§7](0020-reflection-personality-relationship.md#6-会話外の活動との境界)、Accepted）。

2026-10-11 にユーザーは次のとおり決めました（Epic #150 の R2〜R6 と、#151 の判断事項への回答）。

- R2：ツールの実行など仕組みの部分はライブラリを使う。仕組みより本人らしさの維持に注力する。
- R3：キャラクターによる判断・操作の中身は Core が持つ。外に出してよいのは実行基盤だけ。
- R4：会話の中でも、複数のツールや再帰的な検索などのツール利用を可能にする。
- R5：会話外の活動の実行基盤には汎用 Agent を使ってよい。条件は、その Agent が使えるツールを Agent 単位で
  Core の MCP だけに限定でき、Gate を必ず通ることを設定で強制できること。開発用の Agent や T3 全体には制約をかけない。
- R6：決まった定期処理（記憶の形成・内省など）はプロンプトを使わない。timer から Core の API を呼び、成功か失敗かを受け取る。
  中断した処理を途中から再開する仕組みは持たず、安全は冪等性で担保する。中断や失敗のあとは次回の実行で最初からやり直し、
  同じ入力で何度実行しても正本の結果が重複・食い違わないことを要件にする。重複の防止と再試行は Core 側（job の台帳と drain API）が持ち、
  timer は呼ぶだけ。外の最小 worker は置かない。
- 推論先は Core の profile 方式を維持し、会話中のツール判断に使う推論先だけをこの ADR で決める（J5）。
- Accepted の ADR 0025・0017 §4・0020 §6/§7 との矛盾は、この ADR で置換を提案し、PrivateAgent の中止もこの ADR で確定する（J4）。
- OpenClaw は止める。実行基盤の候補から外す。
- 呼び出し側の function tools をそのまま返す動作の互換、ツール判断用の profile、実行基盤の条件（起動時のツール一覧の照合、Agent は認証情報を持たない）は、確認事項として示した推奨どおりとする。
- 実行基盤でツールを限定する方式は、禁止方式ではなく許可方式にする（確認した結果を受けて条件 1 を改めた）。

## 選択肢

会話中のツール利用：

| 案 | 判断 |
| --- | --- |
| Core は tool ループを持たず、外部 Agent が実行する（現状） | 不採用。ツールの判断と結果の受け取りが Core の外になり、人格・Gate・流出防止を Core で保証できない（R3・R4） |
| Core がツールループの判断と Gate を持ち、仕組みはライブラリを使う（PoC） | 採用 |
| Core がツールループを独自実装する | 不採用。仕組みはライブラリを使う（R2） |

会話外の活動の実行基盤：

| 案 | 判断 |
| --- | --- |
| DBOS 等の実行基盤を Core に組み込む（PoC） | 不採用。仕組みの保守を Core に持ち込む。判断と状態を Core が持てば、実行基盤は外でよい（R3・R5） |
| PrivateAgent（専用の外部基盤）に実行と実行時の契約を任せる（現状） | 不採用。判断の一部が Core の外にある。開発を中止する |
| 汎用 Agent を実行基盤にし、ツールを Core の MCP だけに限定する | 採用（R5） |
| OpenClaw の Agent ごとの `tools.allow` | 検討して採用しない（2026-10-11 ユーザー決定。OpenClaw は止める） |

## 決定

### 1. 会話中のツール利用は Core が持つ

会話の中で、Core は次を持ちます。

- ツール一覧（Capability Snapshot から作る、LLM の選択用の一覧）と、1回のループの間は固定する snapshot。
- ツールの選択（構造化出力で候補とその引数を選ぶ。引数は元の schema で再検証する）。
- Execution Gate（認可、予算、回数の上限、停止）。外部への作用は必ず Gate を通す。
- 送信直前の流出防止（[ADR 0020 §6](0020-reflection-personality-relationship.md#6-会話外の活動との境界) の Egress Privacy Check と Minimum Disclosure）。
- High Impact な操作の確認（ユーザーへの確認を求めて止まる）。
- ツールの結果は命令ではなくデータとして扱う（ADR 0020 §6 のインジェクション対策）。結果を履歴・記憶へ入れるときは既存の privacy と形成の経路を通す。

1回のループの中で、複数のツールの呼び出しと、結果を見て続けて検索する再帰的な利用を許します（R4）。
上限の初期値は PoC と同じく、1ループで呼び出し6回、同じツールの連続3回、同じ引数2回、自動の繰り返し3回とし、
LLM の判断回数と1ターンの処理時間にも有限の上限を設けます。数値は実測で調整します。

ループの仕組み（モデルの呼び出し、ツール呼び出しの往復）はライブラリを使います（R2）。候補は Pydantic AI と、
MCP の公式 SDK です。採用するライブラリは実装の Epic で決めますが、次を満たすことを条件にします。

- 各ツールの実行の直前に Core の Gate を必ず通せる（ライブラリが Core を経由せずに外部へ送らない）。
- ループの間、ツール一覧を固定できる。
- 回数・時間の上限を Core が設定できる。
- 推論先を Core の profile から渡せる（ライブラリが provider の選択や fallback を持たない）。

ツールの判断に使う推論先は、キャラクターの profile とは別の「ツール判断用 profile」として設定します（J5）。
構造化出力に対応した profile に限ります。設定がなければ、ツール利用は無効のまま通常の会話を続けます（PoC と同じ）。
応答の文面はキャラクターの profile で生成し、人格の組み立ては [ADR 0026](0026-prompt-assembly.md) に従います。

呼び出し側（外部クライアント）が渡す function tools をそのまま返す既存の動作（ADR 0001）は、互換のため残します。
Core が持つツールと呼び出し側のツールを1つの要求で混ぜる扱いは、実装の Epic で決めます。

### 2. 会話外の活動は、判断と状態を Core が持ち、実行基盤だけを外に出す

Core が持つもの：

- 活動の判断に使う人格・記憶・生活状態（Life State）と、キャラクターとしての判断（Core の LLM と人格の組み立てを使う）。
- Autonomy Target（自律で使ってよい接続）の管理、High Impact の分類と確認、実行結果の状態
  （`APPLIED`、`NO_CHANGE`、`DEFERRED`、`SUPERSEDED`、`CONFLICT`、`REJECTED`、`FAILED`、`RESULT_UNKNOWN`）。
- 活動から会話への報告の経路。活動は会話へ直接話さず、報告を Core に残し、会話側がターンの区切りで話す（PoC の会話と活動 §3）。
- 活動の経験を記憶へ入れる経路（既存の形成・privacy・失効の契約を通す）。

外に出すもの（実行基盤）：

- 活動のループを回す汎用 Agent。Agent は Core の MCP だけを使い、外部への作用はすべて Core の MCP を経由して Gate を通ります。

実行基盤の条件（製品名は定めない）：

1. 許可方式（allowlist）で、Agent 単位に使えるツールを Core の MCP だけに限定できる。組み込みツールや他の MCP を名前で
   禁止する方式（denylist）ではなく、許可したもの以外は使えない方式とする。利用者単位・project の MCP と hook も読み込まない。
2. その限定を、権限確認なしのモードでも設定（起動時の指定）で強制できる。
3. 開発用の Agent や、同じ環境の他の Agent の設定に影響しない。
4. 起動時に、実際に使えるツールの一覧が許可した集合と一致することを確かめられる。一致しなければ活動を始めない
   （許可方式でも、念のため照合する）。
5. Agent 自身は外部サービスの認証情報を持たない。外部への接続は Core の MCP の側で行う。

候補の確認結果は[証跡](../evidence/2026-10-11-agent-tool-restriction.md)のとおりです。Claude Code の CLI は、
組み込みツールをすべて無効にする指定と、指定した MCP だけを使い他の設定ファイル（hook を含む）を読まない指定の組み合わせで、
1〜3 を満たしました（サブスクリプションの認証で動作）。一方、T3 アプリから起動する場合はこれらの指定を渡す項目が見当たらず、
T3 経由では 1 を満たせない見込みです（T3 アプリ経由の確認は未実施）。

推論資源の優先順位は [ADR 0020 §7](0020-reflection-personality-relationship.md#7-優先順位と実行基盤) のとおり
「前景の会話 > ユーザーが依頼した活動 > 自律的な背景処理」とします。

### 3. 決まった定期処理は timer から Core の API を呼ぶ

記憶の形成・内省など、手順が決まっている定期処理はプロンプトを使いません。OS の timer 等が Core の API（drain）を呼び、
成功か失敗かを受け取るだけにします。キャラクターの判断が必要な部分は、Core の中で Core の LLM を使います。

重複の防止と再試行は Core が持ちます。Core は job の台帳（永続予約と状態）と、有限の範囲を処理する drain API を提供し、
同じ job を二重に処理しません。中断した処理を途中から再開する仕組み（checkpoint）は持ちません。途中で止まった job は
次の呼び出しで最初からやり直し、冪等性で結果の重複・食い違いを防ぎます。同じ入力で何度実行しても、正本の結果が
重複したり食い違ったりしないことを要件にします。timer は呼ぶだけで、状態を持ちません。
外の最小 worker は置きません。

### 4. Core に MCP の入口を置く

PoC は「Core 側に MCP サーバー等の汎用の受け口は置かない」としていました
（[目標構成 §5](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/companion-target-architecture-2026-09.md#5-外部から呼ばれる入口)）。
これを変え、会話外の活動の Agent が使う MCP の入口を Core に置きます。理由は、R5 の条件
（Agent のツールを Core の MCP だけに限定し、Gate を必ず通す）を満たすには、Agent が使う操作を Core が MCP として提供する必要があるためです。

MCP の入口は [ADR 0025](0025-shared-runtime-interfaces.md) の共通 runtime を呼ぶ入口の1つで、HTTP と同じく localhost に限ります。
MCP で提供する操作は、Gate・流出防止・High Impact の確認を通る操作だけです。

### 5. PrivateAgent は使わない

PrivateAgent の開発を中止します。PrivateAgent に任せていたことは次のとおり置き換えます。

| 以前の担当（PrivateAgent） | 置き換え先 |
| --- | --- |
| 形成 job の起動・スケジュール・再試行・待機と再開（ADR 0017 §4） | timer が Core の drain API を呼ぶ。重複の防止と再試行は Core の job の台帳。中断した job は途中から再開せず、次回に最初からやり直す（冪等性で担保、§3） |
| Autonomy Target、High Impact の確認、実行結果の状態（ADR 0020 §6） | Core（§2） |
| orchestration・queue・scheduling・checkpoint（ADR 0020 §7） | 定期処理は Core の job の台帳。checkpoint は持たず、冪等なやり直しで担保する。活動のループは汎用 Agent（§2・§3） |

## 置き換える決定

この ADR が Accepted になった時点で、次に置換注記を付けます（本文は書き換えない）。

- [ADR 0001](0001-character-inference-api.md)：「Agent framework は tool 実行ループを持たない Core に不要」「Core で実行しない」。
- [ADR 0002](0002-ollama-native-chat.md)・[ADR 0003](0003-local-llamacpp-provider.md)：Core が tool 実行を持たないことを前提とした記述。
- [ADR 0004](0004-conversation-history.md)：「Core に tool 実行ループを追加しない」。
- [ADR 0007](0007-memory-provenance-and-revocation.md)：「常駐 worker、scheduler、Core 内 tool 実行ループは追加しない」。
- [ADR 0017 §4](0017-memory-formation-admission.md#4-会話からの非同期形成)：PrivateAgent による実行。
- [ADR 0020 §6・§7](0020-reflection-personality-relationship.md#6-会話外の活動との境界)：PrivateAgent が実行し、実行時の契約を持つこと。
- [ADR 0025](0025-shared-runtime-interfaces.md)：「外部 Agent が tool 実行を担当する」。
- README の「製品の範囲」、[推論 API](../api.md)、CONTEXT の PrivateAgent、SPEC の該当行。

## 範囲外

- ライブラリの組み込み、Core の MCP サーバー、ツールループ、job の台帳の拡張の実装（別の Epic で行う）。
- 複数入口の公開範囲と相手の識別（Issue #77）。

## 影響

- Core がツールループと Gate を持つため、会話中のツール利用でも人格・流出防止・承認を Core で保証できます。
  代わりに、ライブラリの更新時に Gate を経由することの回帰検証が必要になります。
- 会話外の活動の判断と状態が Core に集まり、実行基盤を差し替えても本人らしさの部分は変わりません。
- 実行基盤は許可方式でツールを限定するため、CLI の更新でツールが増えても漏れません。念のため起動時のツール一覧も照合します。
- T3 アプリから起動する Agent は、許可方式の指定を渡せない見込みのため、この条件を満たす起動方法（CLI を直接起動する等）を実装の Epic で選びます。
- PrivateAgent の中止により、ADR 0017・0020 の実行主体の記述を置き換える必要があります。
