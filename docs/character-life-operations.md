# Character Life Runtimeの開発・検証手順

#249の実行基盤はDBOSを使用する。採用理由は
[Runtime選定ADR](decisions/character-life-runtime-2026-09.md)、意味・権限の正本は
[Character Life共通契約](decisions/character-life-memory-personality-autonomy-2026-09.md)を参照する。

## 有効化

既定は無効。今回の受入対象はdev/testのみであり、dogfoodへ配備しない。
作業worktreeのPython環境へ`backend/requirements.txt`をインストールする。
既存の`DS_ENVIRONMENT_ID` / `DS_DATA_DIR`による環境分離を維持し、他の環境のDBを流用しない。

`backend/.env`へ次を設定する。APIキーはGitへ追加せず、Issue・ログ・チャットへ貼らない。

```dotenv
DS_CHARACTER_LIFE_ENABLED=true
DS_CHARACTER_LIFE_CRON=*/30 * * * *
INFERENCE_TARGET_CHARACTER_LIFE=ollama/gemma4:e4b
INFERENCE_TARGET_CHARACTER_LIFE_MAX_INPUT_TOKENS=12000
INFERENCE_TARGET_CHARACTER_LIFE_MAX_OUTPUT_TOKENS=1024
INFERENCE_TARGET_CHARACTER_LIFE_TIMEOUT_SECONDS=60
INFERENCE_TARGET_CHARACTER_LIFE_OPTIONS_JSON={"temperature":0}
DS_MCP_CONFIG=/absolute/path/to/worktree/backend/config/character-life-elyth.example.json
ELYTH_API_KEY=取得したAPIキー
```

`character-life`推論Targetはローカルproviderに限定する。chat/privacy等の既存必須Targetの設定も必要。
ELYTHキーの参照名は`ELYTH_API_KEY`であり、MCP設定には値ではなく`secret_ref`を記述する。
接続先は公式の`https://elythworld.com/api/mcp/remote`。標準のBackend起動手順でlifecycleを開始する。

## 状態・許可・実行API

操作APIはFrontendと同じ`Origin`（既定`http://localhost:5173`）を必須とする。
APIキーを操作APIへ送らない。キャラクターは既存のCharacter Cardで検証する。

| 操作 | HTTP endpoint | 入力 |
|---|---|---|
| 状態・許可・実行履歴 | `GET /character-life/miori` | なし |
| 状態作成 | `POST /character-life/miori/states` | `kind`, `content`, 任意の`target_id` / `binding_target_id` |
| 状態変更・完了・休眠 | `PUT /character-life/miori/states/{id}` | `revision`, `content`, `status`, 任意の`target_id` / `binding_target_id` |
| 状態のrevision履歴 | `GET /character-life/miori/states/{id}/history` | なし |
| ELYTH自律許可・取消し | `PUT /character-life/miori/targets/elyth` | `enabled: true/false` |
| 利用者からの実行要求 | `POST /character-life/miori/activities` | `state_id`, `request_id`（UUIDv4） |
| 実行停止 | `POST /character-life/miori/activities/{id}/pause` | なし |
| 停止・保留からの再開 | `POST /character-life/miori/activities/{id}/resume` | なし |
| 内省からの状態形成要求 | `POST /character-life/miori/formation` | `request_id`（UUIDv4） |
| 監査 | `GET /character-life/miori/audit?after=0` | 最後に読んだsequenceを次のafterに指定 |

話題探索には`GOAL_INTENTION`、`target_id: "elyth"`、例えば
「ELYTHで今話題の公開情報を読み、次の会話で共有できる話題を一つ探す。」を登録し、ELYTHを許可する。
Interestだけでは外部実行しない。状態の編集時は取得したrevisionを送る。競合は409となる。
同じrequest_idの再送は同じ実行を返す。別の目的への流用は409となる。
未接続・利用者回答待ち・privacy判定不能・foreground優先は保留結果となり、成功扱いしない。

状態・実行の一覧はそれぞれ直近200件・100件。内省形成jobもcharacter単位で直近100件の結果を表示する。監査はsequence順に100件ずつ取得する。
停止済み実行の再開では新しいattemptを発行し、旧attemptからの遅延結果を採用しない。
意図・許可のrevisionが変わった実行はそのまま再承認せず、新しいrequest_idで要求する。

## 保存・復旧・終了

`DS_DATA_DIR`配下に以下を作成する。

- `character-life.db`: Life State、revision履歴、connection単位のAutonomy Grant、実行結果、形成の重複排除、監査。
- `character-life-system.sqlite`: DBOS workflow / queue / schedule / stepの管理情報。

DBOSへ渡すactivity引数は実行IDとattemptで、外部本文・MCP arguments・APIキーをcheckpointへ保存しない。
監査は外部execution ID・候補ID・引数fingerprint・結果を記録し、native payloadを複製しない。
Life Stateの共有候補と活動完了は同じSQLite transactionで確定する。
#100呼出し前に承認済み観測のhandoffを作業記録として固定し、復旧時は同じrun_id・時刻・本文・sourceを再送する。
handoffにも所有characterを持ち、保存・再開時にRunと照合する。schema v2への起動時移行では既存Runの所有者を補完する。
#100/#101の接続先はこのkeyで冪等に受け付ける。これはSELF EpisodeやPersonalityの正本を代替しない。
接続済みportのFAILED / RESULT_UNKNOWN / DEFERREDは依存結果へ残し、活動をDEFERREDにして同じhandoffをresumeから再送できるようにする。
port未指定（None）は後続Epicが未接続であることを表し、依存結果をDEFERREDのまま表示して話題共有だけを完了できる。接続済みportの一時的な保留とは区別する。
DBOSによる再投入抑止を外部副作用のexactly-once保証とは扱わない。

cronはUTC。missed runのbackfillは無効で、古いscheduleの回復時にも過去の活動を捏造しない。
登録済みキャラクターの内省形成要求と、許可済みの有効なGoal Intentionを定期scanする。
DBOS stepの実行コンテキストはアプリ側event loopへ持ち込まず、活動・形成を正本の安定IDを持つ独立workflowとして投入する。
queueの同時実行数は1で、利用者要求、自律活動、内省形成の順に優先する。
同じGoalへの未完了の自律要求と同じcharacterへの未完了の内省形成を重ねない。活動の未完了queue上限はcharacterごとに100件。
foreground会話がある場合は、認知の前後と外部dispatch直前に活動を保留する。
内省からの状態形成も処理境界で会話・利用者要求を再確認し、優先作業が発生した場合は後続の推論・保存を保留する。形成推論へキャンセルtokenを伝播し、優先度変更・timeout・終了時には結果を採用しない。
実行中の推論を会話開始と同時に強制preemptする設定はない。

通常終了では新規活動を止め、進行中の読取・推論結果を破棄して保留状態を確定してからDBOSとMCPを閉じる。
Backend停止後、両DBを同じ時点の組としてbackupする。Character Lifeの正本はWALを使うため、未checkpointのWALが残る場合はSQLite backup APIで取得する。稼働中の単純なファイルコピーは使わない。
無効化時は`DS_CHARACTER_LIFE_ENABLED=false`でBackendを再起動する。DB・provenanceは保持する。
DBOSのSQLiteは今回のdev/test受入に使用し、運用導入時のDB・長時間運転条件は別途検証する。

## 関連Epicとの接続

| 境界 | 今回の挙動 | 後続の実装責務 |
|---|---|---|
| `MemoryPort` | 本人観測の保存・catch-upは`DEFERRED` | #100のSELF / experienced_at / Memory admission / Episode形成 |
| `ReflectionSource` | 正本未接続は`None`として形成を保留 | #100のACTIVE Reflection形成対象batch（最大16件）・完全な同期revision集合・訂正/非公開化通知 |
| `LifeFormation` | 提供された正本projectionのcharacter・source・revision・privacyを検証してLife Stateを確定 | #249。#100の永続modelを複製しない |
| `PersonalityPort` | `DEFERRED` | #101の証拠・閾値・bounded更新・重複排除 |
| Skill | 自動学習・正本更新なし | #102 |
| write / High Impact / recovery | 未接続。今回のELYTH受入は公開情報の閲覧のみ | #185の影響分類・確認・不明結果回復 |

connectionへのGrantは会話での利用許可と別であり、schema変更でもconnection identityが同じなら保持する。
今回の話題探索は公開情報の5操作に限定し、DM・投稿・通知の既読変更・Field操作を選ばせない。
サンプル接続の`core_policy.operation_allowlist`は、CatalogとExecution Gateの共通制限として会話側にも適用する。
未列挙の新しいTool・Resourceは拒否する。これは管理設定による制限で、connection単位の自律Grantや他の接続の既定動作は変更しない。
非信頼annotationをreadや自動retryへ昇格させない。通常writeを恒久禁止する方針変更ではなく、
#185接続前の制限として扱う。Webサービスは登録済みMCPを介して利用する。直接Web adapter・self-owned Addonは未接続で、#221等の基盤完成後に既存Execution Gate境界へ接続する。直接HTTPで迂回実行しない。
MCPのResource読取と登録済みBindingを再利用する。複数Bindingから選ぶ場合は状態の`binding_target_id`へ管理側のIDを指定する。不足は`binding_input_required`で保留する。

共有候補の`APPLIED`はLife State保存の成功を表す。SELF Episode・Reflection・人格・Skill全体の統合完了を表さない。
Reflection由来の状態は元sourceのrevisionを保存し、会話・実行直前に正本metadataを照合する。
訂正・非公開化は件数制限なしで依存状態を休眠化し、正本取得不能時は派生状態を利用しない。
関連Epic未実装の状態で、共通ADRの全体シナリオが完了したとは報告しない。

## テスト

通常テストでは外部サービスを呼ばない。

```bash
python -m pytest backend/tests/unit/test_character_life_store.py backend/tests/unit/test_character_life_prompt.py backend/tests/module/test_character_life.py backend/tests/module/test_character_life_api.py -q
```

実接続受入はELYTH_API_KEYを実行環境で解決したうえで、明示的に有効化する。
pytestは開発者の`.env`を自動読込しない。秘密を表示せず環境へ渡すこと。
テストは一時data rootを使用し、dogfood指定時は既存fixtureが拒否する。

```bash
export DS_ENVIRONMENT_ID=test
export DS_DATA_DIR="$(mktemp -d /tmp/character-life-test.XXXXXX)"
RUN_CHARACTER_LIFE_REAL_TESTS=true \
CHARACTER_LIFE_ACCEPTANCE_REPORT="$DS_DATA_DIR/runtime/character-life-elyth-result.json" \
python -m pytest backend/tests/integration/test_character_life_elyth_integration.py -q
```

起動ごとに新しいtest data rootを用意し、レポートの上書きを避ける。pytest fixtureは各ケースのDBをさらに独立した一時rootへ分離する。

実接続テストでは登録済みDBOS scheduleを発火してrequested=falseの自律活動を起動し、
ローカルOllama、ELYTH、実socketのHTTP受付、同一要求の重複排除、
次の会話へのLife State projectionと実モデル応答、終了処理を確認する。時計による実cron発火は、外部をmockした実DBOSのモジュールテストでも確認する。
会話の出力上限は通常の.env.exampleと同じ1,024トークンとする。512では思考出力だけで上限へ達し、本文が空になるケースを確認した。
レポートへ本文・キー・argumentsを保存せず、結果・操作名・schema検証種別・時間・会話出力上限を記録する。

## 前景と背景の実測と制限

`RUN_CHARACTER_LIFE_PRIORITY_BENCHMARK=true`を実接続受入に追加すると、同じ実Ollamaへ背景認知と会話を重ね、TTFT・GPU使用量・foreground中のdispatch件数を記録する。
2026-09-08のgemma4:e4b（会話出力上限1,024）による直近の単一試行では、通常TTFT 8.950秒、重複時4.753秒、GPU使用メモリ最大9,815 MiB、使用率最大86%だった。
先行試行では重複時の遅延増加も観測しており、推論内容・cache等による変動を含む。この1試行で背景負荷の性能改善を主張しない。
背景活動はDEFERREDとなり、foreground開始後の外部dispatchは0件。通常の強制pause/resumeとshutdownは別の自動テストで検証する。

この測定はテキスト会話・一つのローカル環境の観測であり、音声streamや配信、異なるGPUの性能保証ではない。
現行Inference Routerのcancelは結果の不採用を保証し、送信済み同期推論のGPU処理を強制停止しない。
今回のdev/testでは処理境界での保留を維持し、強制preemptionは導入しない。共有推論serverでは会話の遅延が増える場合がある。
運用導入は今回の範囲外で、推論配置と音声・複数キャラクター・長時間負荷を含めて別途受入する。
