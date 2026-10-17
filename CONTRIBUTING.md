# 開発規約

## 文書の正本と言語

READMEを入口として製品の範囲を置き、CONTEXTにはドメインの用語・概念モデル（用語集）を置きます。
機能の要件・実装状況・受入条件はSPECに置きます。開発規約の正本はこの文書です。
AGENTSは規約を重複させず、この文書へ案内します。
人間向けの文書とPR本文は日本語で書いてください。証跡・ログは英語でも構いません。
識別子、コマンド、schemaフィールド、出典原文は必要に応じて元の表記を維持します。

アーキテクチャの決定と理由は番号付きADRに記録し、レビュー前の提案はProposedとします。
作業範囲・受入条件・進捗はIssue、正確なrevision・コマンド・環境・結果・制約は
日付付き証跡で管理し、PRからリンクします。会話や進捗メモを承認済み仕様として扱いません。

## 変更とレビュー

最新の`main`から`epic/*`を作り、そのepicから別worktreeと作業branchを作ります。push前にremoteと
リポジトリの識別情報を確認します。コミットは範囲を絞り、レビュー可能な単位にします。
作業branchはfeature/fix/docs/infra/character等の目的別名とし、対応するepic宛てのDraft PRに
Issueを紐付けます。作業branchからmainへの直接PRは作りません。
作業PR→epicは最終headのCIグリーン、epic→mainは最終headのCIグリーンと実際のCodeRabbit
レビュー指摘への対応が必要です。botの概要・review disabled通知はレビュー完了ではありません。
blocking指摘が未解決ならマージしません。ユーザーから当該作業のマージ許可がある場合のみ
これらを満たしてマージし、mainの正確なcommitとpost-merge CIも確認します。
独立reviewはCodeRabbitの代用ではありません。一般の作業に無条件のマージ権限を与える規約では
ありません。別途の決定なしにリポジトリの
セキュリティ設定、branch protection、ruleset、GitHub Appを変更しません。
この規約から新たな承認権限を推定しないでください。

3層方針は[PoC（非公開）の規約](https://github.com/FYuki/digital-souls-poc/blob/7a11b2d9883b8bc2f6fcc53c6187d78e445d4b3c/docs/repository-policy.md)
を参照し、Coreのユーザー指示へ合わせています。音声・PoC固有規則は移植しません。
CodeRabbitは[設定](.coderabbit.yaml)で日本語/assertiveとrepo規約参照を指定します。
小規模public repoではmanual reviewが必要な場合があります。main宛てPRをレビュー可能にして
`@coderabbitai full review`を一度依頼し、実際のreview内容・対象head・適用設定を確認します。
rate limitなら通知されたretry/reset時刻を待ち、連投しません。epicの自動review範囲を増やしません。

## アーキテクチャとプライバシー

schema・contracts・domainの振る舞いは、プロバイダーSDK、通信クライアント、
ストレージadapterに依存させません。adapterはportを実装し、内側へ依存できます。
シリアライズするデータを変更する前にcontractの互換性を定義・検証してください。
プロバイダー選択と認証情報はdomainの振る舞いの外側で扱います。

プライバシー判断はfail-closedとします。ポリシーが欠落・不正・判定不能の場合は
保存・検索・送信を拒否してください。各境界でポリシーを適用します。
プロンプトは強制機構ではありません。認証情報や秘密をプロンプト、fixture、ログ、
コミットに含めないでください。テストには合成データを使います。
該当機能を実装したら、拒否経路、記憶の訂正・削除、adapterの境界を検証します。

## 再現可能な初期検証

`.node-version`と一致するNode **24.19.0**を用意してください。
検証にパッケージのインストール、LLM認証情報、外部通信、GPUは不要です。
Nodeは文書検証ツールの実行環境であり、製品の実装言語を選定したものではありません。
検証ツールは組み込みモジュールのみを使うため、文書検証は追加パッケージを使いません。回答評価のみ、`core/evals/semantic/package-lock.json` の固定依存を使います。

```sh
node --test --test-reporter=./tools/required-tests-reporter.mjs tools/check-docs.test.mjs tools/required-tests-reporter.test.mjs
node tools/check-docs.mjs
git diff --check
```

`Bootstrap checks / docs-tooling`はpathフィルターを設けず、すべてのPRと
mainおよびepicへのpushで実行します。追跡対象Markdownのローカル参照先ファイル、テキストの
空白・改行、JSON構文、検証ツール自体の回帰テストを確認します。
アンカーの存在や外部URLは検証しません。製品の正しさを保証するものでも、
包括的な秘密情報スキャナーでもありません。ステージした内容の機密情報を確認してください。
サンプルmanifestでは、格納したファイルのSHA-256・サイズ・ローカルパスを検証します。

必須テスト用reporterは、skip・TODO・cancelled・失敗、空suite・空ファイル、
ファイル単位のsummary欠落を拒否します。Nodeが空ファイルに付ける見かけのPASSは
登録されたテストとして数えません。固定したNodeでreporterのfixtureテストを実行し、
Node更新時にはイベントとsummaryの挙動を再検証してください。

CIの権限はcontentsの読み取りのみです。公式ActionsをSHAで固定し、secrets、
`pull_request_target`、キャッシュは使いません。固定ツールの更新はレビューを経て行います。

## 製品の品質ゲート

製品コードを受け入れる前に、言語・toolchainをADRで合意し、開発依存の正確なバージョン、
コミットされたlockfile、lockを変更しないインストール手順を追加します。
lint、format-check、静的型検査、UT、IT1、パッケージのbuild・install確認を行う
実際のコマンドを整備します。Coreのコマンドは`core/`で実行します。[推論API契約](docs/api.md)と[ADR 0001](docs/adr/0001-character-inference-api.md)に
初期Python実装の境界を記載しています。固定したuv 0.8.22で以下を実行します。

```sh
cd core
uv sync --frozen
uv run --no-sync ruff check src tests tools/evaluate-semantic-retrieval.py tools/prepare-semantic-tuning-supplements.py tools/evaluate-semantic-embedding-candidates.py evals/semantic/provider.py tools/evaluate-persona-consistency.py tools/evaluate-injection-resistance.py tools/evaluate-privacy-classifier.py
uv run --no-sync ruff format --check src tests tools/evaluate-semantic-retrieval.py tools/prepare-semantic-tuning-supplements.py tools/evaluate-semantic-embedding-candidates.py evals/semantic/provider.py tools/evaluate-persona-consistency.py tools/evaluate-injection-resistance.py tools/evaluate-privacy-classifier.py
uv run --no-sync mypy
uv run --no-sync pytest -m ut -q
uv run --no-sync pytest -m it1 -q
uv build --no-build-isolation
```

wheelの独立環境へのlocked installとimport確認は[API CI](.github/workflows/api.yml)に定義します。
`API checks / api-quality`も全PRとmain/epicへのpushで実行します。UT/IT1はsocketを禁止し、
pytest pluginでskip/xfail/xpass/0件を失敗にします。依存取得にはネットワークが必要ですが
テスト実行に実LLM・APIキー・GPUは不要です。
空の成功ジョブ、テスト0件でのPASS、dummy testを作らないでください。

UTとIT1は外部通信・実LLMを使わない決定的なテストとします。
UTは単体、IT1はfakeやローカルfixtureを使うプロセス内の結合を検証します。
IT2とSTは明示的に承認された実環境でのみ実行してください。
プロバイダー、環境、revision、日付、機密情報を除いた入力・結果、関連する費用・制約を
証跡として残し、mockでの実行を実環境の合格として扱いません。
通常のセットアップは実LLMやGPUなしで使える状態を維持します。

必須テストの失敗・skip・NOT RUNをPASSにしてはいけません。
不足する検証範囲を説明し、必須ゲートが通るまでは準備完了としないでください。
runnerを追加する際は、必須テストの検出が0件なら失敗にします。
required checkの名前を安定させ、path条件や実行条件でpendingのまま残さないでください。

## 所有者が判断するリポジトリ設定

workflowをレビューした後、`docs-tooling`の必須化、PRレビューの必須化、
mainへの直接pushの禁止を検討してください。保護を有効にする前に、
bypass権限とforkでの挙動を確認します。今回、これらの設定は変更していません。


## PostgreSQL adapter の合成契約テスト

保存backendはPostgreSQLへ一本化します（[ADR 0021](docs/adr/0021-postgresql-only-storage.md)）。
SQLiteは撤去済みです。保存を使う試験はPostgreSQLの合成試験で検証します。

PostgreSQL を変更する場合は `core/` で `bash tools/test-postgres.sh` も実行します。専用の
`postgres` marker は実 DB プロセスを使うため、プロセス内の UT / IT1 と分離します。
公式 PostgreSQL 18 イメージを digest 固定し、ネットワークなし・公開ポートなしの
使い捨てコンテナへ Unix socket で接続します。取得時のみネットワークが必要です。
DB は合成データだけで、実環境の認証情報やデータは使いません。実行後は削除します。
CI の `postgres-storage` は全 PR と main / epic push で必須試験を実行し、0 件・skip は
成功にしません。libpq の C 実装は pytest-socket の対象外なので、通信先の隔離はコンテナの
ネットワーク無効化と明示的な Unix socket 接続で保証します。
このテストの成功を実運用・実モデルの IT2 / ST の合格とは扱いません。
設定・制約は [PostgreSQL の利用境界](docs/postgresql.md) を参照してください。

## 回答評価の固定開発依存

[回答評価](core/evals/semantic/README.md)は `evals/semantic` 内の promptfoo **0.117.2** だけを
直接追加依存とし、製品のPython依存へ含めません。`npm ci --no-audit --no-fund`
でlock通りに取得します（取得にはネットワークが必要）。取得後のfixture評価は外部通信・実モデルを使いません。
promptfooの内部DB依存はNode 24のGCクラッシュを避けるため `better-sqlite3 13.0.3` に固定overrideします。
同梱N-API bindingを使い、`evals/semantic/.npmrc` の `ignore-scripts=true` で依存のinstall scriptを無効にします。

```sh
cd core
(cd evals/semantic && npm ci --no-audit --no-fund)
node --test --test-reporter=../tools/required-tests-reporter.mjs tools/semantic-answer.test.mjs
bash tools/evaluate-semantic-answer.sh --runs 3 --output /dev/shm/semantic-answer-report.json
```

`postgres-storage` 内で回答用JS試験と62ケース×3回のfixture gateを必須にします。
raw export・promptfooの内部DBは0700の一時領域で生成し、終了時に削除します。
公開する証跡は本文を含まないreportだけです。実モデル実行は手動で明示profileと実行flagを必要とし、
fixture評価の成功をモデル品質の証拠にしません。
